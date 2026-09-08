from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from dupefinder.db import normalize_path
from dupefinder.models import ReviewItem


@dataclass(slots=True)
class ResultFolderNode:
    name: str
    path: str
    items: list[ReviewItem] = field(default_factory=list)
    children: list["ResultFolderNode"] = field(default_factory=list)
    result_count: int = 0
    savings: int = 0

    def descendant_items(self) -> list[ReviewItem]:
        return [
            *self.items,
            *(
                item
                for child in self.children
                for item in child.descendant_items()
            ),
        ]


@dataclass(slots=True)
class _TrieNode:
    path: Path
    items: list[ReviewItem] = field(default_factory=list)
    children: dict[str, "_TrieNode"] = field(default_factory=dict)


def review_item_anchor(item: ReviewItem, scan_root: str | Path) -> str:
    root = Path(normalize_path(scan_root))
    if item.kind == "file":
        candidates = [str(Path(path).parent) for path in item.locations]
    else:
        candidates = list(item.locations)
    try:
        common = Path(os.path.commonpath(candidates))
    except ValueError:
        return str(root)
    try:
        common.relative_to(root)
    except ValueError:
        return str(root)
    return str(common)


def build_result_hierarchy(
    items: Sequence[ReviewItem],
    scan_root: str | Path,
) -> list[ResultFolderNode | ReviewItem]:
    if not items:
        return []
    root = Path(normalize_path(scan_root))
    anchors = {item.key: Path(review_item_anchor(item, root)) for item in items}
    if len(items) == 1:
        return [items[0]]

    common_root = Path(os.path.commonpath([str(path) for path in anchors.values()]))
    trie_root = _TrieNode(common_root)
    for item in items:
        anchor = anchors[item.key]
        node = trie_root
        try:
            relative_parts = anchor.relative_to(common_root).parts
        except ValueError:
            relative_parts = ()
        for part in relative_parts:
            child_path = node.path / part
            node = node.children.setdefault(
                part.casefold(),
                _TrieNode(child_path),
            )
        node.items.append(item)

    return [_materialize(trie_root)]


def _materialize(node: _TrieNode) -> ResultFolderNode:
    materialized_children = [
        _materialize(child)
        for _key, child in sorted(
            node.children.items(),
            key=lambda pair: pair[1].path.name.casefold(),
        )
    ]
    result = ResultFolderNode(
        name=node.path.name or str(node.path),
        path=str(node.path),
        items=list(node.items),
        children=materialized_children,
    )
    result.result_count = len(result.items) + sum(
        child.result_count for child in result.children
    )
    result.savings = sum(item.savings for item in result.items) + sum(
        child.savings for child in result.children
    )
    return _compress(result)


def _compress(node: ResultFolderNode) -> ResultFolderNode:
    while not node.items and len(node.children) == 1:
        child = node.children[0]
        node = ResultFolderNode(
            name=child.name,
            path=child.path,
            items=child.items,
            children=child.children,
            result_count=child.result_count,
            savings=child.savings,
        )
    node.children = [_compress(child) for child in node.children]
    return node
