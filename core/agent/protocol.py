"""Provider-neutral, schema-constrained multi-turn agent turn envelope.

QGIS-free: standard library only. Defines the strict five-key ``agent_turn``
JSON envelope used for every Agent Workspace provider turn, the deterministic
provider-facing JSON Schema for it, and a strict local parser. Provider output
is always untrusted, even when a provider claims strict structured-output
adherence. The parser accepts a narrow set of authority-neutral tool-call field
aliases used by real providers, normalizes them to the canonical contract, and
then performs the same local tool/argument validation. Ambiguous aliases,
unknown fields, malformed shapes, fences, prose, or JSON substrings extracted
from prose are rejected.

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
    PROPOSAL_KIND_LAYER_STYLE,
    PROPOSAL_KIND_NONE,
    PROPOSAL_KIND_PROCESSING_RUN,
    PROPOSAL_KIND_PYTHON_RUN,
    PROPOSAL_KIND_SQL_RUN,
    PROPOSAL_KIND_TRUSTED_SCRIPT_RUN,
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
_PROPOSAL_KIND_ALIASES = {
    # Some structured-output providers shorten the Processing proposal kind.
    # This is a spelling-only normalization to the same locally validated,
    # inert processing_run proposal contract.
    "processing": PROPOSAL_KIND_PROCESSING_RUN,
    "run": PROPOSAL_KIND_PROCESSING_RUN,
    "run_processing": PROPOSAL_KIND_PROCESSING_RUN,
    "processing_run_proposal": PROPOSAL_KIND_PROCESSING_RUN,
    "style": PROPOSAL_KIND_LAYER_STYLE,
    "sql": PROPOSAL_KIND_SQL_RUN,
    "python": PROPOSAL_KIND_PYTHON_RUN,
    "pyqgis": PROPOSAL_KIND_PYTHON_RUN,
    "script": PROPOSAL_KIND_TRUSTED_SCRIPT_RUN,
    "trusted_script": PROPOSAL_KIND_TRUSTED_SCRIPT_RUN,
}
_TOOL_CALL_MARKERS = frozenset({"function", "tool", "tool_call"})

_TURN_TOP_LEVEL_KEYS = frozenset(
    {"action", "assistant_text", "tool_calls", "proposal_kind", "proposal_json"}
)
_CALL_ALIAS_KEYS = frozenset(
    {
        "call_id",
        "id",
        "tool_name",
        "name",
        "tool",
        "arguments_json",
        "arguments",
        "args",
        "parameters",
        "input",
        "function",
        "type",
        "kind",
    }
)


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
    # How many requested calls were dropped for exceeding the per-turn limit.
    # The run loop tells the provider, so a truncated batch is continued rather
    # than silently half-executed.
    dropped_tool_calls: int = 0

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


def _one_alias(
    candidates: List[Tuple[str, Any]],
    index: int,
    label: str,
    *,
    required: bool = True,
) -> Any:
    if len(candidates) > 1:
        raise ProtocolError(
            f"tool_calls[{index}] has ambiguous {label} aliases: "
            f"{sorted(name for name, _value in candidates)}."
        )
    if not candidates:
        if required:
            raise ProtocolError(f"tool_calls[{index}] is missing {label}.")
        return None
    return candidates[0][1]


def _nested_tool_parts(value: Any, index: int, label: str) -> Tuple[Any, Any]:
    if not isinstance(value, dict):
        raise ProtocolError(f"tool_calls[{index}].{label} must be an object.")
    keys = frozenset(value)
    argument_keys = ("arguments", "arguments_json", "args", "parameters", "input")
    allowed = frozenset(("name",) + argument_keys)
    unknown = keys - allowed
    if unknown or "name" not in keys:
        raise ProtocolError(
            f"tool_calls[{index}].{label} has unexpected or missing fields: "
            f"{sorted(unknown | (frozenset({'name'}) - keys))}."
        )
    arguments = _one_alias(
        [(key, value[key]) for key in argument_keys if key in value],
        index,
        f"{label} arguments",
    )
    return value["name"], arguments


def _normalize_tool_call(item: Dict[str, Any], index: int) -> Tuple[str, str, Any]:
    """Normalize only field-name/container aliases; never tool authority."""
    unknown = frozenset(item) - _CALL_ALIAS_KEYS
    if unknown:
        raise ProtocolError(
            f"tool_calls[{index}] has unexpected fields: {sorted(unknown)}."
        )
    call_id_candidates = [
        (key, item[key]) for key in ("call_id", "id") if key in item
    ]
    call_id = _one_alias(
        call_id_candidates, index, "call id", required=False
    )
    if call_id is None:
        # A call id controls only transcript correlation/deduplication. Creating
        # one locally grants no tool or argument authority.
        call_id = f"provider_call_{index + 1}"

    tool_candidates = [
        (key, item[key])
        for key in ("tool_name", "name", "tool")
        if key in item and not isinstance(item[key], dict)
    ]
    arguments_candidates = [
        (key, item[key])
        for key in ("arguments_json", "arguments", "args", "parameters", "input")
        if key in item
    ]
    if "function" in item:
        function_value = item["function"]
        if isinstance(function_value, str):
            tool_candidates.append(("function", function_value))
        else:
            nested_name, nested_arguments = _nested_tool_parts(
                function_value, index, "function"
            )
            tool_candidates.append(("function.name", nested_name))
            arguments_candidates.append(("function.arguments", nested_arguments))
    if "tool" in item and isinstance(item["tool"], dict):
        nested_name, nested_arguments = _nested_tool_parts(
            item["tool"], index, "tool"
        )
        tool_candidates.append(("tool.name", nested_name))
        arguments_candidates.append(("tool.arguments", nested_arguments))
    marker = _one_alias(
        [(key, item[key]) for key in ("type", "kind") if key in item],
        index,
        "call kind",
        required=False,
    )
    if marker is not None and marker not in _TOOL_CALL_MARKERS:
        raise ProtocolError(f"tool_calls[{index}] has an invalid call kind marker.")
    tool_name = _one_alias(tool_candidates, index, "tool name")
    arguments = _one_alias(arguments_candidates, index, "arguments")
    return call_id, tool_name, arguments


def _parse_tool_calls(tool_calls_data, max_tool_calls_per_turn: int) -> Tuple[AgentToolCall, ...]:
    seen_call_ids: set = set()
    parsed_calls: List[AgentToolCall] = []
    for index, item in enumerate(tool_calls_data):
        if not isinstance(item, dict):
            raise ProtocolError(f"tool_calls[{index}] must be an object.")
        call_id, tool_name, arguments_value = _normalize_tool_call(item, index)
        if not isinstance(call_id, str) or not CALL_ID_PATTERN.fullmatch(call_id):
            # A call id is correlation metadata only; it grants no tool or
            # argument authority. Providers sometimes emit punctuation such
            # as ``describe_layer#1`` even when their configured structured
            # schema disallows it. Replacing an unusable id with a unique
            # local one is authority-neutral and keeps the validated tool name
            # and arguments on the unchanged strict path below.
            call_id = f"provider_call_{index + 1}"
        if call_id in seen_call_ids:
            raise ProtocolError(f"Duplicate call_id within this turn: {call_id!r}.")
        seen_call_ids.add(call_id)
        if not isinstance(tool_name, str) or not TOOL_NAME_PATTERN.fullmatch(tool_name):
            raise ProtocolError(f"tool_calls[{index}] has an invalid tool_name: {tool_name!r}.")
        if isinstance(arguments_value, dict):
            arguments_json = json.dumps(
                arguments_value, ensure_ascii=False, separators=(",", ":")
            )
        elif isinstance(arguments_value, str):
            arguments_json = arguments_value
        else:
            raise ProtocolError(
                f"tool_calls[{index}] arguments must be an object or JSON-object string."
            )
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

    # `action` is the one field with no safe default -- it decides everything.
    # The other four are filled from their most restrictive value when a
    # provider omits them, because real providers routinely drop keys that do
    # not apply to the turn they are producing (a `final` answer with no
    # `tool_calls` key, a `proposal` with no `tool_calls`/`proposal`-less turn
    # with no `proposal_json`). Defaulting a *missing* key to its safe value
    # never widens authority: `tool_calls` defaults to none, `proposal_kind` to
    # "none", `proposal_json` to "". Unknown extra keys are ignored rather than
    # rejected -- only the five known keys are ever read -- so a stray
    # `reasoning`/`thoughts` field a provider adds cannot fail an otherwise
    # valid turn. The inner tool-argument and proposal validators stay strict;
    # that is where authority actually lives.
    if "action" not in data:
        raise ProtocolError("Provider response is missing the required 'action' field.")
    action = data["action"]
    if action not in _ACTIONS:
        raise ProtocolError(f"Unknown agent_turn action: {action!r}.")

    assistant_text = data.get("assistant_text", "")
    if not isinstance(assistant_text, str):
        raise ProtocolError("assistant_text must be a string.")
    if len(assistant_text) > MAX_ASSISTANT_TEXT_CHARS:
        raise ProtocolError(
            f"assistant_text exceeds the {MAX_ASSISTANT_TEXT_CHARS}-character safety limit."
        )

    tool_calls_data = data.get("tool_calls", [])
    if tool_calls_data is None:
        tool_calls_data = []
    if not isinstance(tool_calls_data, list):
        raise ProtocolError("tool_calls must be an array.")
    # An over-long batch is truncated, not rejected. Running *fewer* calls than
    # the provider asked for cannot widen authority -- every surviving call is
    # still name-, argument-, scope- and risk-checked by the controller -- while
    # rejecting the turn threw away a correct multi-step plan. A live workflow
    # run died exactly this way twice in one session: it re-sent an oversized
    # batch after a repair turn, and the second one had no repair left. The run
    # loop reports the dropped count back so the rest arrive next turn.
    dropped_tool_calls = 0
    if len(tool_calls_data) > max_tool_calls_per_turn:
        dropped_tool_calls = len(tool_calls_data) - max_tool_calls_per_turn
        tool_calls_data = tool_calls_data[:max_tool_calls_per_turn]

    proposal_kind = data.get("proposal_kind", PROPOSAL_KIND_NONE)
    if proposal_kind is None or proposal_kind == "":
        proposal_kind = PROPOSAL_KIND_NONE
    if isinstance(proposal_kind, str):
        proposal_kind = _PROPOSAL_KIND_ALIASES.get(proposal_kind, proposal_kind)
    if proposal_kind not in ALL_PROPOSAL_KINDS:
        raise ProtocolError(f"Unknown proposal_kind: {proposal_kind!r}.")
    proposal_json = data.get("proposal_json", "")
    if proposal_json is None:
        proposal_json = ""
    if not isinstance(proposal_json, str):
        raise ProtocolError("proposal_json must be a string.")
    if len(proposal_json) > MAX_PROPOSAL_JSON_CHARS:
        raise ProtocolError(
            f"proposal_json exceeds the {MAX_PROPOSAL_JSON_CHARS}-character safety limit."
        )

    # A few providers use the tool_calls action marker for a terminal response
    # even though they also populate the complete proposal fields. Treat that
    # exact, internally consistent shape as a proposal and discard the repeated
    # calls below. This grants no tool authority: the calls are never parsed or
    # executed, and the inert proposal still crosses every strict proposal and
    # live-runtime validation boundary. Partial proposal data remains invalid.
    if (
        action == ACTION_TOOL_CALLS
        and proposal_kind in PROPOSABLE_KINDS
        and proposal_json.strip()
    ):
        action = ACTION_PROPOSAL

    # DeepSeek can emit a complete inert proposal with the terminal ``final``
    # marker. Treating that marker as a proposal is authority-reducing: no
    # tool calls are executed, and the proposal still passes the strict parser
    # plus the live freshness, scope, and approval boundaries downstream.
    if (
        action == ACTION_FINAL
        and proposal_kind in PROPOSABLE_KINDS
        and proposal_json.strip()
    ):
        action = ACTION_PROPOSAL

    if action == ACTION_FINAL:
        _require_no_proposal(proposal_kind, proposal_json, "A final turn must not carry proposal data.")
        if not assistant_text.strip():
            # Nothing terminal was actually said, so the calls are the only
            # content and the envelope is genuinely inconsistent.
            _require_no_tool_calls(
                tool_calls_data, "A final turn must not include tool calls."
            )
            raise ProtocolError("A final turn must include a non-empty assistant_text.")
        # A provider that answers *and* repeats the calls it just made is the
        # same shape the proposal branch below already tolerates, and dropping
        # the calls is authority-reducing for exactly the same reason: they are
        # never parsed or executed. Failing instead threw away a complete answer
        # -- an owner's "what is the min and max of this field?" died here with
        # the answer already written in assistant_text.
        return AgentTurn(action=ACTION_FINAL, assistant_text=assistant_text, tool_calls=())

    if action == ACTION_PROPOSAL:
        # Some providers repeat their last read-only calls beside a terminal
        # proposal. Dropping those calls is authority-reducing: they are never
        # parsed or executed, while the inert proposal still passes the full
        # proposal parser, receipt validation and live runtime boundary.
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
        action=ACTION_TOOL_CALLS,
        assistant_text=assistant_text,
        tool_calls=parsed_calls,
        dropped_tool_calls=dropped_tool_calls,
    )


def _require_no_tool_calls(tool_calls_data, message: str) -> None:
    if tool_calls_data:
        raise ProtocolError(message)


def _require_no_proposal(proposal_kind: str, proposal_json: str, message: str) -> None:
    if proposal_kind != PROPOSAL_KIND_NONE or proposal_json != "":
        raise ProtocolError(message)
