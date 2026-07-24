"""Typed, dependency-free contracts for the SmartModeler Agent Workspace.

Every later agent feature (LLM loop, approval UI, mutating tools) is expected
to be built on top of these types. Phase 01 has no LLM loop, but the run
limits and result contracts are defined now so the boundary is stable.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, List, Tuple

TOOL_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
CALL_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
REASON_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")

MAX_TOOL_NAME_LENGTH = 64
MAX_TITLE_LENGTH = 120
MAX_DESCRIPTION_LENGTH = 500

# Bounds applied to one AgentToolCall's arguments (small; these are short
# structured inputs such as a layer id or a search query).
MAX_TEXT_ARG_LENGTH = 2000
MAX_ARGUMENT_TOTAL_CHARS = 20000

# Bounds applied to a successful AgentToolResult's data payload (larger;
# these are bounded lists of metadata, not free-form provider text).
MAX_RESULT_STRING_LENGTH = 20000
MAX_RESULT_TOTAL_CHARS = 200000

# Bound applied to an AgentToolResult's human-readable message field.
MAX_RESULT_TEXT_LENGTH = 20000
MAX_PROMPT_TEXT_LENGTH = 12000

# Default budget for the *combined* system + conversation prompt of one agent
# turn. This is a different quantity from MAX_PROMPT_TEXT_LENGTH, which bounds
# a single free-text message: a turn's prompt also carries the static agent
# instructions (~7 200 chars) and every scope-allowed tool's public schema
# (~3 600 chars in the widest scope), so the fixed context alone is close to
# 11 000 characters before the user has typed anything or a single tool result
# has been recorded. Reusing the 12 000-char message bound here left roughly
# one tool result of headroom and made any real multi-tool run fail with
# "does not fit within the configured prompt budget" on its third call.
MAX_AGENT_PROMPT_CHARS = 60000

MAX_JSON_ARRAY_ITEMS = 500
MAX_JSON_OBJECT_KEYS = 200
MAX_JSON_NESTING_DEPTH = 20

# Hard maxima for AgentRunLimits fields: generous enough for any plausible
# future agent loop, small enough that a malformed/adversarial value cannot
# turn a "run limit" into an effectively unbounded run.
MAX_ALLOWED_TURNS = 100
MAX_ALLOWED_TOOL_CALLS_PER_RUN = 500
MAX_ALLOWED_TOOL_CALLS_PER_TURN = 50
MAX_ALLOWED_PROMPT_CHARS = 100_000
MAX_ALLOWED_RESULT_TEXT_CHARS = 200_000

# Bound applied to AgentToolResult.reason_code: a short, stable, lowercase
# snake_case identifier suitable for tests/audit logs (empty is valid for a
# successful result, which carries no reason code at all).
MAX_REASON_CODE_LENGTH = 64

_ALLOWED_SCHEMA_PROPERTY_TYPES = ("string", "integer")

# Phase 01's input_schema subset: exactly these four top-level keywords are
# required (no more, no less), and only these per-type property keywords are
# recognized. Anything outside this set is rejected rather than silently
# ignored, so a schema can never advertise a constraint (e.g. "pattern")
# that dispatch validation does not actually enforce.
_SCHEMA_TOP_LEVEL_KEYS = frozenset(
    {"type", "properties", "required", "additionalProperties"}
)
_STRING_PROPERTY_KEYS = frozenset({"type", "minLength", "maxLength"})
_INTEGER_PROPERTY_KEYS = frozenset({"type", "minimum", "maximum"})


def _is_mapping(value: Any) -> bool:
    return isinstance(value, (dict, MappingProxyType))


def _is_sequence(value: Any) -> bool:
    return isinstance(value, (list, tuple))


def _freeze_json_value(value: Any) -> Any:
    """Recursively convert a plain-or-already-frozen JSON-compatible value
    into a brand-new immutable equivalent (dict/``MappingProxyType`` ->
    ``MappingProxyType``, list/tuple -> tuple).

    Accepting both a plain ``dict`` and an already-frozen ``MappingProxyType``
    on the read side (rather than calling ``copy.deepcopy`` first, which
    cannot pickle a ``MappingProxyType``) lets a second :class:`AgentToolSpec`
    be constructed directly from another registered spec's
    ``input_schema`` -- every container encountered is rebuilt fresh here, so
    the result never aliases the input's containers regardless of which form
    the input arrived in.
    """
    if isinstance(value, (dict, MappingProxyType)):
        return MappingProxyType(
            {key: _freeze_json_value(sub) for key, sub in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(sub) for sub in value)
    return value


def _thaw_json_value(value: Any) -> Any:
    """Recursively convert a frozen schema value back into a plain,
    JSON-serializable, freshly-allocated dict/list (the inverse of
    :func:`_freeze_json_value`), safe to hand to UI/discovery code."""
    if isinstance(value, MappingProxyType):
        return {key: _thaw_json_value(sub) for key, sub in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json_value(sub) for sub in value]
    return value


class AgentMode:
    """How the Agent Workspace is currently allowed to act."""

    ASK = "ask"
    PLAN = "plan"
    ACT = "act"
    ALL: Tuple[str, ...] = (ASK, PLAN, ACT)


class AgentScope:
    """What the current tool call is allowed to inspect or affect."""

    PROJECT = "project"
    ACTIVE_LAYER = "active_layer"
    CURRENT_MODEL = "current_model"
    PLUGINS = "plugins"
    ALL: Tuple[str, ...] = (PROJECT, ACTIVE_LAYER, CURRENT_MODEL, PLUGINS)


class AgentRisk:
    """Risk classification of a tool, used by the fail-closed policy engine."""

    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    MUTATING = "mutating"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"
    PROHIBITED = "prohibited"
    ALL: Tuple[str, ...] = (
        READ_ONLY,
        REVERSIBLE,
        MUTATING,
        DESTRUCTIVE,
        EXTERNAL,
        PROHIBITED,
    )


class PolicyOutcome:
    """Result of a policy decision for one tool call."""

    ALLOW = "allow"
    PREVIEW_ONLY = "preview_only"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"
    ALL: Tuple[str, ...] = (ALLOW, PREVIEW_ONLY, REQUIRE_APPROVAL, DENY)


class AgentResultStatus:
    """The four outcomes an :class:`AgentToolResult` can carry."""

    SUCCESS = "success"
    DENIED = "denied"
    APPROVAL_REQUIRED = "approval_required"
    FAILED = "failed"
    ALL: Tuple[str, ...] = (SUCCESS, DENIED, APPROVAL_REQUIRED, FAILED)


class ContractError(ValueError):
    """Raised when a contract object fails deterministic validation."""


def validate_json_value(
    value: Any,
    max_string_length: int = MAX_TEXT_ARG_LENGTH,
    max_total_chars: int = MAX_ARGUMENT_TOTAL_CHARS,
) -> Any:
    """Recursively validate a bounded JSON-compatible value.

    Rejects non-finite floats (NaN/Infinity), unsupported Python objects,
    collections beyond the safety bounds, any individual string longer than
    ``max_string_length``, and a running total of string characters (across
    every string and object key encountered) beyond ``max_total_chars``.
    Returns the value unchanged (after recursing into lists/dicts) so callers
    can store the validated result. Never silently truncates: a value that
    violates a bound is rejected outright, not shortened.
    """
    budget = {"chars": 0}

    def _consume(text: str) -> None:
        if len(text) > max_string_length:
            raise ContractError(
                f"String value exceeds the {max_string_length}-character limit."
            )
        budget["chars"] += len(text)
        if budget["chars"] > max_total_chars:
            raise ContractError(
                f"Total text content exceeds the {max_total_chars}-character limit."
            )

    def _validate(item: Any, depth: int) -> Any:
        if depth > MAX_JSON_NESTING_DEPTH:
            raise ContractError("Value nesting exceeds the safety limit.")
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, str):
            _consume(item)
            return item
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ContractError("Non-finite numbers are not allowed.")
            return item
        if isinstance(item, int):
            return item
        if isinstance(item, list):
            if len(item) > MAX_JSON_ARRAY_ITEMS:
                raise ContractError("Array exceeds the safety limit.")
            return [_validate(sub, depth + 1) for sub in item]
        if isinstance(item, tuple):
            return _validate(list(item), depth)
        if isinstance(item, dict):
            if len(item) > MAX_JSON_OBJECT_KEYS:
                raise ContractError("Object exceeds the safety limit.")
            result: Dict[str, Any] = {}
            for key, sub in item.items():
                if not isinstance(key, str):
                    raise ContractError("Object keys must be strings.")
                _consume(key)
                result[key] = _validate(sub, depth + 1)
            return result
        raise ContractError(f"Unsupported value type: {type(item).__name__}")

    return _validate(value, 0)


def _require_bounded_int(value: Any, field_name: str, minimum: int, maximum: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ContractError(f"{field_name} must be an integer.")
    if not minimum <= value <= maximum:
        raise ContractError(f"{field_name} must be between {minimum} and {maximum}.")


@dataclass(frozen=True)
class AgentRunLimits:
    """Validated, hard-bounded limits for a future multi-turn agent run.

    Phase 01 has no LLM loop, so only the per-call limits are exercised by
    the controller today, but every field is validated now so a malformed
    or adversarial limits object (negative, zero, boolean, or absurdly
    large) can never be constructed in the first place.
    """

    max_turns: int = 12
    max_tool_calls_per_run: int = 24
    max_tool_calls_per_turn: int = 4
    max_prompt_chars: int = MAX_AGENT_PROMPT_CHARS
    max_result_text_chars: int = MAX_RESULT_TEXT_LENGTH

    def __post_init__(self) -> None:
        _require_bounded_int(self.max_turns, "max_turns", 1, MAX_ALLOWED_TURNS)
        _require_bounded_int(
            self.max_tool_calls_per_run,
            "max_tool_calls_per_run",
            1,
            MAX_ALLOWED_TOOL_CALLS_PER_RUN,
        )
        _require_bounded_int(
            self.max_tool_calls_per_turn,
            "max_tool_calls_per_turn",
            1,
            MAX_ALLOWED_TOOL_CALLS_PER_TURN,
        )
        _require_bounded_int(
            self.max_prompt_chars, "max_prompt_chars", 1, MAX_ALLOWED_PROMPT_CHARS
        )
        _require_bounded_int(
            self.max_result_text_chars,
            "max_result_text_chars",
            1,
            MAX_ALLOWED_RESULT_TEXT_CHARS,
        )
        if self.max_tool_calls_per_turn > self.max_tool_calls_per_run:
            raise ContractError(
                "max_tool_calls_per_turn cannot exceed max_tool_calls_per_run."
            )


def _validate_schema_shape(schema: Any, tool_name: str) -> None:
    """Validate the small, deterministic Phase 01 input-schema subset.

    Supported subset only: object/string/integer types, ``properties``,
    ``required``, ``additionalProperties: false``, string ``minLength``/
    ``maxLength``, and integer ``minimum``/``maximum``. This is not a general
    JSON Schema engine and never will be for Phase 01. Unlike a permissive
    validator, this rejects anything it does not recognize: unknown
    top-level or per-property keywords, non-string property names, inverted
    bounds, and duplicate ``required`` entries all fail closed instead of
    being silently accepted and ignored by dispatch validation.
    """
    if not _is_mapping(schema):
        raise ContractError(f"Invalid input_schema for {tool_name}: must be an object.")
    schema_keys = set(schema.keys())
    unknown_top_level = schema_keys - _SCHEMA_TOP_LEVEL_KEYS
    if unknown_top_level:
        raise ContractError(
            f"Invalid input_schema for {tool_name}: unknown keyword(s) "
            f"{sorted(unknown_top_level)}."
        )
    missing_top_level = _SCHEMA_TOP_LEVEL_KEYS - schema_keys
    if missing_top_level:
        raise ContractError(
            f"Invalid input_schema for {tool_name}: missing keyword(s) "
            f"{sorted(missing_top_level)}."
        )
    if schema["type"] != "object":
        raise ContractError(f"Invalid input_schema for {tool_name}: type must be 'object'.")
    if schema["additionalProperties"] is not False:
        raise ContractError(
            f"Invalid input_schema for {tool_name}: additionalProperties must be false."
        )
    properties = schema["properties"]
    if not _is_mapping(properties):
        raise ContractError(f"Invalid input_schema for {tool_name}: properties must be an object.")
    required = schema["required"]
    if not _is_sequence(required) or any(not isinstance(item, str) for item in required):
        raise ContractError(f"Invalid input_schema for {tool_name}: required must be a string array.")
    if len(required) != len(set(required)):
        raise ContractError(
            f"Invalid input_schema for {tool_name}: required contains duplicate entries."
        )
    for name, definition in properties.items():
        if not isinstance(name, str):
            raise ContractError(
                f"Invalid input_schema for {tool_name}: property names must be strings."
            )
        if not _is_mapping(definition):
            raise ContractError(f"Invalid input_schema property {name!r} for {tool_name}.")
        prop_type = definition.get("type")
        if prop_type not in _ALLOWED_SCHEMA_PROPERTY_TYPES:
            raise ContractError(
                f"Unsupported input_schema property type for {tool_name}.{name}: {prop_type!r}."
            )
        allowed_keys = _STRING_PROPERTY_KEYS if prop_type == "string" else _INTEGER_PROPERTY_KEYS
        unknown_property_keys = set(definition.keys()) - allowed_keys
        if unknown_property_keys:
            raise ContractError(
                f"Invalid input_schema property {tool_name}.{name}: unknown keyword(s) "
                f"{sorted(unknown_property_keys)}."
            )
        if prop_type == "string":
            min_length = definition.get("minLength")
            max_length = definition.get("maxLength")
            for bound_key, bound_value in (("minLength", min_length), ("maxLength", max_length)):
                if bound_value is not None and (
                    isinstance(bound_value, bool)
                    or not isinstance(bound_value, int)
                    or bound_value < 0
                ):
                    raise ContractError(f"Invalid {bound_key} for {tool_name}.{name}.")
            if min_length is not None and max_length is not None and min_length > max_length:
                raise ContractError(
                    f"Invalid input_schema for {tool_name}.{name}: minLength exceeds maxLength."
                )
        else:  # integer
            minimum = definition.get("minimum")
            maximum = definition.get("maximum")
            for bound_key, bound_value in (("minimum", minimum), ("maximum", maximum)):
                if bound_value is not None and (
                    isinstance(bound_value, bool) or not isinstance(bound_value, int)
                ):
                    raise ContractError(f"Invalid {bound_key} for {tool_name}.{name}.")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ContractError(
                    f"Invalid input_schema for {tool_name}.{name}: minimum exceeds maximum."
                )
    for key in required:
        if key not in properties:
            raise ContractError(
                f"Invalid input_schema for {tool_name}: required key {key!r} has no property."
            )


def validate_tool_arguments(schema: Dict[str, Any], arguments: Dict[str, Any]) -> None:
    """Validate ``arguments`` against a Phase 01 tool input schema.

    Raises :class:`ContractError` for unknown properties, missing required
    properties, wrong types (a bool is never accepted as an integer), and
    out-of-range values. Deterministic and dependency-free; not a general
    JSON Schema validator.
    """
    properties: Dict[str, Any] = schema.get("properties", {})
    required: List[str] = schema.get("required", [])
    for key in arguments:
        if key not in properties:
            raise ContractError(f"Unknown argument: {key!r}.")
    for key in required:
        if key not in arguments:
            raise ContractError(f"Missing required argument: {key!r}.")
    for key, value in arguments.items():
        definition = properties[key]
        prop_type = definition["type"]
        if prop_type == "string":
            if not isinstance(value, str):
                raise ContractError(f"Argument {key!r} must be a string.")
            min_length = definition.get("minLength", 0)
            max_length = definition.get("maxLength")
            if len(value) < min_length or (max_length is not None and len(value) > max_length):
                raise ContractError(f"Argument {key!r} is out of the allowed length range.")
        elif prop_type == "integer":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ContractError(f"Argument {key!r} must be an integer.")
            minimum = definition.get("minimum")
            maximum = definition.get("maximum")
            if minimum is not None and value < minimum:
                raise ContractError(f"Argument {key!r} is below the allowed minimum.")
            if maximum is not None and value > maximum:
                raise ContractError(f"Argument {key!r} is above the allowed maximum.")
        else:  # pragma: no cover - unreachable once _validate_schema_shape ran
            raise ContractError(f"Unsupported argument type for {key!r}.")


@dataclass(frozen=True)
class AgentToolSpec:
    """A registered tool's public description, safety classification, and
    machine-readable argument schema."""

    name: str
    title: str
    description: str
    risk: str
    input_schema: Dict[str, Any]
    allowed_scopes: Tuple[str, ...] = field(default_factory=lambda: tuple(AgentScope.ALL))

    def __post_init__(self) -> None:
        if (
            not isinstance(self.name, str)
            or len(self.name) > MAX_TOOL_NAME_LENGTH
            or not TOOL_NAME_PATTERN.fullmatch(self.name)
        ):
            raise ContractError(f"Invalid tool name: {self.name!r}")
        if (
            not isinstance(self.title, str)
            or not self.title.strip()
            or len(self.title) > MAX_TITLE_LENGTH
        ):
            raise ContractError(f"Invalid tool title for {self.name}.")
        if (
            not isinstance(self.description, str)
            or not self.description.strip()
            or len(self.description) > MAX_DESCRIPTION_LENGTH
        ):
            raise ContractError(f"Invalid tool description for {self.name}.")
        if self.risk not in AgentRisk.ALL:
            raise ContractError(f"Unknown risk level for {self.name}: {self.risk!r}")
        scopes = tuple(self.allowed_scopes)
        if not scopes or any(scope not in AgentScope.ALL for scope in scopes):
            raise ContractError(f"Invalid allowed scopes for {self.name}.")
        object.__setattr__(self, "allowed_scopes", scopes)
        _validate_schema_shape(self.input_schema, self.name)
        # _freeze_json_value rebuilds every container fresh (it never returns
        # an input container unchanged), so this single call both detaches
        # the stored schema from the caller's own mutable dict/list objects
        # and makes it recursively immutable -- nothing obtained afterwards
        # (not the caller's original, not this spec, not a copy returned by
        # the registry) can mutate the stored schema in place. Accepting an
        # already-frozen MappingProxyType/tuple input here (P2-001) also
        # means a new spec can be built directly from another registered
        # spec's ``input_schema``.
        object.__setattr__(self, "input_schema", _freeze_json_value(self.input_schema))

    def public_description(self) -> Dict[str, Any]:
        """Return a copy-safe, plain JSON-compatible description for
        UI/discovery, schema included. The returned schema is a fresh plain
        dict/list structure (via :func:`_thaw_json_value`), so mutating it
        can never affect the registered tool's actual contract."""
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "risk": self.risk,
            "allowed_scopes": list(self.allowed_scopes),
            "input_schema": _thaw_json_value(self.input_schema),
        }


@dataclass(frozen=True)
class AgentToolCall:
    """One bounded, JSON-validated request to invoke a registered tool."""

    call_id: str
    tool_name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not CALL_ID_PATTERN.fullmatch(self.call_id):
            raise ContractError(f"Invalid call id: {self.call_id!r}")
        if (
            not isinstance(self.tool_name, str)
            or len(self.tool_name) > MAX_TOOL_NAME_LENGTH
            or not TOOL_NAME_PATTERN.fullmatch(self.tool_name)
        ):
            raise ContractError(f"Invalid tool name: {self.tool_name!r}")
        if not isinstance(self.arguments, dict):
            raise ContractError("Tool call arguments must be a JSON object.")
        validated = validate_json_value(
            dict(self.arguments),
            max_string_length=MAX_TEXT_ARG_LENGTH,
            max_total_chars=MAX_ARGUMENT_TOTAL_CHARS,
        )
        object.__setattr__(self, "arguments", validated)


@dataclass(frozen=True)
class AgentToolResult:
    """The outcome of one tool call: success, denial, approval, or failure.

    ``data`` is only populated (and validated as bounded JSON) on success, so
    a denied/failed result can never accidentally carry unsanitized payloads.
    """

    call_id: str
    tool_name: str
    status: str
    data: Any = None
    message: str = ""
    reason_code: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not CALL_ID_PATTERN.fullmatch(self.call_id):
            raise ContractError(f"Invalid result call id: {self.call_id!r}")
        if (
            not isinstance(self.tool_name, str)
            or len(self.tool_name) > MAX_TOOL_NAME_LENGTH
            or not TOOL_NAME_PATTERN.fullmatch(self.tool_name)
        ):
            raise ContractError(f"Invalid result tool name: {self.tool_name!r}")
        if self.status not in AgentResultStatus.ALL:
            raise ContractError(f"Unknown result status: {self.status!r}")
        if not isinstance(self.message, str):
            raise ContractError("Result message must be a string.")
        if len(self.message) > MAX_RESULT_TEXT_LENGTH:
            object.__setattr__(self, "message", self.message[:MAX_RESULT_TEXT_LENGTH])
        if not isinstance(self.reason_code, str):
            raise ContractError("Result reason code must be a string.")
        if self.reason_code and (
            len(self.reason_code) > MAX_REASON_CODE_LENGTH
            or not REASON_CODE_PATTERN.fullmatch(self.reason_code)
        ):
            raise ContractError(f"Invalid result reason code: {self.reason_code!r}")
        if self.status == AgentResultStatus.SUCCESS:
            validated_data = validate_json_value(
                self.data,
                max_string_length=MAX_RESULT_STRING_LENGTH,
                max_total_chars=MAX_RESULT_TOTAL_CHARS,
            )
            object.__setattr__(self, "data", validated_data)
        else:
            object.__setattr__(self, "data", None)

    def to_dict(self) -> Dict[str, Any]:
        """Return a plain, JSON-serializable representation with no object hooks."""
        return {
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "status": self.status,
            "data": self.data,
            "message": self.message,
            "reason_code": self.reason_code,
        }
