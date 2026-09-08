from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dupefinder.db import Database
from dupefinder.scanner import ScannerEngine


class ScannerTests(unittest.TestCase):
    def test_finds_duplicates_and_scans_new_folders_before_repeating(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            first_folder = root / "a"
            second_folder = root / "b"
            first_folder.mkdir(parents=True)
            second_folder.mkdir()
            content = b"same" * 40_000
            first = first_folder / "old.bin"
            second = second_folder / "new.bin"
            first.write_bytes(content)
            second.write_bytes(content)
            now = time.time()
            os.utime(first, (now - 100, now - 100))
            os.utime(second, (now, now))

            database = Database(Path(temporary) / "state.db")
            result = ScannerEngine(database, root, 100 * 1024).run()
            groups = database.list_duplicate_groups(root)

            self.assertFalse(result.paused)
            self.assertEqual(len(groups), 1)
            self.assertEqual({file.path for file in groups[0].files}, {str(first), str(second)})
            self.assertEqual(groups[0].oldest_file.path, str(first))

            new_folder = root / "c"
            new_folder.mkdir()
            (new_folder / "third.bin").write_bytes(content)
            second_result = ScannerEngine(database, root, 100 * 1024).run()

            self.assertEqual(second_result.folders_completed, 1)
            self.assertEqual(len(database.list_duplicate_groups(root)[0].files), 3)

    def test_scan_can_pause_without_marking_partial_folder_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            root.mkdir()
            (root / "file.bin").write_bytes(b"x" * 120_000)
            database = Database(Path(temporary) / "state.db")

            result = ScannerEngine(database, root, 100 * 1024, should_stop=lambda: True).run()
            self.assertTrue(result.paused)
            self.assertFalse(database.scan_results_valid(root))

            resumed = ScannerEngine(database, root, 100 * 1024).run()
            self.assertEqual(resumed.folders_completed, 1)
            self.assertTrue(database.scan_results_valid(root))

    def test_scanning_starts_before_folder_discovery_finishes(self) -> None:
        class SlowDiscoveryScanner(ScannerEngine):
            @staticmethod
            def _walk_folders(root: Path, onerror):
                folders = list(ScannerEngine._walk_folders(root, onerror))
                for index, folder in enumerate(folders):
                    yield folder
                    if index == 0:
                        time.sleep(0.2)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            (root / "child").mkdir(parents=True)
            (root / "first.bin").write_bytes(b"x" * 120_000)
            (root / "child" / "second.bin").write_bytes(b"y" * 120_000)
            database = Database(Path(temporary) / "state.db")
            progress: list[tuple[int, int, bool, str]] = []

            result = SlowDiscoveryScanner(
                database,
                root,
                100 * 1024,
                on_progress=lambda current, total, complete, folder: progress.append(
                    (current, total, complete, folder)
                ),
            ).run()

            self.assertFalse(result.paused)
            self.assertTrue(any(not complete for _current, _total, complete, _folder in progress))
            self.assertTrue(progress[-1][2])
            self.assertEqual(progress[-1][0], progress[-1][1])

    def test_discovery_error_preserves_cached_subtree_results(self) -> None:
        class IncompleteDiscoveryScanner(ScannerEngine):
            @staticmethod
            def _walk_folders(root: Path, onerror):
                yield str(root)
                onerror(PermissionError("simulated inaccessible subtree"))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            (root / "A").mkdir(parents=True)
            (root / "B").mkdir()
            content = b"same" * 40_000
            (root / "A" / "one.bin").write_bytes(content)
            (root / "B" / "two.bin").write_bytes(content)
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()
            self.assertEqual(len(database.list_duplicate_groups(root)), 1)

            with self.assertRaisesRegex(OSError, "cached results were preserved"):
                IncompleteDiscoveryScanner(database, root, 100 * 1024).run()

            self.assertEqual(len(database.list_duplicate_groups(root)), 1)
            self.assertFalse(database.scan_results_valid(root))

    def test_transient_entry_error_preserves_cached_file_records(self) -> None:
        class Entry:
            def __init__(self, path: Path, fail: bool = False) -> None:
                self.path = str(path)
                self._path = path
                self._fail = fail

            def is_file(self, follow_symlinks: bool = False) -> bool:
                return True

            def stat(self, follow_symlinks: bool = False):
                if self._fail:
                    raise PermissionError("simulated file lock")
                return self._path.stat()

        class ScandirContext:
            def __init__(self, entries) -> None:
                self.entries = entries

            def __enter__(self):
                return iter(self.entries)

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            root.mkdir()
            first = root / "first.bin"
            second = root / "second.bin"
            content = b"same" * 40_000
            first.write_bytes(content)
            second.write_bytes(content)
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()
            self.assertEqual(len(database.list_duplicate_groups(root)), 1)

            context = ScandirContext([Entry(first), Entry(second, fail=True)])
            engine = ScannerEngine(database, root, 100 * 1024)
            with patch("dupefinder.scanner.os.scandir", return_value=context):
                with self.assertRaisesRegex(OSError, "cached file records were preserved"):
                    engine._scan_folder(root)

            self.assertEqual(len(database.list_duplicate_groups(root)), 1)

    def test_transient_candidate_stat_error_preserves_cached_group(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            root.mkdir()
            content = b"same" * 40_000
            first = root / "first.bin"
            second = root / "second.bin"
            third = root / "third.bin"
            first.write_bytes(content)
            second.write_bytes(content)
            third.write_bytes(content)
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()
            engine = ScannerEngine(database, root, 100 * 1024)
            real_stat = Path.stat

            def transient_stat(path: Path, *args, **kwargs):
                if path == third:
                    raise PermissionError("simulated transient lock")
                return real_stat(path, *args, **kwargs)

            with patch("dupefinder.scanner.Path.stat", new=transient_stat):
                engine._process_size(len(content))

            group = database.list_duplicate_groups(root)[0]
            self.assertEqual(len(group.files), 3)


if __name__ == "__main__":
    unittest.main()
