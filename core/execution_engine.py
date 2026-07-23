"""Sequential QGIS Processing executor for validated SmartModeler DAGs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List

from qgis.PyQt.QtCore import QObject, pyqtSignal
from qgis.core import (
    QgsApplication,
    QgsMapLayer,
    QgsProcessing,
    QgsProcessingContext,
    QgsProcessingException,
    QgsProcessingFeedback,
    QgsProcessingUtils,
    QgsProject,
)

from .algorithm_catalog import AlgorithmCatalog
from .graph_model import GraphModel, GraphValidationError, NodeDefinition, SocketType


class ExecutionError(RuntimeError):
    """User-facing graph execution failure."""


@dataclass
class ExecutionReport:
    executed_nodes: int
    added_layers: List[str]
    results: Dict[str, Dict[str, Any]]


class GraphExecutionEngine(QObject):
    """Executes graph nodes in topological order using live Processing providers."""

    node_state_changed = pyqtSignal(str, str, str)
    progress_changed = pyqtSignal(int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.feedback: QgsProcessingFeedback | None = None

    def cancel(self) -> None:
        if self.feedback is not None:
            self.feedback.cancel()

    def execute(self, graph: GraphModel) -> ExecutionReport:
        if not graph.nodes:
            raise ExecutionError("The workflow is empty.")
        AlgorithmCatalog.autobind_unique_project_layers(graph)
        issues = [issue for issue in graph.validate() if issue.level == "error"]
        if issues:
            details = "\n".join(
                f"- {graph.nodes[issue.node_id].title if issue.node_id in graph.nodes else 'Graph'}: {issue.message}"
                for issue in issues
            )
            raise ExecutionError(f"Workflow validation failed:\n{details}")
        try:
            order = graph.get_topological_order()
        except GraphValidationError as error:
            raise ExecutionError(str(error)) from error

        project = QgsProject.instance()
        context = QgsProcessingContext()
        context.setProject(project)
        context.setTransformContext(project.transformContext())
        self.feedback = QgsProcessingFeedback()
        context.setFeedback(self.feedback)
        all_results: Dict[str, Dict[str, Any]] = {}
        for node in graph.nodes.values():
            self._set_state(node, "idle", "")

        try:
            for index, node in enumerate(order):
                if self.feedback.isCanceled():
                    raise ExecutionError("Workflow execution was canceled.")
                percent = int(index * 100 / max(len(order), 1))
                self.progress_changed.emit(percent, f"Running {node.title}")
                self._set_state(node, "running", "Running")
                try:
                    if node.algorithm_id.startswith("smart:"):
                        results = self._execute_smart_node(node, project)
                    else:
                        results = self._execute_processing_node(
                            node, graph, all_results, context
                        )
                except (QgsProcessingException, RuntimeError, ValueError) as error:
                    self._set_state(node, "error", str(error))
                    raise ExecutionError(f"{node.title}: {error}") from error
                node.cached_results = results
                node.is_dirty = False
                all_results[node.node_id] = results
                self._set_state(node, "success", "Completed")

            try:
                added = self._load_terminal_outputs(
                    graph, all_results, context, project
                )
            except (QgsProcessingException, RuntimeError, ValueError) as error:
                raise ExecutionError(f"Could not load workflow outputs: {error}") from error
            self.progress_changed.emit(100, "Workflow complete")
            return ExecutionReport(len(order), added, all_results)
        finally:
            self.feedback = None

    def _set_state(self, node: NodeDefinition, state: str, message: str) -> None:
        node.execution_state = state
        node.execution_message = message
        self.node_state_changed.emit(node.node_id, state, message)

    @staticmethod
    def _execute_smart_node(node: NodeDefinition, project: QgsProject) -> Dict[str, Any]:
        if node.algorithm_id in ("smart:number", "smart:slider"):
            try:
                return {"OUTPUT": float(node.parameters.get("VALUE", 0.0))}
            except (TypeError, ValueError) as error:
                raise ExecutionError("Numeric input VALUE is invalid.") from error

        layer_ref = str(node.parameters.get("LAYER", "")).strip()
        expected = (
            SocketType.RASTER
            if node.algorithm_id == "smart:raster_layer"
            else SocketType.VECTOR
        )
        layer = project.mapLayer(layer_ref) if layer_ref else None
        if layer is None and layer_ref:
            matches = project.mapLayersByName(layer_ref)
            layer = matches[0] if matches else None
        if layer is None:
            choices = AlgorithmCatalog.layer_choices(expected)
            if len(choices) == 1:
                layer = project.mapLayer(next(iter(choices)))
        if layer is None:
            raise ExecutionError("Select an input layer in the node parameters.")
        return {"OUTPUT": layer}

    @staticmethod
    def _execute_processing_node(
        node: NodeDefinition,
        graph: GraphModel,
        all_results: Dict[str, Dict[str, Any]],
        context: QgsProcessingContext,
    ) -> Dict[str, Any]:
        registry = QgsApplication.processingRegistry()
        algorithm = registry.createAlgorithmById(node.algorithm_id)
        if algorithm is None:
            raise ExecutionError(
                f"Processing algorithm is unavailable: {node.algorithm_id}"
            )
        parameters = dict(node.parameters)
        parameters.pop("alg_id", None)
        for edge in graph.incoming_edges(node.node_id):
            source_results = all_results.get(edge.start_node_id, {})
            if edge.start_port_id not in source_results:
                raise ExecutionError(
                    f"Upstream output is missing: {edge.start_node_id}.{edge.start_port_id}"
                )
            value = source_results[edge.start_port_id]
            target_port = node.inputs[edge.end_port_id]
            if target_port.allows_multiple:
                current = parameters.get(edge.end_port_id, [])
                if current in (None, ""):
                    current = []
                elif not isinstance(current, list):
                    current = [current]
                else:
                    current = list(current)
                current.append(value)
                parameters[edge.end_port_id] = current
            else:
                parameters[edge.end_port_id] = value

        for destination in algorithm.destinationParameterDefinitions():
            if parameters.get(destination.name()) in (None, ""):
                parameters[destination.name()] = QgsProcessing.TEMPORARY_OUTPUT
        valid, message = algorithm.checkParameterValues(parameters, context)
        if not valid:
            raise ExecutionError(message or "Processing parameters are invalid.")

        import processing

        results = processing.run(
            algorithm,
            parameters,
            feedback=context.feedback(),
            context=context,
            is_child_algorithm=True,
        )
        if not isinstance(results, dict):
            raise ExecutionError("Processing algorithm returned no result map.")
        return results

    @staticmethod
    def _load_terminal_outputs(
        graph: GraphModel,
        all_results: Dict[str, Dict[str, Any]],
        context: QgsProcessingContext,
        project: QgsProject,
    ) -> List[str]:
        added: List[str] = []
        terminal_ids = {
            node_id
            for node_id in graph.nodes
            if not any(True for _edge in graph.outgoing_edges(node_id))
        }
        for node_id in terminal_ids:
            node = graph.nodes[node_id]
            for output_name, value in all_results.get(node_id, {}).items():
                layer: QgsMapLayer | None
                if isinstance(value, QgsMapLayer):
                    layer = value
                elif isinstance(value, str):
                    layer = context.getMapLayer(value)
                    if layer is None:
                        layer = QgsProcessingUtils.mapLayerFromString(value, context, True)
                else:
                    layer = None
                if layer is None or project.mapLayer(layer.id()) is not None:
                    continue
                owned = context.takeResultLayer(layer.id())
                if owned is not None:
                    layer = owned
                layer.setName(f"{node.title} - {output_name}")
                project.addMapLayer(layer)
                added.append(layer.name())
        return added
