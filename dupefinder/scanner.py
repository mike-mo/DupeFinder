from __future__ import annotations

import os
import queue
import threading
from collections import defaultdict
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThread, Signal

from dupefinder.db import Database, normalize_path
from dupefinder.hashing import FileChangedError, full_hash, partial_hash
from dupefinder.models import FileRecord, ScanResult

GroupCallback = Callable[[int], None]
ProgressCallback = Callable[[int, int, bool, str], None]
StatusCallback = Callable[[str], None]
StopCallback = Callable[[], bool]


class ScannerEngine:
    def __init__(
        self,
        database: Database,
        root_path: str | Path,
        min_size_bytes: int,
        *,
        on_group: GroupCallback | None = None,
        on_progress: ProgressCallback | None = None,
        on_status: StatusCallback | None = None,
        should_stop: StopCallback | None = None,
    ) -> None:
        self.database = database
        self.root_path = normalize_path(root_path)
        self.min_size_bytes = min_size_bytes
        self.on_group = on_group or (lambda _group_id: None)
        self.on_progress = on_progress or (
            lambda _current, _total, _scope_complete, _path: None
        )
        self.on_status = on_status or (lambda _message: None)
        self.should_stop = should_stop or (lambda: False)
        self._candidate_recheck_incomplete = False
        self._scan_had_errors = False

    def run(self) -> ScanResult:
        root = Path(self.root_path)
        if not root.is_dir():
            raise NotADirectoryError(f"Scan folder does not exist: {root}")
        if self.min_size_bytes < 0:
            raise ValueError("Minimum file size cannot be negative")

        self.database.prepare_root(root, self.min_size_bytes)
        self.database.mark_scan_results_valid(self.root_path, False)
        self.on_status("Discovering folders and scanning new areas...")
        folder_queue: queue.Queue[str | None] = queue.Queue()
        discovery_done = threading.Event()
        discovery_stop = threading.Event()
        state_lock = threading.Lock()
        discovered: list[str] = []
        scheduled = 0
        discovery_errors: list[str] = []

        def discover() -> None:
            nonlocal scheduled

            def record_error(error: OSError) -> None:
                discovery_errors.append(str(error))
                self.on_status(f"Could not discover a folder: {error}")

            try:
                for folder in self._walk_folders(root, record_error):
                    if discovery_stop.is_set() or self.should_stop():
                        break
                    normalized = self.database.register_folder(self.root_path, folder)
                    status = self.database.folder_status(self.root_path, normalized)
                    with state_lock:
                        discovered.append(normalized)
                        if status != "completed":
                            scheduled += 1
                    if status != "completed":
                        folder_queue.put(normalized)
            except Exception as exc:
                discovery_errors.append(str(exc))
                self.on_status(f"Folder discovery failed: {exc}")
            finally:
                discovery_done.set()
                folder_queue.put(None)

        producer = threading.Thread(
            target=discover,
            name="DupeFinderFolderDiscovery",
            daemon=True,
        )
        producer.start()
        completed = 0
        changed_groups: set[int] = set()
        try:
            while True:
                if self.should_stop():
                    return ScanResult(
                        True,
                        completed,
                        len(changed_groups),
                        len(discovered),
                    )
                try:
                    folder = folder_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if folder is None:
                    break
                with state_lock:
                    current_total = scheduled if discovery_done.is_set() else len(discovered)
                    scope_complete = discovery_done.is_set()
                self.on_progress(completed, current_total, scope_complete, folder)
                if self._scan_one(folder, changed_groups):
                    completed += 1
                else:
                    return ScanResult(
                        True,
                        completed,
                        len(changed_groups),
                        len(discovered),
                    )
                self.on_progress(completed, current_total, scope_complete, folder)
        finally:
            discovery_stop.set()
            producer.join()

        if discovery_errors:
            raise OSError(
                "Folder discovery was incomplete; cached results were preserved: "
                f"{discovery_errors[0]}"
            )
        if not discovered:
            raise OSError(f"No folders could be discovered under {root}")

        self.database.prune_folders(self.root_path, discovered)
        if scheduled == 0 and not self.should_stop():
            self.database.reset_scan_progress(self.root_path)
            completed = 0
            total = len(discovered)
            for folder in discovered:
                self.on_progress(completed, total, True, folder)
                if not self._scan_one(folder, changed_groups):
                    return ScanResult(
                        True,
                        completed,
                        len(changed_groups),
                        len(discovered),
                    )
                completed += 1
                self.on_progress(completed, total, True, folder)

        self.on_progress(completed, scheduled or len(discovered), True, "")
        if self._scan_had_errors:
            raise OSError(
                "Scan incomplete because one or more folders or files could not "
                "be inspected. Cached results were preserved where possible."
            )
        self.database.mark_scan_results_valid(self.root_path, True)
        self.database.cleanup_duplicate_groups(self.root_path)
        return ScanResult(
            False,
            completed,
            len(changed_groups),
            len(discovered),
        )

    @staticmethod
    def _walk_folders(root: Path, onerror: Callable[[OSError], None]):
        for current, directory_names, _file_names in os.walk(
            root,
            topdown=True,
            onerror=onerror,
            followlinks=False,
        ):
            directory_names.sort(key=str.casefold)
            yield normalize_path(current)

    def _scan_one(self, folder: str, changed_groups: set[int]) -> bool:
        self.database.mark_folder(self.root_path, folder, "scanning")
        try:
            changed_groups.update(self._scan_folder(Path(folder)))
        except (OSError, ValueError) as exc:
            self._scan_had_errors = True
            self.database.mark_folder(self.root_path, folder, "error", str(exc))
            self.on_status(f"Could not scan {folder}: {exc}")
        else:
            if self.should_stop():
                self.database.mark_folder(self.root_path, folder, "pending")
                return False
            self.database.mark_folder(self.root_path, folder, "completed")
        return True

    def _scan_folder(self, folder: Path) -> set[int]:
        seen_paths: list[str] = []
        sizes: set[int] = set()
        inspection_errors: list[str] = []

        with os.scandir(folder) as entries:
            for entry in entries:
                if self.should_stop():
                    break
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError as exc:
                    self.on_status(f"Skipped {entry.path}: {exc}")
                    inspection_errors.append(f"{entry.path}: {exc}")
                    continue
                if stat.st_size < self.min_size_bytes:
                    continue
                path = normalize_path(entry.path)
                self.database.upsert_file(
                    self.root_path,
                    str(folder),
                    path,
                    stat.st_size,
                    stat.st_mtime_ns,
                    stat.st_ctime_ns,
                )
                seen_paths.append(path)
                sizes.add(stat.st_size)

        if self.should_stop():
            return set()
        if inspection_errors:
            raise OSError(
                "Folder inspection was incomplete; cached file records were preserved: "
                f"{inspection_errors[0]}"
            )

        self.database.sync_folder_paths(self.root_path, str(folder), seen_paths)
        changed_groups: set[int] = set()
        for size in sorted(sizes):
            if self.should_stop():
                break
            changed_groups.update(self._process_size(size))
        return changed_groups

    def _process_size(self, size: int) -> set[int]:
        candidates = self.database.get_files_by_size(self.root_path, size)
        if len(candidates) < 2:
            return set()

        self._candidate_recheck_incomplete = False
        partial_groups: dict[str, list[FileRecord]] = defaultdict(list)
        for candidate in candidates:
            current = self._refresh_if_changed(candidate)
            if not current or current.size != size:
                continue
            try:
                digest = current.partial_hash or partial_hash(current.path)
            except (OSError, FileChangedError) as exc:
                self.on_status(f"Skipped hashing {current.path}: {exc}")
                self._candidate_recheck_incomplete = True
                continue
            if current.partial_hash != digest:
                self.database.update_file_hash(current.id, partial=digest)
                current = FileRecord(
                    id=current.id,
                    root_path=current.root_path,
                    folder_path=current.folder_path,
                    path=current.path,
                    size=current.size,
                    mtime_ns=current.mtime_ns,
                    ctime_ns=current.ctime_ns,
                    partial_hash=digest,
                    full_hash=current.full_hash,
                )
            partial_groups[digest].append(current)

        if self._candidate_recheck_incomplete:
            self._scan_had_errors = True
            return set()

        changed_groups: set[int] = set()
        for partial_matches in partial_groups.values():
            if len(partial_matches) < 2:
                continue
            full_groups: dict[str, list[FileRecord]] = defaultdict(list)
            for candidate in partial_matches:
                try:
                    digest = candidate.full_hash or full_hash(candidate.path)
                except (OSError, FileChangedError) as exc:
                    self.on_status(f"Skipped hashing {candidate.path}: {exc}")
                    self._candidate_recheck_incomplete = True
                    continue
                if candidate.full_hash != digest:
                    self.database.update_file_hash(candidate.id, full=digest)
                full_groups[digest].append(candidate)

            if self._candidate_recheck_incomplete:
                self._scan_had_errors = True
                return changed_groups

            for digest, full_matches in full_groups.items():
                if len(full_matches) < 2:
                    continue
                group_id, changed = self.database.ensure_duplicate_group(
                    self.root_path,
                    size,
                    digest,
                    [item.id for item in full_matches],
                )
                if changed:
                    changed_groups.add(group_id)
                    self.on_group(group_id)
        return changed_groups

    def _refresh_if_changed(self, record: FileRecord) -> FileRecord | None:
        try:
            stat = Path(record.path).stat()
        except FileNotFoundError:
            self.database.remove_file(self.root_path, record.path)
            return None
        except OSError as exc:
            self.on_status(f"Could not recheck {record.path}; cached record preserved: {exc}")
            self._candidate_recheck_incomplete = True
            return None
        if stat.st_size < self.min_size_bytes:
            self.database.remove_file(self.root_path, record.path)
            return None
        if (
            stat.st_size != record.size
            or stat.st_mtime_ns != record.mtime_ns
            or stat.st_ctime_ns != record.ctime_ns
        ):
            return self.database.upsert_file(
                self.root_path,
                str(Path(record.path).parent),
                record.path,
                stat.st_size,
                stat.st_mtime_ns,
                stat.st_ctime_ns,
            )
        return record


class ScanWorker(QThread):
    group_found = Signal(int)
    progress_changed = Signal(int, int, bool, str)
    status_changed = Signal(str)
    scan_complete = Signal(bool, int, int)
    scan_failed = Signal(str)

    def __init__(
        self,
        database_path: str | Path,
        root_path: str | Path,
        min_size_bytes: int,
    ) -> None:
        super().__init__()
        self.database_path = Path(database_path)
        self.root_path = str(root_path)
        self.min_size_bytes = min_size_bytes
        self._pause_requested = threading.Event()

    def request_pause(self) -> None:
        self._pause_requested.set()

    def run(self) -> None:
        try:
            engine = ScannerEngine(
                Database(self.database_path),
                self.root_path,
                self.min_size_bytes,
                on_group=self.group_found.emit,
                on_progress=self.progress_changed.emit,
                on_status=self.status_changed.emit,
                should_stop=self._pause_requested.is_set,
            )
            result = engine.run()
        except Exception as exc:
            self.scan_failed.emit(str(exc))
            return
        self.scan_complete.emit(
            result.paused,
            result.folders_completed,
            result.duplicate_groups_changed,
        )
