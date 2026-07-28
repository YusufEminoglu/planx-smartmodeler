"""Keyboard-accessible editor for one graph connection."""
from __future__ import annotations

from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)

from ..core.graph_model import GraphModel


class ConnectionDialog(QDialog):
    """Choose one valid output-to-input connection without using the canvas."""

    def __init__(self, graph: GraphModel, parent=None) -> None:
        super().__init__(parent)
        self.graph = graph
        self.connection = None
        self.setWindowTitle("Connect workflow nodes")
        self.setAccessibleName("Connect workflow nodes")
        self.resize(620, 190)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "Choose an output, then choose one compatible input. Connections "
            "which would create a cycle or replace a single input are omitted."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        self.source_combo = QComboBox()
        self.source_combo.setAccessibleName("Source output")
        self.target_combo = QComboBox()
        self.target_combo.setAccessibleName("Compatible target input")
        form.addRow("Source output", self.source_combo)
        form.addRow("Target input", self.target_combo)
        layout.addLayout(form)

        self.status_label = QLabel("")
        self.status_label.setAccessibleName("Connection availability")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.connect_button = self.buttons.button(
            QDialogButtonBox.StandardButton.Ok
        )
        self.connect_button.setText("Connect")
        self.connect_button.setAutoDefault(False)
        self.connect_button.setDefault(False)
        self.buttons.accepted.connect(self._accept_connection)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        for node in graph.nodes.values():
            for port_id, port in node.outputs.items():
                self.source_combo.addItem(
                    f"{node.title} - {port.name} [{port.socket_type}]",
                    (node.node_id, port_id),
                )
        self.source_combo.currentIndexChanged.connect(self._refresh_targets)
        self._refresh_targets()

    def _refresh_targets(self) -> None:
        self.target_combo.clear()
        source = self.source_combo.currentData()
        if not source:
            self.status_label.setText("This workflow has no output ports.")
            self.connect_button.setEnabled(False)
            return
        start_node_id, start_port_id = source
        for node in self.graph.nodes.values():
            for port_id, port in node.inputs.items():
                valid, _reason = self.graph.validate_connection(
                    start_node_id,
                    start_port_id,
                    node.node_id,
                    port_id,
                )
                if valid:
                    self.target_combo.addItem(
                        f"{node.title} - {port.name} [{port.socket_type}]",
                        (node.node_id, port_id),
                    )
        available = self.target_combo.count()
        self.connect_button.setEnabled(available > 0)
        self.status_label.setText(
            f"{available} compatible target input(s) available."
            if available
            else "No compatible target input is available for this output."
        )

    def _accept_connection(self) -> None:
        source = self.source_combo.currentData()
        target = self.target_combo.currentData()
        if not source or not target:
            return
        self.connection = (*source, *target)
        self.accept()
