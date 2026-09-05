from __future__ import annotations

import hashlib
import html
import json
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from dupefinder.db import Database, normalize_path
from dupefinder.models import DuplicateGroup, FileRecord, ReviewItem, ReviewMapping


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def common_path_boundaries(paths: Sequence[str]) -> tuple[int, int]:
    if not paths:
        return 0, 0
    parts = [Path(path).parts for path in paths]
    shortest = min(len(item) for item in parts)

    prefix = 0
    while prefix < shortest:
        values = {item[prefix].casefold() for item in parts}
        if len(values) != 1:
            break
        prefix += 1

    suffix = 0
    while suffix < shortest - prefix:
        values = {item[-(suffix + 1)].casefold() for item in parts}
        if len(values) != 1:
            break
        suffix += 1
    return prefix, suffix


def highlighted_path_html(path: str, comparison_paths: Sequence[str]) -> str:
    parts = Path(path).parts
    prefix, suffix = common_path_boundaries(comparison_paths)
    highlighted: list[str] = []
    for index, part in enumerate(parts):
        escaped = html.escape(part)
        is_difference = index >= prefix and index < len(parts) - suffix
        if is_difference:
            escaped = (
                '<span style="background-color:#ffe69a;color:#442b00;'
                'font-weight:600;padding:1px 2px;">'
                f"{escaped}</span>"
            )
        else:
            escaped = f'<span style="color:#666;">{escaped}</span>'
        highlighted.append(escaped)
    return html.escape(os.sep).join(highlighted)


def timestamps_differ(files: Sequence[FileRecord]) -> bool:
    modified, created = timestamp_difference_flags(files)
    return modified or created


def timestamp_difference_flags(files: Sequence[FileRecord]) -> tuple[bool, bool]:
    return (
        len({file.mtime_ns for file in files}) > 1,
        len({file.ctime_ns for file in files}) > 1,
    )


def review_item_snapshot(item: ReviewItem) -> str:
    payload = {
        "key": item.key,
        "fingerprint": item.fingerprint,
        "kind": item.kind,
        "root_path": item.root_path,
        "label": item.label,
        "relation": item.relation,
        "locations": item.locations,
        "size": item.size,
        "savings": item.savings,
        "copy_count": item.copy_count,
        "recommended_target": item.recommended_target,
        "groups": [
            {
                "group_id": mapping.group.id,
                "relative_path": mapping.relative_path,
                "paths_by_location": mapping.paths_by_location,
            }
            for mapping in item.mappings
        ],
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True)


@dataclass(slots=True)
class _FolderCandidate:
    locations: tuple[str, ...]
    mappings: dict[int, ReviewMapping]
    relation: str = ""
    savings: int = 0


class ReviewService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self._cache: dict[str, list[ReviewItem]] = {}

    def invalidate(self, root_path: str | Path | None = None) -> None:
        if root_path is None:
            self._cache.clear()
        else:
            self._cache.pop(normalize_path(root_path), None)

    def list_all(self, root_path: str | Path) -> list[ReviewItem]:
        root = normalize_path(root_path)
        items = self._cache.get(root)
        if items is None:
            groups = self.database.list_duplicate_groups(root)
            for group in groups:
                self.database.update_group_membership_fingerprint(
                    group.id,
                    self.database.group_membership_fingerprint(group),
                )

            bundles, bundled_group_ids = self._folder_bundles(root, groups)
            file_items = [
                self._file_item(group)
                for group in groups
                if group.id not in bundled_group_ids
            ]
            items = [*bundles, *file_items]
            items.sort(key=lambda item: (-item.savings, item.label.casefold(), item.key))
            self._cache[root] = items
        self.database.reconcile_review_items(
            root,
            {item.key: item.fingerprint for item in items},
        )
        return list(items)

    def list_inbox(self, root_path: str | Path) -> list[ReviewItem]:
        items = self.list_all(root_path)
        ignored = self.database.ignored_review_keys(root_path)
        batches = self.database.active_review_batches(root_path)
        return [
            item
            for item in items
            if ignored.get(item.key) != item.fingerprint
            and not (
                (batch := batches.get(item.key))
                and batch.status != "stale"
                and batch.review_fingerprint == item.fingerprint
            )
        ]

    def list_ignored(self, root_path: str | Path) -> list[ReviewItem]:
        items = self.list_all(root_path)
        ignored = self.database.ignored_review_keys(root_path)
        return [item for item in items if ignored.get(item.key) == item.fingerprint]

    def find_item(self, root_path: str | Path, review_key: str) -> ReviewItem | None:
        return next(
            (item for item in self.list_all(root_path) if item.key == review_key),
            None,
        )

    def stage(self, item: ReviewItem, target: str | Path | None = None) -> int:
        destination = normalize_path(target or item.recommended_target)
        if destination not in item.locations:
            raise ValueError("Selected destination is not part of this review item")

        action_targets: list[tuple[int, str]] = []
        for mapping in item.mappings:
            if item.kind == "file":
                target_path = destination
            else:
                target_path = mapping.path_for_location(destination)
            action_targets.append((mapping.group.id, target_path))

        batch = self.database.stage_batch(
            root_path=item.root_path,
            review_key=item.key,
            review_fingerprint=item.fingerprint,
            kind=item.kind,
            label=item.label,
            target_root=destination,
            estimated_savings=item.savings,
            locations=item.locations,
            action_targets=action_targets,
        )
        return batch.id

    def ignore(self, item: ReviewItem) -> None:
        self.database.ignore_review_item(
            review_key=item.key,
            root_path=item.root_path,
            fingerprint=item.fingerprint,
            kind=item.kind,
            snapshot_json=review_item_snapshot(item),
        )

    def restore(self, item: ReviewItem) -> None:
        self.database.restore_ignored_item(item.key)

    def move_batch_to_ignored(self, batch_id: int) -> None:
        batch = self.database.get_action_batch(batch_id)
        if not batch:
            raise ValueError("Cart batch no longer exists")
        item = self.find_item(batch.root_path, batch.review_key)
        if not item:
            raise ValueError("The duplicate item changed and must be reviewed again")
        self.ignore(item)

    def _file_item(self, group: DuplicateGroup) -> ReviewItem:
        names = {file.name.casefold() for file in group.files}
        if len(names) == 1:
            label = group.files[0].name
        else:
            suffixes = {Path(file.path).suffix.casefold() for file in group.files}
            label = (
                f"Identical {next(iter(suffixes)) or 'files'}"
                if len(suffixes) == 1
                else "Identical files"
            )
        locations = tuple(file.path for file in group.files)
        fingerprint = self.database.group_membership_fingerprint(group)
        key_payload = f"{group.root_path}|{group.size}|{group.full_hash}"
        key = f"file:{hashlib.sha256(key_payload.encode('utf-8')).hexdigest()}"
        mapping = ReviewMapping(
            group=group,
            relative_path=group.files[0].name,
            paths_by_location=tuple((file.path, file.path) for file in group.files),
        )
        return ReviewItem(
            key=key,
            fingerprint=fingerprint,
            kind="file",
            root_path=group.root_path,
            label=label,
            relation="file",
            locations=locations,
            mappings=(mapping,),
            size=group.size,
            savings=group.size * max(0, len(group.files) - 1),
            copy_count=len(group.files),
            recommended_target=group.oldest_file.path,
        )

    def _folder_bundles(
        self,
        root_path: str,
        groups: Sequence[DuplicateGroup],
    ) -> tuple[list[ReviewItem], set[int]]:
        candidates: dict[tuple[str, ...], _FolderCandidate] = {}
        root = Path(root_path)

        for group in groups:
            relative_parts: list[tuple[str, ...]] = []
            try:
                for file in group.files:
                    relative_parts.append(Path(file.path).relative_to(root).parts)
            except ValueError:
                continue

            suffix_length = self._common_suffix_length(relative_parts)
            for length in range(1, suffix_length + 1):
                raw_locations: list[tuple[str, str]] = []
                paths_by_location: list[tuple[str, str]] = []
                relative_path = str(Path(*relative_parts[0][-length:]))
                for file, parts in zip(group.files, relative_parts, strict=True):
                    location = normalize_path(root.joinpath(*parts[:-length]))
                    raw_locations.append((location.casefold(), location))
                    paths_by_location.append((location, file.path))
                locations = tuple(value for _folded, value in sorted(raw_locations))
                if len({location.casefold() for location in locations}) != len(group.files):
                    continue
                ordered_paths = tuple(
                    sorted(paths_by_location, key=lambda item: item[0].casefold())
                )
                candidate = candidates.setdefault(
                    tuple(location.casefold() for location in locations),
                    _FolderCandidate(locations=locations, mappings={}),
                )
                candidate.mappings[group.id] = ReviewMapping(
                    group=group,
                    relative_path=relative_path,
                    paths_by_location=ordered_paths,
                )

        all_files = self.database.list_files(root_path)
        manifest_cache: dict[str, dict[str, tuple[int, str | None]]] = {}
        complete_cache: dict[str, bool] = {}
        valid: list[_FolderCandidate] = []
        for candidate in candidates.values():
            if len(candidate.mappings) < 2:
                continue
            relation = self._classify_candidate(
                root_path,
                candidate,
                all_files,
                manifest_cache,
                complete_cache,
            )
            if not relation:
                continue
            candidate.relation = relation
            candidate.savings = sum(
                mapping.group.size * max(0, len(mapping.group.files) - 1)
                for mapping in candidate.mappings.values()
            )
            valid.append(candidate)

        valid.sort(
            key=lambda item: (
                -item.savings,
                -len(item.mappings),
                sum(len(Path(location).parts) for location in item.locations),
            )
        )
        selected: list[_FolderCandidate] = []
        used_group_ids: set[int] = set()
        for candidate in valid:
            group_ids = set(candidate.mappings)
            if group_ids & used_group_ids:
                continue
            selected.append(candidate)
            used_group_ids.update(group_ids)

        return [self._folder_item(root_path, candidate) for candidate in selected], used_group_ids

    @staticmethod
    def _common_suffix_length(parts: Sequence[tuple[str, ...]]) -> int:
        if not parts:
            return 0
        shortest = min(len(item) for item in parts)
        count = 0
        while count < shortest:
            values = {item[-(count + 1)].casefold() for item in parts}
            if len(values) != 1:
                break
            count += 1
        return count

    def _classify_candidate(
        self,
        root_path: str,
        candidate: _FolderCandidate,
        all_files: Sequence[FileRecord],
        manifest_cache: dict[str, dict[str, tuple[int, str | None]]],
        complete_cache: dict[str, bool],
    ) -> str | None:
        manifests: list[dict[str, tuple[int, str | None]]] = []
        for location in candidate.locations:
            if location not in complete_cache:
                complete_cache[location] = self.database.folder_scope_complete(
                    root_path,
                    location,
                )
            if not complete_cache[location]:
                return None
            if location not in manifest_cache:
                manifest_cache[location] = self._manifest(location, all_files)
            manifests.append(manifest_cache[location])

        if not manifests or any(not manifest for manifest in manifests):
            return None
        mapping_keys = {
            self._relative_key(mapping.relative_path)
            for mapping in candidate.mappings.values()
        }
        key_sets = [set(manifest) for manifest in manifests]
        intersection = set.intersection(*key_sets)
        if mapping_keys != intersection:
            return None

        for relative in mapping_keys:
            values = [manifest.get(relative) for manifest in manifests]
            if any(value is None or value[1] is None for value in values):
                return None
            if len(set(values)) != 1:
                return None

        if all(keys == key_sets[0] for keys in key_sets) and mapping_keys == key_sets[0]:
            return "identical"
        if any(keys == intersection for keys in key_sets) and all(
            intersection <= keys for keys in key_sets
        ):
            return "subset"
        return None

    @staticmethod
    def _manifest(
        location: str,
        all_files: Sequence[FileRecord],
    ) -> dict[str, tuple[int, str | None]]:
        root = Path(location)
        manifest: dict[str, tuple[int, str | None]] = {}
        for file in all_files:
            path = Path(file.path)
            try:
                relative = path.relative_to(root)
            except ValueError:
                continue
            manifest[ReviewService._relative_key(str(relative))] = (
                file.size,
                file.full_hash,
            )
        return manifest

    @staticmethod
    def _relative_key(path: str) -> str:
        return Path(path).as_posix().casefold()

    def _folder_item(self, root_path: str, candidate: _FolderCandidate) -> ReviewItem:
        mappings = tuple(
            sorted(
                candidate.mappings.values(),
                key=lambda mapping: mapping.relative_path.casefold(),
            )
        )
        source_counts: Counter[str] = Counter()
        for mapping in mappings:
            oldest_path = mapping.group.oldest_file.path
            for location, path in mapping.paths_by_location:
                if path == oldest_path:
                    source_counts[location] += 1
                    break
        recommended = sorted(
            candidate.locations,
            key=lambda location: (-source_counts[location], location.casefold()),
        )[0]
        key_payload = json.dumps(
            [root_path, candidate.relation, [item.casefold() for item in candidate.locations]],
            separators=(",", ":"),
        )
        key = f"folder:{hashlib.sha256(key_payload.encode('utf-8')).hexdigest()}"
        fingerprint_payload = [
            (
                mapping.group.id,
                mapping.relative_path.casefold(),
                tuple(
                    (location.casefold(), path.casefold())
                    for location, path in mapping.paths_by_location
                ),
                self.database.group_membership_fingerprint(mapping.group),
            )
            for mapping in mappings
        ]
        fingerprint = hashlib.sha256(
            json.dumps(
                fingerprint_payload,
                separators=(",", ":"),
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        relation_label = "Identical folders" if candidate.relation == "identical" else "Folder subset"
        return ReviewItem(
            key=key,
            fingerprint=fingerprint,
            kind="folder",
            root_path=root_path,
            label=f"{relation_label}: {Path(candidate.locations[0]).name}",
            relation=candidate.relation,
            locations=candidate.locations,
            mappings=mappings,
            size=sum(mapping.group.size for mapping in mappings),
            savings=candidate.savings,
            copy_count=len(candidate.locations),
            recommended_target=recommended,
        )
