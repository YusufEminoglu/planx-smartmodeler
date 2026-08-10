"""Bounded context builders for the Agent Workspace.

This module is pure Python with no QGIS imports, so it can be unit tested
without a QGIS runtime. QGIS-specific data collection lives in
``runtime_tools.py``, which converts live QGIS objects into the plain
dataclasses defined here before any bounding/formatting happens.

Source paths, URIs, connection strings, and credentials are still structurally
impossible to return: no builder here accepts one. Attribute *values* are the
single deliberate exception, and only through :func:`build_field_values` --
every other builder remains metadata-only. That exception exists because an
agent that cannot see a value cannot tell "the filter matched nothing" from
"the filter was wrong", and it filled the gap by inventing an answer. Values
arrive pre-bounded: one named field at a time, capped sample length, capped
per-value text.
"""
from __future__ import annotations

import itertools
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

MAX_DISPLAY_NAME = 200
MAX_SHORT_TEXT = 64
DEFAULT_LIST_LIMIT = 25
MAX_LIST_ITEMS = 100

# Bounds for ``build_field_values``. One field, a short sample, short values:
# enough to diagnose a filter that returned nothing, never a table export.
DEFAULT_VALUE_SAMPLE = 20
MAX_VALUE_SAMPLE = 50
MAX_VALUE_TEXT = 120

# Bounds for the richer Phase 03 read-only summaries.
MAX_SYMBOL_LAYERS = 12
MAX_ABOUT_TEXT = 1200

_EXHAUSTED = object()

_HEX_COLOR_PATTERN = re.compile(r"^#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")


def bound_text(value: Any, maximum: int) -> str:
    """Return ``value`` coerced to text and truncated to ``maximum`` characters."""
    text = "" if value is None else str(value)
    return text[:maximum]


def normalize_hex_color(value: Any) -> Optional[str]:
    """Return ``value`` as an uppercase ``#RRGGBB``/``#RRGGBBAA`` string.

    Accepts only an already-hex string in one of those two exact shapes
    (case-insensitive). Anything else -- a shorthand ``#RGB``, a named colour,
    an ``rgb(...)`` expression, or a non-string -- returns ``None`` so callers
    can omit a colour they cannot represent safely rather than echoing raw
    provider/style text.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _HEX_COLOR_PATTERN.fullmatch(candidate):
        return None
    return "#" + candidate[1:].upper()


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Coerce ``value`` into a strictly JSON-serializable, finite structure.

    Used only to build the *internal* canonical state that a context token
    signs; the result is never returned to the provider. Unknown/opaque values
    and non-finite numbers are coerced to bounded text so signing can never
    raise on a parameter the graph happens to hold.
    """
    if depth > 12:
        return "..."
    if value is None or isinstance(value, bool) or isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if -1e308 < value < 1e308 and value == value else str(value)
    if isinstance(value, str):
        return value[:10000]
    if isinstance(value, (list, tuple)):
        return [_json_safe(item, depth + 1) for item in list(value)[:500]]
    if isinstance(value, dict):
        return {
            str(key)[:200]: _json_safe(sub, depth + 1)
            for key, sub in list(value.items())[:200]
        }
    return str(value)[:10000]


def canonical_model_state(graph: Any) -> Dict[str, Any]:
    """Build the deterministic internal signing state for a SmartModeler graph.

    Includes node ids, titles, algorithm ids, ports, **and parameter values**,
    plus edges and model metadata, so any meaningful change (a renamed node, a
    reconnected edge, an edited parameter, a changed model name) yields a
    different token. This structure is signed only -- it is never serialized
    back to the provider, so the enclosed parameter values never leak.

    ``graph is None`` (no studio/model open) maps to a stable no-model state so
    ``model.describe`` can still issue a valid token bound to "no model".
    """
    if graph is None:
        return {"model": None}
    nodes = []
    for node in sorted(graph.nodes.values(), key=lambda item: item.node_id):
        nodes.append(
            {
                "id": node.node_id,
                "title": node.title,
                "algorithm_id": node.algorithm_id,
                "parameters": {
                    str(name): _json_safe(value)
                    for name, value in sorted(node.parameters.items())
                },
                "inputs": sorted(
                    {
                        port_id: port.socket_type
                        for port_id, port in node.inputs.items()
                    }.items()
                ),
                "outputs": sorted(
                    {
                        port_id: port.socket_type
                        for port_id, port in node.outputs.items()
                    }.items()
                ),
            }
        )
    edges = sorted(
        (
            [
                edge.start_node_id,
                edge.start_port_id,
                edge.end_node_id,
                edge.end_port_id,
            ]
            for edge in graph.edges.values()
        )
    )
    return {
        "model": {
            "name": str(graph.name),
            "description": str(getattr(graph, "description", "")),
            "nodes": nodes,
            "edges": edges,
        }
    }


def bound_list(items: Iterable[Any], limit: int) -> Tuple[List[Any], bool]:
    """Return ``(bounded_items, truncated)`` with an explicit truncation flag.

    ``limit`` is always clamped to ``[0, MAX_LIST_ITEMS]`` so a caller cannot
    request an unbounded result even by accident. ``items`` is consumed
    lazily through an iterator: at most ``clamped_limit + 1`` items are ever
    pulled (the extra one only to determine ``truncated``), so a caller may
    safely pass a generator over a large or effectively unbounded source
    without materializing it first.
    """
    clamped_limit = max(0, min(limit, MAX_LIST_ITEMS))
    iterator = iter(items)
    bounded = list(itertools.islice(iterator, clamped_limit))
    truncated = next(iterator, _EXHAUSTED) is not _EXHAUSTED
    return bounded, truncated


@dataclass(frozen=True)
class LayerSummary:
    """Bounded, non-sensitive metadata about one project layer.

    Deliberately excludes source URIs, connection strings, and feature data.
    """

    layer_id: str
    name: str
    kind: str
    geometry_type: str = ""
    crs: str = ""
    visible: bool = True
    provider_key: str = ""
    active: bool = False
    # False when $area/$length on this layer would return distorted numbers --
    # a geographic CRS (degrees) or a Mercator one (metres, but inflated by
    # 1/cos^2(latitude)). Both report "metres" or look ordinary otherwise, so
    # the CRS authid alone does not warn anybody.
    area_safe_crs: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "layer_id": bound_text(self.layer_id, 128),
            "name": bound_text(self.name, MAX_DISPLAY_NAME),
            "kind": bound_text(self.kind, MAX_SHORT_TEXT),
            "geometry_type": bound_text(self.geometry_type, MAX_SHORT_TEXT),
            "crs": bound_text(self.crs, 32),
            "visible": bool(self.visible),
            "provider_key": bound_text(self.provider_key, MAX_SHORT_TEXT),
            "active": bool(self.active),
            "area_safe_crs": bool(self.area_safe_crs),
        }


@dataclass(frozen=True)
class FieldSummary:
    """Bounded metadata about one attribute field: name and broad type only."""

    name: str
    field_type: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": bound_text(self.name, 128),
            "field_type": bound_text(self.field_type, MAX_SHORT_TEXT),
        }


@dataclass(frozen=True)
class PluginSummary:
    """Bounded metadata about one installed plugin."""

    package_name: str
    display_name: str
    version: str
    enabled: bool
    has_processing_provider: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "package_name": bound_text(self.package_name, MAX_DISPLAY_NAME),
            "display_name": bound_text(self.display_name, MAX_DISPLAY_NAME),
            "version": bound_text(self.version, MAX_SHORT_TEXT),
            "enabled": bool(self.enabled),
            "has_processing_provider": bool(self.has_processing_provider),
        }


@dataclass(frozen=True)
class ModelNodeSummary:
    """Bounded metadata about one node in the current SmartModeler graph."""

    node_id: str
    title: str
    algorithm_id: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "node_id": bound_text(self.node_id, 64),
            "title": bound_text(self.title, MAX_DISPLAY_NAME),
            "algorithm_id": bound_text(self.algorithm_id, 200),
        }


def build_project_summary(title: str, crs_authid: str, layer_count: int) -> Dict[str, Any]:
    """Project title and CRS only; never the saved project path."""
    return {
        "title": bound_text(title, MAX_DISPLAY_NAME),
        "crs": bound_text(crs_authid, 32),
        "layer_count": max(0, int(layer_count)),
    }


def build_layer_list(
    layers: Iterable[LayerSummary], limit: int = DEFAULT_LIST_LIMIT
) -> Dict[str, Any]:
    bounded, truncated = bound_list((layer.to_dict() for layer in layers), limit)
    return {"layers": bounded, "count": len(bounded), "truncated": truncated}


def build_layer_description(
    layer: LayerSummary,
    fields: Iterable[FieldSummary],
    limit: int = DEFAULT_LIST_LIMIT,
    feature_count: Optional[int] = None,
) -> Dict[str, Any]:
    """Bounded layer metadata. ``feature_count`` is an aggregate, not a feature
    value: how many records a layer holds is the first thing anyone asks about
    it, and omitting it forced the agent to answer "I cannot know that"."""
    bounded_fields, truncated = bound_list((item.to_dict() for item in fields), limit)
    result = layer.to_dict()
    result["fields"] = bounded_fields
    result["fields_truncated"] = truncated
    if feature_count is not None and feature_count >= 0:
        result["feature_count"] = int(feature_count)
    return result


def _finite_number(value: Any) -> Optional[float]:
    """``value`` as a JSON-safe finite float, or ``None`` when it is neither."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or not -1e308 < number < 1e308:
        return None
    return round(number, 6)


def build_field_values(
    field_name: str,
    field_type: str,
    values: Iterable[Any],
    *,
    total_count: int,
    limit: int = DEFAULT_VALUE_SAMPLE,
) -> Dict[str, Any]:
    """Aggregate statistics plus a bounded sample for one attribute field.

    ``values`` is the full non-truncated value sequence for the field: the
    statistics below are computed over all of it, while only the first
    ``limit`` values (capped at :data:`MAX_VALUE_SAMPLE`) are echoed back. The
    caller is responsible for passing values from exactly one field, which is
    what keeps this bounded -- there is no shape here that can return a row.

    ``minimum``/``maximum``/``mean``/``median`` are present only when every
    non-null value is numeric, because an ordering statistic over mixed or
    textual data is exactly the silent nonsense this tool exists to expose.
    """
    clamped = max(1, min(int(limit or DEFAULT_VALUE_SAMPLE), MAX_VALUE_SAMPLE))
    materialized = list(values)
    non_null = [item for item in materialized if item is not None]
    numbers = [_finite_number(item) for item in non_null]
    numeric = bool(non_null) and all(number is not None for number in numbers)

    sample = [
        None if item is None else bound_text(item, MAX_VALUE_TEXT)
        for item in materialized[:clamped]
    ]
    distinct: set = set()
    for item in non_null:
        distinct.add(bound_text(item, MAX_VALUE_TEXT))
        if len(distinct) > MAX_LIST_ITEMS:
            break

    result: Dict[str, Any] = {
        "field_name": bound_text(field_name, 128),
        "field_type": bound_text(field_type, MAX_SHORT_TEXT),
        "numeric": numeric,
        "feature_count": max(0, int(total_count)),
        "null_count": len(materialized) - len(non_null),
        "distinct_count": min(len(distinct), MAX_LIST_ITEMS),
        "distinct_count_exact": len(distinct) <= MAX_LIST_ITEMS,
        "sample": sample,
        "sample_truncated": len(materialized) > len(sample),
    }
    if numeric:
        ordered = sorted(number for number in numbers if number is not None)
        middle = len(ordered) // 2
        median = (
            ordered[middle]
            if len(ordered) % 2
            else (ordered[middle - 1] + ordered[middle]) / 2.0
        )
        result["minimum"] = ordered[0]
        result["maximum"] = ordered[-1]
        result["mean"] = round(sum(ordered) / len(ordered), 6)
        result["median"] = round(median, 6)
    return result


def build_plugin_list(
    plugins: Iterable[PluginSummary], limit: int = DEFAULT_LIST_LIMIT
) -> Dict[str, Any]:
    bounded, truncated = bound_list((item.to_dict() for item in plugins), limit)
    return {"plugins": bounded, "count": len(bounded), "truncated": truncated}


def build_model_summary(
    available: bool,
    title: str = "",
    nodes: Iterable[ModelNodeSummary] = (),
    edge_count: int = 0,
    validation_issues: Iterable[str] = (),
    limit: int = DEFAULT_LIST_LIMIT,
) -> Dict[str, Any]:
    """Bounded current-model metadata, or ``{"available": False}`` when no
    SmartModeler studio/graph is open."""
    if not available:
        return {"available": False}
    bounded_nodes, nodes_truncated = bound_list((node.to_dict() for node in nodes), limit)
    bounded_issues, issues_truncated = bound_list(
        (bound_text(issue, 300) for issue in validation_issues), limit
    )
    return {
        "available": True,
        "title": bound_text(title, MAX_DISPLAY_NAME),
        "nodes": bounded_nodes,
        "node_count": len(bounded_nodes),
        "nodes_truncated": nodes_truncated,
        "edge_count": max(0, int(edge_count)),
        "validation_issues": bounded_issues,
        "validation_issues_truncated": issues_truncated,
    }
