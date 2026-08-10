"""QGIS-specific read-only tool handlers for the Agent Workspace.

Keeps live QGIS/Processing bindings isolated from the pure contracts,
policy, and context modules so those stay unit-testable without a QGIS
runtime. Every handler here is READ_ONLY: no project mutation, no feature
edits, no Processing execution, no plugin method invocation, and no network
access. Argument shape/type/range is already enforced by the controller
against each tool's ``input_schema`` before a handler ever runs; the light
checks below are defense in depth for direct handler invocation in tests.
"""
from __future__ import annotations

import contextlib
import html
import re
from typing import Any, Callable, Dict, Iterator, List, Optional, Tuple

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeatureRequest,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from . import context as agent_context
from .context_tokens import ContextTokenService
from .contracts import AgentRisk, AgentScope, AgentToolCall, AgentToolSpec
from .registry import AgentToolRegistry
from .workspace import WorkspaceManager, WorkspaceError

# Returns the live SmartModeler graph (duck-typed: .name, .nodes, .edges,
# .validate()) or None when no studio/model is open. Implemented as a
# callback owned by the plugin so the registry never holds a stale copy.
ModelProvider = Callable[[], Optional[Any]]
ActiveLayerProvider = Callable[[], Optional[Any]]
WorkspaceRootProvider = Callable[[], Any]

DEFAULT_LIST_LIMIT = agent_context.DEFAULT_LIST_LIMIT
MAX_LIST_LIMIT = agent_context.MAX_LIST_ITEMS
DEFAULT_PROCESSING_SEARCH_LIMIT = 8
DEFAULT_PROCESSING_DESCRIBE_LIMIT = 40
DEFAULT_PROCESSING_RESOLVE_LIMIT = 8
# Package names are short strings, so far more of them fit than detailed rows.
# Whether a plugin is installed must never depend on where its name sorts.
MAX_PLUGIN_NAMES = 300

# Mercator authids used only when a CRS exposes no PROJ string to inspect.
_MERCATOR_AUTHIDS = frozenset(
    {"EPSG:3857", "EPSG:900913", "EPSG:3785", "EPSG:102100", "ESRI:102100"}
)

_QUERY_MAX_LENGTH = 200
_ID_MAX_LENGTH = 200
_PACKAGE_MAX_LENGTH = 128

# Identity/limit constants shared with the runtime validator and apply
# coordinator. Defined in the QGIS-free ``identifiers`` module and re-exported
# here so existing imports (``from .runtime_tools import MODEL_TARGET_ID`` ...)
# keep working while the pure/model-apply paths can import them without qgis.
from .identifiers import (  # noqa: E402 - grouped with the other module constants
    MODEL_PROPOSAL_KIND,
    MODEL_TARGET_ID,
    PROCESSING_PROPOSAL_KIND,
    STYLE_PROPOSAL_KIND,
    STYLE_STATE_LIMIT,
)
from .safe_algorithm_policy import (  # noqa: E402 - same grouping
    BOOL,
    CRS,
    DISTANCE,
    ENUM,
    EXPRESSION,
    FIELD,
    MULTI_RASTER,
    MULTI_VECTOR,
    NUMBER,
    OutputSpec,
    ParamSpec,
    RASTER_LAYER,
    STRING_LABEL,
    STRING_TEXT,
    MAP_EXTENT,
    OSM_TAG,
    VECTOR_LAYER,
    default_policy,
)

# Maps a policy binding-kind to the tagged binding form a processing_run
# proposal must use for that parameter, so processing.describe can tell the
# provider exactly how to set each bindable parameter (see 10_TOOL_PROTOCOL.md).
_KIND_BINDING_FORM: Dict[str, str] = {
    VECTOR_LAYER: "layer",
    RASTER_LAYER: "layer",
    MULTI_RASTER: "layers",
    MULTI_VECTOR: "layers",
    FIELD: "field",
    NUMBER: "number",
    DISTANCE: "distance",
    BOOL: "bool",
    ENUM: "enum",
    CRS: "crs",
    STRING_LABEL: "string",
    STRING_TEXT: "text",
    MAP_EXTENT: "map_extent",
    OSM_TAG: "osm_tag",
    EXPRESSION: "expression",
}
from .plugin_capabilities import (  # noqa: E402 - same grouping
    MAX_ALGORITHMS,
    PluginView,
    ProviderView,
    build_capabilities,
)
from .plugin_actions import (  # noqa: E402
    PLUGIN_ACTION_KIND,
    capability_state as plugin_capability_state,
    public_actions as public_plugin_actions,
)

_LIMIT_PROPERTY = {"type": "integer", "minimum": 1, "maximum": MAX_LIST_LIMIT}


def _object_schema(
    properties: Optional[Dict[str, Any]] = None, required: Optional[list] = None
) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": dict(properties or {}),
        "required": list(required or []),
        "additionalProperties": False,
    }


class ToolExecutionError(RuntimeError):
    """Raised by a handler for a controlled failure; the controller sanitizes it."""


def _clamp_limit(value: Any, default: int = DEFAULT_LIST_LIMIT) -> int:
    """Defensive fallback: the controller already enforces ``_LIMIT_PROPERTY``
    (an integer between 1 and ``MAX_LIST_LIMIT``) via each tool's schema
    before a handler is ever invoked; this only guards direct handler calls."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ToolExecutionError("limit must be an integer.")
    return max(1, min(value, MAX_LIST_LIMIT))


def _layer_kind(layer: Any) -> str:
    if isinstance(layer, QgsVectorLayer):
        return "vector"
    if isinstance(layer, QgsRasterLayer):
        return "raster"
    return "other"


def _layer_geometry_type(layer: Any) -> str:
    if not isinstance(layer, QgsVectorLayer):
        return ""
    try:
        return layer.geometryType().name
    except AttributeError:
        return str(layer.geometryType())


def _layer_is_visible(layer: Any) -> bool:
    project = QgsProject.instance()
    root = project.layerTreeRoot() if project is not None else None
    if root is None:
        return True
    node = root.findLayer(layer.id())
    return bool(node.itemVisibilityChecked()) if node is not None else True


def crs_is_area_safe(crs: Any) -> bool:
    """Whether ``$area``/``$length`` on a layer in ``crs`` returns true metres.

    Two families are not safe and neither announces itself:

    * a **geographic** CRS measures in degrees, so ``$area`` is a number with
      no physical meaning at all;
    * a **Mercator** CRS (EPSG:3857 and its aliases -- what every OSM/XYZ
      download hands over) reports metres, but conformal ones inflated by
      ``1/cos^2(latitude)``: 1.76x at 41 degrees north. A genuinely 324 m^2
      building measures 569 m^2 there, so a "smaller than 400 m^2" filter
      quietly discards it and reports success.

    An unknown or invalid CRS is treated as unsafe: refusing a run the caller
    can fix by reprojecting is cheaper than shipping a plausible wrong number.
    """
    if crs is None:
        return False
    try:
        if not crs.isValid():
            return False
        if crs.isGeographic():
            return False
    except AttributeError:
        return False
    projection = ""
    for accessor in ("toProj", "toProj4"):
        method = getattr(crs, accessor, None)
        if method is None:
            continue
        with contextlib.suppress(Exception):
            projection = str(method() or "")
        if projection:
            break
    if not projection:
        # No PROJ string to inspect: fall back to the authids that matter.
        with contextlib.suppress(Exception):
            return crs.authid() not in _MERCATOR_AUTHIDS
        return False
    return "+proj=merc" not in projection.lower()


def _layer_summary(
    layer: Any, active_layer_id: str = ""
) -> agent_context.LayerSummary:
    crs_authid = ""
    crs = layer.crs()
    if crs is not None and crs.isValid():
        crs_authid = crs.authid()
    provider_key = ""
    try:
        provider_key = layer.providerType() or ""
    except AttributeError:
        pass
    return agent_context.LayerSummary(
        layer_id=layer.id(),
        name=layer.name(),
        kind=_layer_kind(layer),
        geometry_type=_layer_geometry_type(layer),
        crs=crs_authid,
        visible=_layer_is_visible(layer),
        provider_key=provider_key,
        active=bool(active_layer_id and layer.id() == active_layer_id),
        area_safe_crs=crs_is_area_safe(crs),
    )


def _tool_project_summary(_call: AgentToolCall) -> Dict[str, Any]:
    project = QgsProject.instance()
    if project is None:
        return agent_context.build_project_summary("No project", "", 0)
    title = project.title() or "Untitled project"
    crs_authid = ""
    project_crs = project.crs()
    if project_crs is not None and project_crs.isValid():
        crs_authid = project_crs.authid()
    return agent_context.build_project_summary(title, crs_authid, len(project.mapLayers()))


def _tool_layer_list(
    call: AgentToolCall,
    active_layer_provider: Optional[ActiveLayerProvider] = None,
) -> Dict[str, Any]:
    limit = _clamp_limit(
        call.arguments.get("limit"),
        DEFAULT_PROCESSING_SEARCH_LIMIT,
    )
    project = QgsProject.instance()
    layers = list(project.mapLayers().values()) if project is not None else []
    active_layer = None
    if active_layer_provider is not None:
        with contextlib.suppress(Exception):
            active_layer = active_layer_provider()
    active_layer_id = ""
    with contextlib.suppress(Exception):
        active_layer_id = active_layer.id() if active_layer is not None else ""
    if active_layer_id:
        # Keep the active row inside even a small bounded response and make it
        # the unambiguous first candidate for "active layer" requests.
        layers.sort(key=lambda layer: layer.id() != active_layer_id)
    summaries = (
        _layer_summary(layer, active_layer_id)
        for layer in layers
    )
    return agent_context.build_layer_list(summaries, limit)


def _tool_layer_list_factory(
    active_layer_provider: Optional[ActiveLayerProvider],
) -> Callable[[AgentToolCall], Dict[str, Any]]:
    return lambda call: _tool_layer_list(call, active_layer_provider)


def _tool_layer_describe(call: AgentToolCall) -> Dict[str, Any]:
    layer_id = call.arguments.get("layer_id")
    if not isinstance(layer_id, str) or not layer_id.strip():
        raise ToolExecutionError("layer_id must be a non-empty string.")
    limit = _clamp_limit(call.arguments.get("limit"))
    project = QgsProject.instance()
    layer = project.mapLayer(layer_id) if project is not None else None
    if layer is None:
        return {"available": False, "layer_id": agent_context.bound_text(layer_id, 128)}
    fields: Iterator[agent_context.FieldSummary] = iter(())
    feature_count = None
    if isinstance(layer, QgsVectorLayer):
        requested_field = call.arguments.get("field_name", "")
        if not isinstance(requested_field, str):
            raise ToolExecutionError("field_name must be a string.")
        requested_field = requested_field.strip()
        fields = (
            agent_context.FieldSummary(field_def.name(), field_def.typeName())
            for field_def in layer.fields()
            if not requested_field or field_def.name() == requested_field
        )
        with contextlib.suppress(Exception):
            feature_count = layer.featureCount()
    result = agent_context.build_layer_description(
        _layer_summary(layer), fields, limit, feature_count=feature_count
    )
    result["available"] = True
    return result


def _utm_authid(longitude: float, latitude: float) -> str:
    """The WGS 84 / UTM authid covering a WGS 84 coordinate.

    Arithmetic rather than a lookup, so it answers anywhere on Earth and never
    goes stale. Scanning the CRS database for everything whose area of use
    contains the point is the obvious alternative and is not viable: 13 790
    definitions take ~41 s and return 348 hits, which is neither a tool call
    nor an answer.
    """
    zone = int((longitude + 180.0) / 6.0) + 1
    zone = max(1, min(60, zone))
    return f"EPSG:{326 if latitude >= 0 else 327}{zone:02d}"


def _crs_covers(crs: Any, longitude: float, latitude: float) -> bool:
    """Whether ``crs``'s declared area of use contains the point."""
    with contextlib.suppress(Exception):
        bounds = crs.bounds()
        return (
            bounds.xMinimum() <= longitude <= bounds.xMaximum()
            and bounds.yMinimum() <= latitude <= bounds.yMaximum()
        )
    return False


def _crs_suggestion(crs: Any, reason: str) -> Optional[Dict[str, Any]]:
    with contextlib.suppress(Exception):
        if not crs.isValid() or not crs.authid():
            return None
        return {
            "crs": agent_context.bound_text(crs.authid(), 32),
            "description": agent_context.bound_text(crs.description(), 120),
            "reason": reason,
        }
    return None


def _tool_suggest_crs(call: AgentToolCall) -> Dict[str, Any]:
    """Metric CRS candidates for a layer, so "the local CRS" is answerable.

    Without this the agent had to invent an answer to "reproject to the local
    CRS" and the proposal died on "A CRS must look like AUTHORITY:CODE" -- a
    malformed-input error for a question it had no way to research. Every
    candidate here is a live QGIS CRS whose area of use actually contains the
    layer, so the authid is real by construction.
    """
    layer_id = call.arguments.get("layer_id")
    if not isinstance(layer_id, str) or not layer_id.strip():
        raise ToolExecutionError("layer_id must be a non-empty string.")
    project = QgsProject.instance()
    layer = project.mapLayer(layer_id) if project is not None else None
    if layer is None:
        return {
            "available": False,
            "layer_id": agent_context.bound_text(layer_id, 128),
        }

    source_crs = layer.crs()
    result: Dict[str, Any] = {
        "available": True,
        "layer_id": agent_context.bound_text(layer_id, 128),
        "current_crs": agent_context.bound_text(
            source_crs.authid() if source_crs is not None else "", 32
        ),
        "current_crs_area_safe": crs_is_area_safe(source_crs),
    }

    wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
    longitude = latitude = None
    with contextlib.suppress(Exception):
        extent = layer.extent()
        if not extent.isEmpty() and source_crs is not None and source_crs.isValid():
            transform = QgsCoordinateTransform(source_crs, wgs84, project)
            geographic = transform.transformBoundingBox(extent)
            longitude = geographic.center().x()
            latitude = geographic.center().y()
    if longitude is None or latitude is None or not (
        -180.0 <= longitude <= 180.0 and -90.0 <= latitude <= 90.0
    ):
        # No usable footprint: say so rather than suggest a zone for nowhere.
        result["suggestions"] = []
        result["locatable"] = False
        return result

    result["locatable"] = True
    result["centre_lon"] = round(longitude, 3)
    result["centre_lat"] = round(latitude, 3)

    suggestions: List[Dict[str, Any]] = []
    seen: set = set()

    def offer(crs: Any, reason: str) -> None:
        item = _crs_suggestion(crs, reason)
        if item is None or item["crs"] in seen:
            return
        if not crs_is_area_safe(crs) or not _crs_covers(crs, longitude, latitude):
            return
        seen.add(item["crs"])
        suggestions.append(item)

    # The universal answer first: the UTM zone the layer actually sits in.
    offer(QgsCoordinateReferenceSystem(_utm_authid(longitude, latitude)), "utm_zone")

    # Then what this user already works in. A national grid (TUREF/TM27 for
    # western Türkiye, say) is a better local answer than UTM, and reading it
    # off the project and the CRS history means never shipping a country table
    # that can go stale or be wrong.
    with contextlib.suppress(Exception):
        offer(project.crs(), "project_crs")
    with contextlib.suppress(Exception):
        for other in project.mapLayers().values():
            if other.id() != layer_id:
                offer(other.crs(), "used_by_another_layer")
    with contextlib.suppress(Exception):
        registry = QgsApplication.coordinateReferenceSystemRegistry()
        for crs in list(registry.recentCrs())[:20]:
            offer(crs, "recently_used")

    result["suggestions"] = suggestions[:6]
    return result


def _tool_field_values(call: AgentToolCall) -> Dict[str, Any]:
    """Statistics and a bounded value sample for one named field.

    The only tool that returns attribute values, and it returns them for one
    explicitly named field of one explicitly named layer -- never a feature,
    never a row, never a second field alongside. See ``build_field_values``
    for why the exception exists.
    """
    layer_id = call.arguments.get("layer_id")
    if not isinstance(layer_id, str) or not layer_id.strip():
        raise ToolExecutionError("layer_id must be a non-empty string.")
    field_name = call.arguments.get("field_name")
    if not isinstance(field_name, str) or not field_name.strip():
        raise ToolExecutionError("field_name must be a non-empty string.")
    field_name = field_name.strip()
    limit = call.arguments.get("limit")
    limit = (
        agent_context.DEFAULT_VALUE_SAMPLE
        if not isinstance(limit, int) or isinstance(limit, bool)
        else limit
    )

    project = QgsProject.instance()
    layer = project.mapLayer(layer_id) if project is not None else None
    if layer is None or not isinstance(layer, QgsVectorLayer):
        return {
            "available": False,
            "layer_id": agent_context.bound_text(layer_id, 128),
        }
    index = layer.fields().lookupField(field_name)
    if index < 0:
        return {
            "available": False,
            "layer_id": agent_context.bound_text(layer_id, 128),
            "field_name": agent_context.bound_text(field_name, 128),
            "field_missing": True,
        }
    field_type = ""
    with contextlib.suppress(Exception):
        field_type = layer.fields().at(index).typeName()

    feature_count = 0
    with contextlib.suppress(Exception):
        feature_count = layer.featureCount()
    values: List[Any] = []
    try:
        request = QgsFeatureRequest()
        request.setFlags(QgsFeatureRequest.NoGeometry)
        request.setSubsetOfAttributes([index])
        for feature in layer.getFeatures(request):
            value = feature.attribute(index)
            # QGIS hands a typed null back as QVariant/NULL rather than None.
            values.append(None if value is None or str(value) == "NULL" else value)
    except Exception as error:  # pragma: no cover - provider-specific failures
        raise ToolExecutionError("The field values could not be read.") from error

    result = agent_context.build_field_values(
        field_name,
        field_type,
        values,
        total_count=feature_count,
        limit=limit,
    )
    result["available"] = True
    result["layer_id"] = agent_context.bound_text(layer_id, 128)
    result["area_safe_crs"] = crs_is_area_safe(layer.crs())
    return result


def _algorithm_provider_id(algorithm: Any) -> str:
    """The owning provider's id, or "" -- never a source path or module path."""
    with contextlib.suppress(Exception):
        provider = algorithm.provider()
        if provider is not None:
            return agent_context.bound_text(provider.id(), 128)
    return ""


def _tool_processing_search(call: AgentToolCall) -> Dict[str, Any]:
    query = call.arguments.get("query", "")
    if not isinstance(query, str):
        raise ToolExecutionError("query must be a string.")
    limit = _clamp_limit(call.arguments.get("limit"))
    registry = QgsApplication.processingRegistry()
    terms = [term for term in query.lower().split() if term]
    matches: list = []
    policy = default_policy()
    if registry is not None:
        for algorithm in registry.algorithms():
            haystack = f"{algorithm.id()} {algorithm.displayName()}".lower()
            if terms and not all(term in haystack for term in terms):
                continue
            decision = policy.is_runnable(
                algorithm.id(),
                build_param_specs(algorithm),
                build_output_specs(algorithm),
            )
            matches.append(
                {
                    "algorithm_id": agent_context.bound_text(algorithm.id(), 200),
                    "title": agent_context.bound_text(
                        algorithm.displayName(), agent_context.MAX_DISPLAY_NAME
                    ),
                    "group": agent_context.bound_text(
                        algorithm.group(), agent_context.MAX_DISPLAY_NAME
                    ),
                    "provider_id": _algorithm_provider_id(algorithm),
                    "agent_runnable": decision.allowed,
                    "agent_reason": (
                        "" if decision.allowed else decision.reason_code
                    ),
                }
            )
    # Relevance ranking requires the full match set before truncation; this
    # is inherent to "search", not a laziness gap in bound_list() itself.
    matches.sort(
        key=lambda item: (
            not item["agent_runnable"],
            item["algorithm_id"],
        )
    )
    bounded, truncated = agent_context.bound_list(matches, limit)
    return {"algorithms": bounded, "count": len(bounded), "truncated": truncated}


def _expression_help_text(name: str) -> str:
    """Return bounded plain text from QGIS's built-in function help."""

    from qgis.core import QgsExpression

    raw = ""
    with contextlib.suppress(Exception):
        raw = str(QgsExpression.helpText(name) or "")
    plain = html.unescape(re.sub(r"<[^>]+>", " ", raw))
    plain = re.sub(r"\s+", " ", plain).strip()
    return agent_context.bound_text(plain, 1_500)


def _tool_expression_search(call: AgentToolCall) -> Dict[str, Any]:
    """Search the live built-in QGIS expression catalog without evaluation."""

    from qgis.core import QgsExpression

    from .qgis_expression_policy import is_agent_expression_function

    query = call.arguments.get("query", "")
    if not isinstance(query, str) or not query.strip():
        raise ToolExecutionError("query must be a non-empty string.")
    limit = _clamp_limit(call.arguments.get("limit"))
    terms = tuple(
        term for term in re.findall(r"[a-z0-9_$]+", query.casefold()) if term
    )
    scored = []
    for function in QgsExpression.Functions():
        if not is_agent_expression_function(function):
            continue
        with contextlib.suppress(Exception):
            name = str(function.name())
            group = str(function.group())
            haystack = f"{name} {group}".casefold()
            if terms and not all(term in haystack for term in terms):
                continue
            score = (
                0
                if name.casefold() == query.strip().casefold()
                else 1
                if name.casefold().startswith(query.strip().casefold())
                else 2
            )
            scored.append((score, name.casefold(), name, group))
    scored.sort()
    selected = scored[:limit]
    return {
        "functions": [
            {
                "name": agent_context.bound_text(name, 128),
                "group": agent_context.bound_text(group, 128),
                "help": _expression_help_text(name),
                "proposal_binding": "expression",
            }
            for _score, _sort_name, name, group in selected
        ],
        "count": len(selected),
        "truncated": len(scored) > len(selected),
    }


def _param_is_optional(definition: Any) -> bool:
    """Whether a live parameter definition carries the Optional flag.

    QGIS 4 exposes ``Qgis.ProcessingParameterFlag.Optional`` and QGIS 3 exposes
    ``QgsProcessingParameterDefinition.Flag.FlagOptional`` (both are currently
    present on 3.44 and 4.2 with the same bit). Both are probed so a future
    removal of either spelling degrades to "not optional", which fails *closed*
    in the signature gate rather than silently widening it.
    """
    flags = 0
    with contextlib.suppress(Exception):
        flags = int(definition.flags())
    if not flags:
        return False
    for owner, attribute in (
        ("Qgis", "ProcessingParameterFlag.Optional"),
        ("QgsProcessingParameterDefinition", "Flag.FlagOptional"),
    ):
        with contextlib.suppress(Exception):
            from qgis import core as qgis_core

            target: Any = getattr(qgis_core, owner)
            for part in attribute.split("."):
                target = getattr(target, part)
            if flags & int(target):
                return True
    return False


def _param_allows_multiple(definition: Any) -> bool:
    """Whether the parameter accepts a list of inputs."""
    type_names = {cls.__name__ for cls in type(definition).__mro__}
    return "QgsProcessingParameterMultipleLayers" in type_names


def _param_options(definition: Any) -> list:
    """Bounded enum option labels, or [] for a non-enum parameter."""
    options: list = []
    with contextlib.suppress(Exception):  # only enum parameters have options
        options = [
            agent_context.bound_text(str(option), 128)
            for option in list(definition.options())[: agent_context.MAX_LIST_ITEMS]
        ]
    return options


def _param_bound(definition: Any, which: str) -> Any:
    """A numeric parameter's live minimum/maximum, or ``None``.

    Sentinel-sized bounds (the float min/max QGIS uses to mean "unbounded") are
    reported as ``None`` so they do not read as real limits.
    """
    with contextlib.suppress(Exception):
        value = float(getattr(definition, which)())
        if abs(value) >= 1e307:
            return None
        return value
    return None


def build_param_specs(algorithm: Any) -> List[ParamSpec]:
    """Adapt one live algorithm's parameter definitions into QGIS-free views.

    The ``type_names`` set is the definition's full class MRO by name, so the
    pure policy can match a parameter kind (for example, a Distance parameter
    also reports ``QgsProcessingParameterNumber``) without importing QGIS.
    """
    from ..graph_model import GraphModel

    specs: List[ParamSpec] = []
    for definition in algorithm.parameterDefinitions():
        options: tuple = ()
        with contextlib.suppress(Exception):  # only enum parameters have options
            raw_options = definition.options()
            options = tuple(
                agent_context.bound_text(str(option), 128) for option in raw_options
            )
        minimum = None
        maximum = None
        with contextlib.suppress(Exception):  # only numeric parameters have bounds
            minimum = float(definition.minimum())
            maximum = float(definition.maximum())
        has_default = False
        with contextlib.suppress(Exception):
            has_default = bool(GraphModel.value_is_configured(definition.defaultValue()))
        source_type = ""
        if "QgsProcessingParameterMultipleLayers" in {
            cls.__name__ for cls in type(definition).__mro__
        }:
            with contextlib.suppress(Exception):
                live_type = definition.layerType()
                if live_type == Qgis.ProcessingSourceType.Raster:
                    source_type = "raster"
                elif live_type in (
                    Qgis.ProcessingSourceType.Vector,
                    Qgis.ProcessingSourceType.VectorAnyGeometry,
                    Qgis.ProcessingSourceType.VectorPoint,
                    Qgis.ProcessingSourceType.VectorLine,
                    Qgis.ProcessingSourceType.VectorPolygon,
                ):
                    source_type = "vector"
        specs.append(
            ParamSpec(
                name=agent_context.bound_text(definition.name(), 128),
                is_destination=bool(definition.isDestination()),
                type_names=frozenset(cls.__name__ for cls in type(definition).__mro__),
                is_optional=_param_is_optional(definition),
                has_default=has_default,
                options=options,
                minimum=minimum,
                maximum=maximum,
                source_type=source_type,
            )
        )
    return specs


def build_output_specs(algorithm: Any) -> List[OutputSpec]:
    """Build the bounded live output view used by reviewed adapters."""
    return [
        OutputSpec(
            name=agent_context.bound_text(definition.name(), 128),
            type_names=frozenset(
                cls.__name__ for cls in type(definition).__mro__
            ),
        )
        for definition in algorithm.outputDefinitions()
    ]


def algorithm_signature_state(algorithm: Any) -> Dict[str, Any]:
    """The canonical live-signature state a ``processing_run`` receipt signs.

    Deliberately limited to structure -- id plus each parameter's name, type,
    destination flag and optional flag. It carries no default value, no source
    path, and no feature value, and it changes whenever a provider update adds,
    removes, retypes, or re-flags a parameter, which invalidates every open
    receipt for that algorithm.
    """
    parameters = []
    for definition in algorithm.parameterDefinitions():
        parameters.append(
            [
                agent_context.bound_text(definition.name(), 128),
                agent_context.bound_text(type(definition).__name__, 128),
                bool(definition.isDestination()),
                _param_is_optional(definition),
            ]
        )
    parameters.sort(key=lambda item: item[0])
    outputs = []
    for definition in algorithm.outputDefinitions():
        outputs.append(
            [
                agent_context.bound_text(definition.name(), 128),
                agent_context.bound_text(type(definition).__name__, 128),
            ]
        )
    outputs.sort(key=lambda item: item[0])
    return {
        "algorithm_id": agent_context.bound_text(algorithm.id(), 200),
        "parameters": parameters,
        "outputs": outputs,
    }


def _tool_processing_describe_factory(
    token_service: ContextTokenService,
) -> Callable[[AgentToolCall], Dict[str, Any]]:
    def _handler(call: AgentToolCall) -> Dict[str, Any]:
        algorithm_id = call.arguments.get("algorithm_id")
        if not isinstance(algorithm_id, str) or not algorithm_id.strip():
            raise ToolExecutionError("algorithm_id must be a non-empty string.")
        limit = _clamp_limit(
            call.arguments.get("limit"),
            DEFAULT_PROCESSING_DESCRIBE_LIMIT,
        )
        registry = QgsApplication.processingRegistry()
        algorithm = registry.algorithmById(algorithm_id) if registry is not None else None
        if algorithm is None:
            return {
                "available": False,
                "algorithm_id": agent_context.bound_text(algorithm_id, 200),
            }
        # Which parameters a processing_run proposal may actually set, and in
        # which tagged binding form. Without this the provider tried to bind a
        # reviewed-but-unbindable parameter (e.g. reprojectlayer's OPERATION)
        # and the run failed with "This parameter cannot be set by a proposal".
        param_specs = build_param_specs(algorithm)
        run_decision = default_policy().is_runnable(
            algorithm.id(),
            param_specs,
            build_output_specs(algorithm),
        )
        run_record = run_decision.record if run_decision.allowed else None
        specs_by_name = {spec.name: spec for spec in param_specs}
        required_names = (
            set(run_record.required_layer_params) | set(run_record.required_params)
            if run_record is not None
            else set()
        )

        def _binding_of(name: str) -> str:
            if run_record is None:
                return ""
            kind = run_record.bindable.get(name)
            return _KIND_BINDING_FORM.get(kind, "") if kind else ""

        # The safe *contract* of each parameter: enough to explain and to fill
        # in correctly, and deliberately never the raw ``defaultValue()``,
        # which for a third-party algorithm can be a file path or connection.
        # "required" means required in an Agent proposal, not merely that QGIS
        # marks the definition non-optional. A configured QGIS default is
        # intentionally omitted unless the user asks to override it.
        def _parameter_row(definition: Any) -> Dict[str, Any]:
            name = agent_context.bound_text(definition.name(), 128)
            spec = specs_by_name.get(name)
            binding = _binding_of(name)
            return {
                "name": name,
                "type": agent_context.bound_text(definition.type(), 64),
                "required": name in required_names,
                "has_default": bool(spec is not None and spec.has_default),
                "default_behavior": (
                    "omit_to_use_qgis_default"
                    if spec is not None and spec.has_default and binding
                    else ""
                ),
                "destination": bool(definition.isDestination()),
                "multiple": _param_allows_multiple(definition),
                "enum_options": _param_options(definition),
                "minimum": _param_bound(definition, "minimum"),
                "maximum": _param_bound(definition, "maximum"),
                # "" means a processing_run may not set this parameter at all.
                "proposal_binding": binding,
                "alternative_binding": (
                    "layer_extent"
                    if binding == "map_extent"
                    else ""
                ),
            }

        parameter_rows = [
            _parameter_row(definition)
            for definition in algorithm.parameterDefinitions()
        ]
        if len(parameter_rows) > limit:
            # Keep the information needed for a valid proposal when a large
            # third-party signature must fit the prompt budget: required
            # inputs first, then extent/layer bindings, then other bindable
            # inputs, and destinations last. Preserve live parameter order in
            # the retained rows so enum and field relationships remain clear.
            ranked = sorted(
                enumerate(parameter_rows),
                key=lambda item: (
                    0 if item[1]["required"] else (
                        1 if item[1]["alternative_binding"] else (
                            2 if item[1]["proposal_binding"] and not item[1]["destination"] else (
                                3 if not item[1]["destination"] else 4
                            )
                        )
                    ),
                    item[0],
                ),
            )
            kept_indexes = {index for index, _row in ranked[:limit]}
            parameter_rows = [
                row for index, row in enumerate(parameter_rows)
                if index in kept_indexes
            ]
        bounded = parameter_rows
        truncated = len(bounded) < len(list(algorithm.parameterDefinitions()))
        outputs, outputs_truncated = agent_context.bound_list(
            (
                {
                    "name": agent_context.bound_text(output.name(), 128),
                    "type": agent_context.bound_text(output.type(), 64),
                }
                for output in algorithm.outputDefinitions()
            ),
            limit,
        )
        return {
            "available": True,
            "algorithm_id": agent_context.bound_text(algorithm.id(), 200),
            "title": agent_context.bound_text(
                algorithm.displayName(), agent_context.MAX_DISPLAY_NAME
            ),
            "group": agent_context.bound_text(algorithm.group(), agent_context.MAX_DISPLAY_NAME),
            "provider_id": _algorithm_provider_id(algorithm),
            "parameters": bounded,
            "parameters_truncated": truncated,
            "outputs": outputs,
            "outputs_truncated": outputs_truncated,
            "agent_runnable": run_decision.allowed,
            "agent_reason": "" if run_decision.allowed else run_decision.reason_code,
            # The freshness receipt for a later processing_run proposal. It
            # authorizes nothing: the deny-by-default SafeAlgorithmPolicy is the
            # only thing that decides whether this algorithm may ever run, and
            # it is re-checked against the live signature at approval time.
            "context_token": token_service.issue(
                PROCESSING_PROPOSAL_KIND, algorithm.id(), algorithm_signature_state(algorithm)
            ),
        }

    return _handler


def _resolved_identity(described: Any) -> Dict[str, Any]:
    """Lift the identity a proposal needs out of a nested resolve payload.

    ``processing.resolve`` returns the signature under ``resolved``. Everything
    required to *propose* -- the algorithm id and the context token -- was
    therefore one level deeper than the shape every proposal example shows, and
    a token read from the top level came back empty. Surfacing both here makes
    the reachable shape the correct one instead of a documented trap.
    """
    if not isinstance(described, dict):
        return {}
    identity = {}
    token = described.get("context_token")
    if isinstance(token, str) and token:
        identity["context_token"] = token
    algorithm_id = described.get("algorithm_id")
    if isinstance(algorithm_id, str) and algorithm_id:
        identity["algorithm_id"] = algorithm_id
    return identity


def _tool_processing_resolve_factory(
    token_service: ContextTokenService,
) -> Callable[[AgentToolCall], Dict[str, Any]]:
    """Combine Processing discovery and signature inspection in one call."""
    describe = _tool_processing_describe_factory(token_service)

    def _handler(call: AgentToolCall) -> Dict[str, Any]:
        algorithm_id = str(call.arguments.get("algorithm_id", "") or "").strip()
        query = str(call.arguments.get("query", "") or "").strip()
        limit = _clamp_limit(
            call.arguments.get("limit"),
            DEFAULT_PROCESSING_RESOLVE_LIMIT,
        )
        if not algorithm_id and not query:
            raise ToolExecutionError("query or algorithm_id is required.")
        if algorithm_id:
            described = describe(
                AgentToolCall(
                    call_id=call.call_id,
                    tool_name="processing.describe",
                    arguments={"algorithm_id": algorithm_id, "limit": limit},
                )
            )
            return {
                "resolved": described,
                # Also surfaced at the top level. A proposal is rejected outright
                # without this token, and when it existed only under `resolved`
                # a whole correct multi-turn run could end in
                # "Missing or invalid context_token".
                **_resolved_identity(described),
                "algorithms": [],
                "truncated": False,
            }
        search = _tool_processing_search(
            AgentToolCall(
                call_id=call.call_id,
                tool_name="processing.search",
                arguments={"query": query, "limit": min(limit, 5)},
            )
        )
        algorithms = search.get("algorithms", [])
        resolved = None
        if len(algorithms) == 1 and isinstance(algorithms[0], dict):
            candidate = str(algorithms[0].get("algorithm_id", "") or "")
            if candidate:
                resolved = describe(
                    AgentToolCall(
                        call_id=call.call_id,
                        tool_name="processing.describe",
                        arguments={"algorithm_id": candidate, "limit": limit},
                    )
                )
        return {
            "resolved": resolved,
            **_resolved_identity(resolved),
            "algorithms": algorithms,
            "truncated": bool(search.get("truncated", False)),
        }

    return _handler


def _tool_model_summary_factory(model_provider: ModelProvider) -> Callable[[AgentToolCall], Dict[str, Any]]:
    def _handler(call: AgentToolCall) -> Dict[str, Any]:
        limit = _clamp_limit(call.arguments.get("limit"))
        graph = model_provider()
        if graph is None:
            return agent_context.build_model_summary(False)
        nodes = (
            agent_context.ModelNodeSummary(node.node_id, node.title, node.algorithm_id)
            for node in graph.nodes.values()
        )
        return agent_context.build_model_summary(
            True, graph.name, nodes, len(graph.edges), (), limit
        )

    return _handler


def _tool_model_validate_factory(model_provider: ModelProvider) -> Callable[[AgentToolCall], Dict[str, Any]]:
    def _handler(call: AgentToolCall) -> Dict[str, Any]:
        limit = _clamp_limit(call.arguments.get("limit"))
        graph = model_provider()
        if graph is None:
            return {"available": False}
        all_issues = [f"{issue.level}: {issue.message}" for issue in graph.validate()]
        bounded, truncated = agent_context.bound_list(
            (agent_context.bound_text(issue, 300) for issue in all_issues), limit
        )
        return {
            "available": True,
            "issue_count": len(all_issues),
            "issues": bounded,
            "issues_truncated": truncated,
        }

    return _handler


def _iter_plugin_summaries(
    qgis_utils: Any, package_names: Iterator[str], active: set
) -> Iterator[Dict[str, Any]]:
    for package_name in package_names:
        version = ""
        display_name = package_name
        has_provider = False
        with contextlib.suppress(Exception):
            version = str(qgis_utils.pluginMetadata(package_name, "version") or "")
            name_meta = str(qgis_utils.pluginMetadata(package_name, "name") or "")
            display_name = name_meta or package_name
            provider_meta = str(
                qgis_utils.pluginMetadata(package_name, "hasProcessingProvider") or ""
            )
            has_provider = provider_meta.strip().lower() == "yes"
        yield agent_context.PluginSummary(
            package_name=package_name,
            display_name=display_name,
            version=version,
            enabled=package_name in active,
            has_processing_provider=has_provider,
        ).to_dict()


def _tool_plugin_list(call: AgentToolCall) -> Dict[str, Any]:
    limit = _clamp_limit(call.arguments.get("limit"))
    try:
        import qgis.utils as qgis_utils
    except ImportError as error:
        raise ToolExecutionError("Plugin registry is unavailable.") from error

    # ``available_plugins`` is every plugin package QGIS found on disk,
    # whether or not it is currently active/loaded; ``plugins``/
    # ``active_plugins`` only cover loaded/enabled ones. Enumerate the union
    # so a disabled-but-installed plugin still appears (with enabled: false)
    # instead of being silently omitted, while never instantiating or
    # invoking any plugin object.
    available = set(getattr(qgis_utils, "available_plugins", []) or [])
    active = set(getattr(qgis_utils, "active_plugins", []) or [])
    loaded = set(getattr(qgis_utils, "plugins", {}) or {})
    package_names = sorted(available | active | loaded)

    summaries = _iter_plugin_summaries(qgis_utils, iter(package_names), active)
    bounded, truncated = agent_context.bound_list(summaries, limit)
    # The detailed rows are bounded because each one reads metadata, but the
    # *names* are short and must never be withheld. They used to be, and the
    # list is alphabetical: on a QGIS with more plugins than the limit,
    # everything from "z" onwards vanished -- including the OSM downloader this
    # plugin's whole acquisition path depends on. The Agent then reported, from
    # a list that looked complete, that an installed plugin was not installed.
    return {
        "plugins": bounded,
        "count": len(bounded),
        "truncated": truncated,
        "installed_packages": [
            agent_context.bound_text(name, 128)
            for name in package_names[:MAX_PLUGIN_NAMES]
        ],
        "installed_count": len(package_names),
    }


# -- model.describe --------------------------------------------------------


def _value_configured(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _node_topology(node: Any, limit: int) -> Dict[str, Any]:
    input_items = []
    for port_id, port in node.inputs.items():
        input_items.append(
            {
                "port_id": agent_context.bound_text(port_id, 64),
                "socket_type": agent_context.bound_text(port.socket_type, 32),
                "required": bool(port.required),
                "connected": bool(port.is_connected()),
                "configured": _value_configured(
                    node.parameters.get(port_id, port.default_value)
                ),
            }
        )
    output_items = [
        {
            "port_id": agent_context.bound_text(port_id, 64),
            "socket_type": agent_context.bound_text(port.socket_type, 32),
        }
        for port_id, port in node.outputs.items()
    ]
    bounded_inputs, inputs_truncated = agent_context.bound_list(input_items, limit)
    bounded_outputs, outputs_truncated = agent_context.bound_list(output_items, limit)
    return {
        "node_id": agent_context.bound_text(node.node_id, 64),
        "title": agent_context.bound_text(node.title, agent_context.MAX_DISPLAY_NAME),
        "algorithm_id": agent_context.bound_text(node.algorithm_id, 200),
        "inputs": bounded_inputs,
        "inputs_truncated": inputs_truncated,
        "outputs": bounded_outputs,
        "outputs_truncated": outputs_truncated,
    }


def extract_model_topology(graph: Any, limit: int) -> Dict[str, Any]:
    """Return the bounded, values-free topology of ``graph``.

    Never includes baseline parameter values, cached results, paths, or a
    canonical serialization -- only node/port/edge structure and live
    validation issue summaries. ``graph is None`` yields ``available: False``.
    """
    if graph is None:
        return {"available": False}
    node_summaries = (_node_topology(node, limit) for node in graph.nodes.values())
    bounded_nodes, nodes_truncated = agent_context.bound_list(node_summaries, limit)
    edge_summaries = (
        {
            "edge_id": agent_context.bound_text(edge.edge_id, 200),
            "from_node": agent_context.bound_text(edge.start_node_id, 64),
            "from_output": agent_context.bound_text(edge.start_port_id, 64),
            "to_node": agent_context.bound_text(edge.end_node_id, 64),
            "to_input": agent_context.bound_text(edge.end_port_id, 64),
        }
        for edge in graph.edges.values()
    )
    bounded_edges, edges_truncated = agent_context.bound_list(edge_summaries, limit)
    all_issues = [f"{issue.level}: {issue.message}" for issue in graph.validate()]
    bounded_issues, issues_truncated = agent_context.bound_list(
        (agent_context.bound_text(issue, 300) for issue in all_issues), limit
    )
    return {
        "available": True,
        "name": agent_context.bound_text(graph.name, agent_context.MAX_DISPLAY_NAME),
        "node_count": len(graph.nodes),
        "edge_count": len(graph.edges),
        "nodes": bounded_nodes,
        "nodes_truncated": nodes_truncated,
        "edges": bounded_edges,
        "edges_truncated": edges_truncated,
        "validation_issues": bounded_issues,
        "validation_issue_count": len(all_issues),
        "validation_issues_truncated": issues_truncated,
    }


def _tool_model_describe_factory(
    model_provider: ModelProvider, token_service: ContextTokenService
) -> Callable[[AgentToolCall], Dict[str, Any]]:
    def _handler(call: AgentToolCall) -> Dict[str, Any]:
        limit = _clamp_limit(call.arguments.get("limit"))
        graph = model_provider()
        result = extract_model_topology(graph, limit)
        # A token is always issued -- even for the no-model state -- so a
        # model_patch proposal can prove it was written against exactly this
        # (possibly empty) graph. The signed canonical state includes parameter
        # values so any edit invalidates the token, but is never returned here.
        result["context_token"] = token_service.issue(
            MODEL_PROPOSAL_KIND, MODEL_TARGET_ID, agent_context.canonical_model_state(graph)
        )
        return result

    return _handler


# -- layer.style -----------------------------------------------------------


def _symbol_type_name(symbol: Any) -> str:
    """Return a stable word for a symbol type without leaking a Python repr."""
    from qgis.core import QgsFillSymbol, QgsLineSymbol, QgsMarkerSymbol

    if isinstance(symbol, QgsMarkerSymbol):
        return "marker"
    if isinstance(symbol, QgsLineSymbol):
        return "line"
    if isinstance(symbol, QgsFillSymbol):
        return "fill"
    return "symbol"


def _symbol_summary(symbol: Any, limit: int) -> Dict[str, Any]:
    summary: Dict[str, Any] = {"type": _symbol_type_name(symbol)}
    with contextlib.suppress(Exception):
        color = agent_context.normalize_hex_color(symbol.color().name())
        if color is not None:
            summary["color"] = color
    with contextlib.suppress(Exception):
        opacity = float(symbol.opacity())
        if 0.0 <= opacity <= 1.0:
            summary["opacity"] = opacity
    layer_types: List[str] = []
    with contextlib.suppress(Exception):
        for index in range(min(symbol.symbolLayerCount(), agent_context.MAX_SYMBOL_LAYERS)):
            sub = symbol.symbolLayer(index)
            with contextlib.suppress(Exception):
                layer_types.append(agent_context.bound_text(sub.layerType(), 64))
    summary["symbol_layer_types"] = layer_types
    return summary


def _renderer_symbols_iterable(renderer: Any) -> Any:
    """Return the renderer's symbols iterable (never a second materialized copy).

    The result is only ever consumed through ``_bounded_symbols`` below, which
    pulls at most ``limit + 1`` items, so a large categorized/rule renderer (or
    a pathological third-party iterable) is never fully walked here.
    """
    from qgis.core import QgsRenderContext

    with contextlib.suppress(Exception):
        result = renderer.symbols(QgsRenderContext())
        if result:
            return result
    return ()


def _bounded_symbols(symbols_iterable: Any, limit: int):
    """Summarize at most ``limit`` symbols, pulling at most ``limit + 1`` items.

    Returns ``(bounded_summaries, truncated)``. Never computes an exact count by
    exhausting an unsized/third-party iterable; the observed bounded length is
    reported by the caller with an explicit truncation flag instead.
    """
    return agent_context.bound_list(
        (_symbol_summary(symbol, limit) for symbol in symbols_iterable), limit
    )


def _vector_style_state(layer: Any, limit: int) -> Dict[str, Any]:
    state: Dict[str, Any] = {}
    renderer = None
    with contextlib.suppress(Exception):
        renderer = layer.renderer()
    if renderer is not None:
        with contextlib.suppress(Exception):
            state["renderer_type"] = agent_context.bound_text(renderer.type(), 64)
        field_names = set()
        with contextlib.suppress(Exception):
            field_names = {field.name() for field in layer.fields()}
        classify = None
        with contextlib.suppress(Exception):
            classify = renderer.classAttribute()
        if isinstance(classify, str) and classify:
            if classify in field_names:
                state["classification_field"] = agent_context.bound_text(classify, 128)
            else:
                state["classification_uses_expression"] = True
        bounded_symbols, symbols_truncated = _bounded_symbols(
            _renderer_symbols_iterable(renderer), limit
        )
        # A bounded *observed* count only; ``symbol_count_is_total`` states
        # whether the renderer had no further symbols beyond the bound.
        state["symbol_count"] = len(bounded_symbols)
        state["symbol_count_is_total"] = not symbols_truncated
        state["symbols"] = bounded_symbols
        state["symbols_truncated"] = symbols_truncated
    with contextlib.suppress(Exception):
        state["opacity"] = float(layer.opacity())
    _vector_label_state(layer, state)
    return state


def _vector_label_state(layer: Any, state: Dict[str, Any]) -> None:
    enabled = False
    with contextlib.suppress(Exception):
        enabled = bool(layer.labelsEnabled())
    state["labeling_enabled"] = enabled
    labeling = None
    with contextlib.suppress(Exception):
        labeling = layer.labeling()
    if labeling is None:
        return
    with contextlib.suppress(Exception):
        state["labeling_type"] = agent_context.bound_text(labeling.type(), 32)
    field_names = set()
    with contextlib.suppress(Exception):
        field_names = {field.name() for field in layer.fields()}
    with contextlib.suppress(Exception):
        settings = labeling.settings()
        if bool(settings.isExpression):
            state["label_expression_present"] = True
        else:
            field_name = settings.fieldName
            if isinstance(field_name, str) and field_name in field_names:
                state["label_field"] = agent_context.bound_text(field_name, 128)
            elif isinstance(field_name, str) and field_name:
                state["label_expression_present"] = True


def _raster_style_state(layer: Any) -> Dict[str, Any]:
    state: Dict[str, Any] = {}
    with contextlib.suppress(Exception):
        state["band_count"] = int(layer.bandCount())
    renderer = None
    with contextlib.suppress(Exception):
        renderer = layer.renderer()
    if renderer is not None:
        with contextlib.suppress(Exception):
            state["renderer_type"] = agent_context.bound_text(renderer.type(), 64)
        with contextlib.suppress(Exception):
            state["opacity"] = float(renderer.opacity())
    return state


def extract_layer_style_state(layer: Any, limit: int) -> Dict[str, Any]:
    """Return the bounded, privacy-preserving safe style summary for ``layer``.

    Never returns a source URI/path, feature/category/rule value or label, or
    any expression text. Uses defensive capability checks so a missing optional
    QGIS 3/4 API yields an omitted field, never a traceback. This is also the
    exact state a ``layer_style`` context token signs.
    """
    kind = _layer_kind(layer)
    state: Dict[str, Any] = {
        "available": True,
        "layer_id": agent_context.bound_text(layer.id(), 128),
        "kind": kind,
        "geometry_type": _layer_geometry_type(layer),
    }
    if isinstance(layer, QgsVectorLayer):
        state.update(_vector_style_state(layer, limit))
    elif isinstance(layer, QgsRasterLayer):
        state.update(_raster_style_state(layer))
    return state


def _tool_layer_style_factory(
    token_service: ContextTokenService,
) -> Callable[[AgentToolCall], Dict[str, Any]]:
    def _handler(call: AgentToolCall) -> Dict[str, Any]:
        layer_id = call.arguments.get("layer_id")
        if not isinstance(layer_id, str) or not layer_id.strip():
            raise ToolExecutionError("layer_id must be a non-empty string.")
        limit = _clamp_limit(call.arguments.get("limit"))
        project = QgsProject.instance()
        layer = project.mapLayer(layer_id) if project is not None else None
        if layer is None:
            return {"available": False, "layer_id": agent_context.bound_text(layer_id, 128)}
        state = extract_layer_style_state(layer, limit)
        state["context_token"] = token_service.issue(
            STYLE_PROPOSAL_KIND, layer.id(), extract_layer_style_state(layer, STYLE_STATE_LIMIT)
        )
        return state

    return _handler


# -- plugin.describe -------------------------------------------------------


def _netloc_has_empty_port(netloc: str) -> bool:
    """Return whether ``netloc`` carries a port separator with no port digits.

    ``urlsplit(...).port`` returns ``None`` (no error) for a trailing ``host:``
    with an empty port, so that malformed authority must be caught explicitly.
    Colons inside an IPv6 literal ``[...]`` are not port separators.
    """
    if netloc.startswith("["):
        close = netloc.find("]")
        if close == -1:
            return False
        return netloc[close + 1:] == ":"
    return netloc.endswith(":") and ":" in netloc


# A single ordinary DNS label: 1..63 characters, only ASCII letters/digits and
# internal hyphens, with no leading or trailing hyphen and no empty label.
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def _canonical_ascii_host(host: str) -> Optional[str]:
    """Return the single canonical ASCII form of ``host``, else ``None``.

    This is the one canonicalization step, run *before* any authority policy so
    that a Unicode hostname can never be classified on its raw form and then
    silently transformed into a different (e.g. local or loopback) ASCII target.
    The optional DNS root dot is removed deterministically, then the host is
    IDNA-encoded to ASCII: an ASCII host passes through unchanged, while a
    Unicode host is NFKC-normalized and punycode-encoded. This is fully local --
    no DNS lookup and no network access. All IP/local-suffix/DNS-label policy is
    applied by the caller to this returned canonical ASCII host.
    """
    canonical = host.lower().rstrip(".")
    if not canonical:  # a host that is nothing but dots
        return None
    try:
        canonical = canonical.encode("idna").decode("ascii").lower()
    except (UnicodeError, ValueError):
        return None
    canonical = canonical.rstrip(".")
    if not canonical or len(canonical) > 253:
        return None
    return canonical


def _is_local_ascii_host(host: str) -> bool:
    """Whether the canonical ASCII ``host`` is a clearly local / non-public name."""
    return (
        host == "localhost"
        or host.endswith(".localhost")
        or host.endswith(".local")
        or host.endswith(".internal")
        or "." not in host  # a clearly local single-label hostname
    )


def _is_valid_dns_host(host: str) -> bool:
    """Whether the canonical ASCII ``host`` is a valid multi-label DNS name.

    Rejects empty labels, leading/trailing hyphens and any character outside the
    ordinary DNS host set. Operates only on an already-canonical ASCII host.

    A host whose **rightmost label is all digits** is also rejected. Two reasons,
    and the second is the one that matters: no real top-level domain is numeric
    (RFC 1123 / RFC 3696), and -- found by the Phase 07 fuzzer -- the abbreviated
    IPv4 forms browsers and ``inet_aton`` still accept, such as ``127.1`` for
    ``127.0.0.1`` or ``10.1`` for ``10.0.0.1``, are *not* parsed by
    ``ipaddress.ip_address``. Without this rule they miss the IP branch entirely
    and are then waved through as ordinary two-label DNS names, so a local or
    private address could be surfaced as a public link.
    """
    labels = host.split(".")
    if len(labels) < 2:  # an ordinary public URL host is never a single label
        return False
    if labels[-1].isdigit():
        return False
    return all(_DNS_LABEL.match(label) for label in labels)


def _validate_public_url(value: Any) -> str:
    """Return a bounded, ordinary public http(s) documentation URL, else "".

    Rejects any userinfo (even empty ``@``), control/whitespace characters,
    backslashes, malformed ports and non-http(s) schemes. The host is reduced to
    a single canonical ASCII form *first* (root dot removed, IDNA-encoded), and
    only then classified: an IP literal must not be loopback/private/link-local/
    reserved/multicast/unspecified, a ``localhost``/``.localhost``/``.local``/
    ``.internal``/single-label name is rejected, and any other host must be a
    valid multi-label DNS name. Because classification runs on the canonical
    ASCII host, a Unicode host that IDNA-maps to a local/loopback target cannot
    slip through. The returned authority is reconstructed from that canonical
    host, never the raw ``netloc``; the query and fragment are dropped and the
    URL is never fetched.
    """
    import ipaddress
    from urllib.parse import urlsplit, urlunsplit

    if not isinstance(value, str) or not value or len(value) > 500:
        return ""
    if any(ord(char) < 0x20 or ord(char) == 0x7F or char.isspace() for char in value):
        return ""
    if "\\" in value:  # backslashes never appear in an ordinary public http(s) URL
        return ""
    try:
        parts = urlsplit(value)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https"):
        return ""
    if "@" in parts.netloc:  # any userinfo component, even an empty one
        return ""
    # ``urlsplit`` defers port validation until ``.port`` is read: force it here
    # so a non-numeric or out-of-range port raises and is rejected. Do not expose
    # the ``ValueError``.
    try:
        port = parts.port
    except ValueError:
        return ""
    if _netloc_has_empty_port(parts.netloc):  # a stray ``host:`` with no port
        return ""
    if not parts.hostname:
        return ""
    # Step 1 -- canonicalize once, before any authority decision.
    canonical = _canonical_ascii_host(parts.hostname)
    if canonical is None:
        return ""
    # Step 2 -- classify the canonical ASCII host and reconstruct its authority.
    try:
        address = ipaddress.ip_address(canonical)
    except ValueError:
        if _is_local_ascii_host(canonical) or not _is_valid_dns_host(canonical):
            return ""
        authority = canonical if port is None else f"{canonical}:{port}"
    else:
        if (
            address.is_loopback
            or address.is_private
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return ""
        literal = address.compressed
        if address.version == 6:
            literal = f"[{literal}]"
        authority = literal if port is None else f"{literal}:{port}"
    # Reconstruct from the validated canonical authority (never the raw netloc);
    # drop query and fragment so credential-like query material can never leak.
    cleaned = urlunsplit((parts.scheme, authority, parts.path, "", ""))
    if not cleaned or len(cleaned) > 500 or "@" in cleaned:
        return ""
    return cleaned


def _plugin_union(qgis_utils: Any) -> set:
    available = set(getattr(qgis_utils, "available_plugins", []) or [])
    active = set(getattr(qgis_utils, "active_plugins", []) or [])
    loaded = set(getattr(qgis_utils, "plugins", {}) or {})
    return available | active | loaded


def resolve_plugin_package(qgis_utils: Any, requested_name: str) -> str:
    """Resolve a package id, visible plugin name, or specific visible-name alias.

    QGIS' registry is keyed by Python package names (for example ``zero2viz``),
    while users naturally quote the metadata name (for example
    ``02viz - Geospatial Visualization Studio``). Resolution is conservative:
    an exact package wins, and a metadata/alias match is accepted only when it
    identifies exactly one installed package.
    """
    requested = str(requested_name or "").strip()
    packages = sorted(_plugin_union(qgis_utils))
    if requested in packages:
        return requested
    folded = requested.casefold()
    exact_packages = [name for name in packages if name.casefold() == folded]
    if len(exact_packages) == 1:
        return exact_packages[0]
    normalized = re.sub(r"[^a-z0-9]+", "", folded)
    requested_tokens = set(re.findall(r"[a-z0-9]+", folded))
    if len(normalized) < 5:
        return ""

    # These plugins ship as package `zero2x` with the visible name `02x`, so a
    # user quoting either spelling means the same plugin. Without this, asking
    # for "zero2agentosm" matched nothing: the package normalizes to
    # "zero2agentosmdownloader" and the visible name to "02agentosmdownloader",
    # and neither contains the other.
    def _prefix_variants(text: str) -> Tuple[str, ...]:
        variants = {text}
        if text.startswith("zero2"):
            variants.add("02" + text[len("zero2"):])
        elif text.startswith("02"):
            variants.add("zero2" + text[len("02"):])
        return tuple(variants)

    normalized_forms = _prefix_variants(normalized)
    exact = []
    aliases = []
    for package in packages:
        display = ""
        with contextlib.suppress(Exception):
            display = str(qgis_utils.pluginMetadata(package, "name") or "")
        display_norm = re.sub(r"[^a-z0-9]+", "", display.casefold())
        package_norm = re.sub(r"[^a-z0-9]+", "", package.casefold())
        candidates = set(normalized_forms)
        if any(form in (display_norm, package_norm) for form in candidates):
            exact.append(package)
        elif len(normalized) >= 6 and any(
            form and (form in display_norm or form in package_norm)
            for form in candidates
        ):
            aliases.append(package)
        elif (
            len(normalized) >= 6
            and requested_tokens
            and requested_tokens.issubset(
                set(re.findall(r"[a-z0-9]+", display.casefold()))
            )
        ):
            aliases.append(package)
    matches = exact or aliases
    return matches[0] if len(matches) == 1 else ""


def build_plugin_describe(qgis_utils: Any, package_name: str) -> Dict[str, Any]:
    """Return bounded, allowlisted metadata for one installed plugin.

    Resolves the package only against the same union ``plugin.list`` uses and
    never imports, instantiates, or invokes the plugin, reads its files, or
    follows any URL. Plugin metadata is untrusted data and is bounded here
    before serialization.
    """
    resolved = resolve_plugin_package(qgis_utils, package_name)
    if not resolved:
        return {"available": False, "package_name": agent_context.bound_text(package_name, 128)}
    package_name = resolved
    active = set(getattr(qgis_utils, "active_plugins", []) or [])

    def _meta(key: str) -> str:
        value = ""
        with contextlib.suppress(Exception):
            value = str(qgis_utils.pluginMetadata(package_name, key) or "")
        return value

    provider_flag = _meta("hasProcessingProvider").strip().lower() == "yes"
    return {
        "available": True,
        "package_name": agent_context.bound_text(package_name, 128),
        "display_name": agent_context.bound_text(_meta("name") or package_name, agent_context.MAX_DISPLAY_NAME),
        "version": agent_context.bound_text(_meta("version"), agent_context.MAX_SHORT_TEXT),
        "enabled": package_name in active,
        "has_processing_provider": provider_flag,
        "description": agent_context.bound_text(_meta("description"), 500),
        "about": agent_context.bound_text(_meta("about"), agent_context.MAX_ABOUT_TEXT),
        "category": agent_context.bound_text(_meta("category"), agent_context.MAX_SHORT_TEXT),
        "qgis_minimum_version": agent_context.bound_text(_meta("qgisMinimumVersion"), 32),
        "qgis_maximum_version": agent_context.bound_text(_meta("qgisMaximumVersion"), 32),
        "homepage": _validate_public_url(_meta("homepage")),
        "repository": _validate_public_url(_meta("repository")),
        "tracker": _validate_public_url(_meta("tracker")),
    }


def build_plugin_view(qgis_utils: Any, package_name: str) -> Optional[PluginView]:
    """Bounded metadata for one plugin, or ``None`` when it is not installed.

    Reads only the plugin *name union* and QGIS' own ``pluginMetadata`` API. It
    never touches ``qgis.utils.plugins[name]`` -- the loaded plugin *instance* --
    because even reading an attribute off it can execute third-party code.
    """
    resolved = resolve_plugin_package(qgis_utils, package_name)
    if not resolved:
        return None
    package_name = resolved
    active = set(getattr(qgis_utils, "active_plugins", []) or [])

    def _meta(key: str) -> str:
        value = ""
        with contextlib.suppress(Exception):
            value = str(qgis_utils.pluginMetadata(package_name, key) or "")
        return value

    return PluginView(
        package_name=package_name,
        display_name=_meta("name") or package_name,
        version=_meta("version"),
        enabled=package_name in active,
        declares_processing_provider=_meta("hasProcessingProvider").strip().lower() == "yes",
        installed=True,
    )


def build_provider_views(
    registry: Any, *, with_algorithms: bool = True, for_package: str = ""
) -> List[ProviderView]:
    """Adapt every live Processing provider into a QGIS-free view.

    ``owning_package`` comes from ``type(provider).__module__`` -- the Python
    package that defined the provider class. QGIS already constructed and holds
    these objects, and reading a class's ``__module__`` executes no plugin code,
    so this is the one way to *prove* a plugin-to-provider mapping without ever
    asking the plugin.

    Phase 07 (§9.4) makes this two-pass. ``build_capabilities`` lists algorithms
    only for a provider whose owning package **equals** the requested one; every
    other provider contributes identity alone. Passing ``for_package`` therefore
    enumerates algorithms for at most those providers instead of for all of them,
    which matters on a profile with many plugins installed. The returned report is
    byte-for-byte identical either way -- ``for_package`` is a work filter, never
    a visibility filter: every provider is still returned and still eligible to be
    reported as a candidate.
    """
    views: List[ProviderView] = []
    if registry is None:
        return views
    providers = []
    with contextlib.suppress(Exception):
        providers = list(registry.providers())
    wanted = str(for_package or "")
    for provider in providers:
        owning = ""
        with contextlib.suppress(Exception):
            owning = str(type(provider).__module__ or "").split(".")[0]
        provider_id = ""
        with contextlib.suppress(Exception):
            provider_id = str(provider.id() or "")
        name = ""
        with contextlib.suppress(Exception):
            name = str(provider.name() or "")
        algorithms: List[tuple] = []
        if with_algorithms and (not wanted or owning == wanted):
            with contextlib.suppress(Exception):
                for algorithm in list(provider.algorithms())[:MAX_ALGORITHMS * 2]:
                    algorithms.append(
                        (str(algorithm.id()), str(algorithm.displayName()), str(algorithm.group()))
                    )
        views.append(
            ProviderView(
                provider_id=provider_id,
                name=name,
                owning_package=owning,
                algorithms=tuple(algorithms),
            )
        )
    return views


def _tool_plugin_capabilities(
    call: AgentToolCall,
    token_service: Optional[ContextTokenService] = None,
) -> Dict[str, Any]:
    package_name = call.arguments.get("package_name")
    if not isinstance(package_name, str) or not package_name.strip():
        raise ToolExecutionError("package_name must be a non-empty string.")
    limit = _clamp_limit(call.arguments.get("limit"))
    try:
        import qgis.utils as qgis_utils
    except ImportError as error:
        raise ToolExecutionError("Plugin registry is unavailable.") from error
    from ..algorithm_catalog import AlgorithmCatalog

    plugin = build_plugin_view(qgis_utils, package_name)
    if plugin is None:
        return build_capabilities(
            PluginView(package_name=package_name, installed=False), (), limit=limit
        )
    providers = build_provider_views(
        QgsApplication.processingRegistry(), for_package=plugin.package_name
    )
    result = build_capabilities(
        plugin,
        providers,
        limit=limit,
        algorithm_allowed=AlgorithmCatalog.ai_algorithm_allowed,
    )
    actions = public_plugin_actions(plugin.package_name)
    if actions:
        loaded = plugin.package_name in set(
            getattr(qgis_utils, "plugins", {}) or {}
        )
        ready = bool(plugin.enabled and loaded)
        state = plugin_capability_state(
            plugin.package_name, plugin.version, plugin.enabled, loaded
        )
        result["agent_actions"] = actions
        result["agent_executable"] = ready
        result["guidance"] = (
            "This plugin has explicitly reviewed Agent actions listed below. "
            "They require a separate plugin_action approval card."
            if ready
            else "This plugin has reviewed Agent actions, but it must be enabled "
            "and loaded before they can be proposed."
        )
        if ready and token_service is not None:
            result["context_token"] = token_service.issue(
                PLUGIN_ACTION_KIND, plugin.package_name, state
            )
    else:
        result["agent_actions"] = []
    return result


def _tool_plugin_capabilities_factory(
    token_service: ContextTokenService,
) -> Callable[[AgentToolCall], Dict[str, Any]]:
    return lambda call: _tool_plugin_capabilities(call, token_service)


def _tool_plugin_describe(call: AgentToolCall) -> Dict[str, Any]:
    package_name = call.arguments.get("package_name")
    if not isinstance(package_name, str) or not package_name.strip():
        raise ToolExecutionError("package_name must be a non-empty string.")
    try:
        import qgis.utils as qgis_utils
    except ImportError as error:
        raise ToolExecutionError("Plugin registry is unavailable.") from error
    return build_plugin_describe(qgis_utils, package_name)


def _workspace_manager(root_provider: Optional[WorkspaceRootProvider]) -> WorkspaceManager:
    if root_provider is None:
        raise ToolExecutionError("Developer workspace access is unavailable.")
    try:
        return WorkspaceManager(root_provider())
    except WorkspaceError as error:
        raise ToolExecutionError(str(error)) from error


def _tool_workspace_list_factory(root_provider: Optional[WorkspaceRootProvider]):
    def handler(call: AgentToolCall) -> Dict[str, Any]:
        try:
            return _workspace_manager(root_provider).list(call.arguments.get("path", ""))
        except WorkspaceError as error:
            raise ToolExecutionError(str(error)) from error

    return handler


def _tool_workspace_read_factory(
    root_provider: Optional[WorkspaceRootProvider], token_service: ContextTokenService
):
    def handler(call: AgentToolCall) -> Dict[str, Any]:
        try:
            manager = _workspace_manager(root_provider)
            result = manager.read(call.arguments["path"], call.arguments.get("max_chars", 120_000))
            result["context_token"] = token_service.issue(
                "workspace_patch", manager.workspace_id, result["context_state"]
            )
            result.pop("context_state", None)
            return result
        except (WorkspaceError, KeyError) as error:
            raise ToolExecutionError(str(error)) from error

    return handler


def _tool_workspace_inspect_factory(
    root_provider: Optional[WorkspaceRootProvider], token_service: ContextTokenService
):
    def handler(call: AgentToolCall) -> Dict[str, Any]:
        try:
            manager = _workspace_manager(root_provider)
            raw_paths = str(call.arguments.get("paths", ""))
            paths = tuple(item.strip() for item in raw_paths.split(",") if item.strip())
            if not paths or len(paths) > 12:
                raise WorkspaceError("Provide between one and twelve workspace paths.")
            state = manager.state(paths)
            return {
                "workspace_id": manager.workspace_id,
                "files": state["files"],
                "context_token": token_service.issue(
                    "workspace_patch", manager.workspace_id, state
                ),
            }
        except WorkspaceError as error:
            raise ToolExecutionError(str(error)) from error

    return handler


def _tool_workspace_search_factory(root_provider: Optional[WorkspaceRootProvider]):
    def handler(call: AgentToolCall) -> Dict[str, Any]:
        try:
            return _workspace_manager(root_provider).search(
                call.arguments["query"], call.arguments.get("path", "")
            )
        except (WorkspaceError, KeyError) as error:
            raise ToolExecutionError(str(error)) from error

    return handler


def _tool_workspace_command_factory(root_provider: Optional[WorkspaceRootProvider]):
    def handler(call: AgentToolCall) -> Dict[str, Any]:
        try:
            return _workspace_manager(root_provider).command(call.arguments["command"])
        except (WorkspaceError, KeyError) as error:
            raise ToolExecutionError(str(error)) from error

    return handler


def build_default_registry(
    model_provider: ModelProvider,
    token_service: Optional[ContextTokenService] = None,
    active_layer_provider: Optional[ActiveLayerProvider] = None,
    power_enabled_provider: Optional[Callable[[], bool]] = None,
    power_resources: Optional[Any] = None,
    script_library: Optional[Any] = None,
    workspace_root_provider: Optional[WorkspaceRootProvider] = None,
) -> AgentToolRegistry:
    """Build the capability-routed Agent Workspace registry.

    ``token_service`` issues the opaque freshness tokens for ``model.describe``,
    ``layer.style`` and ``processing.describe``; the dock passes the same
    instance to the runtime proposal validator so tokens can be verified. When
    omitted a fresh service is created (useful for isolated tool tests).
    """
    token_service = token_service or ContextTokenService()
    power_enabled_provider = power_enabled_provider or (lambda: False)
    if power_resources is None:
        class _NullPowerResources:
            def issue(self, *_args) -> str:
                return ""

        power_resources = _NullPowerResources()
    if script_library is None:
        class _NullScriptLibrary:
            def list(self):
                return ()

            def get(self, _script_id):
                raise ValueError("Power Mode script library is unavailable.")

        script_library = _NullScriptLibrary()
    registry = AgentToolRegistry()

    def _require_power() -> None:
        if not bool(power_enabled_provider()):
            raise ToolExecutionError("Power Mode is disabled.")

    def _database_list(_call: AgentToolCall) -> Dict[str, Any]:
        _require_power()
        from .power_mode import database_connections

        items = database_connections(power_resources)
        return {"connections": items, "count": len(items), "truncated": False}

    def _database_describe(call: AgentToolCall) -> Dict[str, Any]:
        _require_power()
        from .power_mode import describe_database

        return describe_database(
            power_resources,
            str(call.arguments.get("connection_token", "")),
            _clamp_limit(call.arguments.get("limit")),
            schema_name=str(call.arguments.get("schema", ""))[:128],
            table_name=str(call.arguments.get("table", ""))[:160],
        )

    def _script_list(_call: AgentToolCall) -> Dict[str, Any]:
        _require_power()
        items = [
            {
                "script_id": item.script_id,
                "name": item.name,
                "description": item.description,
                "script_hash": item.script_hash,
            }
            for item in script_library.list()[:50]
        ]
        return {
            "scripts": items,
            "count": len(items),
            "truncated": False,
            "generated_context_token": power_resources.issue(
                "python", "python", "generated", "Generated PyQGIS"
            ),
        }

    def _script_describe(call: AgentToolCall) -> Dict[str, Any]:
        _require_power()
        try:
            item = script_library.get(str(call.arguments.get("script_id", "")))
        except ValueError:
            return {"available": False}
        resource_token = power_resources.issue(
            "script", "python", item.script_id, item.name
        )
        public_parameters = []
        for name, contract in list(item.parameters.items())[:50]:
            row = {"name": str(name)[:100]}
            if isinstance(contract, dict):
                row["type"] = str(contract.get("type", "value"))[:40]
                row["description"] = str(contract.get("description", ""))[:300]
                row["required"] = bool(contract.get("required", False))
            else:
                row["type"] = "value"
                row["description"] = ""
                row["required"] = False
            public_parameters.append(row)
        return {
            "available": True,
            "context_token": resource_token,
            "script_id": item.script_id,
            "name": item.name,
            "description": item.description,
            "script_hash": item.script_hash,
            "parameters": public_parameters,
            "execution_modes": ["subprocess", "live"],
        }

    registry.register(
        AgentToolSpec(
            name="project.summary",
            title="Project summary",
            description=(
                "Returns the project title, CRS, and layer count. The saved "
                "project path is never included."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(),
            allowed_scopes=tuple(AgentScope.ALL),
        ),
        _tool_project_summary,
    )
    registry.register(
        AgentToolSpec(
            name="layer.list",
            title="List layers",
            description=(
                "Lists project layers with id, bounded name, kind, geometry "
                "type, CRS, visibility, provider key, and an exact active-layer "
                "marker. The active layer is returned first when available."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema({"limit": _LIMIT_PROPERTY}),
            allowed_scopes=(AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
        ),
        _tool_layer_list_factory(active_layer_provider),
    )
    registry.register(
        AgentToolSpec(
            name="layer.describe",
            title="Describe layer",
            description=(
                "Describes one layer by id: field names, broad field types, "
                "and how many features it holds. Never returns a source URI or "
                "an individual feature. An optional exact field_name filter "
                "checks the complete live schema without returning unrelated "
                "fields."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "layer_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _ID_MAX_LENGTH,
                    },
                    "field_name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "limit": _LIMIT_PROPERTY,
                },
                required=["layer_id"],
            ),
            allowed_scopes=(AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
        ),
        _tool_layer_describe,
    )
    registry.register(
        AgentToolSpec(
            name="layer.field_values",
            title="Read field values",
            description=(
                "Statistics and a bounded value sample for ONE named field of "
                "one layer: minimum, maximum, mean, median, null count, "
                "distinct count, and up to 50 values. Ordering statistics "
                "appear only when every value is numeric. Call this before "
                "concluding that a filter legitimately matched nothing, and "
                "after any run whose result looks empty or surprising -- a "
                "count alone cannot tell a correct empty result from a wrong "
                "one. Never returns a feature, a row, or a source URI."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "layer_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _ID_MAX_LENGTH,
                    },
                    "field_name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "limit": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": agent_context.MAX_VALUE_SAMPLE,
                    },
                },
                required=["layer_id", "field_name"],
            ),
            allowed_scopes=(AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
        ),
        _tool_field_values,
    )
    registry.register(
        AgentToolSpec(
            name="layer.suggest_crs",
            title="Suggest a metric CRS",
            description=(
                "Metric CRS candidates whose area of use actually contains one "
                "layer: its UTM zone, the project CRS, CRSs used by other "
                "project layers, and recently used ones. Use this whenever the "
                "user asks for a 'local', 'metric' or 'projected' CRS without "
                "naming one, and before any $area/$length calculation on a "
                "layer whose area_safe_crs is false. Never invent an "
                "AUTHORITY:CODE; every candidate returned here is live."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "layer_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _ID_MAX_LENGTH,
                    }
                },
                required=["layer_id"],
            ),
            allowed_scopes=(AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
        ),
        _tool_suggest_crs,
    )
    registry.register(
        AgentToolSpec(
            name="processing.resolve",
            title="Resolve Processing operation",
            description=(
                "Resolves an exact algorithm id, or searches a short operation "
                "name and describes it when unambiguous. Returns one live typed "
                "signature and freshness receipt without execution. An optional "
                "small limit keeps large signatures within the Agent context "
                "while preserving required and extent bindings."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "query": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": _QUERY_MAX_LENGTH,
                    },
                    "algorithm_id": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": _ID_MAX_LENGTH,
                    },
                    "limit": _LIMIT_PROPERTY,
                }
            ),
            allowed_scopes=(
                AgentScope.PROJECT,
                AgentScope.ACTIVE_LAYER,
                AgentScope.CURRENT_MODEL,
            ),
        ),
        _tool_processing_resolve_factory(token_service),
    )
    registry.register(
        AgentToolSpec(
            name="processing.search",
            title="Search Processing algorithms",
            description=(
                "Searches the installed Processing registry with a bounded "
                "query; never runs an algorithm."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "query": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": _QUERY_MAX_LENGTH,
                    },
                    "limit": _LIMIT_PROPERTY,
                }
            ),
            # Also reachable in active-layer scope: a processing_run proposal is
            # valid in that scope, so the read-only search/describe pair that
            # prepares one must be reachable there too. No new data is exposed.
            allowed_scopes=(
                AgentScope.PROJECT,
                AgentScope.ACTIVE_LAYER,
                AgentScope.CURRENT_MODEL,
            ),
        ),
        _tool_processing_search,
    )
    registry.register(
        AgentToolSpec(
            name="processing.describe",
            title="Describe Processing algorithm",
            description=(
                "Describes one installed Processing algorithm's id, title, "
                "group, and parameter names only, plus a freshness receipt for "
                "a later run proposal. Never runs an algorithm."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "algorithm_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _ID_MAX_LENGTH,
                    },
                    "limit": _LIMIT_PROPERTY,
                },
                required=["algorithm_id"],
            ),
            allowed_scopes=(
                AgentScope.PROJECT,
                AgentScope.ACTIVE_LAYER,
                AgentScope.CURRENT_MODEL,
            ),
        ),
        _tool_processing_describe_factory(token_service),
    )
    registry.register(
        AgentToolSpec(
            name="expression.search",
            title="Search QGIS expression functions",
            description=(
                "Searches the live built-in QGIS expression function catalog "
                "and returns bounded QGIS help text. It excludes custom, "
                "dynamic, environment and filesystem functions and never "
                "evaluates an expression or reads feature values."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "query": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _QUERY_MAX_LENGTH,
                    },
                    "limit": _LIMIT_PROPERTY,
                },
                required=["query"],
            ),
            allowed_scopes=(
                AgentScope.PROJECT,
                AgentScope.ACTIVE_LAYER,
                AgentScope.CURRENT_MODEL,
            ),
        ),
        _tool_expression_search,
    )
    registry.register(
        AgentToolSpec(
            name="model.summary",
            title="Current model summary",
            description=(
                "Summarizes the open SmartModeler graph, or reports that no "
                "model is currently open."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema({"limit": _LIMIT_PROPERTY}),
            allowed_scopes=(AgentScope.CURRENT_MODEL,),
        ),
        _tool_model_summary_factory(model_provider),
    )
    registry.register(
        AgentToolSpec(
            name="model.validate",
            title="Validate current model",
            description=(
                "Returns bounded validation issue summaries for the open "
                "SmartModeler graph."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema({"limit": _LIMIT_PROPERTY}),
            allowed_scopes=(AgentScope.CURRENT_MODEL,),
        ),
        _tool_model_validate_factory(model_provider),
    )
    registry.register(
        AgentToolSpec(
            name="plugin.list",
            title="List plugins",
            description=(
                "Lists installed plugins (active or not) with package name, "
                "display name, version, enabled state, and Processing-"
                "provider flag. The detailed rows are bounded and alphabetical; "
                "`installed_packages` names every installed package. Decide "
                "whether a plugin is installed from `installed_packages`, never "
                "from the bounded rows, and use plugin.describe or "
                "plugin.capabilities to confirm one by name."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema({"limit": _LIMIT_PROPERTY}),
            allowed_scopes=(AgentScope.PLUGINS,),
        ),
        _tool_plugin_list,
    )
    registry.register(
        AgentToolSpec(
            name="layer.style",
            title="Describe layer style",
            description=(
                "Summarizes one layer's renderer and labeling: family, opacity, "
                "bounded symbol colors/types, and whether a classification/label "
                "uses a field or an expression. Never returns a source, feature "
                "value, category value, or expression text."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "layer_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _ID_MAX_LENGTH,
                    },
                    "limit": _LIMIT_PROPERTY,
                },
                required=["layer_id"],
            ),
            allowed_scopes=(AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
        ),
        _tool_layer_style_factory(token_service),
    )
    registry.register(
        AgentToolSpec(
            name="model.describe",
            title="Describe current model topology",
            description=(
                "Returns the open SmartModeler graph's safe topology: node "
                "ids/titles/algorithm ids, port structure, edges, and live "
                "validation issues. Never returns baseline parameter values, "
                "outputs, or file paths."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema({"limit": _LIMIT_PROPERTY}),
            allowed_scopes=(AgentScope.CURRENT_MODEL,),
        ),
        _tool_model_describe_factory(model_provider, token_service),
    )
    registry.register(
        AgentToolSpec(
            name="plugin.describe",
            title="Describe installed plugin",
            description=(
                "Returns bounded local help metadata for one installed plugin "
                "(name, version, enabled state, description, about, validated "
                "public URLs). Never imports, invokes, or reads plugin files."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "package_name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _PACKAGE_MAX_LENGTH,
                    }
                },
                required=["package_name"],
            ),
            allowed_scopes=(AgentScope.PLUGINS,),
        ),
        _tool_plugin_describe,
    )
    registry.register(
        AgentToolSpec(
            name="plugin.capabilities",
            title="Plugin capabilities",
            description=(
                "Reports what one installed plugin can actually be used for: "
                "its live Processing provider(s) when that can be proved from "
                "the provider registry, a bounded list of their algorithms, and "
                "an honest status when no reliable mapping exists. Never "
                "imports, instantiates, or calls the plugin. It also lists any "
                "application-reviewed Agent adapter without invoking it, and "
                "never claims an unproved mapping."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "package_name": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _PACKAGE_MAX_LENGTH,
                    },
                    "limit": _LIMIT_PROPERTY,
                },
                required=["package_name"],
            ),
            allowed_scopes=(AgentScope.PLUGINS, AgentScope.PROJECT),
        ),
        _tool_plugin_capabilities_factory(token_service),
    )
    registry.register(
        AgentToolSpec(
            name="database.list",
            title="List database connections",
            description=(
                "Power Mode only. Lists stored PostGIS and GeoPackage connection "
                "names through opaque tokens; never returns URIs or credentials."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(),
            allowed_scopes=(AgentScope.PROJECT,),
        ),
        _database_list,
    )
    registry.register(
        AgentToolSpec(
            name="database.describe",
            title="Describe database",
            description=(
                "Power Mode only. Returns bounded schema/table names, optional "
                "column names/types for one selected table, and a fresh opaque "
                "SQL proposal token without rows, URI or credentials."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "connection_token": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                    },
                    "limit": _LIMIT_PROPERTY,
                    "schema": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": 128,
                    },
                    "table": {
                        "type": "string",
                        "minLength": 0,
                        "maxLength": 160,
                    },
                },
                required=["connection_token"],
            ),
            allowed_scopes=(AgentScope.PROJECT,),
        ),
        _database_describe,
    )
    registry.register(
        AgentToolSpec(
            name="script.list",
            title="List trusted scripts",
            description=(
                "Power Mode only. Lists managed hash-pinned scripts and their "
                "public parameter contracts; never returns source or paths."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(),
            allowed_scopes=(AgentScope.PROJECT,),
        ),
        _script_list,
    )
    registry.register(
        AgentToolSpec(
            name="script.describe",
            title="Describe trusted script",
            description=(
                "Power Mode only. Describes one managed script and issues a "
                "short-lived run receipt; never returns source or file paths."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "script_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    }
                },
                required=["script_id"],
            ),
            allowed_scopes=(AgentScope.PROJECT,),
        ),
        _script_describe,
    )
    registry.register(
        AgentToolSpec(
            name="workspace.list",
            title="List workspace files",
            description=(
                "Developer scope only. Lists source files below the current "
                "SmartModeler plugin root; hidden build, virtual-environment, "
                "and repository internals are excluded."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {"path": {"type": "string", "minLength": 0, "maxLength": 256}}
            ),
            allowed_scopes=(AgentScope.WORKSPACE,),
        ),
        _tool_workspace_list_factory(workspace_root_provider),
    )
    registry.register(
        AgentToolSpec(
            name="workspace.read",
            title="Read workspace file",
            description=(
                "Developer scope only. Reads bounded UTF-8 source text and "
                "returns a freshness receipt for a later reviewed patch."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "path": {"type": "string", "minLength": 1, "maxLength": 256},
                    "max_chars": {"type": "integer", "minimum": 1, "maximum": 120000},
                },
                required=["path"],
            ),
            allowed_scopes=(AgentScope.WORKSPACE,),
        ),
        _tool_workspace_read_factory(workspace_root_provider, token_service),
    )
    registry.register(
        AgentToolSpec(
            name="workspace.inspect",
            title="Inspect workspace state",
            description=(
                "Developer scope only. Returns file existence and digests for "
                "one or more comma-separated source paths plus a freshness "
                "receipt used to validate a patch."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {"paths": {"type": "string", "minLength": 1, "maxLength": 2000}},
                required=["paths"],
            ),
            allowed_scopes=(AgentScope.WORKSPACE,),
        ),
        _tool_workspace_inspect_factory(workspace_root_provider, token_service),
    )
    registry.register(
        AgentToolSpec(
            name="workspace.search",
            title="Search workspace source",
            description=(
                "Developer scope only. Searches UTF-8 source files below the "
                "plugin root with bounded paths, line text, and match count."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "query": {"type": "string", "minLength": 1, "maxLength": 200},
                    "path": {"type": "string", "minLength": 0, "maxLength": 256},
                },
                required=["query"],
            ),
            allowed_scopes=(AgentScope.WORKSPACE,),
        ),
        _tool_workspace_search_factory(workspace_root_provider),
    )
    registry.register(
        AgentToolSpec(
            name="workspace.command",
            title="Run safe workspace command",
            description=(
                "Developer scope only. Runs one fixed diagnostic command from "
                "the allowlist (git status, git diff summary, or pytest). No "
                "shell syntax, arbitrary executable, path, or network command "
                "is accepted."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "command": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 32,
                    }
                },
                required=["command"],
            ),
            allowed_scopes=(AgentScope.WORKSPACE,),
        ),
        _tool_workspace_command_factory(workspace_root_provider),
    )
    return registry
