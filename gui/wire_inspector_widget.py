"""Live Wire Inspector & Data Probe widget for SmartModeler GIS."""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import QWidget, QVBoxLayout, QLabel, QTableWidget, QTableWidgetItem, QGroupBox, QHeaderView
from ..core.graph_model import NodeDefinition


class WireInspectorWidget(QWidget):
    """Inspects intermediate feature data, parameters, and outputs of selected nodes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(6, 6, 6, 6)

        self.lbl_title = QLabel("🔍 Live Wire & Node Inspector")
        self.lbl_title.setStyleSheet("font-weight: bold; color: #00E5FF; font-size: 12px;")
        self.layout.addWidget(self.lbl_title)

        self.lbl_status = QLabel("Select a node or wire to inspect live parameters and feature outputs.")
        self.lbl_status.setWordWrap(True)
        self.lbl_status.setStyleSheet("color: #B0BEC5; font-size: 11px;")
        self.layout.addWidget(self.lbl_status)

        # Attribute Table Preview
        self.grp_table = QGroupBox("📊 Sample Attributes")
        self.table_layout = QVBoxLayout(self.grp_table)
        self.table = QTableWidget()
        self.table.setColumnCount(3)
        self.table.setHorizontalHeaderLabels(["ID", "Name", "Value"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table_layout.addWidget(self.table)
        self.layout.addWidget(self.grp_table)

    def inspect_node(self, node: NodeDefinition):
        if not node:
            self.lbl_title.setText("🔍 Live Wire Inspector")
            self.lbl_status.setText("Select a node or wire to inspect live parameters and feature outputs.")
            self.table.setRowCount(0)
            return

        self.lbl_title.setText(f"🔍 Inspecting Node: {node.title}")
        status_text = f"Category: {node.category}\nID: {node.node_id}\nInputs: {len(node.inputs)} | Outputs: {len(node.outputs)}"
        self.lbl_status.setText(status_text)

        # Populate sample parameters into table
        self.table.setRowCount(len(node.parameters))
        for idx, (key, val) in enumerate(node.parameters.items()):
            self.table.setItem(idx, 0, QTableWidgetItem(str(idx + 1)))
            self.table.setItem(idx, 1, QTableWidgetItem(str(key)))
            self.table.setItem(idx, 2, QTableWidgetItem(str(val)))
