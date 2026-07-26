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
        skipped = set()
        for node in order:
            if not node.is_active:
                skipped.update(self._dependent_nodes(graph, node.node_id))
                skipped.add(node.node_id)
        for node in graph.nodes.values():
            self._set_state(node, "idle", "")

        try:
            for index, node in enumerate(order):
                if self.feedback.isCanceled():
                    raise ExecutionError("Workflow execution was canceled.")
                percent = int(index * 100 / max(len(order), 1))
                if node.node_id in skipped:
                    all_results[node.node_id] = {}
                    self._set_state(node, "skipped", "Skipped")
                    continue
                false_branch = any(
                    branch
                    and not bool(
                        all_results.get(dependency, {}).get(branch, False)
                    )
                    for dependency in node.dependencies
                    for branch in [node.dependency_branches.get(dependency, "")]
                )
                if false_branch:
                    skipped.add(node.node_id)
                    skipped.update(self._dependent_nodes(graph, node.node_id))
                    all_results[node.node_id] = {}
                    self._set_state(node, "skipped", "Conditional branch not selected")
                    continue
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
            return ExecutionReport(len(order) - len(skipped), added, all_results)
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
        if node.algorithm_id == "smart:boolean":
            return {"OUTPUT": bool(node.parameters.get("VALUE", False))}
        if node.algorithm_id in (
            "smart:string",
            "smart:field",
            "smart:crs",
            "smart:extent",
            "smart:enum",
        ):
            return {"OUTPUT": node.parameters.get("VALUE")}

        layer_value = node.parameters.get("LAYER", "")
        if node.algorithm_id in ("smart:multiple_vector", "smart:multiple_raster"):
            refs = layer_value if isinstance(layer_value, list) else [layer_value]
            layers = []
            for reference in refs:
                layer = project.mapLayer(str(reference).strip())
                if layer is None:
                    raise ExecutionError("A collection input layer is unavailable.")
                layers.append(layer)
            return {"OUTPUT": layers}

        layer_ref = str(layer_value).strip()
        expected = (
            SocketType.RASTER
            if node.algorithm_id in ("smart:raster_layer", "smart:multiple_raster")
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
        algorithm = registry.createAlgorithmById(
            node.algorithm_id, node.algorithm_configuration
        )
        if algorithm is None:
            raise ExecutionError(
                f"Processing algorithm is unavailable: {node.algorithm_id}"
            )
        parameters = dict(node.parameters)
        parameters.pop("alg_id", None)
        for input_name, order in node.parameter_source_order.items():
            values = []
            for source in order:
                if source.get("kind") == "static":
                    values.append(source.get("value"))
                    continue
                source_results = all_results.get(source.get("node_id"), {})
                output_name = source.get("output_name")
                if output_name not in source_results:
                    raise ExecutionError(
                        "An ordered upstream output is unavailable."
                    )
                value = source_results[output_name]
                if isinstance(value, list):
                    values.extend(value)
                else:
                    values.append(value)
            port = node.inputs.get(input_name)
            if port is not None and port.allows_multiple:
                parameters[input_name] = values
            elif values:
                parameters[input_name] = values[-1]
        for edge in graph.incoming_edges(node.node_id):
            if edge.end_port_id in node.parameter_source_order:
                continue
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
                if isinstance(value, list):
                    current.extend(value)
                else:
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
        if graph.outputs_declared:
            output_contracts = [
                (
                    public_name,
                    str(contract.get("node_id", "")),
                    str(contract.get("output_name", "")),
                    bool(contract.get("mandatory", False)),
                )
                for public_name, contract in graph.outputs.items()
            ]
        else:
            output_contracts = [
                (output_name, node_id, output_name, False)
                for node_id, node in graph.nodes.items()
                if not any(True for _edge in graph.outgoing_edges(node_id))
                for output_name in all_results.get(node_id, {})
                if graph.output_is_publishable(node, output_name)
            ]
        resolved_outputs = []
        for public_name, node_id, output_name, mandatory in output_contracts:
            node = graph.nodes.get(node_id)
            if (
                node is None
                or not graph.output_is_publishable(node, output_name)
            ):
                if mandatory:
                    raise ExecutionError(
                        f"Mandatory workflow output is unavailable: {public_name}"
                    )
                continue
            value = all_results.get(node_id, {}).get(output_name)
            layer: QgsMapLayer | None
            if isinstance(value, QgsMapLayer):
                layer = value
            elif isinstance(value, str):
                layer = context.getMapLayer(value)
                if layer is None:
                    layer = QgsProcessingUtils.mapLayerFromString(value, context, True)
            else:
                layer = None
            if layer is None:
                if mandatory:
                    raise ExecutionError(
                        f"Mandatory workflow output is unavailable: {public_name}"
                    )
                continue
            resolved_outputs.append((public_name, output_name, node, layer))

        # Validate the entire declared contract before mutating the project.
        for public_name, output_name, node, layer in resolved_outputs:
            if project.mapLayer(layer.id()) is not None:
                continue
            owned = context.takeResultLayer(layer.id())
            if owned is not None:
                layer = owned
            layer.setName(
                public_name
                if graph.outputs_declared
                else f"{node.title} - {output_name}"
            )
            project.addMapLayer(layer)
            added.append(layer.name())
        return added

    @staticmethod
    def _dependent_nodes(graph: GraphModel, node_id: str) -> set:
        """Return all data-flow and explicit-dependency descendants."""
        descendants = set()
        queue = [node_id]
        while queue:
            current = queue.pop(0)
            followers = {
                edge.end_node_id for edge in graph.outgoing_edges(current)
            }
            followers.update(
                candidate_id
                for candidate_id, candidate in graph.nodes.items()
                if current in candidate.dependencies
            )
            for follower in followers:
                if follower not in descendants:
                    descendants.add(follower)
                    queue.append(follower)
        descendants.discard(node_id)
        return descendants
