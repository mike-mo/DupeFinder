from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtTest import QTest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton
from PIL import Image

from dupefinder.db import Database
from dupefinder.models import ReviewSnapshot
from dupefinder.review import ReviewService
from dupefinder.scanner import ScannerEngine
from dupefinder.thumbnails import ThumbnailProvider
from dupefinder.ui.duplicates_view import DuplicatesView, ROLE_KEY
from dupefinder.ui.main_window import MainWindow
from dupefinder.ui.review_card import PathActionRow, ReviewCard


class UiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def wait_until(self, predicate, timeout_ms: int = 5_000) -> None:
        elapsed = 0
        while not predicate() and elapsed < timeout_ms:
            QApplication.processEvents()
            QTest.qWait(20)
            elapsed += 20
        self.assertTrue(predicate(), "Timed out waiting for asynchronous UI state")

    def test_main_window_constructs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                window.show()
                self.assertEqual(window.windowTitle(), "DupeFinder")
                self.assertEqual(window.min_size.value(), 100)
                self.assertEqual(
                    window.clear_results_button.text(),
                    "Clear scan results",
                )
                self.assertEqual(window.ignored.clear_button.text(), "Clear ignored")
                window.close()

    def test_review_snapshot_work_does_not_block_gui_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            root.mkdir()
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                window.folder_edit.setText(str(root))
                gui_thread = threading.get_ident()
                worker_threads: list[int] = []
                timer_fired: list[bool] = []

                def slow_list_all(_service, _root, *, reconcile=True):
                    worker_threads.append(threading.get_ident())
                    time.sleep(0.15)
                    return []

                with patch(
                    "dupefinder.review.ReviewService.list_all",
                    new=slow_list_all,
                ):
                    QTimer.singleShot(20, lambda: timer_fired.append(True))
                    window._refresh_views()
                    self.wait_until(
                        lambda: window.snapshot_worker is None and bool(timer_fired),
                        timeout_ms=3_000,
                    )

                self.assertTrue(timer_fired)
                self.assertTrue(worker_threads)
                self.assertTrue(all(thread_id != gui_thread for thread_id in worker_threads))
                window.close()

    def test_hierarchy_build_does_not_block_gui_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            root.mkdir()
            content = b"same" * 40_000
            (root / "first.bin").write_bytes(content)
            (root / "second.bin").write_bytes(content)
            database = Database(Path(temporary) / "state.db")
            ScannerEngine(database, root, 100 * 1024).run()
            service = ReviewService(database)
            items = service.list_inbox(root)
            view = DuplicatesView(
                service,
                ThumbnailProvider(Path(temporary) / "thumbs"),
            )
            gui_thread = threading.get_ident()
            worker_threads: list[int] = []
            timer_fired: list[bool] = []
            from dupefinder.result_hierarchy import build_result_hierarchy as real_build

            def slow_build(current_items, current_root):
                worker_threads.append(threading.get_ident())
                time.sleep(0.15)
                return real_build(current_items, current_root)

            with patch(
                "dupefinder.ui.duplicates_view.build_result_hierarchy",
                new=slow_build,
            ):
                QTimer.singleShot(20, lambda: timer_fired.append(True))
                view.set_items(str(root), items)
                self.wait_until(
                    lambda: view.hierarchy_worker is None and bool(timer_fired),
                    timeout_ms=3_000,
                )

            self.assertTrue(worker_threads)
            self.assertTrue(all(thread_id != gui_thread for thread_id in worker_threads))
            view.deleteLater()

    def test_window_events_continue_during_active_scan(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            content = b"duplicate" * 20_000
            for index in range(6):
                folder = root / f"folder-{index:02d}"
                folder.mkdir(parents=True)
                (folder / f"copy-{index:02d}.bin").write_bytes(content)
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                window.show()
                window.folder_edit.setText(str(root))
                original_scan_folder = ScannerEngine._scan_folder

                def slow_scan_folder(engine, folder):
                    time.sleep(0.03)
                    return original_scan_folder(engine, folder)

                timer_states: list[bool] = []
                try:
                    with patch.object(
                        ScannerEngine,
                        "_scan_folder",
                        new=slow_scan_folder,
                    ):
                        QTimer.singleShot(
                            50,
                            lambda: timer_states.append(
                                window.worker is not None
                                and window.worker.isRunning()
                            ),
                        )
                        window._start_or_pause_scan()
                        self.wait_until(
                            lambda: bool(timer_states),
                            timeout_ms=3_000,
                        )
                        self.assertTrue(timer_states[0])
                        window.resize(window.width(), 700)
                        QApplication.processEvents()
                        self.assertEqual(window.size().height(), 700)
                        self.wait_until(
                            lambda: window.worker is None,
                            timeout_ms=20_000,
                        )
                    self.wait_until(
                        lambda: window.snapshot_worker is None,
                        timeout_ms=10_000,
                    )
                finally:
                    if window.worker and window.worker.isRunning():
                        window.worker.request_pause()
                        window.worker.wait(5_000)
                    if window.snapshot_worker and window.snapshot_worker.isRunning():
                        window.snapshot_worker.wait(5_000)
                    window.close()

    def test_image_thumbnail_is_generated(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image_path = root / "image.png"
            Image.new("RGB", (320, 200), "navy").save(image_path)
            provider = ThumbnailProvider(root / "cache", size=96)

            pixmap = provider.pixmap(image_path)

            self.assertFalse(pixmap.isNull())
            self.assertLessEqual(pixmap.width(), 96)
            self.assertLessEqual(pixmap.height(), 96)
            self.assertEqual(len(list((root / "cache").glob("*.png"))), 1)

            with patch(
                "dupefinder.thumbnails.ensure_thumbnail",
                side_effect=Image.DecompressionBombError("simulated oversized image"),
            ):
                fallback = provider.pixmap(image_path)
            self.assertFalse(fallback.isNull())

    def test_compact_card_and_inbox_ignored_cart_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                window.show()
                root = Path(temporary) / "scan"
                (root / "A").mkdir(parents=True)
                (root / "B").mkdir()
                content = b"same" * 40_000
                first = root / "A" / "first.bin"
                second = root / "B" / "second.bin"
                first.write_bytes(content)
                second.write_bytes(content)
                now = time.time()
                os.utime(first, (now - 10, now - 10))
                os.utime(second, (now, now))
                ScannerEngine(window.database, root, 100 * 1024).run()
                window.folder_edit.setText(str(root))
                window.review_service.invalidate(root)
                window._refresh_views()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.duplicates.count == 1
                    and len(window.duplicates.result_rows) == 1
                )

                row = next(iter(window.duplicates.result_rows.values()))
                tree_item = window.duplicates.result_items[row.item.key]
                self.assertFalse(tree_item.isExpanded())
                self.assertNotIn(
                    "Open",
                    [button.text() for button in row.findChildren(QPushButton)],
                )
                previews = [
                    label
                    for label in row.findChildren(QLabel)
                    if label.width() == 34 and label.height() == 34
                ]
                self.assertEqual(len(previews), 1)

                QTest.mouseClick(
                    row,
                    Qt.MouseButton.LeftButton,
                    pos=row.rect().center(),
                )
                QApplication.processEvents()
                rows = window.duplicates.findChildren(PathActionRow)
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(button.isHidden() for button in rows[0].action_buttons))
                rows[0].set_actions_visible(True)
                self.assertTrue(all(not button.isHidden() for button in rows[0].action_buttons))

                ignore = next(
                    button
                    for button in row.findChildren(QPushButton)
                    if button.text() == "Ignore"
                )
                ignore.click()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.duplicates.count == 0
                    and window.ignored.count == 1
                )
                self.assertEqual(window.duplicates.count, 0)
                self.assertEqual(window.ignored.count, 1)

                ignored_card = window.ignored.findChildren(ReviewCard)[0]
                stage = next(
                    button
                    for button in ignored_card.findChildren(QPushButton)
                    if button.text() == "Stage recommended"
                )
                stage.click()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.ignored.count == 0
                    and window.cart.count == 1
                )
                self.assertEqual(window.ignored.count, 0)
                self.assertEqual(window.cart.count, 1)
                window.close()

    def test_bulk_stage_applies_only_to_visible_filtered_results(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                root = Path(temporary) / "scan"
                root.mkdir()
                pairs = (
                    ("alpha-one.bin", "alpha-two.bin", b"alpha" * 30_000),
                    ("beta-one.bin", "beta-two.bin", b"beta!" * 35_000),
                )
                for first_name, second_name, content in pairs:
                    (root / first_name).write_bytes(content)
                    (root / second_name).write_bytes(content)
                ScannerEngine(window.database, root, 100 * 1024).run()
                window.folder_edit.setText(str(root))
                window.review_service.invalidate(root)
                window._refresh_views()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.duplicates.hierarchy_worker is None
                    and window.duplicates.count == 2
                )
                self.assertEqual(window.duplicates.count, 2)

                window.duplicates.search.setText("alpha")
                self.assertFalse(window.duplicates.tree.isEnabled())
                self.wait_until(
                    lambda: window.duplicates.hierarchy_worker is None
                    and len(window.duplicates.visible_items) == 1
                )
                self.assertTrue(window.duplicates.tree.isEnabled())
                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ):
                    window.duplicates._stage_all_visible()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.cart.count == 1
                )

                self.assertEqual(window.cart.count, 1)
                self.assertEqual(window.duplicates.count, 1)
                window.close()

    def test_nested_folder_rows_start_collapsed_and_expand_from_row_click(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                window.show()
                root = Path(temporary) / "Ableton"
                pairs = (
                    (
                        r"Lessons\LivePackBanners\banner-one.bin",
                        r"Lessons\LivePackBanners\Archive\banner-one-copy.bin",
                        b"banner-one" * 20_000,
                    ),
                    (
                        r"Lessons\LivePackBanners\banner-two.bin",
                        r"Lessons\LivePackBanners\Archive\banner-two-copy.bin",
                        b"banner-two" * 20_000,
                    ),
                    (
                        r"Lessons\Samples\sample-one.aif",
                        r"Lessons\Samples\Backup\sample-one-copy.aif",
                        b"sample-one" * 20_000,
                    ),
                    (
                        r"Lessons\Samples\sample-two.aif",
                        r"Lessons\Samples\Backup\sample-two-copy.aif",
                        b"sample-two" * 20_000,
                    ),
                )
                for first_relative, second_relative, content in pairs:
                    first = root / first_relative
                    second = root / second_relative
                    first.parent.mkdir(parents=True, exist_ok=True)
                    second.parent.mkdir(parents=True, exist_ok=True)
                    first.write_bytes(content)
                    second.write_bytes(content)
                ScannerEngine(window.database, root, 100 * 1024).run()
                window.folder_edit.setText(str(root))
                window.review_service.invalidate(root)
                window._refresh_views()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.duplicates.tree.topLevelItemCount() == 1
                )

                lessons_path = next(
                    path
                    for path, node in window.duplicates.folder_nodes.items()
                    if node.name == "Lessons"
                )
                lessons = window.duplicates.folder_items[lessons_path]
                lessons_row = window.duplicates.folder_rows[lessons_path]
                self.assertEqual(lessons.text(0), "")
                self.assertGreaterEqual(
                    lessons.sizeHint(0).height(),
                    lessons_row.minimumHeight(),
                )
                self.assertFalse(lessons.isExpanded())
                rectangle = window.duplicates.tree.visualItemRect(lessons)
                QTest.mouseClick(
                    window.duplicates.tree.viewport(),
                    Qt.MouseButton.LeftButton,
                    pos=rectangle.center(),
                )
                QApplication.processEvents()

                self.assertTrue(lessons.isExpanded())
                self.assertEqual(
                    [
                        window.duplicates.folder_nodes[
                            lessons.child(index).data(0, ROLE_KEY)
                        ].name
                        for index in range(2)
                    ],
                    ["LivePackBanners", "Samples"],
                )
                self.assertTrue(
                    all(not lessons.child(index).isExpanded() for index in range(2))
                )
                samples_tree_item = next(
                    lessons.child(index)
                    for index in range(2)
                    if window.duplicates.folder_nodes[
                        lessons.child(index).data(0, ROLE_KEY)
                    ].name
                    == "Samples"
                )
                samples_tree_item.setExpanded(True)
                lessons.setExpanded(False)
                QApplication.processEvents()
                self.assertFalse(window.duplicates.has_expanded_nodes())
                lessons.setExpanded(True)
                QApplication.processEvents()
                self.assertTrue(window.duplicates.has_expanded_nodes())

                samples_path = next(
                    path
                    for path, node in window.duplicates.folder_nodes.items()
                    if node.name == "Samples"
                )
                samples_row = window.duplicates.folder_rows[samples_path]
                stage_all = next(
                    button
                    for button in samples_row.findChildren(QPushButton)
                    if button.text() == "Stage all recommended"
                )
                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ):
                    stage_all.click()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.duplicates.hierarchy_worker is None
                    and window.cart.count == 2
                    and window.duplicates.count == 2
                )

                remaining_path = next(iter(window.duplicates.folder_rows))
                remaining_row = window.duplicates.folder_rows[remaining_path]
                ignore_all = next(
                    button
                    for button in remaining_row.findChildren(QPushButton)
                    if button.text() == "Ignore all"
                )
                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ):
                    ignore_all.click()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.duplicates.count == 0
                    and window.ignored.count == 2
                )
                window.close()

    def test_clear_scan_results_immediately_clears_duplicates_and_cart(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                root = Path(temporary) / "scan"
                root.mkdir()
                for prefix, content in (
                    ("one", b"one" * 50_000),
                    ("two", b"two!" * 45_000),
                    ("three", b"three" * 40_000),
                ):
                    (root / f"{prefix}-a.bin").write_bytes(content)
                    (root / f"{prefix}-b.bin").write_bytes(content)
                ScannerEngine(window.database, root, 100 * 1024).run()
                window.folder_edit.setText(str(root))
                window._refresh_views()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and len(window.latest_all_items) == 3
                )
                items = list(window.latest_all_items)
                window.review_service.ignore(items[0])
                window.review_service.stage(items[1])
                window._review_state_changed()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.duplicates.count == 1
                    and window.cart.count == 1
                    and window.ignored.count == 1
                )

                with patch.object(
                    QMessageBox,
                    "warning",
                    return_value=QMessageBox.StandardButton.Yes,
                ):
                    window._clear_scan_results()

                self.assertEqual(window.duplicates.count, 0)
                self.assertEqual(window.cart.count, 0)
                self.assertEqual(window.ignored.count, 1)
                self.wait_until(
                    lambda: window.clear_worker is None,
                    timeout_ms=5_000,
                )
                self.assertEqual(window.database.list_duplicate_groups(root), [])
                self.assertEqual(window.database.list_files(root), [])
                self.assertEqual(len(window.database.ignored_review_keys(root)), 1)
                window._request_review_refresh(reconcile=True, immediate=True)
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.ignored.count == 1
                )
                self.assertTrue(window.ignored.clear_button.isEnabled())
                window.close()

    def test_cart_keeps_destination_controls_for_other_scan_roots(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                first_root = Path(temporary) / "first-root"
                second_root = Path(temporary) / "second-root"
                for root, marker in (
                    (first_root, b"first" * 30_000),
                    (second_root, b"second" * 30_000),
                ):
                    root.mkdir()
                    (root / "a.bin").write_bytes(marker)
                    (root / "b.bin").write_bytes(marker)
                    ScannerEngine(window.database, root, 100 * 1024).run()
                second_item = ReviewService(window.database).list_inbox(second_root)[0]
                ReviewService(window.database).stage(second_item)
                window.folder_edit.setText(str(first_root))
                window._refresh_views()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.cart.count == 1
                )

                cart_item = window.cart.tree.topLevelItem(0)
                self.assertIsNotNone(window.cart.tree.itemWidget(cart_item, 1))
                with patch.object(
                    QMessageBox,
                    "warning",
                    return_value=QMessageBox.StandardButton.Yes,
                ):
                    window._clear_scan_results()
                self.wait_until(
                    lambda: window.clear_worker is None,
                    timeout_ms=5_000,
                )
                self.assertEqual(window.cart.count, 1)
                cart_item = window.cart.tree.topLevelItem(0)
                self.assertIsNotNone(window.cart.tree.itemWidget(cart_item, 1))
                window.close()

    def test_switching_roots_clears_old_rows_before_async_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                roots = [
                    Path(temporary) / "first-root",
                    Path(temporary) / "second-root",
                ]
                for index, root in enumerate(roots):
                    root.mkdir()
                    content = f"root-{index}".encode("ascii") * 30_000
                    (root / "a.bin").write_bytes(content)
                    (root / "b.bin").write_bytes(content)
                    ScannerEngine(window.database, root, 100 * 1024).run()
                window.folder_edit.setText(str(roots[0]))
                window._refresh_views()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.duplicates.count == 1
                )

                with patch.object(
                    window,
                    "_request_review_refresh",
                    wraps=window._request_review_refresh,
                ):
                    with patch(
                        "dupefinder.ui.main_window.QFileDialog.getExistingDirectory",
                        return_value=str(roots[1]),
                    ):
                        window._choose_folder()
                    self.assertEqual(window.duplicates.count, 0)
                    self.assertEqual(window.ignored.count, 0)

                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.duplicates.count == 1
                )
                window.close()

    def test_commit_remains_disabled_until_reconciled_snapshot_arrives(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                root = Path(temporary) / "scan"
                root.mkdir()
                content = b"same" * 40_000
                (root / "a.bin").write_bytes(content)
                (root / "b.bin").write_bytes(content)
                ScannerEngine(window.database, root, 100 * 1024).run()
                item = ReviewService(window.database).list_inbox(root)[0]
                ReviewService(window.database).stage(item)
                window.folder_edit.setText(str(root))
                window.commit_waiting_for_reconcile = True
                window.cart.set_commit_enabled(False)
                self.assertFalse(window.cart.commit_button.isEnabled())

                window._request_review_refresh(reconcile=True, immediate=True)
                self.assertFalse(window.cart.commit_button.isEnabled())
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and not window.commit_waiting_for_reconcile
                )

                self.assertTrue(window.cart.commit_button.isEnabled())
                window.close()

    def test_cross_root_cart_batch_is_reconciled_before_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                current_root = Path(temporary) / "current"
                cart_root = Path(temporary) / "cart-root"
                for root, content in (
                    (current_root, b"current" * 30_000),
                    (cart_root, b"cart" * 40_000),
                ):
                    root.mkdir()
                    (root / "a.bin").write_bytes(content)
                    (root / "b.bin").write_bytes(content)
                    ScannerEngine(window.database, root, 100 * 1024).run()
                cart_item = ReviewService(window.database).list_inbox(cart_root)[0]
                batch_id = ReviewService(window.database).stage(cart_item)
                (cart_root / "c.bin").write_bytes(b"cart" * 40_000)
                ScannerEngine(window.database, cart_root, 100 * 1024).run()

                window.folder_edit.setText(str(current_root))
                window.commit_waiting_for_reconcile = True
                window.cart.set_commit_enabled(False)
                window._request_review_refresh(reconcile=True, immediate=True)
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and not window.commit_waiting_for_reconcile
                )

                batch = window.database.get_action_batch(batch_id)
                self.assertIsNotNone(batch)
                assert batch is not None
                self.assertEqual(batch.status, "stale")
                self.assertFalse(window.cart.commit_button.isEnabled())
                window.close()

    def test_clear_scan_results_does_not_block_gui_events(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                root = Path(temporary) / "scan"
                root.mkdir()
                window.folder_edit.setText(str(root))
                original_clear = Database.clear_scan_results
                timer_states: list[bool] = []

                def slow_clear(database, root_path):
                    time.sleep(0.2)
                    return original_clear(database, root_path)

                with (
                    patch.object(
                        Database,
                        "clear_scan_results",
                        new=slow_clear,
                    ),
                    patch.object(
                        QMessageBox,
                        "warning",
                        return_value=QMessageBox.StandardButton.Yes,
                    ),
                ):
                    QTimer.singleShot(
                        20,
                        lambda: timer_states.append(
                            window.clear_worker is not None
                            and window.clear_worker.isRunning()
                        ),
                    )
                    window._clear_scan_results()
                    self.assertFalse(window.ignored.isEnabled())
                    self.wait_until(
                        lambda: bool(timer_states),
                        timeout_ms=2_000,
                    )
                    self.assertTrue(timer_states[0])
                    self.wait_until(
                        lambda: window.clear_worker is None,
                        timeout_ms=3_000,
                    )
                    self.assertTrue(window.ignored.isEnabled())
                window.close()

    def test_reconciliation_refreshes_ignored_count_after_membership_change(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                root = Path(temporary) / "scan"
                root.mkdir()
                content = b"same" * 40_000
                (root / "a.bin").write_bytes(content)
                (root / "b.bin").write_bytes(content)
                ScannerEngine(window.database, root, 100 * 1024).run()
                item = ReviewService(window.database).list_inbox(root)[0]
                ReviewService(window.database).ignore(item)
                (root / "c.bin").write_bytes(content)
                ScannerEngine(window.database, root, 100 * 1024).run()
                window.folder_edit.setText(str(root))
                window._request_review_refresh(reconcile=True, immediate=True)
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and window.ignored.count == 0
                    and window.duplicates.count == 1
                )

                self.assertFalse(window.ignored.clear_button.isEnabled())
                window.close()

    def test_close_does_not_restart_scan_or_hierarchy_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                root = Path(temporary) / "scan"
                content = b"same" * 40_000
                for index in range(4):
                    folder = root / f"folder-{index}"
                    folder.mkdir(parents=True)
                    (folder / f"copy-{index}.bin").write_bytes(content)
                window.folder_edit.setText(str(root))
                original_scan_folder = ScannerEngine._scan_folder

                def slow_scan_folder(engine, folder):
                    time.sleep(0.05)
                    return original_scan_folder(engine, folder)

                with patch.object(
                    ScannerEngine,
                    "_scan_folder",
                    new=slow_scan_folder,
                ):
                    window._start_or_pause_scan()
                    self.wait_until(
                        lambda: window.worker is not None
                        and window.worker.isRunning(),
                        timeout_ms=2_000,
                    )
                    window.close()
                QApplication.processEvents()
                QTest.qWait(100)
                QApplication.processEvents()

                self.assertTrue(window.closing)
                self.assertTrue(
                    window.snapshot_worker is None
                    or not window.snapshot_worker.isRunning()
                )
                self.assertTrue(
                    window.duplicates.hierarchy_worker is None
                    or not window.duplicates.hierarchy_worker.isRunning()
                )
                self.assertFalse(window.duplicates.hierarchy_dirty)

    def test_live_scan_snapshot_waits_until_browsing_node_is_collapsed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                root = Path(temporary) / "scan"
                root.mkdir()
                content = b"same" * 40_000
                (root / "a.bin").write_bytes(content)
                (root / "b.bin").write_bytes(content)
                ScannerEngine(window.database, root, 100 * 1024).run()
                window.folder_edit.setText(str(root))
                window._refresh_views()
                self.wait_until(
                    lambda: window.snapshot_worker is None
                    and len(window.duplicates.result_rows) == 1
                )
                key = next(iter(window.duplicates.result_rows))
                tree_item = window.duplicates.result_items[key]
                tree_item.setExpanded(True)
                QApplication.processEvents()
                self.assertTrue(window.duplicates.has_expanded_nodes())
                window.duplicates.set_items(
                    str(root),
                    tuple(window.latest_all_items),
                )
                self.assertTrue(window.duplicates.has_expanded_nodes())
                self.wait_until(
                    lambda: window.duplicates.hierarchy_worker is None
                    and key in window.duplicates.result_items
                    and window.duplicates.result_items[key].isExpanded()
                )
                tree_item = window.duplicates.result_items[key]

                pending = ReviewSnapshot(
                    request_id=window.review_request_id,
                    root_path=str(root),
                    current_items=(),
                    all_items=(),
                    inbox_items=(),
                    ignored_items=(),
                    ignored_saved_count=0,
                    thumbnail_paths=(),
                    reconciled=False,
                    reconciled_roots=(),
                )
                window.scan_active = False
                window._snapshot_ready(pending)

                self.assertIs(window.pending_browse_snapshot, pending)
                self.assertEqual(window.duplicates.count, 1)
                self.assertTrue(tree_item.isExpanded())

                tree_item.setExpanded(False)
                QApplication.processEvents()
                self.wait_until(
                    lambda: window.pending_browse_snapshot is None
                    and window.duplicates.count == 0
                )
                window.close()

    def test_failed_snapshot_refresh_is_retried(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            root.mkdir()
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                window.folder_edit.setText(str(root))
                calls = 0

                def fail_once(_service, _root, *, reconcile=True):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise OSError("simulated snapshot failure")
                    return []

                with patch(
                    "dupefinder.review.ReviewService.list_all",
                    new=fail_once,
                ):
                    window._request_review_refresh(
                        reconcile=True,
                        immediate=True,
                    )
                    self.wait_until(
                        lambda: calls >= 2
                        and window.snapshot_worker is None
                        and not window.snapshot_dirty,
                        timeout_ms=5_000,
                    )

                self.assertGreaterEqual(calls, 2)
                window.close()

    def test_persistent_snapshot_failure_stops_after_retry_limit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            root.mkdir()
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                window.folder_edit.setText(str(root))
                window.snapshot_retry_limit = 1
                calls = 0

                def always_fail(_service, _root, *, reconcile=True):
                    nonlocal calls
                    calls += 1
                    raise OSError("persistent snapshot failure")

                with patch(
                    "dupefinder.review.ReviewService.list_all",
                    new=always_fail,
                ):
                    window._request_review_refresh(
                        reconcile=True,
                        immediate=True,
                    )
                    self.wait_until(
                        lambda: window.snapshot_worker is None
                        and window.snapshot_retry_blocked,
                        timeout_ms=4_000,
                    )

                self.assertEqual(calls, 2)
                self.assertTrue(window.snapshot_dirty)
                window.close()

    def test_thumbnail_decompression_limit_does_not_fail_result_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "scan"
            root.mkdir()
            image = Image.new("RGB", (10, 10), "red")
            first = root / "first.png"
            second = root / "second.png"
            image.save(first)
            image.save(second)
            padding = b"x" * (120_000 - first.stat().st_size)
            first.write_bytes(first.read_bytes() + padding)
            second.write_bytes(second.read_bytes() + padding)
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                ScannerEngine(window.database, root, 100 * 1024).run()
                window.folder_edit.setText(str(root))
                with patch(
                    "dupefinder.review_worker.ensure_thumbnail",
                    side_effect=Image.DecompressionBombError("simulated oversized image"),
                ):
                    window._request_review_refresh(
                        reconcile=True,
                        immediate=True,
                    )
                    self.wait_until(
                        lambda: window.snapshot_worker is None
                        and window.duplicates.count == 1,
                        timeout_ms=3_000,
                    )

                self.assertFalse(window.snapshot_retry_blocked)
                window.close()

    def test_scan_timing_shows_elapsed_duration_and_eta(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                window.scan_started_at = 100.0
                window.scan_scope_complete = True
                window.scan_progress_current = 5
                window.scan_progress_total = 10
                window.scan_rate_seconds_per_folder = 12.0

                with patch(
                    "dupefinder.ui.main_window.time.monotonic",
                    return_value=160.0,
                ):
                    window._update_scan_timing()

                self.assertEqual(
                    window.scan_timing.text(),
                    "Elapsed 01:00 · ETA 01:00",
                )
                window.progress.setRange(0, 0)
                window._settle_progress("Paused")
                self.assertNotEqual(window.progress.maximum(), 0)
                self.assertEqual(window.progress.format(), "Paused")
                window._reset_scan_display()
                self.assertEqual(window.scan_timing.text(), "Elapsed 00:00 · ETA --")
                self.assertEqual(window.progress.maximum(), 1)
                window.close()

    def test_terminal_scan_state_is_recorded_during_close_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                window.scan_active = True
                window.scan_started_at = time.monotonic() - 5
                window.closing = True

                window._scan_complete(
                    paused=True,
                    folders_completed=3,
                    groups_changed=1,
                )

                self.assertFalse(window.scan_active)
                self.assertIn("Paused", window.scan_timing.text())
                self.assertIn("paused", window.status.text().casefold())
                window.closing = False
                window.close()


if __name__ == "__main__":
    unittest.main()
