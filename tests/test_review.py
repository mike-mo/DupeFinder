from __future__ import annotations

import os
import sqlite3
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from dupefinder.db import Database
from dupefinder.review import (
    ReviewService,
    highlighted_path_html,
    timestamp_difference_flags,
)
from dupefinder.scanner import ScannerEngine


class MigrationTests(unittest.TestCase):
    def test_current_database_schema_is_migrated_without_losing_data(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "legacy.db"
            with closing(sqlite3.connect(database_path)) as connection:
                connection.executescript(
                    """
                    CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                    INSERT INTO settings(key, value) VALUES ('min_size_kb', '250');
                    CREATE TABLE duplicate_groups (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        root_path TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        full_hash TEXT NOT NULL,
                        discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TEXT,
                        UNIQUE (root_path, size, full_hash)
                    );
                    CREATE TABLE pending_actions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        group_id INTEGER NOT NULL UNIQUE,
                        root_path TEXT NOT NULL,
                        source_path TEXT NOT NULL,
                        target_path TEXT NOT NULL,
                        paths_json TEXT NOT NULL,
                        size INTEGER NOT NULL,
                        full_hash TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        last_error TEXT,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                    );
                    """
                )

            database = Database(database_path)

            self.assertEqual(database.get_setting("min_size_kb"), "250")
            with closing(sqlite3.connect(database_path)) as connection:
                columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(pending_actions)")
                }
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            self.assertIn("batch_id", columns)
            self.assertIn("review_key", columns)
            self.assertEqual(version, 3)


class DifferenceHelperTests(unittest.TestCase):
    def test_only_different_path_components_are_highlighted(self) -> None:
        paths = [
            r"C:\Photos\Originals\2024\image.jpg",
            r"C:\Photos\Backup\2024\image.jpg",
        ]

        first = highlighted_path_html(paths[0], paths)
        second = highlighted_path_html(paths[1], paths)

        self.assertIn("Originals", first)
        self.assertIn("Backup", second)
        self.assertIn("background-color", first)
        self.assertIn("background-color", second)


class ReviewServiceTests(unittest.TestCase):
    @staticmethod
    def _write(path: Path, value: bytes, modified: float) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        os.utime(path, (modified, modified))

    def test_identical_folders_roll_up_and_stage_to_one_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            now = time.time()
            self._write(root / "A" / "one.bin", b"one" * 50_000, now - 30)
            self._write(root / "B" / "one.bin", b"one" * 50_000, now - 20)
            self._write(root / "A" / "two.bin", b"two" * 50_000, now - 10)
            self._write(root / "B" / "two.bin", b"two" * 50_000, now)
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()

            service = ReviewService(database)
            items = service.list_inbox(root)

            self.assertEqual(len(items), 1)
            item = items[0]
            self.assertEqual(item.kind, "folder")
            self.assertEqual(item.relation, "identical")
            destination = str(root / "B")
            batch_id = service.stage(item, destination)
            batch = database.get_action_batch(batch_id)
            self.assertIsNotNone(batch)
            assert batch is not None
            self.assertEqual(len(batch.actions), 2)
            self.assertEqual(
                {action.target_path for action in batch.actions},
                {str(root / "B" / "one.bin"), str(root / "B" / "two.bin")},
            )
            self.assertEqual(service.list_inbox(root), [])

    def test_subset_folder_rollup_leaves_extra_file_out_of_batch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            now = time.time()
            for folder in ("A", "B"):
                self._write(root / folder / "one.bin", b"one" * 50_000, now)
                self._write(root / folder / "two.bin", b"two" * 50_000, now)
            self._write(root / "B" / "extra.bin", b"extra" * 30_000, now)
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()

            items = ReviewService(database).list_inbox(root)

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].kind, "folder")
            self.assertEqual(items[0].relation, "subset")
            self.assertEqual(items[0].file_count, 2)

    def test_ignored_and_staged_items_return_when_membership_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            now = time.time()
            self._write(root / "A" / "first.bin", b"same" * 40_000, now - 20)
            self._write(root / "B" / "different-name.bin", b"same" * 40_000, now)
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()
            service = ReviewService(database)
            item = service.list_inbox(root)[0]

            service.ignore(item)
            self.assertEqual(service.list_inbox(root), [])
            self.assertEqual(len(service.list_ignored(root)), 1)

            self._write(root / "C" / "third-name.bin", b"same" * 40_000, now + 10)
            ScannerEngine(database, root, 100 * 1024).run()
            service.invalidate(root)
            changed = service.list_inbox(root)
            self.assertEqual(len(changed), 1)
            self.assertEqual(service.list_ignored(root), [])

            batch_id = service.stage(changed[0])
            self._write(root / "D" / "fourth-name.bin", b"same" * 40_000, now + 20)
            ScannerEngine(database, root, 100 * 1024).run()
            service.invalidate(root)
            self.assertEqual(len(service.list_inbox(root)), 1)
            batch = database.get_action_batch(batch_id)
            self.assertIsNotNone(batch)
            assert batch is not None
            self.assertEqual(batch.status, "stale")

    def test_timestamp_difference_detection_suppresses_identical_modified_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            now = time.time()
            self._write(root / "a.bin", b"same" * 40_000, now)
            self._write(root / "b.bin", b"same" * 40_000, now)
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()
            files = database.list_duplicate_groups(root)[0].files

            modified_differs, _created_differs = timestamp_difference_flags(files)
            self.assertFalse(modified_differs)


if __name__ == "__main__":
    unittest.main()
