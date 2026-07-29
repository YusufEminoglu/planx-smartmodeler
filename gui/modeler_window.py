"""Main SmartModeler GIS studio window for QGIS 4."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from qgis.PyQt.QtCore import QByteArray, QSize, QTimer, Qt
from qgis.PyQt.QtGui import QAction, QKeySequence
from qgis.PyQt.QtWidgets import (
    QDialog,
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
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsSettings,
)

from ..core.ai_client import AiNetworkClient, AiTokenUsage
from ..core.ai_mcp_bridge import AiMcpBridge, AiResponseError
from ..core.ai_settings import AiSettingsStore, PROVIDERS
from ..core.algorithm_catalog import AlgorithmCatalog
from ..core.auto_layout import AutoLayoutEngine
from ..core.document_state import DocumentHistory
from ..core.execution_engine import (
    ExecutionReport,
    ExecutionStatus,
    GraphExecutionEngine,
)
from ..core.graph_model import GraphIssue, GraphModel, NodeDefinition
from ..core.model3_serializer import Model3Serializer
from ..core.prompt_context import PromptContextLoader
from ..core.proposal_engine import ProposalRecommendation
from .ai_prompt_widget import AiPromptWidget
from .canvas_scene import CanvasScene
from .canvas_view import CanvasView
from .connection_dialog import ConnectionDialog
from .node_parameter_dialog import NodeParameterDialog
from .node_palette_widget import NodePaletteWidget
from .model_properties_dialog import ModelPropertiesDialog
from .smart_proposal_bar import SmartProposalBar
from .theme import STUDIO_STYLE
from .wire_inspector_widget import WireInspectorWidget


class SmartModelerWindow(QMainWindow):
    """Visual QGIS Processing model designer with validated AI planning."""

    SETTINGS_PREFIX = "SmartModelerGIS/Window/"
    RECOVERY_PREFIX = "SmartModelerGIS/Recovery/"
    AUTOSAVE_INTERVAL_MS = 30_000

    def __init__(self, iface, parent=None, external_run_active=None) -> None:
        super().__init__(parent)
        self.iface = iface
        self._external_run_active = external_run_active or (lambda: False)
        self.settings = QgsSettings()
        self.setWindowTitle("SmartModeler GIS - QGIS 4 Workflow Studio")
        self.setAccessibleName("SmartModeler GIS Workflow Studio")
        self.setAccessibleDescription(
            "Build, validate, and run QGIS Processing workflows."
        )
        self.setMinimumSize(1040, 680)
        self.resize(1440, 900)
        self.setStyleSheet(STUDIO_STYLE)

        self.graph = GraphModel()
        self.scene = CanvasScene(self.graph)
        self.view = CanvasView(self.scene, self)
        self.execution_engine = GraphExecutionEngine(self)
        self.ai_client = AiNetworkClient(self)
        self._token_input = 0
        self._token_output = 0
        self._token_total = 0
        self._ai_canvas_snapshot: str | None = None
        self._ai_request_mode = "new"
        self._last_ai_undo_snapshot: str | None = None
        self._last_ai_applied_snapshot: str | None = None
        self._ai_busy = False
        self._is_executing = False
        self._execution_graph = None
        self._execution_context = None
        self._execution_project = None
        self._execution_feedback = None
        self._execution_layer_lookup = None
        self._execution_algorithms = None
        initial_snapshot = Model3Serializer.export_to_json(self.graph)
        self.document_history = DocumentHistory(initial_snapshot)
        self._current_path: Path | None = None
        self._current_filter = ""
        self._history_suspended = False
        self._force_close = False
        self._execution_action_states = {}

        self._build_ui()
        self._connect_permanent_signals()
        self._connect_scene_signals()
        self._restore_window_state()
        self._refresh_ai_profile()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(self.AUTOSAVE_INTERVAL_MS)
        self._autosave_timer.timeout.connect(self._write_recovery_snapshot)
        self._autosave_timer.start()
        self._update_document_ui()
        QTimer.singleShot(0, self._offer_recovery)

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
        self.splitter.setAccessibleName("Workflow workspace")
        self.palette_widget = NodePaletteWidget(self)
        self.inspector_widget = WireInspectorWidget(self)
        self.inspector_widget.set_graph(self.graph)
        self.view.setAccessibleName("Workflow canvas")
        self.view.setAccessibleDescription(
            "Node graph. Delete removes selected items, Enter configures the "
            "selected node, and F fits the graph when the canvas has focus."
        )
        self.splitter.addWidget(self.palette_widget)
        self.splitter.addWidget(self.view)
        self.splitter.addWidget(self.inspector_widget)
        self.splitter.setCollapsible(1, False)
        self.splitter.setSizes([285, 870, 285])
        layout.addWidget(self.splitter, 1)

        self._build_toolbar()
        self.status_label = QLabel("Ready")
        self.status_label.setAccessibleName("Workflow status")
        self.progress = QProgressBar()
        self.progress.setAccessibleName("Workflow progress")
        self.progress.setTextVisible(False)
        self.progress.setFixedWidth(160)
        self.progress.hide()
        self.statusBar().addWidget(self.status_label, 1)
        self.token_usage_label = QLabel("Tokens -")
        self.token_usage_label.setAccessibleName("AI token usage")
        self.token_usage_label.setStyleSheet("color: #70849F;")
        self.token_usage_label.setToolTip(
            "Provider-reported AI token use in this Workflow Studio window."
        )
        self.statusBar().addPermanentWidget(self.token_usage_label)
        self.statusBar().addPermanentWidget(self.progress)

    def _theme_icon(self, name: str):
        return QgsApplication.getThemeIcon(name)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Workflow", self)
        toolbar.setObjectName("SmartModelerWorkflowToolbar")
        toolbar.setAccessibleName("Workflow commands")
        toolbar.setIconSize(QSize(18, 18))
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        self.undo_action = QAction(
            self._theme_icon("/mActionUndo.svg"), "Undo", self
        )
        self.undo_action.setShortcut(QKeySequence.StandardKey.Undo)
        self.undo_action.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.view.addAction(self.undo_action)
        self.undo_action.setStatusTip("Undo the last workflow edit")
        self.undo_action.triggered.connect(self.undo_document)
        toolbar.addAction(self.undo_action)
        self.redo_action = QAction(
            self._theme_icon("/mActionRedo.svg"), "Redo", self
        )
        self.redo_action.setShortcut(QKeySequence.StandardKey.Redo)
        self.redo_action.setShortcutContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.view.addAction(self.redo_action)
        self.redo_action.setStatusTip("Redo the last undone workflow edit")
        self.redo_action.triggered.connect(self.redo_document)
        toolbar.addAction(self.redo_action)
        toolbar.addSeparator()

        self.run_action = QAction(self._theme_icon("/mActionStart.svg"), "Run", self)
        self.run_action.setShortcut(QKeySequence("Ctrl+R"))
        self.run_action.setStatusTip(
            "Validate and run an immutable workflow snapshot"
        )
        self.run_action.triggered.connect(self.run_model)
        toolbar.addAction(self.run_action)
        self.cancel_run_action = QAction(
            self._theme_icon("/mActionCancel.svg"), "Cancel", self
        )
        self.cancel_run_action.setShortcut(QKeySequence("Esc"))
        self.cancel_run_action.setStatusTip(
            "Cancel the active workflow without adding result layers"
        )
        self.cancel_run_action.setEnabled(False)
        self.cancel_run_action.triggered.connect(self.cancel_model)
        toolbar.addAction(self.cancel_run_action)

        setup_action = QAction(
            self._theme_icon("/mActionEditTable.svg"), "Run setup", self
        )
        setup_action.setToolTip(
            "Review every step in run order and fill in the missing inputs"
        )
        setup_action.setStatusTip(setup_action.toolTip())
        # Explicit lambda: QAction.triggered passes a `checked` bool that would
        # otherwise land in only_when_incomplete.
        setup_action.triggered.connect(lambda: self.open_run_setup())
        toolbar.addAction(setup_action)

        validate_action = QAction(self._theme_icon("/mIconSuccess.svg"), "Validate", self)
        validate_action.setStatusTip(
            "Check the graph and every required input without running it"
        )
        validate_action.triggered.connect(self.validate_model)
        toolbar.addAction(validate_action)
        properties_action = QAction(
            self._theme_icon("/mActionOptions.svg"), "Model properties", self
        )
        properties_action.setStatusTip(
            "Edit workflow metadata and published output layers"
        )
        properties_action.triggered.connect(self.open_model_properties)
        toolbar.addAction(properties_action)
        connect_action = QAction(
            self._theme_icon("/mActionLink.svg"), "Connect nodes", self
        )
        connect_action.setShortcut(QKeySequence("Ctrl+Shift+C"))
        connect_action.setStatusTip(
            "Create a compatible workflow connection using the keyboard"
        )
        connect_action.triggered.connect(self.open_connection_dialog)
        toolbar.addAction(connect_action)
        toolbar.addSeparator()

        new_action = QAction(self._theme_icon("/mActionFileNew.svg"), "New", self)
        new_action.setShortcut(QKeySequence.StandardKey.New)
        new_action.setStatusTip("Create a new empty workflow")
        new_action.triggered.connect(self.new_document)
        toolbar.addAction(new_action)
        open_action = QAction(self._theme_icon("/mActionFileOpen.svg"), "Open", self)
        open_action.setShortcut(QKeySequence.StandardKey.Open)
        open_action.setStatusTip("Open SmartModeler JSON or a QGIS model")
        open_action.triggered.connect(self.import_model)
        toolbar.addAction(open_action)
        self.save_action = QAction(
            self._theme_icon("/mActionFileSave.svg"), "Save", self
        )
        self.save_action.setShortcut(QKeySequence.StandardKey.Save)
        self.save_action.setStatusTip("Save the current workflow")
        self.save_action.triggered.connect(self.save_document)
        toolbar.addAction(self.save_action)
        self.save_as_action = QAction(
            self._theme_icon("/mActionFileSaveAs.svg"), "Save As", self
        )
        self.save_as_action.setShortcut(QKeySequence.StandardKey.SaveAs)
        self.save_as_action.setStatusTip(
            "Save the workflow to a new file or format"
        )
        self.save_as_action.triggered.connect(self.save_document_as)
        toolbar.addAction(self.save_as_action)
        toolbar.addSeparator()

        layout_action = QAction(self._theme_icon(
            "/mActionArrangeSymbolsLeft.svg"), "Auto layout", self)
        layout_action.setStatusTip("Arrange workflow nodes automatically")
        layout_action.triggered.connect(self.auto_layout)
        toolbar.addAction(layout_action)
        fit_action = QAction(self._theme_icon("/mActionZoomFullExtent.svg"), "Fit", self)
        fit_action.setShortcut(QKeySequence("Ctrl+Shift+F"))
        fit_action.setStatusTip("Fit the complete graph in the canvas")
        fit_action.triggered.connect(self.fit_graph)
        toolbar.addAction(fit_action)
        toolbar.addSeparator()

        settings_action = QAction(self._theme_icon("/mActionOptions.svg"), "AI connections", self)
        settings_action.setStatusTip("Configure offline or connected AI profiles")
        settings_action.triggered.connect(self.open_ai_settings)
        toolbar.addAction(settings_action)
        self.undo_ai_action = QAction(
            self._theme_icon("/mActionUndo.svg"), "Undo AI", self
        )
        self.undo_ai_action.setEnabled(False)
        self.undo_ai_action.setStatusTip(
            "Undo the most recent AI workflow replacement"
        )
        self.undo_ai_action.triggered.connect(self.undo_last_ai_change)
        toolbar.addAction(self.undo_ai_action)
        clear_action = QAction(self._theme_icon("/mActionDeleteSelected.svg"), "Clear", self)
        clear_action.setStatusTip("Remove every node after confirmation")
        clear_action.triggered.connect(self.clear_canvas)
        toolbar.addAction(clear_action)

        search_action = QAction("Find algorithm", self)
        search_action.setShortcut(QKeySequence.StandardKey.Find)
        search_action.setStatusTip("Focus the installed algorithm search")
        search_action.triggered.connect(self._focus_algorithm_search)
        self.addAction(search_action)

    def _focus_algorithm_search(self) -> None:
        self.palette_widget.search_bar.setFocus()
        self.palette_widget.search_bar.selectAll()

    def open_connection_dialog(self) -> None:
        if self._is_executing or not self.graph.nodes:
            return
        dialog = ConnectionDialog(self.graph, self)
        if (
            dialog.exec() != QDialog.DialogCode.Accepted
            or dialog.connection is None
        ):
            return
        self.scene.connect_ports(*dialog.connection)

    def _connect_permanent_signals(self) -> None:
        self.ai_prompt_bar.prompt_submitted.connect(self.generate_ai_graph)
        self.palette_widget.node_requested.connect(self.add_node_by_alg)
        self.palette_widget.package_requested.connect(self.load_preset_package)
        self.proposal_bar.algorithm_selected.connect(self.add_node_by_alg)
        self.proposal_bar.proposal_selected.connect(self.apply_smart_proposal)
        self.inspector_widget.configure_requested.connect(self.configure_node)
        self.inspector_widget.node_requested.connect(
            self._select_node_from_outline
        )
        self.execution_engine.node_state_changed.connect(self._node_state_changed)
        self.execution_engine.progress_changed.connect(self._execution_progress)
        self.ai_client.succeeded.connect(self._ai_succeeded)
        self.ai_client.failed.connect(self._ai_failed)
        self.ai_client.busy_changed.connect(self._ai_busy_changed)
        self.ai_client.usage_reported.connect(self._on_token_usage)

    def _on_token_usage(self, usage: AiTokenUsage) -> None:
        if not isinstance(usage, AiTokenUsage):
            return
        self._token_input += usage.input_tokens
        self._token_output += usage.output_tokens
        self._token_total += usage.total_tokens
        self.token_usage_label.setText(f"Tokens {self._token_total:,}")
        self.token_usage_label.setToolTip(
            "Provider-reported usage in this window: "
            f"{self._token_input:,} input + {self._token_output:,} output; "
            f"{self._token_total:,} total. A provider may include reasoning or "
            "cache tokens only in the total."
        )

    def _connect_scene_signals(self) -> None:
        self.scene.node_selected.connect(self.on_node_selected)
        self.scene.node_activated.connect(self.configure_node)
        self.scene.graph_changed.connect(self._on_scene_graph_changed)
        self.scene.connection_rejected.connect(self._connection_rejected)

    def apply_agent_graph(self, graph: GraphModel) -> None:
        """Trusted seam for the Agent Workspace apply coordinator: atomically
        install an already-validated replacement graph and refresh the scene once
        through the same path the AI graph-planner uses. The coordinator captures
        the pre-state and rolls back by calling this again on failure."""
        snapshot = Model3Serializer.export_to_json(graph)
        self._set_graph(graph, fit=True)
        self.document_history.rollback_current(snapshot)
        self._update_document_ui()

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
        self.inspector_widget.set_graph(graph)
        if old_scene is not self.scene:
            old_scene.deleteLater()
        self._sync_ai_workflow_state()
        if fit:
            QTimer.singleShot(0, self.fit_graph)

    def _sync_ai_workflow_state(self) -> None:
        self.ai_prompt_bar.set_workflow_available(bool(self.graph.nodes))

    def _on_scene_graph_changed(self) -> None:
        self._sync_ai_workflow_state()
        self.inspector_widget.refresh_outline()
        self._record_document_change()

    def _select_node_from_outline(self, node_id: str) -> None:
        item = self.scene.node_items.get(node_id)
        if item is None:
            return
        self.scene.clearSelection()
        item.setSelected(True)
        self.view.centerOn(item)
        self.view.setFocus()

    def _record_document_change(self) -> bool:
        if self._history_suspended:
            return False
        try:
            snapshot = Model3Serializer.export_to_json(self.graph)
        except ValueError as error:
            self.status_label.setText(f"Document change is not serializable: {error}")
            return False
        changed = self.document_history.record(snapshot)
        if changed:
            self._update_document_ui()
        return changed

    def _restore_document_snapshot(self, snapshot: str) -> bool:
        graph = Model3Serializer.import_from_json(snapshot)
        if graph is None:
            QMessageBox.critical(
                self,
                "Document history error",
                "The selected workflow revision is invalid.",
            )
            return False
        self._history_suspended = True
        try:
            self._set_graph(graph)
        finally:
            self._history_suspended = False
        self._update_document_ui()
        return True

    def undo_document(self) -> None:
        snapshot = self.document_history.undo()
        if snapshot is not None:
            self._restore_document_snapshot(snapshot)

    def redo_document(self) -> None:
        snapshot = self.document_history.redo()
        if snapshot is not None:
            self._restore_document_snapshot(snapshot)

    def _update_document_ui(self) -> None:
        if not hasattr(self, "undo_action"):
            return
        self.undo_action.setEnabled(self.document_history.can_undo)
        self.redo_action.setEnabled(self.document_history.can_redo)
        name = self._current_path.name if self._current_path else (
            self.graph.name or "Untitled workflow"
        )
        marker = "*" if self.document_history.is_dirty else ""
        self.setWindowTitle(
            f"{marker}{name} - SmartModeler GIS Workflow Studio"
        )

    def open_ai_settings(self) -> None:
        from .ai_settings_dialog import AiSettingsDialog

        dialog = AiSettingsDialog(self)
        dialog.setStyleSheet(STUDIO_STYLE)
        dialog.exec()
        self._refresh_ai_profile()

    def open_model_properties(self) -> None:
        dialog = ModelPropertiesDialog(self.graph, self)
        dialog.setStyleSheet(STUDIO_STYLE)
        if not dialog.exec():
            return
        self.graph.name = dialog.result_name
        self.graph.description = dialog.result_description
        self.graph.outputs_declared = dialog.result_outputs_declared
        self.graph.outputs = dialog.result_outputs
        self._record_document_change()
        self._update_document_ui()
        count = len(self.graph.outputs)
        self.status_label.setText(
            f"Model properties updated; {count} public output"
            f"{'' if count == 1 else 's'}"
        )

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
            baseline = (
                Model3Serializer.import_from_json(self._ai_canvas_snapshot)
                if self._ai_request_mode == "improve"
                and self._ai_canvas_snapshot is not None
                else None
            )
            result = AiMcpBridge.parse_response(response, base_graph=baseline)
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
        self._record_document_change()
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
        self._record_document_change()
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
        self._record_document_change()
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

    def apply_smart_proposal(
        self, proposal: ProposalRecommendation
    ) -> None:
        """Apply one ranked recommendation as an atomic add-and-connect edit."""
        source = self.graph.nodes.get(proposal.source_node_id)
        if source is None or proposal.source_port_id not in source.outputs:
            self.status_label.setText("Proposal expired; select the source node again")
            return
        try:
            node = AlgorithmCatalog.create_node(
                proposal.alg_id, title=proposal.title
            )
        except ValueError as error:
            QMessageBox.warning(self, "Algorithm unavailable", str(error))
            return
        if proposal.target_port_id not in node.inputs:
            self.status_label.setText("Proposal expired; target signature changed")
            return
        node.x = source.x + 350.0
        node.y = source.y + (
            len(list(self.graph.outgoing_edges(source.node_id))) * 95.0
        )
        item = self.scene.add_node_to_scene(node)
        edge = self.graph.add_edge(
            source.node_id,
            proposal.source_port_id,
            node.node_id,
            proposal.target_port_id,
        )
        if edge is None:
            self.graph.remove_node(node.node_id)
            self.scene.node_items.pop(node.node_id, None)
            self.scene.removeItem(item)
            self.status_label.setText(
                f"Proposal could not connect: {self.graph.last_error}"
            )
            return
        self.scene.add_connection_to_scene(edge)
        AlgorithmCatalog.autobind_unique_project_layers(self.graph)
        self._record_document_change()
        self.scene.clearSelection()
        item.setSelected(True)
        self.status_label.setText(proposal.preview)

    def load_preset_package(self, template_id: str) -> None:
        if not self._confirm_replace("load a starter workflow"):
            return
        from ..core.micro_packages import MicroPackageCatalog, MicroPackageError

        try:
            graph = MicroPackageCatalog.instantiate(template_id)
        except MicroPackageError as error:
            QMessageBox.warning(self, "Starter unavailable", str(error))
            return
        self._set_graph(graph)
        self._record_document_change()
        self.status_label.setText(f"Loaded starter: {graph.name}")

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
            self._record_document_change()
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

    def open_run_setup(self, only_when_incomplete: bool = False) -> bool:
        """Show the whole workflow in run order so every open input can be set.

        This is the guided setup the Run action uses. It replaced a chain of
        one modal per unconfigured node, which hid the flow and gave no way to
        go back a step.
        """
        from .run_setup_dialog import RunSetupDialog

        if not self.graph.nodes:
            QMessageBox.warning(self, "Workflow is empty", "Add at least one node first.")
            return False
        if only_when_incomplete and not [
            issue for issue in self._workflow_issues() if issue.code == "missing_input"
        ]:
            return True
        dialog = RunSetupDialog(self.graph, self, iface=self.iface)
        dialog.setStyleSheet(STUDIO_STYLE)
        accepted = bool(dialog.exec())
        for node_id in self.graph.nodes:
            item = self.scene.node_items.get(node_id)
            if item is not None:
                item.refresh()
        self.inspector_widget.inspect_node(self.inspector_widget.node)
        remaining = self._workflow_issues()
        self._mark_workflow_issues(remaining)
        if not accepted:
            self.status_label.setText("Workflow setup canceled")
            return False
        self._record_document_change()
        if remaining:
            QMessageBox.warning(
                self,
                "Workflow still needs attention",
                self._format_issues(remaining),
            )
            return False
        self.status_label.setText("Workflow inputs configured")
        return True

    def _configure_required_inputs(self, issues: list[GraphIssue]) -> bool:
        missing = [issue for issue in issues if issue.code == "missing_input"]
        other = [issue for issue in issues if issue.code != "missing_input"]
        if not missing or other:
            return False
        return self.open_run_setup()

    def _ensure_workflow_ready(self) -> bool:
        if not self.graph.nodes:
            QMessageBox.warning(self, "Workflow is empty", "Add at least one node first.")
            return False
        AlgorithmCatalog.autobind_unique_project_layers(self.graph)
        self._record_document_change()
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
        if (
            self._is_executing
            or self._ai_busy
            or self._external_run_active()
        ):
            if self._external_run_active():
                self.status_label.setText(
                    "Agent Workspace is already running an action."
                )
            return
        if not self._ensure_workflow_ready():
            return
        execution_graph = Model3Serializer.import_from_json(
            Model3Serializer.export_to_json(self.graph)
        )
        if execution_graph is None:
            QMessageBox.critical(
                self,
                "Workflow failed",
                "The workflow could not be prepared for execution.",
            )
            return
        no_threading = self._no_threading_algorithms(execution_graph)
        if no_threading:
            QMessageBox.warning(
                self,
                "Background execution unavailable",
                "This workflow contains Processing algorithms that QGIS requires "
                "to run on the main application thread:\n\n"
                + "\n".join(f"- {name}" for name in no_threading[:20])
                + "\n\nFor safety, SmartModeler will not send them to a "
                "background worker. Export the workflow as a native .model3 "
                "and run it with QGIS Model Designer.",
            )
            return
        registry = QgsApplication.processingRegistry()
        execution_algorithms = {}
        for node in execution_graph.nodes.values():
            if node.algorithm_id.startswith("smart:"):
                continue
            algorithm = registry.createAlgorithmById(
                node.algorithm_id, node.algorithm_configuration
            )
            if algorithm is None:
                QMessageBox.critical(
                    self,
                    "Workflow failed",
                    f"Processing algorithm is no longer available: "
                    f"{node.algorithm_id}",
                )
                return
            execution_algorithms[node.node_id] = algorithm
        project = QgsProject.instance()
        context = QgsProcessingContext()
        context.setProject(project)
        context.setTransformContext(project.transformContext())
        feedback = QgsProcessingFeedback()
        context.setFeedback(feedback)
        self._execution_graph = execution_graph
        self._execution_context = context
        self._execution_project = project
        self._execution_feedback = feedback
        self._execution_layer_lookup = {
            key: layer
            for layer_id, layer in project.mapLayers().items()
            for key in (layer_id, layer.name())
        }
        self._execution_algorithms = execution_algorithms
        self._is_executing = True
        self._set_execution_ui(True)
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.show()
        self.execution_engine.start_async(
            self._execution_graph,
            self._execution_finished,
            display_graph=self.graph,
            context=self._execution_context,
            project=self._execution_project,
            feedback=self._execution_feedback,
            layer_lookup=self._execution_layer_lookup,
            algorithm_lookup=self._execution_algorithms,
        )

    def _execution_finished(self, report) -> None:
        if not isinstance(report, ExecutionReport):
            report = ExecutionReport(
                ExecutionStatus.FAILED,
                len(self.graph.nodes),
                message="The workflow task returned no execution report.",
            )
        execution_graph = self._execution_graph
        if self._force_close or report.status == ExecutionStatus.CANCELED:
            self.execution_engine.discard_pending_outputs()
            report.status = ExecutionStatus.CANCELED
            report.added_layers = []
            report.added_layer_ids = []
            report.message = "Workflow execution was canceled."
        else:
            report = self.execution_engine.commit_pending_outputs(report)
        if execution_graph is not None:
            for node_id, executed_node in execution_graph.nodes.items():
                display_node = self.graph.nodes.get(node_id)
                if display_node is None:
                    continue
                display_node.execution_state = executed_node.execution_state
                display_node.execution_message = executed_node.execution_message
                display_node.is_dirty = executed_node.is_dirty
                display_node.cached_results = dict(executed_node.cached_results)
        self._execution_graph = None
        self._execution_context = None
        self._execution_project = None
        self._execution_feedback = None
        self._execution_layer_lookup = None
        self._execution_algorithms = None
        self._is_executing = False
        self._set_execution_ui(False)
        if not self._force_close:
            self.progress.hide()
        if self._force_close:
            return
        self._show_execution_report(report)

    def _show_execution_report(self, report: ExecutionReport) -> None:
        if report.status == ExecutionStatus.CANCELED:
            self.status_label.setText("Workflow canceled")
            QMessageBox.information(
                self,
                "Workflow canceled",
                f"Canceled after {report.executed_nodes} completed node(s). "
                "No result layers were added.",
            )
            return
        if report.status in (ExecutionStatus.FAILED, ExecutionStatus.PARTIAL):
            title = (
                "Workflow partially completed"
                if report.status == ExecutionStatus.PARTIAL
                else "Workflow failed"
            )
            self.status_label.setText(title)
            cleanup = (
                f"{len(report.added_layer_ids)} result layer(s) remain because "
                "QGIS rejected cleanup."
                if report.added_layer_ids
                else "No result layers were added."
            )
            QMessageBox.critical(
                self,
                title,
                report.message
                + f"\n\nCompleted nodes: {report.executed_nodes}. "
                + cleanup,
            )
            return
        layers = "\n".join(
            f"- {name}" for name in report.added_layers) or "No map layers were produced."
        self.status_label.setText("Workflow complete")
        QMessageBox.information(
            self,
            "Workflow complete",
            f"Executed {report.executed_nodes} nodes.\n\nAdded to project:\n{layers}",
        )

    def cancel_model(self) -> None:
        if not self._is_executing and not self.execution_engine.is_running():
            return
        self.cancel_run_action.setEnabled(False)
        self.status_label.setText("Canceling workflow...")
        self.execution_engine.cancel()

    def _set_execution_ui(self, running: bool) -> None:
        if running:
            self._execution_action_states = {
                action: action.isEnabled()
                for action in self.findChildren(QAction)
            }
            for action in self._execution_action_states:
                action.setEnabled(False)
            self.centralWidget().setEnabled(False)
            self.cancel_run_action.setEnabled(True)
            return
        if self._force_close:
            return
        self.centralWidget().setEnabled(True)
        for action, enabled in self._execution_action_states.items():
            action.setEnabled(enabled)
        self._execution_action_states = {}
        self.run_action.setEnabled(not self._ai_busy)
        self.cancel_run_action.setEnabled(False)
        self.ai_prompt_bar.setEnabled(not self._ai_busy)

    def _node_state_changed(self, node_id: str, _state: str, _message: str) -> None:
        node = self.graph.nodes.get(node_id)
        if node is not None:
            node.execution_state = _state
            node.execution_message = _message
        item = self.scene.node_items.get(node_id)
        if item is not None:
            item.refresh()
        self.inspector_widget.refresh_outline()
        if self.inspector_widget.node is self.graph.nodes.get(node_id):
            self.inspector_widget.inspect_node(self.graph.nodes[node_id])

    @staticmethod
    def _no_threading_algorithms(graph: GraphModel) -> list[str]:
        flag = None
        flag_enum = getattr(Qgis, "ProcessingAlgorithmFlag", None)
        if flag_enum is not None:
            flag = getattr(flag_enum, "NoThreading", None)
        if flag is None:
            flag = getattr(QgsProcessingAlgorithm, "FlagNoThreading", None)
        if flag is None:
            return []
        registry = QgsApplication.processingRegistry()
        blocked = []
        for node in graph.nodes.values():
            if node.algorithm_id.startswith("smart:"):
                continue
            algorithm = registry.createAlgorithmById(
                node.algorithm_id, node.algorithm_configuration
            )
            if algorithm is not None and bool(algorithm.flags() & flag):
                blocked.append(node.title)
        return blocked

    def _execution_progress(self, value: int, message: str) -> None:
        self.progress.setValue(value)
        self.status_label.setText(message)

    def save_document(self, _checked: bool = False) -> bool:
        if self._current_path is None:
            return self.save_document_as()
        return self._save_to_path(self._current_path, self._current_filter)

    def export_model(self) -> None:
        """Compatibility alias retained for existing integrations."""
        self.save_document_as()

    def save_document_as(self, _checked: bool = False) -> bool:
        path, selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save workflow",
            self.graph.name.replace(" ", "_") + ".smartmodeler.json",
            "SmartModeler project (*.smartmodeler.json)"
            ";;QGIS Processing model (*.model3)"
            ";;QGIS Python algorithm (*.py)",
        )
        if not path:
            return False
        return self._save_to_path(Path(path), selected_filter)

    def _save_to_path(self, path: Path, selected_filter: str = "") -> bool:
        path_text = str(path)
        lowered = path_text.lower()
        editable = True
        if "Python" in selected_filter or lowered.endswith(".py"):
            if not lowered.endswith(".py"):
                path = Path(path_text + ".py")
            if not self._export_python(str(path)):
                return False
            editable = False
        elif "QGIS Processing" in selected_filter or lowered.endswith(".model3"):
            if not lowered.endswith(".model3"):
                path = Path(path_text + ".model3")
            if not self._export_model3(str(path)):
                return False
        else:
            if not lowered.endswith(".json"):
                path = Path(path_text + ".smartmodeler.json")
            try:
                self._atomic_write_text(
                    path, Model3Serializer.export_to_json(self.graph)
                )
            except (OSError, ValueError) as error:
                QMessageBox.critical(self, "Save failed", str(error))
                return False
        if editable:
            self._current_path = path
            self._current_filter = selected_filter
            self.document_history.mark_clean()
            self._clear_recovery_snapshot()
            self._update_document_ui()
            self.status_label.setText(f"Saved {path}")
        else:
            self.status_label.setText(f"Exported {path}")
        return True

    @staticmethod
    def _temporary_sibling(path: Path) -> Path:
        return path.with_name(
            f".{path.stem}.{uuid.uuid4().hex}.tmp{path.suffix}"
        )

    @classmethod
    def _atomic_write_text(cls, path: Path, text: str) -> None:
        temporary = cls._temporary_sibling(path)
        try:
            temporary.write_text(text, encoding="utf-8")
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _atomic_export(cls, path: Path, writer) -> tuple[bool, str]:
        temporary = cls._temporary_sibling(path)
        try:
            ok, error = writer(str(temporary))
            if not ok:
                return False, error
            os.replace(temporary, path)
            return True, ""
        finally:
            temporary.unlink(missing_ok=True)

    def _export_model3(self, path: str) -> bool:
        """Save a .model3, treating QGIS' validation issues as advisory.

        An unfinished workflow must still be savable -- QGIS' own Model
        Designer saves incomplete models without complaint, and refusing to
        write the file was losing work over inputs the user had deliberately
        not bound yet.
        """
        try:
            ok, error = self._atomic_export(
                Path(path),
                lambda temporary: Model3Serializer.export_to_model3(
                    self.graph, temporary
                ),
            )
        except Exception as export_error:
            ok, error = False, str(export_error)
        if ok:
            return True
        if QMessageBox.question(
            self,
            "Save this workflow anyway?",
            "QGIS reports that the model is not fully configured yet:\n\n"
            + error
            + "\n\nSave it anyway? The file opens in the QGIS Model Designer, "
            "which will ask for the missing values when you run it.",
        ) != QMessageBox.StandardButton.Yes:
            self.status_label.setText("Save canceled")
            return False
        try:
            ok, error = self._atomic_export(
                Path(path),
                lambda temporary: Model3Serializer.export_to_model3(
                    self.graph, temporary, allow_invalid=True
                ),
            )
        except Exception as export_error:
            ok, error = False, str(export_error)
        if not ok:
            QMessageBox.critical(self, "QGIS model export failed", error)
            return False
        self.status_label.setText(f"Saved {path} (with unconfigured inputs)")
        return True

    def _export_python(self, path: str) -> bool:
        try:
            ok, error = self._atomic_export(
                Path(path),
                lambda temporary: Model3Serializer.export_to_python(
                    self.graph, temporary
                ),
            )
        except Exception as export_error:
            ok, error = False, str(export_error)
        if not ok:
            QMessageBox.critical(self, "Python export failed", error)
            return False
        return True

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
        self._current_path = Path(path)
        self._current_filter = (
            "QGIS Processing model (*.model3)"
            if path.lower().endswith(".model3")
            else "SmartModeler project (*.smartmodeler.json)"
        )
        self.document_history.reset(
            Model3Serializer.export_to_json(self.graph), mark_clean=True
        )
        self._clear_recovery_snapshot()
        self._update_document_ui()
        self.status_label.setText(f"Opened {path}")

    def auto_layout(self) -> None:
        AutoLayoutEngine.apply_layout(self.graph)
        for node_id, node in self.graph.nodes.items():
            item = self.scene.node_items.get(node_id)
            if item is not None:
                item.setPos(node.x, node.y)
        self._record_document_change()
        self.fit_graph()

    def fit_graph(self) -> None:
        rect = self.scene.itemsBoundingRect()
        if not rect.isEmpty():
            self.view.fitInView(rect.adjusted(-80, -80, 80, 80), Qt.AspectRatioMode.KeepAspectRatio)

    def new_document(self, _checked: bool = False) -> None:
        if not self._maybe_save_changes("create a new workflow"):
            return
        self._set_graph(GraphModel(), fit=False)
        self._current_path = None
        self._current_filter = ""
        self.document_history.reset(
            Model3Serializer.export_to_json(self.graph), mark_clean=True
        )
        self._clear_recovery_snapshot()
        self._update_document_ui()
        self.status_label.setText("New empty workflow")

    def clear_canvas(self) -> None:
        self.new_document()

    def _connection_rejected(self, message: str) -> None:
        self.status_label.setText(f"Connection rejected: {message}")

    def _confirm_replace(self, action: str) -> bool:
        return self._maybe_save_changes(action)

    def _maybe_save_changes(self, action: str) -> bool:
        if not self.document_history.is_dirty:
            return True
        choice = QMessageBox.warning(
            self,
            "Unsaved workflow changes",
            f"Save changes before you {action}?",
            QMessageBox.StandardButton.Save
            | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if choice == QMessageBox.StandardButton.Save:
            return self.save_document()
        if choice == QMessageBox.StandardButton.Discard:
            self._clear_recovery_snapshot()
            return True
        return False

    def _write_recovery_snapshot(self) -> None:
        if not self.document_history.is_dirty:
            return
        try:
            snapshot = Model3Serializer.export_to_json(self.graph)
        except ValueError:
            return
        self.settings.setValue(
            self.RECOVERY_PREFIX + "snapshot",
            snapshot,
        )
        self.settings.setValue(
            self.RECOVERY_PREFIX + "path",
            str(self._current_path) if self._current_path else "",
        )
        self.settings.setValue(
            self.RECOVERY_PREFIX + "timestamp",
            datetime.now(timezone.utc).isoformat(),
        )
        self.settings.sync()

    def _clear_recovery_snapshot(self) -> None:
        self.settings.remove(self.RECOVERY_PREFIX)
        self.settings.sync()

    def _offer_recovery(self) -> None:
        snapshot = self.settings.value(
            self.RECOVERY_PREFIX + "snapshot", "", type=str
        )
        if not snapshot:
            return
        graph = Model3Serializer.import_from_json(snapshot)
        if graph is None:
            self._clear_recovery_snapshot()
            return
        if QMessageBox.question(
            self,
            "Recover unsaved workflow?",
            "SmartModeler found an autosaved workflow from an earlier session. "
            "Restore it now?",
        ) != QMessageBox.StandardButton.Yes:
            self._clear_recovery_snapshot()
            return
        self._set_graph(graph)
        recovered_path = self.settings.value(
            self.RECOVERY_PREFIX + "path", "", type=str
        )
        self._current_path = Path(recovered_path) if recovered_path else None
        self._current_filter = ""
        self.document_history.reset(snapshot, mark_clean=False)
        self._update_document_ui()
        self.status_label.setText("Recovered unsaved workflow")

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
        if self._is_executing and not self._force_close:
            self.cancel_model()
            event.ignore()
            return
        if not self._force_close and not self._maybe_save_changes(
            "close the Workflow Studio"
        ):
            event.ignore()
            return
        if not self._force_close and self.document_history.is_dirty:
            clean = self.document_history.clean_snapshot
            graph = (
                Model3Serializer.import_from_json(clean)
                if clean is not None
                else GraphModel()
            )
            if graph is not None:
                self._set_graph(graph, fit=False)
                snapshot = Model3Serializer.export_to_json(graph)
                self.document_history.reset(snapshot, mark_clean=True)
                self._update_document_ui()
        if self.ai_client.is_busy():
            self.ai_client.cancel()
        self._autosave_timer.stop()
        self.settings.setValue(self.SETTINGS_PREFIX + "geometry", self.saveGeometry())
        self.settings.setValue(self.SETTINGS_PREFIX + "state", self.saveState())
        self.settings.setValue(self.SETTINGS_PREFIX + "splitter", self.splitter.saveState())
        super().closeEvent(event)

    def showEvent(self, event) -> None:
        if hasattr(self, "_autosave_timer") and not self._autosave_timer.isActive():
            self._autosave_timer.start()
        super().showEvent(event)

    def prepare_for_shutdown(self) -> None:
        """Preserve dirty work when QGIS unload cannot honor a canceled close."""
        if self._is_executing:
            self.execution_engine.cancel()
        if self.document_history.is_dirty:
            self._write_recovery_snapshot()
        self._force_close = True
