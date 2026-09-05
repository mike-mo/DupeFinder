from __future__ import annotations

import hashlib
from pathlib import Path

from PIL import Image, ImageOps
from PySide6.QtCore import QFileInfo, QSize
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QFileIconProvider

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".tif", ".tiff"}


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
            cache_path = self._cache_path(file_path)
            if not cache_path.exists():
                self._generate(file_path, cache_path)
            pixmap = QPixmap(str(cache_path))
            if not pixmap.isNull():
                return pixmap
        except (OSError, ValueError):
            pass
        return self.icon_provider.icon(QFileInfo(str(file_path))).pixmap(QSize(self.size, self.size))

    def _cache_path(self, path: Path) -> Path:
        stat = path.stat()
        key = f"{path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
        return self.cache_dir / f"{hashlib.sha256(key).hexdigest()}.png"

    def _generate(self, source: Path, destination: Path) -> None:
        with Image.open(source) as image:
            image = ImageOps.exif_transpose(image)
            image.thumbnail((self.size, self.size), Image.Resampling.LANCZOS)
            if image.mode not in ("RGB", "RGBA"):
                image = image.convert("RGBA")
            destination.parent.mkdir(parents=True, exist_ok=True)
            image.save(destination, format="PNG")
