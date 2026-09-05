from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QApplication,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from dupefinder.actions import ActionExecutor
from dupefinder.db import Database, normalize_path
from dupefinder.paths import database_path, thumbnail_cache_dir
from dupefinder.review import ReviewService
from dupefinder.scanner import ScanWorker
from dupefinder.thumbnails import ThumbnailProvider
from dupefinder.ui.cart_view import CartView
from dupefinder.ui.duplicates_view import DuplicatesView
from dupefinder.ui.history_view import HistoryView
from dupefinder.ui.ignored_view import IgnoredView


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("DupeFinder")
        self.resize(1220, 800)

        self.db_path = database_path()
        self.database = Database(self.db_path)
        self.review_service = ReviewService(self.database)
        self.worker: ScanWorker | None = None
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(150)
        self.refresh_timer.timeout.connect(self._refresh_views)

        central = QWidget()
        outer = QVBoxLayout(central)
        controls = QGridLayout()

        controls.addWidget(QLabel("Starting folder:"), 0, 0)
        self.folder_edit = QLineEdit()
        self.folder_edit.setReadOnly(True)
        controls.addWidget(self.folder_edit, 0, 1)
        self.choose_button = QPushButton("Choose...")
        self.choose_button.clicked.connect(self._choose_folder)
        controls.addWidget(self.choose_button, 0, 2)

        controls.addWidget(QLabel("Minimum size:"), 1, 0)
        size_row = QHBoxLayout()
        self.min_size = QSpinBox()
        self.min_size.setRange(0, 2_147_483_647)
        self.min_size.setValue(
            int(self.database.get_setting("min_size_kb", "100") or "100")
        )
        self.min_size.setSuffix(" KB")
        self.min_size.setToolTip("1 KB = 1,024 bytes")
        self.min_size.valueChanged.connect(
            lambda value: self.database.set_setting("min_size_kb", str(value))
        )
        size_row.addWidget(self.min_size)
        size_row.addStretch()
        controls.addLayout(size_row, 1, 1)

        button_row = QHBoxLayout()
        self.scan_button = QPushButton("Start scan")
        self.scan_button.clicked.connect(self._start_or_pause_scan)
        button_row.addWidget(self.scan_button)
        self.rescan_button = QPushButton("Rescan everything")
        self.rescan_button.clicked.connect(self._reset_scan)
        button_row.addWidget(self.rescan_button)
        button_row.addStretch()
        controls.addLayout(button_row, 2, 1, 1, 2)
        outer.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        outer.addWidget(self.progress)
        self.status = QLabel("Choose a folder to begin.")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.status)

        thumbnails = ThumbnailProvider(thumbnail_cache_dir())
        self.duplicates = DuplicatesView(self.review_service, thumbnails)
        self.cart = CartView(self.database, self.review_service)
        self.ignored = IgnoredView(self.review_service, thumbnails)
        self.history = HistoryView(self.database)
        self.tabs = QTabWidget()
        self.tabs.addTab(self.duplicates, "Duplicates")
        self.tabs.addTab(self.cart, "Cart")
        self.tabs.addTab(self.ignored, "Ignored")
        self.tabs.addTab(self.history, "History")
        outer.addWidget(self.tabs)

        self.duplicates.batch_staged.connect(self._decision_staged)
        self.duplicates.changed.connect(self._refresh_views)
        self.cart.cart_changed.connect(self._refresh_views)
        self.cart.commit_requested.connect(self._commit_actions)
        self.ignored.changed.connect(self._refresh_views)
        self.ignored.batch_staged.connect(self._decision_staged)
        self.history.undo_requested.connect(self._undo_commit)
        self.setCentralWidget(central)

        last_root = self.database.get_setting("last_root")
        if last_root and Path(last_root).is_dir():
            self.folder_edit.setText(last_root)
        self._refresh_views()

    def _choose_folder(self) -> None:
        initial = self.folder_edit.text() or str(Path.home())
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose folder to scan",
            initial,
        )
        if not selected:
            return
        root = normalize_path(selected)
        self.folder_edit.setText(root)
        self.database.set_setting("last_root", root)
        self.review_service.invalidate(root)
        self._refresh_views()
        self.status.setText("Ready to scan.")

    def _start_or_pause_scan(self) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.request_pause()
            self.scan_button.setEnabled(False)
            self.status.setText("Pausing after the current file or folder...")
            return

        root = self.folder_edit.text()
        if not root or not Path(root).is_dir():
            QMessageBox.warning(
                self,
                "Choose a folder",
                "Choose an existing folder before scanning.",
            )
            return

        self.worker = ScanWorker(
            self.db_path,
            root,
            self.min_size.value() * 1024,
        )
        self.worker.group_found.connect(self._scan_group_found)
        self.worker.progress_changed.connect(self._scan_progress)
        self.worker.status_changed.connect(self.status.setText)
        self.worker.scan_complete.connect(self._scan_complete)
        self.worker.scan_failed.connect(self._scan_failed)
        self.worker.finished.connect(self._worker_finished)
        self.scan_button.setText("Pause scan")
        self.rescan_button.setEnabled(False)
        self.choose_button.setEnabled(False)
        self.min_size.setEnabled(False)
        self.cart.set_commit_enabled(False)
        self.progress.setRange(0, 0)
        self.status.setText("Discovering folders and scanning new areas...")
        self.worker.start()

    def _scan_group_found(self, _group_id: int) -> None:
        root = self.folder_edit.text()
        if root:
            self.review_service.invalidate(root)
        self.refresh_timer.start()

    def _scan_progress(
        self,
        current: int,
        total: int,
        scope_complete: bool,
        folder: str,
    ) -> None:
        if scope_complete:
            total = max(1, total)
            self.progress.setRange(0, total)
            self.progress.setValue(min(current, total))
            percent = min(100, round(current * 100 / total))
            remaining = max(0, total - current)
            if folder:
                self.status.setText(
                    f"{percent}% scanned · {remaining} folders remaining · {folder}"
                )
        else:
            self.progress.setRange(0, 0)
            self.status.setText(
                f"Scanning now · {current} folders completed · "
                f"{total} folders discovered so far"
            )

    def _scan_complete(
        self,
        paused: bool,
        folders_completed: int,
        groups_changed: int,
    ) -> None:
        root = self.folder_edit.text()
        if root:
            self.review_service.invalidate(root)
        if paused:
            self.status.setText(
                f"Scan paused after processing {folders_completed} folders."
            )
        else:
            self.progress.setValue(self.progress.maximum())
            self.status.setText(
                f"Scan complete: {folders_completed} folders processed, "
                f"{groups_changed} duplicate groups added or updated."
            )
        self._refresh_views()

    def _scan_failed(self, message: str) -> None:
        self.status.setText(f"Scan failed: {message}")
        QMessageBox.critical(self, "Scan failed", message)

    def _worker_finished(self) -> None:
        if self.worker:
            self.worker.deleteLater()
        self.worker = None
        self.scan_button.setText("Start scan")
        self.scan_button.setEnabled(True)
        self.rescan_button.setEnabled(True)
        self.choose_button.setEnabled(True)
        self.min_size.setEnabled(True)
        self.cart.set_commit_enabled(True)

    def _reset_scan(self) -> None:
        root = self.folder_edit.text()
        if not root:
            return
        answer = QMessageBox.question(
            self,
            "Rescan everything",
            "Reset folder progress so the next scan checks every folder again?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.database.reset_scan_progress(root)
        self.review_service.invalidate(root)
        self.status.setText(
            "Scan progress reset. Start a scan to check every folder."
        )

    def _decision_staged(self, _batch_id: int) -> None:
        self._refresh_views()
        self.status.setText(
            "Decision added to the cart. No files have been changed."
        )

    def _commit_actions(self) -> None:
        ready = [
            batch
            for batch in self.database.list_action_batches()
            if batch.status != "stale" and batch.actions
        ]
        if not ready:
            return
        answer = QMessageBox.warning(
            self,
            "Commit staged changes",
            f"Apply {len(ready)} ready cart batch(es)? Replaced copies will be moved "
            "to the Recycle Bin and recorded in History.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            results = ActionExecutor(self.database).commit_all()
        finally:
            QApplication.restoreOverrideCursor()

        failures = [result for result in results if not result.success]
        completed = sum(result.completed_actions for result in results)
        root = self.folder_edit.text()
        if root:
            self.review_service.invalidate(root)
        self._refresh_views()
        if failures:
            details = "\n".join(result.message for result in failures[:8])
            QMessageBox.warning(
                self,
                "Commit completed with errors",
                f"{completed} file action(s) completed; "
                f"{sum(result.failed_actions for result in failures)} failed.\n\n"
                f"{details}",
            )
            self.status.setText(
                f"Commit finished: {completed} completed with errors."
            )
        else:
            QMessageBox.information(
                self,
                "Changes committed",
                f"Completed {completed} file action(s). Replaced files are in "
                "the Recycle Bin and the commit can be undone from History.",
            )
            self.status.setText(f"Committed {completed} file action(s).")

    def _undo_commit(self, commit_batch_id: int) -> None:
        answer = QMessageBox.question(
            self,
            "Undo committed cleanup",
            "Recreate all removed duplicate paths and restore their recorded timestamps?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            result = ActionExecutor(self.database).undo_commit(commit_batch_id)
        finally:
            QApplication.restoreOverrideCursor()
        root = self.folder_edit.text()
        if root:
            self.review_service.invalidate(root)
        self._refresh_views()
        if result.success:
            QMessageBox.information(self, "Commit undone", result.message)
            self.status.setText(result.message)
        else:
            QMessageBox.warning(self, "Could not undo commit", result.message)
            self.status.setText(f"Undo failed: {result.message}")

    def _refresh_views(self) -> None:
        root = self.folder_edit.text() or None
        self.duplicates.load(root)
        self.ignored.load(root)
        self.cart.refresh()
        self.history.refresh()
        self.tabs.setTabText(0, f"Duplicates ({self.duplicates.count})")
        self.tabs.setTabText(1, f"Cart ({self.cart.count})")
        self.tabs.setTabText(2, f"Ignored ({self.ignored.count})")
        self.tabs.setTabText(3, f"History ({self.history.count})")

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.worker and self.worker.isRunning():
            self.worker.request_pause()
            if not self.worker.wait(5_000):
                QMessageBox.warning(
                    self,
                    "Scan still stopping",
                    "Wait for the current file operation to finish before closing.",
                )
                event.ignore()
                return
        event.accept()
