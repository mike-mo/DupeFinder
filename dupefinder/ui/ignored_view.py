from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from dupefinder.models import ReviewItem
from dupefinder.review import ReviewService
from dupefinder.thumbnails import ThumbnailProvider
from dupefinder.ui.review_card import ReviewCard


class IgnoredView(QWidget):
    changed = Signal()
    batch_staged = Signal(int)
    clear_requested = Signal()

    def __init__(
        self,
        service: ReviewService,
        thumbnails: ThumbnailProvider,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.thumbnails = thumbnails
        self.root_path: str | None = None
        self.items: list[ReviewItem] = []
        self.saved_count = 0
        self.expanded: dict[str, bool] = {}

        outer = QVBoxLayout(self)
        header = QHBoxLayout()
        self.summary = QLabel("No ignored duplicate groups.")
        header.addWidget(self.summary, stretch=1)
        self.clear_button = QPushButton("Clear ignored")
        self.clear_button.clicked.connect(self.clear_requested.emit)
        header.addWidget(self.clear_button)
        outer.addLayout(header)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.card_layout = QVBoxLayout(self.container)
        self.card_layout.addStretch()
        scroll.setWidget(self.container)
        outer.addWidget(scroll)

    @property
    def count(self) -> int:
        return self.saved_count

    def load(self, root_path: str | None) -> None:
        items = self.service.list_ignored(root_path) if root_path else []
        self.set_items(root_path, items)

    def set_items(
        self,
        root_path: str | None,
        items: tuple[ReviewItem, ...] | list[ReviewItem],
        saved_count: int | None = None,
    ) -> None:
        self.root_path = root_path
        self._clear()
        self.items = list(items)
        self.saved_count = len(self.items) if saved_count is None else saved_count
        if not root_path:
            self.summary.setText("Choose a folder to see ignored duplicates.")
            self.clear_button.setEnabled(False)
            return
        for item in reversed(self.items):
            card = ReviewCard(
                item,
                self.thumbnails,
                mode="ignored",
                expanded=self.expanded.get(item.key, False),
            )
            card.restore_requested.connect(self._restore)
            card.stage_requested.connect(self._stage)
            card.expansion_changed.connect(self.expanded.__setitem__)
            self.card_layout.insertWidget(0, card)
        count = self.saved_count
        if count and not self.items:
            self.summary.setText(
                f"{count} ignored decision{'s' if count != 1 else ''} preserved. "
                "Run a scan to view the matching duplicate details."
            )
        else:
            self.summary.setText(
                f"{count} ignored duplicate group{'s' if count != 1 else ''}."
            )
        self.clear_button.setEnabled(count > 0)

    def clear_items(self) -> None:
        self.set_items(self.root_path, ())

    def _restore(self, item: ReviewItem) -> None:
        self.service.restore(item)
        self.set_items(
            self.root_path,
            [candidate for candidate in self.items if candidate.key != item.key],
        )
        self.changed.emit()

    def _stage(self, item: ReviewItem, target: str) -> None:
        try:
            batch_id = self.service.stage(item, target)
        except (OSError, ValueError) as exc:
            QMessageBox.warning(
                self,
                "Rescan required",
                f"This ignored duplicate is no longer in the scan cache. "
                f"Run a scan before staging it.\n\n{exc}",
            )
            return
        self.set_items(
            self.root_path,
            [candidate for candidate in self.items if candidate.key != item.key],
        )
        self.batch_staged.emit(batch_id)
        self.changed.emit()

    def _clear(self) -> None:
        self.items = []
        while self.card_layout.count() > 1:
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
