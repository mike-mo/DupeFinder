from __future__ import annotations

from datetime import datetime
from functools import partial
from pathlib import Path

from PySide6.QtCore import QDir, QProcess, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QButtonGroup,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from dupefinder.models import FileRecord, ReviewItem
from dupefinder.review import (
    highlighted_path_html,
    human_size,
    timestamp_difference_flags,
)
from dupefinder.thumbnails import ThumbnailProvider


def open_path(path: str) -> None:
    QDesktopServices.openUrl(QUrl.fromLocalFile(path))


def show_in_explorer(path: str) -> None:
    native = QDir.toNativeSeparators(path)
    if Path(path).is_dir():
        QProcess.startDetached("explorer.exe", [native])
    else:
        QProcess.startDetached("explorer.exe", ["/select,", native])


class PathActionRow(QWidget):
    def __init__(
        self,
        path: str,
        comparison_paths: tuple[str, ...],
        *,
        radio: QRadioButton | None = None,
        file_record: FileRecord | None = None,
        show_modified: bool = False,
        show_created: bool = False,
        is_oldest: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.action_buttons: list[QToolButton] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 1, 2, 1)
        layout.setSpacing(5)

        if radio:
            radio.setToolTip("Keep this location")
            layout.addWidget(radio)

        path_label = QLabel(highlighted_path_html(path, comparison_paths))
        path_label.setTextFormat(Qt.TextFormat.RichText)
        path_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        path_label.setToolTip(path)
        layout.addWidget(path_label, stretch=1)

        if file_record and (show_modified or show_created or is_oldest):
            details: list[str] = []
            if show_modified:
                modified = datetime.fromtimestamp(
                    file_record.mtime_ns / 1_000_000_000
                ).strftime("%Y-%m-%d %H:%M:%S")
                details.append(f"Modified {modified}")
            if show_created:
                created = datetime.fromtimestamp(
                    file_record.ctime_ns / 1_000_000_000
                ).strftime("%Y-%m-%d %H:%M:%S")
                details.append(f"Created {created}")
            if is_oldest:
                details.append(
                    "Oldest" if show_modified or show_created else "Recommended"
                )
            metadata = QLabel(" · ".join(details))
            metadata.setStyleSheet(
                "color:#187b35;font-weight:600;" if is_oldest else "color:#666;"
            )
            layout.addWidget(metadata)

        open_button = self._tool_button(
            QStyle.StandardPixmap.SP_DialogOpenButton,
            "Open",
            partial(open_path, path),
        )
        reveal_button = self._tool_button(
            QStyle.StandardPixmap.SP_DirOpenIcon,
            "Show in Explorer",
            partial(show_in_explorer, path),
        )
        layout.addWidget(open_button)
        layout.addWidget(reveal_button)

    def _tool_button(self, icon, tooltip: str, callback) -> QToolButton:
        button = QToolButton()
        button.setAutoRaise(True)
        button.setIcon(self.style().standardIcon(icon))
        button.setToolTip(tooltip)
        button.clicked.connect(callback)
        button.setVisible(False)
        self.action_buttons.append(button)
        return button

    def enterEvent(self, event) -> None:
        self.set_actions_visible(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:
        self.set_actions_visible(False)
        super().leaveEvent(event)

    def set_actions_visible(self, visible: bool) -> None:
        for button in self.action_buttons:
            button.setVisible(visible)


class ReviewCard(QFrame):
    stage_requested = Signal(object, str)
    ignore_requested = Signal(object)
    restore_requested = Signal(object)
    expansion_changed = Signal(str, bool)

    def __init__(
        self,
        item: ReviewItem,
        thumbnails: ThumbnailProvider,
        *,
        mode: str = "inbox",
        expanded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.item = item
        self.mode = mode
        self.setObjectName("reviewCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame#reviewCard { border:1px solid #d5d5d5; border-radius:5px; }"
        )

        outer = QVBoxLayout(self)
        outer.setContentsMargins(7, 5, 7, 5)
        outer.setSpacing(3)
        header = QHBoxLayout()
        header.setSpacing(7)

        self.toggle = QToolButton()
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )
        self.toggle.setToolTip("Expand details")
        self.toggle.clicked.connect(self._toggle_details)
        header.addWidget(self.toggle)

        preview = QLabel()
        preview.setFixedSize(44, 44)
        preview_path = (
            item.mappings[0].group.files[0].path
            if item.kind == "file"
            else item.locations[0]
        )
        preview.setPixmap(
            thumbnails.pixmap(preview_path).scaled(
                40,
                40,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header.addWidget(preview)

        summary = QVBoxLayout()
        summary.setSpacing(1)
        title = QLabel(f"<b>{item.label}</b>")
        summary.addWidget(title)
        if item.kind == "folder":
            count_text = (
                f"{item.file_count} duplicate files · {item.copy_count} folder locations · "
                f"{human_size(item.size)} duplicate content"
            )
        else:
            count_text = f"{item.copy_count} copies · {human_size(item.size)} each"
        details = QLabel(
            f"{count_text} · <b style='color:#187b35;'>Save {human_size(item.savings)}</b>"
        )
        details.setTextFormat(Qt.TextFormat.RichText)
        summary.addWidget(details)
        target = QLabel(f"Recommended location: {item.recommended_target}")
        target.setToolTip(item.recommended_target)
        target.setStyleSheet("color:#666;")
        summary.addWidget(target)
        header.addLayout(summary, stretch=1)

        if mode == "inbox":
            stage = QPushButton("Stage recommended")
            stage.clicked.connect(
                lambda: self.stage_requested.emit(item, item.recommended_target)
            )
            header.addWidget(stage)
            ignore = QPushButton("Ignore")
            ignore.clicked.connect(lambda: self.ignore_requested.emit(item))
            header.addWidget(ignore)
        else:
            restore = QPushButton("Restore to inbox")
            restore.clicked.connect(lambda: self.restore_requested.emit(item))
            header.addWidget(restore)
            stage = QPushButton("Stage recommended")
            stage.clicked.connect(
                lambda: self.stage_requested.emit(item, item.recommended_target)
            )
            header.addWidget(stage)

        outer.addLayout(header)

        self.details = QWidget()
        self.details.setVisible(expanded)
        details_layout = QVBoxLayout(self.details)
        details_layout.setContentsMargins(51, 2, 0, 2)
        details_layout.setSpacing(2)
        self.button_group = QButtonGroup(self)
        self.location_buttons: dict[str, QRadioButton] = {}

        if item.kind == "file":
            self._add_file_rows(details_layout)
        else:
            self._add_folder_rows(details_layout)

        if mode == "inbox":
            stage_selected = QPushButton("Stage selected location")
            stage_selected.clicked.connect(self._stage_selected)
            details_layout.addWidget(
                stage_selected,
                alignment=Qt.AlignmentFlag.AlignRight,
            )
        outer.addWidget(self.details)

    def _add_file_rows(self, layout: QVBoxLayout) -> None:
        group = self.item.mappings[0].group
        paths = tuple(file.path for file in group.files)
        show_modified, show_created = timestamp_difference_flags(group.files)
        oldest = group.oldest_file
        for file in group.files:
            radio = QRadioButton()
            self.button_group.addButton(radio)
            self.location_buttons[file.path] = radio
            if file.path == self.item.recommended_target:
                radio.setChecked(True)
            layout.addWidget(
                PathActionRow(
                    file.path,
                    paths,
                    radio=radio,
                    file_record=file,
                    show_modified=show_modified,
                    show_created=show_created,
                    is_oldest=file.id == oldest.id,
                )
            )

    def _add_folder_rows(self, layout: QVBoxLayout) -> None:
        paths = self.item.locations
        for location in paths:
            radio = QRadioButton()
            self.button_group.addButton(radio)
            self.location_buttons[location] = radio
            if location == self.item.recommended_target:
                radio.setChecked(True)
            layout.addWidget(
                PathActionRow(
                    location,
                    paths,
                    radio=radio,
                )
            )
        mapping_title = QLabel(
            f"<b>{self.item.file_count} mapped child files</b> "
            f"({self.item.relation} folder relationship)"
        )
        layout.addWidget(mapping_title)
        for mapping in self.item.mappings:
            oldest_location = next(
                (
                    location
                    for location, path in mapping.paths_by_location
                    if path == mapping.group.oldest_file.path
                ),
                "",
            )
            label = QLabel(
                f"{mapping.relative_path} · oldest from {oldest_location}"
            )
            label.setToolTip(
                "\n".join(path for _location, path in mapping.paths_by_location)
            )
            label.setStyleSheet("color:#666;")
            layout.addWidget(label)

    def _stage_selected(self) -> None:
        selected = next(
            (
                location
                for location, button in self.location_buttons.items()
                if button.isChecked()
            ),
            self.item.recommended_target,
        )
        self.stage_requested.emit(self.item, selected)

    def _toggle_details(self, checked: bool) -> None:
        self.details.setVisible(checked)
        self.toggle.setArrowType(
            Qt.ArrowType.DownArrow if checked else Qt.ArrowType.RightArrow
        )
        self.expansion_changed.emit(self.item.key, checked)
