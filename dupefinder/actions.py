from __future__ import annotations

import ctypes
import json
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from send2trash import send2trash

from dupefinder.db import Database
from dupefinder.hashing import full_hash
from dupefinder.models import ActionResult, BatchActionResult, CommitItem, PendingAction

TrashFunction = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class TimestampSnapshot:
    atime_ns: int
    mtime_ns: int
    ctime_ns: int

    @classmethod
    def from_path(cls, path: Path) -> "TimestampSnapshot":
        stat = path.stat()
        return cls(stat.st_atime_ns, stat.st_mtime_ns, stat.st_ctime_ns)


class PartialCommitError(OSError):
    def __init__(
        self,
        message: str,
        retained_path: Path,
        remaining_paths: list[Path],
        retry_target: Path,
    ) -> None:
        super().__init__(message)
        self.retained_path = retained_path
        self.remaining_paths = remaining_paths
        self.retry_target = retry_target


class PublishError(OSError):
    def __init__(self, message: str, retained_path: Path) -> None:
        super().__init__(message)
        self.retained_path = retained_path


@dataclass(frozen=True, slots=True)
class ActionCommitOutcome:
    result: ActionResult
    retained_path: Path | None
    cleanup_paths: tuple[Path, ...]
    disk_changed: bool


def _unix_ns_to_filetime(value_ns: int) -> int:
    return value_ns // 100 + 116_444_736_000_000_000


def _restore_windows_file_times(path: Path, timestamps: TimestampSnapshot) -> None:
    if os.name != "nt":
        return

    from ctypes import wintypes

    create_file = ctypes.windll.kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE

    set_file_time = ctypes.windll.kernel32.SetFileTime
    set_file_time.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
        ctypes.POINTER(wintypes.FILETIME),
    ]
    set_file_time.restype = wintypes.BOOL

    handle = create_file(
        str(path),
        0x0100,
        0x00000001 | 0x00000002 | 0x00000004,
        None,
        3,
        0x02000000,
        None,
    )
    invalid_handle = wintypes.HANDLE(-1).value
    if handle == invalid_handle:
        raise ctypes.WinError()

    def as_filetime(value_ns: int) -> wintypes.FILETIME:
        value = _unix_ns_to_filetime(value_ns)
        return wintypes.FILETIME(value & 0xFFFFFFFF, value >> 32)

    created = as_filetime(timestamps.ctime_ns)
    accessed = as_filetime(timestamps.atime_ns)
    modified = as_filetime(timestamps.mtime_ns)
    try:
        if not set_file_time(
            handle,
            ctypes.byref(created),
            ctypes.byref(accessed),
            ctypes.byref(modified),
        ):
            raise ctypes.WinError()
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def restore_timestamps(path: Path, timestamps: TimestampSnapshot) -> None:
    os.utime(path, ns=(timestamps.atime_ns, timestamps.mtime_ns))
    _restore_windows_file_times(path, timestamps)


def _file_attributes(path: Path) -> int | None:
    if os.name != "nt":
        return None
    attributes = getattr(path.stat(), "st_file_attributes", None)
    return int(attributes) if attributes is not None else None


def _restore_file_attributes(path: Path, attributes: int | None) -> None:
    if os.name != "nt" or attributes is None:
        return
    if not ctypes.windll.kernel32.SetFileAttributesW(str(path), attributes):
        raise ctypes.WinError()


def capture_originals(action: PendingAction) -> str:
    originals: list[dict[str, int | str | None]] = []
    for raw_path in action.paths:
        path = Path(raw_path)
        stat = path.stat()
        originals.append(
            {
                "path": str(path),
                "size": stat.st_size,
                "atime_ns": stat.st_atime_ns,
                "mtime_ns": stat.st_mtime_ns,
                "ctime_ns": stat.st_ctime_ns,
                "mode": stat.st_mode,
                "attributes": _file_attributes(path),
            }
        )
    return json.dumps(originals, separators=(",", ":"), ensure_ascii=True)


def restore_original_metadata(path: Path, snapshot: dict[str, object]) -> None:
    restore_timestamps(
        path,
        TimestampSnapshot(
            atime_ns=int(snapshot["atime_ns"]),
            mtime_ns=int(snapshot["mtime_ns"]),
            ctime_ns=int(snapshot["ctime_ns"]),
        ),
    )
    os.chmod(path, int(snapshot["mode"]))
    attributes = snapshot.get("attributes")
    _restore_file_attributes(path, int(attributes) if attributes is not None else None)


class ActionExecutor:
    def __init__(self, database: Database, trash: TrashFunction = send2trash) -> None:
        self.database = database
        self.trash = trash

    def commit_all(self) -> list[BatchActionResult]:
        results: list[BatchActionResult] = []
        for batch in self.database.list_action_batches():
            if batch.status == "stale":
                continue
            results.append(self.commit_batch(batch.id))
        return results

    def commit_batch(self, batch_id: int) -> BatchActionResult:
        batch = self.database.get_action_batch(batch_id)
        if not batch:
            return BatchActionResult(batch_id, False, 0, 1, "Cart batch no longer exists")
        if batch.status == "stale":
            return BatchActionResult(
                batch_id,
                False,
                0,
                len(batch.actions),
                "Duplicate membership changed; restage this batch before committing.",
            )
        if not batch.actions:
            return BatchActionResult(batch_id, False, 0, 1, "Cart batch has no pending actions")

        retry_history = self.database.find_retry_commit(batch.id)
        commit_batch_id = (
            retry_history.id
            if retry_history
            else self.database.begin_commit_batch(batch)
        )
        existing_items = {
            item.action_id: item
            for item in (retry_history.items if retry_history else ())
            if item.action_id is not None
        }
        preexisting_applied = any(
            item.status == "completed" for item in existing_items.values()
        )
        completed = 0
        changed = 0
        failures: list[str] = []
        for action in batch.actions:
            claims = self._claim_plan(action)
            publish_temp = self._publish_temp_plan(action)
            expected_retained = (
                action.target_path
                if action.target_path != action.source_path
                else action.source_path
            )
            existing_item = existing_items.get(action.id)
            if existing_item:
                commit_item_id = existing_item.id
                previous_cleanup = [
                    Path(path)
                    for path in json.loads(existing_item.cleanup_paths_json)
                ]
                combined_cleanup: dict[str, Path] = {
                    str(path).casefold(): path
                    for path in [
                        *previous_cleanup,
                        *(claimed for _original, claimed in claims),
                        publish_temp,
                    ]
                }
                self.database.update_commit_item(
                    commit_item_id,
                    retained_path=(
                        existing_item.retained_path
                        if existing_item.status == "completed"
                        else action.source_path
                    ),
                    cleanup_paths=tuple(combined_cleanup.values()),
                    status="completed",
                    error="Commit retry was interrupted before journal finalization.",
                )
            else:
                try:
                    originals_json = capture_originals(action)
                except OSError as exc:
                    message = str(exc)
                    self.database.mark_action_error(action.id, message)
                    failures.append(message)
                    continue
                commit_item_id = self.database.add_commit_item(
                    commit_batch_id=commit_batch_id,
                    action_id=action.id,
                    full_hash=action.full_hash,
                    size=action.size,
                    retained_path=action.source_path,
                    originals_json=originals_json,
                    cleanup_paths_json=json.dumps(
                        [
                            *(str(claimed) for _original, claimed in claims),
                            str(publish_temp),
                        ]
                    ),
                    status="completed",
                    error="Commit was interrupted before journal finalization.",
                )

            outcome = self._commit_with_outcome(action, claims, publish_temp)
            if outcome.disk_changed and outcome.retained_path:
                self.database.update_commit_item(
                    commit_item_id,
                    retained_path=str(outcome.retained_path),
                    cleanup_paths=outcome.cleanup_paths,
                    status="completed",
                    error=None if outcome.result.success else outcome.result.message,
                )
                changed += 1
            else:
                if not (existing_item and existing_item.status == "completed"):
                    self.database.update_commit_item(
                        commit_item_id,
                        retained_path=expected_retained,
                        cleanup_paths=(),
                        status="error",
                        error=outcome.result.message,
                    )

            if outcome.result.success:
                completed += 1
            else:
                failures.append(outcome.result.message)

        if batch.kind == "folder" and changed:
            self._remove_empty_source_folders(
                batch.locations,
                batch.target_root,
                batch.actions,
            )

        failed = len(batch.actions) - completed
        if not failures:
            status = "completed"
            message = f"Committed {completed} action(s)"
            error = None
        elif changed or preexisting_applied:
            status = "partial"
            message = f"Applied {changed} action(s); {failed} require attention"
            error = "; ".join(failures)
        else:
            status = "error"
            message = f"All {failed} action(s) failed"
            error = "; ".join(failures)
        current_history = self.database.get_commit_history(commit_batch_id)
        recovered_bytes = self._recovered_bytes(current_history.items) if current_history else 0
        self.database.finish_commit_batch(
            commit_batch_id,
            batch.id,
            status=status,
            recovered_bytes=recovered_bytes,
            error=error,
        )
        return BatchActionResult(
            batch_id=batch.id,
            success=not failures,
            completed_actions=changed,
            failed_actions=failed,
            message=message if not error else f"{message}: {error}",
        )

    def commit(self, action: PendingAction) -> ActionResult:
        return self._commit_with_outcome(
            action,
            self._claim_plan(action),
            self._publish_temp_plan(action),
        ).result

    def _commit_with_outcome(
        self,
        action: PendingAction,
        claims: list[tuple[Path, Path]],
        publish_temp: Path,
    ) -> ActionCommitOutcome:
        try:
            retained_path = self._execute(action, claims, publish_temp)
        except PartialCommitError as exc:
            message = str(exc)
            self.database.rebase_action_after_partial_commit(
                action.id,
                exc.retained_path,
                exc.remaining_paths,
                message,
                exc.retry_target,
            )
            return ActionCommitOutcome(
                ActionResult(action.id, False, message),
                exc.retained_path,
                tuple(exc.remaining_paths),
                True,
            )
        except Exception as exc:
            message = str(exc)
            self.database.mark_action_error(action.id, message)
            return ActionCommitOutcome(
                ActionResult(action.id, False, message),
                None,
                (),
                False,
            )
        try:
            stat = retained_path.stat()
            self.database.record_action_success(
                action,
                retained_path,
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                ctime_ns=stat.st_ctime_ns,
            )
        except Exception as exc:
            message = (
                "Files were changed successfully, but the database could not record "
                f"completion: {exc}"
            )
            try:
                self.database.mark_action_error(action.id, message)
            except Exception:
                pass
            return ActionCommitOutcome(
                ActionResult(action.id, False, message),
                retained_path,
                (),
                True,
            )
        return ActionCommitOutcome(
            ActionResult(action.id, True, f"Kept {retained_path}"),
            retained_path,
            (),
            True,
        )

    def _execute(
        self,
        action: PendingAction,
        claims: list[tuple[Path, Path]],
        publish_temp: Path,
    ) -> Path:
        source = Path(action.source_path)
        target = Path(action.target_path)
        paths = [Path(path) for path in action.paths]
        if source not in paths:
            raise ValueError("The staged source is no longer in the duplicate set")
        timestamps = TimestampSnapshot.from_path(source)
        claimed_pairs: list[tuple[Path, Path]] = []
        try:
            for original, claimed in claims:
                if not original.is_file():
                    raise FileNotFoundError(f"Staged file no longer exists: {original}")
                os.replace(original, claimed)
                claimed_pairs.append((original, claimed))

            for _original, claimed in claimed_pairs:
                self._validate_claimed_file(claimed, action)
        except Exception as exc:
            try:
                self._rollback_claims(claimed_pairs)
            except Exception as rollback_exc:
                retained = self._existing_recovery_path(source, claims)
                remaining = self._surviving_duplicate_paths(retained, claims)
                raise PartialCommitError(
                    f"Validation failed and rollback was incomplete: {exc}; {rollback_exc}",
                    retained,
                    remaining,
                    target,
                ) from exc
            raise

        source_claim = next(
            claimed
            for original, claimed in claimed_pairs
            if original == source
        )
        failed: list[Path] = []
        for original, claimed in claimed_pairs:
            if original == source:
                continue
            try:
                self.trash(str(claimed))
            except OSError:
                failed.append(claimed)

        try:
            retained = self._publish_claim(
                source_claim,
                target,
                action,
                timestamps,
                publish_temp,
            )
        except PublishError as exc:
            remaining = [path for path in failed if path.exists()]
            if publish_temp.exists():
                remaining.append(publish_temp)
            raise PartialCommitError(
                "Duplicate removals started, but the retained file could not be "
                f"published at {target}: {exc}",
                exc.retained_path,
                remaining,
                target,
            ) from exc
        except Exception as exc:
            remaining = [path for path in failed if path.exists()]
            if publish_temp.exists():
                remaining.append(publish_temp)
            raise PartialCommitError(
                "Duplicate removals started, but the retained file could not be "
                f"published at {target}: {exc}",
                source_claim,
                remaining,
                target,
            ) from exc

        if failed:
            raise PartialCommitError(
                "The older file was kept at the selected location, but some replaced "
                "files could not be moved to the Recycle Bin. Retry or undo this decision.",
                retained,
                failed,
                target,
            )
        return retained

    def undo_commit(self, commit_batch_id: int) -> ActionResult:
        history = self.database.get_commit_history(commit_batch_id)
        if not history:
            return ActionResult(commit_batch_id, False, "Commit history entry no longer exists")
        if history.status not in {"completed", "partial", "undo_error"}:
            return ActionResult(
                commit_batch_id,
                False,
                "This history entry has no applied operations available to undo.",
            )

        plans: list[
            tuple[CommitItem, Path, Path, list[dict[str, object]], list[Path]]
        ] = []
        try:
            completed_items = [
                item for item in history.items if item.status == "completed"
            ]
            if completed_items:
                plans = self._preflight_undo(completed_items)
                for commit_item, retained, temporary, originals, cleanup_paths in plans:
                    try:
                        for snapshot in originals:
                            destination = Path(str(snapshot["path"]))
                            if not destination.exists():
                                destination.parent.mkdir(parents=True, exist_ok=True)
                                staged = destination.with_name(
                                    f".{destination.name}.dupefinder-restore-{uuid.uuid4().hex}"
                                )
                                shutil.copyfile(temporary, staged)
                                os.replace(staged, destination)
                            restore_original_metadata(destination, snapshot)

                        original_keys = {
                            str(snapshot["path"]).casefold()
                            for snapshot in originals
                        }
                        cleanup_candidates = list(cleanup_paths)
                        if str(retained).casefold() not in original_keys:
                            cleanup_candidates.append(retained)
                        seen_cleanup: set[str] = set()
                        for cleanup in cleanup_candidates:
                            cleanup_key = str(cleanup).casefold()
                            if cleanup_key in seen_cleanup:
                                continue
                            seen_cleanup.add(cleanup_key)
                            if cleanup.exists():
                                self._validate_cleanup_file(
                                    cleanup,
                                    commit_item.size,
                                    commit_item.full_hash,
                                )
                                self.trash(str(cleanup))

                        self.database.restore_group_records(
                            history.root_path,
                            commit_item.full_hash,
                            originals,
                        )
                        self.database.mark_commit_item_undone(commit_item.id)
                    finally:
                        temporary.unlink(missing_ok=True)
            elif not any(item.status == "undone" for item in history.items):
                raise ValueError("Commit contains no applied file operations to undo")
            self.database.mark_commit_undone(commit_batch_id)
        except Exception as exc:
            for _item, _retained, temporary, _originals, _cleanup in plans:
                temporary.unlink(missing_ok=True)
            message = str(exc)
            self.database.mark_commit_undo_error(commit_batch_id, message)
            return ActionResult(commit_batch_id, False, message)

        return ActionResult(commit_batch_id, True, f"Restored {history.label}")

    def _preflight_undo(
        self,
        items: Sequence[CommitItem],
    ) -> list[
        tuple[CommitItem, Path, Path, list[dict[str, object]], list[Path]]
    ]:
        pending: list[
            tuple[CommitItem, Path, Path, list[dict[str, object]], list[Path]]
        ] = []
        for item in items:
            if item.status != "completed":
                continue
            originals = json.loads(item.originals_json)
            cleanup_paths = [Path(path) for path in json.loads(item.cleanup_paths_json)]
            retained = self._find_recovery_source(item, originals, cleanup_paths)

            for snapshot in originals:
                destination = Path(str(snapshot["path"]))
                if destination.exists():
                    self._validate_cleanup_file(
                        destination,
                        item.size,
                        item.full_hash,
                        conflict_label="Undo destination is occupied",
                    )
            for cleanup in cleanup_paths:
                if cleanup.exists():
                    self._validate_cleanup_file(cleanup, item.size, item.full_hash)

            temporary = self._temporary_undo_path(retained)
            pending.append((item, retained, temporary, originals, cleanup_paths))

        if not pending:
            raise ValueError("Commit contains no applied file operations to undo")

        created: list[Path] = []
        try:
            for _item, retained, temporary, _originals, _cleanup in pending:
                shutil.copy2(retained, temporary)
                created.append(temporary)
        except Exception:
            for temporary in created:
                temporary.unlink(missing_ok=True)
            raise
        return pending

    @staticmethod
    def _find_recovery_source(
        item: CommitItem,
        originals: list[dict[str, object]],
        cleanup_paths: list[Path],
    ) -> Path:
        candidates = [
            Path(item.retained_path),
            *(Path(str(snapshot["path"])) for snapshot in originals),
            *cleanup_paths,
        ]
        seen: set[str] = set()
        for candidate in candidates:
            key = str(candidate).casefold()
            if key in seen:
                continue
            seen.add(key)
            if not candidate.is_file():
                continue
            if candidate.stat().st_size == item.size and full_hash(candidate) == item.full_hash:
                return candidate
        raise FileNotFoundError(
            f"No unchanged duplicate remains to reconstruct {item.retained_path}"
        )

    @staticmethod
    def _validate_cleanup_file(
        path: Path,
        size: int,
        expected_hash: str,
        *,
        conflict_label: str = "Recovery cleanup file changed",
    ) -> None:
        if ".dupefinder-publish-" in path.name:
            return
        if path.stat().st_size != size or full_hash(path) != expected_hash:
            raise ValueError(f"{conflict_label}: {path}")

    @staticmethod
    def _recovered_bytes(items: Sequence[CommitItem]) -> int:
        recovered = 0
        for item in items:
            if item.status != "completed":
                continue
            originals = json.loads(item.originals_json)
            cleanup_paths = json.loads(item.cleanup_paths_json)
            recovered += item.size * max(
                0,
                len(originals) - 1 - len(cleanup_paths),
            )
        return recovered

    @staticmethod
    def _claim_plan(action: PendingAction) -> list[tuple[Path, Path]]:
        claims: list[tuple[Path, Path]] = []
        for raw_path in action.paths:
            original = Path(raw_path)
            while True:
                claimed = original.with_name(
                    f".{original.name}.dupefinder-replaced-{uuid.uuid4().hex}"
                )
                if not claimed.exists():
                    break
            claims.append((original, claimed))
        return claims

    @staticmethod
    def _publish_temp_plan(action: PendingAction) -> Path:
        target = Path(action.target_path)
        while True:
            candidate = target.with_name(
                f".{target.name}.dupefinder-publish-{uuid.uuid4().hex}"
            )
            if not candidate.exists():
                return candidate

    @staticmethod
    def _rollback_claims(
        claims: list[tuple[Path, Path]],
    ) -> None:
        rollback_errors: list[str] = []
        for original, claimed in reversed(claims):
            if claimed.exists() and not original.exists():
                try:
                    os.replace(claimed, original)
                except OSError as exc:
                    rollback_errors.append(f"could not restore {original}: {exc}")
        if rollback_errors:
            raise OSError("; ".join(rollback_errors))

    @staticmethod
    def _existing_recovery_path(
        source: Path,
        claims: list[tuple[Path, Path]],
    ) -> Path:
        for original, claimed in claims:
            if original == source and claimed.exists():
                return claimed
        if source.exists():
            return source
        for _original, claimed in claims:
            if claimed.exists():
                return claimed
        raise FileNotFoundError("No duplicate remained after incomplete rollback")

    @staticmethod
    def _surviving_duplicate_paths(
        retained: Path,
        claims: list[tuple[Path, Path]],
    ) -> list[Path]:
        remaining: list[Path] = []
        seen: set[str] = {str(retained).casefold()}
        for original, claimed in claims:
            candidate = claimed if claimed.exists() else original if original.exists() else None
            if candidate is None:
                continue
            key = str(candidate).casefold()
            if key not in seen:
                seen.add(key)
                remaining.append(candidate)
        return remaining

    @staticmethod
    def _publish_claim(
        source_claim: Path,
        target: Path,
        action: PendingAction,
        timestamps: TimestampSnapshot,
        publish_temp: Path,
    ) -> Path:
        if target.exists():
            raise FileExistsError(f"Target path was occupied during commit: {target}")
        try:
            os.rename(source_claim, target)
        except OSError:
            if target.exists():
                raise FileExistsError(f"Target path was occupied during commit: {target}")
            try:
                with source_claim.open("rb") as source_handle, publish_temp.open(
                    "xb"
                ) as target_handle:
                    shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
                ActionExecutor._validate_claimed_file(publish_temp, action)
                restore_timestamps(publish_temp, timestamps)
                if target.exists():
                    raise FileExistsError(
                        f"Target path was occupied during commit: {target}"
                    )
                os.rename(publish_temp, target)
                source_claim.unlink()
            except Exception:
                publish_temp.unlink(missing_ok=True)
                raise
        else:
            try:
                restore_timestamps(target, timestamps)
            except Exception as exc:
                try:
                    os.rename(target, source_claim)
                except OSError as rollback_exc:
                    raise PublishError(
                        f"Could not restore timestamps or return the retained file: "
                        f"{exc}; {rollback_exc}",
                        target,
                    ) from exc
                raise
        return target

    @staticmethod
    def _validate_claimed_file(path: Path, action: PendingAction) -> None:
        stat = path.stat()
        if stat.st_size != action.size:
            raise ValueError(f"Staged file size changed; review the group again: {path}")
        if full_hash(path) != action.full_hash:
            raise ValueError(f"Staged file content changed; review the group again: {path}")

    @staticmethod
    def _temporary_undo_path(retained: Path) -> Path:
        while True:
            candidate = retained.with_name(
                f".{retained.name}.dupefinder-undo-{uuid.uuid4().hex}"
            )
            if not candidate.exists():
                return candidate

    @staticmethod
    def _remove_empty_source_folders(
        locations: tuple[str, ...],
        target_root: str,
        actions: tuple[PendingAction, ...],
    ) -> None:
        target = Path(target_root)
        for location_value in locations:
            location = Path(location_value)
            if location == target or not location.is_dir():
                continue
            mapped_directories: set[Path] = {location}
            for action in actions:
                for raw_path in action.paths:
                    path = Path(raw_path)
                    try:
                        path.relative_to(location)
                    except ValueError:
                        continue
                    parent = path.parent
                    while parent != location:
                        mapped_directories.add(parent)
                        parent = parent.parent
            for directory in sorted(
                mapped_directories,
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    continue
