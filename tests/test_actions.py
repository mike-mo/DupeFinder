from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from dupefinder.actions import ActionExecutor, capture_originals
from dupefinder.db import Database
from dupefinder.review import ReviewService
from dupefinder.scanner import ScannerEngine


class ActionTests(unittest.TestCase):
    def _scanned_group(self, temporary: str) -> tuple[Database, Path, Path, Path]:
        root = Path(temporary) / "scan"
        root.mkdir()
        old = root / "old.bin"
        new = root / "new.bin"
        third = root / "third.bin"
        content = b"duplicate" * 20_000
        for path in (old, new, third):
            path.write_bytes(content)
        now = time.time()
        os.utime(old, (now - 300, now - 300))
        os.utime(new, (now - 200, now - 200))
        os.utime(third, (now - 100, now - 100))
        database = Database(Path(temporary) / "state.db")
        ScannerEngine(database, root, 100 * 1024).run()
        return database, old, new, third

    def test_newer_selection_moves_oldest_file_to_selected_location(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, third = self._scanned_group(temporary)
            group = database.list_duplicate_groups()[0]
            old_mtime = old.stat().st_mtime_ns
            old_ctime = old.stat().st_ctime_ns
            action = database.stage_action(group.id, new)
            trashed: list[str] = []

            def fake_trash(path: str) -> None:
                trashed.append(path)
                Path(path).unlink()

            result = ActionExecutor(database, fake_trash).commit(action)

            self.assertTrue(result.success)
            self.assertFalse(old.exists())
            self.assertTrue(new.exists())
            self.assertFalse(third.exists())
            self.assertEqual(new.stat().st_mtime_ns, old_mtime)
            if os.name == "nt":
                self.assertEqual(new.stat().st_ctime_ns, old_ctime)
            self.assertEqual(len(trashed), 2)
            self.assertEqual(database.list_pending_actions(), [])
            self.assertEqual(database.list_duplicate_groups(), [])

    def test_changed_file_blocks_commit_without_removing_anything(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, third = self._scanned_group(temporary)
            group = database.list_duplicate_groups()[0]
            action = database.stage_action(group.id, old)
            new.write_bytes(b"changed" * 30_000)

            result = ActionExecutor(database, lambda path: Path(path).unlink()).commit(action)

            self.assertFalse(result.success)
            self.assertTrue(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(third.exists())
            pending = database.list_pending_actions()
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0].status, "error")

    def test_cart_persists_and_partial_recycle_failure_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, _third = self._scanned_group(temporary)
            group = database.list_duplicate_groups()[0]
            action = database.stage_action(group.id, new)

            reopened = Database(database.path)
            self.assertEqual(reopened.list_pending_actions()[0].target_path, str(new))

            failure_injected = False

            def flaky_trash(path: str) -> None:
                nonlocal failure_injected
                if "dupefinder-replaced" in path and not failure_injected:
                    failure_injected = True
                    raise PermissionError("simulated Recycle Bin failure")
                Path(path).unlink()

            first_result = ActionExecutor(reopened, flaky_trash).commit(action)
            self.assertFalse(first_result.success)
            self.assertTrue(new.exists())
            retry_action = reopened.list_pending_actions()[0]
            self.assertEqual(retry_action.source_path, str(new))

            retry_result = ActionExecutor(reopened, lambda path: Path(path).unlink()).commit(retry_action)
            self.assertTrue(retry_result.success)
            self.assertEqual(reopened.list_pending_actions(), [])

    def test_commit_history_can_reconstruct_removed_duplicates(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, third = self._scanned_group(temporary)
            original_times = {
                path: (path.stat().st_atime_ns, path.stat().st_mtime_ns, path.stat().st_ctime_ns)
                for path in (old, new, third)
            }
            item = ReviewService(database).list_inbox(old.parent)[0]
            ReviewService(database).stage(item)
            executor = ActionExecutor(database, lambda path: Path(path).unlink())

            commit_results = executor.commit_all()

            self.assertEqual(len(commit_results), 1)
            self.assertTrue(commit_results[0].success)
            history = database.list_commit_history()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].status, "completed")
            self.assertTrue(old.exists())
            self.assertFalse(new.exists())
            self.assertFalse(third.exists())

            undo = executor.undo_commit(history[0].id)

            self.assertTrue(undo.success)
            for path in (old, new, third):
                self.assertTrue(path.exists())
                self.assertEqual(path.stat().st_mtime_ns, original_times[path][1])
                if os.name == "nt":
                    self.assertEqual(path.stat().st_ctime_ns, original_times[path][2])
            self.assertEqual(database.list_commit_history()[0].status, "undone")

    def test_undo_is_blocked_when_an_original_path_is_occupied(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, _third = self._scanned_group(temporary)
            item = ReviewService(database).list_inbox(old.parent)[0]
            ReviewService(database).stage(item)
            executor = ActionExecutor(database, lambda path: Path(path).unlink())
            executor.commit_all()
            history = database.list_commit_history()[0]
            new.write_bytes(b"unrelated")

            result = executor.undo_commit(history.id)

            self.assertFalse(result.success)
            self.assertEqual(database.list_commit_history()[0].status, "undo_error")
            new.unlink()

            retry = executor.undo_commit(history.id)

            self.assertTrue(retry.success)
            self.assertTrue(new.exists())

    def test_partial_recycle_failure_is_journaled_and_undoable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, third = self._scanned_group(temporary)
            item = ReviewService(database).list_inbox(old.parent)[0]
            ReviewService(database).stage(item, new)
            failure_injected = False

            def flaky_trash(path: str) -> None:
                nonlocal failure_injected
                if not failure_injected:
                    failure_injected = True
                    raise PermissionError("simulated Recycle Bin failure")
                Path(path).unlink()

            executor = ActionExecutor(database, flaky_trash)
            result = executor.commit_all()[0]

            self.assertFalse(result.success)
            history = database.list_commit_history()[0]
            self.assertEqual(history.status, "partial")
            self.assertTrue(any(item.status == "completed" for item in history.items))

            undo = ActionExecutor(
                database,
                lambda path: Path(path).unlink(),
            ).undo_commit(history.id)

            self.assertTrue(undo.success)
            self.assertTrue(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(third.exists())
            self.assertEqual(database.list_action_batches(), [])

    def test_partial_retry_coalesces_history_without_restoring_internal_claims(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, third = self._scanned_group(temporary)
            item = ReviewService(database).list_inbox(old.parent)[0]
            ReviewService(database).stage(item, new)
            failure_injected = False

            def flaky_trash(path: str) -> None:
                nonlocal failure_injected
                if not failure_injected:
                    failure_injected = True
                    raise PermissionError("simulated Recycle Bin failure")
                Path(path).unlink()

            ActionExecutor(database, flaky_trash).commit_all()
            self.assertEqual(len(database.list_commit_history()), 1)

            retry = ActionExecutor(
                database,
                lambda path: Path(path).unlink(),
            ).commit_all()[0]

            self.assertTrue(retry.success)
            history = database.list_commit_history()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].status, "completed")
            self.assertEqual(
                [file.path for file in database.list_files(old.parent)],
                [str(new)],
            )
            undo = ActionExecutor(
                database,
                lambda path: Path(path).unlink(),
            ).undo_commit(history[0].id)
            self.assertTrue(undo.success)
            self.assertTrue(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(third.exists())
            self.assertEqual(
                list(old.parent.glob(".*.dupefinder-replaced-*")),
                [],
            )

    def test_failed_retry_does_not_erase_previous_recovery_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, third = self._scanned_group(temporary)
            item = ReviewService(database).list_inbox(old.parent)[0]
            ReviewService(database).stage(item, new)
            failure_injected = False

            def flaky_trash(path: str) -> None:
                nonlocal failure_injected
                if not failure_injected:
                    failure_injected = True
                    raise PermissionError("simulated Recycle Bin failure")
                Path(path).unlink()

            ActionExecutor(database, flaky_trash).commit_all()
            before = database.list_commit_history()[0]
            self.assertEqual(before.status, "partial")
            self.assertEqual(before.items[0].status, "completed")

            with patch(
                "dupefinder.actions.os.replace",
                side_effect=PermissionError("simulated retry claim failure"),
            ):
                retry = ActionExecutor(
                    database,
                    lambda path: Path(path).unlink(),
                ).commit_all()[0]

            self.assertFalse(retry.success)
            after = database.list_commit_history()[0]
            self.assertEqual(after.status, "partial")
            self.assertEqual(after.items[0].status, "completed")
            undo = ActionExecutor(
                database,
                lambda path: Path(path).unlink(),
            ).undo_commit(after.id)
            self.assertTrue(undo.success)
            self.assertTrue(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(third.exists())

    def test_undo_finalization_failure_can_be_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, third = self._scanned_group(temporary)
            item = ReviewService(database).list_inbox(old.parent)[0]
            ReviewService(database).stage(item)
            executor = ActionExecutor(database, lambda path: Path(path).unlink())
            executor.commit_all()
            history_id = database.list_commit_history()[0].id

            with patch.object(
                database,
                "mark_commit_undone",
                side_effect=OSError("simulated final history update failure"),
            ):
                first = executor.undo_commit(history_id)

            self.assertFalse(first.success)
            self.assertTrue(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(third.exists())
            self.assertEqual(database.get_commit_history(history_id).status, "undo_error")

            retry = executor.undo_commit(history_id)

            self.assertTrue(retry.success)
            self.assertEqual(database.get_commit_history(history_id).status, "undone")

    def test_incomplete_retry_rollback_preserves_all_recovery_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, third = self._scanned_group(temporary)
            item = ReviewService(database).list_inbox(old.parent)[0]
            ReviewService(database).stage(item, new)
            failure_injected = False

            def partial_trash(path: str) -> None:
                nonlocal failure_injected
                if not failure_injected:
                    failure_injected = True
                    raise PermissionError("simulated Recycle Bin failure")
                Path(path).unlink()

            ActionExecutor(database, partial_trash).commit_all()
            real_replace = os.replace
            replace_calls = 0

            def broken_retry_replace(source, destination) -> None:
                nonlocal replace_calls
                replace_calls += 1
                if replace_calls in {2, 3}:
                    raise PermissionError("simulated claim/rollback failure")
                real_replace(source, destination)

            with patch("dupefinder.actions.os.replace", side_effect=broken_retry_replace):
                retry = ActionExecutor(
                    database,
                    lambda path: Path(path).unlink(),
                ).commit_all()[0]

            self.assertFalse(retry.success)
            history = database.list_commit_history()[0]
            self.assertEqual(history.status, "partial")
            self.assertEqual(history.items[0].status, "completed")
            undo = ActionExecutor(
                database,
                lambda path: Path(path).unlink(),
            ).undo_commit(history.id)
            self.assertTrue(undo.success)
            self.assertTrue(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(third.exists())
            self.assertEqual(
                list(old.parent.glob(".*.dupefinder-replaced-*")),
                [],
            )

    def test_timestamp_failure_after_publish_retries_to_selected_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, _third = self._scanned_group(temporary)
            item = ReviewService(database).list_inbox(old.parent)[0]
            ReviewService(database).stage(item, new)

            with patch(
                "dupefinder.actions.restore_timestamps",
                side_effect=OSError("simulated timestamp failure"),
            ):
                first = ActionExecutor(
                    database,
                    lambda path: Path(path).unlink(),
                ).commit_all()[0]

            self.assertFalse(first.success)
            pending = database.list_pending_actions()[0]
            self.assertEqual(pending.target_path, str(new))
            self.assertTrue(Path(pending.source_path).exists())

            retry = ActionExecutor(
                database,
                lambda path: Path(path).unlink(),
            ).commit_all()[0]

            self.assertTrue(retry.success)
            self.assertTrue(new.exists())
            self.assertFalse(old.exists())
            self.assertEqual(
                list(old.parent.glob(".*.dupefinder-replaced-*")),
                [],
            )

    def test_cross_volume_publish_copy_failure_leaves_no_partial_destination(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, _third = self._scanned_group(temporary)
            item = ReviewService(database).list_inbox(old.parent)[0]
            ReviewService(database).stage(item, new)

            def broken_copy(source, destination, length: int) -> None:
                destination.write(source.read(128))
                raise OSError("simulated out of space")

            with (
                patch(
                    "dupefinder.actions.os.rename",
                    side_effect=OSError("simulated cross-volume move"),
                ),
                patch("dupefinder.actions.shutil.copyfileobj", new=broken_copy),
            ):
                result = ActionExecutor(
                    database,
                    lambda path: Path(path).unlink(),
                ).commit_all()[0]

            self.assertFalse(result.success)
            self.assertFalse(new.exists())
            pending = database.list_pending_actions()[0]
            self.assertTrue(Path(pending.source_path).exists())
            self.assertEqual(
                list(old.parent.glob(".*.dupefinder-publish-*")),
                [],
            )

    def test_folder_batch_removes_empty_source_and_undo_recreates_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            source_folder = root / "Source"
            target_folder = root / "Target"
            source_folder.mkdir(parents=True)
            target_folder.mkdir()
            now = time.time()
            for index, name in enumerate(("one.bin", "two.bin")):
                content = name.encode("ascii") * 30_000
                source = source_folder / name
                target = target_folder / name
                source.write_bytes(content)
                target.write_bytes(content)
                os.utime(source, (now - 100 - index, now - 100 - index))
                os.utime(target, (now, now))
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()
            service = ReviewService(database)
            item = service.list_inbox(root)[0]
            self.assertEqual(item.kind, "folder")
            service.stage(item, target_folder)
            executor = ActionExecutor(database, lambda path: Path(path).unlink())

            results = executor.commit_all()

            self.assertTrue(results[0].success)
            self.assertFalse(source_folder.exists())
            self.assertTrue((target_folder / "one.bin").exists())
            history = database.list_commit_history()[0]

            undo = executor.undo_commit(history.id)

            self.assertTrue(undo.success)
            self.assertTrue((source_folder / "one.bin").exists())
            self.assertTrue((source_folder / "two.bin").exists())
            self.assertTrue((target_folder / "one.bin").exists())
            self.assertTrue((target_folder / "two.bin").exists())

    def test_folder_cleanup_preserves_untracked_empty_directories(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            source_folder = root / "Source"
            target_folder = root / "Target"
            source_folder.mkdir(parents=True)
            target_folder.mkdir()
            unrelated_empty = source_folder / "keep-empty"
            unrelated_empty.mkdir()
            content = b"same" * 40_000
            (source_folder / "one.bin").write_bytes(content)
            (target_folder / "one.bin").write_bytes(content)
            other = b"other" * 40_000
            (source_folder / "two.bin").write_bytes(other)
            (target_folder / "two.bin").write_bytes(other)
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()
            service = ReviewService(database)
            item = service.list_inbox(root)[0]
            service.stage(item, target_folder)

            ActionExecutor(database, lambda path: Path(path).unlink()).commit_all()

            self.assertTrue(unrelated_empty.is_dir())

    def test_interrupted_commit_journal_is_recovered_as_undoable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, _new, _third = self._scanned_group(temporary)
            service = ReviewService(database)
            item = service.list_inbox(old.parent)[0]
            batch_id = service.stage(item)
            batch = database.get_action_batch(batch_id)
            assert batch is not None
            action = batch.actions[0]
            history_id = database.begin_commit_batch(batch)
            database.add_commit_item(
                commit_batch_id=history_id,
                action_id=action.id,
                full_hash=action.full_hash,
                size=action.size,
                retained_path=action.source_path,
                originals_json=capture_originals(action),
                cleanup_paths_json="[]",
                status="completed",
            )

            reopened = Database(database.path)
            history = reopened.get_commit_history(history_id)
            assert history is not None
            self.assertEqual(history.status, "partial")

            result = ActionExecutor(
                reopened,
                lambda path: Path(path).unlink(),
            ).undo_commit(history_id)

            self.assertTrue(result.success)
            self.assertEqual(reopened.get_commit_history(history_id).status, "undone")

    def test_database_error_after_filesystem_change_keeps_undo_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, third = self._scanned_group(temporary)
            item = ReviewService(database).list_inbox(old.parent)[0]
            ReviewService(database).stage(item)
            executor = ActionExecutor(database, lambda path: Path(path).unlink())

            with patch.object(
                database,
                "record_action_success",
                side_effect=OSError("simulated database write failure"),
            ):
                result = executor.commit_all()[0]

            self.assertFalse(result.success)
            history = database.list_commit_history()[0]
            self.assertEqual(history.status, "partial")
            self.assertEqual(history.items[0].status, "completed")
            undo = executor.undo_commit(history.id)
            self.assertTrue(undo.success)
            self.assertTrue(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(third.exists())

    def test_retained_path_replacement_during_trash_does_not_report_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database, old, new, third = self._scanned_group(temporary)
            item = ReviewService(database).list_inbox(old.parent)[0]
            ReviewService(database).stage(item, new)
            replacement_injected = False

            def adversarial_trash(path: str) -> None:
                nonlocal replacement_injected
                if not replacement_injected:
                    replacement_injected = True
                    new.write_bytes(b"unrelated content")
                Path(path).unlink()

            executor = ActionExecutor(database, adversarial_trash)
            result = executor.commit_all()[0]

            self.assertFalse(result.success)
            self.assertEqual(new.read_bytes(), b"unrelated content")
            history = database.list_commit_history()[0]
            self.assertEqual(history.status, "partial")
            new.unlink()
            retry_executor = ActionExecutor(
                database,
                lambda path: Path(path).unlink(),
            )
            retry = retry_executor.commit_all()[0]
            self.assertTrue(retry.success)
            self.assertTrue(new.exists())
            self.assertEqual(
                list(old.parent.glob(".*.dupefinder-replaced-*")),
                [],
            )
            history = database.list_commit_history()[0]
            self.assertEqual(history.status, "completed")
            undo = retry_executor.undo_commit(history.id)
            self.assertTrue(undo.success)
            self.assertTrue(old.exists())
            self.assertTrue(new.exists())
            self.assertTrue(third.exists())


if __name__ == "__main__":
    unittest.main()
