"""Trusted coordinator that owns the ONE running agent action.

This is the only place in the plugin where an agent-originated request actually
executes. It is application code: the provider never reaches it, never names the
algorithm it runs (that came from a reviewed, policy-checked plan), and never
supplies a destination (every destination was already forced to a temporary
output by the runtime validator).

What it guarantees:

- **one running action maximum** -- a second start is refused with
  ``RUN_IN_PROGRESS`` and starts nothing;
- **temporary results only** -- a ``processing_run`` executes one trusted
  ``processing.run(..., is_child_algorithm=True)`` over a fresh context and
  feedback, and a ``model_run`` delegates to the existing, trusted
  :class:`GraphExecutionEngine` rather than reimplementing graph execution;
- **atomic at the layer-addition boundary** -- a failed, canceled, or late run
  adds no layer, and a partial add is rolled back;
- **late callbacks are inert** -- every run carries a monotonic ticket, so a
  result arriving after cancel or teardown adds nothing and revives nothing;
- **no leak** -- every Processing failure string is replaced by a bounded,
  path-free, credential-free message before it reaches a signal, the UI, or the
  ledger.

It never saves or closes the project, never writes a user-selected file, and
never issues a follow-up provider call. A reviewed network adapter may use a
QGIS-owned temporary file prepared by the validator. Execution runs on the QGIS
main thread for V1.
"""
from __future__ import annotations

import contextlib
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QObject, pyqtSignal
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsMapLayer,
    QgsMessageLog,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProcessingUtils,
    QgsProject,
)

from . import context as agent_context
from .proposals import (
    PROPOSAL_KIND_MODEL_RUN,
    PROPOSAL_KIND_PYTHON_RUN,
    PROPOSAL_KIND_PROCESSING_RUN,
    PROPOSAL_KIND_SQL_RUN,
    PROPOSAL_KIND_TRUSTED_SCRIPT_RUN,
    ProposalReason,
)
from .run_planner import RunResultSummary
from .run_state import (
    CANCELED,
    FAILED,
    FINISHED,
    RunState,
    RunTicket,
    sanitize_run_message,
)

# The UI must stay responsive enough to click Cancel during a synchronous run,
# so progress callbacks pump the event loop -- but no more often than this, and
# never re-entrantly.
_PUMP_INTERVAL_SECONDS = 0.08
# A single reviewed run cannot legitimately produce more results than this.
MAX_RESULT_LAYERS = 20

ModelProvider = Callable[[], Optional[Any]]


class RunCoordinator(QObject):
    """Owns the single running action and its result layers."""

    run_progress = pyqtSignal(int, str)
    # dict: the bounded summary of a completed run (see RunResultSummary).
    run_finished = pyqtSignal(dict)
    # (reason_code, bounded message)
    run_failed = pyqtSignal(str, str)
    run_canceled = pyqtSignal()

    def __init__(
        self,
        model_provider: ModelProvider,
        parent: Optional[QObject] = None,
        power_runtime: Optional[Any] = None,
    ) -> None:
        super().__init__(parent)
        self._model_provider = model_provider
        self._state = RunState()
        self._feedback: Optional[QgsProcessingFeedback] = None
        self._engine: Optional[Any] = None
        self._pumping = False
        self._last_pump = 0.0
        self._power_runtime = power_runtime

    # -- lifecycle ---------------------------------------------------------

    def is_running(self) -> bool:
        return self._state.is_running()

    def cancel(self) -> None:
        """Cancel the running action. Terminal and idempotent."""
        if not self._state.cancel():
            return
        with contextlib.suppress(Exception):
            if self._feedback is not None:
                self._feedback.cancel()
        with contextlib.suppress(Exception):
            if self._engine is not None:
                self._engine.cancel()
        with contextlib.suppress(Exception):
            if self._power_runtime is not None:
                self._power_runtime.cancel()

    def shutdown(self) -> None:
        """Cancel and tear down so no in-flight result can outlive the dock."""
        self.cancel()
        self._state.reset()
        self._feedback = None
        self._engine = None

    # -- starts ------------------------------------------------------------

    def start_processing_run(
        self,
        action_id: str,
        title: str,
        display_name: str,
        algorithm_id: str,
        parameters: Dict[str, Any],
        destinations: Sequence[str],
    ) -> str:
        """Run one reviewed algorithm. Returns "" or a refusal reason code."""
        ticket = self._state.start(action_id, PROPOSAL_KIND_PROCESSING_RUN, title)
        if ticket is None:
            return ProposalReason.RUN_IN_PROGRESS
        try:
            self._execute_processing(
                ticket, display_name, algorithm_id, dict(parameters), tuple(destinations)
            )
        except Exception as error:  # noqa: BLE001 - every failure is sanitized
            self._fail(ticket, sanitize_run_message(error))
        finally:
            self._feedback = None
        return ""

    def start_model_run(self, action_id: str, title: str, display_name: str) -> str:
        """Run the current graph through the trusted engine. Returns "" or a code."""
        ticket = self._state.start(action_id, PROPOSAL_KIND_MODEL_RUN, title)
        if ticket is None:
            return ProposalReason.RUN_IN_PROGRESS
        try:
            self._execute_model(ticket, display_name)
        except Exception as error:  # noqa: BLE001 - every failure is sanitized
            self._fail(ticket, sanitize_run_message(error))
        finally:
            self._engine = None
            self._feedback = None
        return ""

    def start_power_run(
        self,
        action_id: str,
        kind: str,
        title: str,
        display_name: str,
        ingredients: Dict[str, Any],
    ) -> str:
        if kind not in (
            PROPOSAL_KIND_SQL_RUN,
            PROPOSAL_KIND_TRUSTED_SCRIPT_RUN,
            PROPOSAL_KIND_PYTHON_RUN,
        ) or self._power_runtime is None:
            return ProposalReason.SIDE_EFFECT_BLOCKED
        ticket = self._state.start(action_id, kind, title)
        if ticket is None:
            return ProposalReason.RUN_IN_PROGRESS
        try:
            if kind == PROPOSAL_KIND_SQL_RUN:
                layers, _message = self._power_runtime.execute_sql(ingredients)
            else:
                layers, _message = self._power_runtime.execute_python(ingredients)
            if (
                kind in (PROPOSAL_KIND_TRUSTED_SCRIPT_RUN, PROPOSAL_KIND_PYTHON_RUN)
                and ingredients.get("execution_mode") == "live"
            ):
                self._finish_success(
                    ticket,
                    kind,
                    display_name,
                    [layer.id() for layer in layers],
                    [agent_context.bound_text(layer.name(), agent_context.MAX_DISPLAY_NAME)
                     for layer in layers],
                )
            elif layers:
                self._finish_with_layers(
                    ticket,
                    kind,
                    display_name,
                    [(f"OUTPUT_{index + 1}", layer) for index, layer in enumerate(layers)],
                )
            else:
                self._finish_success(ticket, kind, display_name, [], [])
        except Exception as error:  # noqa: BLE001
            if self._state.canceled:
                self._finish_canceled(ticket)
            else:
                self._fail(ticket, sanitize_run_message(error))
        return ""

    # -- progress ----------------------------------------------------------

    def _emit_progress(self, percent: Any, text: Any) -> None:
        value = 0
        with contextlib.suppress(Exception):
            value = max(0, min(100, int(percent)))
        self.run_progress.emit(value, agent_context.bound_text(str(text), 120))
        self._pump()

    def _pump(self) -> None:
        """Let the UI repaint and deliver a Cancel click during a run.

        Guarded against re-entrancy and rate-limited: the dock disables every
        control except Cancel while a run is live, and the one-run-max rule
        makes a second start impossible, so the only user action this can
        deliver mid-run is the cancellation it exists for.
        """
        if self._pumping:
            return
        now = time.monotonic()
        if now - self._last_pump < _PUMP_INTERVAL_SECONDS:
            return
        self._pumping = True
        try:
            with contextlib.suppress(Exception):
                QCoreApplication.processEvents()
        finally:
            self._pumping = False
            self._last_pump = time.monotonic()

    # -- processing_run ----------------------------------------------------

    def _execute_processing(
        self,
        ticket: RunTicket,
        display_name: str,
        algorithm_id: str,
        parameters: Dict[str, Any],
        destinations: Tuple[str, ...],
    ) -> None:
        if not destinations or len(destinations) > MAX_RESULT_LAYERS:
            self._fail(ticket, "The run declares an invalid result-layer count.")
            return
        registry = QgsApplication.processingRegistry()
        algorithm = registry.createAlgorithmById(algorithm_id) if registry is not None else None
        if algorithm is None:
            self._fail(ticket, "That algorithm is no longer available.")
            return
        project = QgsProject.instance()
        context = QgsProcessingContext()
        if project is not None:
            context.setProject(project)
            with contextlib.suppress(Exception):
                context.setTransformContext(project.transformContext())
        feedback = QgsProcessingFeedback()
        with contextlib.suppress(Exception):
            feedback.progressChanged.connect(
                lambda percent: self._emit_progress(percent, f"Running {display_name}")
            )
        context.setFeedback(feedback)
        self._feedback = feedback
        self._emit_progress(0, f"Running {display_name}")

        import processing

        results = processing.run(
            algorithm, parameters, feedback=feedback, context=context, is_child_algorithm=True
        )
        if not isinstance(results, dict):
            self._fail(ticket, "The algorithm returned no result.")
            return
        if feedback.isCanceled() or not self._state.accepts(ticket):
            # Cancelled, superseded, or torn down while running: take nothing.
            self._finish_canceled(ticket)
            return
        owned = self._take_result_layers(results, destinations, context)
        self._finish_with_layers(ticket, PROPOSAL_KIND_PROCESSING_RUN, display_name, owned)

    def _take_result_layers(
        self, results: Dict[str, Any], destinations: Tuple[str, ...], context: Any
    ) -> List[Tuple[str, Any]]:
        """Take each destination's result as an application-owned layer."""
        resolved: List[Tuple[str, Any]] = []
        owned_ids = set()
        project = QgsProject.instance()
        for name in destinations:
            if name not in results:
                raise RuntimeError(f"The algorithm omitted result {name}.")
            value = results.get(name)
            layer = None
            if isinstance(value, QgsMapLayer):
                layer = value
            elif isinstance(value, str) and value:
                with contextlib.suppress(Exception):
                    layer = context.takeResultLayer(value)
                if layer is None:
                    with contextlib.suppress(Exception):
                        layer = QgsProcessingUtils.mapLayerFromString(value, context, True)
            if layer is None:
                raise RuntimeError(f"Result {name} is not a map layer.")
            if project is not None and project.mapLayer(layer.id()) is not None:
                raise RuntimeError(
                    f"Result {name} aliases an existing project layer."
                )
            if layer.id() in owned_ids:
                raise RuntimeError("Two declared results reference the same layer.")
            owned_ids.add(layer.id())
            resolved.append((name, layer))
        owned: List[Tuple[str, Any]] = []
        for name, layer in resolved:
            with contextlib.suppress(Exception):
                taken = context.takeResultLayer(layer.id())
                if taken is not None:
                    layer = taken
            owned.append((name, layer))
        return owned

    # -- model_run ---------------------------------------------------------

    def _execute_model(self, ticket: RunTicket, display_name: str) -> None:
        from ..execution_engine import (
            ExecutionStatus,
            GraphExecutionEngine,
        )

        graph = self._model_provider()
        if graph is None or not graph.nodes:
            self._fail(ticket, "There is no current workflow to run.")
            return
        project = QgsProject.instance()
        engine = GraphExecutionEngine(self)
        self._engine = engine
        with contextlib.suppress(Exception):
            engine.progress_changed.connect(
                lambda percent, text: self._emit_progress(percent, text)
            )
        self._emit_progress(0, f"Running {display_name}")
        try:
            report = engine.execute(graph)
        except Exception as error:  # noqa: BLE001 - fail closed on engine faults
            if self._state.canceled:
                self._finish_canceled(ticket)
            else:
                self._fail(ticket, sanitize_run_message(error))
            return
        added_ids = list(report.added_layer_ids)
        if (
            report.status == ExecutionStatus.CANCELED
            or not self._state.accepts(ticket)
        ):
            # Cancelled or torn down mid-run: the project must look untouched.
            remaining = self._remove_layers(added_ids)
            if remaining:
                self._fail(
                    ticket,
                    "Cancellation could not remove every result layer.",
                )
            else:
                self._finish_canceled(ticket)
            return
        if report.status != ExecutionStatus.COMPLETED:
            remaining = self._remove_layers(added_ids)
            self._fail(
                ticket,
                (
                    "The failed workflow could not remove every result layer."
                    if remaining
                    else report.message or "The workflow did not complete."
                ),
            )
            return
        if len(added_ids) > MAX_RESULT_LAYERS:
            remaining = self._remove_layers(added_ids)
            self._fail(
                ticket,
                (
                    "The oversized workflow result could not be rolled back."
                    if remaining
                    else "The workflow produced too many result layers for one agent run."
                ),
            )
            return
        owned: List[Tuple[str, Any]] = []
        for layer_id in added_ids:
            layer = project.mapLayer(layer_id) if project is not None else None
            if layer is not None:
                owned.append((layer_id, layer))
        if len(owned) != len(added_ids):
            remaining = self._remove_layers(added_ids)
            self._fail(
                ticket,
                (
                    "An incomplete workflow result could not be rolled back."
                    if remaining
                    else "A workflow result layer became unavailable."
                ),
            )
            return
        self._finish_model(ticket, display_name, owned)

    @staticmethod
    def _remove_layers(layer_ids: Sequence[str]) -> List[str]:
        project = QgsProject.instance()
        if project is None:
            return list(layer_ids)
        remaining = []
        for layer_id in layer_ids:
            with contextlib.suppress(Exception):
                project.removeMapLayer(layer_id)
            try:
                if project.mapLayer(layer_id) is not None:
                    remaining.append(layer_id)
            except Exception:
                remaining.append(layer_id)
        return remaining

    # -- terminal transitions ---------------------------------------------

    def _finish_with_layers(
        self, ticket: RunTicket, kind: str, display_name: str, owned: List[Tuple[str, Any]]
    ) -> None:
        """Add every taken result layer, or none of them, then report."""
        project = QgsProject.instance()
        if project is None:
            self._fail(ticket, "The project is not available to receive the result.")
            return
        added_ids: List[str] = []
        added_names: List[str] = []
        try:
            for name, layer in owned:
                with contextlib.suppress(Exception):
                    layer.setName(f"{display_name} - {name}")
                added_layer = project.addMapLayer(layer)
                registered = project.mapLayer(layer.id()) is not None
                if registered:
                    added_ids.append(layer.id())
                    added_names.append(
                        agent_context.bound_text(
                            layer.name(), agent_context.MAX_DISPLAY_NAME
                        )
                    )
                if added_layer is None or not registered:
                    raise RuntimeError("The result layer could not be added.")
        except Exception:  # noqa: BLE001 - all or nothing at the add boundary
            remaining = self._remove_layers(added_ids)
            self._fail(
                ticket,
                (
                    "The result failed and its partial layers could not be removed."
                    if remaining
                    else "The result could not be added to the project."
                ),
            )
            return
        self._finish_success(ticket, kind, display_name, added_ids, added_names)

    def _finish_model(
        self, ticket: RunTicket, display_name: str, owned: List[Tuple[str, Any]]
    ) -> None:
        added_ids = [layer_id for layer_id, _layer in owned]
        added_names = [
            agent_context.bound_text(layer.name(), agent_context.MAX_DISPLAY_NAME)
            for _layer_id, layer in owned
        ]
        self._finish_success(
            ticket, PROPOSAL_KIND_MODEL_RUN, display_name, added_ids, added_names
        )

    @staticmethod
    def _empty_result_layers(
        layer_ids: Sequence[str], layer_names: Sequence[str]
    ) -> List[str]:
        """Names of the result layers that came back with zero features.

        Only vector results can be empty in a way that matters here; a raster
        result and any layer whose count cannot be read are left out rather
        than guessed at.
        """
        project = QgsProject.instance()
        if project is None:
            return []
        empty: List[str] = []
        for index, layer_id in enumerate(layer_ids):
            layer = project.mapLayer(layer_id)
            count = None
            with contextlib.suppress(Exception):
                count = layer.featureCount()
            if count == 0:
                name = (
                    layer_names[index]
                    if index < len(layer_names)
                    else str(layer_id)
                )
                empty.append(
                    agent_context.bound_text(name, agent_context.MAX_DISPLAY_NAME)
                )
        return empty

    def _finish_success(
        self,
        ticket: RunTicket,
        kind: str,
        display_name: str,
        layer_ids: List[str],
        layer_names: List[str],
    ) -> None:
        empty_names = self._empty_result_layers(layer_ids, layer_names)
        lines = [f"Added {len(layer_ids)} temporary result layer(s)."]
        if empty_names:
            lines.append(
                f"{len(empty_names)} of them hold no features. The run "
                f"succeeded; the result is empty."
            )
        summary = RunResultSummary(
            kind=kind,
            title=ticket.title,
            target=agent_context.bound_text(display_name, agent_context.MAX_DISPLAY_NAME),
            layer_names=tuple(layer_names),
            layer_ids=tuple(layer_ids),
            lines=tuple(lines),
            empty_layer_names=tuple(empty_names),
        )
        if not self._state.accepts(ticket):
            remaining = self._remove_layers(layer_ids)
            if remaining:
                self._fail(
                    ticket,
                    "Cancellation could not remove every result layer.",
                )
            else:
                self._finish_canceled(ticket)
            return
        if not self._state.finish(ticket, FINISHED):
            remaining = self._remove_layers(layer_ids)
            if remaining:
                QgsMessageLog.logMessage(
                    "A stale Agent run left result layers that QGIS refused to remove.",
                    "SmartModeler GIS",
                    Qgis.MessageLevel.Critical,
                )
            return
        self._emit_progress(100, "Run complete")
        self.run_finished.emit(summary.to_dict())

    def _finish_canceled(self, ticket: RunTicket) -> None:
        if not self._state.finish(ticket, CANCELED):
            return
        self.run_canceled.emit()

    def _fail(self, ticket: RunTicket, message: str) -> None:
        if not self._state.finish(ticket, FAILED):
            return
        self.run_failed.emit(ProposalReason.EXECUTION_FAILED, sanitize_run_message(message))
