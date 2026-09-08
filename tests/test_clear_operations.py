from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dupefinder.actions import ActionExecutor
from dupefinder.db import Database
from dupefinder.review import ReviewService
from dupefinder.scanner import ScannerEngine


class ClearOperationTests(unittest.TestCase):
    def _scan_three_groups(self, temporary: str) -> tuple[Database, Path, ReviewService]:
        root = Path(temporary) / "scan"
        root.mkdir()
        for prefix, content in (
            ("one", b"one" * 50_000),
            ("two", b"two!" * 45_000),
            ("three", b"three" * 40_000),
        ):
            (root / f"{prefix}-a.bin").write_bytes(content)
            (root / f"{prefix}-b.bin").write_bytes(content)
        database = Database(Path(temporary) / "state.db")
        ScannerEngine(database, root, 100 * 1024).run()
        return database, root, ReviewService(database)

    def test_clear_scan_results_removes_cache_and_cart_but_preserves_ignored_and_history(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, root, service = self._scan_three_groups(temporary)
            items = service.list_inbox(root)
            service.stage(items[0])
            ActionExecutor(database, lambda path: Path(path).unlink()).commit_all()
            service.invalidate(root)
            remaining = service.list_inbox(root)
            service.ignore(remaining[0])
            service.stage(remaining[1])

            database.clear_scan_results(root)

            self.assertEqual(database.list_duplicate_groups(root), [])
            self.assertEqual(database.list_files(root), [])
            self.assertEqual(database.list_action_batches(), [])
            self.assertEqual(len(database.ignored_review_keys(root)), 1)
            self.assertEqual(len(database.list_commit_history()), 1)

    def test_clear_ignored_returns_cached_item_to_inbox(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            root.mkdir()
            content = b"same" * 40_000
            (root / "first.bin").write_bytes(content)
            (root / "second.bin").write_bytes(content)
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()
            service = ReviewService(database)
            item = service.list_inbox(root)[0]
            service.ignore(item)
            self.assertEqual(service.list_inbox(root), [])

            database.clear_ignored(root)
            service.invalidate(root)

            self.assertEqual(len(service.list_inbox(root)), 1)

    def test_ignored_folder_rollup_survives_clear_and_identical_rescan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            for folder_name in ("Original", "Backup"):
                folder = root / folder_name
                folder.mkdir(parents=True)
                (folder / "one.bin").write_bytes(b"one" * 50_000)
                (folder / "two.bin").write_bytes(b"two" * 50_000)
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()
            service = ReviewService(database)
            item = service.list_inbox(root)[0]
            self.assertEqual(item.kind, "folder")
            service.ignore(item)

            database.clear_scan_results(root)
            ReviewService(database).list_all(root, reconcile=True)
            self.assertEqual(len(database.ignored_review_keys(root)), 1)

            ScannerEngine(database, root, 100 * 1024).run()
            reloaded = ReviewService(database)
            self.assertEqual(reloaded.list_inbox(root), [])
            self.assertEqual(len(reloaded.list_ignored(root)), 1)


if __name__ == "__main__":
    unittest.main()
