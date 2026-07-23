"""Deterministic controller proving the full agent boundary.

    tool call -> call validation -> registry lookup -> argument-schema
    validation -> policy decision -> trusted handler -> bounded result

The controller never invokes a handler for a denied, preview-only, or
approval-required decision, converts any handler exception into a sanitized
failure result, and never retains a QGIS object in the returned result (a
handler must already return plain JSON-compatible data; see
``runtime_tools.py``).
"""
from __future__ import annotations

from typing import Optional

from .contracts import (
    AgentResultStatus,
    AgentRunLimits,
    AgentToolCall,
    AgentToolResult,
    ContractError,
    MAX_RESULT_TOTAL_CHARS,
    PolicyOutcome,
    validate_json_value,
    validate_tool_arguments,
)
from .policy import decide
from .registry import AgentToolRegistry


class RunLimitExceededError(RuntimeError):
    """Raised internally when an :class:`AgentRunState` bound is exceeded."""

    def __init__(self, message: str, reason_code: str = "run_limit_exceeded") -> None:
        super().__init__(message)
        self.reason_code = reason_code


class AgentRunState:
    """Tracks per-run/per-turn tool-call counters against :class:`AgentRunLimits`.

    A run state has an explicit lifecycle: ``start_turn()`` must be called
    before any tool call is counted through ``note_tool_call()``. A tool call
    attempted with no active turn is rejected deterministically rather than
    silently counted against an implicit turn. Phase 01 has no multi-turn LLM
    loop, so no code path currently drives this lifecycle end to end, but the
    controller and its tests exercise it directly.
    """

    def __init__(self, limits: Optional[AgentRunLimits] = None) -> None:
        self.limits = limits or AgentRunLimits()
        self.turns = 0
        self.tool_calls_this_run = 0
        self.tool_calls_this_turn = 0
        self._turn_active = False

    def start_turn(self) -> None:
        if self.turns >= self.limits.max_turns:
            raise RunLimitExceededError("Maximum turns exceeded.", "max_turns_exceeded")
        self.turns += 1
        self.tool_calls_this_turn = 0
        self._turn_active = True

    def check_capacity(self, count: int) -> None:
        """Non-mutating preflight: raise if ``count`` further tool calls would
        exceed remaining run/turn capacity, without touching any counter.

        Lets a caller reject a whole provider turn atomically (before any call
        id is committed or any handler runs) when the batch cannot fit the
        remaining quota, so a quota-invalid multi-call turn never executes
        partially. ``note_tool_call()`` remains the authoritative per-call
        mutation and defense-in-depth check.
        """
        if not self._turn_active:
            raise RunLimitExceededError(
                "A tool call requires an active turn; call start_turn() first.",
                "no_active_turn",
            )
        if self.tool_calls_this_run + count > self.limits.max_tool_calls_per_run:
            raise RunLimitExceededError(
                "Maximum tool calls for this run exceeded.", "run_call_limit_exceeded"
            )
        if self.tool_calls_this_turn + count > self.limits.max_tool_calls_per_turn:
            raise RunLimitExceededError(
                "Maximum tool calls for this turn exceeded.", "turn_call_limit_exceeded"
            )

    def note_tool_call(self) -> None:
        if not self._turn_active:
            raise RunLimitExceededError(
                "A tool call requires an active turn; call start_turn() first.",
                "no_active_turn",
            )
        if self.tool_calls_this_run >= self.limits.max_tool_calls_per_run:
            raise RunLimitExceededError(
                "Maximum tool calls for this run exceeded.", "run_call_limit_exceeded"
            )
        if self.tool_calls_this_turn >= self.limits.max_tool_calls_per_turn:
            raise RunLimitExceededError(
                "Maximum tool calls for this turn exceeded.", "turn_call_limit_exceeded"
            )
        self.tool_calls_this_run += 1
        self.tool_calls_this_turn += 1


class AgentController:
    """Executes one tool call through the schema/policy-gated, trusted registry.

    ``self.limits`` is authoritative: any :class:`AgentRunState` passed to
    ``execute`` must have been created from (or hold limits equal to) this
    controller's own limits. A foreign run state with different limits is
    rejected before any counter is touched, so a caller cannot bypass a
    controller's configured bounds by supplying a more permissive state. Use
    :meth:`new_run_state` to obtain a state that is always in sync.
    """

    def __init__(
        self, registry: AgentToolRegistry, limits: Optional[AgentRunLimits] = None
    ) -> None:
        self.registry = registry
        self.limits = limits or AgentRunLimits()

    def new_run_state(self) -> AgentRunState:
        """Return a fresh :class:`AgentRunState` bound to this controller's limits."""
        return AgentRunState(self.limits)

    def execute(
        self,
        call: AgentToolCall,
        mode: str,
        scope: str,
        run_state: Optional[AgentRunState] = None,
        approved: object = False,
    ) -> AgentToolResult:
        if run_state is not None:
            if run_state.limits != self.limits:
                return AgentToolResult(
                    call.call_id,
                    call.tool_name,
                    AgentResultStatus.DENIED,
                    None,
                    "The supplied run state does not match this controller's limits.",
                    "run_state_mismatch",
                )
            try:
                run_state.note_tool_call()
            except RunLimitExceededError as error:
                return AgentToolResult(
                    call.call_id,
                    call.tool_name,
                    AgentResultStatus.DENIED,
                    None,
                    str(error),
                    error.reason_code,
                )

        spec = self.registry.get_spec(call.tool_name)
        if spec is None:
            return AgentToolResult(
                call.call_id,
                call.tool_name,
                AgentResultStatus.DENIED,
                None,
                "Unknown tool.",
                "unknown_tool",
            )

        try:
            validate_tool_arguments(spec.input_schema, call.arguments)
        except ContractError as error:
            return AgentToolResult(
                call.call_id,
                call.tool_name,
                AgentResultStatus.FAILED,
                None,
                str(error),
                "invalid_arguments",
            )

        decision = decide(spec.risk, mode, scope, spec.allowed_scopes, approved=approved)

        if decision.outcome in (PolicyOutcome.DENY, PolicyOutcome.PREVIEW_ONLY):
            # Phase 01 has no preview/approval UI yet; preview-only and denied
            # calls both report as denied with a distinguishing reason code so
            # a future UI can render "preview" without a result-contract change.
            return AgentToolResult(
                call.call_id,
                call.tool_name,
                AgentResultStatus.DENIED,
                None,
                decision.reason,
                decision.reason_code,
            )
        if decision.outcome == PolicyOutcome.REQUIRE_APPROVAL:
            return AgentToolResult(
                call.call_id,
                call.tool_name,
                AgentResultStatus.APPROVAL_REQUIRED,
                None,
                decision.reason,
                decision.reason_code,
            )

        handler = self.registry.get_handler(call.tool_name)
        if handler is None:
            return AgentToolResult(
                call.call_id,
                call.tool_name,
                AgentResultStatus.FAILED,
                None,
                "Tool handler is unavailable.",
                "handler_missing",
            )

        try:
            data = handler(call)
        except Exception:  # noqa: BLE001 - handler failures must always be sanitized
            return AgentToolResult(
                call.call_id,
                call.tool_name,
                AgentResultStatus.FAILED,
                None,
                "The tool could not complete this request.",
                "handler_error",
            )

        try:
            # The controller's own (possibly narrower) configured result-text
            # bound is authoritative for this run, in addition to the
            # contract's fixed hard maximum applied by AgentToolResult itself.
            bounded_data = validate_json_value(
                data,
                max_string_length=self.limits.max_result_text_chars,
                max_total_chars=MAX_RESULT_TOTAL_CHARS,
            )
        except ContractError:
            return AgentToolResult(
                call.call_id,
                call.tool_name,
                AgentResultStatus.FAILED,
                None,
                "The tool result was invalid.",
                "invalid_result",
            )

        try:
            return AgentToolResult(
                call.call_id,
                call.tool_name,
                AgentResultStatus.SUCCESS,
                bounded_data,
                "",
                "",
            )
        except ContractError:
            return AgentToolResult(
                call.call_id,
                call.tool_name,
                AgentResultStatus.FAILED,
                None,
                "The tool result was invalid.",
                "invalid_result",
            )
