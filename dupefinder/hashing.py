from __future__ import annotations

import hashlib
import os
from pathlib import Path

PARTIAL_CHUNK_SIZE = 64 * 1024
FULL_CHUNK_SIZE = 1024 * 1024


class FileChangedError(OSError):
    """Raised when a file changes while it is being hashed."""


def _stable_stat(path: Path) -> os.stat_result:
    return path.stat()


def _verify_unchanged(path: Path, before: os.stat_result) -> None:
    after = path.stat()
    if after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
        raise FileChangedError(f"File changed while hashing: {path}")


def partial_hash(path: str | Path) -> str:
    file_path = Path(path)
    before = _stable_stat(file_path)
    digest = hashlib.blake2b(digest_size=16)
    digest.update(before.st_size.to_bytes(8, byteorder="little", signed=False))

    with file_path.open("rb") as handle:
        if before.st_size <= PARTIAL_CHUNK_SIZE * 2:
            digest.update(handle.read())
        else:
            digest.update(handle.read(PARTIAL_CHUNK_SIZE))
            handle.seek(-PARTIAL_CHUNK_SIZE, os.SEEK_END)
            digest.update(handle.read(PARTIAL_CHUNK_SIZE))

    _verify_unchanged(file_path, before)
    return digest.hexdigest()

def full_hash(path: str | Path) -> str:
    file_path = Path(path)
    before = _stable_stat(file_path)
    digest = hashlib.blake2b(digest_size=32)

    with file_path.open("rb") as handle:
        while chunk := handle.read(FULL_CHUNK_SIZE):
            digest.update(chunk)

    _verify_unchanged(file_path, before)
    return digest.hexdigest()
