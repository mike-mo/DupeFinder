from __future__ import annotations

import time
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
from dupefinder.maintenance_worker import ClearScanResultsWorker
from dupefinder.models import ReviewSnapshot
from dupefinder.paths import database_path, thumbnail_cache_dir
from dupefinder.review import ReviewService
from dupefinder.review_worker import ReviewSnapshotWorker
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
        self.thumbnail_cache = thumbnail_cache_dir()
        self.database = Database(self.db_path)
        self.review_service = ReviewService(self.database)
        self.worker: ScanWorker | None = None
        self.scan_active = False
        self.commit_waiting_for_reconcile = False
        self.snapshot_worker: ReviewSnapshotWorker | None = None
        self.clear_worker: ClearScanResultsWorker | None = None
        self.snapshot_dirty = False
        self.snapshot_reconcile = False
        self.active_snapshot_reconcile = False
        self.active_snapshot_request_id = -1
        self.snapshot_retry_count = 0
        self.snapshot_retry_blocked = False
        self.snapshot_retry_limit = 3
        self.review_request_id = 0
        self.latest_all_items = ()
        self.clear_remaining_items = ()
        self.closing = False
        self.pending_browse_snapshot: ReviewSnapshot | None = None
        self.scan_started_at: float | None = None
        self.scan_rate_seconds_per_folder: float | None = None
        self.scan_rate_sample_time: float | None = None
        self.scan_rate_sample_current = 0
        self.scan_progress_current = 0
        self.scan_progress_total = 0
        self.scan_scope_complete = False

        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(500)
        self.refresh_timer.timeout.connect(self._start_snapshot_worker)
        self.scan_timing_timer = QTimer(self)
        self.scan_timing_timer.setInterval(1_000)
        self.scan_timing_timer.timeout.connect(self._update_scan_timing)

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
        self.clear_results_button = QPushButton("Clear scan results")
        self.clear_results_button.clicked.connect(self._clear_scan_results)
        button_row.addWidget(self.clear_results_button)
        button_row.addStretch()
        controls.addLayout(button_row, 2, 1, 1, 2)
        outer.addLayout(controls)

        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        progress_row = QHBoxLayout()
        progress_row.addWidget(self.progress, stretch=1)
        self.scan_timing = QLabel("Elapsed 00:00 · ETA --")
        self.scan_timing.setMinimumWidth(210)
        progress_row.addWidget(self.scan_timing)
        outer.addLayout(progress_row)
        self.status = QLabel("Choose a folder to begin.")
        self.status.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        outer.addWidget(self.status)

        thumbnails = ThumbnailProvider(self.thumbnail_cache)
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
        self.duplicates.changed.connect(self._review_state_changed)
        self.duplicates.browse_state_changed.connect(self._browse_state_changed)
        self.cart.cart_changed.connect(self._review_state_changed)
        self.cart.commit_requested.connect(self._commit_actions)
        self.ignored.changed.connect(self._review_state_changed)
        self.ignored.batch_staged.connect(self._decision_staged)
        self.ignored.clear_requested.connect(self._clear_ignored)
        self.history.undo_requested.connect(self._undo_commit)
        self.setCentralWidget(central)

        self.commit_waiting_for_reconcile = bool(
            self.database.list_action_batches()
        )
        self.cart.set_commit_enabled(
            not self.commit_waiting_for_reconcile
        )

        last_root = self.database.get_setting("last_root")
        if last_root and Path(last_root).is_dir():
            self.folder_edit.setText(last_root)
            self._request_review_refresh(reconcile=True, immediate=True)
        else:
            self._apply_empty_root()

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
        if self.snapshot_worker:
            self.snapshot_worker.requestInterruption()
        self.review_request_id += 1
        self.folder_edit.setText(root)
        self.database.set_setting("last_root", root)
        self.review_service.invalidate()
        self.pending_browse_snapshot = None
        self._reset_scan_display()
        self.duplicates.clear_results(root)
        self.ignored.set_items(root, ())
        self._update_tab_counts()
        self._request_review_refresh(reconcile=True, immediate=True)
        self.status.setText("Loading results for the selected folder...")

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

        self.scan_active = True
        self.commit_waiting_for_reconcile = True
        self.review_request_id += 1
        self.scan_started_at = time.monotonic()
        self.scan_rate_seconds_per_folder = None
        self.scan_rate_sample_time = self.scan_started_at
        self.scan_rate_sample_current = 0
        self.scan_progress_current = 0
        self.scan_progress_total = 0
        self.scan_scope_complete = False
        self.scan_timing_timer.start()
        self._update_scan_timing()
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
        self.clear_results_button.setEnabled(False)
        self.choose_button.setEnabled(False)
        self.min_size.setEnabled(False)
        self.cart.set_commit_enabled(False)
        self.progress.setRange(0, 0)
        self.progress.setFormat("")
        self.status.setText("Discovering folders and scanning new areas...")
        self.worker.start()

    def _scan_group_found(self, _group_id: int) -> None:
        self._request_review_refresh(reconcile=False, immediate=False)

    def _scan_progress(
        self,
        current: int,
        total: int,
        scope_complete: bool,
        folder: str,
    ) -> None:
        now = time.monotonic()
        if (
            current > self.scan_rate_sample_current
            and self.scan_rate_sample_time is not None
        ):
            elapsed = now - self.scan_rate_sample_time
            completed_delta = current - self.scan_rate_sample_current
            if elapsed > 0 and completed_delta > 0:
                sample_rate = elapsed / completed_delta
                if self.scan_rate_seconds_per_folder is None:
                    self.scan_rate_seconds_per_folder = sample_rate
                else:
                    self.scan_rate_seconds_per_folder = (
                        self.scan_rate_seconds_per_folder * 0.75
                        + sample_rate * 0.25
                    )
                self.scan_rate_sample_time = now
                self.scan_rate_sample_current = current
        self.scan_progress_current = current
        self.scan_progress_total = total
        self.scan_scope_complete = scope_complete
        if scope_complete:
            total = max(1, total)
            self.progress.setRange(0, total)
            self.progress.setFormat("%p%")
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
        self._update_scan_timing()

    def _scan_complete(
        self,
        paused: bool,
        folders_completed: int,
        groups_changed: int,
    ) -> None:
        self.scan_active = False
        self.scan_timing_timer.stop()
        self.review_request_id += 1
        if paused:
            self._settle_progress("Paused")
            self.status.setText(
                f"Scan paused after processing {folders_completed} folders."
            )
            self._update_scan_timing("Paused")
        else:
            self.database.mark_scan_results_valid(
                self.folder_edit.text(),
                True,
            )
            self._update_scan_timing("Complete")
            self.progress.setValue(self.progress.maximum())
            self.progress.setFormat("%p%")
            self.status.setText(
                f"Scan complete: {folders_completed} folders processed, "
                f"{groups_changed} duplicate groups added or updated."
            )
        if not self.closing:
            self._request_review_refresh(reconcile=True, immediate=True)

    def _scan_failed(self, message: str) -> None:
        self.scan_active = False
        self.scan_timing_timer.stop()
        self.commit_waiting_for_reconcile = True
        self._settle_progress("Incomplete")
        self.status.setText(f"Scan failed: {message}")
        self._update_scan_timing("Incomplete")
        if not self.closing:
            self._request_review_refresh(reconcile=True, immediate=True)
            QMessageBox.critical(self, "Scan failed", message)

    def _worker_finished(self) -> None:
        if self.worker:
            self.worker.deleteLater()
        self.worker = None
        self.scan_button.setText("Start scan")
        self.scan_button.setEnabled(True)
        self.clear_results_button.setEnabled(True)
        self.choose_button.setEnabled(True)
        self.min_size.setEnabled(True)
        self.cart.set_commit_enabled(
            not self.commit_waiting_for_reconcile
        )

    def _clear_scan_results(self) -> None:
        root = self.folder_edit.text()
        if not root:
            return
        answer = QMessageBox.warning(
            self,
            "Clear scan results",
            "Clear the current folder's Duplicates results, scan cache, folder "
            "progress, and Cart? Ignored decisions and History will be preserved.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.refresh_timer.stop()
        self.pending_browse_snapshot = None
        self._reset_scan_display()
        if self.snapshot_worker:
            self.snapshot_worker.requestInterruption()
        self.review_request_id += 1
        self.snapshot_dirty = False
        self.snapshot_reconcile = False
        self.clear_remaining_items = tuple(
            item
            for item in self.cart.review_items.values()
            if item.root_path != root
        )
        self.latest_all_items = self.clear_remaining_items
        self.duplicates.clear_results(root)
        self.cart.hide_root(root)
        self.cart.set_review_items(self.clear_remaining_items)
        self.cart.set_commit_enabled(False)
        self.ignored.setEnabled(False)
        self._update_tab_counts()
        self.scan_button.setEnabled(False)
        self.clear_results_button.setEnabled(False)
        self.choose_button.setEnabled(False)
        self.min_size.setEnabled(False)
        self.status.setText("Clearing scan results and Cart...")
        worker = ClearScanResultsWorker(self.db_path, root)
        self.clear_worker = worker
        worker.clear_complete.connect(self._clear_scan_complete)
        worker.clear_failed.connect(self._clear_scan_failed)
        worker.finished.connect(self._clear_worker_finished)
        worker.start()

    def _clear_scan_complete(self, root: str) -> None:
        self.review_service.invalidate(root)
        self.cart.unhide_root(root)
        self.cart.set_review_items(self.clear_remaining_items)
        active_batches = self.database.list_action_batches()
        self.commit_waiting_for_reconcile = bool(active_batches)
        self.cart.set_commit_enabled(
            not self.commit_waiting_for_reconcile
        )
        self._update_tab_counts()
        self.status.setText(
            "Scan results and Cart cleared. Ignored decisions and History were preserved."
        )
        if not self.closing:
            self._request_review_refresh(reconcile=True, immediate=True)

    def _clear_scan_failed(self, message: str) -> None:
        self.cart.unhide_root(self.folder_edit.text())
        self.status.setText(f"Could not clear scan results: {message}")
        if not self.closing:
            QMessageBox.critical(self, "Could not clear scan results", message)
            self._request_review_refresh(reconcile=True, immediate=True)

    def _clear_worker_finished(self) -> None:
        if self.clear_worker:
            self.clear_worker.deleteLater()
        self.clear_worker = None
        self.scan_button.setEnabled(True)
        self.clear_results_button.setEnabled(True)
        self.choose_button.setEnabled(True)
        self.min_size.setEnabled(True)
        self.ignored.setEnabled(True)

    def _clear_ignored(self) -> None:
        if self.clear_worker and self.clear_worker.isRunning():
            return
        root = self.folder_edit.text()
        if not root:
            return
        answer = QMessageBox.question(
            self,
            "Clear ignored duplicates",
            "Return all ignored duplicate groups for this folder to the Duplicates inbox?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.database.clear_ignored(root)
        self.review_request_id += 1
        self.ignored.clear_items()
        self._request_review_refresh(
            reconcile=not self.scan_active,
            immediate=True,
        )

    def _decision_staged(self, _batch_id: int) -> None:
        self.review_request_id += 1
        self.commit_waiting_for_reconcile = True
        self.cart.set_commit_enabled(False)
        self.cart.refresh()
        self._request_review_refresh(
            reconcile=not self.scan_active,
            immediate=True,
        )
        self.status.setText(
            "Decision added to the cart. No files have been changed."
        )

    def _review_state_changed(self) -> None:
        self.review_request_id += 1
        self.commit_waiting_for_reconcile = True
        self.cart.set_commit_enabled(False)
        self.cart.refresh()
        self._request_review_refresh(
            reconcile=not self.scan_active,
            immediate=True,
        )

    def _commit_actions(self) -> None:
        ready = [
            batch
            for batch in self.database.list_action_batches()
            if batch.status != "stale" and batch.actions
        ]
        if not ready:
            return
        ready_roots = {batch.root_path for batch in ready}
        if self.commit_waiting_for_reconcile or any(
            not self.database.scan_results_valid(root)
            for root in ready_roots
        ):
            self.cart.set_commit_enabled(False)
            self._request_review_refresh(reconcile=True, immediate=True)
            QMessageBox.warning(
                self,
                "Cart validation pending",
                "Commit is disabled until every Cart folder has a complete, "
                "reconciled scan result.",
            )
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
        self.review_request_id += 1
        self.history.refresh()
        self._request_review_refresh(reconcile=True, immediate=True)
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
        self.review_request_id += 1
        self.history.refresh()
        self._request_review_refresh(reconcile=True, immediate=True)
        if result.success:
            QMessageBox.information(self, "Commit undone", result.message)
            self.status.setText(result.message)
        else:
            QMessageBox.warning(self, "Could not undo commit", result.message)
            self.status.setText(f"Undo failed: {result.message}")

    def _request_review_refresh(self, *, reconcile: bool, immediate: bool) -> None:
        if self.closing:
            return
        root = self.folder_edit.text()
        if not root:
            self._apply_empty_root()
            return
        if self.snapshot_retry_blocked and not immediate:
            return
        if self.snapshot_retry_blocked:
            self.snapshot_retry_blocked = False
            self.snapshot_retry_count = 0
        self.snapshot_dirty = True
        self.snapshot_reconcile = self.snapshot_reconcile or reconcile
        if immediate:
            self.refresh_timer.stop()
            self._start_snapshot_worker()
        elif not self.refresh_timer.isActive():
            self.refresh_timer.start()

    def _start_snapshot_worker(self) -> None:
        if self.closing:
            return
        if self.snapshot_worker is not None:
            return
        root = self.folder_edit.text()
        if not root or not self.snapshot_dirty:
            return
        request_id = self.review_request_id
        reconcile = self.snapshot_reconcile
        self.active_snapshot_request_id = request_id
        self.active_snapshot_reconcile = reconcile
        self.snapshot_dirty = False
        self.snapshot_reconcile = False
        worker = ReviewSnapshotWorker(
            self.db_path,
            root,
            self.thumbnail_cache,
            reconcile=reconcile,
            request_id=request_id,
            additional_roots=tuple(
                batch.root_path
                for batch in self.database.list_action_batches()
                if batch.root_path != root
            ),
        )
        self.snapshot_worker = worker
        worker.snapshot_ready.connect(self._snapshot_ready)
        worker.snapshot_failed.connect(self._snapshot_failed)
        worker.finished.connect(self._snapshot_finished)
        worker.start()

    def _snapshot_ready(self, snapshot: ReviewSnapshot) -> None:
        if self.closing:
            return
        if (
            snapshot.request_id != self.review_request_id
            or snapshot.root_path != self.folder_edit.text()
        ):
            return
        self.refresh_timer.setInterval(500)
        self.snapshot_retry_count = 0
        self.snapshot_retry_blocked = False
        if self.duplicates.has_expanded_nodes():
            self.pending_browse_snapshot = snapshot
            return
        self._apply_snapshot(snapshot)

    def _apply_snapshot(self, snapshot: ReviewSnapshot) -> None:
        self.pending_browse_snapshot = None
        self.latest_all_items = snapshot.all_items
        current_items = list(snapshot.current_items)
        items_by_root: dict[str, list] = {}
        for item in snapshot.all_items:
            items_by_root.setdefault(item.root_path, []).append(item)
        for reconciled_root in snapshot.reconciled_roots:
            root_items = items_by_root.get(reconciled_root, [])
            self.database.reconcile_review_items(
                reconciled_root,
                {item.key: item.fingerprint for item in root_items},
            )
        if snapshot.reconciled:
            inbox_items, ignored_items = self.review_service.partition_items(
                current_items,
                snapshot.root_path,
            )
        else:
            inbox_items = list(snapshot.inbox_items)
            ignored_items = list(snapshot.ignored_items)
        ignored_saved_count = (
            self.database.ignored_review_count(snapshot.root_path)
            if snapshot.reconciled
            else snapshot.ignored_saved_count
        )
        reconciled_roots = set(snapshot.reconciled_roots)
        self.commit_waiting_for_reconcile = any(
            batch.root_path not in reconciled_roots
            for batch in self.database.list_action_batches()
        )
        thumbnails = dict(snapshot.thumbnail_paths)
        self.duplicates.set_items(
            snapshot.root_path,
            inbox_items,
            thumbnails,
        )
        self.ignored.set_items(
            snapshot.root_path,
            ignored_items,
            ignored_saved_count,
        )
        self.cart.set_review_items(snapshot.all_items)
        self.cart.set_commit_enabled(
            not self.scan_active
            and not self.commit_waiting_for_reconcile
        )
        self._update_tab_counts()

    def _browse_state_changed(self, browsing: bool) -> None:
        if browsing or not self.pending_browse_snapshot or self.closing:
            return
        snapshot = self.pending_browse_snapshot
        if (
            snapshot.request_id == self.review_request_id
            and snapshot.root_path == self.folder_edit.text()
        ):
            self._apply_snapshot(snapshot)
        else:
            self.pending_browse_snapshot = None

    def _snapshot_failed(self, message: str) -> None:
        if self.closing:
            return
        if (
            self.snapshot_worker
            and self.snapshot_worker.request_id == self.review_request_id
            and self.snapshot_worker.root_path == self.folder_edit.text()
        ):
            if self.snapshot_retry_count < self.snapshot_retry_limit:
                self.snapshot_retry_count += 1
                self.snapshot_dirty = True
                self.snapshot_reconcile = (
                    self.snapshot_reconcile or self.active_snapshot_reconcile
                )
                delay = 1_000 * (2 ** (self.snapshot_retry_count - 1))
                self.refresh_timer.start(delay)
            else:
                self.snapshot_dirty = True
                self.snapshot_retry_blocked = True
        if not self.scan_active:
            self.status.setText(f"Could not refresh results: {message}")

    def _snapshot_finished(self) -> None:
        if self.snapshot_worker:
            self.snapshot_worker.deleteLater()
        self.snapshot_worker = None
        if (
            self.snapshot_dirty
            and not self.closing
            and not self.snapshot_retry_blocked
            and not self.refresh_timer.isActive()
        ):
            QTimer.singleShot(0, self._start_snapshot_worker)

    def _resume_after_canceled_close(self, *, hierarchy_shutdown: bool = False) -> None:
        self.closing = False
        self.snapshot_retry_blocked = False
        self.snapshot_retry_count = 0
        if hierarchy_shutdown:
            self.duplicates.cancel_shutdown()
        if self.folder_edit.text():
            self.snapshot_dirty = True
            self.snapshot_reconcile = (
                self.snapshot_reconcile or self.active_snapshot_reconcile
            )
            if not self.refresh_timer.isActive():
                self.refresh_timer.start()
        if self.scan_active:
            self.scan_timing_timer.start()

    def _refresh_views(self) -> None:
        self._request_review_refresh(
            reconcile=not self.scan_active,
            immediate=True,
        )

    def _apply_empty_root(self) -> None:
        self.pending_browse_snapshot = None
        self._reset_scan_display()
        self.latest_all_items = ()
        self.duplicates.clear_results(None)
        self.ignored.set_items(None, ())
        self.cart.set_review_items(())
        self._update_tab_counts()

    def _update_tab_counts(self) -> None:
        self.tabs.setTabText(0, f"Duplicates ({self.duplicates.count})")
        self.tabs.setTabText(1, f"Cart ({self.cart.count})")
        self.tabs.setTabText(2, f"Ignored ({self.ignored.count})")
        self.tabs.setTabText(3, f"History ({self.history.count})")

    def _update_scan_timing(self, final_state: str | None = None) -> None:
        if self.scan_started_at is None:
            self.scan_timing.setText("Elapsed 00:00 · ETA --")
            return
        elapsed = max(0.0, time.monotonic() - self.scan_started_at)
        elapsed_text = self._format_duration(elapsed)
        if final_state:
            self.scan_timing.setText(f"Elapsed {elapsed_text} · {final_state}")
            return
        if (
            self.scan_scope_complete
            and self.scan_rate_seconds_per_folder is not None
            and self.scan_progress_total > self.scan_progress_current
        ):
            remaining = self.scan_progress_total - self.scan_progress_current
            eta = remaining * self.scan_rate_seconds_per_folder
            eta_text = self._format_duration(eta)
        elif self.scan_scope_complete and self.scan_progress_total <= self.scan_progress_current:
            eta_text = "00:00"
        else:
            eta_text = "estimating..."
        self.scan_timing.setText(f"Elapsed {elapsed_text} · ETA {eta_text}")

    def _reset_scan_display(self) -> None:
        self.scan_timing_timer.stop()
        self.scan_started_at = None
        self.scan_rate_seconds_per_folder = None
        self.scan_rate_sample_time = None
        self.scan_rate_sample_current = 0
        self.scan_progress_current = 0
        self.scan_progress_total = 0
        self.scan_scope_complete = False
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.scan_timing.setText("Elapsed 00:00 · ETA --")

    def _settle_progress(self, label: str) -> None:
        if self.progress.minimum() == 0 and self.progress.maximum() == 0:
            total = max(
                1,
                self.scan_progress_total,
                self.scan_progress_current,
            )
            self.progress.setRange(0, total)
            self.progress.setValue(min(self.scan_progress_current, total))
        self.progress.setFormat(label)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total = max(0, int(round(seconds)))
        days, remainder = divmod(total, 86_400)
        hours, remainder = divmod(remainder, 3_600)
        minutes, secs = divmod(remainder, 60)
        if days:
            return f"{days}d {hours:02d}:{minutes:02d}:{secs:02d}"
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    def closeEvent(self, event: QCloseEvent) -> None:
        self.closing = True
        self.refresh_timer.stop()
        self.scan_timing_timer.stop()
        self.snapshot_dirty = False
        self.snapshot_reconcile = False
        if self.snapshot_worker and self.snapshot_worker.isRunning():
            self.snapshot_worker.requestInterruption()
        if self.worker and self.worker.isRunning():
            self.worker.request_pause()
            if not self.worker.wait(5_000):
                QMessageBox.warning(
                    self,
                    "Scan still stopping",
                    "Wait for the current file operation to finish before closing.",
                )
                self._resume_after_canceled_close()
                event.ignore()
                return
        if self.snapshot_worker and self.snapshot_worker.isRunning():
            if not self.snapshot_worker.wait(5_000):
                QMessageBox.warning(
                    self,
                    "Results still refreshing",
                    "Wait for the current results refresh to finish before closing.",
                )
                self._resume_after_canceled_close()
                event.ignore()
                return
        if self.clear_worker and self.clear_worker.isRunning():
            if not self.clear_worker.wait(5_000):
                QMessageBox.warning(
                    self,
                    "Scan results still clearing",
                    "Wait for the current clear operation to finish before closing.",
                )
                self._resume_after_canceled_close()
                event.ignore()
                return
        if not self.duplicates.begin_shutdown(5_000):
            QMessageBox.warning(
                self,
                "Results still grouping",
                "Wait for the current result grouping operation to finish before closing.",
            )
            self._resume_after_canceled_close(hierarchy_shutdown=True)
            event.ignore()
            return
        event.accept()
