"""Sequential QGIS Processing executor for validated SmartModeler DAGs."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from qgis.PyQt.QtCore import QObject, QThread, QTimer, pyqtSignal
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsMapLayer,
    QgsMessageLog,
    QgsProcessing,
    QgsProcessingAlgRunnerTask,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProcessingUtils,
    QgsProject,
)

from .algorithm_catalog import AlgorithmCatalog
from .document_codec import DocumentCodecError, GraphDocumentCodec
from .graph_model import GraphModel, GraphValidationError, NodeDefinition, SocketType


class ExecutionError(RuntimeError):
    """User-facing graph execution failure."""


class ExecutionStatus:
    PREPARED = "prepared"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    PARTIAL = "partial"


@dataclass
class ExecutionReport:
    status: str
    total_nodes: int
    executed_nodes: int = 0
    skipped_nodes: int = 0
    added_layers: List[str] = field(default_factory=list)
    added_layer_ids: List[str] = field(default_factory=list)
    results: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    failed_node_id: str = ""
    message: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == ExecutionStatus.COMPLETED


class GraphExecutionEngine(QObject):
    """Executes graph nodes in topological order using live Processing providers."""

    node_state_changed = pyqtSignal(str, str, str)
    progress_changed = pyqtSignal(int, str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.feedback: QgsProcessingFeedback | None = None
        self._running = False
        self._cancel_requested = False
        self._step_index = 0
        self._step_total = 1
        self._display_graph: GraphModel | None = None
        self._pending_output = None
        self._async_state: Dict[str, Any] | None = None
        self._async_task: QgsProcessingAlgRunnerTask | None = None

    def is_running(self) -> bool:
        return self._running

    def cancel(self) -> None:
        self._cancel_requested = True
        if self._async_task is not None:
            self._async_task.cancel()
        if self.feedback is not None:
            self.feedback.cancel()

    def start_async(
        self,
        graph: GraphModel,
        on_finished: Callable[[ExecutionReport], None],
        *,
        display_graph: GraphModel | None = None,
        context: QgsProcessingContext | None = None,
        project: QgsProject | None = None,
        feedback: QgsProcessingFeedback | None = None,
        layer_lookup: Dict[str, QgsMapLayer] | None = None,
        algorithm_lookup: Dict[str, Any] | None = None,
    ) -> None:
        """Run a prepared DAG through QGIS' thread-safe algorithm task API."""
        if self._running:
            QTimer.singleShot(
                0,
                lambda: on_finished(
                    ExecutionReport(
                        ExecutionStatus.FAILED,
                        len(graph.nodes),
                        message="A workflow execution is already running.",
                    )
                ),
            )
            return
        if not graph.nodes:
            QTimer.singleShot(
                0,
                lambda: on_finished(
                    ExecutionReport(
                        ExecutionStatus.FAILED,
                        0,
                        message="The workflow is empty.",
                    )
                ),
            )
            return
        issues = [issue for issue in graph.validate() if issue.level == "error"]
        if issues:
            details = "\n".join(
                f"- {graph.nodes[issue.node_id].title if issue.node_id in graph.nodes else 'Graph'}: {issue.message}"
                for issue in issues
            )
            QTimer.singleShot(
                0,
                lambda: on_finished(
                    ExecutionReport(
                        ExecutionStatus.FAILED,
                        len(graph.nodes),
                        message=f"Workflow validation failed:\n{details}",
                    )
                ),
            )
            return
        try:
            order = graph.get_topological_order()
        except GraphValidationError as error:
            error_text = str(error)
            QTimer.singleShot(
                0,
                lambda message=error_text: on_finished(
                    ExecutionReport(
                        ExecutionStatus.FAILED,
                        len(graph.nodes),
                        message=message,
                    )
                ),
            )
            return

        project = project or QgsProject.instance()
        if context is None:
            context = QgsProcessingContext()
            context.setProject(project)
            context.setTransformContext(project.transformContext())
        self.feedback = feedback or QgsProcessingFeedback()
        self.feedback.progressChanged.connect(self._algorithm_progress_changed)
        context.setFeedback(self.feedback)
        skipped = set()
        for node in order:
            if not node.is_active:
                skipped.update(self._dependent_nodes(graph, node.node_id))
                skipped.add(node.node_id)
        self._display_graph = display_graph or graph
        self._pending_output = None
        self._running = True
        self._step_index = 0
        self._step_total = max(len(order), 1)
        self._async_state = {
            "graph": graph,
            "order": order,
            "index": 0,
            "results": {},
            "skipped": skipped,
            "executed": 0,
            "skipped_count": 0,
            "context": context,
            "project": project,
            "layer_lookup": layer_lookup,
            "algorithm_lookup": algorithm_lookup or {},
            "on_finished": on_finished,
        }
        for node in graph.nodes.values():
            self._set_state(node, "idle", "")
        QTimer.singleShot(0, self._run_next_async)

    def _run_next_async(self) -> None:
        state = self._async_state
        if state is None:
            return
        order = state["order"]
        while state["index"] < len(order):
            index = state["index"]
            node = order[index]
            self._step_index = index
            if self._is_canceled():
                self._finish_async(
                    self._canceled_report(
                        order,
                        state["executed"],
                        state["skipped_count"],
                        state["results"],
                        node.node_id,
                    )
                )
                return
            state["index"] += 1
            if node.node_id in state["skipped"]:
                state["results"][node.node_id] = {}
                state["skipped_count"] += 1
                self._set_state(node, "skipped", "Skipped")
                continue
            false_branch = any(
                branch
                and not bool(
                    state["results"].get(dependency, {}).get(branch, False)
                )
                for dependency in node.dependencies
                for branch in [node.dependency_branches.get(dependency, "")]
            )
            if false_branch:
                state["skipped"].add(node.node_id)
                state["skipped"].update(
                    self._dependent_nodes(state["graph"], node.node_id)
                )
                state["results"][node.node_id] = {}
                state["skipped_count"] += 1
                self._set_state(
                    node, "skipped", "Conditional branch not selected"
                )
                continue

            percent = int(index * 100 / max(len(order), 1))
            self.progress_changed.emit(percent, f"Running {node.title}")
            if self._is_canceled():
                self._finish_async(
                    self._canceled_report(
                        order,
                        state["executed"],
                        state["skipped_count"],
                        state["results"],
                        node.node_id,
                    )
                )
                return
            self._set_state(node, "running", "Running")
            if node.algorithm_id.startswith("smart:"):
                try:
                    results = self._execute_smart_node(
                        node, state["project"], state["layer_lookup"]
                    )
                except Exception as error:
                    self._finish_async(self._async_error_report(node, error))
                    return
                self._accept_async_results(node, results)
                continue

            try:
                algorithm, parameters = self._prepare_processing_node(
                    node,
                    state["graph"],
                    state["results"],
                    state["context"],
                    state["algorithm_lookup"].get(node.node_id),
                )
            except Exception as error:
                self._finish_async(self._async_error_report(node, error))
                return
            task = QgsProcessingAlgRunnerTask(
                algorithm,
                parameters,
                state["context"],
                self.feedback,
            )
            task.executed.connect(
                lambda successful, results, node_id=node.node_id: (
                    self._processing_task_finished(
                        node_id, successful, results
                    )
                )
            )
            self._async_task = task
            QgsApplication.taskManager().addTask(task)
            return

        self._pending_output = (
            state["graph"],
            state["results"],
            state["context"],
            state["project"],
        )
        self._finish_async(
            ExecutionReport(
                ExecutionStatus.PREPARED,
                len(order),
                state["executed"],
                state["skipped_count"],
                results=self._summarize_results(state["results"]),
                message="Workflow results are ready to commit.",
            )
        )

    def _processing_task_finished(
        self, node_id: str, successful: bool, results: Dict[str, Any]
    ) -> None:
        state = self._async_state
        self._async_task = None
        if state is None:
            return
        node = state["graph"].nodes.get(node_id)
        if node is None:
            self._finish_async(
                ExecutionReport(
                    ExecutionStatus.PARTIAL,
                    len(state["order"]),
                    state["executed"],
                    state["skipped_count"],
                    results=self._summarize_results(state["results"]),
                    message="The active workflow node disappeared.",
                )
            )
            return
        if self._is_canceled():
            self._set_state(node, "canceled", "Canceled")
            self._finish_async(
                self._canceled_report(
                    state["order"],
                    state["executed"],
                    state["skipped_count"],
                    state["results"],
                    node.node_id,
                )
            )
            return
        if not successful or not isinstance(results, dict):
            self._finish_async(
                self._async_error_report(
                    node,
                    ExecutionError(
                        "Processing algorithm failed without a result map."
                    ),
                )
            )
            return
        self._accept_async_results(node, results)
        QTimer.singleShot(0, self._run_next_async)

    def _accept_async_results(
        self, node: NodeDefinition, results: Dict[str, Any]
    ) -> None:
        state = self._async_state
        if state is None:
            return
        node.cached_results = self._summarize_node_results(results)
        node.is_dirty = False
        state["results"][node.node_id] = results
        state["executed"] += 1
        self._set_state(node, "success", "Completed")

    def _async_error_report(
        self, node: NodeDefinition, error: Exception
    ) -> ExecutionReport:
        state = self._async_state
        if state is None:
            return ExecutionReport(
                ExecutionStatus.FAILED, 0, message=str(error)
            )
        self._set_state(node, "error", str(error))
        return ExecutionReport(
            (
                ExecutionStatus.PARTIAL
                if state["executed"]
                else ExecutionStatus.FAILED
            ),
            len(state["order"]),
            state["executed"],
            state["skipped_count"],
            results=self._summarize_results(state["results"]),
            failed_node_id=node.node_id,
            message=f"{node.title}: {error}",
        )

    def _finish_async(self, report: ExecutionReport) -> None:
        state = self._async_state
        if state is None:
            return
        callback = state["on_finished"]
        if self.feedback is not None:
            try:
                self.feedback.progressChanged.disconnect(
                    self._algorithm_progress_changed
                )
            except (RuntimeError, TypeError):
                pass
        self.feedback = None
        self._async_task = None
        self._async_state = None
        self._running = False
        self._display_graph = None
        callback(report)

    def execute(
        self,
        graph: GraphModel,
        defer_output_commit: bool = False,
        *,
        prepared: bool = False,
        display_graph: GraphModel | None = None,
        context: QgsProcessingContext | None = None,
        project: QgsProject | None = None,
        feedback: QgsProcessingFeedback | None = None,
        layer_lookup: Dict[str, QgsMapLayer] | None = None,
        algorithm_lookup: Dict[str, Any] | None = None,
    ) -> ExecutionReport:
        if self._running:
            return ExecutionReport(
                ExecutionStatus.FAILED,
                len(graph.nodes),
                message="A workflow execution is already running.",
            )
        if not graph.nodes:
            return ExecutionReport(
                ExecutionStatus.FAILED,
                0,
                message="The workflow is empty.",
            )
        original_graph = display_graph or graph
        if not prepared:
            AlgorithmCatalog.autobind_unique_project_layers(graph)
        issues = [issue for issue in graph.validate() if issue.level == "error"]
        if issues:
            details = "\n".join(
                f"- {graph.nodes[issue.node_id].title if issue.node_id in graph.nodes else 'Graph'}: {issue.message}"
                for issue in issues
            )
            return ExecutionReport(
                ExecutionStatus.FAILED,
                len(graph.nodes),
                message=f"Workflow validation failed:\n{details}",
            )
        try:
            order = graph.get_topological_order()
        except GraphValidationError as error:
            return ExecutionReport(
                ExecutionStatus.FAILED,
                len(graph.nodes),
                message=str(error),
            )
        if prepared:
            execution_graph = graph
        else:
            try:
                execution_graph = GraphDocumentCodec.decode(
                    GraphDocumentCodec.encode(graph),
                    AlgorithmCatalog.create_node,
                )
            except (DocumentCodecError, TypeError, ValueError) as error:
                return ExecutionReport(
                    ExecutionStatus.FAILED,
                    len(graph.nodes),
                    message=f"Workflow snapshot failed: {error}",
                )
        self._display_graph = original_graph
        self._pending_output = None
        graph = execution_graph
        order = graph.get_topological_order()

        project = project or QgsProject.instance()
        if context is None:
            context = QgsProcessingContext()
            context.setProject(project)
            context.setTransformContext(project.transformContext())
        self._running = True
        self.feedback = feedback or QgsProcessingFeedback()
        self.feedback.progressChanged.connect(self._algorithm_progress_changed)
        context.setFeedback(self.feedback)
        all_results: Dict[str, Dict[str, Any]] = {}
        skipped = set()
        executed = 0
        skipped_count = 0
        for node in order:
            if not node.is_active:
                skipped.update(self._dependent_nodes(graph, node.node_id))
                skipped.add(node.node_id)
        for node in graph.nodes.values():
            self._set_state(node, "idle", "")

        try:
            for index, node in enumerate(order):
                self._step_index = index
                self._step_total = max(len(order), 1)
                if self._is_canceled():
                    return self._canceled_report(
                        order, executed, skipped_count, all_results, node.node_id
                    )
                percent = int(index * 100 / max(len(order), 1))
                if node.node_id in skipped:
                    all_results[node.node_id] = {}
                    skipped_count += 1
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
                    skipped_count += 1
                    self._set_state(node, "skipped", "Conditional branch not selected")
                    continue
                self.progress_changed.emit(percent, f"Running {node.title}")
                if self._is_canceled():
                    return self._canceled_report(
                        order, executed, skipped_count, all_results, node.node_id
                    )
                self._set_state(node, "running", "Running")
                try:
                    if node.algorithm_id.startswith("smart:"):
                        results = self._execute_smart_node(
                            node, project, layer_lookup
                        )
                    else:
                        results = self._execute_processing_node(
                            node,
                            graph,
                            all_results,
                            context,
                            (
                                algorithm_lookup.get(node.node_id)
                                if algorithm_lookup is not None
                                else None
                            ),
                        )
                except Exception as error:
                    if self._is_canceled():
                        self._set_state(node, "canceled", "Canceled")
                        return self._canceled_report(
                            order,
                            executed,
                            skipped_count,
                            all_results,
                            node.node_id,
                        )
                    self._set_state(node, "error", str(error))
                    return ExecutionReport(
                        (
                            ExecutionStatus.PARTIAL
                            if executed
                            else ExecutionStatus.FAILED
                        ),
                        len(order),
                        executed,
                        skipped_count,
                        results=self._summarize_results(all_results),
                        failed_node_id=node.node_id,
                        message=f"{node.title}: {error}",
                    )
                if self._is_canceled():
                    self._set_state(node, "canceled", "Canceled")
                    return self._canceled_report(
                        order, executed, skipped_count, all_results, node.node_id
                    )
                node.cached_results = self._summarize_node_results(results)
                node.is_dirty = False
                all_results[node.node_id] = results
                executed += 1
                self._set_state(node, "success", "Completed")

            if defer_output_commit:
                self._pending_output = (
                    graph,
                    all_results,
                    context,
                    project,
                )
                return ExecutionReport(
                    ExecutionStatus.PREPARED,
                    len(order),
                    executed,
                    skipped_count,
                    results=self._summarize_results(all_results),
                    message="Workflow results are ready to commit.",
                )
            try:
                added_ids: List[str] = []
                added_names = self._load_terminal_outputs(
                    graph,
                    all_results,
                    context,
                    project,
                    committed_ids=added_ids,
                    cancel_check=self._is_canceled,
                )
            except Exception as error:
                return ExecutionReport(
                    (
                        ExecutionStatus.PARTIAL
                        if executed
                        else ExecutionStatus.FAILED
                    ),
                    len(order),
                    executed,
                    skipped_count,
                    added_layer_ids=added_ids,
                    results=self._summarize_results(all_results),
                    message=f"Could not load workflow outputs: {error}",
                )
            self.progress_changed.emit(100, "Workflow complete")
            return ExecutionReport(
                ExecutionStatus.COMPLETED,
                len(order),
                executed,
                skipped_count,
                added_names,
                added_ids,
                self._summarize_results(all_results),
                message="Workflow complete.",
            )
        finally:
            if self.feedback is not None:
                try:
                    self.feedback.progressChanged.disconnect(
                        self._algorithm_progress_changed
                    )
                except (RuntimeError, TypeError):
                    pass
            self.feedback = None
            self._running = False
            self._cancel_requested = False
            self._display_graph = None

    def commit_pending_outputs(
        self, report: ExecutionReport
    ) -> ExecutionReport:
        """Commit a worker-prepared output set from the main QGIS thread."""
        pending = self._pending_output
        self._pending_output = None
        if report.status != ExecutionStatus.PREPARED or pending is None:
            self._cancel_requested = False
            return report
        graph, all_results, context, project = pending
        added_ids: List[str] = []
        try:
            added_names = self._load_terminal_outputs(
                graph,
                all_results,
                context,
                project,
                committed_ids=added_ids,
                cancel_check=self._is_canceled,
            )
        except Exception as error:
            report.status = (
                ExecutionStatus.PARTIAL
                if report.executed_nodes
                else ExecutionStatus.FAILED
            )
            report.added_layer_ids = added_ids
            report.message = f"Could not load workflow outputs: {error}"
            return report
        else:
            report.status = ExecutionStatus.COMPLETED
            report.added_layers = added_names
            report.added_layer_ids = added_ids
            report.message = "Workflow complete."
            self.progress_changed.emit(100, "Workflow complete")
            return report
        finally:
            self._cancel_requested = False

    def discard_pending_outputs(self) -> None:
        """Release uncommitted worker results without touching the project."""
        self._pending_output = None
        self._cancel_requested = False

    def _is_canceled(self) -> bool:
        return self._cancel_requested or bool(
            self.feedback is not None and self.feedback.isCanceled()
        )

    def _algorithm_progress_changed(self, value: float) -> None:
        bounded = min(max(float(value), 0.0), 100.0)
        overall = int(
            (self._step_index + bounded / 100.0)
            * 100
            / max(self._step_total, 1)
        )
        self.progress_changed.emit(overall, "Running workflow")

    def _canceled_report(
        self,
        order: List[NodeDefinition],
        executed: int,
        skipped: int,
        results: Dict[str, Dict[str, Any]],
        node_id: str,
    ) -> ExecutionReport:
        for node in order:
            if node.execution_state in ("idle", "running"):
                self._set_state(node, "canceled", "Canceled")
        self.progress_changed.emit(
            int((executed + skipped) * 100 / max(len(order), 1)),
            "Workflow canceled",
        )
        return ExecutionReport(
            ExecutionStatus.CANCELED,
            len(order),
            executed,
            skipped,
            results=self._summarize_results(results),
            failed_node_id=node_id,
            message="Workflow execution was canceled.",
        )

    def _set_state(self, node: NodeDefinition, state: str, message: str) -> None:
        node.execution_state = state
        node.execution_message = message
        display_node = (
            self._display_graph.nodes.get(node.node_id)
            if self._display_graph is not None
            else None
        )
        if (
            display_node is not None
            and display_node is not node
            and QThread.currentThread() == self.thread()
        ):
            display_node.execution_state = state
            display_node.execution_message = message
            display_node.is_dirty = node.is_dirty
            display_node.cached_results = dict(node.cached_results)
        self.node_state_changed.emit(node.node_id, state, message)

    @classmethod
    def _summarize_results(
        cls, results: Dict[str, Dict[str, Any]]
    ) -> Dict[str, Dict[str, Any]]:
        return {
            node_id: cls._summarize_node_results(node_results)
            for node_id, node_results in results.items()
        }

    @staticmethod
    def _summarize_node_results(results: Dict[str, Any]) -> Dict[str, Any]:
        summary = {}
        for name, value in results.items():
            if isinstance(value, QgsMapLayer):
                summary[name] = {
                    "kind": "layer",
                    "layer_id": value.id(),
                    "name": value.name(),
                }
            elif isinstance(value, (str, int, float, bool)) or value is None:
                summary[name] = value
            elif isinstance(value, list):
                summary[name] = {
                    "kind": "collection",
                    "count": len(value),
                }
            else:
                summary[name] = {
                    "kind": "value",
                    "type": value.__class__.__name__,
                }
        return summary

    @staticmethod
    def _execute_smart_node(
        node: NodeDefinition,
        project: QgsProject,
        layer_lookup: Dict[str, QgsMapLayer] | None = None,
    ) -> Dict[str, Any]:
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
                key = str(reference).strip()
                layer = (
                    layer_lookup.get(key)
                    if layer_lookup is not None
                    else project.mapLayer(key)
                )
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
        layer = (
            layer_lookup.get(layer_ref)
            if layer_lookup is not None and layer_ref
            else project.mapLayer(layer_ref)
            if layer_ref
            else None
        )
        if layer is None and layer_ref and layer_lookup is None:
            matches = project.mapLayersByName(layer_ref)
            layer = matches[0] if matches else None
        if layer is None and layer_lookup is None:
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
        prepared_algorithm=None,
    ) -> Dict[str, Any]:
        algorithm, parameters = GraphExecutionEngine._prepare_processing_node(
            node,
            graph,
            all_results,
            context,
            prepared_algorithm,
        )

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
    def _prepare_processing_node(
        node: NodeDefinition,
        graph: GraphModel,
        all_results: Dict[str, Dict[str, Any]],
        context: QgsProcessingContext,
        prepared_algorithm=None,
    ):
        algorithm = prepared_algorithm
        if algorithm is None:
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
        return algorithm, parameters

    @staticmethod
    def _load_terminal_outputs(
        graph: GraphModel,
        all_results: Dict[str, Dict[str, Any]],
        context: QgsProcessingContext,
        project: QgsProject,
        committed_ids: List[str] | None = None,
        cancel_check=None,
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
        committed = []
        for public_name, output_name, node, layer in resolved_outputs:
            if cancel_check is not None and cancel_check():
                raise ExecutionError("Workflow output commit was canceled.")
            if project.mapLayer(layer.id()) is not None:
                continue
            owned = context.takeResultLayer(layer.id())
            if owned is not None:
                layer = owned
            original_name = layer.name()
            try:
                layer.setName(
                    public_name
                    if graph.outputs_declared
                    else f"{node.title} - {output_name}"
                )
                added_layer = project.addMapLayer(layer)
                registered = project.mapLayer(layer.id()) is not None
                if registered:
                    committed.append((layer, original_name))
                    if committed_ids is not None:
                        committed_ids.append(layer.id())
                if cancel_check is not None and cancel_check():
                    raise ExecutionError("Workflow output commit was canceled.")
                if added_layer is None or not registered:
                    raise ExecutionError(
                        f"QGIS rejected workflow output: {public_name}"
                    )
                added.append(layer.name())
            except Exception as error:
                GraphExecutionEngine._restore_layer_name(
                    layer, original_name
                )
                rollback_failed = []
                for committed_layer, committed_name in reversed(committed):
                    GraphExecutionEngine._restore_layer_name(
                        committed_layer, committed_name
                    )
                    try:
                        project.removeMapLayer(committed_layer.id())
                        still_present = (
                            project.mapLayer(committed_layer.id()) is not None
                        )
                    except Exception:
                        still_present = True
                    if still_present:
                        rollback_failed.append(committed_layer.id())
                if committed_ids is not None:
                    committed_ids[:] = rollback_failed
                if rollback_failed:
                    QgsMessageLog.logMessage(
                        "QGIS refused to remove one or more workflow result "
                        "layers after an output commit failed.",
                        "SmartModeler GIS",
                        Qgis.MessageLevel.Critical,
                    )
                    raise ExecutionError(
                        "Workflow output commit failed and rollback was incomplete."
                    ) from error
                raise ExecutionError(
                    "Workflow outputs could not be committed atomically."
                ) from error
        return added

    @staticmethod
    def _restore_layer_name(layer: QgsMapLayer, name: str) -> bool:
        """Best-effort cosmetic rollback which never blocks layer removal."""
        try:
            layer.setName(name)
        except Exception:
            return False
        return True

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
