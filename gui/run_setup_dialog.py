"""One sheet showing the whole workflow, with every input that still needs a value.

Replaces the previous chain of one modal dialog per node: a workflow with six
unconfigured nodes used to mean six consecutive pop-ups with no way to see the
flow, compare steps, or go back. Here the run order is visible top to bottom,
each step names where its connected inputs come from, and every open decision
is editable in place.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QLabel,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.graph_model import GraphModel, NodeDefinition
from .parameter_form import NodeParameterForm


class RunSetupDialog(QDialog):
    """Reviews and completes every step of a workflow before running it."""

    def __init__(self, graph: GraphModel, parent=None, iface=None) -> None:
        super().__init__(parent)
        self.graph = graph
        self.iface = iface
        self._forms: List[Tuple[NodeDefinition, NodeParameterForm]] = []
        # Taken before any editor exists so Cancel is a true rollback even
        # after the "show every parameter" toggle has rebuilt the sheet.
        self._original_parameters: Dict[str, dict] = {
            node_id: dict(node.parameters) for node_id, node in graph.nodes.items()
        }
        self.setWindowTitle("Run setup - review the whole workflow")
        self.resize(760, 720)
        self._build_ui()

    # -- construction ----------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        title = QLabel(self.graph.name or "Workflow")
        title.setObjectName("dialogTitle")
        root.addWidget(title)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("dialogSubtitle")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        self.show_all_check = QCheckBox("Show every parameter, not just the missing ones")
        self.show_all_check.toggled.connect(self._rebuild_steps)
        root.addWidget(self.show_all_check)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        root.addWidget(self.scroll, 1)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.run_button = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.run_button.setText("Run workflow")
        # Enter must never launch a run from a parameter editor.
        self.run_button.setAutoDefault(False)
        self.run_button.setDefault(False)
        self.buttons.accepted.connect(self._accept)
        self.buttons.rejected.connect(self.reject)
        root.addWidget(self.buttons)

        self._rebuild_steps()

    def _ordered_nodes(self) -> List[NodeDefinition]:
        ordered = self.graph.get_topological_order()
        if len(ordered) == len(self.graph.nodes):
            return ordered
        # A cyclic graph cannot be ordered; the run is refused elsewhere, but
        # the sheet must still show every step rather than nothing.
        return list(self.graph.nodes.values())

    def _rebuild_steps(self) -> None:
        # Keep whatever the user has already typed across the toggle.
        for node, form in self._forms:
            form.apply(form.collect())
        self._forms = []

        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setSpacing(10)
        show_all = self.show_all_check.isChecked()

        for index, node in enumerate(self._ordered_nodes(), start=1):
            layout.addWidget(self._build_step(index, node, show_all))
        layout.addStretch(1)

        self.scroll.setWidget(panel)
        self._refresh_summary()

    def _build_step(self, index: int, node: NodeDefinition, show_all: bool) -> QGroupBox:
        form_object = NodeParameterForm(node, self, iface=self.iface)
        self._forms.append((node, form_object))

        missing = form_object.unconfigured_names()
        heading = f"{index}. {node.title}"
        if missing:
            heading += f"   -  {len(missing)} input(s) needed"
        box = QGroupBox(heading)
        box.setObjectName("runStepNeedsInput" if missing else "runStep")
        outer = QVBoxLayout(box)

        subtitle = QLabel(node.algorithm_id)
        subtitle.setObjectName("dialogSubtitle")
        outer.addWidget(subtitle)

        incoming = self._incoming_description(node)
        if incoming:
            source_label = QLabel(incoming)
            source_label.setObjectName("stepSources")
            source_label.setWordWrap(True)
            outer.addWidget(source_label)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        outer.addWidget(line)

        panel = QWidget(box)
        form = QFormLayout(panel)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        added = form_object.populate(form, only_unconfigured=not show_all)
        if added:
            outer.addWidget(panel)
        else:
            panel.deleteLater()
            outer.addWidget(QLabel("Ready - every input of this step is set."))
        return box

    def _incoming_description(self, node: NodeDefinition) -> str:
        parts = []
        for edge in self.graph.incoming_edges(node.node_id):
            source = self.graph.nodes.get(edge.start_node_id)
            source_title = source.title if source is not None else edge.start_node_id
            parts.append(f"{edge.end_port_id} <- {source_title} ({edge.start_port_id})")
        if not parts:
            return ""
        return "Takes input from:  " + ";   ".join(parts)

    # -- state -----------------------------------------------------------

    def _collect_all(self) -> Dict[str, dict]:
        return {node.node_id: form.collect() for node, form in self._forms}

    def _missing_by_node(self, collected: Dict[str, dict]) -> Dict[str, List[str]]:
        missing: Dict[str, List[str]] = {}
        for node, form in self._forms:
            names = form.missing_in(collected[node.node_id])
            if names:
                missing[node.title] = names
        return missing

    def _refresh_summary(self) -> None:
        collected = self._collect_all()
        missing = self._missing_by_node(collected)
        steps = len(self._forms)
        if missing:
            total = sum(len(names) for names in missing.values())
            self.summary_label.setText(
                f"{steps} step(s) in run order. {total} input(s) across "
                f"{len(missing)} step(s) still need a value or a project layer."
            )
        else:
            self.summary_label.setText(
                f"{steps} step(s) in run order. Every required input is set."
            )

    def _accept(self) -> None:
        collected = self._collect_all()
        missing = self._missing_by_node(collected)
        if missing:
            detail = "\n".join(
                f"- {title}: {', '.join(names)}" for title, names in missing.items()
            )
            QMessageBox.warning(
                self,
                "Some inputs are still missing",
                "These steps cannot run yet:\n\n" + detail,
            )
            self._refresh_summary()
            return
        for node, form in self._forms:
            form.apply(collected[node.node_id])
        self.accept()

    def reject(self) -> None:
        for node_id, parameters in self._original_parameters.items():
            node = self.graph.nodes.get(node_id)
            if node is not None:
                node.parameters = dict(parameters)
        super().reject()
