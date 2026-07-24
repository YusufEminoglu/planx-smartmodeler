"""Pure, QGIS-free, event-driven multi-turn Agent Chat state machine.

``AgentRunLoop`` never sends a network request itself. It accepts a user
request and, later, raw provider text (or a provider failure message), and
returns a small immutable :class:`RunEvent` telling the caller (the Qt dock)
what to do next: send a provider request, show a final answer, show a
sanitized failure, or acknowledge a cancellation. All tool execution goes
through the existing :class:`~.controller.AgentController` and the trusted
registry -- this module never resolves a tool by name itself.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from .contracts import AgentMode, AgentResultStatus, AgentScope
from .controller import AgentController, RunLimitExceededError
from .prompt_builder import (
    PromptBudget,
    PromptBuildError,
    SessionMemory,
    build_prompt,
    select_tools_for_scope,
)
from .proposals import (
    PROPOSAL_KIND_LAYER_STYLE,
    PROPOSAL_KIND_MODEL_PATCH,
    PROPOSAL_KIND_MODEL_RUN,
    PROPOSAL_KIND_PROCESSING_RUN,
    ProposalReason,
    ProposalValidation,
)
from .protocol import AgentTurn, ProtocolError, agent_turn_response_schema, parse_agent_turn

# A validator that turns one parsed pure proposal draft into a bounded
# validated preview or a controlled failure. Injected by the dock (it wraps the
# trusted runtime proposal boundary); the run loop stays QGIS-free and never
# resolves a live layer/graph or a context token itself.
ProposalValidator = Callable[[str, Any, str, str], ProposalValidation]

# Which application-owned scope each proposal kind is compatible with.
_PROPOSAL_SCOPES = {
    PROPOSAL_KIND_MODEL_PATCH: (AgentScope.CURRENT_MODEL,),
    PROPOSAL_KIND_LAYER_STYLE: (AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
    PROPOSAL_KIND_PROCESSING_RUN: (AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
    PROPOSAL_KIND_MODEL_RUN: (AgentScope.CURRENT_MODEL,),
}

# Bound on the preview text kept in bounded session memory after a proposal.
MAX_PROPOSAL_MEMORY_CHARS = 1_500

# Public failure/event text handed to the dock is always bounded: a provider
# or network failure message is untrusted and could be very large.
MAX_FAILURE_TEXT_CHARS = 2_000

# Reason codes returned by AgentRunState.note_tool_call()/start_turn() (see
# controller.py) that mean "a configured run/turn limit was reached" -- the
# run loop treats every one of these as a terminal, no-further-provider-cost
# stop rather than retrying.
_LIMIT_REASON_CODES = (
    "max_turns_exceeded",
    "run_call_limit_exceeded",
    "turn_call_limit_exceeded",
    "no_active_turn",
)


class RunLoopError(ValueError):
    """Raised for a caller misuse of :class:`AgentRunLoop` (not a run failure)."""


class RunAlreadyActiveError(RunLoopError):
    """Raised when a new run is requested while one is already active."""


class RunEventKind:
    """The five instructions an :class:`AgentRunLoop` can hand back."""

    REQUEST_PROVIDER = "request_provider"
    FINAL = "final"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PROPOSAL = "proposal"
    ALL = (REQUEST_PROVIDER, FINAL, FAILED, CANCELLED, PROPOSAL)


@dataclass(frozen=True)
class ProviderRequest:
    """Everything the dock needs to send one provider turn, and nothing else.

    No API key, endpoint, ``AiProfile``, QGIS object, handler, or callback is
    ever placed here -- those stay in the existing settings/network boundary.
    """

    request_token: str
    system_prompt: str
    user_prompt: str
    response_schema: Dict[str, Any]


@dataclass(frozen=True)
class RunEvent:
    """One plain, JSON-compatible, bounded instruction for the dock."""

    kind: str
    text: str = ""
    reason_code: str = ""
    request: Optional[ProviderRequest] = None
    tool_events: Tuple[Dict[str, Any], ...] = field(default_factory=tuple)
    proposal: Optional[Dict[str, Any]] = None


class AgentRunLoop:
    """A single-run-at-a-time, provider-neutral Agent Chat state machine."""

    def __init__(
        self,
        controller: AgentController,
        static_instructions: str,
        prompt_budget: Optional[PromptBudget] = None,
        proposal_validator: Optional[ProposalValidator] = None,
    ) -> None:
        self.controller = controller
        self.static_instructions = static_instructions
        # A None validator means proposals are unsupported for this loop; such
        # a turn fails closed rather than reaching any live validation.
        self._proposal_validator = proposal_validator
        # controller.limits.max_prompt_chars is authoritative for the combined
        # system+user prompt. A caller may customize the other budget fields,
        # but its max_prompt_chars is always normalized to the controller's
        # value so a supplied budget can never widen (or narrow) the
        # controller's prompt bound.
        if prompt_budget is None:
            self._prompt_budget = PromptBudget(
                max_prompt_chars=controller.limits.max_prompt_chars
            )
        else:
            self._prompt_budget = replace(
                prompt_budget, max_prompt_chars=controller.limits.max_prompt_chars
            )
        self.session_memory = SessionMemory(self._prompt_budget)
        self._reset_run_state()

    # -- lifecycle -----------------------------------------------------

    def _reset_run_state(self) -> None:
        self._active = False
        self._terminal = False
        self._run_id = ""
        self._mode = ""
        self._scope = ""
        self._user_text = ""
        self._run_state = None
        self._current_token: Optional[str] = None
        self._token_counter = 0
        self._seen_call_ids: set = set()
        self._turn_events: List[Dict[str, Any]] = []

    def is_active(self) -> bool:
        return self._active and not self._terminal

    @property
    def mode(self) -> str:
        """The mode captured at :meth:`start`; fixed for the active run."""
        return self._mode

    @property
    def scope(self) -> str:
        """The scope captured at :meth:`start`; fixed for the active run."""
        return self._scope

    @property
    def prompt_budget(self) -> PromptBudget:
        return self._prompt_budget

    @property
    def turns_used(self) -> int:
        """How many provider turns the active/most recent run has started."""
        return self._run_state.turns if self._run_state is not None else 0

    @property
    def tool_calls_used(self) -> int:
        """How many tool calls the active/most recent run has executed."""
        return self._run_state.tool_calls_this_run if self._run_state is not None else 0

    def start(self, user_text: str, mode: str, scope: str) -> RunEvent:
        """Start a new run. Raises :class:`RunAlreadyActiveError` if a run is
        already active -- new input is rejected, never implicitly queued."""
        if self.is_active():
            raise RunAlreadyActiveError(
                "A run is already active; stop it or wait for it to finish first."
            )
        if not isinstance(user_text, str) or not user_text.strip():
            raise RunLoopError("A user message is required to start a run.")
        self._reset_run_state()
        # Fail closed on an invalid application mode/scope BEFORE any provider
        # request is built or any run state is created, so a configuration bug
        # can never widen authority or reach the proposal validator.
        if mode not in AgentMode.ALL:
            return self._fail("The requested agent mode is not valid.", "invalid_mode")
        if scope not in AgentScope.ALL:
            return self._fail("The requested agent scope is not valid.", "invalid_scope")
        self._active = True
        self._run_id = uuid.uuid4().hex[:16]
        self._mode = mode
        self._scope = scope
        self._user_text = user_text
        self._run_state = self.controller.new_run_state()
        return self._advance_turn()

    def cancel(self) -> RunEvent:
        """Terminate the active run immediately and invalidate its
        outstanding request token, so a later provider callback for it is
        ignored rather than reviving the run."""
        if not self.is_active():
            return RunEvent(kind=RunEventKind.CANCELLED, text="No run is active.")
        self._terminal = True
        self._current_token = None
        return RunEvent(kind=RunEventKind.CANCELLED, text="The run was cancelled.")

    def new_chat(self) -> None:
        """Clear session memory (the **New chat** action). The dock is
        responsible for confirming with the user first when there is
        content; this call never touches QGIS state."""
        if self.is_active():
            raise RunAlreadyActiveError("Cannot start a new chat while a run is active.")
        self.session_memory.clear()
        self._reset_run_state()

    # -- provider callbacks ----------------------------------------------

    def submit_provider_response(self, request_token: str, raw_text: str) -> Optional[RunEvent]:
        """Feed one raw provider response back into the run.

        Returns ``None`` for a stale token (a late callback after
        :meth:`cancel` or after the run already advanced past that turn) --
        the caller should treat ``None`` as "nothing to do."
        """
        if not self._is_current_token(request_token):
            return None
        self._current_token = None
        try:
            turn = parse_agent_turn(raw_text, self.controller.limits.max_tool_calls_per_turn)
        except ProtocolError as error:
            return self._fail(
                f"The AI response could not be understood: {error}", "malformed_provider_turn"
            )
        if turn.is_final:
            return self._finish(turn.assistant_text)
        if turn.is_proposal:
            return self._handle_proposal(turn)
        return self._execute_turn(turn)

    def submit_provider_failure(self, request_token: str, message: str) -> Optional[RunEvent]:
        """Feed a provider/network failure back into the run. Stale tokens
        are ignored the same way as :meth:`submit_provider_response`."""
        if not self._is_current_token(request_token):
            return None
        self._current_token = None
        return self._fail(str(message), "provider_request_failed")

    def _is_current_token(self, request_token: str) -> bool:
        return (
            self.is_active()
            and self._current_token is not None
            and request_token == self._current_token
        )

    # -- internal state machine ------------------------------------------

    def _execute_turn(self, turn: AgentTurn) -> RunEvent:
        # Atomically preflight the whole turn's call count against remaining
        # run/turn capacity BEFORE committing any call id or invoking any
        # handler, so a quota-invalid batch (e.g. two calls when one run call
        # remains) is rejected without any partial execution. The controller's
        # per-call note_tool_call() remains the authoritative counter and
        # defense-in-depth check.
        try:
            self._run_state.check_capacity(len(turn.tool_calls))
        except RunLimitExceededError as error:
            return self._fail(
                "The configured tool-call limit for this run was reached.",
                error.reason_code,
            )

        this_turn_events: List[Dict[str, Any]] = []
        if turn.assistant_text:
            note = {"kind": "assistant_note", "text": turn.assistant_text}
            self._turn_events.append(note)
            this_turn_events.append(note)

        for call in turn.tool_calls:
            trace_call_id = self._trace_call_id(call.call_id)
            # approved=False is always supplied by this trusted application
            # code; provider output can never set, infer, or influence it.
            result = self.controller.execute(
                call, self._mode, self._scope, run_state=self._run_state, approved=False
            )
            result_dict = result.to_dict()
            event_dict = {
                "kind": "tool_result",
                "tool_name": call.tool_name,
                "call_id": trace_call_id,
                "result": result_dict,
            }
            self._turn_events.append(event_dict)
            this_turn_events.append(event_dict)
            if result.status == AgentResultStatus.APPROVAL_REQUIRED:
                return self._fail(
                    "This action requires approval, which Agent Chat cannot grant "
                    "in this phase.",
                    "approval_required",
                    tool_events=tuple(this_turn_events),
                )
            if result.reason_code in _LIMIT_REASON_CODES:
                return self._fail(
                    "The configured tool-call limit for this run was reached.",
                    result.reason_code,
                    tool_events=tuple(this_turn_events),
                )

        return self._advance_turn(
            assistant_text=turn.assistant_text, tool_events=tuple(this_turn_events)
        )

    def _trace_call_id(self, call_id: str) -> str:
        """Return a run-unique id for this call's trace event.

        A provider that numbers its calls ``c1``, ``c2`` from scratch on every
        turn is not misbehaving: a call id only labels results *within* one
        turn, and per-turn uniqueness is already enforced by
        :func:`parse_agent_turn`. Treating cross-turn reuse as a fatal
        protocol error made such providers (DeepSeek among them) unable to
        complete a second turn at all, so a repeated id is disambiguated for
        the run's own record instead of ending the run.
        """
        if call_id not in self._seen_call_ids:
            self._seen_call_ids.add(call_id)
            return call_id
        turn = self._run_state.turns if self._run_state is not None else 0
        qualified = f"{call_id}#t{turn}"
        suffix = 2
        while qualified in self._seen_call_ids:
            qualified = f"{call_id}#t{turn}.{suffix}"
            suffix += 1
        self._seen_call_ids.add(qualified)
        return qualified

    def _advance_turn(
        self, assistant_text: str = "", tool_events: Tuple[Dict[str, Any], ...] = ()
    ) -> RunEvent:
        try:
            self._run_state.start_turn()
        except RunLimitExceededError as error:
            return self._fail(
                "The configured turn limit for this run was reached.",
                error.reason_code,
                tool_events=tool_events,
            )

        tool_specs = select_tools_for_scope(self.controller.registry.list_specs(), self._scope)
        try:
            prompt = build_prompt(
                static_instructions=self.static_instructions,
                mode=self._mode,
                scope=self._scope,
                tool_specs=tool_specs,
                user_text=self._user_text,
                session_history=self.session_memory.exchanges(),
                current_run_events=self._turn_events,
                budget=self._prompt_budget,
            )
        except PromptBuildError as error:
            return self._fail(str(error), "prompt_build_failed", tool_events=tool_events)

        schema = agent_turn_response_schema(self.controller.limits.max_tool_calls_per_turn)
        self._token_counter += 1
        token = f"{self._run_id}-{self._token_counter}"
        self._current_token = token
        request = ProviderRequest(
            request_token=token,
            system_prompt=prompt.system_prompt,
            user_prompt=prompt.user_prompt,
            response_schema=schema,
        )
        return RunEvent(
            kind=RunEventKind.REQUEST_PROVIDER,
            text=assistant_text,
            request=request,
            tool_events=tool_events,
        )

    def _handle_proposal(self, turn: AgentTurn) -> RunEvent:
        """Validate one terminal, inert proposal turn and stop.

        A proposal never starts another provider turn, never consumes tool
        quota, and never changes ``approved``. Ask rejects it before any live
        validation; Plan/Act validate it against the application-owned scope
        through the injected trusted validator exactly once.
        """
        kind = turn.proposal_kind
        if self._mode == AgentMode.ASK:
            return self._fail(
                "Proposals are not available in Ask mode; switch to Plan or Act.",
                ProposalReason.NOT_ALLOWED_IN_ASK,
            )
        # Only Plan or Act may propose; any other (including an invalid) mode
        # fails closed before the validator is ever called.
        if self._mode not in (AgentMode.PLAN, AgentMode.ACT):
            return self._fail(
                "Proposals require Plan or Act mode.", "invalid_mode"
            )
        if self._scope not in _PROPOSAL_SCOPES.get(kind, ()):
            return self._fail(
                "This proposal is not compatible with the selected scope.",
                ProposalReason.SCOPE_MISMATCH,
            )
        if self._proposal_validator is None:
            return self._fail(
                "Proposals are not available in this session.",
                ProposalReason.VALIDATION_FAILED,
            )
        # The injected validator must fail closed even if it raises: a raw
        # exception (or its message/traceback) must never escape to the caller.
        try:
            validation = self._proposal_validator(kind, turn.proposal, self._mode, self._scope)
        except Exception:  # noqa: BLE001 - a validator failure must be sanitized
            return self._fail(
                "The proposal could not be validated.", ProposalReason.VALIDATION_FAILED
            )
        if not isinstance(validation, ProposalValidation):
            return self._fail(
                "The proposal could not be validated.", ProposalReason.VALIDATION_FAILED
            )
        if not validation.ok:
            return self._fail(
                validation.message or "The proposal was rejected.",
                validation.reason_code or ProposalReason.VALIDATION_FAILED,
            )
        return self._finish_proposal(turn.assistant_text, kind, validation.preview)

    def _finish_proposal(
        self, assistant_text: str, kind: str, preview: Optional[Dict[str, Any]]
    ) -> RunEvent:
        self._terminal = True
        preview = preview or {}
        title = preview.get("title", "") if isinstance(preview, dict) else ""
        summary = (
            f"{assistant_text}\n[Proposal ({kind}): {title}] Not applied; review only."
        )[:MAX_PROPOSAL_MEMORY_CHARS]
        # Only the bounded validated preview summary enters memory; the raw
        # provider response and raw proposal JSON never do.
        self.session_memory.append(self._user_text, summary)
        return RunEvent(
            kind=RunEventKind.PROPOSAL,
            text=assistant_text,
            reason_code=kind,
            proposal=preview,
        )

    def _finish(self, assistant_text: str) -> RunEvent:
        self._terminal = True
        self.session_memory.append(self._user_text, assistant_text)
        return RunEvent(kind=RunEventKind.FINAL, text=assistant_text)

    def _fail(
        self,
        message: str,
        reason_code: str,
        tool_events: Tuple[Dict[str, Any], ...] = (),
    ) -> RunEvent:
        self._terminal = True
        text = message if isinstance(message, str) else str(message)
        if len(text) > MAX_FAILURE_TEXT_CHARS:
            text = text[:MAX_FAILURE_TEXT_CHARS]
        return RunEvent(
            kind=RunEventKind.FAILED, text=text, reason_code=reason_code, tool_events=tool_events
        )
