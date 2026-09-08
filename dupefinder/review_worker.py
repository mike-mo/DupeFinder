from __future__ import annotations

from pathlib import Path

from PIL import Image
from PySide6.QtCore import QThread, Signal

from dupefinder.db import Database
from dupefinder.models import ReviewSnapshot
from dupefinder.review import ReviewService
from dupefinder.thumbnails import ensure_thumbnail


class ReviewSnapshotWorker(QThread):
    snapshot_ready = Signal(object)
    snapshot_failed = Signal(str)

    def __init__(
        self,
        database_path: str | Path,
        root_path: str,
        thumbnail_cache: str | Path,
        *,
        reconcile: bool,
        request_id: int,
        additional_roots: tuple[str, ...] = (),
    ) -> None:
        super().__init__()
        self.database_path = Path(database_path)
        self.root_path = root_path
        self.thumbnail_cache = Path(thumbnail_cache)
        self.reconcile = reconcile
        self.request_id = request_id
        self.additional_roots = additional_roots

    def run(self) -> None:
        try:
            database = Database(self.database_path)
            service = ReviewService(database)
            roots = tuple(
                dict.fromkeys((self.root_path, *self.additional_roots))
            )
            items_by_root = {}
            for root in roots:
                if self.isInterruptionRequested():
                    return
                items_by_root[root] = service.list_all(root, reconcile=False)
            if self.isInterruptionRequested():
                return
            current_items = items_by_root[self.root_path]
            inbox, ignored = service.partition_items(
                current_items,
                self.root_path,
            )
            all_items = [
                item
                for root in roots
                for item in items_by_root[root]
            ]
            thumbnails: list[tuple[str, str]] = []
            for item in [*inbox, *ignored]:
                if self.isInterruptionRequested():
                    return
                representative = (
                    item.mappings[0].group.files[0].path
                    if item.kind == "file"
                    else item.locations[0]
                )
                try:
                    thumbnail = ensure_thumbnail(
                        representative,
                        self.thumbnail_cache,
                        96,
                    )
                except (
                    OSError,
                    ValueError,
                    SyntaxError,
                    Image.DecompressionBombError,
                ):
                    thumbnail = None
                if thumbnail:
                    thumbnails.append((item.key, str(thumbnail)))
            snapshot = ReviewSnapshot(
                request_id=self.request_id,
                root_path=self.root_path,
                current_items=tuple(current_items),
                all_items=tuple(all_items),
                inbox_items=tuple(inbox),
                ignored_items=tuple(ignored),
                ignored_saved_count=database.ignored_review_count(self.root_path),
                thumbnail_paths=tuple(thumbnails),
                reconciled=(
                    self.reconcile
                    and database.scan_results_valid(self.root_path)
                ),
                reconciled_roots=tuple(
                    root
                    for root in roots
                    if self.reconcile and database.scan_results_valid(root)
                ),
            )
        except Exception as exc:
            self.snapshot_failed.emit(str(exc))
            return
        self.snapshot_ready.emit(snapshot)
