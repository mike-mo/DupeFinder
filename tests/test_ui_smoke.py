from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QLabel, QMessageBox, QPushButton
from PIL import Image

from dupefinder.scanner import ScannerEngine
from dupefinder.thumbnails import ThumbnailProvider
from dupefinder.ui.main_window import MainWindow
from dupefinder.ui.review_card import PathActionRow, ReviewCard


class UiSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_main_window_constructs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
                self.assertEqual(window.windowTitle(), "DupeFinder")
                self.assertEqual(window.min_size.value(), 100)
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

    def test_compact_card_and_inbox_ignored_cart_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with patch.dict(os.environ, {"DUPEFINDER_DATA_DIR": temporary}):
                window = MainWindow()
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

                cards = window.duplicates.findChildren(ReviewCard)
                self.assertEqual(len(cards), 1)
                card = cards[0]
                self.assertFalse(card.details.isVisible())
                self.assertNotIn(
                    "Open",
                    [button.text() for button in card.findChildren(QPushButton)],
                )
                previews = [
                    label
                    for label in card.findChildren(QLabel)
                    if label.width() == 44 and label.height() == 44
                ]
                self.assertEqual(len(previews), 1)

                card.toggle.click()
                rows = card.findChildren(PathActionRow)
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(button.isHidden() for button in rows[0].action_buttons))
                rows[0].set_actions_visible(True)
                self.assertTrue(all(not button.isHidden() for button in rows[0].action_buttons))

                ignore = next(
                    button
                    for button in card.findChildren(QPushButton)
                    if button.text() == "Ignore"
                )
                ignore.click()
                QApplication.processEvents()
                self.assertEqual(window.duplicates.count, 0)
                self.assertEqual(window.ignored.count, 1)

                ignored_card = window.ignored.findChildren(ReviewCard)[0]
                stage = next(
                    button
                    for button in ignored_card.findChildren(QPushButton)
                    if button.text() == "Stage recommended"
                )
                stage.click()
                QApplication.processEvents()
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
                self.assertEqual(window.duplicates.count, 2)

                window.duplicates.search.setText("alpha")
                self.assertEqual(len(window.duplicates.visible_items), 1)
                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ):
                    window.duplicates._stage_all_visible()
                QApplication.processEvents()

                self.assertEqual(window.cart.count, 1)
                self.assertEqual(window.duplicates.count, 1)
                window.close()


if __name__ == "__main__":
    unittest.main()
