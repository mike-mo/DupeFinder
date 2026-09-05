from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from dupefinder.models import ReviewItem
from dupefinder.review import ReviewService, human_size
from dupefinder.thumbnails import IMAGE_EXTENSIONS, ThumbnailProvider
from dupefinder.ui.review_card import ReviewCard

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm"}
AUDIO_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg"}
ARCHIVE_EXTENSIONS = {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2"}
DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".txt",
    ".md",
}


class DuplicatesView(QWidget):
    batch_staged = Signal(int)
    changed = Signal()

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
        self.all_items: list[ReviewItem] = []
        self.visible_items: list[ReviewItem] = []
        self.expanded: dict[str, bool] = {}

        outer = QVBoxLayout(self)
        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name or path...")
        self.search.textChanged.connect(self._apply_filters)
        filters.addWidget(self.search, stretch=1)

        self.kind_filter = QComboBox()
        self.kind_filter.addItem("Files and folders", "all")
        self.kind_filter.addItem("Files only", "file")
        self.kind_filter.addItem("Folder rollups only", "folder")
        self.kind_filter.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.kind_filter)

        self.type_filter = QComboBox()
        for label, value in (
            ("All types", "all"),
            ("Images", "image"),
            ("Video", "video"),
            ("Audio", "audio"),
            ("Documents", "document"),
            ("Archives", "archive"),
            ("Other", "other"),
        ):
            self.type_filter.addItem(label, value)
        self.type_filter.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.type_filter)

        self.minimum_savings = QSpinBox()
        self.minimum_savings.setRange(0, 2_000_000)
        self.minimum_savings.setSuffix(" MB min savings")
        self.minimum_savings.valueChanged.connect(self._apply_filters)
        filters.addWidget(self.minimum_savings)

        self.sort_order = QComboBox()
        self.sort_order.addItem("Savings (largest first)", "savings")
        self.sort_order.addItem("Copies/locations", "copies")
        self.sort_order.addItem("Name", "name")
        self.sort_order.addItem("Path", "path")
        self.sort_order.currentIndexChanged.connect(self._apply_filters)
        filters.addWidget(self.sort_order)
        outer.addLayout(filters)

        actions = QHBoxLayout()
        self.summary = QLabel("Choose a folder and start a scan.")
        actions.addWidget(self.summary, stretch=1)
        self.stage_visible = QPushButton("Stage recommended for visible")
        self.stage_visible.clicked.connect(self._stage_all_visible)
        actions.addWidget(self.stage_visible)
        self.ignore_visible = QPushButton("Ignore visible")
        self.ignore_visible.clicked.connect(self._ignore_all_visible)
        actions.addWidget(self.ignore_visible)
        outer.addLayout(actions)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.container = QWidget()
        self.card_layout = QVBoxLayout(self.container)
        self.card_layout.setSpacing(5)
        self.card_layout.addStretch()
        self.scroll.setWidget(self.container)
        outer.addWidget(self.scroll)

    @property
    def count(self) -> int:
        return len(self.all_items)

    def load(self, root_path: str | None) -> None:
        self.root_path = root_path
        if not root_path:
            self.all_items = []
            self._apply_filters()
            return
        self.all_items = self.service.list_inbox(root_path)
        self._apply_filters()

    def refresh_group(self, _group_id: int) -> None:
        if self.root_path:
            self.load(self.root_path)
            self.changed.emit()

    def _apply_filters(self) -> None:
        query = self.search.text().strip().casefold()
        kind = self.kind_filter.currentData()
        file_type = self.type_filter.currentData()
        minimum = self.minimum_savings.value() * 1024 * 1024
        items = [
            item
            for item in self.all_items
            if (kind == "all" or item.kind == kind)
            and item.savings >= minimum
            and self._matches_text(item, query)
            and self._matches_type(item, file_type)
        ]
        sort_key = self.sort_order.currentData()
        if sort_key == "copies":
            items.sort(key=lambda item: (-item.copy_count, -item.savings, item.label.casefold()))
        elif sort_key == "name":
            items.sort(key=lambda item: (item.label.casefold(), -item.savings))
        elif sort_key == "path":
            items.sort(key=lambda item: (item.locations[0].casefold(), -item.savings))
        else:
            items.sort(key=lambda item: (-item.savings, item.label.casefold()))
        self.visible_items = items
        self._rebuild_cards()

    def _rebuild_cards(self) -> None:
        while self.card_layout.count() > 1:
            layout_item = self.card_layout.takeAt(0)
            if layout_item.widget():
                layout_item.widget().deleteLater()
        for item in reversed(self.visible_items):
            card = ReviewCard(
                item,
                self.thumbnails,
                expanded=self.expanded.get(item.key, False),
            )
            card.stage_requested.connect(self._stage)
            card.ignore_requested.connect(self._ignore)
            card.expansion_changed.connect(self.expanded.__setitem__)
            self.card_layout.insertWidget(0, card)
        total = len(self.all_items)
        visible = len(self.visible_items)
        savings = sum(item.savings for item in self.visible_items)
        self.summary.setText(
            f"{visible} of {total} inbox item{'s' if total != 1 else ''} visible · "
            f"{human_size(savings)} potential savings"
        )
        enabled = bool(self.visible_items)
        self.stage_visible.setEnabled(enabled)
        self.ignore_visible.setEnabled(enabled)

    def _stage(self, item: ReviewItem, target: str) -> None:
        try:
            batch_id = self.service.stage(item, target)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not stage decision", str(exc))
            return
        self.load(self.root_path)
        self.batch_staged.emit(batch_id)
        self.changed.emit()

    def _ignore(self, item: ReviewItem) -> None:
        self.service.ignore(item)
        self.load(self.root_path)
        self.changed.emit()

    def _stage_all_visible(self) -> None:
        if not self.visible_items:
            return
        savings = sum(item.savings for item in self.visible_items)
        answer = QMessageBox.question(
            self,
            "Stage visible recommendations",
            f"Stage the recommended destination for {len(self.visible_items)} visible "
            f"item(s), representing {human_size(savings)} potential savings?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        errors: list[str] = []
        last_batch_id = 0
        for item in list(self.visible_items):
            try:
                last_batch_id = self.service.stage(item)
            except (OSError, ValueError) as exc:
                errors.append(f"{item.label}: {exc}")
        self.load(self.root_path)
        if last_batch_id:
            self.batch_staged.emit(last_batch_id)
        self.changed.emit()
        if errors:
            QMessageBox.warning(
                self,
                "Some items were not staged",
                "\n".join(errors[:10]),
            )

    def _ignore_all_visible(self) -> None:
        if not self.visible_items:
            return
        answer = QMessageBox.question(
            self,
            "Ignore visible duplicates",
            f"Move all {len(self.visible_items)} visible item(s) to Ignored Duplicates?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        for item in list(self.visible_items):
            self.service.ignore(item)
        self.load(self.root_path)
        self.changed.emit()

    @staticmethod
    def _matches_text(item: ReviewItem, query: str) -> bool:
        if not query:
            return True
        return query in item.label.casefold() or any(
            query in location.casefold() for location in item.locations
        )

    @staticmethod
    def _matches_type(item: ReviewItem, selected: str) -> bool:
        if selected == "all":
            return True
        categories: set[str] = set()
        for mapping in item.mappings:
            suffix = Path(mapping.relative_path).suffix.casefold()
            if suffix in IMAGE_EXTENSIONS:
                categories.add("image")
            elif suffix in VIDEO_EXTENSIONS:
                categories.add("video")
            elif suffix in AUDIO_EXTENSIONS:
                categories.add("audio")
            elif suffix in DOCUMENT_EXTENSIONS:
                categories.add("document")
            elif suffix in ARCHIVE_EXTENSIONS:
                categories.add("archive")
            else:
                categories.add("other")
        return selected in categories
