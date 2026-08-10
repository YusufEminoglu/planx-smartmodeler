"""Deterministic, bounded prompt and session-history construction for Agent Chat.

QGIS-free: builds only the system/user prompt text for one provider turn from
static ``agent_context/`` instructions, scope-allowed tool descriptions, the
current user request, bounded prior session history, and this run's own
assistant/tool trace. Never touches QGIS and never sends a network request --
that remains the dock/``AiNetworkClient``'s job.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .contracts import AgentToolSpec, MAX_ALLOWED_PROMPT_CHARS

# Recommended Phase 02 defaults (work order Section 8.2).
MAX_USER_MESSAGE_CHARS = 4_000
MAX_SESSION_EXCHANGES = 6
MAX_SESSION_TEXT_CHARS = 6_000
# One tool result's share of the per-turn prompt. Raised from 3,000: the whole
# prompt budget is 30,000, so 3,000 gave a single result 10% and silently
# discarded anything larger. A real plugin.capabilities listing is ~7,700
# characters, so *every* capability inspection of a substantial plugin was
# replaced by an omission marker -- the model asked what PlanX could do, learned
# nothing, retried smaller, learned nothing again, and concluded the tool did
# not exist. 8,000 still leaves room for three large results in one turn, and
# oversized Processing/capability results are now compacted rather than dropped.
MAX_TOOL_RESULT_PROMPT_CHARS = 8_000
MAX_WORKING_TRACE_CHARS = 2_500

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
    system_chars: int = 0
    tool_schema_chars: int = 0
    history_chars: int = 0
    event_chars: int = 0
    estimated_input_tokens: int = 0


def estimate_input_tokens(characters: int) -> int:
    """Return a conservative provider-neutral estimate used only for budgets."""
    return max(0, int(math.ceil(max(0, int(characters)) / 3.0)))


def select_tools_for_scope(
    tool_specs: Sequence[AgentToolSpec], scope: str
) -> List[AgentToolSpec]:
    """Return only the tools whose ``allowed_scopes`` include ``scope``, in
    the same deterministic order as ``tool_specs`` (already sorted by the
    registry). Only these tools are ever advertised to the provider for a
    turn captured with this scope."""
    return [spec for spec in tool_specs if scope in spec.allowed_scopes]


def select_tools_for_request(
    tool_specs: Sequence[AgentToolSpec],
    scope: str,
    user_text: str,
    *,
    power_enabled: bool = False,
    session_history: Sequence[SessionExchange] = (),
) -> List[AgentToolSpec]:
    """Select the smallest useful deterministic capability pack for a request."""
    scoped = select_tools_for_scope(tool_specs, scope)
    folded = str(user_text or "").casefold()
    continuation_markers = (
        "o zaman", "other layer", "use it", "ilerle", "devam", "continue",
        "tekrar", "retry", "dogru dedin", "haklisin",
    )
    # Short follow-ups such as "hazır", "yapsana", a bare layer/field name,
    # or "why?" need the latest bounded exchange for capability routing.
    # The provider already receives that exchange; this merely keeps the same
    # scope-filtered discovery tools advertised for the continuation.
    if (
        session_history
        and (
            len(folded.strip()) <= 80
            or any(marker in folded for marker in continuation_markers)
        )
    ):
        # A retry can follow a short diagnostic exchange ("why couldn't you
        # do it?" -> "try again"). Looking at only the immediately preceding
        # message loses the original operation and falsely removes its tools.
        # The bounded session already caps retained exchanges and characters;
        # inspecting the latest three user messages preserves the operation
        # without making the tool pack depend on unbounded history.
        recent = [
            item.user_text.casefold()
            for item in session_history[-3:]
            if isinstance(item, SessionExchange)
        ]
        if recent:
            folded = "\n".join((folded, *recent))
    wanted = set()
    if scope in ("project", "active_layer"):
        wanted.update(
            ("project.summary", "layer.list", "layer.describe", "layer.field_values")
        )
    if scope == "current_model":
        wanted.update(("model.summary", "model.describe", "model.validate"))
    if scope == "plugins":
        wanted.update(("plugin.list", "plugin.describe", "plugin.capabilities"))
    if scope == "workspace":
        wanted.update(
            (
                "workspace.list",
                "workspace.read",
                "workspace.inspect",
                "workspace.search",
                "workspace.command",
            )
        )

    # Named for the *operation the user asks for*, in both working languages.
    # A routing audit (test_agent_capability_routing) found this table missed
    # most Turkish verbs and even "fix invalid geometries", so ordinary requests
    # fell through to the minimal discovery pack.
    processing_terms = (
        "process", "algorithm", "buffer", "reproject", "extract", "calculate",
        "field", "expression", "rand(", "$area", "$length", "clip", "merge",
        "ilçe", "ilce", "district", "mahalle", "neighborhood", "konak",
        "dissolve", "processing", "filter", "filtre", "süz", "suz",
        "extract by attribute", "yeni katman", "katman olarak üret",
        "sütun", "sutun", "alan hesap",
        # geometry and overlay verbs
        "tampon", "dönüştür", "donustur", "erit", "onar", "kırp", "kirp",
        "kesiş", "kesis", "birleştir", "birlestir", "eşleştir", "eslestir",
        "merkez", "rastgele", "say", "ayıkla", "ayikla",
        "geometr", "centroid", "intersect", "join", "count", "random",
        "convex", "bounding", "fix", "crs", "epsg", "difference",
    )
    if any(term in folded for term in processing_terms):
        wanted.update(
            (
                "processing.resolve",
                "processing.search",
                "processing.describe",
                "expression.search",
                "layer.list",
                "layer.describe",
                # A threshold filter or an area calculation is exactly where a
                # value-blind agent invents "there simply are none".
                "layer.field_values",
                # "reproject to the local CRS" and "add an area column" both
                # need a real authid; without this the model guesses one.
                "layer.suggest_crs",
            )
        )
    # OSM requests name a *subject*, and the original three (roads, buildings,
    # trees) were a small fraction of what people ask for. "indir"/"download" is
    # itself a strong signal here: the downloader is the acquisition path.
    osm_terms = (
        "osm", "openstreetmap", "overpass", "02agent", "road", "building",
        "tree", "yol", "bina", "ağaç", "agac",
        "indir", "download",
        "okul", "school", "hastane", "hospital", "eczane", "pharmacy",
        "park", "yeşil alan", "yesil alan", "green",
        "nehir", "river", "su yolu", "waterway", "akarsu",
        "sokak", "cadde", "street", "highway",
        "arazi kullanım", "arazi kullanim", "landuse", "amenity", "poi",
        "durak", "transit", "railway", "demiryolu",
    )
    if any(term in folded for term in osm_terms):
        wanted.update(
            (
                "processing.resolve",
                "processing.search",
                "processing.describe",
                "plugin.capabilities",
                "layer.list",
            )
        )
    if any(term in folded for term in ("plugin", "eklenti", "02agent", "02viz")):
        wanted.update(("plugin.list", "plugin.describe", "plugin.capabilities"))
    # Classifying a layer *is* styling in QGIS, but a user asks for it by the
    # method: "jenks", "natural breaks", "quantile", "sınıflandır". Keying only
    # on style vocabulary left layer.style unadvertised for exactly those
    # requests, so the Agent reported that no styling tool existed and hunted
    # for a Processing algorithm instead -- even after the user settled for
    # equal interval, which it could have proposed.
    if any(
        term in folded
        for term in (
            "style", "symbol", "label", "renderer", "stil", "sembol", "etiket",
            "classif", "sınıflandır", "siniflandir", "sınıf", "sinif",
            "jenks", "natural break", "doğal kırılma", "dogal kirilma",
            "quantile", "quintile", "kantil", "graduated", "kademeli",
            "categor", "kategori", "renklendir", "colour", "color",
        )
    ):
        # A graduated/Jenks renderer needs the field's real numeric range, not
        # just its type, so the class breaks can be sanity-checked.
        wanted.update(
            ("layer.list", "layer.describe", "layer.style", "layer.field_values")
        )
    if power_enabled and any(
        term in folded
        for term in (
            "sql", "postgis", "geopackage", "database", "veritaban",
            "python", "pyqgis", "script", "betik", "power mode", "güç modu",
        )
    ):
        # Power Mode enables these capabilities, but does not make them useful
        # for every request. Route the bounded discovery schemas only to an
        # explicit Power task (or its short continuation, already folded with
        # the previous request above). A normal filter stays a Processing task.
        wanted.update(
            (
                "database.list",
                "database.describe",
                "script.list",
                "script.describe",
            )
        )

    # An unknown project request still needs one discovery route, but not the
    # entire registry.
    if not wanted and scope in ("project", "active_layer"):
        wanted.update(("project.summary", "layer.list", "processing.resolve"))
    selected = [spec for spec in scoped if spec.name in wanted]
    return selected or scoped[:3]


def _omit_if_oversized(event: Dict[str, Any], max_chars: int) -> Dict[str, Any]:
    """Replace an oversized ``tool_result`` event's result with a small,
    valid JSON omission record instead of slicing raw JSON text."""
    if event.get("kind") != "tool_result":
        return event
    result = event.get("result")
    serialized = json.dumps(result, ensure_ascii=False, sort_keys=True)
    if len(serialized) <= max_chars:
        return event
    tool_name = event.get("tool_name")
    if tool_name in {"processing.resolve", "processing.describe"}:
        compacted = _compact_processing_tool_event(event, max_chars)
        if compacted is not None:
            return compacted
    if tool_name == "plugin.capabilities":
        compacted = _compact_capabilities_tool_event(event, max_chars)
        if compacted is not None:
            return compacted
    status = result.get("status") if isinstance(result, dict) else ""
    omitted = dict(event)
    omitted["result"] = {
        "status": status if isinstance(status, str) else "",
        # Say what to do about it. A bare "omitted" invites the same call
        # again, which produces the same omission, which is how a run burns
        # its turns without ever learning anything.
        "reason": (
            "tool result omitted: it exceeded the per-result prompt budget. "
            "Do not repeat this call unchanged -- narrow it instead (a smaller "
            "limit, a more specific query, or one named target) or continue "
            "from the evidence you already have."
        ),
        "original_chars": len(serialized),
        "budget_chars": max_chars,
    }
    return omitted


def _compact_capabilities_tool_event(
    event: Dict[str, Any], max_chars: int
) -> Optional[Dict[str, Any]]:
    """Keep a plugin's algorithm *ids* when the full listing does not fit.

    An omitted capability listing is unrecoverable in a way an oversized one is
    not: the model asked what a plugin can do, learned nothing, asked again with
    a smaller limit, learned nothing again, and eventually told the user it
    could not find a tool that was installed and runnable the whole time. A real
    PlanX listing is ~7,700 characters against a 3,000 cap, so this happened on
    every single call.

    The per-algorithm ``group`` blurb is most of that weight and none of the
    answer -- an id and a title are what a follow-up ``processing.resolve``
    needs. Dropping the blurb, then trimming the list from the end with an
    explicit truncation flag, keeps the result honest and usable.
    """
    result = event.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
        return None
    data = dict(result["data"])
    rows = data.get("algorithms")
    if not isinstance(rows, list):
        return None

    def sized(payload: Dict[str, Any]) -> int:
        return len(json.dumps(payload, ensure_ascii=False, sort_keys=True))

    slim = [
        {key: row[key] for key in ("algorithm_id", "title") if key in row}
        for row in rows
        if isinstance(row, dict)
    ]
    for count in (len(slim), 40, 30, 20, 12, 6):
        if count > len(slim):
            continue
        candidate = dict(data)
        candidate["algorithms"] = slim[:count]
        candidate["algorithms_truncated"] = count < len(rows)
        if count < len(rows):
            candidate["algorithms_total"] = len(rows)
        compacted = dict(event)
        compacted["result"] = {**result, "data": candidate}
        if sized(compacted["result"]) <= max_chars:
            return compacted
    return None


def _compact_processing_tool_event(
    event: Dict[str, Any], max_chars: int
) -> Optional[Dict[str, Any]]:
    """Keep the live receipt and bindable rows of a large Processing result."""
    result = event.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
        return None
    data = result["data"]

    def compact_description(description: Dict[str, Any]) -> Dict[str, Any]:
        compact: Dict[str, Any] = {
            key: description[key]
            for key in (
                "available", "algorithm_id", "title", "group", "provider_id",
                "agent_runnable", "agent_reason", "context_token",
            )
            if key in description
        }
        rows = description.get("parameters")
        if isinstance(rows, list):
            compact_rows = []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                compact_row = {
                    key: row[key]
                    for key in (
                        "name", "type", "required", "destination", "multiple",
                        "proposal_binding", "alternative_binding", "default_behavior",
                        "minimum", "maximum",
                    )
                    if key in row
                }
                options = row.get("enum_options")
                if isinstance(options, list):
                    compact_row["enum_options"] = options[:12]
                    if len(options) > 12:
                        compact_row["enum_options_truncated"] = True
                compact_rows.append(compact_row)
            compact["parameters"] = compact_rows
            compact["parameters_truncated"] = bool(description.get("parameters_truncated"))
        return compact

    compact_data: Dict[str, Any] = {}
    resolved = data.get("resolved")
    if isinstance(resolved, dict):
        compact_data["resolved"] = compact_description(resolved)
        algorithms = data.get("algorithms")
        if isinstance(algorithms, list):
            compact_data["algorithms"] = [
                {
                    key: item[key]
                    for key in ("algorithm_id", "title", "provider_id")
                    if key in item
                }
                for item in algorithms
                if isinstance(item, dict)
            ]
    else:
        compact_data = compact_description(data)

    compacted = dict(event)
    compacted["result"] = dict(result)
    compacted["result"]["data"] = compact_data
    if len(json.dumps(compacted, ensure_ascii=False, sort_keys=True)) <= max_chars:
        return compacted

    description = compact_data.get("resolved", compact_data)
    rows = description.get("parameters") if isinstance(description, dict) else None
    if isinstance(rows, list):
        description["parameters"] = [
            row
            for row in rows
            if isinstance(row, dict)
            and (
                row.get("required") is True
                or row.get("proposal_binding")
                or row.get("alternative_binding")
            )
            and row.get("destination") is not True
        ]
        description["parameters_truncated"] = True
        if len(json.dumps(compacted, ensure_ascii=False, sort_keys=True)) <= max_chars:
            return compacted
        # Preserve enum choices only for rows whose type makes those choices
        # actionable; display-only choice lists are not needed to build a
        # proposal. Keep required rows first if a signature is still large.
        for row in description["parameters"]:
            if not isinstance(row, dict):
                continue
            if "enum_options" in row and "enum" not in str(row.get("type", "")).casefold():
                row.pop("enum_options", None)
                row.pop("enum_options_truncated", None)
        if len(json.dumps(compacted, ensure_ascii=False, sort_keys=True)) <= max_chars:
            return compacted
        description["parameters"] = description["parameters"][:12]
        description["parameters_truncated"] = True
        if len(json.dumps(compacted, ensure_ascii=False, sort_keys=True)) <= max_chars:
            return compacted

        # Last bounded form: the provider still gets the exact receipt and the
        # parameter binding contract, while verbose labels/options cannot make
        # the trusted evidence disappear from the working trace altogether.
        description["parameters"] = [
            {
                key: row[key]
                for key in (
                    "name", "type", "required", "proposal_binding",
                    "alternative_binding",
                )
                if key in row
            }
            for row in description["parameters"]
            if isinstance(row, dict)
        ][:8]
        description.pop("group", None)
        description.pop("provider_id", None)
        description.pop("agent_reason", None)
        description["parameters_truncated"] = True
        if len(json.dumps(compacted, ensure_ascii=False, sort_keys=True)) <= max_chars:
            return compacted
    return None


def _events_omitted_marker(dropped: int) -> Dict[str, Any]:
    """A single small, valid event standing in for ``dropped`` older events."""
    return {
        "kind": "events_omitted",
        "dropped_events": dropped,
        "reason": (
            "the earliest events of this run were dropped to fit the prompt "
            "budget; call a tool again if you still need that information"
        ),
    }


def _compact_working_events(
    events: Sequence[Dict[str, Any]], max_chars: int = MAX_WORKING_TRACE_CHARS
) -> List[Dict[str, Any]]:
    """Keep newest semantically distinct tool results within a small budget."""
    latest = []
    for event in reversed(events):
        item = dict(event)
        candidate = [item] + latest
        if len(json.dumps(candidate, ensure_ascii=False, sort_keys=True)) > max_chars:
            if item.get("kind") == "tool_result":
                compacted = _omit_if_oversized(item, max(256, max_chars - 220))
                candidate = [compacted] + latest
                if len(json.dumps(candidate, ensure_ascii=False, sort_keys=True)) <= max_chars:
                    latest = candidate
                    continue
            continue
        latest = candidate
    dropped = max(0, len(events) - len(latest))
    if dropped:
        marker = _events_omitted_marker(dropped)
        candidate = [marker] + latest
        if len(json.dumps(candidate, ensure_ascii=False, sort_keys=True)) <= max_chars:
            latest = candidate
    return latest


def _fixed_length(
    static_instructions: str,
    mode: str,
    scope: str,
    tool_descriptions: List[Dict[str, Any]],
    user_text: str,
    bounded_events: List[Dict[str, Any]],
) -> int:
    """Length of the never-negotiable part of the prompt: no session history."""
    payload = _payload(
        mode, scope, tool_descriptions, user_text, bounded_events, (), False
    )
    return len(static_instructions) + len(
        json.dumps(payload, ensure_ascii=False, sort_keys=True)
    )


def _drop_oldest_events_to_fit(
    static_instructions: str,
    mode: str,
    scope: str,
    tool_descriptions: List[Dict[str, Any]],
    user_text: str,
    bounded_events: List[Dict[str, Any]],
    max_prompt_chars: int,
) -> List[Dict[str, Any]]:
    """Fold the oldest run events into one marker until the fixed context fits.

    Raises :class:`PromptBuildError` only when even a trace-free prompt is too
    large -- that is a genuine configuration problem (the instructions or the
    tool schemas alone exceed the budget), not something dropping history can
    repair.
    """
    for dropped in range(1, len(bounded_events) + 1):
        candidate = [_events_omitted_marker(dropped)] + bounded_events[dropped:]
        if _fixed_length(
            static_instructions, mode, scope, tool_descriptions, user_text, candidate
        ) <= max_prompt_chars:
            return candidate
    raise PromptBuildError(
        "The required context (instructions, tools, and your current request) "
        f"does not fit within the {max_prompt_chars}-character prompt budget, "
        "even with this run's tool trace dropped. Raise the run's "
        "max_prompt_chars or narrow the scope so fewer tools are advertised."
    )


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
    bounded_events = _compact_working_events(bounded_events)

    def combined_length(history_entries: Sequence[SessionExchange], history_truncated: bool) -> int:
        payload = _payload(
            mode, scope, tool_descriptions, user_text, bounded_events, history_entries, history_truncated
        )
        return len(static_instructions) + len(
            json.dumps(payload, ensure_ascii=False, sort_keys=True)
        )

    # The instructions, the advertised tools and the current request are never
    # dropped. This run's own trace is the one part of the "fixed" context that
    # grows without bound as the run makes more tool calls, so when the fixed
    # context no longer fits, the *oldest* turn events are folded into a single
    # small omission marker until it does. A long inspection run must lose its
    # earliest tool output, not die -- failing the whole run here is what made
    # a third tool call unusable in practice.
    if _fixed_length(
        static_instructions, mode, scope, tool_descriptions, user_text, bounded_events
    ) > budget.max_prompt_chars:
        bounded_events = _drop_oldest_events_to_fit(
            static_instructions,
            mode,
            scope,
            tool_descriptions,
            user_text,
            bounded_events,
            budget.max_prompt_chars,
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
    tool_schema_chars = len(
        json.dumps(tool_descriptions, ensure_ascii=False, sort_keys=True)
    )
    history_chars = len(
        json.dumps(
            [
                {"user_text": item.user_text, "assistant_text": item.assistant_text}
                for item in included
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    event_chars = len(
        json.dumps(bounded_events, ensure_ascii=False, sort_keys=True)
    )
    combined_chars = len(static_instructions) + len(user_prompt)
    return PromptResult(
        system_prompt=static_instructions,
        user_prompt=user_prompt,
        history_truncated=history_truncated,
        system_chars=len(static_instructions),
        tool_schema_chars=tool_schema_chars,
        history_chars=history_chars,
        event_chars=event_chars,
        estimated_input_tokens=estimate_input_tokens(combined_chars),
    )
