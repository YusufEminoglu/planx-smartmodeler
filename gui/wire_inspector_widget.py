"""Selected node inspector and execution result summary."""
from __future__ import annotations

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.graph_model import NodeDefinition


class WireInspectorWidget(QWidget):
    configure_requested = pyqtSignal(object)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.node: NodeDefinition | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        eyebrow = QLabel("NODE INSPECTOR")
        eyebrow.setObjectName("panelEyebrow")
        layout.addWidget(eyebrow)
        self.title = QLabel("Nothing selected")
        self.title.setObjectName("inspectorTitle")
        self.title.setWordWrap(True)
        layout.addWidget(self.title)
        self.status = QLabel("Select a node to inspect its configuration and outputs.")
        self.status.setObjectName("mutedLabel")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.configure_button = QPushButton("Configure parameters")
        self.configure_button.setEnabled(False)
        self.configure_button.clicked.connect(self._configure)
        layout.addWidget(self.configure_button)

        group = QGroupBox("Parameters and results")
        group_layout = QVBoxLayout(group)
        self.table = QTableWidget()
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Name", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        group_layout.addWidget(self.table)
        layout.addWidget(group, 1)

    def inspect_node(self, node: NodeDefinition | None) -> None:
        self.node = node
        self.configure_button.setEnabled(node is not None)
        if node is None:
            self.title.setText("Nothing selected")
            self.status.setText("Select a node to inspect its configuration and outputs.")
            self.table.setRowCount(0)
            return
        self.title.setText(node.title)
        self.status.setText(
            f"{node.algorithm_id}\nState: {node.execution_state}"
            + (f" - {node.execution_message}" if node.execution_message else "")
        )
        rows = [(key, value) for key, value in node.parameters.items()]
        rows.extend(
            (f"result:{key}", self._result_summary(value))
            for key, value in node.cached_results.items()
        )
        self.table.setRowCount(len(rows))
        for index, (key, value) in enumerate(rows):
            self.table.setItem(index, 0, QTableWidgetItem(str(key)))
            self.table.setItem(index, 1, QTableWidgetItem(str(value)))

    @staticmethod
    def _result_summary(value) -> str:
        if hasattr(value, "name") and callable(value.name):
            return f"Layer: {value.name()}"
        text = str(value)
        return text if len(text) <= 300 else text[:297] + "..."

    def _configure(self) -> None:
        if self.node is not None:
            self.configure_requested.emit(self.node)
