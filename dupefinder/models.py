from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal


@dataclass(frozen=True, slots=True)
class FileRecord:
    id: int
    root_path: str
    folder_path: str
    path: str
    size: int
    mtime_ns: int
    ctime_ns: int
    partial_hash: str | None = None
    full_hash: str | None = None

    @property
    def modified_at(self) -> datetime:
        return datetime.fromtimestamp(self.mtime_ns / 1_000_000_000)

    @property
    def name(self) -> str:
        return Path(self.path).name


@dataclass(frozen=True, slots=True)
class DuplicateGroup:
    id: int
    root_path: str
    size: int
    full_hash: str
    files: tuple[FileRecord, ...]

    @property
    def oldest_file(self) -> FileRecord:
        return min(self.files, key=lambda item: (item.mtime_ns, item.ctime_ns, item.path.casefold()))


@dataclass(frozen=True, slots=True)
class PendingAction:
    id: int
    group_id: int
    root_path: str
    source_path: str
    target_path: str
    paths: tuple[str, ...]
    size: int
    full_hash: str
    status: str
    last_error: str | None
    batch_id: int | None = None

    @property
    def removal_count(self) -> int:
        return max(0, len(self.paths) - 1)


@dataclass(frozen=True, slots=True)
class ActionResult:
    action_id: int
    success: bool
    message: str


@dataclass(frozen=True, slots=True)
class ReviewMapping:
    group: DuplicateGroup
    relative_path: str
    paths_by_location: tuple[tuple[str, str], ...]

    def path_for_location(self, location: str) -> str:
        for candidate_location, path in self.paths_by_location:
            if candidate_location == location:
                return path
        raise KeyError(location)


@dataclass(frozen=True, slots=True)
class ReviewItem:
    key: str
    fingerprint: str
    kind: Literal["file", "folder"]
    root_path: str
    label: str
    relation: Literal["file", "identical", "subset"]
    locations: tuple[str, ...]
    mappings: tuple[ReviewMapping, ...]
    size: int
    savings: int
    copy_count: int
    recommended_target: str

    @property
    def file_count(self) -> int:
        return len(self.mappings)

    @property
    def group_ids(self) -> tuple[int, ...]:
        return tuple(mapping.group.id for mapping in self.mappings)


@dataclass(frozen=True, slots=True)
class ReviewSnapshot:
    request_id: int
    root_path: str
    current_items: tuple[ReviewItem, ...]
    all_items: tuple[ReviewItem, ...]
    inbox_items: tuple[ReviewItem, ...]
    ignored_items: tuple[ReviewItem, ...]
    ignored_saved_count: int
    thumbnail_paths: tuple[tuple[str, str], ...]
    reconciled: bool
    reconciled_roots: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ActionBatch:
    id: int
    root_path: str
    review_key: str
    review_fingerprint: str
    kind: str
    label: str
    target_root: str
    status: str
    estimated_savings: int
    last_error: str | None
    locations: tuple[str, ...]
    actions: tuple[PendingAction, ...]

    @property
    def removal_count(self) -> int:
        return sum(action.removal_count for action in self.actions)


@dataclass(frozen=True, slots=True)
class CommitItem:
    id: int
    commit_batch_id: int
    action_id: int | None
    full_hash: str
    size: int
    retained_path: str
    originals_json: str
    cleanup_paths_json: str
    status: str
    last_error: str | None


@dataclass(frozen=True, slots=True)
class CommitHistory:
    id: int
    action_batch_id: int | None
    root_path: str
    label: str
    review_key: str
    committed_at: str
    undone_at: str | None
    status: str
    recovered_bytes: int
    last_error: str | None
    items: tuple[CommitItem, ...]


@dataclass(frozen=True, slots=True)
class BatchActionResult:
    batch_id: int
    success: bool
    completed_actions: int
    failed_actions: int
    message: str


@dataclass(frozen=True, slots=True)
class ScanResult:
    paused: bool
    folders_completed: int
    duplicate_groups_changed: int
    folders_discovered: int = 0
