from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThread, Signal

from dupefinder.db import Database


class ClearScanResultsWorker(QThread):
    clear_complete = Signal(str)
    clear_failed = Signal(str)

    def __init__(
        self,
        database_path: str | Path,
        root_path: str,
    ) -> None:
        super().__init__()
        self.database_path = Path(database_path)
        self.root_path = root_path

    def run(self) -> None:
        try:
            Database(self.database_path).clear_scan_results(self.root_path)
        except Exception as exc:
            self.clear_failed.emit(str(exc))
            return
        self.clear_complete.emit(self.root_path)
