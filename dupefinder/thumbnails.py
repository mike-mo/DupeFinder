from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QFileInfo, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFileIconProvider

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


def ensure_thumbnail(
    source: str | Path,
    cache_dir: str | Path,
    size: int = 96,
) -> Path | None:
    file_path = Path(source)
    if file_path.suffix.casefold() not in IMAGE_EXTENSIONS:
        return None
    stat = file_path.stat()
    key = f"{file_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{size}".encode(
        "utf-8"
    )
    destination = Path(cache_dir) / f"{hashlib.sha256(key).hexdigest()}.png"
    if destination.exists():
        return destination
    with Image.open(file_path) as image:
        image = ImageOps.exif_transpose(image)
        image.thumbnail((size, size), Image.Resampling.LANCZOS)
        if image.mode not in ("RGB", "RGBA"):
            image = image.convert("RGBA")
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, format="PNG")
    return destination


class ThumbnailProvider:
    def __init__(self, cache_dir: str | Path, size: int = 96) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.size = size
        self.icon_provider = QFileIconProvider()

    def pixmap(self, path: str | Path) -> QPixmap:
        file_path = Path(path)
        if file_path.suffix.casefold() not in IMAGE_EXTENSIONS:
            return self.icon_provider.icon(QFileInfo(str(file_path))).pixmap(QSize(self.size, self.size))

        try:
            cache_path = ensure_thumbnail(file_path, self.cache_dir, self.size)
            if cache_path is None:
                raise ValueError("Not an image")
            pixmap = QPixmap(str(cache_path))
            if not pixmap.isNull():
                return pixmap
        except (
            OSError,
            ValueError,
            SyntaxError,
            Image.DecompressionBombError,
        ):
            pass
        return self.icon_provider.icon(QFileInfo(str(file_path))).pixmap(QSize(self.size, self.size))
