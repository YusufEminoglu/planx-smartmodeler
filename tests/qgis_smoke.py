"""Run with OSGeo4W python-qgis to verify live QGIS 4 integration."""
from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

from qgis.PyQt.QtCore import QEvent, QMetaType, QPointF, QTimer, Qt
from qgis.PyQt.QtGui import QAction, QColor, QIcon, QImage, QKeyEvent
from qgis.PyQt.QtWidgets import QApplication, QLineEdit
from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingModelAlgorithm,
    QgsProcessingModelChildAlgorithm,
    QgsProcessingModelChildDependency,
    QgsProcessingModelChildParameterSource,
    QgsProcessingModelOutput,
    QgsProcessingModelParameter,
    QgsProcessingOutputNumber,
    QgsProcessingOutputString,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterField,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
    QgsProcessingProvider,
    QgsProject,
    QgsVectorLayer,
    Qgis,
)

SILENT_CANCEL_OBSERVATIONS = []


def run_checks() -> str:
    plugin_root = Path(__file__).resolve().parents[2]
    plugin_root_text = str(plugin_root)
    while plugin_root_text in sys.path:
        sys.path.remove(plugin_root_text)
    sys.path.insert(0, plugin_root_text)
    for module_name in list(sys.modules):
        if module_name == "planx_smartmodeler" or module_name.startswith(
            "planx_smartmodeler."
        ):
            del sys.modules[module_name]

    import qgis.utils as qgis_utils_probe

    from planx_smartmodeler.core.agent.contracts import (
        AgentMode,
        AgentResultStatus,
        AgentScope,
        AgentToolCall,
    )
    from planx_smartmodeler.core.agent.controller import AgentController
    from planx_smartmodeler.core.agent.run_coordinator import (
        MAX_RESULT_LAYERS,
        RunCoordinator,
    )
    from planx_smartmodeler.core.agent.runtime_tools import build_default_registry
    from planx_smartmodeler.core.algorithm_catalog import AlgorithmCatalog
    from planx_smartmodeler.core.ai_client import AiNetworkClient, AiTokenUsage
    from planx_smartmodeler.core.ai_mcp_bridge import AiMcpBridge, AiResponseError
    from planx_smartmodeler.core.ai_settings import (
        AiProfile,
        AiSettingsStore,
        scoped_ai_settings_isolation,
    )
    from planx_smartmodeler.core.execution_engine import (
        ExecutionError,
        ExecutionStatus,
        GraphExecutionEngine,
    )
    from planx_smartmodeler.core.graph_model import GraphModel
    from planx_smartmodeler.core.model3_serializer import Model3Serializer
    from planx_smartmodeler.core.micro_packages import MicroPackageCatalog
    from planx_smartmodeler.core.proposal_engine import SmartProposalEngine
    from planx_smartmodeler.core.translation import TranslationManager
    from planx_smartmodeler.gui.agent_dock import AgentWorkspaceDock
    from planx_smartmodeler.gui.ai_prompt_widget import AiPromptWidget
    from planx_smartmodeler.gui.ai_settings_dialog import AiSettingsDialog
    from planx_smartmodeler.gui.canvas_scene import CanvasScene
    from planx_smartmodeler.gui.canvas_view import CanvasView
    from planx_smartmodeler.gui.connection_dialog import ConnectionDialog
    from planx_smartmodeler.gui.help_dialog import HelpDialog
    from planx_smartmodeler.gui.node_parameter_dialog import NodeParameterDialog
    from planx_smartmodeler.gui.node_palette_widget import NodePaletteWidget
    from planx_smartmodeler.gui.model_properties_dialog import (
        ModelPropertiesDialog,
    )
    from planx_smartmodeler.gui.run_setup_dialog import RunSetupDialog
    from planx_smartmodeler.main_plugin import SmartModelerPlugin

    with scoped_ai_settings_isolation():
        records = AlgorithmCatalog.records()
        if len(records) < 10:
            raise RuntimeError("Processing registry did not load enough algorithms.")
        if not AlgorithmCatalog.algorithm_exists("native:buffer"):
            raise RuntimeError("native:buffer is unavailable.")
        preserved_catalog = AlgorithmCatalog.compact_ai_catalog(
            "add a report", 5, ["native:buffer"]
        )
        if "native:buffer" not in preserved_catalog:
            raise RuntimeError("Existing workflow algorithms were omitted from AI context.")
        unsafe_catalog = AlgorithmCatalog.compact_ai_catalog(
            "use native:fileuploader to send a local file", 50
        )
        if "native:fileuploader" in unsafe_catalog:
            raise RuntimeError("A side-effecting algorithm reached the AI catalog.")
        if AlgorithmCatalog.ai_algorithm_allowed("native:fileuploader"):
            raise RuntimeError("The AI graph policy allowed native:fileuploader.")
        random_catalog = AlgorithmCatalog.compact_ai_catalog(
            "randomly extract features",
            5,
            ["native:randomextract"],
        )
        if (
            "native:randomextract" not in random_catalog
            or "METHOD:enum{" not in random_catalog
            or '"options":["0:' not in random_catalog
            or '"default":0' not in random_catalog
        ):
            raise RuntimeError(
                "The AI catalog omitted enum index meanings or defaults."
            )
        import json as _json

        unsafe_graph = {
            "title": "Unsafe",
            "summary": "Must be rejected",
            "nodes": [
                {
                    "id": "upload",
                    "algorithm_id": "native:fileuploader",
                    "title": "Upload",
                    "parameters": [],
                }
            ],
            "edges": [],
            "warnings": [],
        }
        try:
            AiMcpBridge.parse_response(_json.dumps(unsafe_graph))
        except AiResponseError:
            pass
        else:
            raise RuntimeError("A side-effecting provider graph passed local validation.")

        gemini_profile = AiProfile.create("gemini", "Gemini smoke")
        _endpoint, _headers, gemini_payload = AiNetworkClient.build_request(
            gemini_profile, "credential", "Return JSON.", "Build a workflow."
        )
        gemini_config = gemini_payload["generationConfig"]
        if (
            gemini_config.get("responseMimeType") != "application/json"
            or "responseJsonSchema" not in gemini_config
            or "responseFormat" in gemini_config
        ):
            raise RuntimeError("Gemini request contract is invalid.")

        deepseek_profile = AiProfile.create("deepseek", "DeepSeek smoke")
        deepseek_endpoint, _headers, deepseek_payload = AiNetworkClient.build_request(
            deepseek_profile, "credential", "Return JSON.", "Build a workflow."
        )
        if (
            deepseek_endpoint != "https://api.deepseek.com/chat/completions"
            or deepseek_payload.get("model") != "deepseek-v4-flash"
            or deepseek_payload.get("response_format") != {"type": "json_object"}
        ):
            raise RuntimeError("DeepSeek request contract is invalid.")

        graph = GraphModel("SmartModeler smoke test")
        source = AlgorithmCatalog.create_node("smart:input_layer", "source")
        buffer_node = AlgorithmCatalog.create_node("native:buffer", "buffer")
        project = QgsProject.instance()
        original_layer_ids = set(project.mapLayers())
        input_layer = QgsVectorLayer("Point?crs=EPSG:3857", "smoke_points", "memory")
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0, 0)))
        input_layer.dataProvider().addFeature(feature)
        input_layer.updateExtents()
        project.addMapLayer(input_layer)

        auto_graph = GraphModel("Automatic layer binding")
        auto_buffer = AlgorithmCatalog.create_node("native:buffer", "auto_buffer")
        auto_graph.add_node(auto_buffer)
        if (
            AlgorithmCatalog.autobind_unique_project_layers(auto_graph) != 1
            or auto_buffer.parameters.get("INPUT") != input_layer.id()
        ):
            raise RuntimeError("A unique compatible project layer was not auto-bound.")

        source.parameters["LAYER"] = input_layer.id()
        buffer_node.parameters["DISTANCE"] = 10.0
        graph.add_node(source)
        graph.add_node(buffer_node)
        edge = graph.add_edge("source", "OUTPUT", "buffer", "INPUT")
        if edge is None:
            raise RuntimeError(graph.last_error)

        report = GraphExecutionEngine().execute(graph)
        if (
            report.status != ExecutionStatus.COMPLETED
            or report.executed_nodes != 2
            or not report.added_layers
            or not report.added_layer_ids
            or any(
                project.mapLayer(layer_id) is None
                for layer_id in report.added_layer_ids
            )
        ):
            raise RuntimeError("Processing execution did not load the buffer result.")
        if isinstance(
            report.results.get("buffer", {}).get("OUTPUT"), QgsVectorLayer
        ):
            raise RuntimeError("Execution report retained a raw QGIS layer result.")

        branch_graph = GraphModel("Conditional execution")
        branch_source = AlgorithmCatalog.create_node(
            "smart:boolean", "condition", "Condition"
        )
        branch_source.parameters["VALUE"] = False
        branch_target = AlgorithmCatalog.create_node(
            "smart:number", "selected", "Selected branch"
        )
        branch_descendant = AlgorithmCatalog.create_node(
            "smart:number", "descendant", "Branch descendant"
        )
        branch_target.dependencies = ["condition"]
        branch_target.dependency_branches["condition"] = "OUTPUT"
        branch_descendant.dependencies = ["selected"]
        for branch_node in (branch_source, branch_target, branch_descendant):
            branch_graph.add_node(branch_node)
        branch_report = GraphExecutionEngine().execute(branch_graph)
        if (
            branch_report.status != ExecutionStatus.COMPLETED
            or branch_report.executed_nodes != 1
            or branch_target.execution_state != "skipped"
            or branch_descendant.execution_state != "skipped"
        ):
            raise RuntimeError("A false conditional branch was executed.")

        cancel_graph = GraphModel("Cancelable execution")
        cancel_first = AlgorithmCatalog.create_node(
            "smart:number", "cancel_first", "First"
        )
        cancel_second = AlgorithmCatalog.create_node(
            "smart:number", "cancel_second", "Second"
        )
        cancel_second.dependencies = ["cancel_first"]
        cancel_graph.add_node(cancel_first)
        cancel_graph.add_node(cancel_second)
        cancel_engine = GraphExecutionEngine()
        cancel_engine.progress_changed.connect(
            lambda _value, _message: cancel_engine.cancel()
        )
        cancel_project_ids = set(project.mapLayers())
        cancel_report = cancel_engine.execute(cancel_graph)
        if (
            cancel_report.status != ExecutionStatus.CANCELED
            or cancel_report.executed_nodes != 0
            or cancel_engine.is_running()
            or set(project.mapLayers()) != cancel_project_ids
        ):
            raise RuntimeError("Studio cancellation was not terminal and atomic.")

        prestart_cancel_engine = GraphExecutionEngine()
        prestart_cancel_engine.cancel()
        prestart_cancel_report = prestart_cancel_engine.execute(cancel_graph)
        if (
            prestart_cancel_report.status != ExecutionStatus.CANCELED
            or prestart_cancel_report.executed_nodes != 0
            or prestart_cancel_engine.is_running()
            or set(project.mapLayers()) != cancel_project_ids
        ):
            raise RuntimeError(
                "A cancellation arriving immediately before execution was lost."
            )

        partial_graph = GraphModel("Partial execution")
        partial_first = AlgorithmCatalog.create_node(
            "smart:number", "partial_first", "First"
        )
        partial_bad = AlgorithmCatalog.create_node(
            "smart:number", "partial_bad", "Invalid number"
        )
        partial_bad.parameters["VALUE"] = "not-a-number"
        partial_bad.dependencies = ["partial_first"]
        partial_graph.add_node(partial_first)
        partial_graph.add_node(partial_bad)
        partial_project_ids = set(project.mapLayers())
        partial_report = GraphExecutionEngine().execute(partial_graph)
        if (
            partial_report.status != ExecutionStatus.PARTIAL
            or partial_report.executed_nodes != 1
            or partial_report.failed_node_id != "partial_bad"
            or partial_report.added_layer_ids
            or set(project.mapLayers()) != partial_project_ids
        ):
            raise RuntimeError("Partial execution outcome or ownership is invalid.")

        exception_graph = GraphModel("Structured provider exception")
        exception_graph.add_node(
            AlgorithmCatalog.create_node(
                "smart:number", "exception_node", "Exception node"
            )
        )
        original_smart_executor = GraphExecutionEngine._execute_smart_node

        def _raise_plain_provider_exception(
            _node, _project, _layer_lookup=None
        ):
            raise Exception("plain provider failure")

        GraphExecutionEngine._execute_smart_node = staticmethod(
            _raise_plain_provider_exception
        )
        try:
            exception_report = GraphExecutionEngine().execute(exception_graph)
        finally:
            GraphExecutionEngine._execute_smart_node = staticmethod(
                original_smart_executor
            )
        if (
            exception_report.status != ExecutionStatus.FAILED
            or exception_report.failed_node_id != "exception_node"
            or "plain provider failure" not in exception_report.message
        ):
            raise RuntimeError("A plain provider exception escaped its report.")

        reentrant_graph = GraphModel("Reentrant execution")
        reentrant_graph.add_node(
            AlgorithmCatalog.create_node(
                "smart:number", "reentrant_number", "Number"
            )
        )
        reentrant_engine = GraphExecutionEngine()
        nested_reports = []

        def _attempt_nested_run(_value, _message):
            if not nested_reports:
                nested_reports.append(reentrant_engine.execute(reentrant_graph))

        reentrant_engine.progress_changed.connect(_attempt_nested_run)
        reentrant_report = reentrant_engine.execute(reentrant_graph)
        if (
            reentrant_report.status != ExecutionStatus.COMPLETED
            or len(nested_reports) != 1
            or nested_reports[0].status != ExecutionStatus.FAILED
            or "already running" not in nested_reports[0].message
            or reentrant_engine.is_running()
        ):
            raise RuntimeError("Execution engine accepted a reentrant run.")

        mutation_graph = GraphModel("Immutable execution snapshot")
        mutation_first = AlgorithmCatalog.create_node(
            "smart:number", "mutation_first", "First"
        )
        mutation_second = AlgorithmCatalog.create_node(
            "smart:number", "mutation_second", "Second"
        )
        mutation_second.dependencies = ["mutation_first"]
        mutation_graph.add_node(mutation_first)
        mutation_graph.add_node(mutation_second)
        mutation_engine = GraphExecutionEngine()
        mutation_attempted = []

        def _mutate_live_graph(_value, _message):
            if not mutation_attempted:
                mutation_attempted.append(True)
                mutation_graph.remove_node("mutation_second")

        mutation_engine.progress_changed.connect(_mutate_live_graph)
        mutation_report = mutation_engine.execute(mutation_graph)
        if (
            mutation_report.status != ExecutionStatus.COMPLETED
            or mutation_report.executed_nodes != 2
            or set(mutation_graph.nodes) != {"mutation_first"}
            or set(mutation_report.results)
            != {"mutation_first", "mutation_second"}
        ):
            raise RuntimeError("Execution did not use an immutable graph snapshot.")

        properties_graph = GraphModel("Declared outputs")
        properties_source = AlgorithmCatalog.create_node(
            "smart:input_layer", "properties_source", "Model input"
        )
        properties_buffer = AlgorithmCatalog.create_node(
            "native:buffer", "properties_buffer", "Intermediate buffer"
        )
        properties_centroids = AlgorithmCatalog.create_node(
            "native:centroids", "properties_centroids", "Undeclared terminal"
        )
        properties_number = AlgorithmCatalog.create_node(
            "smart:number", "properties_number", "Scalar input"
        )
        for properties_node in (
            properties_source,
            properties_buffer,
            properties_centroids,
            properties_number,
        ):
            properties_graph.add_node(properties_node)
        if (
            properties_graph.add_edge(
                "properties_source",
                "OUTPUT",
                "properties_buffer",
                "INPUT",
            )
            is None
            or properties_graph.add_edge(
                "properties_buffer",
                "OUTPUT",
                "properties_centroids",
                "INPUT",
            )
            is None
        ):
            raise RuntimeError("Model properties fixture could not be connected.")
        properties_graph.outputs_declared = True
        declared_context = QgsProcessingContext()
        declared_context.setProject(project)
        hidden_layer = QgsVectorLayer(
            "Point?crs=EPSG:3857", "hidden_terminal", "memory"
        )
        public_layer = QgsVectorLayer(
            "Point?crs=EPSG:3857", "public_intermediate", "memory"
        )
        if GraphExecutionEngine._load_terminal_outputs(
            properties_graph,
            {
                "properties_buffer": {"OUTPUT": public_layer},
                "properties_centroids": {"OUTPUT": hidden_layer},
            },
            declared_context,
            project,
        ):
            raise RuntimeError("A zero-output declaration loaded a terminal layer.")
        properties_graph.outputs["PUBLIC_RESULT"] = {
            "node_id": "properties_buffer",
            "output_name": "OUTPUT",
            "description": "",
            "mandatory": False,
            "default": None,
        }
        added_declared = GraphExecutionEngine._load_terminal_outputs(
            properties_graph,
            {
                "properties_buffer": {"OUTPUT": public_layer},
                "properties_centroids": {"OUTPUT": hidden_layer},
            },
            declared_context,
            project,
        )
        if (
            added_declared != ["PUBLIC_RESULT"]
            or project.mapLayer(public_layer.id()) is None
            or project.mapLayer(hidden_layer.id()) is not None
        ):
            raise RuntimeError("Declared output loading ignored the public contract.")
        properties_graph.outputs["PUBLIC_RESULT"]["mandatory"] = True
        try:
            GraphExecutionEngine._load_terminal_outputs(
                properties_graph,
                {},
                declared_context,
                project,
            )
        except ExecutionError:
            pass
        else:
            raise RuntimeError("A missing mandatory output did not fail the run.")

        atomic_layer = QgsVectorLayer(
            "Point?crs=EPSG:3857", "atomic_original", "memory"
        )
        properties_graph.outputs["MISSING_RESULT"] = {
            "node_id": "properties_centroids",
            "output_name": "OUTPUT",
            "description": "",
            "mandatory": True,
            "default": None,
        }
        project_ids_before_atomic_check = set(project.mapLayers())
        try:
            GraphExecutionEngine._load_terminal_outputs(
                properties_graph,
                {"properties_buffer": {"OUTPUT": atomic_layer}},
                declared_context,
                project,
            )
        except ExecutionError:
            pass
        else:
            raise RuntimeError("A partial mandatory output contract was accepted.")
        if (
            set(project.mapLayers()) != project_ids_before_atomic_check
            or project.mapLayer(atomic_layer.id()) is not None
            or atomic_layer.name() != "atomic_original"
        ):
            raise RuntimeError(
                "A failed mandatory output contract partially mutated the project."
            )
        properties_graph.outputs.pop("MISSING_RESULT")

        class _RejectSecondOutputProject:
            def __init__(self):
                self.layers = {}
                self.add_calls = 0

            def mapLayer(self, layer_id):
                return self.layers.get(layer_id)

            def addMapLayer(self, layer):
                self.add_calls += 1
                if self.add_calls == 2:
                    return None
                self.layers[layer.id()] = layer
                return layer

            def removeMapLayer(self, layer_id):
                self.layers.pop(layer_id, None)

        rollback_graph = GraphModel("Atomic output commit")
        rollback_first_node = AlgorithmCatalog.create_node(
            "native:buffer", "rollback_first", "First output"
        )
        rollback_second_node = AlgorithmCatalog.create_node(
            "native:centroids", "rollback_second", "Second output"
        )
        rollback_graph.add_node(rollback_first_node)
        rollback_graph.add_node(rollback_second_node)
        rollback_graph.outputs_declared = True
        rollback_graph.outputs["FIRST"] = {
            "node_id": "rollback_first",
            "output_name": "OUTPUT",
            "mandatory": True,
        }
        rollback_graph.outputs["SECOND"] = {
            "node_id": "rollback_second",
            "output_name": "OUTPUT",
            "mandatory": True,
        }
        rollback_first_layer = QgsVectorLayer(
            "Point?crs=EPSG:3857", "rollback_first_original", "memory"
        )
        rollback_second_layer = QgsVectorLayer(
            "Point?crs=EPSG:3857", "rollback_second_original", "memory"
        )
        rejecting_project = _RejectSecondOutputProject()
        try:
            GraphExecutionEngine._load_terminal_outputs(
                rollback_graph,
                {
                    "rollback_first": {"OUTPUT": rollback_first_layer},
                    "rollback_second": {"OUTPUT": rollback_second_layer},
                },
                declared_context,
                rejecting_project,
            )
        except ExecutionError:
            pass
        else:
            raise RuntimeError("A partial QGIS output commit was accepted.")
        if (
            rejecting_project.layers
            or rollback_first_layer.name() != "rollback_first_original"
            or rollback_second_layer.name() != "rollback_second_original"
        ):
            raise RuntimeError("A rejected output commit was not rolled back.")

        class _RejectRollbackProject(_RejectSecondOutputProject):
            def removeMapLayer(self, _layer_id):
                return False

        failed_rollback_project = _RejectRollbackProject()
        failed_rollback_ids = []
        try:
            GraphExecutionEngine._load_terminal_outputs(
                rollback_graph,
                {
                    "rollback_first": {"OUTPUT": rollback_first_layer},
                    "rollback_second": {"OUTPUT": rollback_second_layer},
                },
                declared_context,
                failed_rollback_project,
                committed_ids=failed_rollback_ids,
            )
        except ExecutionError as error:
            if "rollback was incomplete" not in str(error):
                raise RuntimeError(
                    "An incomplete output rollback was not reported."
                ) from error
        else:
            raise RuntimeError("An incomplete output rollback was accepted.")
        if failed_rollback_ids != [rollback_first_layer.id()]:
            raise RuntimeError(
                "An incomplete output rollback lost ownership of its remaining layer."
            )

        ledger_graph = GraphModel("Exact output ledger")
        ledger_node = AlgorithmCatalog.create_node(
            "native:buffer", "ledger_node", "Ledger output"
        )
        ledger_graph.add_node(ledger_node)
        ledger_graph.outputs_declared = True
        ledger_graph.outputs["LEDGER_RESULT"] = {
            "node_id": "ledger_node",
            "output_name": "OUTPUT",
            "mandatory": True,
        }
        ledger_layer = QgsVectorLayer(
            "Point?crs=EPSG:3857", "ledger_result", "memory"
        )
        unrelated_layer = QgsVectorLayer(
            "Point?crs=EPSG:3857", "unrelated_injected", "memory"
        )
        injected_layer_ids = []

        def _inject_unrelated_layer(layers):
            if (
                not injected_layer_ids
                and any(layer.id() == ledger_layer.id() for layer in layers)
            ):
                injected_layer_ids.append(unrelated_layer.id())
                project.addMapLayer(unrelated_layer)

        project.layersAdded.connect(_inject_unrelated_layer)
        exact_committed_ids = []
        try:
            GraphExecutionEngine._load_terminal_outputs(
                ledger_graph,
                {"ledger_node": {"OUTPUT": ledger_layer}},
                declared_context,
                project,
                committed_ids=exact_committed_ids,
            )
        finally:
            project.layersAdded.disconnect(_inject_unrelated_layer)
        if (
            exact_committed_ids != [ledger_layer.id()]
            or injected_layer_ids != [unrelated_layer.id()]
            or unrelated_layer.id() in exact_committed_ids
        ):
            raise RuntimeError("Output ownership used a whole-project layer diff.")
        project.removeMapLayer(ledger_layer.id())
        project.removeMapLayer(unrelated_layer.id())

        ownership_coordinator = RunCoordinator(lambda: None)
        ownership_context = QgsProcessingContext()
        ownership_layer = QgsVectorLayer(
            "Point?crs=EPSG:3857", "ownership", "memory"
        )
        ownership_cases = (
            ({}, ("OUTPUT",), "omitted"),
            ({"OUTPUT": 3}, ("OUTPUT",), "not a map layer"),
            (
                {"FIRST": ownership_layer, "SECOND": ownership_layer},
                ("FIRST", "SECOND"),
                "same layer",
            ),
        )
        for ownership_results, ownership_destinations, expected_message in ownership_cases:
            try:
                ownership_coordinator._take_result_layers(
                    ownership_results,
                    ownership_destinations,
                    ownership_context,
                )
            except RuntimeError as error:
                if expected_message not in str(error):
                    raise
            else:
                raise RuntimeError(
                    f"Agent result ownership accepted {expected_message}."
                )
        project.addMapLayer(ownership_layer)
        try:
            try:
                ownership_coordinator._take_result_layers(
                    {"OUTPUT": ownership_layer},
                    ("OUTPUT",),
                    ownership_context,
                )
            except RuntimeError as error:
                if "existing project layer" not in str(error):
                    raise
            else:
                raise RuntimeError(
                    "Agent result ownership accepted an existing project layer."
                )
        finally:
            project.removeMapLayer(ownership_layer.id())

        late_cancel_layer = QgsVectorLayer(
            "Point?crs=EPSG:3857", "late_cancel", "memory"
        )
        late_cancel_layer_id = late_cancel_layer.id()
        late_cancel_layer_name = late_cancel_layer.name()
        project.addMapLayer(late_cancel_layer)
        late_cancel_coordinator = RunCoordinator(lambda: None)
        late_cancel_events = []
        late_cancel_coordinator.run_canceled.connect(
            lambda: late_cancel_events.append("canceled")
        )
        late_cancel_ticket = late_cancel_coordinator._state.start(
            "late-cancel", "processing_run", "Late cancel"
        )
        late_cancel_coordinator.cancel()
        late_cancel_coordinator._finish_success(
            late_cancel_ticket,
            "processing_run",
            "Late cancel",
            [late_cancel_layer_id],
            [late_cancel_layer_name],
        )
        if (
            late_cancel_events != ["canceled"]
            or project.mapLayer(late_cancel_layer_id) is not None
        ):
            raise RuntimeError(
                "A cancel during the layer-add boundary became a success."
            )

        invalid_output_graph = GraphModel("Invalid output")
        invalid_number = AlgorithmCatalog.create_node(
            "smart:number", "invalid_number", "Invalid number"
        )
        invalid_output_graph.add_node(invalid_number)
        invalid_output_graph.outputs_declared = True
        invalid_output_graph.outputs["INVALID"] = {
            "node_id": "invalid_number",
            "output_name": "OUTPUT",
            "description": "",
            "mandatory": True,
            "default": None,
        }
        invalid_model, invalid_fatal, _invalid_issues = (
            Model3Serializer.build_native_model(invalid_output_graph)
        )
        if invalid_model is not None or "Processing layer" not in invalid_fatal:
            raise RuntimeError("Native export accepted a scalar public output.")

        packages = MicroPackageCatalog.available()
        if len(packages) != 15:
            raise RuntimeError("The shipped micro-package catalog is incomplete.")
        for package in packages:
            package_graph = MicroPackageCatalog.instantiate(
                package.package_id
            )
            if (
                len(package_graph.nodes) != package.node_count
                or not package_graph.outputs_declared
                or not package_graph.outputs
            ):
                raise RuntimeError(
                    f"Micro-package did not build its contract: {package.package_id}"
                )
            package_model, fatal, issues = Model3Serializer.build_native_model(
                package_graph
            )
            if package_model is None or fatal or issues:
                raise RuntimeError(
                    f"Micro-package is not a valid native model: "
                    f"{package.package_id}: {fatal or issues}"
                )

        properties = ModelPropertiesDialog(properties_graph)
        properties.name_edit.setText("Published smoke workflow")
        properties.explicit_outputs.setChecked(True)
        intermediate_row = None
        visible_sources = set()
        for row in range(properties.output_table.rowCount()):
            output_source = properties.output_table.item(row, 0).data(
                Qt.ItemDataRole.UserRole
            )
            visible_sources.add(output_source)
            if output_source == ("properties_buffer", "OUTPUT"):
                intermediate_row = row
        if (
            intermediate_row is None
            or ("properties_source", "OUTPUT") in visible_sources
            or ("properties_number", "OUTPUT") in visible_sources
        ):
            raise RuntimeError(
                "Model properties violated the Processing-layer output contract."
            )
        properties.output_table.item(intermediate_row, 0).setCheckState(
            Qt.CheckState.Checked
        )
        properties.output_table.item(intermediate_row, 1).setText(
            "PUBLIC_INTERMEDIATE"
        )
        properties.accept()
        if (
            properties.result_name != "Published smoke workflow"
            or not properties.result_outputs_declared
            or properties.result_outputs["PUBLIC_INTERMEDIATE"]["node_id"]
            != "properties_buffer"
        ):
            raise RuntimeError("Model properties did not collect output metadata.")
        properties_graph.name = properties.result_name
        properties_graph.description = properties.result_description
        properties_graph.outputs_declared = properties.result_outputs_declared
        properties_graph.outputs = properties.result_outputs
        properties_model, properties_fatal, properties_issues = (
            Model3Serializer.build_native_model(properties_graph)
        )
        if properties_model is None or properties_fatal or properties_issues:
            raise RuntimeError(
                "An intermediate public layer did not build as a native model."
            )
        child_outputs = properties_model.childAlgorithms()[
            "properties_buffer"
        ].modelOutputs()
        if "PUBLIC_INTERMEDIATE" not in child_outputs:
            raise RuntimeError("Native model omitted the intermediate output.")

        zero_properties = ModelPropertiesDialog(properties_graph)
        zero_properties.explicit_outputs.setChecked(True)
        for row in range(zero_properties.output_table.rowCount()):
            zero_properties.output_table.item(row, 0).setCheckState(
                Qt.CheckState.Unchecked
            )
        zero_properties.accept()
        if (
            not zero_properties.result_outputs_declared
            or zero_properties.result_outputs
        ):
            raise RuntimeError("Model properties could not declare zero outputs.")

        scene = CanvasScene(graph)
        for node in graph.nodes.values():
            scene.add_node_to_scene(node)
        for graph_edge in graph.edges.values():
            scene.add_connection_to_scene(graph_edge)
        view = CanvasView(scene)
        prompt = AiPromptWidget()
        prompt.set_workflow_available(True)
        palette = NodePaletteWidget()
        palette_activations = []
        palette.node_requested.connect(
            lambda algorithm_id, _title, _category: (
                palette_activations.append(algorithm_id)
            )
        )
        first_group = palette.tree.topLevelItem(0)
        first_algorithm = (
            first_group.child(0)
            if first_group is not None and first_group.childCount()
            else None
        )
        if first_algorithm is not None:
            palette.tree.itemActivated.emit(first_algorithm, 0)
        if not palette_activations:
            raise RuntimeError("Enter did not activate a palette algorithm.")
        settings_dialog = AiSettingsDialog()
        settings_dialog.reveal_button.setChecked(True)
        if (
            settings_dialog.reveal_button.text() != "Hide"
            or settings_dialog.key_edit.echoMode()
            != QLineEdit.EchoMode.Normal
        ):
            raise RuntimeError("The API key reveal control did not expose Hide.")
        canceled_tests = []
        settings_dialog.client.is_busy = lambda: True
        settings_dialog.client.cancel = lambda: canceled_tests.append(True)
        settings_dialog.reject()
        if canceled_tests != [True]:
            raise RuntimeError(
                "Closing AI settings did not cancel its active connection test."
            )
        help_dialog = HelpDialog()
        if not help_dialog.accessibleName():
            raise RuntimeError("The in-application help dialog is inaccessible.")
        help_dialog.close()
        translation = TranslationManager(
            str(plugin_root / "planx_smartmodeler")
        )
        if translation.install() != "en":
            raise RuntimeError("A missing locale catalog did not fall back to English.")
        translation.remove()
        translation.remove()
        icon_path = plugin_root / "planx_smartmodeler" / "icons" / "icon.png"
        icon = QIcon(str(icon_path))
        icon_image = QImage(str(icon_path))
        transparent_pixels = sum(
            1
            for y in range(icon_image.height())
            for x in range(icon_image.width())
            if icon_image.pixelColor(x, y).alpha() == 0
        )
        if (
            icon_image.size().width() != 64
            or icon_image.size().height() != 64
            or not icon_image.hasAlphaChannel()
            or transparent_pixels < 2_048
            or any(
                icon_image.pixelColor(x, y).alpha() != 0
                for x, y in ((0, 0), (63, 0), (0, 63), (63, 63))
            )
        ):
            raise RuntimeError(
                "Workflow Studio icon background is not transparently packaged."
            )
        if (
            not view.scene()
            or not prompt.isEnabled()
            or prompt.mode_combo.currentData() != "improve"
            or prompt.generate_button.text() != "Improve workflow"
            or palette.tree.topLevelItemCount() == 0
            or settings_dialog.key_status.text() == ""
            or icon.isNull()
        ):
            raise RuntimeError("Qt widgets did not initialize.")
        settings_dialog.close()

        from planx_smartmodeler.gui.modeler_window import SmartModelerWindow
        import tempfile as _document_tempfile

        no_thread_flag = None
        processing_flag_enum = getattr(Qgis, "ProcessingAlgorithmFlag", None)
        if processing_flag_enum is not None:
            no_thread_flag = getattr(
                processing_flag_enum, "NoThreading", None
            )
        if no_thread_flag is None:
            no_thread_flag = getattr(
                QgsProcessingAlgorithm, "FlagNoThreading", None
            )
        no_thread_graph = None
        if no_thread_flag is not None:
            for candidate in QgsApplication.processingRegistry().algorithms():
                if not bool(candidate.flags() & no_thread_flag):
                    continue
                try:
                    candidate_node = AlgorithmCatalog.create_node(
                        candidate.id(), "no_thread_fixture"
                    )
                except Exception:
                    continue
                no_thread_graph = GraphModel("No-threading guard")
                no_thread_graph.add_node(candidate_node)
                break
        if (
            no_thread_graph is None
            or not SmartModelerWindow._no_threading_algorithms(no_thread_graph)
        ):
            raise RuntimeError(
                "Studio did not identify a live NoThreading algorithm."
            )

        document_window = SmartModelerWindow(None)
        if (
            not document_window.view.accessibleName()
            or not document_window.palette_widget.search_bar.accessibleName()
            or not document_window.inspector_widget.outline.accessibleName()
            or document_window.undo_action.shortcutContext()
            != Qt.ShortcutContext.WidgetWithChildrenShortcut
            or document_window.redo_action.shortcutContext()
            != Qt.ShortcutContext.WidgetWithChildrenShortcut
        ):
            raise RuntimeError(
                "Studio accessibility names or text-safe shortcuts are missing."
            )
        if document_window.document_history.is_dirty:
            raise RuntimeError("A new Workflow Studio document started dirty.")
        document_window.add_node_by_alg("smart:number")
        if not document_window.document_history.is_dirty or not document_window.undo_action.isEnabled():
            raise RuntimeError("Adding a node did not mark the document dirty and undoable.")
        document_window.undo_document()
        if document_window.graph.nodes or not document_window.redo_action.isEnabled():
            raise RuntimeError("Document Undo did not restore the empty graph.")
        document_window.redo_document()
        if len(document_window.graph.nodes) != 1:
            raise RuntimeError("Document Redo did not restore the added node.")
        number_node = next(iter(document_window.graph.nodes.values()))
        if document_window.inspector_widget.outline.topLevelItemCount() != 1:
            raise RuntimeError("The accessible graph outline did not track nodes.")
        number_output = number_node.outputs["OUTPUT"]
        recommendations = SmartProposalEngine.get_proposals_for_port(
            number_output, number_node
        )
        if (
            not recommendations
            or recommendations[0].alg_id != "native:buffer"
            or recommendations[0].target_port_id != "DISTANCE"
        ):
            raise RuntimeError("Ranked proposal did not resolve the live target port.")
        document_window.apply_smart_proposal(recommendations[0])
        if (
            len(document_window.graph.nodes) != 2
            or len(document_window.graph.edges) != 1
            or next(iter(document_window.graph.edges.values())).end_port_id
            != "DISTANCE"
        ):
            raise RuntimeError("Smart proposal did not add and auto-connect its node.")
        document_window.undo_document()
        if len(document_window.graph.nodes) != 1:
            raise RuntimeError("Smart proposal was not one undoable document edit.")

        keyboard_graph = GraphModel("Keyboard connections")
        keyboard_number = AlgorithmCatalog.create_node(
            "smart:number", "keyboard_number", "Distance"
        )
        keyboard_buffer = AlgorithmCatalog.create_node(
            "native:buffer", "keyboard_buffer", "Buffer"
        )
        keyboard_graph.add_node(keyboard_number)
        keyboard_graph.add_node(keyboard_buffer)
        connection_dialog = ConnectionDialog(keyboard_graph)
        source_index = next(
            (
                index
                for index in range(connection_dialog.source_combo.count())
                if tuple(connection_dialog.source_combo.itemData(index))
                == ("keyboard_number", "OUTPUT")
            ),
            -1,
        )
        connection_dialog.source_combo.setCurrentIndex(source_index)
        target_index = next(
            (
                index
                for index in range(connection_dialog.target_combo.count())
                if tuple(connection_dialog.target_combo.itemData(index))
                == ("keyboard_buffer", "DISTANCE")
            ),
            -1,
        )
        connection_dialog.target_combo.setCurrentIndex(target_index)
        connection_dialog._accept_connection()
        if connection_dialog.connection != (
            "keyboard_number",
            "OUTPUT",
            "keyboard_buffer",
            "DISTANCE",
        ):
            raise RuntimeError(
                "The keyboard connection dialog did not select a valid edge: "
                f"source_index={source_index}, target_index={target_index}, "
                f"connection={connection_dialog.connection!r}."
            )
        with _document_tempfile.TemporaryDirectory() as document_tmp:
            document_path = Path(document_tmp) / "atomic.smartmodeler.json"
            if not document_window._save_to_path(
                document_path, "SmartModeler project (*.smartmodeler.json)"
            ):
                raise RuntimeError("Atomic SmartModeler document save failed.")
            if document_window.document_history.is_dirty or not document_path.is_file():
                raise RuntimeError("A successful save did not mark the document clean.")
            reopened = Model3Serializer.import_from_json(
                document_path.read_text(encoding="utf-8")
            )
            if reopened is None or len(reopened.nodes) != 1:
                raise RuntimeError("The atomically saved document could not be reopened.")
        document_window.add_node_by_alg("smart:number")
        document_window.prepare_for_shutdown()
        if not document_window.settings.value(
            document_window.RECOVERY_PREFIX + "snapshot", "", type=str
        ):
            raise RuntimeError("Forced plugin shutdown did not preserve dirty recovery state.")
        document_window._clear_recovery_snapshot()
        document_window.close()

        class _FakeIface:
            def mainWindow(self):
                return None

            def addPluginToMenu(self, _name, _action):
                pass

            def removePluginMenu(self, _name, _action):
                pass

            # Recorded, not ignored. A stub that accepted any toolbar call let
            # this plugin register on the Vector toolbar while every sibling and
            # the shared template used the Plugins toolbar -- and whether the
            # Vector toolbar is visible at all is per-profile UI state, so the
            # same build looked icon-less in one QGIS profile and fine in
            # another.
            def __init__(self):
                self.toolbar_added = []
                self.toolbar_removed = []

            def addToolBarIcon(self, action):
                self.toolbar_added.append(action)

            def removeToolBarIcon(self, action):
                self.toolbar_removed.append(action)

            def addDockWidget(self, _area, _dock):
                pass

            def removeDockWidget(self, _dock):
                pass

        fake_iface = _FakeIface()
        lifecycle_plugin = SmartModelerPlugin(fake_iface)
        if lifecycle_plugin._current_graph() is not None:
            raise RuntimeError(
                "Agent Workspace reported a model before the studio was ever constructed."
            )
        lifecycle_plugin.initGui()
        try:
            if len(fake_iface.toolbar_added) != 2:
                raise RuntimeError(
                    "Both plugin actions must reach the Plugins toolbar; "
                    f"got {len(fake_iface.toolbar_added)}."
                )
            if (
                lifecycle_plugin.agent_action is None
                or lifecycle_plugin.agent_action.icon().isNull()
                or lifecycle_plugin.agent_action.icon().pixmap(16, 16).isNull()
            ):
                raise RuntimeError(
                    "Agent Workspace did not load a usable small toolbar icon."
                )
            if lifecycle_plugin._current_graph() is not None:
                raise RuntimeError(
                    "Agent Workspace reported a model before Workflow Studio ever ran."
                )
            lifecycle_plugin.run()
            if lifecycle_plugin._current_graph() is None:
                raise RuntimeError(
                    "Agent Workspace did not report the model while the studio was visible."
                )
            lifecycle_plugin.window.hide()
            if lifecycle_plugin._current_graph() is not None:
                raise RuntimeError(
                    "Agent Workspace still reported a model after the studio was hidden."
                )
            if lifecycle_plugin.window is None:
                raise RuntimeError("Hiding the studio window destroyed it instead of hiding it.")
            lifecycle_plugin.window.show()
            lifecycle_plugin.window._on_token_usage(AiTokenUsage(100, 20, 125))
            if lifecycle_plugin.window.token_usage_label.text() != "Input 100 · Output 20":
                raise RuntimeError("Workflow Studio did not render provider token usage.")
            if lifecycle_plugin._current_graph() is None:
                raise RuntimeError(
                    "Agent Workspace did not report the model again after the studio was reopened."
                )
            original_profile = AiSettingsStore().active_profile()
            routed_profile = AiProfile.create(
                "openai", "Workflow agent route"
            )
            routed_store = AiSettingsStore()
            saved, _message = routed_store.save_profile(
                routed_profile, api_key="smoke-workflow-key"
            )
            if not saved:
                raise RuntimeError(
                    "Could not prepare the isolated Workflow Studio agent profile."
                )
            routed_store.set_active(routed_profile.profile_id)
            routed_requests = []
            lifecycle_plugin.agent_dock.ai_client.generate_structured = (
                lambda *args, **_kwargs: routed_requests.append(args)
            )
            lifecycle_plugin.window.add_node_by_alg("smart:number")
            lifecycle_plugin.window.generate_ai_graph(
                "Add a buffer step after the current node.", "improve"
            )
            if (
                not routed_requests
                or not lifecycle_plugin.agent_dock.run_loop.is_active()
                or lifecycle_plugin.agent_dock.run_loop.mode != AgentMode.ACT
                or lifecycle_plugin.agent_dock.run_loop.scope
                != AgentScope.CURRENT_MODEL
                or "Planning in Agent Workspace"
                not in lifecycle_plugin.window.status_label.text()
            ):
                raise RuntimeError(
                    "Workflow Studio online AI did not route through the "
                    "shared multi-turn Agent Workspace."
                )
            routed_prompt = lifecycle_plugin.agent_dock.run_loop._user_text
            if (
                "Preserve all unrelated nodes" not in routed_prompt
                or "Add a buffer step" not in routed_prompt
            ):
                raise RuntimeError(
                    "Workflow Studio lost its improve semantics while routing "
                    "to Agent Workspace."
                )
            lifecycle_plugin.agent_dock._on_stop_clicked()
            routed_store.set_active(original_profile.profile_id)
            agent_ticket = lifecycle_plugin.agent_dock.run_coordinator._state.start(
                "global-lock", "model_run", "Global lock"
            )
            lifecycle_plugin.window.run_model()
            if (
                agent_ticket is None
                or lifecycle_plugin.window._is_executing
                or "Agent Workspace" not in lifecycle_plugin.window.status_label.text()
            ):
                raise RuntimeError(
                    "Studio started while an Agent action owned the global run slot."
                )
            lifecycle_plugin.agent_dock.run_coordinator._state.finish(
                agent_ticket, "failed"
            )
            active_unload_graph = GraphModel("Active unload")
            active_unload_first = AlgorithmCatalog.create_node(
                "smart:number", "active_unload_first", "First"
            )
            active_unload_second = AlgorithmCatalog.create_node(
                "smart:number", "active_unload_second", "Second"
            )
            active_unload_second.dependencies = ["active_unload_first"]
            active_unload_graph.add_node(active_unload_first)
            active_unload_graph.add_node(active_unload_second)
            active_window = lifecycle_plugin.window
            active_window._set_graph(active_unload_graph)
            active_unload_events = []
            active_ui_lock_checks = []

            def _unload_during_run(_value, _message):
                if not active_unload_events:
                    active_ui_lock_checks.append(
                        not active_window.centralWidget().isEnabled()
                        and active_window.cancel_run_action.isEnabled()
                        and lifecycle_plugin.agent_dock._external_run_active()
                        and all(
                            not action.isEnabled()
                            for action in active_window.findChildren(QAction)
                            if action is not active_window.cancel_run_action
                        )
                    )
                    active_unload_events.append("unload")
                    lifecycle_plugin.unload()

            active_window.execution_engine.progress_changed.connect(
                _unload_during_run
            )
            active_unload_project_ids = set(project.mapLayers())
            active_window.run_model()
            active_unload_deadline = time.monotonic() + 10.0
            while (
                active_window._is_executing
                and time.monotonic() < active_unload_deadline
            ):
                QApplication.processEvents()
                time.sleep(0.01)
            if (
                active_unload_events != ["unload"]
                or active_ui_lock_checks != [True]
                or lifecycle_plugin.window is not None
                or active_window.execution_engine.is_running()
                or set(project.mapLayers()) != active_unload_project_ids
            ):
                raise RuntimeError("Active plugin unload was not terminal and clean.")
            active_window._clear_recovery_snapshot()
        finally:
            lifecycle_plugin.unload()
        if lifecycle_plugin._current_graph() is not None:
            raise RuntimeError("Agent Workspace reported a model after unload().")
        # Every icon added must be taken back, or an uninstall leaves a dead
        # button on the user's toolbar.
        if len(fake_iface.toolbar_removed) != len(fake_iface.toolbar_added):
            raise RuntimeError(
                f"unload() removed {len(fake_iface.toolbar_removed)} toolbar icons "
                f"but initGui() added {len(fake_iface.toolbar_added)}."
            )

        expected_agent_tools = {
            "project.summary",
            "layer.list",
            "layer.describe",
            "processing.search",
            "processing.describe",
            "processing.resolve",
            "expression.search",
            "model.summary",
            "model.validate",
            "plugin.list",
            "layer.style",
            "model.describe",
            "plugin.describe",
            "plugin.capabilities",
            "database.list",
            "database.describe",
            "script.list",
            "script.describe",
            "workspace.list",
            "workspace.read",
            "workspace.inspect",
            "workspace.search",
            "workspace.command",
        }
        empty_dock = AgentWorkspaceDock(None, lambda: None)
        registry_tool_names = {spec.name for spec in empty_dock.registry.list_specs()}
        if registry_tool_names != expected_agent_tools:
            raise RuntimeError(
                "The Agent Workspace registry does not contain the expected tools."
            )
        if empty_dock.scope_combo.count() != 5 or empty_dock.mode_combo.count() != 3:
            raise RuntimeError("Agent Workspace dock did not construct its selectors under Qt 6.")
        if empty_dock.mode_combo.itemData(2) != AgentMode.ACT or empty_dock.mode_combo.itemText(
            2
        ) != "Act (approve to apply)":
            raise RuntimeError("The Act option is not presented honestly (approve to apply).")
        from qgis.PyQt.QtWidgets import QPushButton

        # Phase 04 adds exactly one explicit-approval Apply plus Reject and a
        # single-level Undo. Any auto/bulk/execution action remains forbidden.
        # Phase 05 relabels that one button to "Run" for a validated run
        # proposal, so a *fresh* dock must still expose no Run control at all --
        # which is exactly what this asserts.
        forbidden_actions = (
            "accept", "approve", "execute", "run", "commit", "export", "save",
            "approve all", "apply all",
        )
        for button in empty_dock.findChildren(QPushButton):
            label = button.text().strip().lower()
            if any(word == label or label.startswith(word + " ") for word in forbidden_actions):
                raise RuntimeError(f"Agent Workspace exposed a forbidden action button: {button.text()!r}")
        # A fresh dock has no pending action: Apply/Reject are disabled and Apply
        # is never the default button; the approval card is hidden.
        if empty_dock.apply_button.isEnabled() or empty_dock.reject_button.isEnabled():
            raise RuntimeError("Apply/Reject were enabled with no pending action.")
        if empty_dock.apply_button.isDefault() or empty_dock.apply_button.autoDefault():
            raise RuntimeError("Apply must never be the default button.")
        if empty_dock.approval_group.isVisible() or empty_dock.undo_button.isEnabled():
            raise RuntimeError("The approval card/Undo were active with no pending action.")
        if empty_dock.proposal_view is None or empty_dock.proposal_group is None:
            raise RuntimeError("Agent Workspace did not construct its read-only proposal preview.")

        no_model_controller = AgentController(empty_dock.registry)
        no_model_result = no_model_controller.execute(
            AgentToolCall(call_id="smoke_model_missing", tool_name="model.summary"),
            AgentMode.ASK,
            AgentScope.CURRENT_MODEL,
        )
        if (
            no_model_result.status != AgentResultStatus.SUCCESS
            or no_model_result.data.get("available") is not False
        ):
            raise RuntimeError("model.summary did not report an absent model provider correctly.")

        live_registry = build_default_registry(lambda: graph)
        live_controller = AgentController(live_registry)
        live_model_result = live_controller.execute(
            AgentToolCall(call_id="smoke_model_present", tool_name="model.summary"),
            AgentMode.ASK,
            AgentScope.CURRENT_MODEL,
        )
        if (
            live_model_result.status != AgentResultStatus.SUCCESS
            or live_model_result.data.get("available") is not True
            or live_model_result.data.get("node_count") != 2
        ):
            raise RuntimeError("model.summary did not describe the open SmartModeler graph.")

        project_result = live_controller.execute(
            AgentToolCall(call_id="smoke_project_summary", tool_name="project.summary"),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        if project_result.status != AgentResultStatus.SUCCESS or "title" not in project_result.data:
            raise RuntimeError("project.summary failed against the smoke project.")

        layer_list_result = live_controller.execute(
            AgentToolCall(call_id="smoke_layer_list", tool_name="layer.list"),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        if layer_list_result.status != AgentResultStatus.SUCCESS or not layer_list_result.data["layers"]:
            raise RuntimeError("layer.list did not return the smoke project layer.")
        layer_entry_text = str(layer_list_result.data)
        if "memory?" in layer_entry_text or "Point?crs=" in layer_entry_text:
            raise RuntimeError("layer.list leaked a source URI into the agent context.")

        layer_describe_result = live_controller.execute(
            AgentToolCall(
                call_id="smoke_layer_describe",
                tool_name="layer.describe",
                arguments={"layer_id": input_layer.id()},
            ),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        if (
            layer_describe_result.status != AgentResultStatus.SUCCESS
            or layer_describe_result.data.get("available") is not True
        ):
            raise RuntimeError("layer.describe failed against the smoke layer.")
        describe_text = str(layer_describe_result.data)
        if "memory?" in describe_text or "POINT(" in describe_text.upper():
            raise RuntimeError("layer.describe leaked a source URI or feature value.")
        if layer_describe_result.data.get("feature_count") != 1:
            raise RuntimeError("layer.describe did not report the layer's feature count.")

        # Attribute values must never become provider-visible tool results.
        # Keep a layer with sensitive-looking values in the project and prove
        # that the registry exposes no value-inspection tool for it.
        tagged_layer = QgsVectorLayer("Point?crs=EPSG:3857", "smoke_highways", "memory")
        tagged_layer.dataProvider().addAttributes(
            [QgsField("highway", QMetaType.Type.QString)]
        )
        tagged_layer.updateFields()
        tagged_features = []
        for tag in ("bus_stop", "bus_stop", "bus_stop", "crossing", None):
            tagged_feature = QgsFeature(tagged_layer.fields())
            tagged_feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(1, 1)))
            tagged_feature.setAttribute("highway", tag)
            tagged_features.append(tagged_feature)
        tagged_layer.dataProvider().addFeatures(tagged_features)
        tagged_layer.updateExtents()
        project.addMapLayer(tagged_layer)
        if "layer.field_values" in registry_tool_names:
            raise RuntimeError("Attribute-value inspection reached the provider tool registry.")

        missing_layer_id_result = live_controller.execute(
            AgentToolCall(call_id="smoke_layer_describe_missing", tool_name="layer.describe"),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        if (
            missing_layer_id_result.status != AgentResultStatus.FAILED
            or missing_layer_id_result.reason_code != "invalid_arguments"
        ):
            raise RuntimeError(
                "layer.describe did not fail closed on a missing required argument."
            )
        oversized_query_result = live_controller.execute(
            AgentToolCall(
                call_id="smoke_processing_search_oversized",
                tool_name="processing.search",
                arguments={"query": "buffer" * 100},
            ),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        if (
            oversized_query_result.status != AgentResultStatus.FAILED
            or oversized_query_result.reason_code != "invalid_arguments"
        ):
            raise RuntimeError(
                "processing.search did not fail closed on an over-length query argument."
            )

        available_names = set(getattr(qgis_utils_probe, "available_plugins", []) or [])
        active_names = set(getattr(qgis_utils_probe, "active_plugins", []) or [])
        plugin_list_result = live_controller.execute(
            AgentToolCall(
                call_id="smoke_plugin_list", tool_name="plugin.list", arguments={"limit": 100}
            ),
            AgentMode.ASK,
            AgentScope.PLUGINS,
        )
        if plugin_list_result.status != AgentResultStatus.SUCCESS:
            raise RuntimeError("plugin.list failed in the smoke environment.")
        listed_plugins = {item["package_name"]: item for item in plugin_list_result.data["plugins"]}
        if not plugin_list_result.data["truncated"]:
            missing_available = available_names - set(listed_plugins)
            if missing_available:
                raise RuntimeError(
                    "plugin.list omitted available plugin package(s): "
                    f"{sorted(missing_available)[:5]}"
                )
        inactive_available = available_names - active_names
        reported_inactive = [
            name
            for name in inactive_available
            if name in listed_plugins and listed_plugins[name]["enabled"] is False
        ]
        if inactive_available and not reported_inactive:
            raise RuntimeError(
                "plugin.list did not report any available-but-inactive plugin as enabled: false."
            )

        # -- Phase 03: rich read-only tools + inert validated proposals ----
        import json as _json

        from qgis.core import (
            QgsCategorizedSymbolRenderer,
            QgsPalLayerSettings,
            QgsRendererCategory,
            QgsSymbol,
            QgsVectorLayerSimpleLabeling,
        )

        from planx_smartmodeler.core.agent.run_loop import RunEventKind

        def _agent_turn(action, assistant_text="", tool_calls=None, kind="none", proposal_json=""):
            return _json.dumps(
                {
                    "action": action,
                    "assistant_text": assistant_text,
                    "tool_calls": tool_calls or [],
                    "proposal_kind": kind,
                    "proposal_json": proposal_json,
                }
            )

        style_layer = QgsVectorLayer(
            "Polygon?crs=EPSG:3857&field=name:string&field=pop:integer", "style_probe", "memory"
        )
        symbol = QgsSymbol.defaultSymbol(style_layer.geometryType())
        category = QgsRendererCategory(1, symbol, "SENTINEL_CATEGORY_LABEL")
        style_layer.setRenderer(QgsCategorizedSymbolRenderer("pop", [category]))
        label_settings = QgsPalLayerSettings()
        label_settings.fieldName = "concat('SENTINEL_LABEL_EXPRESSION', \"name\")"
        label_settings.isExpression = True
        style_layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
        style_layer.setLabelsEnabled(True)
        project.addMapLayer(style_layer)

        proposal_dock = AgentWorkspaceDock(fake_iface, lambda: graph)

        style_result = proposal_dock.controller.execute(
            AgentToolCall(
                call_id="smoke_layer_style",
                tool_name="layer.style",
                arguments={"layer_id": style_layer.id()},
            ),
            AgentMode.PLAN,
            AgentScope.PROJECT,
        )
        if style_result.status != AgentResultStatus.SUCCESS or not style_result.data.get("context_token"):
            raise RuntimeError("layer.style did not return a bounded context token.")
        style_text = str(style_result.data)
        for leaked in ("SENTINEL_CATEGORY_LABEL", "SENTINEL_LABEL_EXPRESSION", "Polygon?crs=", "memory?"):
            if leaked in style_text:
                raise RuntimeError(f"layer.style leaked forbidden content: {leaked}")
        if style_result.data.get("classification_field") != "pop":
            raise RuntimeError("layer.style did not detect the real classification field.")
        if style_result.data.get("label_expression_present") is not True:
            raise RuntimeError("layer.style did not flag a label expression without exposing it.")

        model_describe_result = proposal_dock.controller.execute(
            AgentToolCall(call_id="smoke_model_describe", tool_name="model.describe"),
            AgentMode.PLAN,
            AgentScope.CURRENT_MODEL,
        )
        if model_describe_result.status != AgentResultStatus.SUCCESS or not model_describe_result.data.get(
            "context_token"
        ):
            raise RuntimeError("model.describe did not return topology plus a context token.")
        describe_text = str(model_describe_result.data)
        if input_layer.id() in describe_text or "10.0" in describe_text:
            raise RuntimeError("model.describe leaked a baseline parameter/path value.")
        model_token = model_describe_result.data["context_token"]

        plugin_describe_result = proposal_dock.controller.execute(
            AgentToolCall(
                call_id="smoke_plugin_describe",
                tool_name="plugin.describe",
                arguments={"package_name": "planx_smartmodeler"},
            ),
            AgentMode.ASK,
            AgentScope.PLUGINS,
        )
        if plugin_describe_result.status != AgentResultStatus.SUCCESS:
            raise RuntimeError("plugin.describe failed for the SmartModeler package.")

        model_serialization_before = Model3Serializer.export_to_json(graph)
        valid_patch = _json.dumps(
            {
                "schema_version": 1,
                "context_token": model_token,
                "title": "Rename the model",
                "summary": "Give the workflow a clearer name.",
                "operations": [
                    {"op": "set_model_metadata", "name": "Renamed by proposal", "description": "d"}
                ],
                "warnings": [],
            }
        )

        def _feed_proposal(mode, scope, kind, proposal_json):
            start = proposal_dock.run_loop.start("propose", mode, scope)
            return proposal_dock.run_loop.submit_provider_response(
                start.request.request_token,
                _agent_turn("proposal", "Here.", kind=kind, proposal_json=proposal_json),
            )

        proposal_event = _feed_proposal(
            AgentMode.PLAN, AgentScope.CURRENT_MODEL, "model_patch", valid_patch
        )
        if proposal_event is None or proposal_event.kind != RunEventKind.PROPOSAL:
            raise RuntimeError("A valid model proposal did not reach a PROPOSAL event in Plan mode.")
        if Model3Serializer.export_to_json(graph) != model_serialization_before:
            raise RuntimeError("Validating a model proposal changed the live graph.")
        if graph.name == "Renamed by proposal":
            raise RuntimeError("A proposal was applied to the live graph.")

        # A stale proposal (graph changed after the token was issued) must reject.
        graph.nodes["buffer"].title = "Buffer (touched)"
        model_serialization_after_touch = Model3Serializer.export_to_json(graph)
        stale_event = _feed_proposal(
            AgentMode.PLAN, AgentScope.CURRENT_MODEL, "model_patch", valid_patch
        )
        if (
            stale_event is None
            or stale_event.kind != RunEventKind.FAILED
            or stale_event.reason_code != "stale_proposal_context"
        ):
            raise RuntimeError("A stale model proposal was not rejected after a graph change.")

        # A valid style proposal must render without changing renderer/labels/opacity/dirty.
        style_token = style_result.data["context_token"]
        renderer_before = style_layer.renderer()
        opacity_before = style_layer.opacity()
        labels_before = style_layer.labelsEnabled()
        dirty_before = project.isDirty()
        style_proposal = _json.dumps(
            {
                "schema_version": 1,
                "context_token": style_token,
                "target_layer_id": style_layer.id(),
                "title": "Keep current style",
                "summary": "No structural change, just confirm.",
                "renderer": {"family": "keep", "field": "", "class_count": 0, "palette": [], "opacity": 1.0},
                "labels": {"enabled": False, "field": ""},
                "warnings": [],
            }
        )
        style_event = _feed_proposal(
            AgentMode.PLAN, AgentScope.PROJECT, "layer_style", style_proposal
        )
        if style_event is None or style_event.kind != RunEventKind.PROPOSAL:
            raise RuntimeError("A valid style proposal did not reach a PROPOSAL event.")
        if (
            style_layer.renderer() is not renderer_before
            or style_layer.opacity() != opacity_before
            or style_layer.labelsEnabled() != labels_before
            or project.isDirty() != dirty_before
        ):
            raise RuntimeError("A style proposal changed live renderer/labels/opacity/project state.")

        # An Ask-mode proposal must be rejected before any live validation.
        ask_event = _feed_proposal(
            AgentMode.ASK, AgentScope.CURRENT_MODEL, "model_patch", valid_patch
        )
        if (
            ask_event is None
            or ask_event.kind != RunEventKind.FAILED
            or ask_event.reason_code != "proposal_not_allowed_in_ask"
        ):
            raise RuntimeError("An Ask-mode proposal was not rejected.")

        # A style proposal in ACTIVE_LAYER scope must reject when the fake iface
        # has no matching active layer (target mismatch, before any mutation).
        active_mismatch = _feed_proposal(
            AgentMode.PLAN, AgentScope.ACTIVE_LAYER, "layer_style", style_proposal
        )
        if (
            active_mismatch is None
            or active_mismatch.kind != RunEventKind.FAILED
            or active_mismatch.reason_code != "proposal_target_missing"
        ):
            raise RuntimeError("An ACTIVE_LAYER style target mismatch was not rejected.")

        # An old style token must be rejected after a represented style change.
        style_layer.setOpacity(0.42)
        stale_style = _feed_proposal(
            AgentMode.PLAN, AgentScope.PROJECT, "layer_style", style_proposal
        )
        if (
            stale_style is None
            or stale_style.kind != RunEventKind.FAILED
            or stale_style.reason_code != "stale_proposal_context"
        ):
            raise RuntimeError("A stale style proposal was not rejected after a style change.")
        style_layer.setOpacity(opacity_before)

        # Defense-in-depth: the runtime validator itself rejects an invalid mode
        # and an incompatible scope, independently of the run loop.
        from planx_smartmodeler.core.agent.proposals import parse_proposal as _parse_proposal

        parsed_patch = _parse_proposal("model_patch", valid_patch)
        bad_mode = proposal_dock._proposal_validator.validate(
            "model_patch", parsed_patch, "invalid_mode", AgentScope.CURRENT_MODEL
        )
        bad_scope = proposal_dock._proposal_validator.validate(
            "model_patch", parsed_patch, AgentMode.PLAN, AgentScope.PROJECT
        )
        if bad_mode.ok or bad_scope.ok or bad_scope.reason_code != "proposal_scope_mismatch":
            raise RuntimeError("The runtime validator did not fail closed on mode/scope.")

        # A deterministic in-memory raster fixture: a raster layer.style summary
        # plus a compatible inert raster_gray proposal.
        import tempfile as _tempfile

        from osgeo import gdal as _gdal
        from osgeo import osr as _osr

        raster_tmp = _tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        raster_tmp.close()
        raster_id = None
        try:
            driver = _gdal.GetDriverByName("GTiff")
            dataset = driver.Create(raster_tmp.name, 2, 2, 1, _gdal.GDT_Byte)
            dataset.SetGeoTransform((0.0, 1.0, 0.0, 2.0, 0.0, -1.0))
            srs = _osr.SpatialReference()
            srs.ImportFromEPSG(3857)
            dataset.SetProjection(srs.ExportToWkt())
            dataset.GetRasterBand(1).Fill(1)
            dataset = None
            from qgis.core import QgsRasterLayer

            raster_layer = QgsRasterLayer(raster_tmp.name, "raster_probe", "gdal")
            if not raster_layer.isValid():
                raise RuntimeError("The in-memory raster fixture was not valid.")
            project.addMapLayer(raster_layer)
            raster_id = raster_layer.id()
            raster_style = proposal_dock.controller.execute(
                AgentToolCall(
                    call_id="smoke_raster_style",
                    tool_name="layer.style",
                    arguments={"layer_id": raster_id},
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            if (
                raster_style.status != AgentResultStatus.SUCCESS
                or raster_style.data.get("kind") != "raster"
                or not raster_style.data.get("context_token")
            ):
                raise RuntimeError("layer.style did not summarize the raster fixture.")
            if raster_tmp.name in str(raster_style.data):
                raise RuntimeError("layer.style leaked the raster source path.")
            raster_renderer_before = raster_layer.renderer()
            raster_proposal = _json.dumps(
                {
                    "schema_version": 1,
                    "context_token": raster_style.data["context_token"],
                    "target_layer_id": raster_id,
                    "title": "Grayscale raster",
                    "summary": "Render the raster as single-band gray.",
                    "renderer": {
                        "family": "raster_gray",
                        "field": "",
                        "class_count": 0,
                        "palette": [],
                        "opacity": 1.0,
                    },
                    "labels": {"enabled": False, "field": ""},
                    "warnings": [],
                }
            )
            raster_event = _feed_proposal(
                AgentMode.PLAN, AgentScope.PROJECT, "layer_style", raster_proposal
            )
            if raster_event is None or raster_event.kind != RunEventKind.PROPOSAL:
                raise RuntimeError("A valid raster style proposal did not reach a PROPOSAL event.")
            if raster_layer.renderer() is not raster_renderer_before:
                raise RuntimeError("A raster style proposal changed the live renderer.")
        finally:
            if raster_id is not None:
                project.removeMapLayer(raster_id)
            os.unlink(raster_tmp.name)

        # After every rejection/validation path the live graph is unchanged.
        if Model3Serializer.export_to_json(graph) != model_serialization_after_touch:
            raise RuntimeError("A rejected proposal path changed the live graph.")

        # -- Phase 04: explicit approval, atomic apply and safe Undo ---------
        class _SmokeModelAdapter:
            def __init__(self, model):
                self._graph = model
                self.fail = False

            def current_graph(self):
                return self._graph

            def install_graph(self, model):
                if self.fail:
                    raise RuntimeError("injected install failure")
                self._graph = model

        apply_graph = Model3Serializer.import_from_json(Model3Serializer.export_to_json(graph))
        adapter = _SmokeModelAdapter(apply_graph)
        apply_dock = AgentWorkspaceDock(fake_iface, adapter.current_graph, model_apply=adapter)

        def _feed_act(dock, scope, kind, proposal_json):
            start = dock.run_loop.start("propose", AgentMode.ACT, scope)
            event = dock.run_loop.submit_provider_response(
                start.request.request_token,
                _agent_turn("proposal", "Here.", kind=kind, proposal_json=proposal_json),
            )
            dock._handle_run_event(event)
            return event

        def _model_token(dock):
            md = dock.controller.execute(
                AgentToolCall(call_id="p4_md", tool_name="model.describe"),
                AgentMode.PLAN, AgentScope.CURRENT_MODEL,
            )
            return md.data["context_token"]

        def _rename_patch(token, name):
            return _json.dumps({
                "schema_version": 1, "context_token": token, "title": "Rename model",
                "summary": "Give the workflow a clearer name.",
                "operations": [{"op": "set_model_metadata", "name": name, "description": "d"}],
                "warnings": [],
            })

        # No Apply/Accept/Run widget exists on the read-only preview and the Apply
        # button is never the default/auto-focused control.
        for forbidden in ("accept_button", "run_button", "commit_button", "approve_all_button"):
            if hasattr(apply_dock, forbidden):
                raise RuntimeError(f"An unexpected {forbidden} exists on the dock.")
        if apply_dock.apply_button.isDefault() or apply_dock.apply_button.autoDefault():
            raise RuntimeError("Apply must never be the default button.")

        # Ask/Plan create no pending action.
        plan_start = apply_dock.run_loop.start("q", AgentMode.PLAN, AgentScope.CURRENT_MODEL)
        plan_event = apply_dock.run_loop.submit_provider_response(
            plan_start.request.request_token,
            _agent_turn("proposal", "Here.", kind="model_patch",
                        proposal_json=_rename_patch(_model_token(apply_dock), "Plan only")),
        )
        apply_dock._handle_run_event(plan_event)
        if apply_dock._pending_action is not None:
            raise RuntimeError("A Plan-mode proposal created a pending action.")

        # Act model proposal creates a pending action; nothing changes before Apply.
        name_before = adapter.current_graph().name
        _feed_act(apply_dock, AgentScope.CURRENT_MODEL, "model_patch",
                  _rename_patch(_model_token(apply_dock), "Applied by agent"))
        if apply_dock._pending_action is None:
            raise RuntimeError("An Act model proposal did not create a pending action.")
        if not apply_dock.apply_button.isEnabled():
            raise RuntimeError("The approval card Apply button was not enabled.")
        if adapter.current_graph().name != name_before:
            raise RuntimeError("A pending model action mutated the graph before Apply.")

        # Explicit Apply mutates atomically and enables Undo.
        model_pre = Model3Serializer.export_to_json(adapter.current_graph())
        apply_dock._on_apply_clicked()
        if adapter.current_graph().name != "Applied by agent":
            raise RuntimeError("Apply did not change the live model.")
        if apply_dock._pending_action is not None or apply_dock.apply_button.isEnabled():
            raise RuntimeError("The pending action was not cleared after Apply.")
        if apply_dock._last_applied is None or not apply_dock.undo_button.isEnabled():
            raise RuntimeError("Undo was not enabled after a successful model apply.")
        # A second click does nothing (one-shot).
        apply_dock._on_apply_clicked()
        if adapter.current_graph().name != "Applied by agent":
            raise RuntimeError("A repeated Apply click mutated the model again.")

        # A Studio run owns the global mutation slot, including Agent Undo.
        applied_before_lock = apply_dock._last_applied
        apply_dock._external_run_active = lambda: True
        apply_dock._refresh_undo_button()
        if apply_dock.undo_button.isEnabled():
            raise RuntimeError("Agent Undo stayed enabled during a Studio run.")
        apply_dock._on_undo_clicked()
        if (
            apply_dock._last_applied is not applied_before_lock
            or adapter.current_graph().name != "Applied by agent"
        ):
            raise RuntimeError("Agent Undo mutated the model during a Studio run.")
        apply_dock._external_run_active = lambda: False
        apply_dock._refresh_undo_button()
        if not apply_dock.undo_button.isEnabled():
            raise RuntimeError("Agent Undo did not recover after the Studio run.")

        # Undo restores the exact prior model.
        apply_dock._on_undo_clicked()
        if Model3Serializer.export_to_json(adapter.current_graph()) != model_pre:
            raise RuntimeError("Undo did not restore the prior model.")

        # Atomic rollback: an injected install failure leaves the model unchanged.
        adapter.fail = True
        name_pre_fail = adapter.current_graph().name
        _feed_act(apply_dock, AgentScope.CURRENT_MODEL, "model_patch",
                  _rename_patch(_model_token(apply_dock), "Should not stick"))
        apply_dock._on_apply_clicked()
        if adapter.current_graph().name != name_pre_fail:
            raise RuntimeError("A failed model apply changed the live model.")
        adapter.fail = False

        # A mode/scope change fails a pending action closed.
        _feed_act(apply_dock, AgentScope.CURRENT_MODEL, "model_patch",
                  _rename_patch(_model_token(apply_dock), "Superseded"))
        if apply_dock._pending_action is None:
            raise RuntimeError("Expected a pending action before the mode change.")
        apply_dock._on_mode_or_scope_changed()
        if apply_dock._pending_action is not None or apply_dock.apply_button.isEnabled():
            raise RuntimeError("A mode/scope change did not clear the pending action.")

        # Layer-style Act apply + Undo on the real vector layer.
        style_tok = apply_dock.controller.execute(
            AgentToolCall(call_id="p4_style", tool_name="layer.style",
                          arguments={"layer_id": style_layer.id()}),
            AgentMode.PLAN, AgentScope.PROJECT,
        ).data["context_token"]
        renderer_type_before = style_layer.renderer().type()
        single_symbol = _json.dumps({
            "schema_version": 1, "context_token": style_tok,
            "target_layer_id": style_layer.id(), "title": "Single symbol",
            "summary": "One symbol for the whole layer.",
            "renderer": {"family": "single_symbol", "field": "", "class_count": 1,
                         "palette": ["#3366CC"], "opacity": 1.0},
            "labels": {"enabled": False, "field": ""}, "warnings": [],
        })
        _feed_act(apply_dock, AgentScope.PROJECT, "layer_style", single_symbol)
        if apply_dock._pending_action is None:
            raise RuntimeError("An Act style proposal did not create a pending action.")
        apply_dock._on_apply_clicked()
        if style_layer.renderer().type() != "singleSymbol":
            raise RuntimeError("Apply did not install the single-symbol renderer.")
        if not apply_dock.undo_button.isEnabled():
            raise RuntimeError("Undo was not enabled after a style apply.")
        symbol = style_layer.renderer().symbol()
        original_color = symbol.color()
        symbol.setColor(QColor("#AA3311"))
        if apply_dock._apply_coordinator.can_undo(apply_dock._last_applied):
            raise RuntimeError("A later symbol edit did not invalidate style Undo.")
        symbol.setColor(original_color)
        apply_dock._on_undo_clicked()
        if style_layer.renderer().type() != renderer_type_before:
            raise RuntimeError("Undo did not restore the prior renderer.")

        # The ledger records outcomes and leaks no raw values.
        ledger_text = "\n".join(str(e.to_dict()) for e in apply_dock.action_ledger.entries())
        for leaked in ("SENTINEL_CATEGORY_LABEL", "SENTINEL_LABEL_EXPRESSION", "#3366CC"):
            if leaked in ledger_text:
                raise RuntimeError(f"The action ledger leaked forbidden content: {leaked}")
        if not any(e.status == "applied" for e in apply_dock.action_ledger.entries()):
            raise RuntimeError("The ledger did not record an applied action.")

        # Shutdown clears all pending/applied/ledger state.
        apply_dock.shutdown()
        if (apply_dock._pending_action is not None or apply_dock._last_applied is not None
                or apply_dock.action_ledger.entries()):
            raise RuntimeError("shutdown did not clear Phase 04 action state.")

        # -- Phase 05: approved safe Processing / current-model execution ----
        run_graph = Model3Serializer.import_from_json(Model3Serializer.export_to_json(graph))
        run_adapter = _SmokeModelAdapter(run_graph)
        run_dock = AgentWorkspaceDock(fake_iface, run_adapter.current_graph, model_apply=run_adapter)

        def _alg_token(algorithm_id, scope=AgentScope.PROJECT):
            described = run_dock.controller.execute(
                AgentToolCall(call_id="p5_describe", tool_name="processing.describe",
                              arguments={"algorithm_id": algorithm_id}),
                AgentMode.PLAN, scope,
            )
            if described.status != AgentResultStatus.SUCCESS:
                raise RuntimeError(f"processing.describe failed for {algorithm_id}.")
            token = described.data.get("context_token")
            if not token:
                raise RuntimeError("processing.describe issued no run freshness receipt.")
            return token

        # -- Owner-QA follow-up: describe tells the model which params bind ---
        # The reproject run failed with "This parameter cannot be set by a
        # proposal" because the provider tried to set an unbindable parameter.
        # processing.describe now marks each parameter's proposal_binding so the
        # provider only sets the ones it may.
        reproject_desc = run_dock.controller.execute(
            AgentToolCall(call_id="p5_binding", tool_name="processing.describe",
                          arguments={"algorithm_id": "native:reprojectlayer"}),
            AgentMode.PLAN, AgentScope.PROJECT,
        )
        binding_by_name = {
            p["name"]: p["proposal_binding"] for p in reproject_desc.data["parameters"]
        }
        if binding_by_name.get("INPUT") != "layer" or binding_by_name.get("TARGET_CRS") != "crs":
            raise RuntimeError("reprojectlayer's bindable parameters were not advertised.")
        if binding_by_name.get("OUTPUT"):
            raise RuntimeError("A destination parameter was wrongly marked bindable.")
        if any(
            p["proposal_binding"] and p["name"] not in ("INPUT", "TARGET_CRS")
            for p in reproject_desc.data["parameters"]
        ):
            raise RuntimeError("An unbindable reproject parameter was advertised as bindable.")

        def _run_json(token, algorithm_id, inputs, title="Agent run"):
            return _json.dumps({
                "schema_version": 1, "context_token": token, "algorithm_id": algorithm_id,
                "title": title, "summary": "Run a reviewed algorithm on a project layer.",
                "inputs": inputs, "warnings": [],
            })

        def _model_run_json(token):
            return _json.dumps({
                "schema_version": 1, "context_token": token, "title": "Run the workflow",
                "summary": "Run the current workflow and add its outputs.", "warnings": [],
            })

        # A processing_run proposal is inert until an explicit Run click.
        layers_before = set(project.mapLayers())
        buffer_run = _run_json(
            _alg_token("native:buffer"), "native:buffer",
            {"INPUT": {"layer": input_layer.id()}, "DISTANCE": {"distance": 5}},
        )
        _feed_act(run_dock, AgentScope.PROJECT, "processing_run", buffer_run)
        if run_dock._pending_action is None:
            raise RuntimeError("An Act processing_run proposal created no pending action.")
        if run_dock.apply_button.text() != "Run":
            raise RuntimeError("The run approval card did not offer a Run action.")
        if run_dock.apply_button.isDefault() or run_dock.apply_button.autoDefault():
            raise RuntimeError("Run must never be the default button.")
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("A pending run added a layer before approval.")

        # The explicit click executes exactly one run and adds one temp layer.
        run_dock._on_apply_clicked()
        added = set(project.mapLayers()) - layers_before
        if len(added) != 1:
            raise RuntimeError(f"A buffer run added {len(added)} layers instead of one.")
        buffer_result_id = next(iter(added))
        if not run_dock.undo_button.isEnabled():
            raise RuntimeError("Undo was not offered after a successful run.")
        # A repeated click is a no-op: the one-shot nonce was already consumed.
        run_dock._on_apply_clicked()
        if set(project.mapLayers()) - layers_before != added:
            raise RuntimeError("A repeated Run click executed the run again.")
        run_dock._on_undo_clicked()
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("Undo did not remove exactly the buffer result layer.")
        del buffer_result_id

        # -- Owner-QA follow-up: filter features into a new layer ------------
        # The owner asked to "keep only the bus_stop points as a new layer".
        # native:extractbyattribute is now on the reviewed run allowlist; a
        # processing_run of it must add the matching-features layer (and its
        # forced FAIL_OUTPUT complement) without ever writing to disk.
        extract_before = set(project.mapLayers())
        extract_run = _run_json(
            _alg_token("native:extractbyattribute"), "native:extractbyattribute",
            {
                "INPUT": {"layer": tagged_layer.id()},
                "FIELD": {"field": "highway", "layer_param": "INPUT"},
                "OPERATOR": {"enum": 0},
                "VALUE": {"string": "bus_stop"},
            },
            title="Extract bus stops",
        )
        _feed_act(run_dock, AgentScope.PROJECT, "processing_run", extract_run)
        if run_dock._pending_action is None:
            raise RuntimeError("extractbyattribute produced no pending run action.")
        run_dock._on_apply_clicked()
        extract_added = set(project.mapLayers()) - extract_before
        if not extract_added:
            raise RuntimeError("The extract-by-attribute run added no layer.")
        matched = None
        for layer_id in extract_added:
            layer = project.mapLayer(layer_id)
            if isinstance(layer, QgsVectorLayer) and layer.featureCount() == 3:
                matched = layer
        if matched is None:
            counts = sorted(
                project.mapLayer(lid).featureCount() for lid in extract_added
            )
            raise RuntimeError(
                f"No extracted layer held the 3 bus_stop points; counts were {counts}."
            )
        run_dock._on_undo_clicked()
        if set(project.mapLayers()) != extract_before:
            raise RuntimeError("Undo did not remove the extract-by-attribute outputs.")

        # -- Owner-QA follow-up: random N as a new layer --------------------
        # The owner asked Agent Chat to take 3 of an existing point layer's
        # features randomly and create a new layer. randomselection only changes
        # input selection state; randomextract is the reviewed one-output
        # operation that faithfully satisfies the request.
        random_desc = run_dock.controller.execute(
            AgentToolCall(
                call_id="p5_random_extract",
                tool_name="processing.describe",
                arguments={"algorithm_id": "native:randomextract"},
            ),
            AgentMode.PLAN,
            AgentScope.PROJECT,
        )
        if (
            random_desc.status != AgentResultStatus.SUCCESS
            or not random_desc.data.get("agent_runnable")
        ):
            raise RuntimeError("native:randomextract was not advertised as agent-runnable.")
        random_bindings = {
            p["name"]: p["proposal_binding"] for p in random_desc.data["parameters"]
        }
        if random_bindings != {
            "INPUT": "layer",
            "METHOD": "enum",
            "NUMBER": "number",
            "OUTPUT": "",
        }:
            raise RuntimeError(
                f"randomextract advertised unexpected bindings: {random_bindings!r}"
            )

        random_before = set(project.mapLayers())
        random_run = _run_json(
            random_desc.data["context_token"],
            "native:randomextract",
            {
                "INPUT": {"layer": tagged_layer.id()},
                "METHOD": {"enum": 0},
                "NUMBER": {"number": 3},
            },
            title="Randomly extract 3 points",
        )
        _feed_act(run_dock, AgentScope.PROJECT, "processing_run", random_run)
        if run_dock._pending_action is None:
            raise RuntimeError("randomextract produced no pending run action.")
        run_dock._on_apply_clicked()
        random_added = set(project.mapLayers()) - random_before
        if len(random_added) != 1:
            raise RuntimeError(
                f"A randomextract run added {len(random_added)} layers instead of one."
            )
        random_layer = project.mapLayer(next(iter(random_added)))
        if not isinstance(random_layer, QgsVectorLayer) or random_layer.featureCount() != 3:
            raise RuntimeError("randomextract did not create a 3-feature vector layer.")
        if tagged_layer.selectedFeatureCount() != 0:
            raise RuntimeError("randomextract unexpectedly changed the input selection state.")
        run_dock._on_undo_clicked()
        if set(project.mapLayers()) != random_before:
            raise RuntimeError("Undo did not remove the randomextract output.")
        # This QA run is undone; neutralize its one-action cost so the rest of
        # Phase 05 keeps its careful per-session budget calibration.
        run_dock._session_action_count -= 1

        # -- Owner-QA follow-up: spatial extract (extract by location) -------
        # native:extractbylocation joined the allowlist so "keep the features of
        # X that intersect Y" runs as a reviewed processing_run. PREDICATE is
        # bound as a single live option index; the run writes a forced temporary
        # output and never touches disk. Intersecting a layer with itself keeps
        # every feature, which is enough to prove the run and its Undo.
        extractloc_before = set(project.mapLayers())
        extractloc_run = _run_json(
            _alg_token("native:extractbylocation"), "native:extractbylocation",
            {
                "INPUT": {"layer": tagged_layer.id()},
                "PREDICATE": {"enum": 0},
                "INTERSECT": {"layer": tagged_layer.id()},
            },
            title="Extract by location",
        )
        _feed_act(run_dock, AgentScope.PROJECT, "processing_run", extractloc_run)
        if run_dock._pending_action is None:
            raise RuntimeError("extractbylocation produced no pending run action.")
        run_dock._on_apply_clicked()
        extractloc_added = set(project.mapLayers()) - extractloc_before
        if len(extractloc_added) != 1:
            raise RuntimeError(
                f"An extractbylocation run added {len(extractloc_added)} layers instead of one."
            )
        run_dock._on_undo_clicked()
        if set(project.mapLayers()) != extractloc_before:
            raise RuntimeError("Undo did not remove the extract-by-location output.")
        # This QA run is undone; neutralize its one-action cost so the rest of
        # Phase 05 keeps its careful per-session budget calibration (the later
        # model_run must still land inside MAX_SESSION_ACTIONS).
        run_dock._session_action_count -= 1

        # A second reviewed algorithm runs the same way.
        centroids_run = _run_json(
            _alg_token("native:centroids"), "native:centroids",
            {"INPUT": {"layer": input_layer.id()}},
        )
        _feed_act(run_dock, AgentScope.PROJECT, "processing_run", centroids_run)
        run_dock._on_apply_clicked()
        centroid_added = set(project.mapLayers()) - layers_before
        if len(centroid_added) != 1:
            raise RuntimeError("A centroids run did not add exactly one temporary layer.")
        # A user-modified result blocks the destructive Undo.
        centroid_layer = project.mapLayer(next(iter(centroid_added)))
        centroid_layer.setName("Kept by the user")
        if run_dock._apply_coordinator.can_undo(run_dock._last_applied):
            raise RuntimeError("Undo stayed available after the user renamed the result.")
        for layer_id in centroid_added:
            project.removeMapLayer(layer_id)
        run_dock._last_applied = None

        # A Plan-mode run proposal previews only: no pending action, no Run.
        plan_run_start = run_dock.run_loop.start("preview", AgentMode.PLAN, AgentScope.PROJECT)
        plan_run_event = run_dock.run_loop.submit_provider_response(
            plan_run_start.request.request_token,
            _agent_turn("proposal", "Here.", kind="processing_run",
                        proposal_json=_run_json(
                            _alg_token("native:buffer"), "native:buffer",
                            {"INPUT": {"layer": input_layer.id()}})),
        )
        run_dock._handle_run_event(plan_run_event)
        if plan_run_event.kind != RunEventKind.PROPOSAL:
            raise RuntimeError("A valid Plan-mode run proposal did not validate.")
        if run_dock._pending_action is not None or run_dock.apply_button.isEnabled():
            raise RuntimeError("A Plan-mode run proposal created an approvable action.")
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("A Plan-mode run proposal executed something.")

        # Cancelling during the run adds no layer and revives nothing: the
        # coordinator emits its first progress signal before Processing starts,
        # so cancelling there proves a late result is discarded.
        if run_dock.run_coordinator.is_running():
            raise RuntimeError("The coordinator reported a run before one started.")
        run_dock.run_coordinator.cancel()  # terminal + idempotent with no run
        cancel_events: list = []
        run_dock.run_coordinator.run_canceled.connect(lambda: cancel_events.append("canceled"))
        run_dock.run_coordinator.run_progress.connect(
            lambda _p, _t: run_dock.run_coordinator.cancel()
        )
        _feed_act(run_dock, AgentScope.PROJECT, "processing_run",
                  _run_json(_alg_token("native:buffer"), "native:buffer",
                            {"INPUT": {"layer": input_layer.id()},
                             "DISTANCE": {"distance": 7}}))
        run_dock._on_apply_clicked()
        if not cancel_events:
            raise RuntimeError("Cancelling during a run did not report a cancellation.")
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("A cancelled run added a layer to the project.")
        if run_dock._last_applied is not None:
            raise RuntimeError("A cancelled run offered an Undo target.")
        if run_dock.run_coordinator.is_running():
            raise RuntimeError("A cancelled run stayed in the running state.")
        run_dock.run_coordinator.run_progress.disconnect()
        run_dock.run_coordinator.run_canceled.disconnect()

        # One running action maximum.
        occupied = run_dock.run_coordinator._state.start("busy", "processing_run", "Busy")
        refusal = run_dock.run_coordinator.start_processing_run(
            "second", "Second", "Buffer", "native:buffer", {}, ("OUTPUT",)
        )
        if refusal != "proposal_run_in_progress":
            raise RuntimeError("A second run was not refused while one was running.")
        run_dock.run_coordinator._state.finish(occupied, "finished")

        # A non-allowlisted algorithm is refused even with a valid receipt.
        blocked_id = "native:pixelstopoints"
        if AlgorithmCatalog.algorithm_exists(blocked_id):
            blocked_run = _run_json(
                _alg_token(blocked_id), blocked_id, {"INPUT_RASTER": {"layer": input_layer.id()}}
            )
            blocked_event = _feed_act(
                run_dock, AgentScope.PROJECT, "processing_run", blocked_run
            )
            if blocked_event.kind == RunEventKind.PROPOSAL:
                raise RuntimeError("A non-allowlisted algorithm reached a validated proposal.")
            if run_dock._pending_action is not None:
                raise RuntimeError("A non-allowlisted algorithm created a pending action.")

        # A destination binding is refused, and a path never parses at all.
        dest_run = _run_json(
            _alg_token("native:buffer"), "native:buffer",
            {"INPUT": {"layer": input_layer.id()}, "OUTPUT": {"string": "result"}},
        )
        if _feed_act(run_dock, AgentScope.PROJECT, "processing_run",
                     dest_run).kind == RunEventKind.PROPOSAL:
            raise RuntimeError("A proposal supplying an output destination was validated.")
        path_run = _run_json(
            _alg_token("native:buffer"), "native:buffer",
            {"INPUT": {"layer": input_layer.id()}, "OUTPUT": {"string": "C:/tmp/out.gpkg"}},
        )
        if _feed_act(run_dock, AgentScope.PROJECT, "processing_run",
                     path_run).kind == RunEventKind.PROPOSAL:
            raise RuntimeError("A proposal supplying an output path was validated.")
        # A stale receipt (rotated session secret) is refused.
        stale_token = _alg_token("native:buffer")
        run_dock.token_service.rotate()
        stale_run = _run_json(
            stale_token, "native:buffer", {"INPUT": {"layer": input_layer.id()}}
        )
        if _feed_act(run_dock, AgentScope.PROJECT, "processing_run",
                     stale_run).kind == RunEventKind.PROPOSAL:
            raise RuntimeError("A stale run receipt was accepted.")
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("A refused run proposal changed the project's layers.")

        # Real raster run: native:cellstatistics into a temporary raster.
        raster_run_tmp = _tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        raster_run_tmp.close()
        raster_run_id = None
        try:
            from osgeo import gdal as _run_gdal
            from osgeo import osr as _run_osr
            from qgis.core import QgsRasterLayer as _QgsRasterLayer

            run_driver = _run_gdal.GetDriverByName("GTiff")
            run_dataset = run_driver.Create(raster_run_tmp.name, 4, 4, 1, _run_gdal.GDT_Byte)
            run_dataset.SetGeoTransform((0.0, 1.0, 0.0, 4.0, 0.0, -1.0))
            run_srs = _run_osr.SpatialReference()
            run_srs.ImportFromEPSG(3857)
            run_dataset.SetProjection(run_srs.ExportToWkt())
            run_dataset.GetRasterBand(1).Fill(3)
            run_dataset = None
            run_raster = _QgsRasterLayer(raster_run_tmp.name, "run_raster", "gdal")
            if not run_raster.isValid():
                raise RuntimeError("The raster run fixture was not valid.")
            project.addMapLayer(run_raster)
            raster_run_id = run_raster.id()
            raster_before = set(project.mapLayers())
            cellstats_run = _run_json(
                _alg_token("native:cellstatistics"), "native:cellstatistics",
                {
                    "INPUT": {"layers": [raster_run_id]},
                    "REFERENCE_LAYER": {"layer": raster_run_id},
                    "STATISTIC": {"enum_string": "Mean"},
                },
                title="Cell statistics",
            )
            _feed_act(run_dock, AgentScope.PROJECT, "processing_run", cellstats_run)
            if run_dock._pending_action is None:
                raise RuntimeError("A raster run proposal created no pending action.")
            run_dock._on_apply_clicked()
            raster_added = set(project.mapLayers()) - raster_before
            if len(raster_added) != 1:
                raise RuntimeError("A cellstatistics run did not add one temporary raster.")
            if not run_dock.undo_button.isEnabled():
                raise RuntimeError("Undo was not offered after the raster run.")
            run_dock._on_undo_clicked()
            if set(project.mapLayers()) != raster_before:
                raise RuntimeError("Undo did not remove the raster result layer.")
            if raster_run_tmp.name in run_dock.transcript.toPlainText():
                raise RuntimeError("The run surface leaked the raster source path.")
        finally:
            if raster_run_id is not None:
                project.removeMapLayer(raster_run_id)
            os.unlink(raster_run_tmp.name)

        # Start a fresh bounded session before exercising model_run.  The
        # preceding adversarial and real-run cases intentionally consume the
        # ten-action safety budget; a production user would choose New chat at
        # this point, and the smoke test must not bypass or weaken that guard.
        run_dock.run_loop.new_chat()
        run_dock.token_service.rotate()
        run_dock._clear_all_action_state()

        # model_run: the current 2-node workflow, approved and undone.
        model_before = set(project.mapLayers())
        model_token = run_dock.controller.execute(
            AgentToolCall(call_id="p5_md", tool_name="model.describe"),
            AgentMode.PLAN, AgentScope.CURRENT_MODEL,
        ).data["context_token"]
        _feed_act(run_dock, AgentScope.CURRENT_MODEL, "model_run", _model_run_json(model_token))
        if run_dock._pending_action is None:
            raise RuntimeError("An Act model_run proposal created no pending action.")
        if set(project.mapLayers()) != model_before:
            raise RuntimeError("A pending model_run added a layer before approval.")
        run_dock._on_apply_clicked()
        model_added = set(project.mapLayers()) - model_before
        if not model_added:
            raise RuntimeError("A model_run added no terminal output layer.")
        if not run_dock.undo_button.isEnabled():
            raise RuntimeError("Undo was not offered after a model_run.")
        run_dock._on_undo_clicked()
        if set(project.mapLayers()) != model_before:
            raise RuntimeError("Undo did not remove the model_run result layers.")

        # The ledger records the execution outcomes and leaks no raw values.
        run_ledger = [e.to_dict() for e in run_dock.action_ledger.entries()]
        run_statuses = {entry["status"] for entry in run_ledger}
        for expected in ("proposed", "approved", "running", "completed", "undone"):
            if expected not in run_statuses:
                raise RuntimeError(f"The ledger did not record a {expected!r} run entry.")
        run_ledger_text = "\n".join(str(entry) for entry in run_ledger)
        for leaked in (input_layer.id(), "TEMPORARY_OUTPUT", "EPSG:"):
            if leaked in run_ledger_text:
                raise RuntimeError(f"The run ledger leaked forbidden content: {leaked}")

        # Shutdown cancels and tears the run coordinator down.
        run_dock.shutdown()
        if (run_dock.run_coordinator.is_running() or run_dock._running_action is not None
                or run_dock._pending_action is not None or run_dock.action_ledger.entries()):
            raise RuntimeError("shutdown did not clear Phase 05 run state.")
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("Phase 05 execution left layers behind in the project.")

        # -- Phase 06: plugin-aware assistance ------------------------------
        caps_dock = AgentWorkspaceDock(fake_iface, lambda: None)

        def _capabilities(package_name):
            outcome = caps_dock.controller.execute(
                AgentToolCall(call_id="p6_caps", tool_name="plugin.capabilities",
                              arguments={"package_name": package_name}),
                AgentMode.ASK, AgentScope.PLUGINS,
            )
            if outcome.status != AgentResultStatus.SUCCESS:
                raise RuntimeError(f"plugin.capabilities failed for {package_name}.")
            return outcome.data

        # The registry includes the core, Power metadata, and Developer
        # Workspace inspection tools.
        tool_names = {d["name"] for d in caps_dock.registry.public_tool_descriptions()}
        if (
            len(tool_names) != 23
            or "plugin.capabilities" not in tool_names
            or "expression.search" not in tool_names
        ):
            raise RuntimeError(f"Expected the complete tool registry, got {len(tool_names)}.")

        # An unknown package is reported honestly, never invented.
        unknown = _capabilities("definitely_not_a_real_plugin_xyz")
        if unknown["status"] != "not_installed" or unknown["available"]:
            raise RuntimeError("An unknown package was not reported as not installed.")
        if unknown["algorithms"] or unknown["providers"]:
            raise RuntimeError("An unknown package produced providers or algorithms.")

        # Provider views must be derivable from the live registry without ever
        # touching a plugin object, and must never carry a module/source path.
        from planx_smartmodeler.core.agent.runtime_tools import build_provider_views

        provider_views = build_provider_views(QgsApplication.processingRegistry())
        if not provider_views:
            raise RuntimeError("No live Processing providers were adapted.")
        for view in provider_views:
            if "/" in view.owning_package or "\\" in view.owning_package:
                raise RuntimeError("A provider view leaked a filesystem path.")
            if "." in view.owning_package:
                raise RuntimeError("A provider view kept a dotted module path.")

        # Every installed plugin must yield one of the five honest statuses, and
        # a claim of 'confirmed' must be backed by a provider proved to come
        # from that exact package.
        import qgis.utils as _qgis_utils

        packages = sorted(set(getattr(_qgis_utils, "available_plugins", []) or []))
        confirmed_seen = 0
        ui_only_seen = 0
        by_package = {view.owning_package: view for view in provider_views}
        for package in packages[:25]:
            report = _capabilities(package)
            if report["status"] not in (
                "confirmed_provider", "declared_unconfirmed",
                "candidate_only", "ui_only_or_unmapped",
            ):
                raise RuntimeError(f"{package} produced an unknown status.")
            if report["agent_executable"]:
                if (
                    not report.get("agent_actions")
                    or not report.get("context_token")
                    or not report.get("enabled")
                ):
                    raise RuntimeError(
                        "A plugin was executable without a ready reviewed adapter."
                    )
            if report["status"] == "confirmed_provider":
                confirmed_seen += 1
                if package not in by_package:
                    raise RuntimeError(
                        f"{package} was confirmed without a provider proving it."
                    )
                if report["confidence"] != "confirmed":
                    raise RuntimeError("A confirmed status carried the wrong confidence.")
            else:
                ui_only_seen += 1
                if report["algorithms"]:
                    raise RuntimeError(
                        f"{package} listed algorithms without a confirmed provider."
                    )
        if not packages:
            print("  note: no installed plugins in this clean profile; per-package "
                  "capability statuses were not exercised against real packages")

        # The confirmed-mapping path must be proven against a REAL live provider
        # even when the clean profile has no third-party plugin installed: take
        # an actual provider, claim its own defining package, and assert it is
        # confirmed and lists that provider's real algorithms.
        from planx_smartmodeler.core.agent.plugin_capabilities import (
            PluginView as _PluginView,
            build_capabilities as _build_capabilities,
        )

        real = next((v for v in provider_views if v.owning_package and v.algorithms), None)
        if real is None:
            raise RuntimeError("No live provider with algorithms was available to test.")
        confirmed_report = _build_capabilities(
            _PluginView(real.owning_package, declares_processing_provider=True),
            provider_views,
            algorithm_allowed=AlgorithmCatalog.ai_algorithm_allowed,
        )
        if confirmed_report["status"] != "confirmed_provider":
            raise RuntimeError("A provider's own defining package was not confirmed.")
        if confirmed_report["confidence"] != "confirmed":
            raise RuntimeError("A confirmed mapping reported the wrong confidence.")
        if not confirmed_report["algorithms"]:
            raise RuntimeError("A confirmed provider listed no algorithms.")
        # One package may legitimately define several providers, so the expected
        # id set is the union over every provider that package defined.
        live_ids = {
            algorithm[0]
            for view in provider_views
            if view.owning_package == real.owning_package
            for algorithm in view.algorithms
        }
        if not live_ids:
            raise RuntimeError("The confirmed package exposed no live algorithm ids.")
        for row in confirmed_report["algorithms"]:
            if row["algorithm_id"] not in live_ids:
                raise RuntimeError("A confirmed listing contained a foreign algorithm.")
        if confirmed_report["agent_executable"] and not confirmed_report.get(
            "agent_actions"
        ):
            raise RuntimeError("A confirmed provider was reported as agent-executable.")

        # The same providers must NOT confirm a package that did not define them.
        foreign_report = _build_capabilities(
            _PluginView("totally_unrelated_package_name", declares_processing_provider=True),
            provider_views,
        )
        if foreign_report["status"] == "confirmed_provider":
            raise RuntimeError("A foreign package was falsely confirmed.")
        if foreign_report["algorithms"]:
            raise RuntimeError("A foreign package received an algorithm listing.")

        # processing.describe reports runnability from the live signature. Core
        # native algorithms, safe PlanX domain-text algorithms, and the one
        # reviewed QuickOSM adapter are positive cases; unreviewed providers
        # remain negative.
        native_described = caps_dock.controller.execute(
            AgentToolCall(call_id="p6_pd1", tool_name="processing.describe",
                          arguments={"algorithm_id": "native:buffer"}),
            AgentMode.ASK, AgentScope.PROJECT,
        ).data
        if not native_described.get("agent_runnable"):
            raise RuntimeError("A reviewed native algorithm was not reported as runnable.")
        if not native_described.get("provider_id"):
            raise RuntimeError("processing.describe did not expose the provider id.")
        for parameter in native_described.get("parameters", []):
            if "default" in parameter:
                raise RuntimeError("processing.describe leaked a parameter default value.")
            if not isinstance(parameter.get("required"), bool):
                raise RuntimeError("processing.describe omitted the required flag.")
        if not native_described.get("outputs"):
            raise RuntimeError("processing.describe did not report output definitions.")
        if AlgorithmCatalog.algorithm_exists("planx:spacesyntax"):
            planx_described = caps_dock.controller.execute(
                AgentToolCall(
                    call_id="p6_planx",
                    tool_name="processing.describe",
                    arguments={"algorithm_id": "planx:spacesyntax"},
                ),
                AgentMode.ASK,
                AgentScope.PROJECT,
            ).data
            planx_bindings = {
                row["name"]: row.get("proposal_binding", "")
                for row in planx_described.get("parameters", [])
            }
            if (
                not planx_described.get("agent_runnable")
                or planx_bindings.get("NETWORK") != "layer"
                or planx_bindings.get("RADII") != "text"
            ):
                raise RuntimeError(
                    "planx:spacesyntax was not exposed through safe live bindings."
                )
        quick_id = "quickosm:downloadosmdataextentquery"
        if AlgorithmCatalog.algorithm_exists(quick_id):
            quick_described = caps_dock.controller.execute(
                AgentToolCall(
                    call_id="p6_quickosm",
                    tool_name="processing.describe",
                    arguments={"algorithm_id": quick_id},
                ),
                AgentMode.ASK,
                AgentScope.PROJECT,
            ).data
            quick_bindings = {
                row["name"]: row.get("proposal_binding", "")
                for row in quick_described.get("parameters", [])
            }
            if (
                not quick_described.get("agent_runnable")
                or quick_bindings.get("KEY") != "osm_tag"
                or quick_bindings.get("EXTENT") != "map_extent"
                or quick_bindings.get("SERVER")
            ):
                raise RuntimeError(
                    "The QuickOSM adapter exposed an unsafe or incomplete binding set."
                )
        non_native = next(
            (a for a in ("gdal:buffervectors", "qgis:basicstatisticsforfields")
             if AlgorithmCatalog.algorithm_exists(a)), "")
        if non_native:
            other = caps_dock.controller.execute(
                AgentToolCall(call_id="p6_pd2", tool_name="processing.describe",
                              arguments={"algorithm_id": non_native}),
                AgentMode.ASK, AgentScope.PROJECT,
            ).data
            if other.get("agent_runnable"):
                raise RuntimeError(
                    f"{non_native} was reported runnable; the allowlist grew unexpectedly."
                )

        # Continuation: an outcome note is bounded/sanitized, the cap holds, and
        # New chat resets it.
        caps_dock._record_action_outcome("processing_run", "completed", "Buffer")
        notes = caps_dock.run_loop.session_memory.exchanges()
        if not notes or "processing_run" not in notes[-1].assistant_text:
            raise RuntimeError("A completed action left no outcome note in session memory.")
        for leaked in ("C:\\", "TEMPORARY_OUTPUT", "EPSG:"):
            if leaked in notes[-1].assistant_text:
                raise RuntimeError(f"The outcome note leaked {leaked}.")
        while caps_dock._session_action_budget_left():
            caps_dock._record_action_outcome("processing_run", "completed", "T")
        if caps_dock._session_action_budget_left():
            raise RuntimeError("The per-session action cap never engaged.")
        caps_dock.run_loop.new_chat()
        caps_dock._clear_all_action_state()
        if not caps_dock._session_action_budget_left():
            raise RuntimeError("New chat did not reset the per-session action cap.")

        # -- Phase 07: hardening, UX and lifecycle --------------------------
        # The two-pass provider walk must be an optimization only: enumerating
        # algorithms for just the requested package has to produce byte-identical
        # capability reports, for a package that owns providers and one that does
        # not.
        for probe_package in (real.owning_package, "totally_unrelated_package_name"):
            full = _build_capabilities(
                _PluginView(probe_package, declares_processing_provider=True),
                build_provider_views(QgsApplication.processingRegistry()),
                algorithm_allowed=AlgorithmCatalog.ai_algorithm_allowed,
            )
            filtered = _build_capabilities(
                _PluginView(probe_package, declares_processing_provider=True),
                build_provider_views(
                    QgsApplication.processingRegistry(), for_package=probe_package
                ),
                algorithm_allowed=AlgorithmCatalog.ai_algorithm_allowed,
            )
            if full != filtered:
                raise RuntimeError(
                    f"The two-pass provider walk changed the report for {probe_package}."
                )

        # The panel must survive being wrapped in a scroll area on both Qt
        # builds, and the transcript must be a bounded rolling window.
        if caps_dock.widget() is None or caps_dock.widget().widget() is None:
            raise RuntimeError("The Agent Workspace scroll container did not construct.")
        if caps_dock.transcript.maximumBlockCount() <= 0:
            raise RuntimeError("The transcript is unbounded.")
        for _ in range(caps_dock.transcript.maximumBlockCount() + 200):
            caps_dock._append_line("flood")
        if caps_dock.transcript.blockCount() > caps_dock.transcript.maximumBlockCount() + 1:
            raise RuntimeError("The transcript grew past its cap.")

        # Visible Plan/Act semantics: each mode states its own limit.
        hints = set()
        for index in range(caps_dock.mode_combo.count()):
            caps_dock.mode_combo.setCurrentIndex(index)
            hints.add(caps_dock.mode_hint_label.text())
        if len(hints) != caps_dock.mode_combo.count():
            raise RuntimeError("The mode hint did not change with the selected mode.")
        caps_dock.mode_combo.setCurrentIndex(0)

        # Every interactive control announces itself, and Apply is never the
        # default button a stray Enter could trigger.
        for control in (
            caps_dock.apply_button, caps_dock.reject_button, caps_dock.undo_button,
            caps_dock.send_button, caps_dock.prompt_input, caps_dock.mode_combo,
        ):
            if not control.accessibleName():
                raise RuntimeError("An Agent Workspace control has no accessible name.")
        if caps_dock.apply_button.isDefault() or caps_dock.apply_button.autoDefault():
            raise RuntimeError("Apply became a default button.")

        # The risk badge is rendered from the kind and the validated destructive
        # flag, and an unknown kind must not be shown as reassuring.
        class _CardStub:
            def __init__(self, kind, destructive):
                self.kind = kind
                self._destructive = destructive

            def to_public_card(self):
                return {
                    "kind": self.kind, "title": "t", "target": "target",
                    "summary": "s", "destructive": self._destructive, "warnings": [],
                }

        badges = {}
        for kind, destructive in (
            ("layer_style", False), ("model_patch", True), ("processing_run", False),
            ("not_a_real_kind", False),
        ):
            caps_dock._show_approval_card(_CardStub(kind, destructive))
            if not caps_dock.risk_badge_label.isVisible() and caps_dock.isVisible():
                raise RuntimeError(f"No risk badge was shown for {kind}.")
            badges[kind] = caps_dock.risk_badge_label.text()
        if not badges["layer_style"].lower().startswith("low"):
            raise RuntimeError("A style change was not shown as low risk.")
        if not badges["model_patch"].lower().startswith("high"):
            raise RuntimeError("A destructive model patch was not shown as high risk.")
        if not badges["not_a_real_kind"].lower().startswith("high"):
            raise RuntimeError("An unknown action kind did not fail closed to high risk.")

        # The stale-check timer runs only while a card is up, and clearing the
        # card stops it -- no Qt timer may outlive the pending action.
        if not caps_dock._stale_timer.isActive():
            raise RuntimeError("The stale-check timer did not start with an approval card.")
        caps_dock._clear_approval_card()
        if caps_dock._stale_timer.isActive():
            raise RuntimeError("The stale-check timer outlived the approval card.")
        if caps_dock.risk_badge_label.isVisible():
            raise RuntimeError("The risk badge survived clearing the approval card.")

        # A stale tick can only take approval away, and only when the action has
        # really expired: with no pending action it must simply stop.
        caps_dock._pending_action = None
        caps_dock._stale_timer.start()
        caps_dock._on_stale_tick()
        if caps_dock._stale_timer.isActive() or caps_dock.apply_button.isEnabled():
            raise RuntimeError("A stale tick without a pending action misbehaved.")

        caps_dock.shutdown()
        if caps_dock._stale_timer.isActive():
            raise RuntimeError("shutdown left the stale-check timer running.")

        proposal_dock.shutdown()
        project.removeMapLayer(style_layer.id())

        layer_ids_before_agent_chat = set(project.mapLayers())

        def _fake_generate_structured(responses, dock):
            def _handler(_profile, _api_key, _system_prompt, _user_prompt, _contract):
                dock.ai_client.succeeded.emit(responses.pop(0))

            return _handler

        chat_dock = AgentWorkspaceDock(fake_iface, lambda: None)
        if (
            not chat_dock.profile_label.text().startswith("Profile:")
            or chat_dock.ai_settings_button is None
        ):
            raise RuntimeError("Agent Workspace profile label/settings controls did not construct.")

        first_turn_raw = _json.dumps(
            {
                "action": "tool_calls",
                "assistant_text": "Checking your project.",
                "tool_calls": [
                    {"call_id": "smoke1", "tool_name": "project.summary", "arguments_json": "{}"}
                ],
                "proposal_kind": "none",
                "proposal_json": "",
            }
        )
        final_turn_raw = _json.dumps(
            {
                "action": "final",
                "assistant_text": "Your project has been inspected.",
                "tool_calls": [],
                "proposal_kind": "none",
                "proposal_json": "",
            }
        )
        chat_dock._active_profile = AiProfile.create("openai_compatible", "Smoke chat profile")
        chat_dock._active_api_key = "smoke-key"
        chat_dock.ai_client.generate_structured = _fake_generate_structured(
            [first_turn_raw, final_turn_raw], chat_dock
        )
        start_event = chat_dock.run_loop.start(
            "What is my project called?", AgentMode.ASK, AgentScope.PROJECT
        )
        chat_dock._handle_run_event(start_event)

        if chat_dock.run_loop.is_active():
            raise RuntimeError("The two-turn Agent Chat run did not reach a terminal state.")
        if chat_dock.run_loop.mode != AgentMode.ASK or chat_dock.run_loop.scope != AgentScope.PROJECT:
            raise RuntimeError("Agent Chat did not keep the captured mode/scope for the run.")
        transcript_text = chat_dock.transcript.toPlainText()
        if "Your project has been inspected." not in transcript_text:
            raise RuntimeError("Agent Chat did not render its final answer.")
        if "[tool: project.summary] success" not in transcript_text:
            raise RuntimeError("Agent Chat did not execute the real project.summary tool.")
        if "arguments_json" in transcript_text or "smoke-key" in transcript_text:
            raise RuntimeError("Agent Chat transcript leaked raw provider/argument/secret text.")
        if set(project.mapLayers()) != layer_ids_before_agent_chat:
            raise RuntimeError("Agent Chat mutated the project's layers.")
        final_status = chat_dock.status_label.text()
        if "tool call" not in final_status or "turn" not in final_status.lower():
            raise RuntimeError("Agent Chat did not render turn/tool-call usage in its status.")
        chat_dock._on_token_usage(AiTokenUsage(80, 12, 95, 24))
        if chat_dock.token_usage_label.text() != "Last 80 · Chat 80 · Cached 24":
            raise RuntimeError("Agent Workspace did not render provider token usage.")
        if "80 input + 12 output" not in chat_dock.token_usage_label.toolTip():
            raise RuntimeError("Agent Workspace token tooltip omitted provider usage detail.")
        if chat_dock._active_api_key != "" or chat_dock._active_profile is not None:
            raise RuntimeError("Agent Chat did not clear its transient key/profile after finishing.")

        cancel_dock = AgentWorkspaceDock(fake_iface, lambda: None)
        cancel_dock._active_profile = AiProfile.create("openai_compatible", "Smoke cancel profile")
        cancel_dock._active_api_key = "smoke-key"
        cancel_sent: list = []
        cancel_dock.ai_client.generate_structured = (
            lambda *_args, **_kwargs: cancel_sent.append(True)
        )
        cancel_start_event = cancel_dock.run_loop.start("hello", AgentMode.ASK, AgentScope.PROJECT)
        cancel_dock._handle_run_event(cancel_start_event)
        if not cancel_dock.run_loop.is_active() or not cancel_sent:
            raise RuntimeError("Agent Chat did not start a run for the cancel probe.")
        cancel_dock._on_stop_clicked()
        if cancel_dock.run_loop.is_active():
            raise RuntimeError("Stop did not cancel the active Agent Chat run.")
        if cancel_dock.stop_button.isEnabled() or not cancel_dock.send_button.isEnabled():
            raise RuntimeError("Stop did not restore the Agent Workspace dock controls.")
        if cancel_dock._active_api_key != "" or cancel_dock._active_profile is not None:
            raise RuntimeError("Stop did not clear the dock's transient API key/profile.")
        cancel_dock.ai_client.succeeded.emit(final_turn_raw)
        if cancel_dock.run_loop.is_active() or cancel_dock.run_loop.turns_used > 1:
            raise RuntimeError("A late provider callback after cancel revived the run.")

        shutdown_dock = AgentWorkspaceDock(fake_iface, lambda: None)
        shutdown_dock._active_profile = AiProfile.create("openai_compatible", "Smoke shutdown profile")
        shutdown_dock._active_api_key = "smoke-key"
        shutdown_dock.ai_client.generate_structured = lambda *_args, **_kwargs: None
        shutdown_start_event = shutdown_dock.run_loop.start(
            "hello", AgentMode.ASK, AgentScope.PROJECT
        )
        shutdown_dock._handle_run_event(shutdown_start_event)
        if not shutdown_dock.run_loop.is_active():
            raise RuntimeError("Agent Chat did not start a run for the shutdown probe.")
        shutdown_dock.shutdown()
        if shutdown_dock.run_loop.is_active():
            raise RuntimeError("shutdown() did not cancel the active Agent Chat run.")
        if shutdown_dock._active_api_key != "" or shutdown_dock._active_profile is not None:
            raise RuntimeError("shutdown() did not clear the dock's transient API key/profile.")
        shutdown_dock.ai_client.succeeded.emit(final_turn_raw)
        if shutdown_dock.run_loop.is_active() or shutdown_dock.run_loop.turns_used > 1:
            raise RuntimeError("A late provider callback after shutdown revived the run.")

        # Offline Send must not start network activity.
        offline_store = AiSettingsStore()
        offline_profile = AiProfile.create("offline", "Smoke offline profile")
        offline_store.save_profile(offline_profile)
        offline_store.set_active(offline_profile.profile_id)
        offline_dock = AgentWorkspaceDock(fake_iface, lambda: None)
        offline_network_calls: list = []
        offline_dock.ai_client.generate_structured = (
            lambda *_args, **_kwargs: offline_network_calls.append(True)
        )
        # Phase 07: an offline profile says so before the user writes anything,
        # rather than only failing once they press Send.
        if "quick inspections only" not in offline_dock.profile_label.text():
            raise RuntimeError("The Offline state was not shown up front on the profile line.")
        offline_dock.prompt_input.setPlainText("hello")
        offline_dock._on_send_clicked()
        if offline_network_calls or offline_dock.run_loop.is_active():
            raise RuntimeError("Offline Agent Chat started network activity or a run.")

        # Ctrl+Enter sends; it must go through the same guarded send path and
        # must not be able to reach an approval control.
        offline_dock.prompt_input.setPlainText("second message")
        _ctrl_enter = QKeyEvent(
            QEvent.Type.KeyPress, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier
        )
        offline_dock.eventFilter(offline_dock.prompt_input, _ctrl_enter)
        if offline_network_calls or offline_dock.apply_button.isEnabled():
            raise RuntimeError("Ctrl+Enter reached the network or an approval control.")
        offline_dock.shutdown()

        workflow_context = AiMcpBridge.workflow_context(graph)
        if '"id":"source"' not in workflow_context or '"id":"buffer"' not in workflow_context:
            raise RuntimeError("The current workflow was not serialized for iterative AI.")
        if input_layer.id() in workflow_context or "10.0" in workflow_context:
            raise RuntimeError("A local workflow parameter leaked into iterative AI context.")
        restored_ai_graph = AiMcpBridge.parse_response(
            workflow_context,
            base_graph=graph,
        ).graph
        if (
            restored_ai_graph.nodes["source"].parameters.get("LAYER")
            != input_layer.id()
            or restored_ai_graph.nodes["buffer"].parameters.get("DISTANCE")
            != 10.0
        ):
            raise RuntimeError("Redacted AI parameters were not restored locally.")
        try:
            AiMcpBridge.parse_response(workflow_context)
        except AiResponseError:
            pass
        else:
            raise RuntimeError("A local-value token was accepted without a baseline.")

        suitability_id = "planx_suitability_lab:data_harmonizer"
        if AlgorithmCatalog.algorithm_exists(suitability_id):
            harmonizer = AlgorithmCatalog.create_node(suitability_id, "harmonizer")
            input_rasters = harmonizer.inputs.get("INPUT_RASTERS")
            if (
                input_rasters is None
                or not input_rasters.required
                or not input_rasters.allows_multiple
            ):
                raise RuntimeError(
                    "The suitability raster collection was not modeled as required."
                )
            parameter_dialog = NodeParameterDialog(harmonizer)
            wrapper = parameter_dialog.form.native_wrappers.get("INPUT_RASTERS")
            if wrapper is None or wrapper.wrappedWidget() is None:
                raise RuntimeError(
                    "The native QGIS multiple-raster parameter widget was not created."
                )
            parameter_dialog.close()

        model_path = Path(__file__).with_name("_smoke.model3")
        try:
            ok, error = Model3Serializer.export_to_model3(graph, str(model_path))
            if not ok:
                raise RuntimeError(error)
            imported, error = Model3Serializer.import_from_model3(str(model_path))
            if imported is None or len(imported.nodes) != 2:
                details = "none" if imported is None else ", ".join(
                    f"{node.node_id}:{node.algorithm_id}" for node in imported.nodes.values()
                )
                raise RuntimeError(
                    error or f"QGIS model round-trip nodes were [{details}]."
                )
        finally:
            model_path.unlink(missing_ok=True)

        # -- V0.7: typed model parameters and semantic source round-trip ----
        fixture_provider = ConfiguredSchemaProvider()
        if not QgsApplication.processingRegistry().addProvider(fixture_provider):
            raise RuntimeError("Could not register the configured-schema fixture.")
        configured_path = Path(__file__).with_name("_smoke_configured.model3")
        configured_roundtrip_path = Path(__file__).with_name(
            "_smoke_configured_roundtrip.model3"
        )
        typed_path = Path(__file__).with_name("_smoke_typed.model3")
        typed_roundtrip_path = Path(__file__).with_name(
            "_smoke_typed_roundtrip.model3"
        )
        mixed_path = Path(__file__).with_name("_smoke_mixed.model3")
        mixed_roundtrip_path = Path(__file__).with_name(
            "_smoke_mixed_roundtrip.model3"
        )
        try:
            for invalid_destinations in (
                (),
                tuple(
                    f"RESULT_{index}"
                    for index in range(MAX_RESULT_LAYERS + 1)
                ),
            ):
                destination_coordinator = RunCoordinator(lambda: None)
                destination_failures = []
                destination_coordinator.run_failed.connect(
                    lambda _reason, message: destination_failures.append(message)
                )
                refusal = destination_coordinator.start_processing_run(
                    "invalid-destinations",
                    "Invalid destinations",
                    "Configured fixture",
                    "smartmodeler_fixture:configured_schema",
                    {"NUMBER": 1},
                    invalid_destinations,
                )
                if (
                    refusal
                    or len(destination_failures) != 1
                    or "invalid result-layer count"
                    not in destination_failures[0]
                ):
                    raise RuntimeError(
                        "Agent execution accepted an invalid result-layer count."
                    )

            silent_graph = GraphModel("Progressless cancellation")
            silent_node = AlgorithmCatalog.create_node(
                "smartmodeler_fixture:silent_cancel",
                "silent_cancel",
                "Progressless task",
            )
            silent_graph.add_node(silent_node)
            silent_window = SmartModelerWindow(None)
            silent_window._set_graph(silent_graph)
            silent_reports = []
            silent_window._show_execution_report = silent_reports.append
            SILENT_CANCEL_OBSERVATIONS.clear()
            silent_project_ids = set(project.mapLayers())
            silent_started = time.monotonic()
            silent_window.run_model()
            QTimer.singleShot(
                100, silent_window.cancel_run_action.trigger
            )
            silent_deadline = silent_started + 4.0
            while (
                silent_window._is_executing
                and time.monotonic() < silent_deadline
            ):
                QApplication.processEvents()
                time.sleep(0.01)
            silent_elapsed = time.monotonic() - silent_started
            if (
                silent_window._is_executing
                or silent_elapsed >= 3.0
                or not SILENT_CANCEL_OBSERVATIONS
                or SILENT_CANCEL_OBSERVATIONS[-1][0] != "canceled"
                or len(silent_reports) != 1
                or silent_reports[0].status != ExecutionStatus.CANCELED
                or silent_reports[0].added_layer_ids
                or set(project.mapLayers()) != silent_project_ids
            ):
                raise RuntimeError(
                    "A progressless Processing task was not canceled "
                    "responsively and atomically."
                )
            silent_window.document_history.reset(
                Model3Serializer.export_to_json(silent_window.graph),
                mark_clean=True,
            )
            silent_window.close()

            configured_model = QgsProcessingModelAlgorithm(
                "Configured schema", "SmartModeler GIS", "configured_schema"
            )
            configured_child = QgsProcessingModelChildAlgorithm(
                "smartmodeler_fixture:configured_schema"
            )
            configured_child.setChildId("configured")
            configured_child.setDescription("Configured child")
            configured_child.setConfiguration({"mode": "text"})
            configured_child.addParameterSources(
                "TEXT",
                [
                    QgsProcessingModelChildParameterSource.fromStaticValue(
                        "preserved"
                    )
                ],
            )
            configured_model.addChildAlgorithm(configured_child)
            if not configured_model.toFile(str(configured_path)):
                raise RuntimeError("Could not write the configured-schema fixture.")
            configured_graph, error = Model3Serializer.import_from_model3(
                str(configured_path)
            )
            if configured_graph is None:
                raise RuntimeError(error or "Configured-schema import failed.")
            configured_node = configured_graph.nodes["configured"]
            if (
                set(configured_node.inputs) != {"TEXT"}
                or set(configured_node.outputs) != {"TEXT_OUTPUT"}
                or configured_node.algorithm_configuration != {"mode": "text"}
            ):
                raise RuntimeError(
                    "Configured algorithm did not use its live port schema."
                )
            configured_json = Model3Serializer.export_to_json(configured_graph)
            configured_json_graph = Model3Serializer.import_from_json(
                configured_json
            )
            if (
                configured_json_graph is None
                or set(configured_json_graph.nodes["configured"].inputs)
                != {"TEXT"}
            ):
                raise RuntimeError(
                    "Configured algorithm schema changed in JSON round-trip."
                )
            ok, error = Model3Serializer.export_to_model3(
                configured_json_graph,
                str(configured_roundtrip_path),
                allow_invalid=True,
            )
            if not ok:
                raise RuntimeError(error)
            configured_reopened = QgsProcessingModelAlgorithm()
            if not configured_reopened.fromFile(str(configured_roundtrip_path)):
                raise RuntimeError("Configured-schema round-trip could not reopen.")
            reopened_configured_child = configured_reopened.childAlgorithms()[
                "configured"
            ]
            reopened_configured_algorithm = reopened_configured_child.algorithm()
            if (
                dict(reopened_configured_child.configuration())
                != {"mode": "text"}
                or reopened_configured_algorithm.parameterDefinition("TEXT")
                is None
                or reopened_configured_algorithm.parameterDefinition("NUMBER")
                is not None
                or reopened_configured_algorithm.outputDefinition("TEXT_OUTPUT")
                is None
            ):
                raise RuntimeError(
                    "Configured algorithm schema changed in native round-trip."
                )

            typed_model = QgsProcessingModelAlgorithm(
                "Typed inputs", "SmartModeler GIS", "typed_inputs"
            )
            typed_definitions = [
                QgsProcessingParameterVectorLayer("VECTOR", "Vector"),
                QgsProcessingParameterNumber(
                    "NUMBER", "Number", defaultValue=12.5, minValue=0, maxValue=1000
                ),
                QgsProcessingParameterBoolean(
                    "BOOLEAN", "Boolean", defaultValue=True
                ),
                QgsProcessingParameterString(
                    "STRING", "String", defaultValue="roads"
                ),
                QgsProcessingParameterCrs(
                    "CRS", "CRS", defaultValue="EPSG:3857"
                ),
                QgsProcessingParameterExtent(
                    "EXTENT", "Extent", defaultValue="0,10,0,10 [EPSG:3857]"
                ),
                QgsProcessingParameterEnum(
                    "ENUM",
                    "Enum",
                    options=["first", "second"],
                    allowMultiple=True,
                    defaultValue=[0, 1],
                ),
                QgsProcessingParameterField(
                    "FIELD",
                    "Field",
                    defaultValue="name",
                    parentLayerParameterName="VECTOR",
                    allowMultiple=True,
                ),
                QgsProcessingParameterMultipleLayers(
                    "VECTORS",
                    "Vectors",
                    layerType=Qgis.ProcessingSourceType.Vector,
                ),
                QgsProcessingParameterMultipleLayers(
                    "RASTERS",
                    "Rasters",
                    layerType=Qgis.ProcessingSourceType.Raster,
                ),
            ]
            for index, definition in enumerate(typed_definitions):
                component = QgsProcessingModelParameter(definition.name())
                component.setDescription(definition.description())
                component.setPosition(QPointF(index * 40.0, index * 20.0))
                typed_model.addModelParameter(definition, component)
            if not typed_model.toFile(str(typed_path)):
                raise RuntimeError("Could not write the typed .model3 fixture.")

            typed_graph, error = Model3Serializer.import_from_model3(str(typed_path))
            if typed_graph is None:
                raise RuntimeError(error or "Typed .model3 import failed.")
            expected_smart_types = {
                "VECTOR": "smart:input_layer",
                "NUMBER": "smart:number",
                "BOOLEAN": "smart:boolean",
                "STRING": "smart:string",
                "CRS": "smart:crs",
                "EXTENT": "smart:extent",
                "ENUM": "smart:enum",
                "FIELD": "smart:field",
                "VECTORS": "smart:multiple_vector",
                "RASTERS": "smart:multiple_raster",
            }
            actual_smart_types = {
                node_id: node.algorithm_id
                for node_id, node in typed_graph.nodes.items()
            }
            if actual_smart_types != expected_smart_types:
                raise RuntimeError(
                    f"Typed model inputs changed type: {actual_smart_types}"
                )
            typed_graph.nodes["NUMBER"].set_parameter("VALUE", 9.0)
            typed_json = Model3Serializer.export_to_json(typed_graph)
            typed_json_graph = Model3Serializer.import_from_json(typed_json)
            if typed_json_graph is None:
                raise RuntimeError("Typed SmartModeler JSON did not round-trip.")
            ok, error = Model3Serializer.export_to_model3(
                typed_json_graph,
                str(typed_roundtrip_path),
                allow_invalid=True,
            )
            if not ok:
                raise RuntimeError(error)
            typed_reopened = QgsProcessingModelAlgorithm()
            if not typed_reopened.fromFile(str(typed_roundtrip_path)):
                raise RuntimeError("Typed round-trip .model3 could not be reopened.")
            original_parameter_maps = {
                definition.name(): definition.toVariantMap()
                for definition in typed_model.parameterDefinitions()
            }
            reopened_parameter_maps = {
                definition.name(): definition.toVariantMap()
                for definition in typed_reopened.parameterDefinitions()
            }
            for name, original_map in original_parameter_maps.items():
                reopened_map = reopened_parameter_maps.get(name, {})
                for key in (
                    "parameter_type",
                    "default",
                    "options",
                    "allow_multiple",
                    "parent_layer",
                    "layer_type",
                ):
                    expected_value = (
                        9.0
                        if name == "NUMBER" and key == "default"
                        else original_map.get(key)
                    )
                    if key in original_map and reopened_map.get(key) != expected_value:
                        raise RuntimeError(
                            f"Typed parameter {name}.{key} changed in round-trip."
                        )

            mixed_model = QgsProcessingModelAlgorithm(
                "Mixed sources", "SmartModeler GIS", "mixed_sources"
            )
            vector_definition = QgsProcessingParameterVectorLayer(
                "VECTOR_INPUT", "Vector input"
            )
            vector_component = QgsProcessingModelParameter("VECTOR_INPUT")
            vector_component.setPosition(QPointF(0, 0))
            mixed_model.addModelParameter(vector_definition, vector_component)
            buffer_child = QgsProcessingModelChildAlgorithm("native:buffer")
            buffer_child.setChildId("buffer")
            buffer_child.setDescription("Buffer")
            buffer_child.setPosition(QPointF(250, 0))
            buffer_child.setActive(False)
            buffer_child.setConfiguration({"custom_mode": "preserve-me"})
            mixed_model.addChildAlgorithm(buffer_child)
            merge_child = QgsProcessingModelChildAlgorithm(
                "native:mergevectorlayers"
            )
            merge_child.setChildId("merge")
            merge_child.setDescription("Merge")
            merge_child.setPosition(QPointF(500, 0))
            merge_child.addParameterSources(
                "LAYERS",
                [
                    QgsProcessingModelChildParameterSource.fromStaticValue(
                        "static-layer-a"
                    ),
                    QgsProcessingModelChildParameterSource.fromModelParameter(
                        "VECTOR_INPUT"
                    ),
                    QgsProcessingModelChildParameterSource.fromChildOutput(
                        "buffer", "OUTPUT"
                    ),
                    QgsProcessingModelChildParameterSource.fromStaticValue(
                        "static-layer-b"
                    ),
                ],
            )
            merge_child.setDependencies(
                [QgsProcessingModelChildDependency("buffer", "OUTPUT")]
            )
            public_output = QgsProcessingModelOutput(
                "MERGED_RESULT", "Merged public result"
            )
            public_output.setChildId("merge")
            public_output.setChildOutputName("OUTPUT")
            public_output.setMandatory(True)
            public_output.setDefaultValue("temporary")
            merge_child.setModelOutputs({"MERGED_RESULT": public_output})
            mixed_model.addChildAlgorithm(merge_child)
            if not mixed_model.toFile(str(mixed_path)):
                raise RuntimeError("Could not write the mixed-source fixture.")

            mixed_graph, error = Model3Serializer.import_from_model3(str(mixed_path))
            if mixed_graph is None:
                raise RuntimeError(error or "Mixed-source .model3 import failed.")
            merge_node = mixed_graph.nodes["merge"]
            imported_buffer = mixed_graph.nodes["buffer"]
            if (
                imported_buffer.is_active
                or imported_buffer.algorithm_configuration
                != {"custom_mode": "preserve-me"}
            ):
                raise RuntimeError("Child active/configuration state was not imported.")
            source_order = merge_node.parameter_source_order.get("LAYERS", [])
            source_kinds = [source.get("kind") for source in source_order]
            if source_kinds != ["static", "edge", "edge", "static"]:
                raise RuntimeError(f"Mixed source order changed: {source_order}")
            if merge_node.parameters.get("LAYERS") != [
                "static-layer-a",
                "static-layer-b",
            ]:
                raise RuntimeError("Mixed static sources were lost or reordered.")
            if (
                merge_node.dependencies != ["buffer"]
                or merge_node.dependency_branches.get("buffer") != "OUTPUT"
            ):
                raise RuntimeError("Conditional child dependency was not preserved.")
            output_contract = mixed_graph.outputs.get("MERGED_RESULT", {})
            if (
                output_contract.get("node_id") != "merge"
                or output_contract.get("output_name") != "OUTPUT"
                or not output_contract.get("mandatory")
                or output_contract.get("default") != "temporary"
            ):
                raise RuntimeError("Declared model output metadata was not preserved.")
            ok, error = Model3Serializer.export_to_model3(
                mixed_graph,
                str(mixed_roundtrip_path),
                allow_invalid=True,
            )
            if not ok:
                raise RuntimeError(error)
            mixed_reopened = QgsProcessingModelAlgorithm()
            if not mixed_reopened.fromFile(str(mixed_roundtrip_path)):
                raise RuntimeError("Mixed round-trip .model3 could not be reopened.")
            reopened_merge = mixed_reopened.childAlgorithms()["merge"]
            reopened_buffer = mixed_reopened.childAlgorithms()["buffer"]
            if (
                reopened_buffer.isActive()
                or dict(reopened_buffer.configuration())
                != {"custom_mode": "preserve-me"}
            ):
                raise RuntimeError("Child active/configuration state changed in round-trip.")
            reopened_sources = reopened_merge.parameterSources()["LAYERS"]
            reopened_source_kinds = [
                int(source.source()) for source in reopened_sources
            ]
            expected_source_kinds = [
                int(Qgis.ProcessingModelChildParameterSource.StaticValue),
                int(Qgis.ProcessingModelChildParameterSource.ModelParameter),
                int(Qgis.ProcessingModelChildParameterSource.ChildOutput),
                int(Qgis.ProcessingModelChildParameterSource.StaticValue),
            ]
            if reopened_source_kinds != expected_source_kinds:
                raise RuntimeError(
                    "Native mixed parameter source order was not preserved."
                )
            reopened_dependencies = reopened_merge.dependencies()
            dependency = reopened_dependencies[0] if reopened_dependencies else None
            dependency_child = getattr(dependency, "childId", "")
            dependency_branch = getattr(
                dependency, "conditionalBranch", ""
            )
            dependency_child = (
                dependency_child()
                if callable(dependency_child)
                else dependency_child
            )
            dependency_branch = (
                dependency_branch()
                if callable(dependency_branch)
                else dependency_branch
            )
            if (
                len(reopened_dependencies) != 1
                or dependency_child != "buffer"
                or dependency_branch != "OUTPUT"
            ):
                raise RuntimeError("Native dependency metadata changed in round-trip.")
            reopened_output = reopened_merge.modelOutputs().get("MERGED_RESULT")
            if (
                reopened_output is None
                or reopened_output.childOutputName() != "OUTPUT"
                or not reopened_output.isMandatory()
                or reopened_output.defaultValue() != "temporary"
            ):
                raise RuntimeError("Native model output metadata changed in round-trip.")
        finally:
            configured_path.unlink(missing_ok=True)
            configured_roundtrip_path.unlink(missing_ok=True)
            typed_path.unlink(missing_ok=True)
            typed_roundtrip_path.unlink(missing_ok=True)
            mixed_path.unlink(missing_ok=True)
            mixed_roundtrip_path.unlink(missing_ok=True)
            for layer_id in set(project.mapLayers()) - original_layer_ids:
                project.removeMapLayer(layer_id)
            QgsApplication.processingRegistry().removeProvider(fixture_provider)

        # -- Owner-QA follow-up: an unfinished workflow must still save -------
        # The reported failure: a workflow whose inputs were not bound yet
        # refused to export at all, so the work was unsavable. Unbound required
        # inputs now become model inputs and a literal the algorithm rejects is
        # dropped rather than poisoning the whole model.
        rough = GraphModel("Unfinished workflow")
        rough_buffer = AlgorithmCatalog.create_node("native:buffer", title="Buffer step")
        rough_buffer.parameters["END_CAP_STYLE"] = "not-an-enum-index"
        rough.add_node(rough_buffer)
        rough_centroids = AlgorithmCatalog.create_node("native:centroids", title="Centres")
        rough.add_node(rough_centroids)
        if rough.add_edge(
            rough_buffer.node_id, "OUTPUT", rough_centroids.node_id, "INPUT"
        ) is None:
            raise RuntimeError(rough.last_error)

        native_model, fatal, issues = Model3Serializer.build_native_model(rough)
        if fatal:
            raise RuntimeError(fatal)
        if issues:
            raise RuntimeError(
                "An unfinished workflow still reports validation issues: "
                + "; ".join(issues)
            )
        promoted = {
            definition.name() for definition in native_model.parameterDefinitions()
        }
        if not any(name.endswith("_INPUT") for name in promoted):
            raise RuntimeError("The unbound required INPUT did not become a model input.")

        rough_path = Path(__file__).with_name("_smoke_rough.model3")
        python_path = Path(__file__).with_name("_smoke_rough.py")
        try:
            ok, error = Model3Serializer.export_to_model3(rough, str(rough_path))
            if not ok:
                raise RuntimeError(f"An unfinished workflow refused to save: {error}")
            reopened, error = Model3Serializer.import_from_model3(str(rough_path))
            if reopened is None:
                raise RuntimeError(error or "The saved unfinished model did not reopen.")
            ok, error = Model3Serializer.export_to_python(rough, str(python_path))
            if not ok:
                raise RuntimeError(f"Python export failed: {error}")
            exported_source = python_path.read_text(encoding="utf-8")
            if "QgsProcessingAlgorithm" not in exported_source:
                raise RuntimeError("The exported Python is not a Processing algorithm.")
            compile(exported_source, str(python_path), "exec")
        finally:
            rough_path.unlink(missing_ok=True)
            python_path.unlink(missing_ok=True)

        # -- Owner-QA follow-up: Run shows the whole flow, not one modal/node --
        original_rough_parameters = dict(rough_buffer.parameters)
        original_rough_sources = {
            name: list(sources)
            for name, sources in rough_buffer.parameter_source_order.items()
        }
        original_rough_dirty = rough_buffer.is_dirty
        setup = RunSetupDialog(rough)
        try:
            if [node.title for node, _form in setup._forms] != ["Buffer step", "Centres"]:
                raise RuntimeError("Run setup did not list every step in run order.")
            if "Buffer step" not in setup._missing_by_node(setup._collect_all()):
                raise RuntimeError("Run setup did not report the unset required input.")
            setup.show_all_check.setChecked(True)
            if len(setup._forms) != 2:
                raise RuntimeError("Run setup lost a step when showing all parameters.")
            mutated = dict(rough_buffer.parameters)
            mutated["DISTANCE"] = 999
            setup._forms[0][1].apply(mutated)
            setup.reject()
            if (
                rough_buffer.parameters != original_rough_parameters
                or rough_buffer.parameter_source_order
                != original_rough_sources
                or rough_buffer.is_dirty != original_rough_dirty
            ):
                raise RuntimeError(
                    "Cancelling run setup did not restore the complete node state."
                )
        finally:
            setup.deleteLater()

        unavailable_graph = GraphModel("Unavailable algorithm")
        unavailable_node = AlgorithmCatalog.create_node(
            "native:buffer", "unavailable_step", "Unavailable step"
        )
        unavailable_node.algorithm_id = "missing_provider:missing_algorithm"
        unavailable_graph.add_node(unavailable_node)
        unavailable_setup = RunSetupDialog(unavailable_graph)
        try:
            if (
                unavailable_setup.run_button.isEnabled()
                or "missing" not in unavailable_setup.summary_label.text().lower()
            ):
                raise RuntimeError(
                    "Run setup presented an unavailable algorithm as ready."
                )
        finally:
            unavailable_setup.deleteLater()

        return f"QGIS {Qgis.QGIS_VERSION}: {len(records)} algorithms; smoke test passed"


class SmartModelerSmokeAlgorithm(QgsProcessingAlgorithm):
    """Makes the smoke suite runnable through QGIS' qgis_process executable."""

    def name(self) -> str:
        return "smartmodeler_smoke"

    def displayName(self) -> str:
        return "SmartModeler smoke test"

    def group(self) -> str:
        return "Tests"

    def groupId(self) -> str:
        return "tests"

    def createInstance(self):
        return SmartModelerSmokeAlgorithm()

    def initAlgorithm(self, _configuration=None) -> None:
        self.addOutput(QgsProcessingOutputString("RESULT", "Smoke test result"))

    def processAlgorithm(self, _parameters, _context, _feedback):
        return {"RESULT": run_checks()}


class ConfiguredSchemaAlgorithm(QgsProcessingAlgorithm):
    """Changes its live signature from the supplied configuration map."""

    def name(self) -> str:
        return "configured_schema"

    def displayName(self) -> str:
        return "Configured schema fixture"

    def group(self) -> str:
        return "Tests"

    def groupId(self) -> str:
        return "tests"

    def createInstance(self):
        return ConfiguredSchemaAlgorithm()

    def initAlgorithm(self, configuration=None) -> None:
        if dict(configuration or {}).get("mode") == "text":
            self.addParameter(QgsProcessingParameterString("TEXT", "Text"))
            self.addOutput(QgsProcessingOutputString("TEXT_OUTPUT", "Text output"))
        else:
            self.addParameter(QgsProcessingParameterNumber("NUMBER", "Number"))
            self.addOutput(
                QgsProcessingOutputNumber("NUMBER_OUTPUT", "Number output")
            )

    def processAlgorithm(self, parameters, _context, _feedback):
        if self.parameterDefinition("TEXT") is not None:
            return {"TEXT_OUTPUT": parameters.get("TEXT", "")}
        return {"NUMBER_OUTPUT": parameters.get("NUMBER", 0)}


class SilentCancelableAlgorithm(QgsProcessingAlgorithm):
    """Long-running fixture which deliberately emits no progress updates."""

    def name(self) -> str:
        return "silent_cancel"

    def displayName(self) -> str:
        return "Progressless cancellation fixture"

    def group(self) -> str:
        return "Tests"

    def groupId(self) -> str:
        return "tests"

    def createInstance(self):
        return SilentCancelableAlgorithm()

    def initAlgorithm(self, _configuration=None) -> None:
        self.addOutput(QgsProcessingOutputNumber("RESULT", "Result"))

    def processAlgorithm(self, _parameters, _context, feedback):
        started = time.monotonic()
        for iteration in range(500):
            if feedback.isCanceled():
                SILENT_CANCEL_OBSERVATIONS.append(
                    ("canceled", time.monotonic() - started, iteration)
                )
                return {"RESULT": -1}
            time.sleep(0.01)
        SILENT_CANCEL_OBSERVATIONS.append(
            ("completed", time.monotonic() - started, 500)
        )
        return {"RESULT": 1}


class ConfiguredSchemaProvider(QgsProcessingProvider):
    """Temporary provider used only by the real-QGIS smoke suite."""

    def id(self) -> str:
        return "smartmodeler_fixture"

    def name(self) -> str:
        return "SmartModeler fixture"

    def loadAlgorithms(self) -> None:
        self.addAlgorithm(ConfiguredSchemaAlgorithm())
        self.addAlgorithm(SilentCancelableAlgorithm())


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QgsApplication([], False)
    application.initQgis()
    # After initQgis the prefix is known; the bundled Processing framework
    # lives under <prefix>/python/plugins and is not on sys.path by default
    # when this script is launched directly through python-qgis(-ltr).bat.
    plugins_path = os.path.join(QgsApplication.prefixPath(), "python", "plugins")
    if plugins_path not in sys.path:
        sys.path.append(plugins_path)
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
        try:
            print(run_checks())
        except Exception:
            # Emit the real failed assertion before Qt/QGIS shutdown. Some
            # failure paths keep a worker alive long enough that waiting for
            # interpreter-final traceback output hides the actual cause.
            traceback.print_exc()
            raise
        return 0
    finally:
        task_deadline = time.monotonic() + 10.0
        while (
            QgsApplication.taskManager().countActiveTasks()
            and time.monotonic() < task_deadline
        ):
            QApplication.processEvents()
            time.sleep(0.01)
        for widget in QApplication.topLevelWidgets():
            prepare_for_shutdown = getattr(
                widget, "prepare_for_shutdown", None
            )
            if callable(prepare_for_shutdown):
                prepare_for_shutdown()
            widget.close()
            widget.deleteLater()
        QApplication.sendPostedEvents(
            None, QEvent.Type.DeferredDelete
        )
        QApplication.processEvents()
        QgsProject.instance().clear()
        application.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
