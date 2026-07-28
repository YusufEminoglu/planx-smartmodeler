"""Selected node inspector and execution result summary."""
from __future__ import annotations

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QGroupBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.graph_model import NodeDefinition


class WireInspectorWidget(QWidget):
    configure_requested = pyqtSignal(object)
    node_requested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setAccessibleName("Node inspector")
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
        self.status.setAccessibleName("Selected node status")
        self.status.setObjectName("mutedLabel")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.outline = QTreeWidget()
        self.outline.setAccessibleName("Accessible workflow outline")
        self.outline.setAccessibleDescription(
            "Nodes with their complete inputs, outputs, connections, and state."
        )
        self.outline.setHeaderLabel("Workflow outline")
        self.outline.itemActivated.connect(self._outline_activated)
        layout.addWidget(self.outline, 1)

        self.configure_button = QPushButton("Configure parameters")
        self.configure_button.setAccessibleName(
            "Configure selected node parameters"
        )
        self.configure_button.setEnabled(False)
        self.configure_button.clicked.connect(self._configure)
        layout.addWidget(self.configure_button)

        group = QGroupBox("Parameters and results")
        group_layout = QVBoxLayout(group)
        self.table = QTableWidget()
        self.table.setAccessibleName("Selected node parameters and results")
        self.table.setColumnCount(2)
        self.table.setHorizontalHeaderLabels(["Name", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        group_layout.addWidget(self.table)
        layout.addWidget(group, 1)
        self._graph = None

    def set_graph(self, graph) -> None:
        self._graph = graph
        self.refresh_outline()

    def refresh_outline(self) -> None:
        self.outline.clear()
        if self._graph is None or not self._graph.nodes:
            empty = QTreeWidgetItem(
                ["No nodes. Add an algorithm from the library."]
            )
            empty.setFlags(Qt.ItemFlag.ItemIsEnabled)
            self.outline.addTopLevelItem(empty)
            return
        for node in self._graph.nodes.values():
            top = QTreeWidgetItem(
                [f"{node.title} - {node.execution_state}"]
            )
            top.setData(0, Qt.ItemDataRole.UserRole, node.node_id)
            inputs = QTreeWidgetItem([f"Inputs ({len(node.inputs)})"])
            outputs = QTreeWidgetItem([f"Outputs ({len(node.outputs)})"])
            for port_id, port in node.inputs.items():
                sources = [
                    (
                        self._graph.nodes[edge.start_node_id].title
                        if edge.start_node_id in self._graph.nodes
                        else edge.start_node_id
                    )
                    + "."
                    + edge.start_port_id
                    for edge in self._graph.incoming_edges(node.node_id)
                    if edge.end_port_id == port_id
                ]
                suffix = (
                    " <- " + ", ".join(sources)
                    if sources
                    else " - not connected"
                )
                inputs.addChild(
                    QTreeWidgetItem(
                        [f"{port.name} [{port.socket_type}]{suffix}"]
                    )
                )
            for port in node.outputs.values():
                outputs.addChild(
                    QTreeWidgetItem(
                        [
                            f"{port.name} [{port.socket_type}] - "
                            f"{len(port.connected_edges)} connection(s)"
                        ]
                    )
                )
            top.addChildren([inputs, outputs])
            self.outline.addTopLevelItem(top)

    def _outline_activated(
        self, item: QTreeWidgetItem, _column: int
    ) -> None:
        node_id = item.data(0, Qt.ItemDataRole.UserRole)
        if node_id:
            self.node_requested.emit(str(node_id))

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
