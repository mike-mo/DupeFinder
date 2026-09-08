from __future__ import annotations

from collections import deque
from pathlib import Path

from PySide6.QtCore import QFileInfo, QSize, QThread, Qt, QTimer, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFileIconProvider,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QSizePolicy,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dupefinder.models import ReviewItem
from dupefinder.result_hierarchy import ResultFolderNode, build_result_hierarchy
from dupefinder.review import ReviewService, human_size, timestamp_difference_flags
from dupefinder.thumbnails import IMAGE_EXTENSIONS, ThumbnailProvider
from dupefinder.ui.review_card import PathActionRow

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

ROLE_KIND = int(Qt.ItemDataRole.UserRole)
ROLE_KEY = ROLE_KIND + 1
KIND_FOLDER = "folder-group"
KIND_RESULT = "result"
KIND_DETAIL = "detail"
KIND_DUMMY = "dummy"


def _matches_text(item: ReviewItem, query: str) -> bool:
    if not query:
        return True
    return query in item.label.casefold() or any(
        query in location.casefold() for location in item.locations
    )


def _item_categories(item: ReviewItem) -> set[str]:
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
    return categories


class HierarchyBuildWorker(QThread):
    result_ready = Signal(object, object, int)

    def __init__(
        self,
        items: tuple[ReviewItem, ...],
        root_path: str,
        *,
        query: str,
        kind: str,
        file_type: str,
        minimum_savings: int,
        sort_key: str,
        request_id: int,
    ) -> None:
        super().__init__()
        self.items = items
        self.root_path = root_path
        self.query = query
        self.kind = kind
        self.file_type = file_type
        self.minimum_savings = minimum_savings
        self.sort_key = sort_key
        self.request_id = request_id

    def run(self) -> None:
        if self.isInterruptionRequested():
            return
        items = [
            item
            for item in self.items
            if (self.kind == "all" or item.kind == self.kind)
            and item.savings >= self.minimum_savings
            and _matches_text(item, self.query)
            and (
                self.file_type == "all"
                or self.file_type in _item_categories(item)
            )
        ]
        if self.isInterruptionRequested():
            return
        if self.sort_key == "copies":
            items.sort(
                key=lambda item: (
                    -item.copy_count,
                    -item.savings,
                    item.label.casefold(),
                )
            )
        elif self.sort_key == "name":
            items.sort(key=lambda item: (item.label.casefold(), -item.savings))
        elif self.sort_key == "path":
            items.sort(key=lambda item: (item.locations[0].casefold(), -item.savings))
        else:
            items.sort(key=lambda item: (-item.savings, item.label.casefold()))
        hierarchy = build_result_hierarchy(items, self.root_path)
        if self.isInterruptionRequested():
            return
        self.result_ready.emit(tuple(items), hierarchy, self.request_id)


class CompactResultRow(QWidget):
    toggle_requested = Signal(str)
    stage_requested = Signal(object, str)
    ignore_requested = Signal(object)

    def __init__(
        self,
        item: ReviewItem,
        thumbnails: ThumbnailProvider,
        thumbnail_path: str | None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self.setMinimumHeight(56)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(7)

        preview = QLabel()
        preview.setFixedSize(34, 34)
        pixmap = QPixmap(thumbnail_path) if thumbnail_path else QPixmap()
        if pixmap.isNull():
            representative = (
                item.mappings[0].group.files[0].path
                if item.kind == "file"
                else item.locations[0]
            )
            icon = QFileIconProvider().icon(QFileInfo(representative))
            pixmap = icon.pixmap(30, 30)
        preview.setPixmap(
            pixmap.scaled(
                30,
                30,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        preview.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(preview)

        summary = QVBoxLayout()
        summary.setSpacing(0)
        title = QLabel(f"<b>{item.label}</b>")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        summary.addWidget(title)
        if item.kind == "folder":
            stats = (
                f"{item.file_count} files · {item.copy_count} locations · "
                f"{human_size(item.size)} duplicate content"
            )
        else:
            stats = f"{item.copy_count} copies · {human_size(item.size)} each"
        details = QLabel(
            f"{stats} · <b style='color:#187b35;'>Save {human_size(item.savings)}</b>"
        )
        details.setTextFormat(Qt.TextFormat.RichText)
        details.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        summary.addWidget(details)
        recommended = QLabel(f"Recommended location: {item.recommended_target}")
        recommended.setStyleSheet("color:#666;")
        recommended.setToolTip(item.recommended_target)
        recommended.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        summary.addWidget(recommended)
        layout.addLayout(summary, stretch=1)

        stage = QPushButton("Stage recommended")
        stage.clicked.connect(
            lambda: self.stage_requested.emit(item, item.recommended_target)
        )
        layout.addWidget(stage)
        ignore = QPushButton("Ignore")
        ignore.clicked.connect(lambda: self.ignore_requested.emit(item))
        layout.addWidget(ignore)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_requested.emit(self.item.key)
            event.accept()
            return
        super().mousePressEvent(event)


class FolderGroupRow(QWidget):
    toggle_requested = Signal(str)
    stage_all_requested = Signal(object)
    ignore_all_requested = Signal(object)

    def __init__(
        self,
        node: ResultFolderNode,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.node = node
        self.setMinimumHeight(46)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(7)

        icon_label = QLabel()
        icon_label.setFixedSize(30, 30)
        icon_label.setPixmap(
            QFileIconProvider()
            .icon(QFileIconProvider.IconType.Folder)
            .pixmap(26, 26)
        )
        icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        layout.addWidget(icon_label)

        summary = QVBoxLayout()
        summary.setSpacing(0)
        title = QLabel(f"<b>{node.name}</b>")
        title.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        summary.addWidget(title)
        details = QLabel(
            f"{node.result_count} results · "
            f"<b style='color:#187b35;'>Save {human_size(node.savings)}</b>"
        )
        details.setTextFormat(Qt.TextFormat.RichText)
        details.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        summary.addWidget(details)
        layout.addLayout(summary, stretch=1)

        stage = QPushButton("Stage all recommended")
        stage.clicked.connect(lambda: self.stage_all_requested.emit(node))
        layout.addWidget(stage)
        ignore = QPushButton("Ignore all")
        ignore.clicked.connect(lambda: self.ignore_all_requested.emit(node))
        layout.addWidget(ignore)

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.toggle_requested.emit(self.node.path)
            event.accept()
            return
        super().mousePressEvent(event)


class DuplicatesView(QWidget):
    batch_staged = Signal(int)
    changed = Signal()
    browse_state_changed = Signal(bool)

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
        self.thumbnail_paths: dict[str, str] = {}
        self.result_items: dict[str, QTreeWidgetItem] = {}
        self.result_rows: dict[str, CompactResultRow] = {}
        self.folder_items: dict[str, QTreeWidgetItem] = {}
        self.folder_rows: dict[str, FolderGroupRow] = {}
        self.folder_nodes: dict[str, ResultFolderNode] = {}
        self.detail_groups: dict[str, QButtonGroup] = {}
        self._build_queue: deque[
            tuple[QTreeWidgetItem | None, ResultFolderNode | ReviewItem]
        ] = deque()
        self._build_generation = 0
        self._pressed_expansion: dict[int, bool] = {}
        self.hierarchy_worker: HierarchyBuildWorker | None = None
        self.hierarchy_dirty = False
        self.hierarchy_request_id = 0
        self.hierarchy_timer = QTimer(self)
        self.hierarchy_timer.setSingleShot(True)
        self.hierarchy_timer.setInterval(120)
        self.hierarchy_timer.timeout.connect(self._start_hierarchy_worker)
        self.shutting_down = False
        self.rebuilding_tree = False
        self._restore_expanded_folders: set[str] = set()
        self._restore_expanded_results: set[str] = set()
        self._restore_scroll_value = 0
        self._shutdown_restart_needed = False

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

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(22)
        self.tree.setUniformRowHeights(False)
        self.tree.itemPressed.connect(self._item_pressed)
        self.tree.itemClicked.connect(self._item_clicked)
        self.tree.itemExpanded.connect(self._item_expanded)
        self.tree.itemCollapsed.connect(self._item_collapsed)
        outer.addWidget(self.tree)

    @property
    def count(self) -> int:
        return len(self.all_items)

    def set_items(
        self,
        root_path: str | None,
        items: tuple[ReviewItem, ...] | list[ReviewItem],
        thumbnail_paths: dict[str, str] | None = None,
    ) -> None:
        if self.shutting_down:
            return
        self.root_path = root_path
        self.all_items = list(items)
        self.thumbnail_paths = thumbnail_paths or {}
        self._apply_filters(immediate=True)

    def clear_results(self, root_path: str | None) -> None:
        self.root_path = root_path
        self.all_items = []
        self.visible_items = []
        self.thumbnail_paths = {}
        self.hierarchy_timer.stop()
        self.hierarchy_request_id += 1
        self.hierarchy_dirty = False
        self._build_generation += 1
        self._build_queue.clear()
        self.tree.clear()
        self.tree.setEnabled(True)
        self.result_items.clear()
        self.result_rows.clear()
        self.folder_items.clear()
        self.folder_rows.clear()
        self.folder_nodes.clear()
        for group in self.detail_groups.values():
            group.deleteLater()
        self.detail_groups.clear()
        self._update_summary()
        self.browse_state_changed.emit(False)

    def _apply_filters(self, _value=None, *, immediate: bool = False) -> None:
        if self.shutting_down:
            return
        self.hierarchy_request_id += 1
        self.hierarchy_dirty = True
        self.stage_visible.setEnabled(False)
        self.ignore_visible.setEnabled(False)
        self.tree.setEnabled(False)
        self.summary.setText("Updating result groups...")
        if immediate:
            self.hierarchy_timer.stop()
            self._start_hierarchy_worker()
        elif not self.hierarchy_timer.isActive():
            self.hierarchy_timer.start()

    def _start_hierarchy_worker(self) -> None:
        if self.shutting_down:
            return
        if self.hierarchy_worker is not None:
            return
        if not self.hierarchy_dirty:
            return
        if not self.root_path:
            self.hierarchy_dirty = False
            self.visible_items = []
            self._start_tree_build([])
            return
        self.hierarchy_dirty = False
        worker = HierarchyBuildWorker(
            tuple(self.all_items),
            self.root_path,
            query=self.search.text().strip().casefold(),
            kind=self.kind_filter.currentData(),
            file_type=self.type_filter.currentData(),
            minimum_savings=self.minimum_savings.value() * 1024 * 1024,
            sort_key=self.sort_order.currentData(),
            request_id=self.hierarchy_request_id,
        )
        self.hierarchy_worker = worker
        worker.result_ready.connect(self._hierarchy_ready)
        worker.finished.connect(self._hierarchy_finished)
        worker.start()

    def _hierarchy_ready(
        self,
        items: tuple[ReviewItem, ...],
        hierarchy: list[ResultFolderNode | ReviewItem],
        request_id: int,
    ) -> None:
        if self.shutting_down:
            return
        if request_id != self.hierarchy_request_id:
            return
        self.visible_items = list(items)
        self._start_tree_build(hierarchy)

    def _hierarchy_finished(self) -> None:
        if self.hierarchy_worker:
            self.hierarchy_worker.deleteLater()
        self.hierarchy_worker = None
        if self.hierarchy_dirty and not self.shutting_down:
            QTimer.singleShot(0, self._start_hierarchy_worker)

    def _start_tree_build(
        self,
        hierarchy: list[ResultFolderNode | ReviewItem],
    ) -> None:
        self._restore_expanded_folders = {
            path
            for path, item in self.folder_items.items()
            if self._is_effectively_expanded(item)
        }
        self._restore_expanded_results = {
            key
            for key, item in self.result_items.items()
            if self._is_effectively_expanded(item)
        }
        self._restore_scroll_value = self.tree.verticalScrollBar().value()
        self.rebuilding_tree = True
        self._build_generation += 1
        generation = self._build_generation
        self.tree.clear()
        self.result_items.clear()
        self.result_rows.clear()
        self.folder_items.clear()
        self.folder_rows.clear()
        self.folder_nodes.clear()
        for group in self.detail_groups.values():
            group.deleteLater()
        self.detail_groups.clear()
        self._build_queue.clear()
        self.tree.setEnabled(True)
        for entry in hierarchy:
            self._build_queue.append((None, entry))
        self._update_summary()
        QTimer.singleShot(0, lambda: self._build_chunk(generation))

    def _build_chunk(self, generation: int) -> None:
        if self.shutting_down or generation != self._build_generation:
            return
        for _index in range(50):
            if not self._build_queue:
                self._finish_tree_build()
                return
            parent, entry = self._build_queue.popleft()
            if isinstance(entry, ResultFolderNode):
                tree_item = QTreeWidgetItem()
                tree_item.setToolTip(0, entry.path)
                tree_item.setData(0, ROLE_KIND, KIND_FOLDER)
                tree_item.setData(0, ROLE_KEY, entry.path)
                if parent:
                    parent.addChild(tree_item)
                else:
                    self.tree.addTopLevelItem(tree_item)
                row = FolderGroupRow(entry)
                row.toggle_requested.connect(self._toggle_folder)
                row.stage_all_requested.connect(self._stage_folder_node)
                row.ignore_all_requested.connect(self._ignore_folder_node)
                self.tree.setItemWidget(tree_item, 0, row)
                row_size = row.sizeHint()
                tree_item.setSizeHint(
                    0,
                    QSize(row_size.width(), max(row_size.height(), row.minimumHeight())),
                )
                self.folder_items[entry.path] = tree_item
                self.folder_rows[entry.path] = row
                self.folder_nodes[entry.path] = entry
                if entry.path in self._restore_expanded_folders:
                    tree_item.setExpanded(True)
                for item in reversed(entry.items):
                    self._build_queue.appendleft((tree_item, item))
                for child in reversed(entry.children):
                    self._build_queue.appendleft((tree_item, child))
            else:
                tree_item = QTreeWidgetItem()
                tree_item.setData(0, ROLE_KIND, KIND_RESULT)
                tree_item.setData(0, ROLE_KEY, entry.key)
                dummy = QTreeWidgetItem()
                dummy.setData(0, ROLE_KIND, KIND_DUMMY)
                tree_item.addChild(dummy)
                if parent:
                    parent.addChild(tree_item)
                else:
                    self.tree.addTopLevelItem(tree_item)
                row = CompactResultRow(
                    entry,
                    self.thumbnails,
                    self.thumbnail_paths.get(entry.key),
                )
                row.toggle_requested.connect(self._toggle_result)
                row.stage_requested.connect(self._stage)
                row.ignore_requested.connect(self._ignore)
                self.tree.setItemWidget(tree_item, 0, row)
                row_size = row.sizeHint()
                tree_item.setSizeHint(
                    0,
                    QSize(row_size.width(), max(row_size.height(), row.minimumHeight())),
                )
                self.result_items[entry.key] = tree_item
                self.result_rows[entry.key] = row
                if entry.key in self._restore_expanded_results:
                    tree_item.setExpanded(True)
        if self._build_queue:
            QTimer.singleShot(0, lambda: self._build_chunk(generation))
        else:
            self._finish_tree_build()

    def _finish_tree_build(self) -> None:
        self.rebuilding_tree = False
        scroll_value = self._restore_scroll_value
        QTimer.singleShot(
            0,
            lambda: self.tree.verticalScrollBar().setValue(scroll_value),
        )
        self.browse_state_changed.emit(self.has_expanded_nodes())

    def _item_pressed(self, item: QTreeWidgetItem, _column: int) -> None:
        self._pressed_expansion[id(item)] = item.isExpanded()

    def _item_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        previous = self._pressed_expansion.pop(id(item), item.isExpanded())
        if item.data(0, ROLE_KIND) == KIND_FOLDER:
            if item.isExpanded() == previous:
                item.setExpanded(not item.isExpanded())

    def _toggle_result(self, key: str) -> None:
        item = self.result_items.get(key)
        if item:
            item.setExpanded(not item.isExpanded())

    def _toggle_folder(self, path: str) -> None:
        item = self.folder_items.get(path)
        if item:
            item.setExpanded(not item.isExpanded())

    def _item_expanded(self, item: QTreeWidgetItem) -> None:
        if item.data(0, ROLE_KIND) == KIND_RESULT:
            key = item.data(0, ROLE_KEY)
            review_item = next(
                (candidate for candidate in self.visible_items if candidate.key == key),
                None,
            )
            if review_item:
                self._populate_details(item, review_item)
        if not self.rebuilding_tree:
            self.browse_state_changed.emit(self.has_expanded_nodes())

    def _item_collapsed(self, item: QTreeWidgetItem) -> None:
        if item.data(0, ROLE_KIND) == KIND_RESULT:
            key = item.data(0, ROLE_KEY)
            item.takeChildren()
            dummy = QTreeWidgetItem()
            dummy.setData(0, ROLE_KIND, KIND_DUMMY)
            item.addChild(dummy)
            group = self.detail_groups.pop(key, None)
            if group:
                group.deleteLater()
        if not self.rebuilding_tree:
            self.browse_state_changed.emit(self.has_expanded_nodes())

    def _populate_details(
        self,
        tree_item: QTreeWidgetItem,
        review_item: ReviewItem,
    ) -> None:
        tree_item.takeChildren()
        group = QButtonGroup(self.tree)
        self.detail_groups[review_item.key] = group
        buttons: dict[str, QRadioButton] = {}
        if review_item.kind == "file":
            duplicate_group = review_item.mappings[0].group
            paths = tuple(file.path for file in duplicate_group.files)
            show_modified, show_created = timestamp_difference_flags(
                duplicate_group.files
            )
            oldest = duplicate_group.oldest_file
            rows = [
                (
                    file.path,
                    file,
                    file.id == oldest.id,
                )
                for file in duplicate_group.files
            ]
        else:
            paths = review_item.locations
            rows = [(path, None, False) for path in paths]

        for path, file_record, is_oldest in rows:
            radio = QRadioButton()
            group.addButton(radio)
            buttons[path] = radio
            if path == review_item.recommended_target:
                radio.setChecked(True)
            detail = QTreeWidgetItem()
            detail.setData(0, ROLE_KIND, KIND_DETAIL)
            tree_item.addChild(detail)
            widget = PathActionRow(
                path,
                paths,
                radio=radio,
                file_record=file_record,
                show_modified=bool(file_record) and show_modified,
                show_created=bool(file_record) and show_created,
                is_oldest=is_oldest,
            )
            self.tree.setItemWidget(detail, 0, widget)

        if review_item.kind == "folder":
            for mapping in review_item.mappings:
                detail = QTreeWidgetItem(
                    [f"{mapping.relative_path} · oldest from {mapping.group.oldest_file.path}"]
                )
                detail.setData(0, ROLE_KIND, KIND_DETAIL)
                tree_item.addChild(detail)

        action_item = QTreeWidgetItem()
        action_item.setData(0, ROLE_KIND, KIND_DETAIL)
        tree_item.addChild(action_item)
        action_widget = QWidget()
        action_layout = QHBoxLayout(action_widget)
        action_layout.setContentsMargins(0, 2, 4, 2)
        action_layout.addStretch()
        stage = QPushButton("Stage selected location")
        stage.clicked.connect(
            lambda _checked=False, current=review_item, current_buttons=buttons: self._stage(
                current,
                next(
                    (
                        location
                        for location, button in current_buttons.items()
                        if button.isChecked()
                    ),
                    current.recommended_target,
                ),
            )
        )
        action_layout.addWidget(stage)
        self.tree.setItemWidget(action_item, 0, action_widget)

    def _stage(self, item: ReviewItem, target: str) -> None:
        try:
            batch_id = self.service.stage(item, target)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not stage decision", str(exc))
            return
        self._remove_local_item(item.key)
        self.batch_staged.emit(batch_id)
        self.changed.emit()

    def _ignore(self, item: ReviewItem) -> None:
        self.service.ignore(item)
        self._remove_local_item(item.key)
        self.changed.emit()

    def _stage_folder_node(self, node: ResultFolderNode) -> None:
        items = node.descendant_items()
        if not items:
            return
        answer = QMessageBox.question(
            self,
            "Stage folder recommendations",
            f"Stage the recommended decisions for all {len(items)} results under "
            f"{node.name}, representing {human_size(node.savings)} potential savings?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        errors: list[str] = []
        staged_keys: set[str] = set()
        last_batch_id = 0
        for item in items:
            try:
                last_batch_id = self.service.stage(item)
                staged_keys.add(item.key)
            except (OSError, ValueError) as exc:
                errors.append(f"{item.label}: {exc}")
        self.all_items = [
            item for item in self.all_items if item.key not in staged_keys
        ]
        self._apply_filters(immediate=True)
        if last_batch_id:
            self.batch_staged.emit(last_batch_id)
        self.changed.emit()
        if errors:
            QMessageBox.warning(
                self,
                "Some folder results were not staged",
                "\n".join(errors[:10]),
            )

    def _ignore_folder_node(self, node: ResultFolderNode) -> None:
        items = node.descendant_items()
        if not items:
            return
        answer = QMessageBox.question(
            self,
            "Ignore folder results",
            f"Move all {len(items)} duplicate results under {node.name} to Ignored?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        ignored_keys = {item.key for item in items}
        for item in items:
            self.service.ignore(item)
        self.all_items = [
            item for item in self.all_items if item.key not in ignored_keys
        ]
        self._apply_filters(immediate=True)
        self.changed.emit()

    def _remove_local_item(self, key: str) -> None:
        self.all_items = [item for item in self.all_items if item.key != key]
        self._apply_filters(immediate=True)

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
        staged_keys: set[str] = set()
        last_batch_id = 0
        for item in list(self.visible_items):
            try:
                last_batch_id = self.service.stage(item)
                staged_keys.add(item.key)
            except (OSError, ValueError) as exc:
                errors.append(f"{item.label}: {exc}")
        self.all_items = [item for item in self.all_items if item.key not in staged_keys]
        self._apply_filters(immediate=True)
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
        ignored_keys = {item.key for item in self.visible_items}
        for item in list(self.visible_items):
            self.service.ignore(item)
        self.all_items = [item for item in self.all_items if item.key not in ignored_keys]
        self._apply_filters(immediate=True)
        self.changed.emit()

    def wait_for_workers(self, timeout_ms: int) -> bool:
        if self.hierarchy_worker and self.hierarchy_worker.isRunning():
            return self.hierarchy_worker.wait(timeout_ms)
        return True

    def has_expanded_nodes(self) -> bool:
        if self.rebuilding_tree and (
            self._restore_expanded_folders or self._restore_expanded_results
        ):
            return True
        return any(
            self._is_effectively_expanded(item)
            for item in self.folder_items.values()
        ) or any(
            self._is_effectively_expanded(item)
            for item in self.result_items.values()
        )

    @staticmethod
    def _is_effectively_expanded(item: QTreeWidgetItem) -> bool:
        if not item.isExpanded():
            return False
        parent = item.parent()
        while parent is not None:
            if not parent.isExpanded():
                return False
            parent = parent.parent()
        return True

    def begin_shutdown(self, timeout_ms: int) -> bool:
        self._shutdown_restart_needed = bool(
            self.hierarchy_dirty
            or (
                self.hierarchy_worker
                and self.hierarchy_worker.isRunning()
            )
            or self._build_queue
        )
        self.shutting_down = True
        self.hierarchy_timer.stop()
        self.hierarchy_dirty = False
        self._build_generation += 1
        self._build_queue.clear()
        if self.hierarchy_worker and self.hierarchy_worker.isRunning():
            self.hierarchy_worker.requestInterruption()
            return self.hierarchy_worker.wait(timeout_ms)
        return True

    def cancel_shutdown(self) -> None:
        self.shutting_down = False
        self.tree.setEnabled(True)
        if self._shutdown_restart_needed:
            self._shutdown_restart_needed = False
            self._apply_filters(immediate=True)

    def _update_summary(self) -> None:
        if not self.root_path:
            self.summary.setText("Choose a folder and start a scan.")
            self.stage_visible.setEnabled(False)
            self.ignore_visible.setEnabled(False)
            return
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
