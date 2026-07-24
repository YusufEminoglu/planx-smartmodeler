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
import re
from typing import Any, Callable, Dict, Iterator, List, Optional

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsFeatureRequest,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from . import context as agent_context
from .context_tokens import ContextTokenService
from .contracts import AgentRisk, AgentScope, AgentToolCall, AgentToolSpec
from .registry import AgentToolRegistry

# Returns the live SmartModeler graph (duck-typed: .name, .nodes, .edges,
# .validate()) or None when no studio/model is open. Implemented as a
# callback owned by the plugin so the registry never holds a stale copy.
ModelProvider = Callable[[], Optional[Any]]

DEFAULT_LIST_LIMIT = agent_context.DEFAULT_LIST_LIMIT
MAX_LIST_LIMIT = agent_context.MAX_LIST_ITEMS

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
from .safe_algorithm_policy import ParamSpec, default_policy  # noqa: E402 - same grouping
from .plugin_capabilities import (  # noqa: E402 - same grouping
    MAX_ALGORITHMS,
    PluginView,
    ProviderView,
    build_capabilities,
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


def _clamp_limit(value: Any) -> int:
    """Defensive fallback: the controller already enforces ``_LIMIT_PROPERTY``
    (an integer between 1 and ``MAX_LIST_LIMIT``) via each tool's schema
    before a handler is ever invoked; this only guards direct handler calls."""
    if value is None:
        return DEFAULT_LIST_LIMIT
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


def _layer_summary(layer: Any) -> agent_context.LayerSummary:
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


def _tool_layer_list(call: AgentToolCall) -> Dict[str, Any]:
    limit = _clamp_limit(call.arguments.get("limit"))
    project = QgsProject.instance()
    layers = project.mapLayers().values() if project is not None else ()
    summaries = (_layer_summary(layer) for layer in layers)
    return agent_context.build_layer_list(summaries, limit)


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
        fields = (
            agent_context.FieldSummary(field_def.name(), field_def.typeName())
            for field_def in layer.fields()
        )
        with contextlib.suppress(Exception):
            feature_count = layer.featureCount()
    result = agent_context.build_layer_description(
        _layer_summary(layer), fields, limit, feature_count=feature_count
    )
    result["available"] = True
    return result


def _tool_layer_field_values(call: AgentToolCall) -> Dict[str, Any]:
    """Aggregate one attribute into distinct values and their counts.

    Bounded twice over: at most ``MAX_FIELD_VALUES`` distinct values are ever
    returned, and the scan stops after ``MAX_SCAN_FEATURES`` records so a
    multi-million-feature layer cannot freeze the dock. The result says which
    of those two happened rather than silently presenting a partial tally.
    """
    layer_id = call.arguments.get("layer_id")
    if not isinstance(layer_id, str) or not layer_id.strip():
        raise ToolExecutionError("layer_id must be a non-empty string.")
    field_name = call.arguments.get("field")
    if not isinstance(field_name, str) or not field_name.strip():
        raise ToolExecutionError("field must be a non-empty string.")
    project = QgsProject.instance()
    layer = project.mapLayer(layer_id) if project is not None else None
    if not isinstance(layer, QgsVectorLayer):
        return {
            "available": False,
            "layer_id": agent_context.bound_text(layer_id, 128),
            "reason": "no such vector layer in this project",
        }
    index = layer.fields().indexOf(field_name)
    if index < 0:
        return {
            "available": False,
            "layer_id": agent_context.bound_text(layer_id, 128),
            "field": agent_context.bound_text(field_name, 128),
            "reason": "the layer has no field with that name",
        }
    request = QgsFeatureRequest()
    request.setFlags(Qgis.FeatureRequestFlag.NoGeometry)
    request.setSubsetOfAttributes([index])
    counts: Dict[Any, int] = {}
    scanned = 0
    complete = True
    for feature in layer.getFeatures(request):
        if scanned >= agent_context.MAX_SCAN_FEATURES:
            complete = False
            break
        # NULL is a QVariant on QGIS 3 and None on QGIS 4; normalize both so a
        # count of empty values reads the same on either runtime.
        value = feature.attribute(index)
        key = "" if value is None or str(value) == "NULL" else value
        counts[key] = counts.get(key, 0) + 1
        scanned += 1
    total = -1
    with contextlib.suppress(Exception):
        total = layer.featureCount()
    result = agent_context.build_field_values(
        layer_id=layer.id(),
        field_name=field_name,
        values=counts.items(),
        feature_count=total,
        scanned=scanned,
        complete=complete,
        limit=_clamp_limit(call.arguments.get("limit")),
    )
    result["available"] = True
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
    if registry is not None:
        for algorithm in registry.algorithms():
            haystack = f"{algorithm.id()} {algorithm.displayName()}".lower()
            if terms and not all(term in haystack for term in terms):
                continue
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
                }
            )
    # Relevance ranking requires the full match set before truncation; this
    # is inherent to "search", not a laziness gap in bound_list() itself.
    matches.sort(key=lambda item: item["algorithm_id"])
    bounded, truncated = agent_context.bound_list(matches, limit)
    return {"algorithms": bounded, "count": len(bounded), "truncated": truncated}


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
            )
        )
    return specs


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
    return {
        "algorithm_id": agent_context.bound_text(algorithm.id(), 200),
        "parameters": parameters,
    }


def _tool_processing_describe_factory(
    token_service: ContextTokenService,
) -> Callable[[AgentToolCall], Dict[str, Any]]:
    def _handler(call: AgentToolCall) -> Dict[str, Any]:
        algorithm_id = call.arguments.get("algorithm_id")
        if not isinstance(algorithm_id, str) or not algorithm_id.strip():
            raise ToolExecutionError("algorithm_id must be a non-empty string.")
        limit = _clamp_limit(call.arguments.get("limit"))
        registry = QgsApplication.processingRegistry()
        algorithm = registry.algorithmById(algorithm_id) if registry is not None else None
        if algorithm is None:
            return {
                "available": False,
                "algorithm_id": agent_context.bound_text(algorithm_id, 200),
            }
        # The safe *contract* of each parameter: enough to explain and to fill
        # in correctly, and deliberately never ``defaultValue()``, which for a
        # third-party algorithm can be a file path or a connection string.
        parameters = (
            {
                "name": agent_context.bound_text(definition.name(), 128),
                "type": agent_context.bound_text(definition.type(), 64),
                "required": not _param_is_optional(definition),
                "destination": bool(definition.isDestination()),
                "multiple": _param_allows_multiple(definition),
                "enum_options": _param_options(definition),
                "minimum": _param_bound(definition, "minimum"),
                "maximum": _param_bound(definition, "maximum"),
            }
            for definition in algorithm.parameterDefinitions()
        )
        bounded, truncated = agent_context.bound_list(parameters, limit)
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
            # A membership *test* for this one id, not an enumerator: it stops
            # the provider proposing a run that would certainly be refused. The
            # allowlist itself remains non-enumerable and cannot be extended.
            "agent_runnable": default_policy().record_for(algorithm.id()) is not None,
            # The freshness receipt for a later processing_run proposal. It
            # authorizes nothing: the deny-by-default SafeAlgorithmPolicy is the
            # only thing that decides whether this algorithm may ever run, and
            # it is re-checked against the live signature at approval time.
            "context_token": token_service.issue(
                PROCESSING_PROPOSAL_KIND, algorithm.id(), algorithm_signature_state(algorithm)
            ),
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
    return {"plugins": bounded, "count": len(bounded), "truncated": truncated}


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


def build_plugin_describe(qgis_utils: Any, package_name: str) -> Dict[str, Any]:
    """Return bounded, allowlisted metadata for one installed plugin.

    Resolves the package only against the same union ``plugin.list`` uses and
    never imports, instantiates, or invokes the plugin, reads its files, or
    follows any URL. Plugin metadata is untrusted data and is bounded here
    before serialization.
    """
    if package_name not in _plugin_union(qgis_utils):
        return {"available": False, "package_name": agent_context.bound_text(package_name, 128)}
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
    if package_name not in _plugin_union(qgis_utils):
        return None
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


def _tool_plugin_capabilities(call: AgentToolCall) -> Dict[str, Any]:
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
    return build_capabilities(
        plugin,
        providers,
        limit=limit,
        algorithm_allowed=AlgorithmCatalog.ai_algorithm_allowed,
    )


def _tool_plugin_describe(call: AgentToolCall) -> Dict[str, Any]:
    package_name = call.arguments.get("package_name")
    if not isinstance(package_name, str) or not package_name.strip():
        raise ToolExecutionError("package_name must be a non-empty string.")
    try:
        import qgis.utils as qgis_utils
    except ImportError as error:
        raise ToolExecutionError("Plugin registry is unavailable.") from error
    return build_plugin_describe(qgis_utils, package_name)


def build_default_registry(
    model_provider: ModelProvider,
    token_service: Optional[ContextTokenService] = None,
) -> AgentToolRegistry:
    """Build and return the thirteen-tool read-only Agent Workspace registry.

    ``token_service`` issues the opaque freshness tokens for ``model.describe``,
    ``layer.style`` and ``processing.describe``; the dock passes the same
    instance to the runtime proposal validator so tokens can be verified. When
    omitted a fresh service is created (useful for isolated tool tests).
    """
    token_service = token_service or ContextTokenService()
    registry = AgentToolRegistry()

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
                "type, CRS, visibility, and provider key."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema({"limit": _LIMIT_PROPERTY}),
            allowed_scopes=(AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
        ),
        _tool_layer_list,
    )
    registry.register(
        AgentToolSpec(
            name="layer.describe",
            title="Describe layer",
            description=(
                "Describes one layer by id: field names, broad field types, "
                "and how many features it holds. Never returns a source URI or "
                "an individual feature."
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
        _tool_layer_describe,
    )
    registry.register(
        AgentToolSpec(
            name="layer.field_values",
            title="Count a field's values",
            description=(
                "Aggregates one attribute of one vector layer into its distinct "
                "values and how many features carry each. Use it to answer "
                "'how many are X' and to build a categorized style whose "
                "classes match the real data. Returns counts only -- never an "
                "individual feature, id, or geometry -- and reports honestly "
                "when the layer was too large to count completely."
            ),
            risk=AgentRisk.READ_ONLY,
            input_schema=_object_schema(
                {
                    "layer_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _ID_MAX_LENGTH,
                    },
                    "field": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": _ID_MAX_LENGTH,
                    },
                    "limit": _LIMIT_PROPERTY,
                },
                required=["layer_id", "field"],
            ),
            allowed_scopes=(AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
        ),
        _tool_layer_field_values,
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
                "provider flag."
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
                "imports, instantiates, or calls the plugin, and never claims an "
                "unproved mapping."
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
        _tool_plugin_capabilities,
    )
    return registry
