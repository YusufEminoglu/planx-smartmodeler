"""Compact parameter editor backed by live QGIS Processing definitions."""
from __future__ import annotations

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.graph_model import NodeDefinition
from .parameter_form import NodeParameterForm


class NodeParameterDialog(QDialog):
    """Edits literal values while connected inputs remain graph-controlled."""

    def __init__(
        self,
        node: NodeDefinition,
        parent=None,
        require_complete: bool = False,
    ) -> None:
        super().__init__(parent)
        self.node = node
        self.require_complete = require_complete
        self.form = NodeParameterForm(node, self, iface=getattr(parent, "iface", None))
        self.setWindowTitle(f"Configure - {node.title}")
        self.resize(560, 600)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        title = QLabel(self.node.title)
        title.setObjectName("dialogTitle")
        subtitle = QLabel(self.node.algorithm_id)
        subtitle.setObjectName("dialogSubtitle")
        subtitle.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(title)
        root.addWidget(subtitle)

        self.title_edit = QLineEdit(self.node.title)
        root.addWidget(self.title_edit)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        panel = QWidget(scroll)
        form = QFormLayout(panel)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll.setWidget(panel)
        root.addWidget(scroll, 1)

        self.form.populate(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _save(self) -> None:
        parameters = self.form.collect()
        missing = self.form.missing_in(parameters)
        if self.require_complete and missing:
            QMessageBox.warning(
                self,
                "Required inputs are missing",
                "Configure these inputs before saving:\n\n"
                + "\n".join(f"- {name}" for name in missing),
            )
            return
        self.node.title = self.title_edit.text().strip() or self.node.title
        self.form.apply(parameters)
        self.accept()
