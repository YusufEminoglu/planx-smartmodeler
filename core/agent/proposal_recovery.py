"""Narrow, authority-neutral recovery for common provider proposal mistakes.

The strict protocol/parser remains the security boundary.  This module is used
only after that parser rejects a provider response, and can repair three
mechanical mistakes without asking the provider for another paid turn:

* restore a freshness receipt from a trusted read-only inspection result;
* request the one missing read-only inspection needed for that receipt;
* make a layer-style class count agree with its bounded palette/family.
* replace a missing or blank proposal display note with a fixed safe note;
* promote a semantically valid proposal accidentally labelled ``final``.
* convert the retired ``parameters``/``temporary_output`` processing shape to
  the current typed ``inputs`` shape without accepting a destination.

It never changes a proposal target, algorithm, input binding, field, value,
operation, output destination, mode, scope, or approval state.  The recovered
proposal is sent back through the unchanged strict parser and then through the
live runtime validator, so recovery cannot make an unsafe action runnable.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Tuple

from .identifiers import MODEL_RUN_KIND, MODEL_TARGET_ID
from .proposals import (
    MAX_PALETTE_COLORS,
    MAX_PROPOSAL_JSON_CHARS,
    MAX_TOKEN_CHARS,
    PROPOSAL_KIND_LAYER_STYLE,
    PROPOSAL_KIND_MODEL_PATCH,
    PROPOSAL_KIND_PROCESSING_RUN,
)
from .protocol import (
    ACTION_FINAL,
    ACTION_PROPOSAL,
    MAX_RAW_RESPONSE_CHARS,
    AgentTurn,
    ProtocolError,
    parse_agent_turn,
)

ReceiptKey = Tuple[str, str]

_PROPOSAL_KIND_ALIASES = {
    "processing": PROPOSAL_KIND_PROCESSING_RUN,
    "run": PROPOSAL_KIND_PROCESSING_RUN,
    "style": PROPOSAL_KIND_LAYER_STYLE,
}

_LEGACY_LAYER_PARAMETER_NAMES = frozenset(
    {"INPUT", "INPUT_LAYER", "SOURCE", "OVERLAY", "REFERENCE", "MASK", "CLIP", "LAYER"}
)
_LEGACY_ENUM_PARAMETER_NAMES = frozenset(
    {"METHOD", "OPERATOR", "TYPE", "JOIN_STYLE", "END_CAP_STYLE", "CAP_STYLE"}
)
_LEGACY_DESTINATION_NAMES = frozenset(
    {"OUTPUT", "DESTINATION", "OUTPUT_LAYER", "OUTPUT_FILE", "TEMPORARY_OUTPUT"}
)


def _legacy_binding(name: str, value: Any) -> Optional[dict]:
    """Convert one old scalar parameter to a current inert binding."""
    if isinstance(value, dict):
        # Preserve an already-typed binding if a provider nested it under the
        # legacy parameters object; the strict parser validates the tag.
        if len(value) == 1 and next(iter(value)) in {
            "layer", "layers", "number", "bool", "enum", "enum_string",
            "string", "text", "crs", "distance", "map_extent", "layer_extent",
            "osm_tag", "expression",
        }:
            return dict(value)
        if set(value) == {"value"}:
            value = value["value"]
        else:
            return None
    upper = name.upper()
    if upper in _LEGACY_DESTINATION_NAMES or upper.endswith("_DESTINATION"):
        return None
    if isinstance(value, bool):
        return {"bool": value}
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if upper in _LEGACY_ENUM_PARAMETER_NAMES:
            return {"enum": value}
        if upper in {"DISTANCE", "RADIUS", "TOLERANCE"}:
            return {"distance": value}
        return {"number": value}
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return {"layers": list(value)} if upper.endswith("LAYERS") else {"string": ",".join(value)}
    if isinstance(value, str):
        if upper in _LEGACY_LAYER_PARAMETER_NAMES or upper.endswith("_LAYER"):
            return {"layer": value}
        if upper in {"FIELD", "FIELD_NAME"}:
            return {"field": value, "layer_param": "INPUT"}
        if upper in {"FORMULA", "EXPRESSION"}:
            return {"expression": value}
        if upper in {"CRS", "TARGET_CRS"}:
            return {"crs": value}
        return {"string": value}
    return None


def _normalize_legacy_processing(proposal: dict) -> None:
    """Translate the retired DeepSeek processing shape in place.

    The conversion is deliberately narrow and authority-reducing. It never
    carries forward output destinations, file paths, SQL, or Python source.
    The resulting object still has to pass ``parse_proposal`` and the trusted
    runtime validator.
    """
    if "inputs" in proposal or not isinstance(proposal.get("parameters"), dict):
        return
    parameters = proposal["parameters"]
    inputs = {}
    for raw_name, value in list(parameters.items())[:30]:
        if not isinstance(raw_name, str) or not raw_name.strip():
            return
        binding = _legacy_binding(raw_name, value)
        if binding is not None:
            inputs[raw_name] = binding
    proposal["inputs"] = inputs
    proposal.pop("parameters", None)
    proposal.pop("temporary_output", None)
    proposal.setdefault("schema_version", 1)
    proposal.setdefault("title", f"Run {proposal.get('algorithm_id', 'Processing algorithm')}")
    proposal.setdefault("summary", "Run the reviewed Processing algorithm with temporary output.")
    proposal.setdefault("warnings", [])


@dataclass(frozen=True)
class InspectionRequest:
    """One trusted read-only inspection needed to recover a proposal."""

    tool_name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class RecoveryResult:
    """Either a recovered turn, a required inspection, or no safe recovery."""

    turn: Optional[AgentTurn] = None
    inspection: Optional[InspectionRequest] = None


def _reject_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate object key")
        result[key] = value
    return result


def _object(text: str) -> Optional[dict]:
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except (RecursionError, ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def _receipt_target(kind: str, proposal: Mapping[str, Any]) -> Tuple[str, str]:
    if kind == PROPOSAL_KIND_PROCESSING_RUN:
        target = proposal.get("algorithm_id")
        return kind, target if isinstance(target, str) else ""
    if kind == PROPOSAL_KIND_LAYER_STYLE:
        target = proposal.get("target_layer_id")
        return kind, target if isinstance(target, str) else ""
    if kind in (PROPOSAL_KIND_MODEL_PATCH, MODEL_RUN_KIND):
        # model.describe issues one model_patch-bound receipt which is also the
        # freshness receipt consumed by a model_run.
        return PROPOSAL_KIND_MODEL_PATCH, MODEL_TARGET_ID
    return "", ""


def _inspection_for(kind: str, target: str) -> Optional[InspectionRequest]:
    if kind == PROPOSAL_KIND_PROCESSING_RUN and target:
        return InspectionRequest(
            "processing.describe", {"algorithm_id": target}
        )
    if kind == PROPOSAL_KIND_LAYER_STYLE and target:
        return InspectionRequest("layer.style", {"layer_id": target})
    if kind in (PROPOSAL_KIND_MODEL_PATCH, MODEL_RUN_KIND):
        return InspectionRequest("model.describe", {})
    return None


def _token_is_usable(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_TOKEN_CHARS


def _normalize_style(proposal: dict) -> None:
    """Repair only renderer cardinality/default-container mistakes.

    Palette colours, fields and requested families are never invented.  When a
    provider supplies too many colours, the documented twelve-colour bound is
    applied deterministically.  The strict parser still validates every colour
    and all remaining layer-style semantics afterwards.
    """

    renderer = proposal.get("renderer")
    if not isinstance(renderer, dict):
        return
    family = renderer.get("family")
    palette = renderer.get("palette")
    if not isinstance(palette, list):
        return

    if len(palette) > MAX_PALETTE_COLORS:
        palette = list(palette[:MAX_PALETTE_COLORS])
        renderer["palette"] = palette

    if family == "single_symbol" and palette:
        renderer["palette"] = list(palette[:1])
        renderer["class_count"] = 1
    elif family in ("categorized", "graduated", "raster_pseudocolor"):
        if 2 <= len(palette) <= MAX_PALETTE_COLORS:
            renderer["class_count"] = len(palette)
    elif family in ("keep", "raster_gray", "raster_multiband"):
        renderer["class_count"] = 0
        renderer["palette"] = []

    # These are inert display defaults.  They add no renderer authority and
    # keep common provider omissions from wasting a second network turn.
    proposal.setdefault("labels", {"enabled": False, "field": ""})
    proposal.setdefault("warnings", [])


def recover_agent_turn(
    raw_text: str,
    max_tool_calls_per_turn: int,
    receipts: Mapping[ReceiptKey, str],
) -> RecoveryResult:
    """Return a safely recovered proposal turn or its missing inspection.

    Non-proposal responses, malformed outer envelopes, unknown targets and
    semantic proposal errors outside the narrow rules above are not repaired.
    """

    if not isinstance(raw_text, str) or len(raw_text) > MAX_RAW_RESPONSE_CHARS:
        return RecoveryResult()
    outer = _object(raw_text.strip())
    if outer is None or outer.get("action") not in (ACTION_PROPOSAL, ACTION_FINAL):
        return RecoveryResult()
    if outer.get("tool_calls") not in (None, []):
        return RecoveryResult()
    kind = outer.get("proposal_kind")
    proposal_json = outer.get("proposal_json")
    if not isinstance(kind, str) or not isinstance(proposal_json, str):
        return RecoveryResult()
    kind = _PROPOSAL_KIND_ALIASES.get(kind, kind)
    if not proposal_json.strip() or len(proposal_json) > MAX_PROPOSAL_JSON_CHARS:
        return RecoveryResult()
    proposal = _object(proposal_json)
    if proposal is None:
        return RecoveryResult()

    if kind == PROPOSAL_KIND_PROCESSING_RUN:
        _normalize_legacy_processing(proposal)

    receipt_kind, target = _receipt_target(kind, proposal)
    if not receipt_kind or not target:
        return RecoveryResult()
    if not _token_is_usable(proposal.get("context_token")):
        token = receipts.get((receipt_kind, target), "")
        if not _token_is_usable(token):
            return RecoveryResult(inspection=_inspection_for(kind, target))
        proposal["context_token"] = token

    if kind == PROPOSAL_KIND_LAYER_STYLE:
        _normalize_style(proposal)

    repaired = dict(outer)
    repaired["proposal_kind"] = kind
    # DeepSeek occasionally labels a complete proposal envelope as ``final``.
    # This promotion changes no proposal content or authority: the strict
    # parser still requires a proposable kind, parses the inert proposal, and
    # the live runtime validator still performs the freshness and scope checks.
    if repaired.get("action") == ACTION_FINAL:
        repaired["action"] = ACTION_PROPOSAL
    assistant_text = repaired.get("assistant_text")
    if assistant_text is None or (
        isinstance(assistant_text, str) and not assistant_text.strip()
    ):
        # Display text carries no authority; proposal target, inputs and token
        # remain unchanged and are still parsed and live-validated below.
        repaired["assistant_text"] = "A validated proposal is ready."
    repaired["tool_calls"] = []
    repaired["proposal_json"] = json.dumps(
        proposal, ensure_ascii=False, separators=(",", ":")
    )
    try:
        turn = parse_agent_turn(
            json.dumps(repaired, ensure_ascii=False, separators=(",", ":")),
            max_tool_calls_per_turn,
        )
    except ProtocolError:
        return RecoveryResult()
    return RecoveryResult(turn=turn)
