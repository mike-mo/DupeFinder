from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QLabel, QScrollArea, QVBoxLayout, QWidget

from dupefinder.models import ReviewItem
from dupefinder.review import ReviewService
from dupefinder.thumbnails import ThumbnailProvider
from dupefinder.ui.review_card import ReviewCard


class IgnoredView(QWidget):
    changed = Signal()
    batch_staged = Signal(int)

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
        self.expanded: dict[str, bool] = {}

        outer = QVBoxLayout(self)
        self.summary = QLabel("No ignored duplicate groups.")
        outer.addWidget(self.summary)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.card_layout = QVBoxLayout(self.container)
        self.card_layout.addStretch()
        scroll.setWidget(self.container)
        outer.addWidget(scroll)

    @property
    def count(self) -> int:
        return len(self.items)

    def load(self, root_path: str | None) -> None:
        self.root_path = root_path
        self._clear()
        if not root_path:
            self.summary.setText("Choose a folder to see ignored duplicates.")
            return
        self.items = self.service.list_ignored(root_path)
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
        count = len(self.items)
        self.summary.setText(
            f"{count} ignored duplicate group{'s' if count != 1 else ''}."
        )

    def _restore(self, item: ReviewItem) -> None:
        self.service.restore(item)
        self.load(self.root_path)
        self.changed.emit()

    def _stage(self, item: ReviewItem, target: str) -> None:
        batch_id = self.service.stage(item, target)
        self.load(self.root_path)
        self.batch_staged.emit(batch_id)
        self.changed.emit()

    def _clear(self) -> None:
        self.items = []
        while self.card_layout.count() > 1:
            item = self.card_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
