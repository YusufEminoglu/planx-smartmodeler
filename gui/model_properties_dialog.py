"""Editable workflow metadata and declared-output contract."""
from __future__ import annotations

import re

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from ..core.graph_model import GraphModel


class ModelPropertiesDialog(QDialog):
    """Edit model identity and the exact set of public outputs."""

    OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

    def __init__(self, graph: GraphModel, parent=None) -> None:
        super().__init__(parent)
        self.graph = graph
        self.setWindowTitle("Model properties")
        self.resize(820, 560)
        self.result_name = graph.name
        self.result_description = graph.description
        self.result_outputs_declared = graph.outputs_declared
        self.result_outputs = dict(graph.outputs)

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.name_edit = QLineEdit(graph.name)
        self.description_edit = QTextEdit(graph.description)
        self.description_edit.setMaximumHeight(100)
        form.addRow("Model name", self.name_edit)
        form.addRow("Description", self.description_edit)
        layout.addLayout(form)

        self.explicit_outputs = QCheckBox(
            "Publish only the outputs selected below"
        )
        self.explicit_outputs.setChecked(graph.outputs_declared)
        self.explicit_outputs.setToolTip(
            "When enabled, zero selected rows means the workflow adds no "
            "result layer. Intermediate Processing layer outputs can be "
            "published."
        )
        layout.addWidget(self.explicit_outputs)
        hint = QLabel(
            "Select exact public results. Public names are used as layer names "
            "when the workflow runs."
        )
        hint.setWordWrap(True)
        hint.setObjectName("mutedLabel")
        layout.addWidget(hint)

        self.output_table = QTableWidget(0, 5)
        self.output_table.setHorizontalHeaderLabels(
            ["Publish", "Public name", "Source", "Description", "Mandatory"]
        )
        self.output_table.horizontalHeader().setStretchLastSection(False)
        self.output_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.output_table, 1)
        self._populate_outputs()
        self.explicit_outputs.toggled.connect(self.output_table.setEnabled)
        self.output_table.setEnabled(self.explicit_outputs.isChecked())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _populate_outputs(self) -> None:
        existing_by_source = {
            (str(contract.get("node_id")), str(contract.get("output_name"))): (
                public_name,
                contract,
            )
            for public_name, contract in self.graph.outputs.items()
        }
        for node in self.graph.nodes.values():
            for output_name, port in node.outputs.items():
                if not self.graph.output_is_publishable(node, output_name):
                    continue
                row = self.output_table.rowCount()
                self.output_table.insertRow(row)
                existing = existing_by_source.get(
                    (node.node_id, output_name)
                )
                publish = QTableWidgetItem()
                publish.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                publish.setCheckState(
                    Qt.CheckState.Checked
                    if existing
                    else Qt.CheckState.Unchecked
                )
                publish.setData(
                    Qt.ItemDataRole.UserRole,
                    (node.node_id, output_name),
                )
                publish.setData(
                    Qt.ItemDataRole.UserRole + 1,
                    existing[1].get("default") if existing else None,
                )
                self.output_table.setItem(row, 0, publish)

                public_name = existing[0] if existing else output_name
                self.output_table.setItem(
                    row, 1, QTableWidgetItem(public_name)
                )
                source = QTableWidgetItem(
                    f"{node.title}.{port.name}  [{node.node_id}.{output_name}]"
                )
                source.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.output_table.setItem(row, 2, source)
                description = (
                    str(existing[1].get("description", ""))
                    if existing
                    else port.description
                )
                self.output_table.setItem(
                    row, 3, QTableWidgetItem(description)
                )
                mandatory = QTableWidgetItem()
                mandatory.setFlags(
                    Qt.ItemFlag.ItemIsEnabled
                    | Qt.ItemFlag.ItemIsUserCheckable
                )
                mandatory.setCheckState(
                    Qt.CheckState.Checked
                    if existing and existing[1].get("mandatory")
                    else Qt.CheckState.Unchecked
                )
                self.output_table.setItem(row, 4, mandatory)

    def accept(self) -> None:
        name = self.name_edit.text().strip()
        if not name or len(name) > 300:
            QMessageBox.warning(
                self,
                "Invalid model name",
                "Use a model name between 1 and 300 characters.",
            )
            return
        outputs = {}
        if self.explicit_outputs.isChecked():
            for row in range(self.output_table.rowCount()):
                publish = self.output_table.item(row, 0)
                if publish.checkState() != Qt.CheckState.Checked:
                    continue
                public_name = self.output_table.item(row, 1).text().strip()
                if not self.OUTPUT_NAME.fullmatch(public_name):
                    QMessageBox.warning(
                        self,
                        "Invalid public output",
                        "Public output names must be unique identifiers of at "
                        "most 128 characters.",
                    )
                    return
                if public_name in outputs:
                    QMessageBox.warning(
                        self,
                        "Duplicate public output",
                        "Each published output needs a unique public name.",
                    )
                    return
                node_id, output_name = publish.data(
                    Qt.ItemDataRole.UserRole
                )
                outputs[public_name] = {
                    "node_id": node_id,
                    "output_name": output_name,
                    "description": self.output_table.item(
                        row, 3
                    ).text().strip(),
                    "mandatory": (
                        self.output_table.item(row, 4).checkState()
                        == Qt.CheckState.Checked
                    ),
                    "default": publish.data(
                        Qt.ItemDataRole.UserRole + 1
                    ),
                }
        self.result_name = name
        self.result_description = self.description_edit.toPlainText().strip()
        self.result_outputs_declared = self.explicit_outputs.isChecked()
        self.result_outputs = outputs
        super().accept()
