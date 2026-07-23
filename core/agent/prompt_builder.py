"""Deterministic, bounded prompt and session-history construction for Agent Chat.

QGIS-free: builds only the system/user prompt text for one provider turn from
static ``agent_context/`` instructions, scope-allowed tool descriptions, the
current user request, bounded prior session history, and this run's own
assistant/tool trace. Never touches QGIS and never sends a network request --
that remains the dock/``AiNetworkClient``'s job.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Tuple

from .contracts import AgentToolSpec, MAX_ALLOWED_PROMPT_CHARS

# Recommended Phase 02 defaults (work order Section 8.2).
MAX_USER_MESSAGE_CHARS = 4_000
MAX_SESSION_EXCHANGES = 12
MAX_SESSION_TEXT_CHARS = 30_000
MAX_TOOL_RESULT_PROMPT_CHARS = 8_000

# Hard maxima: a malformed/adversarial PromptBudget can never exceed these.
MAX_ALLOWED_USER_MESSAGE_CHARS = 20_000
MAX_ALLOWED_SESSION_EXCHANGES = 50
MAX_ALLOWED_SESSION_TEXT_CHARS = 200_000
MAX_ALLOWED_TOOL_RESULT_PROMPT_CHARS = 50_000


class PromptBuildError(ValueError):
    """Raised when a bounded provider prompt cannot be constructed safely."""


def _require_bounded_int(value: Any, field_name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PromptBuildError(f"{field_name} must be an integer.")
    if not minimum <= value <= maximum:
        raise PromptBuildError(f"{field_name} must be between {minimum} and {maximum}.")


@dataclass(frozen=True)
class PromptBudget:
    """Validated, hard-bounded prompt/session budgets for one Agent Chat run.

    ``max_prompt_chars`` should be taken directly from the run's
    ``AgentRunLimits.max_prompt_chars`` so that limit stays authoritative for
    the combined system + user/conversation prompt sent for each turn.
    """

    max_prompt_chars: int
    max_user_message_chars: int = MAX_USER_MESSAGE_CHARS
    max_session_exchanges: int = MAX_SESSION_EXCHANGES
    max_session_text_chars: int = MAX_SESSION_TEXT_CHARS
    max_tool_result_prompt_chars: int = MAX_TOOL_RESULT_PROMPT_CHARS

    def __post_init__(self) -> None:
        _require_bounded_int(
            self.max_prompt_chars, "max_prompt_chars", 1, MAX_ALLOWED_PROMPT_CHARS
        )
        _require_bounded_int(
            self.max_user_message_chars,
            "max_user_message_chars",
            1,
            MAX_ALLOWED_USER_MESSAGE_CHARS,
        )
        _require_bounded_int(
            self.max_session_exchanges,
            "max_session_exchanges",
            0,
            MAX_ALLOWED_SESSION_EXCHANGES,
        )
        _require_bounded_int(
            self.max_session_text_chars,
            "max_session_text_chars",
            0,
            MAX_ALLOWED_SESSION_TEXT_CHARS,
        )
        _require_bounded_int(
            self.max_tool_result_prompt_chars,
            "max_tool_result_prompt_chars",
            1,
            MAX_ALLOWED_TOOL_RESULT_PROMPT_CHARS,
        )


@dataclass(frozen=True)
class SessionExchange:
    """One bounded prior user request / final assistant answer pair."""

    user_text: str
    assistant_text: str


class SessionMemory:
    """Bounded, process-memory-only conversation history.

    Holds at most ``budget.max_session_exchanges`` exchanges and at most
    ``budget.max_session_text_chars`` combined characters; appending a new
    exchange drops the oldest exchange(s) first if either bound would be
    exceeded. Never written to a file, ``QgsSettings``, a project property,
    or a log -- :meth:`clear` (the **New chat** action) is the only way
    history disappears other than the bounds above.
    """

    def __init__(self, budget: PromptBudget) -> None:
        self._budget = budget
        self._exchanges: List[SessionExchange] = []

    def append(self, user_text: str, assistant_text: str) -> None:
        self._exchanges.append(SessionExchange(user_text, assistant_text))
        self._enforce_bounds()

    def clear(self) -> None:
        self._exchanges.clear()

    def is_empty(self) -> bool:
        return not self._exchanges

    def exchanges(self) -> Tuple[SessionExchange, ...]:
        return tuple(self._exchanges)

    def _enforce_bounds(self) -> None:
        while len(self._exchanges) > self._budget.max_session_exchanges:
            self._exchanges.pop(0)
        while self._exchanges and self._total_chars() > self._budget.max_session_text_chars:
            self._exchanges.pop(0)

    def _total_chars(self) -> int:
        return sum(
            len(exchange.user_text) + len(exchange.assistant_text)
            for exchange in self._exchanges
        )


@dataclass(frozen=True)
class PromptResult:
    """A bounded, deterministic system/user prompt pair for one provider turn."""

    system_prompt: str
    user_prompt: str
    history_truncated: bool


def select_tools_for_scope(
    tool_specs: Sequence[AgentToolSpec], scope: str
) -> List[AgentToolSpec]:
    """Return only the tools whose ``allowed_scopes`` include ``scope``, in
    the same deterministic order as ``tool_specs`` (already sorted by the
    registry). Only these tools are ever advertised to the provider for a
    turn captured with this scope."""
    return [spec for spec in tool_specs if scope in spec.allowed_scopes]


def _omit_if_oversized(event: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    """Replace an oversized ``tool_result`` event's result with a small,
    valid JSON omission record instead of slicing raw JSON text."""
    if event.get("kind") != "tool_result":
        return event
    result = event.get("result")
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if len(serialized) <= max_chars:
        return event
    status = result.get("status") if isinstance(result, dict) else ""
    omitted = dict(event)
    omitted["result"] = {
        "status": status if isinstance(status, str) else "",
        "reason": "tool result omitted: exceeded the prompt budget",
        "original_chars": len(serialized),
    }
    return omitted


def _payload(
    mode: str,
    scope: str,
    tool_descriptions: List[Dict[str, Any]],
    user_text: str,
    bounded_events: List[Dict[str, Any]],
    history_entries: Sequence[SessionExchange],
    history_truncated: bool,
) -> Dict[str, Any]:
    return {
        "mode": mode,
        "scope": scope,
        "tools": tool_descriptions,
        "session_history": [
            {"user_text": item.user_text, "assistant_text": item.assistant_text}
            for item in history_entries
        ],
        "history_truncated": history_truncated,
        "current_request": user_text,
        "current_turn_events": bounded_events,
    }


def build_prompt(
    *,
    static_instructions: str,
    mode: str,
    scope: str,
    tool_specs: Sequence[AgentToolSpec],
    user_text: str,
    session_history: Sequence[SessionExchange],
    current_run_events: Sequence[Dict[str, Any]],
    budget: PromptBudget,
) -> PromptResult:
    """Deterministically build one provider turn's bounded system/user prompt.

    Untrusted dynamic fields (tool descriptions, the user's request, prior
    session text, and this run's own tool results) are always JSON-serialized
    as data, never concatenated into instruction text. Raises
    :class:`PromptBuildError` instead of silently truncating the current
    user request, and instead of sending a request that cannot fit
    ``budget.max_prompt_chars`` even with all prior history dropped.
    """
    if not isinstance(static_instructions, str):
        raise PromptBuildError("static_instructions must be a string.")
    if not isinstance(user_text, str):
        raise PromptBuildError("user_text must be a string.")
    if len(user_text) > budget.max_user_message_chars:
        raise PromptBuildError(
            f"Your message exceeds the {budget.max_user_message_chars}-character "
            "limit; shorten it and try again."
        )

    tool_descriptions = [spec.public_description() for spec in tool_specs]
    bounded_events = [
        _omit_if_oversized(dict(event), budget.max_tool_result_prompt_chars)
        for event in current_run_events
    ]

    def combined_length(history_entries: Sequence[SessionExchange], history_truncated: bool) -> int:
        payload = _payload(
            mode, scope, tool_descriptions, user_text, bounded_events, history_entries, history_truncated
        )
        return len(static_instructions) + len(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )

    # The fixed context (instructions, tools, current request, this run's own
    # trace) is never dropped; if it alone cannot fit, fail before any
    # network request rather than silently degrading it.
    if combined_length((), history_truncated=False) > budget.max_prompt_chars:
        raise PromptBuildError(
            "The required context (instructions, tools, and your current "
            "request) does not fit within the configured prompt budget."
        )

    # Include as much history as fits, dropping the oldest exchange first.
    # `history_truncated=False` is used while sizing candidates: it is the
    # longer of the two possible values for this field, so a candidate that
    # fits here is guaranteed to also fit with the (shorter) final flag.
    ordered_recent_first = list(reversed(session_history))
    included_recent_first: List[SessionExchange] = []
    for exchange in ordered_recent_first:
        candidate_recent_first = included_recent_first + [exchange]
        candidate_chronological = list(reversed(candidate_recent_first))
        if combined_length(candidate_chronological, history_truncated=False) <= budget.max_prompt_chars:
            included_recent_first = candidate_recent_first
        else:
            break

    included = list(reversed(included_recent_first))
    history_truncated = len(included) < len(session_history)
    final_payload = _payload(
        mode, scope, tool_descriptions, user_text, bounded_events, included, history_truncated
    )
    user_prompt = json.dumps(final_payload, ensure_ascii=False, sort_keys=True)
    return PromptResult(
        system_prompt=static_instructions,
        user_prompt=user_prompt,
        history_truncated=history_truncated,
    )
