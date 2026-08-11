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

import json
import re
import uuid
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Tuple

from .contracts import AgentMode, AgentResultStatus, AgentScope, AgentToolCall
from .controller import AgentController, RunLimitExceededError
from .prompt_builder import (
    PromptBudget,
    PromptBuildError,
    SessionMemory,
    build_prompt,
    estimate_input_tokens,
    select_tools_for_request,
)
from .proposals import (
    PROPOSAL_KIND_LAYER_STYLE,
    PROPOSAL_KIND_MODEL_PATCH,
    PROPOSAL_KIND_MODEL_RUN,
    PROPOSAL_KIND_PROCESSING_RUN,
    PROPOSAL_KIND_PLUGIN_ACTION,
    PROPOSAL_KIND_PYTHON_RUN,
    PROPOSAL_KIND_SQL_RUN,
    PROPOSAL_KIND_TRUSTED_SCRIPT_RUN,
    PROPOSAL_KIND_WORKSPACE_PATCH,
    ProposalError,
    ProposalReason,
    ProposalValidation,
    _within_one_edit,
    parse_proposal,
)
from .proposal_recovery import InspectionRequest, recover_agent_turn
from .protocol import (
    ACTION_PROPOSAL,
    AgentTurn,
    ProtocolError,
    agent_turn_response_schema,
    parse_agent_turn,
)

# A validator that turns one parsed pure proposal draft into a bounded
# validated preview or a controlled failure. Injected by the dock (it wraps the
# trusted runtime proposal boundary); the run loop stays QGIS-free and never
# resolves a live layer/graph or a context token itself.
ProposalValidator = Callable[[str, Any, str, str], ProposalValidation]
InstructionProvider = Callable[[str, str, bool], str]
PowerEnabledProvider = Callable[[], bool]
ActiveLayerIdProvider = Callable[[], Optional[str]]

TOTAL_TOKEN_WARNING_START = 300_000
TOTAL_TOKEN_WARNING_STEP = 100_000
SINGLE_TURN_WARNING_TOKENS = 100_000

# Repeating an already successful read-only inspection yields no new evidence,
# but it should trigger strategy recovery rather than an immediate terminal
# failure. The provider gets three increasingly explicit chances to use the
# cached evidence, choose a materially different route, or report the exact
# blocker. A fourth consecutive fully reused turn is considered unresponsive.
MAX_NO_PROGRESS_INTERVENTIONS = 3

# A provider can occasionally return a fenced/prose-wrapped or otherwise
# incomplete envelope even when JSON mode is enabled. Give the same run bounded
# repair turns instead of terminating the user's operation immediately.
#
# The budget is spent per *distinct* fault, not per attempt. A run-wide cap of
# one attempt looked safe and was not: an owner session spent it repairing a
# malformed envelope, and the next, unrelated mechanical fault in the same
# request -- a missing context_token -- had nothing left and killed the whole
# operation. Two independent mistakes need two repairs. What must never happen
# is retrying the *same* fault, which is the actual token sink, so a repair
# signature is recorded and never retried, and the number of distinct faults is
# still capped.
# How many resolved algorithm ids the durable per-run digest may carry.
MAX_REMEMBERED_ALGORITHMS = 24
# How many live node ids the same digest may carry.
MAX_REMEMBERED_NODE_IDS = 40
MAX_PROVIDER_RECOVERY_ATTEMPTS = 3
# Total repair turns per run, counting a repeat of a fault the run already
# repaired once. A repeat is only reachable after new tool calls ran, so this
# ceiling and the per-run tool-call budget bound the loop twice over.
MAX_PROVIDER_RECOVERY_TOTAL = 5
MAX_TRANSIENT_FAILURE_RETRIES = 1

# Which application-owned scope each proposal kind is compatible with.
_PROPOSAL_SCOPES = {
    PROPOSAL_KIND_MODEL_PATCH: (AgentScope.CURRENT_MODEL,),
    PROPOSAL_KIND_LAYER_STYLE: (AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
    PROPOSAL_KIND_PROCESSING_RUN: (AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
    PROPOSAL_KIND_MODEL_RUN: (AgentScope.CURRENT_MODEL,),
    PROPOSAL_KIND_PLUGIN_ACTION: (AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
    PROPOSAL_KIND_SQL_RUN: (AgentScope.PROJECT,),
    PROPOSAL_KIND_TRUSTED_SCRIPT_RUN: (AgentScope.PROJECT,),
    PROPOSAL_KIND_PYTHON_RUN: (AgentScope.PROJECT,),
    PROPOSAL_KIND_WORKSPACE_PATCH: (AgentScope.WORKSPACE,),
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

_ATTRIBUTE_FILTER_ALGORITHM = "native:extractbyattribute"
_NAMED_LAYER_RE = re.compile(
    r"^\s*(?P<name>.{1,160}?)\s+bu\s+katman\w*", re.IGNORECASE
)
_ATTRIBUTE_FIELD_BEFORE_RE = re.compile(
    r"(?:\"(?P<quoted>[^\"\r\n]{1,128})\"|"
    r"(?P<plain>[A-Za-z_][A-Za-z0-9_]{0,127}))\s+"
    r"(?:sütun(?:unda|undaki)?|sutun(?:unda|undaki)?|"
    r"alan(?:ında|inda|daki)?|column|field)\b",
    re.IGNORECASE,
)
_ATTRIBUTE_FIELD_NAMED_RE = re.compile(
    r"(?:\"(?P<quoted>[^\"\r\n]{1,128})\"|"
    r"(?P<plain>[A-Za-z_][A-Za-z0-9_]{0,127}))\s+"
    r"(?:isimli|adlı|adli|named)\s+"
    r"(?:sütun\w*|sutun\w*|alan\w*|column|field)\b",
    re.IGNORECASE,
)
_ATTRIBUTE_FIELD_AFTER_RE = re.compile(
    r"(?:sütun(?:un)?|sutun(?:un)?|alan(?:ın)?|alanin|column|field)\s+"
    r"(?:adı|adi|ismi|name(?:d)?)\s+"
    r"(?:\"(?P<quoted>[^\"\r\n]{1,128})\"|"
    r"(?P<plain>[A-Za-z_][A-Za-z0-9_]{0,127}))",
    re.IGNORECASE,
)
_ATTRIBUTE_FIELD_VALUE_RE = re.compile(
    r"(?P<plain>[A-Za-z_][A-Za-z0-9_]{0,127})\s+"
    r"(?:değeri|degeri|value)\b",
    re.IGNORECASE,
)
_ATTRIBUTE_VALUE_AFTER_RE = re.compile(
    r"(?:değer\w*|deger\w*|value(?:\s+is)?|"
    r"eşit(?:tir)?|esit(?:tir)?|=)\s*"
    r"(?:\"(?P<double>[^\"\r\n]{1,256})\"|"
    r"'(?P<single>[^'\r\n]{1,256})'|"
    r"(?P<plain>[^\s,;.\r\n]{1,256}))",
    re.IGNORECASE,
)
_NUMERIC_LITERAL = r"[+-]?(?:\d+(?:[.,]\d+)?|[.,]\d+)"
_NUMERIC_COMPARISON_PATTERNS = (
    (
        re.compile(
            rf"(?P<value>{_NUMERIC_LITERAL})\s*"
            r"(?:(?:değerinin|degerinin|value)\s+)?"
            r"(?:altına|altinda|altında|altındaki|altindaki|"
            r"altı|alti|below|under|less\s+than)\b",
            re.IGNORECASE,
        ),
        4,
        "is less than",
    ),
    (
        re.compile(
            rf"(?P<value>{_NUMERIC_LITERAL})\s*"
            r"(?:'?(?:dan|den))\s+(?:küçük|kucuk|az)\b",
            re.IGNORECASE,
        ),
        4,
        "is less than",
    ),
    (
        re.compile(
            rf"(?P<value>{_NUMERIC_LITERAL})\s*"
            r"(?:(?:değerinin|degerinin|value)\s+)?"
            r"(?:üstüne|ustune|üstünde|ustunde|üstündeki|ustundeki|"
            r"üstü|ustu|above|over|greater\s+than)\b",
            re.IGNORECASE,
        ),
        2,
        "is greater than",
    ),
    (
        re.compile(
            r"(?:below|under|less\s+than)\s+"
            rf"(?P<value>{_NUMERIC_LITERAL})\b",
            re.IGNORECASE,
        ),
        4,
        "is less than",
    ),
    (
        re.compile(
            r"(?:above|over|greater\s+than)\s+"
            rf"(?P<value>{_NUMERIC_LITERAL})\b",
            re.IGNORECASE,
        ),
        2,
        "is greater than",
    ),
    (
        re.compile(
            rf"(?P<value>{_NUMERIC_LITERAL})\s*"
            r"(?:'?(?:dan|den))\s+(?:büyük|buyuk|fazla)\b",
            re.IGNORECASE,
        ),
        2,
        "is greater than",
    ),
    (
        re.compile(
            rf"(?P<operator><=|>=|<|>)\s*(?P<value>{_NUMERIC_LITERAL})"
        ),
        None,
        "",
    ),
)
_ATTRIBUTE_VALUE_BEFORE_RE = re.compile(
    r"(?:\"(?P<double>[^\"\r\n]{1,256})\"|"
    r"'(?P<single>[^'\r\n]{1,256})'|"
    r"(?P<plain>[A-Za-z0-9_:+-]{1,256}))\s+"
    r"(?:değer\w*|deger\w*|value\w*)",
    re.IGNORECASE,
)
_FILTER_RETRY_TERMS = (
    "tekrar dene",
    "tekrar yap",
    "işlemi yap",
    "islemi yap",
    "yeniden dene",
    "bir daha dene",
    "try again",
    "retry",
)


@dataclass(frozen=True)
class _AttributeFilterIntent:
    field_name: str
    value: str
    target_layer_name: str = ""
    operator_index: int = 0
    operator_label: str = "equals"


def _parse_numeric_comparison(
    text: str,
) -> Optional[Tuple[str, int, str]]:
    """Return a normalized threshold, QGIS enum, and readable relation."""

    symbol_operators = {
        "<": (4, "is less than"),
        "<=": (5, "is less than or equal to"),
        ">": (2, "is greater than"),
        ">=": (3, "is greater than or equal to"),
    }
    for pattern, operator_index, label in _NUMERIC_COMPARISON_PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        value = match.group("value").replace(",", ".")
        if operator_index is None:
            operator_index, label = symbol_operators[match.group("operator")]
        return value, operator_index, label
    return None


def _parse_direct_attribute_filter(text: str) -> Optional[_AttributeFilterIntent]:
    """Recognize one narrow new-layer attribute-filter request."""

    if not isinstance(text, str) or not text.strip():
        return None
    folded = text.casefold()
    named_layer_match = _NAMED_LAYER_RE.search(text)
    if not any(
        term in folded
        for term in (
            "yeni katman",
            "new layer",
            "katman olarak",
            "filter",
            "filtre",
            "süz",
            "suz",
        )
    ):
        return None
    field_match = (
        _ATTRIBUTE_FIELD_AFTER_RE.search(text)
        or _ATTRIBUTE_FIELD_NAMED_RE.search(text)
        or _ATTRIBUTE_FIELD_BEFORE_RE.search(text)
        or _ATTRIBUTE_FIELD_VALUE_RE.search(text)
    )
    comparison = _parse_numeric_comparison(text)
    value_before = _ATTRIBUTE_VALUE_BEFORE_RE.search(text)
    value_after = _ATTRIBUTE_VALUE_AFTER_RE.search(text)
    # Prefer an explicitly quoted value on either side of the Turkish value
    # marker. This avoids treating the preceding word in
    # ``sütununda değeri "low"`` as the value while still accepting
    # ``"low" değerindekileri``.
    if value_before is not None and (
        value_before.group("double") or value_before.group("single")
    ):
        value_match = value_before
    elif value_after is not None and (
        value_after.group("double") or value_after.group("single")
    ):
        value_match = value_after
    else:
        value_match = value_after or value_before
    if field_match is None or (comparison is None and value_match is None):
        return None
    field_groups = field_match.groupdict()
    field_name = (
        field_groups.get("quoted") or field_groups.get("plain") or ""
    ).strip()
    if comparison is not None:
        value, operator_index, operator_label = comparison
    else:
        value = (
            value_match.group("double")
            or value_match.group("single")
            or value_match.group("plain")
            or ""
        ).strip()
        operator_index = 0
        operator_label = "equals"
    if not field_name or not value or "\x00" in field_name or "\x00" in value:
        return None
    target_layer_name = ""
    if named_layer_match is not None:
        target_layer_name = named_layer_match.group("name").strip(" \t\"'")
    return _AttributeFilterIntent(
        field_name=field_name,
        value=value,
        target_layer_name=target_layer_name,
        operator_index=operator_index,
        operator_label=operator_label,
    )


def _attribute_filter_intent(
    user_text: str, session_history: Tuple[Any, ...]
) -> Optional[_AttributeFilterIntent]:
    direct = _parse_direct_attribute_filter(user_text)
    if direct is not None:
        return direct
    folded = str(user_text or "").casefold()
    if len(folded.strip()) > 80 or not any(
        term in folded for term in _FILTER_RETRY_TERMS
    ):
        return None
    # Retry the most recent matching operation, skipping an intervening
    # diagnostic exchange. SessionMemory already bounds this history.
    for exchange in reversed(session_history):
        previous = getattr(exchange, "user_text", "")
        recovered = _parse_direct_attribute_filter(previous)
        if recovered is not None:
            return recovered
    return None


def _total_warning_milestone(projected_tokens: int) -> int:
    """Return the latest 300k/100k cumulative warning milestone reached."""
    if projected_tokens < TOTAL_TOKEN_WARNING_START:
        return 0
    steps = (
        projected_tokens - TOTAL_TOKEN_WARNING_START
    ) // TOTAL_TOKEN_WARNING_STEP
    return TOTAL_TOKEN_WARNING_START + steps * TOTAL_TOKEN_WARNING_STEP


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
    BUDGET_CONFIRMATION = "budget_confirmation"
    ALL = (
        REQUEST_PROVIDER, FINAL, FAILED, CANCELLED, PROPOSAL,
        BUDGET_CONFIRMATION,
    )


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
    estimated_input_tokens: int = 0
    prompt_metrics: Dict[str, int] = field(default_factory=dict)


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
        instruction_provider: Optional[InstructionProvider] = None,
        power_enabled_provider: Optional[PowerEnabledProvider] = None,
        active_layer_id_provider: Optional[ActiveLayerIdProvider] = None,
    ) -> None:
        self.controller = controller
        self.static_instructions = static_instructions
        # A None validator means proposals are unsupported for this loop; such
        # a turn fails closed rather than reaching any live validation.
        self._proposal_validator = proposal_validator
        self._instruction_provider = instruction_provider
        self._power_enabled_provider = power_enabled_provider or (lambda: False)
        self._active_layer_id_provider = active_layer_id_provider or (lambda: None)
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
        self._estimated_input_tokens = 0
        self._pending_budget_request: Optional[ProviderRequest] = None
        self._pending_budget_milestone = 0
        self._acknowledged_budget_milestone = 0
        self._turn_events: List[Dict[str, Any]] = []
        # Successful read-only results are reused only within this active run.
        # A new user message resets the cache, so active-layer/project changes
        # between requests are always re-inspected.
        self._successful_tool_results: Dict[Tuple[str, str], Dict[str, Any]] = {}
        # Freshness receipts observed during this run. They come only from
        # trusted read-only tool results and are used solely by the narrow
        # proposal-recovery path; starting another run discards them so a
        # previous request can never silently supply the new request's context.
        self._proposal_receipts: Dict[Tuple[str, str], str] = {}
        self._recovery_call_counter = 0
        self._layer_extent_listing_attempted = False
        self._consecutive_fully_reused_turns = 0
        self._provider_recovery_attempts = 0
        # Repair signatures already spent in this run, mapped to the run's
        # tool-call count at the time, so a different mistake can still be
        # repaired and the same one only after real progress. See
        # MAX_PROVIDER_RECOVERY_ATTEMPTS / MAX_PROVIDER_RECOVERY_TOTAL.
        self._provider_recovery_faults: Dict[str, int] = {}
        self._transient_failure_retries = 0
        # The node ids the last model.describe reported, kept so a trimmed
        # working trace cannot leave the provider writing placeholder ids.
        self._live_node_ids: List[str] = []

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

    @property
    def estimated_input_tokens(self) -> int:
        return self._estimated_input_tokens

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
        self._pending_budget_request = None
        return RunEvent(kind=RunEventKind.CANCELLED, text="The run was cancelled.")

    def confirm_budget(self) -> RunEvent:
        """Release exactly one already-built provider request after user consent."""
        request = self._pending_budget_request
        if not self.is_active() or request is None:
            raise RunLoopError("There is no provider request waiting for token approval.")
        self._pending_budget_request = None
        if self._pending_budget_milestone:
            self._acknowledged_budget_milestone = max(
                self._acknowledged_budget_milestone,
                self._pending_budget_milestone,
            )
        self._pending_budget_milestone = 0
        self._estimated_input_tokens += request.estimated_input_tokens
        return RunEvent(kind=RunEventKind.REQUEST_PROVIDER, request=request)

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
        recovery_events: Tuple[Dict[str, Any], ...] = ()
        try:
            turn = parse_agent_turn(raw_text, self.controller.limits.max_tool_calls_per_turn)
        except ProtocolError as error:
            recovered, recovery_events = self._recover_provider_proposal(raw_text)
            if recovered is None:
                error_text = str(error).casefold()
                if "layer extent id" in error_text:
                    fault = "typed_proposal:layer_extent"
                elif "input binding" in error_text:
                    fault = "typed_proposal:input_binding"
                elif "proposal_kind" in error_text:
                    fault = "typed_proposal:proposal_kind"
                elif "context_token" in error_text:
                    fault = "typed_proposal:context_token"
                elif "invalid node id" in error_text:
                    fault = "typed_proposal:node_id"
                elif "parameter" in error_text:
                    fault = "typed_proposal:parameter_value"
                elif "non-empty proposal_json" in error_text:
                    fault = "typed_proposal:empty_payload"
                else:
                    fault = "typed_proposal:other"
                if self._is_mechanical_proposal_error(str(error)) and self._may_recover(
                    fault
                ):
                    self._spend_recovery(fault)
                    if fault == "typed_proposal:node_id":
                        # Telling it that ids come from model.describe is not
                        # enough when the trace holding that result has already
                        # been trimmed. Re-read the graph so the live ids are
                        # the newest thing in front of it.
                        _result, inspection_events = self._run_recovery_inspection(
                            InspectionRequest("model.describe", {})
                        )
                        if _result is not None:
                            recovery_events = (*recovery_events, *inspection_events)
                    if "layer extent id" in error_text:
                        repair_instruction = (
                            "The previous proposal omitted a valid project layer id "
                            "for EXTENT. Use the exact id from the latest successful "
                            "layer.list result as {\"layer_extent\": \"<id>\"}; "
                            "do not use an empty value, true, coordinates, or a "
                            "guessed name. Preserve the inspected algorithm and "
                            "return one corrected proposal."
                        )
                    elif "input binding" in error_text:
                        repair_instruction = (
                            "The previous proposal used an invalid input binding "
                            "envelope. Each inputs value must be exactly one typed "
                            "object such as {\"layer\": \"id\"}, {\"number\": 5}, "
                            "{\"enum\": 0}, {\"string\": \"text\"}, or "
                            "{\"layer_extent\": \"id\"}; do not use wrapper keys, "
                            "parameters, or destinations. Return one corrected "
                            "proposal."
                        )
                    elif "proposal_kind" in error_text:
                        # Naming the kinds *this* scope accepts, not a fixed
                        # "use processing_run": in Current model scope that
                        # advice is impossible to follow, and a live workflow
                        # run burned its repair on it and then died.
                        allowed = sorted(
                            proposal_kind
                            for proposal_kind, scopes in _PROPOSAL_SCOPES.items()
                            if self._scope in scopes
                        )
                        repair_instruction = (
                            "The previous proposal envelope used an invalid or "
                            "missing proposal_kind. Return exactly one proposal "
                            "and set proposal_kind to "
                            + (
                                f"one of: {', '.join(allowed)}."
                                if allowed
                                else "a kind this scope accepts."
                            )
                            + (
                                " A graph edit in this scope is a model_patch."
                                if PROPOSAL_KIND_MODEL_PATCH in allowed
                                else ""
                            )
                            + " Keep proposal_json as one valid JSON object and "
                            "preserve the trusted context_token and inspected "
                            "evidence. Do not claim execution."
                        )
                    elif "invalid node id" in error_text:
                        repair_instruction = (
                            "A node id in the previous proposal was a "
                            "placeholder, not a live id. Every node_id, "
                            "from_node and to_node must be either a node id "
                            "listed by model.describe or a new short id you "
                            "define in this same patch with add_node. Call "
                            "model.describe if you do not have the current "
                            "ids, then return one corrected proposal."
                        )
                    elif "parameter" in error_text:
                        repair_instruction = (
                            # The refusal names the parameter and what it
                            # wanted; repeating it here is what makes the
                            # repair specific instead of generic advice.
                            f"The previous proposal was refused: {str(error)[:200]} "
                            "In a "
                            "model_patch every parameters entry is exactly "
                            "{\"name\":\"PARAM\",\"value\":<value>} where the "
                            "value is a plain number, string, boolean, or list "
                            "of strings -- never a tagged object such as "
                            "{\"expression\":\"$area\"} or {\"number\":5}, "
                            "never null, and never a path. Write the value "
                            "itself (\"$area\", 250) and return one corrected "
                            "proposal with the same algorithms and the exact "
                            "context_token from the latest model.describe."
                        )
                    elif "non-empty proposal_json" in error_text:
                        repair_instruction = (
                            "You returned a proposal turn with an empty "
                            "proposal_json, so there was nothing to show the "
                            "user. Return the same proposal again with "
                            "proposal_json set to the complete JSON object for "
                            "that proposal_kind, using the evidence you already "
                            "inspected. Do not repeat successful tool calls."
                        )
                    else:
                        repair_instruction = (
                            "The previous terminal proposal was mechanically invalid. "
                            "Return one corrected proposal using the exact typed "
                            "binding forms and the fresh context_token from the "
                            "successful inspection. Do not repeat successful tool "
                            "calls and do not claim execution."
                        )
                    recovery = {
                        "kind": "provider_recovery",
                        "strategy": "repair_typed_proposal",
                        "instruction": repair_instruction,
                    }
                    self._turn_events.append(recovery)
                    return self._advance_turn(
                        tool_events=(*recovery_events, recovery)
                    )
                if self._may_recover("response_envelope") and not (
                    self._looks_like_terminal_proposal(raw_text)
                ):
                    self._spend_recovery("response_envelope")
                    recovery = {
                        "kind": "provider_recovery",
                        "strategy": "repair_response",
                        "instruction": (
                            "The previous provider response was not a valid single "
                            "agent_turn envelope. Return exactly one JSON object "
                            "matching the advertised schema. Do not use Markdown, "
                            "prose, or extra keys. Continue the same request from "
                            "the inspected evidence; do not repeat successful calls."
                        ),
                    }
                    self._turn_events.append(recovery)
                    return self._advance_turn(tool_events=(recovery,))
                return self._fail(
                    f"The AI response could not be understood: {error}",
                    "malformed_provider_turn",
                    tool_events=recovery_events,
                )
            turn = recovered
        if turn.is_final:
            if (
                self._scope == AgentScope.ACTIVE_LAYER
                and self._is_active_layer_blocker(turn.assistant_text)
                and self._may_recover("active_layer_blocker")
            ):
                self._spend_recovery("active_layer_blocker")
                recovery = {
                    "kind": "provider_recovery",
                    "strategy": "continue_active_layer_proposal",
                    "instruction": (
                        "You returned a final message asking for a layer id, but "
                        "the run is already in ACTIVE_LAYER scope. Do not ask the "
                        "user for an id or return final text. Continue the same "
                        "request and return exactly one processing_run proposal. "
                        "Bind the primary input to the current active layer using "
                        "the exact id from the latest successful layer.list result; "
                        "preserve the inspected algorithm and fresh context_token."
                    ),
                }
                self._turn_events.append(recovery)
                return self._advance_turn(tool_events=(recovery,))
            if (
                self._mode in (AgentMode.ACT, AgentMode.PLAN)
                and self._announces_unfinished_work(turn.assistant_text)
                and self._may_recover("announced_but_did_not_act")
            ):
                self._spend_recovery("announced_but_did_not_act")
                recovery = {
                    "kind": "provider_recovery",
                    "strategy": "do_the_work_you_announced",
                    "instruction": (
                        "You ended the run by describing your next step instead "
                        "of taking it, so the user received a sentence and no "
                        "result. The run is still open and the tools you named "
                        "are still available. Do that step now: return the "
                        "tool_calls you said you needed (within this turn's "
                        "limit, continuing in later turns if more are needed), "
                        "or return the proposal itself. Do not restate the plan."
                    ),
                }
                self._turn_events.append(recovery)
                return self._advance_turn(tool_events=(recovery,))
            if (
                self._mode in (AgentMode.ACT, AgentMode.PLAN)
                and self._promises_an_unattached_proposal(turn.assistant_text)
                and self._may_recover("unattached_proposal")
            ):
                self._spend_recovery("unattached_proposal")
                recovery = {
                    "kind": "provider_recovery",
                    "strategy": "attach_the_promised_proposal",
                    "instruction": (
                        "Your final message told the user to approve a run, but no "
                        "proposal was attached, so the application had nothing to "
                        "show and the user cannot approve anything. Do not repeat "
                        "the message and do not ask again. Return the run you just "
                        "described as exactly one proposal now, reusing the "
                        "algorithm, bindings and fresh context_token from your "
                        "latest successful inspection."
                    ),
                }
                self._turn_events.append(recovery)
                return self._advance_turn(tool_events=(recovery,))
            return self._finish(turn.assistant_text)
        if turn.is_proposal:
            return self._handle_proposal(turn, tool_events=recovery_events)
        return self._execute_turn(turn)

    @staticmethod
    def _is_mechanical_proposal_error(message: str) -> bool:
        """Allow one bounded provider retry for deterministic proposal typos."""
        text = str(message or "").casefold()
        return any(
            marker in text
            for marker in (
                "missing or invalid context_token",
                "unknown input binding form",
                "an input binding must use exactly one tagged form",
                "layer extent id is required",
                "proposal turn must set proposal_kind",
                "unknown proposal_kind",
                # A proposal envelope with an empty proposal_json is the same
                # class of mistake: the provider decided what to propose and
                # then dropped the payload. Failing the run made the user
                # retype a complex request that was one sentence from working.
                "non-empty proposal_json",
                # A placeholder where a live node id belongs
                # ("<existing_node_id>") is the same disease as a placeholder
                # receipt: the provider wrote the shape of an id instead of
                # reading one. model.describe has the real ones.
                "invalid node id",
                # A workflow patch carries raw parameter values, while a
                # processing_run carries tagged binding objects. A provider
                # that writes {"expression":"$area"} into a patch has confused
                # the two shapes -- observed live, and it killed a complete
                # four-node workflow one keystroke from valid.
                "parameter value has an unsupported type",
                "parameter list value has an unsupported type",
                "parameter numbers must be finite",
                "parameter text exceeds the safety limit",
            )
        )

    @staticmethod
    def _is_active_layer_blocker(message: str) -> bool:
        """Recognize a provider asking for an id already supplied by scope."""
        text = str(message or "").casefold()
        asks_for_id = any(
            marker in text
            for marker in (
                "layer id",
                "layer_id",
                "provide the layer",
                "confirm the active layer",
            )
        )
        asks_to_bind = any(
            marker in text
            for marker in ("input", "bind", "produced", "output layer")
        )
        return asks_for_id and asks_to_bind

    @staticmethod
    def _announces_unfinished_work(message: str) -> bool:
        """Recognize a final turn that *describes* the next step instead of taking it.

        Observed live in Workflow Studio: "I need to resolve the algorithms for
        dissolve, multipart to singleparts, field calculator and centroid before
        proposing the workflow patch." The run ended there. Every tool it named
        was one turn away and the run still had its full budget, so the user got
        a sentence where a workflow belonged.

        A turn that actually asks the user something is not this: a question is
        a legitimate terminal answer, so any question mark disqualifies the text.
        """
        text = str(message or "").casefold()
        if "?" in text:
            return False
        intends = any(
            marker in text
            for marker in (
                "i need to",
                "i will now",
                "i will resolve",
                "i will inspect",
                "let me ",
                "i should ",
                "next step",
                "before proposing",
                # The same stall worn as a refusal, or handed to the user:
                # "I cannot propose a model_patch without ..." and "Please
                # resolve 'buffer' and 'dissolve'". The user cannot call a
                # tool; only the provider can, and its budget was barely
                # touched in both live cases.
                "i cannot propose",
                "cannot complete",
                "i lack the",
                "have not been resolved",
                "not been resolved",
                "please resolve",
                "please provide the algorithm",
                "lütfen",
                "lutfen",
                "çözün",
                "cozun",
                "izleyin",
                "gerekiyor",
                "yapacağım",
                "yapacagim",
                "çözmem",
                "cozmem",
                "inceleyeceğim",
                "inceleyecegim",
            )
        )
        names_work = any(
            marker in text
            for marker in (
                "resolve",
                "inspect",
                "describe",
                "propos",
                "patch",
                "workflow",
                "algorithm",
                "çöz",
                "coz",
                "incele",
                "öner",
                "oner",
                "algoritma",
            )
        )
        return intends and names_work

    @staticmethod
    def _promises_an_unattached_proposal(message: str) -> bool:
        """Recognize a final turn that asks the user to approve nothing.

        After finishing its inspections a provider sometimes writes "approve the
        run below" while returning ``final`` with no proposal attached. The
        application shows the sentence, no approval card exists, and the user is
        left asking for a card that was never proposed -- observed in a real
        session where the same claim was repeated twice before the run ended.

        This is only consulted for a turn that carried no proposal, so the worst
        case is one extra bounded turn.
        """
        text = str(message or "").casefold()
        # Explaining who shows the card is a legitimate answer, not a claim that
        # one is waiting.
        if any(
            marker in text
            for marker in (
                "çalıştıramam",
                "calistiramam",
                "cannot run",
                "uygulama tarafından",
                "shown by the application",
            )
        ):
            return False
        return any(
            marker in text
            for marker in (
                "onaylayın",
                "onaylayin",
                "onaylayarak",
                "onaylayabilirsiniz",
                "onay kartını",
                "onay kartini",
                "approve the",
                "approval card",
                "click run",
                # The other half of the same claim: reporting the work as done
                # when no proposal was ever attached, so nothing was built and
                # nothing is waiting. Seen live as a bare "The request is
                # complete." after five turns of inspection.
                "request is complete",
                "workflow is complete",
                "workflow is ready",
                "has been created",
                "tamamlandı",
                "tamamlandi",
                "oluşturuldu",
                "olusturuldu",
                "hazır",
                "hazir",
            )
        )

    @staticmethod
    def _is_recoverable_proposal_validation(message: str) -> bool:
        """Identify bounded live-signature mistakes worth one provider repair."""
        text = str(message or "").casefold()
        # Destination attempts are an intentional fail-closed boundary. They
        # must not be turned into a second provider request or become
        # indistinguishable from a harmless signature typo.
        if "parameter output" in text or "output_" in text or "destination" in text:
            return False
        return any(
            marker in text
            for marker in (
                "cannot be set by a proposal",
                "a required input was not provided",
                "this value form is not valid for this parameter",
                "requested settings are not valid for this algorithm",
                "not been inspected in this session",
                "inspect it again",
                "geometry variable must be evaluated",
                # A parameter name the algorithm does not have. The provider
                # has processing.describe's parameter list and can pick a real
                # one; ending a whole workflow over one wrong name does not.
                "not permitted on the target node",
                "layer extent id is required",
                "input layer is not in the project",
                # An id this QGIS build does not have is a mechanical mistake:
                # the provider wrote a plausible id (native:rastercalculator,
                # native:distance) instead of resolving the real one. A
                # *restricted* id is a policy refusal and stays fail closed --
                # it must never become a bounded retry loop against the
                # blocklist.
                "unavailable algorithm",
                # A connection between incompatible sockets is a modelling
                # mistake the message states precisely ("vector cannot feed
                # raster"), so the provider can drop the edge or insert the
                # conversion. Ending a five-node workflow over one wrong edge
                # threw away the other four nodes with it.
                "invalid connection",
                # A parameter written in the wrong Python type is the same
                # class of typo. The *safety* rejections next to these in
                # proposals.py (paths, URIs, credential-shaped values, control
                # characters) are deliberate boundaries and stay fail closed.
                "value is required",
                "must be finite",
            )
        )

    @staticmethod
    def _live_validation_fault(validation: ProposalValidation) -> str:
        """Name the fault finely enough that two different mistakes are two faults.

        Keying only on ``reason_code`` collapsed every live-validation refusal
        into one fault, so a workflow that fixed a stale receipt and then made
        an unrelated parameter mistake had no repair left for the second one --
        observed live. The message's first words separate the families while
        staying bounded and value-free (the validator never puts a parameter
        *value* at the front of its message).
        """
        words = "".join(
            character if character.isalnum() or character.isspace() else " "
            for character in str(validation.message or "").casefold()
        ).split()
        family = "-".join(words[:4])[:60]
        return f"live_validated:{str(validation.reason_code or '')[:40]}:{family}"

    @staticmethod
    def _is_stale_processing_validation(message: str) -> bool:
        text = str(message or "").casefold()
        return (
            "not been inspected in this session" in text
            or "inspect it again" in text
        )

    @staticmethod
    def _looks_like_terminal_proposal(raw_text: str) -> bool:
        """Keep semantic proposal failures on their strict receipt path.

        A proposal with a stale/missing receipt is not a transport-format issue;
        asking the provider to rewrite it would spend a turn without adding the
        trusted live inspection that the proposal recovery path requires.
        """
        try:
            value = json.loads(str(raw_text or "").strip())
        except (TypeError, ValueError, RecursionError):
            return False
        return isinstance(value, dict) and value.get("action") == ACTION_PROPOSAL

    def submit_provider_failure(self, request_token: str, message: str) -> Optional[RunEvent]:
        """Feed a provider/network failure back into the run. Stale tokens
        are ignored the same way as :meth:`submit_provider_response`."""
        if not self._is_current_token(request_token):
            return None
        self._current_token = None
        if (
            self._transient_failure_retries < MAX_TRANSIENT_FAILURE_RETRIES
            and self._is_transient_provider_failure(message)
        ):
            self._transient_failure_retries += 1
            recovery = {
                "kind": "provider_recovery",
                "strategy": "retry_transient_failure",
                "instruction": (
                    "The previous provider request failed transiently. Retry the "
                    "same request once with the exact advertised agent_turn JSON "
                    "envelope. Do not change scope, mode, or requested intent."
                ),
            }
            self._turn_events.append(recovery)
            return self._advance_turn(tool_events=(recovery,))
        return self._fail(str(message), "provider_request_failed")

    @staticmethod
    def _is_transient_provider_failure(message: str) -> bool:
        """Recognize retryable transport failures without retrying auth/input errors."""
        text = str(message or "").casefold()
        transient_markers = (
            "timed out",
            "timeout",
            "network",
            "connection refused",
            "host not found",
            "temporarily unavailable",
            "service unavailable",
            "http 408",
            "http 409",
            "http 425",
            "http 429",
            "http 500",
            "http 502",
            "http 503",
            "http 504",
            "(408)",
            "(409)",
            "(425)",
            "(429)",
            "(500)",
            "(502)",
            "(503)",
            "(504)",
            "empty response",
            "empty content",
            "content was empty",
        )
        return any(marker in text for marker in transient_markers)

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
        call_keys = [self._tool_cache_key(call) for call in turn.tool_calls]
        cached_before_turn = dict(self._successful_tool_results)
        new_call_count = sum(
            key not in cached_before_turn for key in call_keys
        )
        try:
            self._run_state.check_capacity(new_call_count)
        except RunLimitExceededError as error:
            return self._fail(
                "The configured tool-call limit for this run was reached.",
                error.reason_code,
            )

        this_turn_events: List[Dict[str, Any]] = []
        if turn.dropped_tool_calls:
            # The batch was truncated, not refused. Saying so keeps the
            # provider from assuming the dropped inspections happened -- and
            # from concluding it "cannot" continue, which is what a run that
            # simply had to ask again did on a live workflow request.
            notice = {
                # Not a "recovery": no repair turn is spent, the turn simply
                # does its allowed work. The dock says so in its own words.
                "kind": "provider_notice",
                "strategy": "tool_calls_truncated",
                "instruction": (
                    f"{turn.dropped_tool_calls} of your requested calls exceeded "
                    f"the {self.controller.limits.max_tool_calls_per_turn}-call "
                    "turn limit and were not executed; the results below are "
                    "from the calls that ran. Request the remaining ones in the "
                    "next turn. The run is still open -- never ask the user to "
                    "call a tool you can call yourself."
                ),
            }
            self._turn_events.append(notice)
            this_turn_events.append(notice)
        if turn.assistant_text:
            note = {"kind": "assistant_note", "text": turn.assistant_text}
            self._turn_events.append(note)
            this_turn_events.append(note)

        for call, cache_key in zip(turn.tool_calls, call_keys):
            trace_call_id = self._trace_call_id(call.call_id)
            # Reuse only results from an earlier provider turn. Duplicate
            # calls inside one batch retain atomic execution/count semantics.
            cached_result = cached_before_turn.get(cache_key)
            reused = cached_result is not None
            if reused:
                result_dict = dict(cached_result)
            else:
                # approved=False is always supplied by this trusted
                # application code; provider output can never influence it.
                result = self.controller.execute(
                    call,
                    self._mode,
                    self._scope,
                    run_state=self._run_state,
                    approved=False,
                )
                result_dict = result.to_dict()
                if result.status == AgentResultStatus.SUCCESS:
                    self._successful_tool_results[cache_key] = dict(result_dict)
            self._remember_proposal_receipt(call.tool_name, result_dict)
            self._remember_model_nodes(call.tool_name, result_dict)
            event_dict = {
                "kind": "tool_result",
                "tool_name": call.tool_name,
                "call_id": trace_call_id,
                "reused": reused,
                "result": result_dict,
            }
            self._turn_events.append(event_dict)
            this_turn_events.append(event_dict)
            if result_dict.get("status") == AgentResultStatus.APPROVAL_REQUIRED:
                return self._fail(
                    "This action requires approval, which Agent Chat cannot grant "
                    "in this phase.",
                    "approval_required",
                    tool_events=tuple(this_turn_events),
                )
            if result_dict.get("reason_code") in _LIMIT_REASON_CODES:
                return self._fail(
                    "The configured tool-call limit for this run was reached.",
                    str(result_dict.get("reason_code")),
                    tool_events=tuple(this_turn_events),
                )

        fully_reused = bool(call_keys) and all(
            key in cached_before_turn for key in call_keys
        )
        if fully_reused:
            self._consecutive_fully_reused_turns += 1
        else:
            self._consecutive_fully_reused_turns = 0

        # A project-extent request needs two different facts: the algorithm
        # signature and the concrete layer id. If a provider keeps repeating
        # the signature call, obtain the missing read-only layer listing once
        # on the application's side so the next provider turn has actionable
        # evidence. This is bounded, scope-limited, and never selects a layer
        # or creates a proposal automatically.
        if (
            fully_reused
            and not self._layer_extent_listing_attempted
            and self._should_autofetch_layer_extent_source()
        ):
            self._layer_extent_listing_attempted = True
            _result, extent_events = self._run_recovery_inspection(
                InspectionRequest("layer.list", {"limit": 100})
            )
            if extent_events:
                this_turn_events.extend(extent_events)
                self._consecutive_fully_reused_turns = 0

        if (
            self._consecutive_fully_reused_turns
            > MAX_NO_PROGRESS_INTERVENTIONS
        ):
            return self._fail(
                "The AI kept repeating the same read-only inspections after "
                "three strategy-change prompts. The run was stopped without "
                "re-executing those tools. Try again or state the one missing "
                "input explicitly.",
                "repeated_inspections_no_progress",
                tool_events=tuple(this_turn_events),
            )
        if fully_reused:
            intervention = self._no_progress_intervention(
                self._consecutive_fully_reused_turns
            )
            self._turn_events.append(intervention)
            this_turn_events.append(intervention)

        return self._advance_turn(
            assistant_text=turn.assistant_text, tool_events=tuple(this_turn_events)
        )

    def _should_autofetch_layer_extent_source(self) -> bool:
        """Whether the current project request clearly needs a layer id."""
        if self._scope != AgentScope.PROJECT:
            return False
        folded = str(self._user_text or "").casefold()
        return (
            "layer_extent" in folded
            or ("layer extent" in folded and "project" in folded)
            or ("current extent" in folded and "layer" in folded)
        ) and not any(
            key[0] == "layer.list" for key in self._successful_tool_results
        )

    def _may_recover(self, fault: str) -> bool:
        """Whether one repair turn may be spent on ``fault``.

        Two unrelated mechanical mistakes in one request are ordinary and each
        deserves its own repair; repeating an *unchanged* mistake is the
        unbounded token sink the cap exists for. So a fault already repaired
        may be repaired again only when the run has done real work since --
        executed at least one more tool call -- and never beyond
        ``MAX_PROVIDER_RECOVERY_TOTAL`` repairs or
        ``MAX_PROVIDER_RECOVERY_ATTEMPTS`` distinct faults.

        Both bounds terminate: repeats require new tool calls, and tool calls
        are themselves capped per run. Without the progress rule a live
        workflow that stalled, was pushed into acting, resolved four more
        algorithms and then stalled once more had nothing left and ended with
        no result -- five turns and two thirds of its budget unused.
        """
        spent_at = self._provider_recovery_faults.get(fault)
        if spent_at is not None and self._provider_progress() <= spent_at:
            return False
        if self._provider_recovery_attempts >= MAX_PROVIDER_RECOVERY_TOTAL:
            return False
        if (
            spent_at is None
            and len(self._provider_recovery_faults) >= MAX_PROVIDER_RECOVERY_ATTEMPTS
        ):
            return False
        return True

    def _provider_progress(self) -> int:
        """Tool calls the *provider* asked for, excluding recovery inspections.

        A repair the loop performs for the provider (re-describing an algorithm
        or the graph) must not count as the provider making progress, or an
        unchanged mistake could earn repair after repair by triggering the very
        inspection that repairs it.
        """
        return max(0, self.tool_calls_used - self._recovery_call_counter)

    def _spend_recovery(self, fault: str) -> None:
        # Remember how much provider-driven work had happened at the moment of
        # the repair, so a repeat of the same fault needs new work first.
        self._provider_recovery_faults[fault] = self._provider_progress()
        self._provider_recovery_attempts += 1

    def _no_progress_intervention(self, level: int) -> Dict[str, Any]:
        """Return one trusted, mode-aware strategy instruction for the provider."""

        if level == 1:
            strategy = "finish_from_existing_evidence"
            instruction = (
                "The latest calls were exact repeats of successful inspections "
                "and produced no new information. Do not call them again. Use "
                "the existing results to answer now"
            )
        elif level == 2:
            strategy = "change_tool_or_arguments"
            instruction = (
                "A second repeated inspection still produced no new information. "
                "Change strategy: use a different advertised tool or materially "
                "different arguments only when one specific fact is missing"
            )
        else:
            strategy = "terminal_recovery"
            instruction = (
                "This is the final strategy-recovery prompt. Do not repeat any "
                "cached call. Complete the request from the evidence already "
                "present, or state the exact missing capability or user input"
            )
        if self._mode in (AgentMode.PLAN, AgentMode.ACT):
            completion = (
                "; return a supported proposal when the evidence is sufficient. "
                "If completion is impossible, return a concise final answer "
                "naming the exact blocker. Never infer that Processing or another "
                "capability is unavailable merely because an inspection is "
                "read-only."
            )
        else:
            completion = (
                ". If another fact is truly required, make one materially new "
                "advertised tool call; otherwise return a concise final answer "
                "with the exact result or blocker. Never infer that Processing "
                "or another capability is unavailable merely because an "
                "inspection is read-only."
            )
        return {
            "kind": "strategy_intervention",
            "level": level,
            "strategy": strategy,
            "instruction": instruction + completion,
        }

    @staticmethod
    def _tool_cache_key(call: AgentToolCall) -> Tuple[str, str]:
        """Return a deterministic semantic key for one read-only tool call."""
        arguments = json.dumps(
            call.arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return call.tool_name, arguments

    def _remember_proposal_receipt(
        self, tool_name: str, result: Dict[str, Any]
    ) -> None:
        """Cache one trusted inspection receipt for this run only."""

        if result.get("status") != AgentResultStatus.SUCCESS:
            return
        data = result.get("data")
        if not isinstance(data, dict):
            return
        if tool_name == "processing.resolve":
            resolved = data.get("resolved")
            if not isinstance(resolved, dict):
                return
            data = resolved
        token = data.get("context_token")
        if not isinstance(token, str) or not token:
            return
        if tool_name in ("processing.describe", "processing.resolve"):
            target = data.get("algorithm_id")
            key = PROPOSAL_KIND_PROCESSING_RUN
        elif tool_name == "layer.style":
            target = data.get("layer_id")
            key = PROPOSAL_KIND_LAYER_STYLE
        elif tool_name == "model.describe":
            from .identifiers import MODEL_PROPOSAL_KIND, MODEL_TARGET_ID

            target = MODEL_TARGET_ID
            key = MODEL_PROPOSAL_KIND
        else:
            return
        if isinstance(target, str) and target:
            self._proposal_receipts[(key, target)] = token

    def _remember_model_nodes(self, tool_name: str, result: Dict[str, Any]) -> None:
        """Keep the live node ids the last ``model.describe`` reported.

        Same reason as the resolved-algorithm digest: the topology is trimmed
        out of the working trace, and a provider with no ids in front of it
        writes the *shape* of one -- ``"<existing_node_id>"`` -- which rejects
        the whole patch. These ids come straight from a trusted read-only
        inspection and are already in the graph.
        """
        if tool_name != "model.describe" or result.get("status") != AgentResultStatus.SUCCESS:
            return
        data = result.get("data")
        if not isinstance(data, dict):
            return
        nodes = data.get("nodes")
        if not isinstance(nodes, list):
            return
        self._live_node_ids = [
            str(node.get("node_id"))[:64]
            for node in nodes[:MAX_REMEMBERED_NODE_IDS]
            if isinstance(node, dict) and node.get("node_id")
        ]

    def _resolved_algorithms_digest(self) -> Optional[Dict[str, Any]]:
        """A tiny, durable list of the algorithms this run has already resolved.

        The working trace is trimmed to fit the prompt budget, so after a few
        turns the earliest ``processing.resolve`` results are gone -- and a
        live run at turn eleven told the user that "the required algorithms
        have not been resolved in this session" about algorithms it resolved at
        turn two. From where it sat, that was true.

        The ids are already trusted run state: each one carries the freshness
        receipt a proposal has to echo. Repeating them costs a few dozen
        characters, cannot add authority, and removes the whole failure.
        """
        from .identifiers import MODEL_PROPOSAL_KIND, MODEL_TARGET_ID

        ids = [
            target
            for (kind, target) in self._proposal_receipts
            if kind == PROPOSAL_KIND_PROCESSING_RUN and target
        ]
        workflow_token = self._proposal_receipts.get(
            (MODEL_PROPOSAL_KIND, MODEL_TARGET_ID), ""
        )
        if not ids and not self._live_node_ids and not workflow_token:
            return None
        note = (
            "algorithm_ids are already resolved in this run -- use them "
            "directly; call processing.resolve again only when you need a "
            "parameter list. open_workflow_node_ids are the ids the open graph "
            "actually has -- reference or remove those, never a placeholder."
        )
        digest = {
            "kind": "run_facts",
            "algorithm_ids": ids[-MAX_REMEMBERED_ALGORITHMS:],
            "open_workflow_node_ids": list(self._live_node_ids),
            "note": note,
        }
        if workflow_token:
            # The receipt this run's own model.describe returned for the open
            # graph. Providers kept echoing a *different* token -- an algorithm
            # receipt, or one copied from an older turn -- and a complete patch
            # was refused for a copy error the run could correct itself.
            #
            # This grants nothing. The token only exists because a trusted
            # read-only inspection produced it in this run, and the validator
            # still verifies it against the live graph at approval time: if the
            # graph has moved since, this value is stale and the patch is
            # refused exactly as it is today.
            digest["workflow_context_token"] = workflow_token
            digest["note"] += (
                " workflow_context_token is the receipt model.describe returned "
                "for the open graph in this run -- echo it verbatim in a "
                "model_patch."
            )
        return digest

    def _run_recovery_inspection(
        self, inspection: InspectionRequest
    ) -> Tuple[Optional[Dict[str, Any]], Tuple[Dict[str, Any], ...]]:
        """Execute exactly one controller-gated read-only recovery inspection."""

        try:
            self._run_state.check_capacity(1)
        except RunLimitExceededError:
            return None, ()
        self._recovery_call_counter += 1
        call = AgentToolCall(
            call_id=f"recovery_{self._recovery_call_counter}",
            tool_name=inspection.tool_name,
            arguments=dict(inspection.arguments),
        )
        result = self.controller.execute(
            call,
            self._mode,
            self._scope,
            run_state=self._run_state,
            approved=False,
        )
        result_dict = result.to_dict()
        self._remember_proposal_receipt(call.tool_name, result_dict)
        self._remember_model_nodes(call.tool_name, result_dict)
        event = {
            "kind": "tool_result",
            "tool_name": call.tool_name,
            "call_id": self._trace_call_id(call.call_id),
            "result": result_dict,
        }
        self._turn_events.append(event)
        if result.status != AgentResultStatus.SUCCESS:
            return None, (event,)
        return result_dict, (event,)

    def _recover_provider_proposal(
        self, raw_text: str
    ) -> Tuple[Optional[AgentTurn], Tuple[Dict[str, Any], ...]]:
        """Recover mechanical proposal errors with bounded read-only receipts."""

        events: List[Dict[str, Any]] = []
        # A proposal may need two independent facts: a fresh Processing
        # signature receipt and a project layer id for a layer_extent binding.
        # Complete both trusted inspections before spending another provider
        # turn, while keeping the bound explicit and small.
        active_layer_id = ""
        if self._scope == AgentScope.ACTIVE_LAYER:
            value = self._active_layer_id_provider()
            if isinstance(value, str):
                active_layer_id = value.strip()
        for _ in range(2):
            outcome = recover_agent_turn(
                raw_text,
                self.controller.limits.max_tool_calls_per_turn,
                self._proposal_receipts,
                active_layer_id=active_layer_id,
            )
            if outcome.turn is not None:
                return outcome.turn, tuple(events)
            if outcome.inspection is None:
                return None, tuple(events)
            _result, inspection_events = self._run_recovery_inspection(
                outcome.inspection
            )
            events.extend(inspection_events)
            if _result is None:
                return None, tuple(events)
        return None, tuple(events)

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

    def _try_local_attribute_filter(self) -> Optional[RunEvent]:
        """Prepare one exact active-layer attribute filter without another AI turn.

        This path is deliberately narrow: it requires a named field, an equality
        or numeric comparison value, and a new-layer/filter phrase. An explicit
        layer name wins; otherwise the active layer is used. It performs three
        ordinary controller-gated read-only inspections, then sends the
        resulting inert Processing proposal through the same live validator and
        explicit Run approval used for provider proposals. An absent exact
        field triggers one additional bounded inspection for a possible
        one-edit correction. No Processing algorithm is executed here.
        """

        if (
            self._scope not in (AgentScope.PROJECT, AgentScope.ACTIVE_LAYER)
            or self._proposal_validator is None
        ):
            return None
        intent = _attribute_filter_intent(
            self._user_text, self.session_memory.exchanges()
        )
        if intent is None:
            return None
        if self._mode == AgentMode.ASK:
            return self._finish(
                "This request creates a new layer. Select "
                "'Act (approve to apply)' in Agent mode and send it again; "
                "Power Mode does not change the Agent mode."
            )
        if self._mode not in (AgentMode.PLAN, AgentMode.ACT):
            return None

        try:
            self._run_state.check_capacity(3)
        except RunLimitExceededError as error:
            return self._fail(
                "The configured tool-call limit is too small to inspect the "
                "active layer and prepare this filter safely.",
                error.reason_code,
            )

        events: List[Dict[str, Any]] = []
        layer_result, layer_events = self._run_recovery_inspection(
            InspectionRequest("layer.list", {"limit": 100})
        )
        events.extend(layer_events)
        if layer_result is None:
            return self._fail(
                "The active layer could not be inspected.",
                "attribute_filter_layer_list_failed",
                tool_events=tuple(events),
            )
        layer_data = layer_result.get("data")
        layers = layer_data.get("layers") if isinstance(layer_data, dict) else None
        if intent.target_layer_name:
            named_matches = [
                item
                for item in layers or ()
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"].casefold()
                == intent.target_layer_name.casefold()
                and isinstance(item.get("layer_id"), str)
                and item.get("layer_id")
            ]
            if len(named_matches) != 1:
                return self._fail(
                    f"The project does not contain exactly one layer named "
                    f"{intent.target_layer_name!r}.",
                    "attribute_filter_named_layer_missing",
                    tool_events=tuple(events),
                )
            target_layer = named_matches[0]
        else:
            target_layer = next(
                (
                    item
                    for item in layers or ()
                    if isinstance(item, dict)
                    and item.get("active") is True
                    and isinstance(item.get("layer_id"), str)
                    and item.get("layer_id")
                ),
                None,
            )
            if target_layer is None:
                return self._fail(
                    "No active layer is available for the requested filter.",
                    "attribute_filter_no_active_layer",
                    tool_events=tuple(events),
                )
        layer_id = target_layer["layer_id"]

        resolve_result, resolve_events = self._run_recovery_inspection(
            InspectionRequest(
                "processing.resolve",
                {"algorithm_id": _ATTRIBUTE_FILTER_ALGORITHM},
            )
        )
        events.extend(resolve_events)
        if resolve_result is None:
            return self._fail(
                "The attribute-filter algorithm could not be inspected.",
                "attribute_filter_resolve_failed",
                tool_events=tuple(events),
            )
        resolve_data = resolve_result.get("data")
        resolved = (
            resolve_data.get("resolved")
            if isinstance(resolve_data, dict)
            else None
        )
        if (
            not isinstance(resolved, dict)
            or resolved.get("available") is not True
            or resolved.get("algorithm_id") != _ATTRIBUTE_FILTER_ALGORITHM
            or resolved.get("agent_runnable") is not True
            or not isinstance(resolved.get("context_token"), str)
            or not resolved.get("context_token")
        ):
            return self._fail(
                "The reviewed attribute-filter algorithm is not available.",
                "attribute_filter_unavailable",
                tool_events=tuple(events),
            )

        describe_result, describe_events = self._run_recovery_inspection(
            InspectionRequest(
                "layer.describe",
                {
                    "layer_id": layer_id,
                    "field_name": intent.field_name,
                    "limit": 100,
                },
            )
        )
        events.extend(describe_events)
        if describe_result is None:
            return self._fail(
                "The active layer fields could not be inspected.",
                "attribute_filter_describe_failed",
                tool_events=tuple(events),
            )
        describe_data = describe_result.get("data")
        fields = (
            describe_data.get("fields")
            if isinstance(describe_data, dict)
            else None
        )
        field_names = [
            item["name"]
            for item in fields or ()
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and item.get("name")
        ]
        resolved_field_name = (
            intent.field_name
            if intent.field_name in field_names
            else ""
        )
        warnings = []
        if not resolved_field_name:
            try:
                self._run_state.check_capacity(1)
            except RunLimitExceededError as error:
                return self._fail(
                    "The configured tool-call limit is too small to check a "
                    "possible field-name correction.",
                    error.reason_code,
                    tool_events=tuple(events),
                )
            fallback_result, fallback_events = self._run_recovery_inspection(
                InspectionRequest(
                    "layer.describe", {"layer_id": layer_id, "limit": 100}
                )
            )
            events.extend(fallback_events)
            if fallback_result is None:
                return self._fail(
                    "The active layer fields could not be inspected for a "
                    "field-name correction.",
                    "attribute_filter_describe_failed",
                    tool_events=tuple(events),
                )
            fallback_data = fallback_result.get("data")
            fallback_fields = (
                fallback_data.get("fields")
                if isinstance(fallback_data, dict)
                else None
            )
            field_names = [
                item["name"]
                for item in fallback_fields or ()
                if isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item.get("name")
            ]
            near_matches = [
                name
                for name in field_names
                if _within_one_edit(intent.field_name, name)
            ]
            if len(near_matches) == 1:
                resolved_field_name = near_matches[0]
                warnings.append(
                    f"Interpreted requested field {intent.field_name!r} as "
                    f"{resolved_field_name!r}; review this correction before Run."
                )
        if not resolved_field_name:
            return self._fail(
                f"The target layer does not contain the field "
                f"{intent.field_name!r}, and there was no unique one-edit "
                f"correction.",
                "attribute_filter_field_missing",
                tool_events=tuple(events),
            )

        proposal_data = {
            "schema_version": 1,
            "context_token": resolved["context_token"],
            "algorithm_id": _ATTRIBUTE_FILTER_ALGORITHM,
            "title": "Filter layer by attribute",
            "summary": (
                f"Create a temporary layer where {resolved_field_name} "
                f"{intent.operator_label} {intent.value}."
            ),
            "inputs": {
                "INPUT": {"layer": layer_id},
                "FIELD": {
                    "field": resolved_field_name,
                    "layer_param": "INPUT",
                },
                "OPERATOR": {"enum": intent.operator_index},
                "VALUE": {"string": intent.value},
            },
            "warnings": warnings,
        }
        try:
            proposal = parse_proposal(
                PROPOSAL_KIND_PROCESSING_RUN,
                json.dumps(
                    proposal_data, ensure_ascii=False, separators=(",", ":")
                ),
            )
        except ProposalError:
            return self._fail(
                "The attribute-filter proposal could not be constructed safely.",
                "attribute_filter_proposal_failed",
                tool_events=tuple(events),
            )
        turn = AgentTurn(
            action=ACTION_PROPOSAL,
            assistant_text="The attribute filter is ready to review.",
            proposal_kind=PROPOSAL_KIND_PROCESSING_RUN,
            proposal=proposal,
        )
        return self._handle_proposal(turn, tool_events=tuple(events))

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

        local_filter = self._try_local_attribute_filter()
        if local_filter is not None:
            return local_filter

        power_enabled = bool(self._power_enabled_provider())
        tool_specs = select_tools_for_request(
            self.controller.registry.list_specs(),
            self._scope,
            self._user_text,
            power_enabled=power_enabled,
            session_history=self.session_memory.exchanges(),
        )
        static_instructions = self.static_instructions
        if self._instruction_provider is not None:
            static_instructions = self._instruction_provider(
                self._user_text, self._scope, power_enabled
            )
        # The digest goes last so it is the newest event: trimming drops the
        # oldest first, and this is the one part of the trace a long run cannot
        # afford to forget.
        digest = self._resolved_algorithms_digest()
        current_events = (
            [*self._turn_events, digest] if digest is not None else self._turn_events
        )
        try:
            prompt = build_prompt(
                static_instructions=static_instructions,
                mode=self._mode,
                scope=self._scope,
                tool_specs=tool_specs,
                user_text=self._user_text,
                session_history=self.session_memory.exchanges(),
                current_run_events=current_events,
                budget=self._prompt_budget,
            )
        except PromptBuildError as error:
            return self._fail(str(error), "prompt_build_failed", tool_events=tool_events)

        schema = agent_turn_response_schema(self.controller.limits.max_tool_calls_per_turn)
        schema_chars = len(json.dumps(schema, ensure_ascii=False, sort_keys=True))
        estimated_tokens = estimate_input_tokens(
            len(prompt.system_prompt) + len(prompt.user_prompt) + schema_chars
        )
        self._token_counter += 1
        token = f"{self._run_id}-{self._token_counter}"
        self._current_token = token
        request = ProviderRequest(
            request_token=token,
            system_prompt=prompt.system_prompt,
            user_prompt=prompt.user_prompt,
            response_schema=schema,
            estimated_input_tokens=estimated_tokens,
            prompt_metrics={
                "system_chars": prompt.system_chars,
                "tool_schema_chars": prompt.tool_schema_chars,
                "history_chars": prompt.history_chars,
                "event_chars": prompt.event_chars,
                "schema_chars": schema_chars,
                "combined_chars": (
                    len(prompt.system_prompt) + len(prompt.user_prompt) + schema_chars
                ),
            },
        )
        projected = self._estimated_input_tokens + estimated_tokens
        milestone = _total_warning_milestone(projected)
        warn_for_total = milestone > self._acknowledged_budget_milestone
        warn_for_turn = estimated_tokens >= SINGLE_TURN_WARNING_TOKENS
        if warn_for_total or warn_for_turn:
            self._pending_budget_request = request
            self._pending_budget_milestone = milestone
            reasons = []
            if warn_for_turn:
                reasons.append(
                    f"the next request alone is estimated at "
                    f"{estimated_tokens:,} input tokens"
                )
            if warn_for_total:
                reasons.append(
                    f"this task would cross the {milestone:,}-token "
                    f"cumulative warning milestone"
                )
            return RunEvent(
                kind=RunEventKind.BUDGET_CONFIRMATION,
                text=(
                    f"Token notice: {'; and '.join(reasons)}. "
                    f"Projected task total: {projected:,}. Continue?"
                ),
                tool_events=tool_events,
            )
        self._estimated_input_tokens = projected
        return RunEvent(
            kind=RunEventKind.REQUEST_PROVIDER,
            text=assistant_text,
            request=request,
            tool_events=tool_events,
        )

    def _handle_proposal(
        self,
        turn: AgentTurn,
        tool_events: Tuple[Dict[str, Any], ...] = (),
    ) -> RunEvent:
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
                tool_events=tool_events,
            )
        # Only Plan or Act may propose; any other (including an invalid) mode
        # fails closed before the validator is ever called.
        if self._mode not in (AgentMode.PLAN, AgentMode.ACT):
            return self._fail(
                "Proposals require Plan or Act mode.",
                "invalid_mode",
                tool_events=tool_events,
            )
        if self._scope not in _PROPOSAL_SCOPES.get(kind, ()):
            # Name what this scope *does* accept. The bare version left both the
            # model and the user guessing: a correct Workflow Studio plan came
            # back as a processing_run, was refused, and nothing in the refusal
            # said that model_patch was the artefact being asked for.
            allowed = sorted(
                proposal_kind
                for proposal_kind, scopes in _PROPOSAL_SCOPES.items()
                if self._scope in scopes
            )
            return self._fail(
                f"A {kind} proposal is not valid in the {self._scope} scope. "
                + (
                    f"This scope accepts: {', '.join(allowed)}."
                    if allowed
                    else "This scope accepts no proposals."
                ),
                ProposalReason.SCOPE_MISMATCH,
                tool_events=tool_events,
            )
        if self._proposal_validator is None:
            return self._fail(
                "Proposals are not available in this session.",
                ProposalReason.VALIDATION_FAILED,
                tool_events=tool_events,
            )
        # The injected validator must fail closed even if it raises: a raw
        # exception (or its message/traceback) must never escape to the caller.
        try:
            validation = self._proposal_validator(kind, turn.proposal, self._mode, self._scope)
        except Exception:  # noqa: BLE001 - a validator failure must be sanitized
            return self._fail(
                "The proposal could not be validated.",
                ProposalReason.VALIDATION_FAILED,
                tool_events=tool_events,
            )
        if not isinstance(validation, ProposalValidation):
            return self._fail(
                "The proposal could not be validated.",
                ProposalReason.VALIDATION_FAILED,
                tool_events=tool_events,
            )
        if not validation.ok:
            if (
                self._is_recoverable_proposal_validation(validation.message)
                # A stale receipt is repaired by *re-inspecting the live
                # target*, never by re-issuing a receipt for a proposal
                # written against state that has since moved. Processing and
                # Workflow Studio both have a trusted read-only inspection to
                # do that with (processing.describe / model.describe), and the
                # rewritten proposal crosses the same freshness boundary again.
                # A layer_style receipt has no such re-inspection here, so it
                # stays fail closed.
                and (
                    kind in (PROPOSAL_KIND_PROCESSING_RUN, PROPOSAL_KIND_MODEL_PATCH)
                    or not self._is_stale_processing_validation(validation.message)
                )
                and self._may_recover(self._live_validation_fault(validation))
            ):
                self._spend_recovery(self._live_validation_fault(validation))
                recovery_events = tool_events
                if (
                    kind == PROPOSAL_KIND_PROCESSING_RUN
                    and self._is_stale_processing_validation(validation.message)
                ):
                    algorithm_id = getattr(turn.proposal, "algorithm_id", "")
                    _result, inspection_events = self._run_recovery_inspection(
                        InspectionRequest(
                            "processing.describe",
                            {"algorithm_id": algorithm_id},
                        )
                    )
                    if _result is None:
                        return self._fail(
                            validation.message or "The proposal was rejected.",
                            validation.reason_code or ProposalReason.VALIDATION_FAILED,
                            tool_events=tool_events,
                        )
                    recovery_events = (*tool_events, *inspection_events)
                elif (
                    kind == PROPOSAL_KIND_MODEL_PATCH
                    and self._is_stale_processing_validation(validation.message)
                ):
                    # The graph moved under the patch -- an applied earlier
                    # patch, an edit in the studio, or a receipt copied from
                    # an older turn. Re-describe the live graph once so the
                    # repair turn rewrites the patch against the node ids and
                    # receipt that exist *now*; the rewritten patch is then
                    # validated against live state exactly as before, and the
                    # user still approves the preview.
                    _result, inspection_events = self._run_recovery_inspection(
                        InspectionRequest("model.describe", {})
                    )
                    if _result is None:
                        return self._fail(
                            validation.message or "The proposal was rejected.",
                            validation.reason_code or ProposalReason.VALIDATION_FAILED,
                            tool_events=tool_events,
                        )
                    recovery_events = (*tool_events, *inspection_events)
                elif (
                    kind == PROPOSAL_KIND_PROCESSING_RUN
                    and any(
                        marker in str(validation.message or "").casefold()
                        for marker in (
                            "layer extent id is required",
                            "input layer is not in the project",
                        )
                    )
                ):
                    # Layer ids are project evidence, not something the
                    # provider may invent or carry forward from a prior
                    # stage. Inspect the current project once, then let the
                    # bounded repair turn select the exact id from the
                    # trusted layer.list result.
                    _result, inspection_events = self._run_recovery_inspection(
                        InspectionRequest("layer.list", {"limit": 100})
                    )
                    if _result is None:
                        return self._fail(
                            validation.message or "The proposal was rejected.",
                            validation.reason_code or ProposalReason.VALIDATION_FAILED,
                            tool_events=tool_events,
                        )
                    recovery_events = (*tool_events, *inspection_events)
                if kind == PROPOSAL_KIND_MODEL_PATCH:
                    # A workflow patch has no bindings, no destinations and no
                    # project layers, so the Processing wording below told the
                    # provider to fix things this proposal does not even have.
                    repair_instruction = (
                        "The live validator rejected the previous workflow "
                        f"patch: {str(validation.message)[:300]} "
                        "Return one corrected model_patch. Use only algorithm "
                        "ids that a processing.resolve or processing.describe "
                        "result reported in this session -- never an id you "
                        "assumed exists -- and resolve the missing ones first "
                        "if you have to. Reference only node ids present in "
                        "the latest model.describe result, and echo the "
                        "context_token from that same latest result. Do not "
                        "bind input layers and do not claim the workflow was "
                        "changed."
                    )
                else:
                    repair_instruction = (
                        "The live validator rejected the previous proposal for a "
                        "mechanical signature reason: "
                        f"{str(validation.message)[:300]} "
                        "Return one corrected proposal using only parameters whose "
                        "processing.describe row has a non-empty proposal_binding. "
                        "Omit every destination/output field; outputs are forced to "
                        "temporary layers. For every layer or layer_extent input, "
                        "use only an id from the latest successful layer.list "
                        "result; do not reuse a stale id or invent a name. "
                        "Preserve the inspected algorithm, exact context_token, "
                        "and user intent. Do not claim execution."
                    )
                recovery = {
                    "kind": "provider_recovery",
                    "strategy": "repair_live_validated_proposal",
                    "instruction": repair_instruction,
                }
                self._turn_events.append(recovery)
                return self._advance_turn(tool_events=(*recovery_events, recovery))
            return self._fail(
                validation.message or "The proposal was rejected.",
                validation.reason_code or ProposalReason.VALIDATION_FAILED,
                tool_events=tool_events,
            )
        return self._finish_proposal(
            turn.assistant_text, kind, validation.preview, tool_events=tool_events
        )

    def _finish_proposal(
        self,
        assistant_text: str,
        kind: str,
        preview: Optional[Dict[str, Any]],
        tool_events: Tuple[Dict[str, Any], ...] = (),
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
            tool_events=tool_events,
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
        # Record the failed attempt so a follow-up like "why?" has the context.
        # Without this the next run started fresh and the agent could only say
        # it did not understand. The user's request is only stored once a real
        # run is under way (a bad mode/scope fails before _user_text is set).
        if self._user_text:
            self.session_memory.append(
                self._user_text, f"[Attempt did not complete: {text}]"
            )
        return RunEvent(
            kind=RunEventKind.FAILED, text=text, reason_code=reason_code, tool_events=tool_events
        )
