"""Provider-neutral, schema-constrained multi-turn agent turn envelope.

QGIS-free: standard library only. Defines the strict five-key ``agent_turn``
JSON envelope used for every Agent Workspace provider turn, the deterministic
provider-facing JSON Schema for it, and a strict local parser. Provider output
is always untrusted, even when a provider claims strict structured-output
adherence: malformed shape, extra fields, fences, leading/trailing prose, or
JSON substrings extracted from prose are rejected outright rather than
repaired. The legacy Phase 02 three-key shape is rejected after Phase 03.

The envelope now carries an optional, terminal, inert *proposal*: a
`model_patch` or `layer_style` draft encoded as a JSON-object string, parsed
here only into a bounded pure draft (via ``proposals.py``) -- never into a QGIS
object. Live target/graph validation happens later at the trusted runtime
proposal boundary.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .contracts import AgentToolCall, CALL_ID_PATTERN, ContractError, TOOL_NAME_PATTERN
from .proposals import (
    ALL_PROPOSAL_KINDS,
    MAX_PROPOSAL_JSON_CHARS,
    PROPOSABLE_KINDS,
    PROPOSAL_KIND_NONE,
    ProposalError,
    parse_proposal,
)

# A raw provider response larger than this is rejected before any JSON
# parsing is attempted at all (defense in depth against an adversarial or
# malfunctioning provider sending an enormous payload).
MAX_RAW_RESPONSE_CHARS = 100_000

# Bound on the user-visible assistant_text field of one provider turn.
MAX_ASSISTANT_TEXT_CHARS = 8_000

# Bound on the raw arguments_json string of one tool call, checked before
# json.loads is attempted on it. The decoded object is additionally bounded
# by AgentToolCall's own per-string/total-character budgets.
MAX_ARGUMENTS_JSON_CHARS = 20_000

ACTION_TOOL_CALLS = "tool_calls"
ACTION_FINAL = "final"
ACTION_PROPOSAL = "proposal"
_ACTIONS = (ACTION_TOOL_CALLS, ACTION_FINAL, ACTION_PROPOSAL)

_TURN_TOP_LEVEL_KEYS = frozenset(
    {"action", "assistant_text", "tool_calls", "proposal_kind", "proposal_json"}
)
_CALL_KEYS = frozenset({"call_id", "tool_name", "arguments_json"})


class ProtocolError(ValueError):
    """Raised when raw provider text violates the strict agent_turn envelope."""


def _reject_duplicate_keys(pairs):
    """``json.loads`` object hook that rejects duplicate keys at every level.

    Standard ``json`` silently keeps the last value for a repeated key; an
    adversarial provider could exploit that to smuggle a second ``action`` or
    a second ``arguments_json`` value past a naive shape check. Raising here
    makes any duplicate object key fail closed instead.
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate object key: {key!r}")
        result[key] = value
    return result


def _strict_json_loads(text: str, where: str = "Provider response") -> Any:
    """Parse ``text`` as one JSON value, failing closed on every expected
    decoder error.

    Rejects duplicate object keys, and converts ``JSONDecodeError``,
    ``RecursionError`` (a deeply nested but sub-limit payload can overflow the
    decoder's own recursion), and any other ``ValueError`` into a bounded
    :class:`ProtocolError` so nothing escapes into a Qt callback as an
    ordinary Python exception.
    """
    try:
        return json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except RecursionError:
        # Do no further recursive work (str(error)/chaining) near the limit.
        raise ProtocolError(f"{where} JSON nesting is too deep to parse safely.") from None
    except ValueError as error:  # includes json.JSONDecodeError and the hook's ValueError
        raise ProtocolError(f"{where} was not valid JSON: {error}") from error


@dataclass(frozen=True)
class AgentTurn:
    """One strictly parsed provider turn.

    ``tool_calls`` holds already-constructed, already-validated
    :class:`AgentToolCall` instances -- there is no separate "parsed call"
    type, so nothing downstream can see an unvalidated tool name or argument
    set. ``proposal`` is a bounded pure proposal draft (never a QGIS object)
    and is only populated for an ``ACTION_PROPOSAL`` turn.
    """

    action: str
    assistant_text: str
    tool_calls: Tuple[AgentToolCall, ...] = field(default_factory=tuple)
    proposal_kind: str = PROPOSAL_KIND_NONE
    proposal: Optional[Any] = None

    @property
    def is_final(self) -> bool:
        return self.action == ACTION_FINAL

    @property
    def is_proposal(self) -> bool:
        return self.action == ACTION_PROPOSAL


def agent_turn_response_schema(max_tool_calls_per_turn: int) -> Dict[str, Any]:
    """Return the deterministic, provider-facing JSON Schema for one agent_turn.

    Uses only conservative keywords already exercised elsewhere in this
    codebase across every configured provider (``type``, ``properties``,
    ``required``, ``additionalProperties``, ``enum``, ``items``,
    ``maxItems``). The semantic five-key table is enforced locally in
    :func:`parse_agent_turn`, not through provider-specific conditional
    schemas, so the schema stays portable.
    """
    if (
        not isinstance(max_tool_calls_per_turn, int)
        or isinstance(max_tool_calls_per_turn, bool)
        or max_tool_calls_per_turn < 1
    ):
        raise ProtocolError("max_tool_calls_per_turn must be a positive integer.")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {"type": "string", "enum": list(_ACTIONS)},
            "assistant_text": {"type": "string"},
            "tool_calls": {
                "type": "array",
                "maxItems": max_tool_calls_per_turn,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "call_id": {"type": "string"},
                        "tool_name": {"type": "string"},
                        "arguments_json": {"type": "string"},
                    },
                    "required": ["call_id", "tool_name", "arguments_json"],
                },
            },
            "proposal_kind": {"type": "string", "enum": list(ALL_PROPOSAL_KINDS)},
            "proposal_json": {"type": "string"},
        },
        "required": [
            "action",
            "assistant_text",
            "tool_calls",
            "proposal_kind",
            "proposal_json",
        ],
    }


def _parse_tool_calls(tool_calls_data, max_tool_calls_per_turn: int) -> Tuple[AgentToolCall, ...]:
    seen_call_ids: set = set()
    parsed_calls: List[AgentToolCall] = []
    for index, item in enumerate(tool_calls_data):
        if not isinstance(item, dict):
            raise ProtocolError(f"tool_calls[{index}] must be an object.")
        item_keys = frozenset(item.keys())
        if item_keys != _CALL_KEYS:
            raise ProtocolError(
                f"tool_calls[{index}] has unexpected or missing fields: "
                f"{sorted(item_keys ^ _CALL_KEYS)}."
            )
        call_id = item["call_id"]
        tool_name = item["tool_name"]
        arguments_json = item["arguments_json"]
        if not isinstance(call_id, str) or not CALL_ID_PATTERN.fullmatch(call_id):
            raise ProtocolError(f"tool_calls[{index}] has an invalid call_id: {call_id!r}.")
        if call_id in seen_call_ids:
            raise ProtocolError(f"Duplicate call_id within this turn: {call_id!r}.")
        seen_call_ids.add(call_id)
        if not isinstance(tool_name, str) or not TOOL_NAME_PATTERN.fullmatch(tool_name):
            raise ProtocolError(f"tool_calls[{index}] has an invalid tool_name: {tool_name!r}.")
        if not isinstance(arguments_json, str):
            raise ProtocolError(f"tool_calls[{index}].arguments_json must be a string.")
        if len(arguments_json) > MAX_ARGUMENTS_JSON_CHARS:
            raise ProtocolError(
                f"tool_calls[{index}].arguments_json exceeds the "
                f"{MAX_ARGUMENTS_JSON_CHARS}-character safety limit."
            )
        arguments = _strict_json_loads(
            arguments_json, where=f"tool_calls[{index}].arguments_json"
        )
        if not isinstance(arguments, dict):
            raise ProtocolError(
                f"tool_calls[{index}].arguments_json must decode to a JSON object."
            )
        try:
            call = AgentToolCall(call_id=call_id, tool_name=tool_name, arguments=arguments)
        except ContractError as error:
            raise ProtocolError(str(error)) from error
        parsed_calls.append(call)
    return tuple(parsed_calls)


def parse_agent_turn(raw_text: str, max_tool_calls_per_turn: int) -> AgentTurn:
    """Strictly parse one raw provider response into an :class:`AgentTurn`.

    Never repairs malformed input: no fence stripping, no prose trimming, no
    JSON-substring extraction. A response that is not exactly one valid,
    exact-five-key ``agent_turn`` JSON object obeying the action/proposal
    semantic table raises :class:`ProtocolError`.
    """
    if (
        not isinstance(max_tool_calls_per_turn, int)
        or isinstance(max_tool_calls_per_turn, bool)
        or max_tool_calls_per_turn < 1
    ):
        raise ProtocolError("max_tool_calls_per_turn must be a positive integer.")
    if not isinstance(raw_text, str):
        raise ProtocolError("Provider response must be text.")
    if len(raw_text) > MAX_RAW_RESPONSE_CHARS:
        raise ProtocolError(
            f"Provider response exceeds the {MAX_RAW_RESPONSE_CHARS}-character safety limit."
        )

    stripped = raw_text.strip()
    if not stripped:
        raise ProtocolError("Provider response was empty.")
    data = _strict_json_loads(stripped)
    if not isinstance(data, dict):
        raise ProtocolError("Provider response must be a single JSON object.")

    actual_keys = frozenset(data.keys())
    if actual_keys != _TURN_TOP_LEVEL_KEYS:
        raise ProtocolError(
            "Provider response has unexpected or missing fields: "
            f"{sorted(actual_keys ^ _TURN_TOP_LEVEL_KEYS)}."
        )

    action = data["action"]
    if action not in _ACTIONS:
        raise ProtocolError(f"Unknown agent_turn action: {action!r}.")

    assistant_text = data["assistant_text"]
    if not isinstance(assistant_text, str):
        raise ProtocolError("assistant_text must be a string.")
    if len(assistant_text) > MAX_ASSISTANT_TEXT_CHARS:
        raise ProtocolError(
            f"assistant_text exceeds the {MAX_ASSISTANT_TEXT_CHARS}-character safety limit."
        )

    tool_calls_data = data["tool_calls"]
    if not isinstance(tool_calls_data, list):
        raise ProtocolError("tool_calls must be an array.")
    if len(tool_calls_data) > max_tool_calls_per_turn:
        raise ProtocolError(
            f"tool_calls exceeds the configured {max_tool_calls_per_turn}-call turn limit."
        )

    proposal_kind = data["proposal_kind"]
    if proposal_kind not in ALL_PROPOSAL_KINDS:
        raise ProtocolError(f"Unknown proposal_kind: {proposal_kind!r}.")
    proposal_json = data["proposal_json"]
    if not isinstance(proposal_json, str):
        raise ProtocolError("proposal_json must be a string.")
    if len(proposal_json) > MAX_PROPOSAL_JSON_CHARS:
        raise ProtocolError(
            f"proposal_json exceeds the {MAX_PROPOSAL_JSON_CHARS}-character safety limit."
        )

    if action == ACTION_FINAL:
        _require_no_tool_calls(tool_calls_data, "A final turn must not include tool calls.")
        _require_no_proposal(proposal_kind, proposal_json, "A final turn must not carry proposal data.")
        if not assistant_text.strip():
            raise ProtocolError("A final turn must include a non-empty assistant_text.")
        return AgentTurn(action=ACTION_FINAL, assistant_text=assistant_text, tool_calls=())

    if action == ACTION_PROPOSAL:
        _require_no_tool_calls(tool_calls_data, "A proposal turn must not include tool calls.")
        if not assistant_text.strip():
            raise ProtocolError("A proposal turn must include a non-empty assistant_text.")
        if proposal_kind not in PROPOSABLE_KINDS:
            raise ProtocolError(
                "A proposal turn must set proposal_kind to a proposable kind "
                "(model_patch, layer_style, processing_run, or model_run)."
            )
        if not proposal_json.strip():
            raise ProtocolError("A proposal turn must include a non-empty proposal_json object.")
        try:
            proposal = parse_proposal(proposal_kind, proposal_json)
        except ProposalError as error:
            raise ProtocolError(str(error)) from error
        return AgentTurn(
            action=ACTION_PROPOSAL,
            assistant_text=assistant_text,
            tool_calls=(),
            proposal_kind=proposal_kind,
            proposal=proposal,
        )

    # action == ACTION_TOOL_CALLS
    _require_no_proposal(
        proposal_kind, proposal_json, "A tool_calls turn must not carry proposal data."
    )
    if not tool_calls_data:
        raise ProtocolError("A tool_calls turn must request at least one tool call.")
    parsed_calls = _parse_tool_calls(tool_calls_data, max_tool_calls_per_turn)
    return AgentTurn(
        action=ACTION_TOOL_CALLS, assistant_text=assistant_text, tool_calls=parsed_calls
    )


def _require_no_tool_calls(tool_calls_data, message: str) -> None:
    if tool_calls_data:
        raise ProtocolError(message)


def _require_no_proposal(proposal_kind: str, proposal_json: str, message: str) -> None:
    if proposal_kind != PROPOSAL_KIND_NONE or proposal_json != "":
        raise ProtocolError(message)
