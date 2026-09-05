from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QHeaderView,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from dupefinder.db import Database
from dupefinder.review import human_size


class HistoryView(QWidget):
    undo_requested = Signal(int)

    def __init__(self, database: Database, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.database = database
        layout = QVBoxLayout(self)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            ["Commit", "When", "Recovered", "Status", ""]
        )
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in range(1, 5):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self.tree)
        self.refresh()

    @property
    def count(self) -> int:
        return self.tree.topLevelItemCount()

    def refresh(self) -> None:
        self.tree.clear()
        for history in self.database.list_commit_history():
            item = QTreeWidgetItem(
                [
                    history.label,
                    history.committed_at,
                    human_size(history.recovered_bytes),
                    history.status.replace("_", " ").title(),
                    "",
                ]
            )
            if history.last_error:
                item.setToolTip(3, history.last_error)
            self.tree.addTopLevelItem(item)
            for operation in history.items:
                child = QTreeWidgetItem(
                    [
                        operation.retained_path,
                        "",
                        human_size(operation.size),
                        operation.status.title(),
                        "",
                    ]
                )
                item.addChild(child)
            can_undo_or_finalize = (
                any(operation.status == "completed" for operation in history.items)
                or (
                    bool(history.items)
                    and all(operation.status == "undone" for operation in history.items)
                )
            )
            if (
                history.status in {"completed", "partial", "undo_error"}
                and can_undo_or_finalize
            ):
                undo = QPushButton("Undo")
                undo.clicked.connect(
                    lambda _checked=False, history_id=history.id: self.undo_requested.emit(
                        history_id
                    )
                )
                self.tree.setItemWidget(item, 4, undo)
