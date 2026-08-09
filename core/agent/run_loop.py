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
    ProposalError,
    ProposalReason,
    ProposalValidation,
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
# incomplete envelope even when JSON mode is enabled. Give the same run one
# bounded repair turn instead of terminating the user's operation immediately.
# This is deliberately a run-wide cap: repeated malformed output must never
# become an unbounded token sink.
MAX_PROVIDER_RECOVERY_ATTEMPTS = 1
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


def _within_one_edit(left: str, right: str) -> bool:
    """Return whether two bounded names differ by at most one edit."""

    left = str(left).casefold()
    right = str(right).casefold()
    if left == right:
        return True
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) <= 1
    if len(left) > len(right):
        left, right = right, left
    short_index = 0
    long_index = 0
    edits = 0
    while short_index < len(left) and long_index < len(right):
        if left[short_index] == right[long_index]:
            short_index += 1
            long_index += 1
            continue
        edits += 1
        if edits > 1:
            return False
        long_index += 1
    return True


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
    ) -> None:
        self.controller = controller
        self.static_instructions = static_instructions
        # A None validator means proposals are unsupported for this loop; such
        # a turn fails closed rather than reaching any live validation.
        self._proposal_validator = proposal_validator
        self._instruction_provider = instruction_provider
        self._power_enabled_provider = power_enabled_provider or (lambda: False)
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
        self._consecutive_fully_reused_turns = 0
        self._provider_recovery_attempts = 0
        self._transient_failure_retries = 0

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
                if (
                    self._provider_recovery_attempts < MAX_PROVIDER_RECOVERY_ATTEMPTS
                    and not self._looks_like_terminal_proposal(raw_text)
                ):
                    self._provider_recovery_attempts += 1
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
            return self._finish(turn.assistant_text)
        if turn.is_proposal:
            return self._handle_proposal(turn, tool_events=recovery_events)
        return self._execute_turn(turn)

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
        """Recover one mechanical proposal error without another provider turn."""

        outcome = recover_agent_turn(
            raw_text,
            self.controller.limits.max_tool_calls_per_turn,
            self._proposal_receipts,
        )
        if outcome.turn is not None:
            return outcome.turn, ()
        if outcome.inspection is None:
            return None, ()
        _result, events = self._run_recovery_inspection(outcome.inspection)
        if _result is None:
            return None, events
        retried = recover_agent_turn(
            raw_text,
            self.controller.limits.max_tool_calls_per_turn,
            self._proposal_receipts,
        )
        return retried.turn, events

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
        try:
            prompt = build_prompt(
                static_instructions=static_instructions,
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
            return self._fail(
                "This proposal is not compatible with the selected scope.",
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
