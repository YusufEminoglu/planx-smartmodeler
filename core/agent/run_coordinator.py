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

It never saves or closes the project, never writes a file, and never issues a
follow-up provider call. Execution runs on the QGIS main thread for V1.
"""
from __future__ import annotations

import contextlib
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from qgis.PyQt.QtCore import QCoreApplication, QObject, pyqtSignal
from qgis.core import (
    QgsApplication,
    QgsMapLayer,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProcessingUtils,
    QgsProject,
)

from . import context as agent_context
from .proposals import (
    PROPOSAL_KIND_MODEL_RUN,
    PROPOSAL_KIND_PROCESSING_RUN,
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

    def __init__(self, model_provider: ModelProvider, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self._model_provider = model_provider
        self._state = RunState()
        self._feedback: Optional[QgsProcessingFeedback] = None
        self._engine: Optional[Any] = None
        self._pumping = False
        self._last_pump = 0.0

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
        if not owned:
            self._fail(ticket, "The run produced no layer that could be added.")
            return
        self._finish_with_layers(ticket, PROPOSAL_KIND_PROCESSING_RUN, display_name, owned)

    def _take_result_layers(
        self, results: Dict[str, Any], destinations: Tuple[str, ...], context: Any
    ) -> List[Tuple[str, Any]]:
        """Take each destination's result as an application-owned layer."""
        owned: List[Tuple[str, Any]] = []
        for name in destinations[:MAX_RESULT_LAYERS]:
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
                continue
            with contextlib.suppress(Exception):
                taken = context.takeResultLayer(layer.id())
                if taken is not None:
                    layer = taken
            owned.append((name, layer))
        return owned

    # -- model_run ---------------------------------------------------------

    def _execute_model(self, ticket: RunTicket, display_name: str) -> None:
        from ..execution_engine import GraphExecutionEngine

        graph = self._model_provider()
        if graph is None or not graph.nodes:
            self._fail(ticket, "There is no current workflow to run.")
            return
        project = QgsProject.instance()
        before = set(project.mapLayers()) if project is not None else set()
        engine = GraphExecutionEngine(self)
        self._engine = engine
        with contextlib.suppress(Exception):
            engine.progress_changed.connect(
                lambda percent, text: self._emit_progress(percent, text)
            )
        self._emit_progress(0, f"Running {display_name}")
        try:
            engine.execute(graph)
        except Exception as error:  # noqa: BLE001 - roll the added layers back
            self._remove_layers(self._new_layer_ids(before))
            # The engine reports a cancellation as an ordinary execution error;
            # report it to the human as the cancellation it actually was.
            if self._state.canceled:
                self._finish_canceled(ticket)
            else:
                self._fail(ticket, sanitize_run_message(error))
            return
        added_ids = self._new_layer_ids(before)
        if not self._state.accepts(ticket):
            # Cancelled or torn down mid-run: the project must look untouched.
            self._remove_layers(added_ids)
            self._finish_canceled(ticket)
            return
        owned: List[Tuple[str, Any]] = []
        for layer_id in added_ids[:MAX_RESULT_LAYERS]:
            layer = project.mapLayer(layer_id) if project is not None else None
            if layer is not None:
                owned.append((layer_id, layer))
        self._finish_model(ticket, display_name, owned)

    @staticmethod
    def _new_layer_ids(before: set) -> List[str]:
        project = QgsProject.instance()
        if project is None:
            return []
        return [layer_id for layer_id in project.mapLayers() if layer_id not in before]

    @staticmethod
    def _remove_layers(layer_ids: Sequence[str]) -> None:
        project = QgsProject.instance()
        if project is None:
            return
        for layer_id in layer_ids:
            with contextlib.suppress(Exception):
                project.removeMapLayer(layer_id)

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
                if project.addMapLayer(layer) is None:
                    raise RuntimeError("The result layer could not be added.")
                added_ids.append(layer.id())
                added_names.append(
                    agent_context.bound_text(layer.name(), agent_context.MAX_DISPLAY_NAME)
                )
        except Exception:  # noqa: BLE001 - all or nothing at the add boundary
            self._remove_layers(added_ids)
            self._fail(ticket, "The result could not be added to the project.")
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

    def _finish_success(
        self,
        ticket: RunTicket,
        kind: str,
        display_name: str,
        layer_ids: List[str],
        layer_names: List[str],
    ) -> None:
        lines = [f"Added {len(layer_ids)} temporary result layer(s)."]
        summary = RunResultSummary(
            kind=kind,
            title=ticket.title,
            target=agent_context.bound_text(display_name, agent_context.MAX_DISPLAY_NAME),
            layer_names=tuple(layer_names),
            layer_ids=tuple(layer_ids),
            lines=tuple(lines),
        )
        if not self._state.finish(ticket, FINISHED):
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
