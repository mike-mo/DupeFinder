from __future__ import annotations

import os
from pathlib import Path


def user_data_dir() -> Path:
    override = os.environ.get("DUPEFINDER_DATA_DIR")
    if override:
        root = Path(override).expanduser()
    else:
        local_app_data = os.environ.get("LOCALAPPDATA")
        root = Path(local_app_data) / "DupeFinder" if local_app_data else Path.home() / ".dupefinder"

    root.mkdir(parents=True, exist_ok=True)
    return root


def database_path() -> Path:
    return user_data_dir() / "dupefinder.db"


def thumbnail_cache_dir() -> Path:
    path = user_data_dir() / "thumbnails"
    path.mkdir(parents=True, exist_ok=True)
    return path
