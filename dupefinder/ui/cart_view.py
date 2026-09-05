from __future__ import annotations

from functools import partial

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dupefinder.db import Database
from dupefinder.review import ReviewService, human_size


class CartView(QWidget):
    cart_changed = Signal()
    commit_requested = Signal()

    def __init__(
        self,
        database: Database,
        service: ReviewService,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.database = database
        self.service = service
        self._commit_allowed = True

        layout = QVBoxLayout(self)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            ["Decision", "Destination", "Files removed", "Savings", "Status", ""]
        )
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for column in range(2, 6):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree)

        self.commit_button = QPushButton("Commit all ready changes")
        self.commit_button.clicked.connect(self.commit_requested.emit)
        layout.addWidget(self.commit_button)
        self.refresh()

    @property
    def count(self) -> int:
        return self.tree.topLevelItemCount()

    def refresh(self) -> None:
        self.tree.clear()
        batches = self.database.list_action_batches()
        ready_count = 0
        total_savings = 0
        for batch in batches:
            item = QTreeWidgetItem(
                [
                    batch.label,
                    "",
                    str(batch.removal_count),
                    human_size(batch.estimated_savings),
                    batch.status.title(),
                    "",
                ]
            )
            if batch.last_error:
                item.setToolTip(4, batch.last_error)
            self.tree.addTopLevelItem(item)

            current_review = self.service.find_item(batch.root_path, batch.review_key)
            if current_review:
                destination = QComboBox()
                destination.addItems(current_review.locations)
                selected = (
                    batch.target_root
                    if batch.target_root in current_review.locations
                    else current_review.recommended_target
                )
                destination.setCurrentText(selected)
                destination.currentTextChanged.connect(
                    partial(self._change_destination, batch.id, current_review)
                )
                self.tree.setItemWidget(item, 1, destination)
            else:
                item.setText(1, batch.target_root)

            buttons = QWidget()
            button_layout = QHBoxLayout(buttons)
            button_layout.setContentsMargins(0, 0, 0, 0)
            ignore = QPushButton("Ignore")
            ignore.clicked.connect(partial(self._ignore, batch.id))
            button_layout.addWidget(ignore)
            remove = QPushButton("Return to inbox")
            remove.clicked.connect(partial(self._remove, batch.id))
            button_layout.addWidget(remove)
            self.tree.setItemWidget(item, 5, buttons)

            for action in batch.actions:
                child = QTreeWidgetItem(
                    [
                        action.source_path,
                        action.target_path,
                        str(action.removal_count),
                        human_size(action.size * action.removal_count),
                        action.status.title(),
                        "",
                    ]
                )
                if action.last_error:
                    child.setToolTip(4, action.last_error)
                item.addChild(child)

            if batch.status != "stale" and batch.actions:
                ready_count += 1
                total_savings += batch.estimated_savings

        count = len(batches)
        self.summary.setText(
            f"{count} cart batch{'es' if count != 1 else ''} · "
            f"{ready_count} ready · {human_size(total_savings)} staged savings"
        )
        self.commit_button.setEnabled(
            self._commit_allowed and ready_count > 0
        )

    def set_commit_enabled(self, enabled: bool) -> None:
        self._commit_allowed = enabled
        self.refresh()

    def _change_destination(self, old_batch_id: int, review_item, target: str) -> None:
        try:
            new_batch_id = self.service.stage(review_item, target)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not update destination", str(exc))
            self.refresh()
            return
        if new_batch_id != old_batch_id:
            self.database.remove_action_batch(old_batch_id)
        self.refresh()
        self.cart_changed.emit()

    def _ignore(self, batch_id: int) -> None:
        try:
            self.service.move_batch_to_ignored(batch_id)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not ignore batch", str(exc))
            return
        self.refresh()
        self.cart_changed.emit()

    def _remove(self, batch_id: int) -> None:
        self.database.remove_action_batch(batch_id)
        self.refresh()
        self.cart_changed.emit()
