"""Main SmartModeler GIS studio window for QGIS 4."""
from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtCore import QByteArray, QSize, QTimer, Qt
from qgis.PyQt.QtGui import QAction, QKeySequence
from qgis.PyQt.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QSplitter,
    QToolBar,
    QVBoxLayout,
    QWidget,
)
from qgis.core import QgsApplication, QgsSettings

from ..core.ai_client import AiNetworkClient
from ..core.ai_mcp_bridge import AiMcpBridge, AiResponseError
from ..core.ai_settings import AiSettingsStore, PROVIDERS
from ..core.algorithm_catalog import AlgorithmCatalog
from ..core.auto_layout import AutoLayoutEngine
from ..core.execution_engine import ExecutionError, GraphExecutionEngine
from ..core.graph_model import GraphIssue, GraphModel, NodeDefinition
from ..core.model3_serializer import Model3Serializer
from ..core.prompt_context import PromptContextLoader
from .ai_prompt_widget import AiPromptWidget
from .canvas_scene import CanvasScene
from .canvas_view import CanvasView
from .node_parameter_dialog import NodeParameterDialog
from .node_palette_widget import NodePaletteWidget
from .smart_proposal_bar import SmartProposalBar
from .theme import STUDIO_STYLE
from .wire_inspector_widget import WireInspectorWidget


class SmartModelerWindow(QMainWindow):
    """Visual QGIS Processing model designer with validated AI planning."""

    SETTINGS_PREFIX = "SmartModelerGIS/Window/"

    def __init__(self, iface, parent=None) -> None:
        super().__init__(parent)
        self.iface = iface
        self.settings = QgsSettings()
        self.setWindowTitle("SmartModeler GIS - QGIS 4 Workflow Studio")
        self.setMinimumSize(1040, 680)
        self.resize(1440, 900)
        self.setStyleSheet(STUDIO_STYLE)

        self.graph = GraphModel()
        self.scene = CanvasScene(self.graph)
        self.view = CanvasView(self.scene, self)
        self.execution_engine = GraphExecutionEngine(self)
        self.ai_client = AiNetworkClient(self)
        self._ai_canvas_snapshot: str | None = None
        self._ai_request_mode = "new"
        self._last_ai_undo_snapshot: str | None = None
        self._last_ai_applied_snapshot: str | None = None
        self._ai_busy = False
        self._is_executing = False

        self._build_ui()
        self._connect_permanent_signals()
        self._connect_scene_signals()
        self._restore_window_state()
        self._refresh_ai_profile()

    def _build_ui(self) -> None:
        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.ai_prompt_bar = AiPromptWidget(self)
        layout.addWidget(self.ai_prompt_bar)
        self.proposal_bar = SmartProposalBar(self)
        layout.addWidget(self.proposal_bar)

        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.palette_widget = NodePaletteWidget(self)
        self.inspector_widget = WireInspectorWidget(self)
        self.splitter.addWidget(self.palette_widget)
        self.splitter.addWidget(self.view)
        self.splitter.addWidget(self.inspector_widget)
        self.splitter.setCollapsible(1, False)
        self.splitter.setSizes([285, 870, 285])
        layout.addWidget(self.splitter, 1)

        self._build_toolbar()
        self.status_label = QLabel("Ready")
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(160)
        self.progress.hide()
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.progress)

    def _theme_icon(self, name: str):
        return QgsApplication.getThemeIcon(name)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Workflow", self)
        toolbar.setObjectName("SmartModelerWorkflowToolbar")
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        self.run_action = QAction(self._theme_icon("/mActionStart.svg"), "Run", self)
        self.run_action.setShortcut(QKeySequence("Ctrl+R"))
        self.run_action.triggered.connect(self.run_model)
        toolbar.addAction(self.run_action)

        validate_action = QAction(self._theme_icon("/mIconSuccess.svg"), "Validate", self)
        validate_action.triggered.connect(self.validate_model)
        toolbar.addAction(validate_action)
        toolbar.addSeparator()

        open_action = QAction(self._theme_icon("/mActionFileOpen.svg"), "Open", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.triggered.connect(self.import_model)
        toolbar.addAction(open_action)
        save_action = QAction(self._theme_icon("/mActionFileSave.svg"), "Save", self)
        save_action.setShortcut(QKeySequence.StandardKey.Save)
        save_action.triggered.connect(self.export_model)
        toolbar.addAction(save_action)
        toolbar.addSeparator()

        layout_action = QAction(self._theme_icon(
            "/mActionArrangeSymbolsLeft.svg"), "Auto layout", self)
        layout_action.triggered.connect(self.auto_layout)
        toolbar.addAction(layout_action)
        fit_action = QAction(self._theme_icon("/mActionZoomFullExtent.svg"), "Fit", self)
        fit_action.setShortcut(QKeySequence("F"))
        fit_action.triggered.connect(self.fit_graph)
        toolbar.addAction(fit_action)
        toolbar.addSeparator()

        settings_action = QAction(self._theme_icon("/mActionOptions.svg"), "AI connections", self)
        settings_action.triggered.connect(self.open_ai_settings)
        toolbar.addAction(settings_action)
        self.undo_ai_action = QAction(
            self._theme_icon("/mActionUndo.svg"), "Undo AI", self
        )
        self.undo_ai_action.setEnabled(False)
        self.undo_ai_action.triggered.connect(self.undo_last_ai_change)
        toolbar.addAction(self.undo_ai_action)
        clear_action = QAction(self._theme_icon("/mActionDeleteSelected.svg"), "Clear", self)
        clear_action.triggered.connect(self.clear_canvas)
        toolbar.addAction(clear_action)

    def _connect_permanent_signals(self) -> None:
        self.ai_prompt_bar.prompt_submitted.connect(self.generate_ai_graph)
        self.palette_widget.node_requested.connect(self.add_node_by_alg)
        self.palette_widget.package_requested.connect(self.load_preset_package)
        self.proposal_bar.proposal_selected.connect(self.add_node_by_alg)
        self.inspector_widget.configure_requested.connect(self.configure_node)
        self.execution_engine.node_state_changed.connect(self._node_state_changed)
        self.execution_engine.progress_changed.connect(self._execution_progress)
        self.ai_client.succeeded.connect(self._ai_succeeded)
        self.ai_client.failed.connect(self._ai_failed)
        self.ai_client.busy_changed.connect(self._ai_busy_changed)

    def _connect_scene_signals(self) -> None:
        self.scene.node_selected.connect(self.on_node_selected)
        self.scene.node_activated.connect(self.configure_node)
        self.scene.graph_changed.connect(self._sync_ai_workflow_state)
        self.scene.connection_rejected.connect(self._connection_rejected)

    def apply_agent_graph(self, graph: GraphModel) -> None:
        """Trusted seam for the Agent Workspace apply coordinator: atomically
        install an already-validated replacement graph and refresh the scene once
        through the same path the AI graph-planner uses. The coordinator captures
        the pre-state and rolls back by calling this again on failure."""
        self._set_graph(graph, fit=True)

    def _set_graph(self, graph: GraphModel, fit: bool = True) -> None:
        old_scene = self.scene
        self.graph = graph
        self.scene = CanvasScene(graph)
        self.view.set_canvas_scene(self.scene)
        for node in graph.nodes.values():
            self.scene.add_node_to_scene(node)
        for edge in graph.edges.values():
            self.scene.add_connection_to_scene(edge)
        self._connect_scene_signals()
        self.inspector_widget.inspect_node(None)
        if old_scene is not self.scene:
            old_scene.deleteLater()
        self._sync_ai_workflow_state()
        if fit:
            QTimer.singleShot(0, self.fit_graph)

    def _sync_ai_workflow_state(self) -> None:
        self.ai_prompt_bar.set_workflow_available(bool(self.graph.nodes))

    def open_ai_settings(self) -> None:
        from .ai_settings_dialog import AiSettingsDialog

        dialog = AiSettingsDialog(self)
        dialog.setStyleSheet(STUDIO_STYLE)
        dialog.exec()
        self._refresh_ai_profile()

    def _refresh_ai_profile(self) -> None:
        profile = AiSettingsStore().active_profile()
        provider = PROVIDERS[profile.provider_id]
        label = profile.name if profile.name else provider.name
        self.ai_prompt_bar.set_provider_name(label)

    def generate_ai_graph(self, prompt_text: str, mode: str = "new") -> None:
        if len(prompt_text) > 12000:
            QMessageBox.warning(
                self,
                "Prompt is too long",
                "Keep the workflow request under 12,000 characters.",
            )
            return
        mode = "improve" if mode == "improve" and self.graph.nodes else "new"
        if mode == "new" and not self._confirm_replace("build a new AI workflow"):
            return
        store = AiSettingsStore()
        profile = store.active_profile()
        self._ai_canvas_snapshot = Model3Serializer.export_to_json(self.graph)
        self._ai_request_mode = mode
        if profile.provider_id == "offline":
            result = AiMcpBridge.generate_offline(
                prompt_text, self.graph if mode == "improve" else None
            )
            self._review_and_apply_ai_result(result)
            return
        api_key = store.secret(profile.profile_id)
        errors = profile.validate(api_key)
        if errors:
            self._ai_canvas_snapshot = None
            self._ai_request_mode = "new"
            QMessageBox.warning(
                self,
                "AI connection is not ready",
                "\n".join(errors) + "\n\nOpen AI connections to fix this profile.",
            )
            return
        project_context = AlgorithmCatalog.project_context() if profile.include_project_context else ""
        algorithm_context = (
            AlgorithmCatalog.compact_ai_catalog(
                prompt_text,
                profile.max_catalog_algorithms,
                (
                    node.algorithm_id
                    for node in self.graph.nodes.values()
                )
                if mode == "improve"
                else (),
            )
            if profile.include_algorithm_catalog
            else ""
        )
        current_workflow = (
            AiMcpBridge.workflow_context(self.graph)
            if mode == "improve"
            else ""
        )
        system_prompt = PromptContextLoader().build(
            project_context, algorithm_context, current_workflow
        )
        user_prompt = (
            "Edit the supplied current workflow according to this request. Return "
            "the complete updated graph and preserve everything unrelated:\n\n"
            + prompt_text
            if mode == "improve"
            else prompt_text
        )
        self.ai_client.generate(profile, api_key, system_prompt, user_prompt)

    def _ai_succeeded(self, response: str) -> None:
        try:
            result = AiMcpBridge.parse_response(response)
        except AiResponseError as error:
            self._ai_canvas_snapshot = None
            self._ai_request_mode = "new"
            self.status_label.setText("AI workflow rejected")
            QMessageBox.critical(
                self,
                "AI workflow rejected",
                "The provider response was blocked before reaching the canvas:\n\n"
                + str(error),
            )
            return
        self._review_and_apply_ai_result(result)

    def _review_and_apply_ai_result(self, result) -> None:
        current_snapshot = Model3Serializer.export_to_json(self.graph)
        canvas_changed = (
            self._ai_canvas_snapshot is not None
            and current_snapshot != self._ai_canvas_snapshot
        )
        baseline = (
            Model3Serializer.import_from_json(self._ai_canvas_snapshot)
            if self._ai_canvas_snapshot is not None
            else None
        )
        request_mode = self._ai_request_mode
        self._ai_canvas_snapshot = None
        self._ai_request_mode = "new"
        if canvas_changed and QMessageBox.question(
            self,
            "Canvas changed while AI was planning",
            "Replace the newer canvas with the AI workflow?",
        ) != QMessageBox.StandardButton.Yes:
            self.status_label.setText("Kept the current workflow")
            return
        if request_mode == "improve" and baseline is not None:
            AiMcpBridge.preserve_existing_layout(baseline, result.graph)
            changes = AiMcpBridge.describe_graph_changes(baseline, result.graph)
            if changes == "No graph changes were proposed.":
                self.status_label.setText("AI kept the current workflow unchanged")
                QMessageBox.information(
                    self,
                    "No workflow changes proposed",
                    (result.summary + "\n\n" if result.summary else "") + changes,
                )
                return
            if QMessageBox.question(
                self,
                "Apply AI improvement?",
                (result.summary + "\n\n" if result.summary else "")
                + changes
                + "\n\nApply these validated changes to the canvas?",
            ) != QMessageBox.StandardButton.Yes:
                self.status_label.setText("AI improvement was not applied")
                return
        self._last_ai_undo_snapshot = current_snapshot
        self._set_graph(result.graph)
        self._last_ai_applied_snapshot = Model3Serializer.export_to_json(self.graph)
        self.undo_ai_action.setEnabled(True)
        self._show_ai_result(result.summary, result.warnings)

    def _ai_failed(self, message: str) -> None:
        self._ai_canvas_snapshot = None
        self._ai_request_mode = "new"
        QMessageBox.critical(self, "AI planning failed", message)
        self.status_label.setText("AI planning failed")

    def undo_last_ai_change(self) -> None:
        if self._last_ai_undo_snapshot is None:
            return
        current = Model3Serializer.export_to_json(self.graph)
        if (
            self._last_ai_applied_snapshot is not None
            and current != self._last_ai_applied_snapshot
            and QMessageBox.question(
                self,
                "Undo AI and replace newer edits?",
                "The canvas changed after the AI update. Restore the workflow from "
                "immediately before that AI turn?",
            ) != QMessageBox.StandardButton.Yes
        ):
            return
        graph = Model3Serializer.import_from_json(self._last_ai_undo_snapshot)
        if graph is None:
            QMessageBox.critical(
                self, "Undo AI failed", "The previous workflow snapshot is invalid."
            )
            return
        self._set_graph(graph)
        self._last_ai_undo_snapshot = None
        self._last_ai_applied_snapshot = None
        self.undo_ai_action.setEnabled(False)
        self.status_label.setText("Restored the workflow from before the last AI turn")

    def _ai_busy_changed(self, busy: bool) -> None:
        self._ai_busy = busy
        self.ai_prompt_bar.set_busy(busy)
        self.run_action.setEnabled(not busy and not self._is_executing)
        self.progress.setRange(0, 0 if busy else 100)
        self.progress.setVisible(busy)
        self.status_label.setText("AI is planning a validated workflow..." if busy else "Ready")

    def _show_ai_result(self, summary: str, warnings: list[str]) -> None:
        AlgorithmCatalog.autobind_unique_project_layers(self.graph)
        issues = self._workflow_issues()
        self._mark_workflow_issues(issues)
        missing = [item for item in issues if item.code == "missing_input"]
        if missing:
            self.status_label.setText(
                f"Workflow planned - {len(missing)} required inputs need setup"
            )
            QMessageBox.information(
                self,
                "Workflow planned - setup required",
                (summary + "\n\n" if summary else "")
                + f"{len(missing)} required input(s) still need project layers or values. "
                "Click Run to open the guided setup.\n\n"
                + "\n".join(f"- {item}" for item in warnings),
            )
            return
        self.status_label.setText(
            f"Workflow ready - {len(self.graph.nodes)} nodes, {len(self.graph.edges)} connections"
        )
        if warnings:
            QMessageBox.information(
                self,
                "Workflow ready - review required",
                (summary + "\n\n" if summary else "")
                + "\n".join(f"- {item}" for item in warnings),
            )

    def add_node_by_alg(
        self, algorithm_id: str, title: str | None = None, _category: str = "General"
    ) -> None:
        try:
            node = AlgorithmCatalog.create_node(algorithm_id, title=title)
        except ValueError as error:
            QMessageBox.warning(self, "Algorithm unavailable", str(error))
            return
        center = self.view.mapToScene(self.view.viewport().rect().center())
        offset = (len(self.graph.nodes) % 8) * 18.0
        node.x = center.x() + offset
        node.y = center.y() + offset
        item = self.scene.add_node_to_scene(node)
        self._sync_ai_workflow_state()
        AlgorithmCatalog.autobind_unique_project_layers(self.graph)
        self.scene.clearSelection()
        item.setSelected(True)
        all_issues = self._workflow_issues()
        node_issues = [
            issue for issue in all_issues if issue.node_id == node.node_id
        ]
        self._mark_workflow_issues(all_issues)
        if node_issues:
            self.status_label.setText(
                f"Added {node.title}; Run opens setup, or double-click to configure"
            )
        else:
            self.status_label.setText(f"Added {node.title}; ready")

    def load_preset_package(self, template_id: str) -> None:
        if not self._confirm_replace("load a starter workflow"):
            return
        prompts = {
            "tpl_buffer": "Buffer a project vector layer",
            "tpl_filter_buffer": "Filter a vector layer by expression and then buffer it",
            "tpl_terrain": "Calculate slope from a DEM raster",
        }
        prompt = prompts.get(template_id)
        if prompt:
            result = AiMcpBridge.generate_offline(prompt)
            self._set_graph(result.graph)
            self.status_label.setText(f"Loaded starter: {result.graph.name}")

    def on_node_selected(self, node: NodeDefinition | None) -> None:
        self.proposal_bar.update_for_node(node)
        self.inspector_widget.inspect_node(node)

    def configure_node(
        self, node: NodeDefinition, require_complete: bool = False
    ) -> bool:
        dialog = NodeParameterDialog(
            node, self, require_complete=require_complete
        )
        dialog.setStyleSheet(STUDIO_STYLE)
        if dialog.exec():
            self.graph.mark_dirty_from(node.node_id)
            item = self.scene.node_items.get(node.node_id)
            if item is not None:
                item.refresh()
            self.inspector_widget.inspect_node(node)
            return True
        return False

    def _workflow_issues(self) -> list[GraphIssue]:
        issues = self.graph.validate()
        for node in self.graph.nodes.values():
            if not AlgorithmCatalog.algorithm_exists(node.algorithm_id):
                issues.append(
                    GraphIssue(
                        "error",
                        f"Algorithm unavailable: {node.algorithm_id}",
                        node.node_id,
                        "algorithm",
                    )
                )
        return issues

    def _mark_workflow_issues(self, issues: list[GraphIssue]) -> None:
        by_node: dict[str, list[GraphIssue]] = {}
        for issue in issues:
            if issue.node_id:
                by_node.setdefault(issue.node_id, []).append(issue)
        for node_id, node in self.graph.nodes.items():
            node_issues = by_node.get(node_id, [])
            if node_issues:
                node.execution_state = (
                    "needs_input"
                    if all(issue.code == "missing_input" for issue in node_issues)
                    else "invalid"
                )
                node.execution_message = "; ".join(
                    issue.message for issue in node_issues
                )
            elif node.execution_state in ("needs_input", "invalid"):
                node.execution_state = "idle"
                node.execution_message = ""
            item = self.scene.node_items.get(node_id)
            if item is not None:
                item.refresh()

    def _focus_node(self, node: NodeDefinition) -> None:
        item = self.scene.node_items.get(node.node_id)
        if item is None:
            return
        self.scene.clearSelection()
        item.setSelected(True)
        self.view.centerOn(item)
        self.inspector_widget.inspect_node(node)

    def _configure_required_inputs(self, issues: list[GraphIssue]) -> bool:
        missing = [issue for issue in issues if issue.code == "missing_input"]
        other = [issue for issue in issues if issue.code != "missing_input"]
        if not missing or other:
            return False
        node_ids = list(dict.fromkeys(issue.node_id for issue in missing))
        names = "\n".join(
            f"- {self.graph.nodes[node_id].title}"
            for node_id in node_ids[:10]
            if node_id in self.graph.nodes
        )
        if QMessageBox.question(
            self,
            "Finish workflow setup",
            f"{len(missing)} required input(s) are not configured across "
            f"{len(node_ids)} node(s). Open each node now?\n\n{names}",
        ) != QMessageBox.StandardButton.Yes:
            self.status_label.setText("Workflow needs input configuration")
            return False

        for node_id in node_ids:
            node = self.graph.nodes.get(node_id)
            if node is None:
                continue
            current = [
                issue
                for issue in self._workflow_issues()
                if issue.node_id == node_id and issue.code == "missing_input"
            ]
            if not current:
                continue
            self._focus_node(node)
            self.status_label.setText(f"Configure required inputs: {node.title}")
            if not self.configure_node(node, require_complete=True):
                self._mark_workflow_issues(self._workflow_issues())
                self.status_label.setText("Workflow setup canceled")
                return False

        remaining = self._workflow_issues()
        self._mark_workflow_issues(remaining)
        if remaining:
            QMessageBox.warning(
                self,
                "Workflow still needs attention",
                self._format_issues(remaining),
            )
            return False
        self.status_label.setText("Workflow inputs configured")
        return True

    def _ensure_workflow_ready(self) -> bool:
        if not self.graph.nodes:
            QMessageBox.warning(self, "Workflow is empty", "Add at least one node first.")
            return False
        AlgorithmCatalog.autobind_unique_project_layers(self.graph)
        issues = self._workflow_issues()
        self._mark_workflow_issues(issues)
        if not issues:
            return True
        if self._configure_required_inputs(issues):
            return True
        if any(issue.code != "missing_input" for issue in issues):
            QMessageBox.warning(
                self,
                "Workflow needs attention",
                self._format_issues(issues),
            )
        return False

    def _format_issues(self, issues: list[GraphIssue]) -> str:
        return "\n".join(
            f"- {self.graph.nodes[item.node_id].title if item.node_id in self.graph.nodes else 'Graph'}: {item.message}"
            for item in issues
        )

    def validate_model(self) -> None:
        if self._ensure_workflow_ready():
            QMessageBox.information(self, "Workflow valid",
                                    "The graph is acyclic and all required inputs are configured.")

    def run_model(self) -> None:
        if self._is_executing or self._ai_busy:
            return
        if not self._ensure_workflow_ready():
            return
        self._is_executing = True
        self.run_action.setEnabled(False)
        self.ai_prompt_bar.setEnabled(False)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.show()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            report = self.execution_engine.execute(self.graph)
        except ExecutionError as error:
            QMessageBox.critical(self, "Workflow failed", str(error))
            self.status_label.setText("Workflow failed")
            return
        except Exception as error:
            QMessageBox.critical(
                self,
                "Unexpected workflow error",
                f"QGIS could not complete the workflow:\n{error}",
            )
            self.status_label.setText("Workflow failed")
            return
        finally:
            self._is_executing = False
            self.run_action.setEnabled(not self._ai_busy)
            self.ai_prompt_bar.setEnabled(not self._ai_busy)
            QApplication.restoreOverrideCursor()
            self.progress.hide()
        layers = "\n".join(
            f"- {name}" for name in report.added_layers) or "No map layers were produced."
        QMessageBox.information(
            self,
            "Workflow complete",
            f"Executed {report.executed_nodes} nodes.\n\nAdded to project:\n{layers}",
        )

    def _node_state_changed(self, node_id: str, _state: str, _message: str) -> None:
        item = self.scene.node_items.get(node_id)
        if item is not None:
            item.refresh()
        if self.inspector_widget.node is self.graph.nodes.get(node_id):
            self.inspector_widget.inspect_node(self.graph.nodes[node_id])

    def _execution_progress(self, value: int, message: str) -> None:
        self.progress.setValue(value)
        self.status_label.setText(message)
        QApplication.processEvents()

    def export_model(self) -> None:
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save workflow",
            self.graph.name.replace(" ", "_") + ".model3",
            "QGIS Processing model (*.model3);;SmartModeler project (*.smartmodeler.json)",
        )
        if not path:
            return
        if "SmartModeler" in selected_filter or path.lower().endswith(".json"):
            if not path.lower().endswith(".json"):
                path += ".smartmodeler.json"
            try:
                Path(path).write_text(Model3Serializer.export_to_json(self.graph), encoding="utf-8")
            except OSError as error:
                QMessageBox.critical(self, "Save failed", str(error))
                return
        else:
            if not path.lower().endswith(".model3"):
                path += ".model3"
            try:
                ok, error = Model3Serializer.export_to_model3(self.graph, path)
            except Exception as export_error:
                ok, error = False, str(export_error)
            if not ok:
                QMessageBox.critical(self, "QGIS model export failed", error)
                return
        self.status_label.setText(f"Saved {path}")

    def import_model(self) -> None:
        path, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Open workflow",
            "",
            "Workflow files (*.model3 *.json);;QGIS Processing model (*.model3);;SmartModeler project (*.json)",
        )
        if not path:
            return
        if path.lower().endswith(".model3"):
            try:
                graph, error = Model3Serializer.import_from_model3(path)
            except Exception as model_error:
                graph, error = None, str(model_error)
        else:
            try:
                graph = Model3Serializer.import_from_json(Path(path).read_text(encoding="utf-8"))
                error = "" if graph is not None else "The JSON file is not a valid SmartModeler project."
            except (OSError, UnicodeError) as file_error:
                graph, error = None, str(file_error)
        if graph is None:
            QMessageBox.critical(self, "Open failed", error)
            return
        if not self._confirm_replace("open this workflow"):
            return
        self._set_graph(graph)
        self.status_label.setText(f"Opened {path}")

    def auto_layout(self) -> None:
        AutoLayoutEngine.apply_layout(self.graph)
        for node_id, node in self.graph.nodes.items():
            item = self.scene.node_items.get(node_id)
            if item is not None:
                item.setPos(node.x, node.y)
        self.fit_graph()

    def fit_graph(self) -> None:
        rect = self.scene.itemsBoundingRect()
        if not rect.isEmpty():
            self.view.fitInView(rect.adjusted(-80, -80, 80, 80), Qt.AspectRatioMode.KeepAspectRatio)

    def clear_canvas(self) -> None:
        if self.graph.nodes and QMessageBox.question(
            self, "Clear workflow", "Remove every node and connection from the canvas?"
        ) != QMessageBox.StandardButton.Yes:
            return
        self._set_graph(GraphModel(), fit=False)
        self.status_label.setText("New empty workflow")

    def _connection_rejected(self, message: str) -> None:
        self.status_label.setText(f"Connection rejected: {message}")

    def _confirm_replace(self, action: str) -> bool:
        if not self.graph.nodes:
            return True
        return QMessageBox.question(
            self,
            "Replace current workflow?",
            f"{action.capitalize()} will replace the current canvas. Continue?",
        ) == QMessageBox.StandardButton.Yes

    def _restore_window_state(self) -> None:
        geometry = self.settings.value(self.SETTINGS_PREFIX + "geometry")
        state = self.settings.value(self.SETTINGS_PREFIX + "state")
        splitter = self.settings.value(self.SETTINGS_PREFIX + "splitter")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        if isinstance(state, QByteArray):
            self.restoreState(state)
        if isinstance(splitter, QByteArray):
            self.splitter.restoreState(splitter)

    def closeEvent(self, event) -> None:
        if self.ai_client.is_busy():
            self.ai_client.cancel()
        self.settings.setValue(self.SETTINGS_PREFIX + "geometry", self.saveGeometry())
        self.settings.setValue(self.SETTINGS_PREFIX + "state", self.saveState())
        self.settings.setValue(self.SETTINGS_PREFIX + "splitter", self.splitter.saveState())
        super().closeEvent(event)
