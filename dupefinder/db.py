from __future__ import annotations

import json
import hashlib
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Sequence

from dupefinder.models import (
    ActionBatch,
    CommitHistory,
    CommitItem,
    DuplicateGroup,
    FileRecord,
    PendingAction,
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_roots (
    path TEXT PRIMARY KEY,
    min_size_bytes INTEGER NOT NULL,
    cycle_started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS folders (
    root_path TEXT NOT NULL,
    path TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'scanning', 'completed', 'error')),
    completed_at TEXT,
    last_error TEXT,
    PRIMARY KEY (root_path, path),
    FOREIGN KEY (root_path) REFERENCES scan_roots(path) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL,
    folder_path TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    ctime_ns INTEGER NOT NULL,
    partial_hash TEXT,
    full_hash TEXT,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (root_path, path),
    FOREIGN KEY (root_path, folder_path)
        REFERENCES folders(root_path, path) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_files_root_size
    ON files(root_path, size);
CREATE INDEX IF NOT EXISTS idx_files_partial_hash
    ON files(root_path, size, partial_hash);
CREATE INDEX IF NOT EXISTS idx_files_full_hash
    ON files(root_path, size, full_hash);

CREATE TABLE IF NOT EXISTS duplicate_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    full_hash TEXT NOT NULL,
    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    resolved_at TEXT,
    membership_fingerprint TEXT NOT NULL DEFAULT '',
    UNIQUE (root_path, size, full_hash)
);

CREATE TABLE IF NOT EXISTS duplicate_group_files (
    group_id INTEGER NOT NULL,
    file_id INTEGER NOT NULL,
    PRIMARY KEY (group_id, file_id),
    FOREIGN KEY (group_id) REFERENCES duplicate_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (file_id) REFERENCES files(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id INTEGER NOT NULL UNIQUE,
    root_path TEXT NOT NULL,
    source_path TEXT NOT NULL,
    target_path TEXT NOT NULL,
    paths_json TEXT NOT NULL,
    size INTEGER NOT NULL,
    full_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'error', 'completed')),
    last_error TEXT,
    batch_id INTEGER,
    review_key TEXT NOT NULL DEFAULT '',
    review_fingerprint TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ignored_review_items (
    review_key TEXT PRIMARY KEY,
    root_path TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'folder')),
    snapshot_json TEXT NOT NULL,
    ignored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS action_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    root_path TEXT NOT NULL,
    review_key TEXT NOT NULL,
    review_fingerprint TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('file', 'folder', 'legacy')),
    label TEXT NOT NULL,
    target_root TEXT NOT NULL,
    locations_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'stale', 'error', 'completed')),
    estimated_savings INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_action_batches_root_status
    ON action_batches(root_path, status);

CREATE TABLE IF NOT EXISTS commit_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    action_batch_id INTEGER,
    root_path TEXT NOT NULL,
    review_key TEXT NOT NULL,
    label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'completed', 'partial', 'error',
            'undone', 'undo_error'
        )),
    recovered_bytes INTEGER NOT NULL DEFAULT 0,
    committed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    undone_at TEXT,
    last_error TEXT
);

CREATE TABLE IF NOT EXISTS commit_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_batch_id INTEGER NOT NULL,
    action_id INTEGER,
    full_hash TEXT NOT NULL,
    size INTEGER NOT NULL,
    retained_path TEXT NOT NULL,
    originals_json TEXT NOT NULL,
    cleanup_paths_json TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'completed'
        CHECK (status IN ('completed', 'error', 'undone')),
    last_error TEXT,
    FOREIGN KEY (commit_batch_id) REFERENCES commit_batches(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_commit_batches_committed
    ON commit_batches(committed_at DESC);
CREATE INDEX IF NOT EXISTS idx_commit_items_batch
    ON commit_items(commit_batch_id);
"""

CURRENT_SCHEMA_VERSION = 3


def normalize_path(path: str | Path) -> str:
    return str(Path(path).expanduser().resolve())


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=30,
            factory=ClosingConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)
            connection.execute(
                """
                UPDATE commit_batches
                SET status = 'partial',
                    last_error = COALESCE(
                        last_error,
                        'A previous commit was interrupted. Undo is available for journaled operations.'
                    )
                WHERE status = 'pending'
                """
            )

    def _migrate(self, connection: sqlite3.Connection) -> None:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if version < 2:
            duplicate_columns = self._column_names(connection, "duplicate_groups")
            if "membership_fingerprint" not in duplicate_columns:
                connection.execute(
                    """
                    ALTER TABLE duplicate_groups
                    ADD COLUMN membership_fingerprint TEXT NOT NULL DEFAULT ''
                    """
                )

            pending_columns = self._column_names(connection, "pending_actions")
            if "batch_id" not in pending_columns:
                connection.execute("ALTER TABLE pending_actions ADD COLUMN batch_id INTEGER")
            if "review_key" not in pending_columns:
                connection.execute(
                    "ALTER TABLE pending_actions ADD COLUMN review_key TEXT NOT NULL DEFAULT ''"
                )
            if "review_fingerprint" not in pending_columns:
                connection.execute(
                    """
                    ALTER TABLE pending_actions
                    ADD COLUMN review_fingerprint TEXT NOT NULL DEFAULT ''
                    """
                )

            batch_columns = self._column_names(connection, "action_batches")
            if "locations_json" not in batch_columns:
                connection.execute(
                    """
                    ALTER TABLE action_batches
                    ADD COLUMN locations_json TEXT NOT NULL DEFAULT '[]'
                    """
                )

            legacy_rows = connection.execute(
                """
                SELECT pa.*, dg.full_hash
                FROM pending_actions pa
                LEFT JOIN duplicate_groups dg ON dg.id = pa.group_id
                WHERE pa.batch_id IS NULL AND pa.status IN ('pending', 'error')
                """
            ).fetchall()
            for row in legacy_rows:
                review_key = row["review_key"] or f"file:{row['root_path']}:{row['full_hash'] or row['group_id']}"
                cursor = connection.execute(
                    """
                    INSERT INTO action_batches(
                        root_path, review_key, review_fingerprint, kind, label,
                        target_root, locations_json, status, estimated_savings, last_error
                    )
                    VALUES (?, ?, ?, 'legacy', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["root_path"],
                        review_key,
                        row["review_fingerprint"],
                        Path(row["target_path"]).name,
                        row["target_path"],
                        row["paths_json"],
                        "error" if row["status"] == "error" else "pending",
                        row["size"] * max(0, len(json.loads(row["paths_json"])) - 1),
                        row["last_error"],
                    ),
                )
                connection.execute(
                    """
                    UPDATE pending_actions
                    SET batch_id = ?, review_key = ?
                    WHERE id = ?
                    """,
                    (cursor.lastrowid, review_key, row["id"]),
                )

        if version < 3:
            commit_item_columns = self._column_names(connection, "commit_items")
            if "cleanup_paths_json" not in commit_item_columns:
                connection.execute(
                    """
                    ALTER TABLE commit_items
                    ADD COLUMN cleanup_paths_json TEXT NOT NULL DEFAULT '[]'
                    """
                )

        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_pending_actions_batch ON pending_actions(batch_id)"
        )
        connection.execute(f"PRAGMA user_version = {CURRENT_SCHEMA_VERSION}")

    @staticmethod
    def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
        return {
            row["name"]
            for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings(key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )

    def prepare_root(self, root_path: str | Path, min_size_bytes: int) -> str:
        root = normalize_path(root_path)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT min_size_bytes FROM scan_roots WHERE path = ?",
                (root,),
            ).fetchone()
            connection.execute(
                """
                INSERT INTO scan_roots(path, min_size_bytes) VALUES (?, ?)
                ON CONFLICT(path) DO UPDATE SET min_size_bytes = excluded.min_size_bytes
                """,
                (root, min_size_bytes),
            )
            if row and row["min_size_bytes"] != min_size_bytes:
                connection.execute(
                    """
                    UPDATE folders
                    SET status = 'pending', completed_at = NULL, last_error = NULL
                    WHERE root_path = ?
                    """,
                    (root,),
                )
        return root

    def register_folders(self, root_path: str, folder_paths: Sequence[str]) -> None:
        folders = [(root_path, normalize_path(path)) for path in folder_paths]
        with self.transaction() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO folders(root_path, path) VALUES (?, ?)",
                folders,
            )
            if folders:
                placeholders = ",".join("?" for _ in folders)
                values = [root_path, *(path for _, path in folders)]
                connection.execute(
                    f"DELETE FROM folders WHERE root_path = ? AND path NOT IN ({placeholders})",
                    values,
                )
            else:
                connection.execute("DELETE FROM folders WHERE root_path = ?", (root_path,))
            self._cleanup_groups(connection, root_path)

    def register_folder(self, root_path: str, folder_path: str | Path) -> str:
        folder = normalize_path(folder_path)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO folders(root_path, path) VALUES (?, ?)",
                (root_path, folder),
            )
        return folder

    def folder_status(self, root_path: str, folder_path: str | Path) -> str:
        folder = normalize_path(folder_path)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM folders WHERE root_path = ? AND path = ?",
                (root_path, folder),
            ).fetchone()
        return row["status"] if row else "pending"

    def prune_folders(self, root_path: str, folder_paths: Sequence[str]) -> None:
        folders = [normalize_path(path) for path in folder_paths]
        with self.transaction() as connection:
            if folders:
                placeholders = ",".join("?" for _ in folders)
                connection.execute(
                    f"DELETE FROM folders WHERE root_path = ? AND path NOT IN ({placeholders})",
                    (root_path, *folders),
                )
            else:
                connection.execute("DELETE FROM folders WHERE root_path = ?", (root_path,))
            self._cleanup_groups(connection, root_path)

    def folders_to_scan(self, root_path: str, traversal_order: Sequence[str]) -> list[str]:
        normalized = [normalize_path(path) for path in traversal_order]
        with self.transaction() as connection:
            pending_rows = connection.execute(
                """
                SELECT path FROM folders
                WHERE root_path = ? AND status != 'completed'
                """,
                (root_path,),
            ).fetchall()
            pending = {row["path"] for row in pending_rows}
            if not pending and normalized:
                connection.execute(
                    """
                    UPDATE folders
                    SET status = 'pending', completed_at = NULL, last_error = NULL
                    WHERE root_path = ?
                    """,
                    (root_path,),
                )
                connection.execute(
                    "UPDATE scan_roots SET cycle_started_at = CURRENT_TIMESTAMP WHERE path = ?",
                    (root_path,),
                )
                pending = set(normalized)
        return [path for path in normalized if path in pending]

    def reset_scan_progress(self, root_path: str | Path) -> None:
        root = normalize_path(root_path)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE folders
                SET status = 'pending', completed_at = NULL, last_error = NULL
                WHERE root_path = ?
                """,
                (root,),
            )

    def mark_folder(self, root_path: str, folder_path: str, status: str, error: str | None = None) -> None:
        completed = "CURRENT_TIMESTAMP" if status == "completed" else "NULL"
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE folders
                SET status = ?, completed_at = {completed}, last_error = ?
                WHERE root_path = ? AND path = ?
                """,
                (status, error, root_path, normalize_path(folder_path)),
            )

    def upsert_file(
        self,
        root_path: str,
        folder_path: str,
        path: str | Path,
        size: int,
        mtime_ns: int,
        ctime_ns: int,
    ) -> FileRecord:
        normalized = normalize_path(path)
        folder = normalize_path(folder_path)
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM files WHERE root_path = ? AND path = ?",
                (root_path, normalized),
            ).fetchone()
            changed = bool(
                existing
                and (
                    existing["size"] != size
                    or existing["mtime_ns"] != mtime_ns
                    or existing["ctime_ns"] != ctime_ns
                    or existing["root_path"] != root_path
                )
            )
            if changed:
                connection.execute(
                    "DELETE FROM duplicate_group_files WHERE file_id = ?",
                    (existing["id"],),
                )
            connection.execute(
                """
                INSERT INTO files(
                    root_path, folder_path, path, size, mtime_ns, ctime_ns,
                    partial_hash, full_hash, last_seen_at
                )
                VALUES (?, ?, ?, ?, ?, ?, NULL, NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(root_path, path) DO UPDATE SET
                    root_path = excluded.root_path,
                    folder_path = excluded.folder_path,
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    ctime_ns = excluded.ctime_ns,
                    partial_hash = CASE
                        WHEN files.size != excluded.size
                          OR files.mtime_ns != excluded.mtime_ns
                          OR files.ctime_ns != excluded.ctime_ns
                        THEN NULL ELSE files.partial_hash END,
                    full_hash = CASE
                        WHEN files.size != excluded.size
                          OR files.mtime_ns != excluded.mtime_ns
                          OR files.ctime_ns != excluded.ctime_ns
                        THEN NULL ELSE files.full_hash END,
                    last_seen_at = CURRENT_TIMESTAMP
                """,
                (root_path, folder, normalized, size, mtime_ns, ctime_ns),
            )
            row = connection.execute(
                "SELECT * FROM files WHERE root_path = ? AND path = ?",
                (root_path, normalized),
            ).fetchone()
            self._cleanup_groups(connection, root_path)
        return self._file_from_row(row)

    def sync_folder_paths(self, root_path: str, folder_path: str, seen_paths: Sequence[str]) -> None:
        folder = normalize_path(folder_path)
        normalized = [normalize_path(path) for path in seen_paths]
        with self.transaction() as connection:
            if normalized:
                placeholders = ",".join("?" for _ in normalized)
                connection.execute(
                    f"""
                    DELETE FROM files
                    WHERE root_path = ? AND folder_path = ? AND path NOT IN ({placeholders})
                    """,
                    (root_path, folder, *normalized),
                )
            else:
                connection.execute(
                    "DELETE FROM files WHERE root_path = ? AND folder_path = ?",
                    (root_path, folder),
                )
            self._cleanup_groups(connection, root_path)

    def remove_file(self, root_path: str, path: str | Path) -> None:
        normalized = normalize_path(path)
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM files WHERE root_path = ? AND path = ?",
                (root_path, normalized),
            )
            self._cleanup_groups(connection, root_path)

    def get_files_by_size(self, root_path: str, size: int) -> list[FileRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM files
                WHERE root_path = ? AND size = ?
                ORDER BY path COLLATE NOCASE
                """,
                (root_path, size),
            ).fetchall()
        return [self._file_from_row(row) for row in rows]

    def list_files(self, root_path: str | Path) -> list[FileRecord]:
        root = normalize_path(root_path)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM files
                WHERE root_path = ?
                ORDER BY path COLLATE NOCASE
                """,
                (root,),
            ).fetchall()
        return [self._file_from_row(row) for row in rows]

    def folder_scope_complete(self, root_path: str | Path, folder_path: str | Path) -> bool:
        root = normalize_path(root_path)
        folder = Path(normalize_path(folder_path))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT path, status FROM folders WHERE root_path = ?",
                (root,),
            ).fetchall()
        matching = [
            row
            for row in rows
            if Path(row["path"]) == folder or Path(row["path"]).is_relative_to(folder)
        ]
        return bool(matching) and all(row["status"] == "completed" for row in matching)

    def update_file_hash(
        self,
        file_id: int,
        *,
        partial: str | None = None,
        full: str | None = None,
    ) -> None:
        assignments: list[str] = []
        values: list[object] = []
        if partial is not None:
            assignments.append("partial_hash = ?")
            values.append(partial)
        if full is not None:
            assignments.append("full_hash = ?")
            values.append(full)
        if not assignments:
            return
        values.append(file_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE files SET {', '.join(assignments)} WHERE id = ?",
                values,
            )

    def ensure_duplicate_group(
        self,
        root_path: str,
        size: int,
        full_hash: str,
        file_ids: Sequence[int],
    ) -> tuple[int, bool]:
        unique_ids = sorted(set(file_ids))
        if len(unique_ids) < 2:
            raise ValueError("A duplicate group requires at least two files")

        with self.transaction() as connection:
            existing = connection.execute(
                """
                SELECT id, resolved_at FROM duplicate_groups
                WHERE root_path = ? AND size = ? AND full_hash = ?
                """,
                (root_path, size, full_hash),
            ).fetchone()
            if existing:
                group_id = existing["id"]
                old_ids = {
                    row["file_id"]
                    for row in connection.execute(
                        "SELECT file_id FROM duplicate_group_files WHERE group_id = ?",
                        (group_id,),
                    )
                }
                changed = old_ids != set(unique_ids) or existing["resolved_at"] is not None
                connection.execute(
                    "UPDATE duplicate_groups SET resolved_at = NULL WHERE id = ?",
                    (group_id,),
                )
                connection.execute(
                    "DELETE FROM duplicate_group_files WHERE group_id = ?",
                    (group_id,),
                )
            else:
                cursor = connection.execute(
                    """
                    INSERT INTO duplicate_groups(root_path, size, full_hash)
                    VALUES (?, ?, ?)
                    """,
                    (root_path, size, full_hash),
                )
                group_id = int(cursor.lastrowid)
                changed = True
            connection.executemany(
                "INSERT INTO duplicate_group_files(group_id, file_id) VALUES (?, ?)",
                [(group_id, file_id) for file_id in unique_ids],
            )
        return group_id, changed

    def update_group_membership_fingerprint(self, group_id: int, fingerprint: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE duplicate_groups SET membership_fingerprint = ? WHERE id = ?",
                (fingerprint, group_id),
            )

    def get_duplicate_group(self, group_id: int) -> DuplicateGroup | None:
        with self._connect() as connection:
            group_row = connection.execute(
                """
                SELECT * FROM duplicate_groups
                WHERE id = ? AND resolved_at IS NULL
                """,
                (group_id,),
            ).fetchone()
            if not group_row:
                return None
            file_rows = connection.execute(
                """
                SELECT f.* FROM files f
                JOIN duplicate_group_files dgf ON dgf.file_id = f.id
                WHERE dgf.group_id = ?
                ORDER BY f.mtime_ns, f.ctime_ns, f.path COLLATE NOCASE
                """,
                (group_id,),
            ).fetchall()
        files = tuple(self._file_from_row(row) for row in file_rows)
        if len(files) < 2:
            return None
        return DuplicateGroup(
            id=group_row["id"],
            root_path=group_row["root_path"],
            size=group_row["size"],
            full_hash=group_row["full_hash"],
            files=files,
        )

    def list_duplicate_groups(self, root_path: str | Path | None = None) -> list[DuplicateGroup]:
        parameters: tuple[object, ...] = ()
        where = "WHERE dg.resolved_at IS NULL"
        if root_path is not None:
            where += " AND dg.root_path = ?"
            parameters = (normalize_path(root_path),)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT dg.id
                FROM duplicate_groups dg
                JOIN duplicate_group_files dgf ON dgf.group_id = dg.id
                {where}
                GROUP BY dg.id
                HAVING COUNT(*) >= 2
                ORDER BY dg.discovered_at DESC, dg.id DESC
                """,
                parameters,
            ).fetchall()
        groups: list[DuplicateGroup] = []
        for row in rows:
            group = self.get_duplicate_group(row["id"])
            if group:
                groups.append(group)
        return groups

    def stage_action(self, group_id: int, target_path: str | Path) -> PendingAction:
        group = self.get_duplicate_group(group_id)
        if not group:
            raise ValueError("Duplicate group no longer exists")
        review_key = f"file:{group.root_path}:{group.size}:{group.full_hash}"
        fingerprint = self.group_membership_fingerprint(group)
        batch = self.stage_batch(
            root_path=group.root_path,
            review_key=review_key,
            review_fingerprint=fingerprint,
            kind="file",
            label=group.files[0].name if group.files else "Duplicate file",
            target_root=normalize_path(target_path),
            estimated_savings=group.size * max(0, len(group.files) - 1),
            locations=[file.path for file in group.files],
            action_targets=[(group.id, normalize_path(target_path))],
        )
        return batch.actions[0]

    def stage_batch(
        self,
        *,
        root_path: str,
        review_key: str,
        review_fingerprint: str,
        kind: str,
        label: str,
        target_root: str,
        estimated_savings: int,
        locations: Sequence[str],
        action_targets: Sequence[tuple[int, str]],
    ) -> ActionBatch:
        if not action_targets:
            raise ValueError("An action batch must contain at least one duplicate group")

        prepared: list[tuple[DuplicateGroup, str]] = []
        for group_id, target_path in action_targets:
            group = self.get_duplicate_group(group_id)
            if not group:
                raise ValueError(f"Duplicate group {group_id} no longer exists")
            target = normalize_path(target_path)
            if target not in {file.path for file in group.files}:
                raise ValueError(f"Target path is not in duplicate group {group_id}: {target}")
            prepared.append((group, target))

        normalized_root = normalize_path(root_path)
        with self.transaction() as connection:
            old_batch_rows = connection.execute(
                """
                SELECT DISTINCT batch_id
                FROM pending_actions
                WHERE group_id IN ({})
                  AND batch_id IS NOT NULL
                  AND status IN ('pending', 'error')
                """.format(",".join("?" for _ in prepared)),
                tuple(group.id for group, _ in prepared),
            ).fetchall()
            old_batch_ids = [row["batch_id"] for row in old_batch_rows]
            if old_batch_ids:
                placeholders = ",".join("?" for _ in old_batch_ids)
                connection.execute(
                    f"DELETE FROM pending_actions WHERE batch_id IN ({placeholders})",
                    old_batch_ids,
                )
                connection.execute(
                    f"DELETE FROM action_batches WHERE id IN ({placeholders})",
                    old_batch_ids,
                )

            connection.execute(
                """
                DELETE FROM ignored_review_items
                WHERE review_key = ?
                """,
                (review_key,),
            )
            cursor = connection.execute(
                """
                INSERT INTO action_batches(
                    root_path, review_key, review_fingerprint, kind, label,
                    target_root, locations_json, status, estimated_savings
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    normalized_root,
                    review_key,
                    review_fingerprint,
                    kind,
                    label,
                    normalize_path(target_root),
                    json.dumps([normalize_path(location) for location in locations]),
                    estimated_savings,
                ),
            )
            batch_id = int(cursor.lastrowid)

            for group, target in prepared:
                paths = tuple(file.path for file in group.files)
                source = group.oldest_file.path
                connection.execute(
                    """
                    INSERT INTO pending_actions(
                        group_id, root_path, source_path, target_path, paths_json,
                        size, full_hash, status, last_error, batch_id, review_key,
                        review_fingerprint, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(group_id) DO UPDATE SET
                        root_path = excluded.root_path,
                        source_path = excluded.source_path,
                        target_path = excluded.target_path,
                        paths_json = excluded.paths_json,
                        size = excluded.size,
                        full_hash = excluded.full_hash,
                        status = 'pending',
                        last_error = NULL,
                        batch_id = excluded.batch_id,
                        review_key = excluded.review_key,
                        review_fingerprint = excluded.review_fingerprint,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        group.id,
                        group.root_path,
                        source,
                        target,
                        json.dumps(paths),
                        group.size,
                        group.full_hash,
                        batch_id,
                        review_key,
                        review_fingerprint,
                    ),
                )

        batch = self.get_action_batch(batch_id)
        if not batch:
            raise RuntimeError("Could not reload the staged action batch")
        return batch

    @staticmethod
    def group_membership_fingerprint(group: DuplicateGroup) -> str:
        payload = [
            (
                file.path.casefold(),
                file.size,
                file.mtime_ns,
                file.ctime_ns,
                file.full_hash,
            )
            for file in sorted(group.files, key=lambda item: item.path.casefold())
        ]
        return hashlib.sha256(
            json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        ).hexdigest()

    def list_pending_actions(self) -> list[PendingAction]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM pending_actions
                WHERE status IN ('pending', 'error')
                ORDER BY created_at, id
                """
            ).fetchall()
        return [self._action_from_row(row) for row in rows]

    def list_action_batches(self) -> list[ActionBatch]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM action_batches
                WHERE status IN ('pending', 'stale', 'error')
                ORDER BY created_at, id
                """
            ).fetchall()
        return [self._batch_from_row(row) for row in rows]

    def get_action_batch(self, batch_id: int) -> ActionBatch | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM action_batches WHERE id = ?",
                (batch_id,),
            ).fetchone()
        return self._batch_from_row(row) if row else None

    def find_action_batch(self, review_key: str) -> ActionBatch | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM action_batches
                WHERE review_key = ? AND status IN ('pending', 'stale', 'error')
                ORDER BY id DESC LIMIT 1
                """,
                (review_key,),
            ).fetchone()
        return self._batch_from_row(row) if row else None

    def remove_action_batch(self, batch_id: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                "DELETE FROM pending_actions WHERE batch_id = ? AND status != 'completed'",
                (batch_id,),
            )
            connection.execute("DELETE FROM action_batches WHERE id = ?", (batch_id,))

    def set_action_batch_status(
        self,
        batch_id: int,
        status: str,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE action_batches
                SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, error, batch_id),
            )

    def active_review_batches(self, root_path: str | Path) -> dict[str, ActionBatch]:
        root = normalize_path(root_path)
        return {
            batch.review_key: batch
            for batch in self.list_action_batches()
            if batch.root_path == root
        }

    def ignore_review_item(
        self,
        *,
        review_key: str,
        root_path: str,
        fingerprint: str,
        kind: str,
        snapshot_json: str,
    ) -> None:
        existing = self.find_action_batch(review_key)
        if existing:
            self.remove_action_batch(existing.id)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ignored_review_items(
                    review_key, root_path, fingerprint, kind, snapshot_json, ignored_at
                )
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(review_key) DO UPDATE SET
                    root_path = excluded.root_path,
                    fingerprint = excluded.fingerprint,
                    kind = excluded.kind,
                    snapshot_json = excluded.snapshot_json,
                    ignored_at = CURRENT_TIMESTAMP
                """,
                (review_key, normalize_path(root_path), fingerprint, kind, snapshot_json),
            )

    def restore_ignored_item(self, review_key: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM ignored_review_items WHERE review_key = ?",
                (review_key,),
            )

    def ignored_review_keys(self, root_path: str | Path) -> dict[str, str]:
        root = normalize_path(root_path)
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT review_key, fingerprint
                FROM ignored_review_items
                WHERE root_path = ?
                """,
                (root,),
            ).fetchall()
        return {row["review_key"]: row["fingerprint"] for row in rows}

    def reconcile_review_items(self, root_path: str | Path, fingerprints: dict[str, str]) -> None:
        root = normalize_path(root_path)
        with self.transaction() as connection:
            ignored = connection.execute(
                """
                SELECT review_key, fingerprint
                FROM ignored_review_items
                WHERE root_path = ?
                """,
                (root,),
            ).fetchall()
            obsolete_ignored = [
                row["review_key"]
                for row in ignored
                if fingerprints.get(row["review_key"]) != row["fingerprint"]
            ]
            if obsolete_ignored:
                placeholders = ",".join("?" for _ in obsolete_ignored)
                connection.execute(
                    f"DELETE FROM ignored_review_items WHERE review_key IN ({placeholders})",
                    obsolete_ignored,
                )

            batches = connection.execute(
                """
                SELECT id, review_key, review_fingerprint, status
                FROM action_batches
                WHERE root_path = ? AND status IN ('pending', 'error')
                """,
                (root,),
            ).fetchall()
            for row in batches:
                if fingerprints.get(row["review_key"]) != row["review_fingerprint"]:
                    connection.execute(
                        """
                        UPDATE action_batches
                        SET status = 'stale',
                            last_error = 'Duplicate membership changed; restage this decision.',
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (row["id"],),
                    )

    def begin_commit_batch(self, batch: ActionBatch) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO commit_batches(
                    action_batch_id, root_path, review_key, label, status
                )
                VALUES (?, ?, ?, ?, 'pending')
                """,
                (batch.id, batch.root_path, batch.review_key, batch.label),
            )
            return int(cursor.lastrowid)

    def add_commit_item(
        self,
        *,
        commit_batch_id: int,
        action_id: int,
        full_hash: str,
        size: int,
        retained_path: str,
        originals_json: str,
        cleanup_paths_json: str = "[]",
        status: str = "error",
        error: str | None = None,
    ) -> int:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO commit_items(
                    commit_batch_id, action_id, full_hash, size, retained_path,
                    originals_json, cleanup_paths_json, status, last_error
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    commit_batch_id,
                    action_id,
                    full_hash,
                    size,
                    normalize_path(retained_path),
                    originals_json,
                    cleanup_paths_json,
                    status,
                    error,
                ),
            )
            return int(cursor.lastrowid)

    def update_commit_item(
        self,
        commit_item_id: int,
        *,
        retained_path: str,
        cleanup_paths: Sequence[str | Path],
        status: str,
        error: str | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE commit_items
                SET retained_path = ?, cleanup_paths_json = ?, status = ?, last_error = ?
                WHERE id = ?
                """,
                (
                    normalize_path(retained_path),
                    json.dumps([normalize_path(path) for path in cleanup_paths]),
                    status,
                    error,
                    commit_item_id,
                ),
            )

    def finish_commit_batch(
        self,
        commit_batch_id: int,
        action_batch_id: int,
        *,
        status: str,
        recovered_bytes: int,
        error: str | None = None,
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE commit_batches
                SET status = ?, recovered_bytes = ?, last_error = ?
                WHERE id = ?
                """,
                (status, recovered_bytes, error, commit_batch_id),
            )
            connection.execute(
                """
                UPDATE action_batches
                SET status = ?, last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (
                    "completed" if status == "completed" else "error",
                    error,
                    action_batch_id,
                ),
            )

    def list_commit_history(self) -> list[CommitHistory]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM commit_batches
                ORDER BY committed_at DESC, id DESC
                """
            ).fetchall()
        return [self._history_from_row(row) for row in rows]

    def get_commit_history(self, commit_batch_id: int) -> CommitHistory | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM commit_batches WHERE id = ?",
                (commit_batch_id,),
            ).fetchone()
        return self._history_from_row(row) if row else None

    def find_retry_commit(self, action_batch_id: int) -> CommitHistory | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM commit_batches
                WHERE action_batch_id = ? AND status IN ('partial', 'error')
                ORDER BY id DESC LIMIT 1
                """,
                (action_batch_id,),
            ).fetchone()
        return self._history_from_row(row) if row else None

    def mark_commit_undone(self, commit_batch_id: int) -> None:
        with self.transaction() as connection:
            connection.execute(
                """
                UPDATE commit_items SET status = 'undone', last_error = NULL
                WHERE commit_batch_id = ? AND status = 'completed'
                """,
                (commit_batch_id,),
            )
            connection.execute(
                """
                UPDATE commit_batches
                SET status = 'undone', undone_at = CURRENT_TIMESTAMP, last_error = NULL
                WHERE id = ?
                """,
                (commit_batch_id,),
            )
            row = connection.execute(
                "SELECT action_batch_id FROM commit_batches WHERE id = ?",
                (commit_batch_id,),
            ).fetchone()
            if row and row["action_batch_id"]:
                connection.execute(
                    "DELETE FROM pending_actions WHERE batch_id = ?",
                    (row["action_batch_id"],),
                )
                connection.execute(
                    "DELETE FROM action_batches WHERE id = ?",
                    (row["action_batch_id"],),
                )

    def mark_commit_item_undone(self, commit_item_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE commit_items
                SET status = 'undone', last_error = NULL
                WHERE id = ?
                """,
                (commit_item_id,),
            )

    def restore_group_records(
        self,
        root_path: str,
        full_hash: str,
        originals: Sequence[dict[str, object]],
    ) -> int:
        file_ids: list[int] = []
        size = 0
        for snapshot in originals:
            path = Path(str(snapshot["path"]))
            folder = self.register_folder(root_path, path.parent)
            record = self.upsert_file(
                root_path,
                folder,
                path,
                int(snapshot["size"]),
                int(snapshot["mtime_ns"]),
                int(snapshot["ctime_ns"]),
            )
            self.update_file_hash(record.id, full=full_hash)
            file_ids.append(record.id)
            size = int(snapshot["size"])
        group_id, _changed = self.ensure_duplicate_group(
            root_path,
            size,
            full_hash,
            file_ids,
        )
        return group_id

    def mark_commit_undo_error(self, commit_batch_id: int, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE commit_batches
                SET status = 'undo_error', last_error = ?
                WHERE id = ?
                """,
                (message, commit_batch_id),
            )

    def get_pending_action(self, action_id: int) -> PendingAction | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM pending_actions
                WHERE id = ? AND status IN ('pending', 'error')
                """,
                (action_id,),
            ).fetchone()
        return self._action_from_row(row) if row else None

    def remove_pending_action(self, action_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM pending_actions WHERE id = ?", (action_id,))

    def update_pending_target(self, action_id: int, target_path: str | Path) -> PendingAction:
        action = self.get_pending_action(action_id)
        if not action:
            raise ValueError("Pending action no longer exists")
        target = normalize_path(target_path)
        if target not in action.paths:
            raise ValueError("The target path is not part of this staged duplicate set")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE pending_actions
                SET target_path = ?, status = 'pending', last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (target, action_id),
            )
            connection.execute(
                """
                UPDATE action_batches
                SET target_root = ?, status = 'pending', last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (target, action.batch_id),
            )
        updated = self.get_pending_action(action_id)
        if not updated:
            raise RuntimeError("Could not reload the updated pending action")
        return updated

    def mark_action_error(self, action_id: int, message: str) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT batch_id FROM pending_actions WHERE id = ?",
                (action_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE pending_actions
                SET status = 'error', last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (message, action_id),
            )
            if row and row["batch_id"]:
                connection.execute(
                    """
                    UPDATE action_batches
                    SET status = 'error', last_error = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (message, row["batch_id"]),
                )

    def rebase_action_after_partial_commit(
        self,
        action_id: int,
        retained_path: str | Path,
        remaining_paths: Sequence[str | Path],
        message: str,
        target_path: str | Path | None = None,
    ) -> None:
        retained = normalize_path(retained_path)
        target = normalize_path(target_path or retained_path)
        paths = [retained, *(normalize_path(path) for path in remaining_paths)]
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE pending_actions
                SET source_path = ?, target_path = ?, paths_json = ?,
                    status = 'error', last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (retained, target, json.dumps(paths), message, action_id),
            )
            connection.execute(
                """
                UPDATE action_batches
                SET status = 'error', last_error = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = (SELECT batch_id FROM pending_actions WHERE id = ?)
                """,
                (message, action_id),
            )

    def record_action_success(
        self,
        action: PendingAction,
        retained_path: str | Path,
        *,
        size: int,
        mtime_ns: int,
        ctime_ns: int,
    ) -> None:
        retained = normalize_path(retained_path)
        folder = str(Path(retained).parent)
        with self.transaction() as connection:
            group_path_rows = connection.execute(
                """
                SELECT f.path
                FROM files f
                JOIN duplicate_group_files dgf ON dgf.file_id = f.id
                WHERE dgf.group_id = ?
                """,
                (action.group_id,),
            ).fetchall()
            cached_paths = {
                *action.paths,
                *(row["path"] for row in group_path_rows),
            }
            connection.execute(
                """
                UPDATE pending_actions
                SET status = 'completed', last_error = NULL, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (action.id,),
            )
            if action.batch_id:
                remaining = connection.execute(
                    """
                    SELECT 1 FROM pending_actions
                    WHERE batch_id = ? AND status IN ('pending', 'error') AND id != ?
                    LIMIT 1
                    """,
                    (action.batch_id, action.id),
                ).fetchone()
                if not remaining:
                    connection.execute(
                        """
                        UPDATE action_batches
                        SET status = 'completed', last_error = NULL,
                            updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                        """,
                        (action.batch_id,),
                    )
            connection.execute(
                "UPDATE duplicate_groups SET resolved_at = CURRENT_TIMESTAMP WHERE id = ?",
                (action.group_id,),
            )
            connection.execute(
                f"""
                DELETE FROM files
                WHERE root_path = ? AND path IN ({','.join('?' for _ in cached_paths)})
                """,
                (action.root_path, *cached_paths),
            )
            folder_exists = connection.execute(
                "SELECT 1 FROM folders WHERE root_path = ? AND path = ?",
                (action.root_path, folder),
            ).fetchone()
            if folder_exists:
                connection.execute(
                    """
                    INSERT INTO files(
                        root_path, folder_path, path, size, mtime_ns, ctime_ns,
                        partial_hash, full_hash
                    )
                    VALUES (?, ?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(root_path, path) DO UPDATE SET
                        root_path = excluded.root_path,
                        folder_path = excluded.folder_path,
                        size = excluded.size,
                        mtime_ns = excluded.mtime_ns,
                        ctime_ns = excluded.ctime_ns,
                        partial_hash = NULL,
                        full_hash = excluded.full_hash,
                        last_seen_at = CURRENT_TIMESTAMP
                    """,
                    (
                        action.root_path,
                        folder,
                        retained,
                        size,
                        mtime_ns,
                        ctime_ns,
                        action.full_hash,
                    ),
                )

    @staticmethod
    def _cleanup_groups(connection: sqlite3.Connection, root_path: str) -> None:
        connection.execute(
            """
            DELETE FROM duplicate_groups
            WHERE root_path = ?
              AND id IN (
                  SELECT dg.id
                  FROM duplicate_groups dg
                  LEFT JOIN duplicate_group_files dgf ON dgf.group_id = dg.id
                  WHERE dg.root_path = ?
                  GROUP BY dg.id
                  HAVING COUNT(dgf.file_id) < 2
              )
            """,
            (root_path, root_path),
        )

    @staticmethod
    def _file_from_row(row: sqlite3.Row) -> FileRecord:
        return FileRecord(
            id=row["id"],
            root_path=row["root_path"],
            folder_path=row["folder_path"],
            path=row["path"],
            size=row["size"],
            mtime_ns=row["mtime_ns"],
            ctime_ns=row["ctime_ns"],
            partial_hash=row["partial_hash"],
            full_hash=row["full_hash"],
        )

    @staticmethod
    def _action_from_row(row: sqlite3.Row) -> PendingAction:
        return PendingAction(
            id=row["id"],
            group_id=row["group_id"],
            root_path=row["root_path"],
            source_path=row["source_path"],
            target_path=row["target_path"],
            paths=tuple(json.loads(row["paths_json"])),
            size=row["size"],
            full_hash=row["full_hash"],
            status=row["status"],
            last_error=row["last_error"],
            batch_id=row["batch_id"] if "batch_id" in row.keys() else None,
        )

    def _batch_from_row(self, row: sqlite3.Row) -> ActionBatch:
        with self._connect() as connection:
            action_rows = connection.execute(
                """
                SELECT * FROM pending_actions
                WHERE batch_id = ? AND status IN ('pending', 'error')
                ORDER BY id
                """,
                (row["id"],),
            ).fetchall()
        return ActionBatch(
            id=row["id"],
            root_path=row["root_path"],
            review_key=row["review_key"],
            review_fingerprint=row["review_fingerprint"],
            kind=row["kind"],
            label=row["label"],
            target_root=row["target_root"],
            status=row["status"],
            estimated_savings=row["estimated_savings"],
            last_error=row["last_error"],
            locations=tuple(json.loads(row["locations_json"])),
            actions=tuple(self._action_from_row(action_row) for action_row in action_rows),
        )

    def _history_from_row(self, row: sqlite3.Row) -> CommitHistory:
        with self._connect() as connection:
            item_rows = connection.execute(
                """
                SELECT * FROM commit_items
                WHERE commit_batch_id = ?
                ORDER BY id
                """,
                (row["id"],),
            ).fetchall()
        items = tuple(
            CommitItem(
                id=item["id"],
                commit_batch_id=item["commit_batch_id"],
                action_id=item["action_id"],
                full_hash=item["full_hash"],
                size=item["size"],
                retained_path=item["retained_path"],
                originals_json=item["originals_json"],
                cleanup_paths_json=item["cleanup_paths_json"],
                status=item["status"],
                last_error=item["last_error"],
            )
            for item in item_rows
        )
        return CommitHistory(
            id=row["id"],
            action_batch_id=row["action_batch_id"],
            root_path=row["root_path"],
            label=row["label"],
            review_key=row["review_key"],
            committed_at=row["committed_at"],
            undone_at=row["undone_at"],
            status=row["status"],
            recovered_bytes=row["recovered_bytes"],
            last_error=row["last_error"],
            items=items,
        )
