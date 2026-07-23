"""Pure, QGIS-free planner that turns one parsed run proposal into a resolved,
policy-checked execution plan.

Every *security* decision for an execution lives here, not in the QGIS adapter:
which parameters a proposal may bind at all, whether a tagged binding may
satisfy the live parameter's kind, whether a referenced layer/field/enum option
actually exists, whether a number is inside the live bounds, and which
destinations exist (always forced to a temporary output by the caller). The
trusted runtime boundary (`runtime_proposals.py`) only *adapts* live QGIS
objects into the small immutable views below and then materializes the plan.

Keeping the decisions here means they are unit-testable without a Processing
registry, and that a mistake in the QGIS adapter cannot silently widen the
policy: the adapter can only narrow what the plan already permits.

Nothing here resolves a path, opens a file, touches a QgsProject, or executes
anything. A plan is inert data.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, List, Mapping, Optional, Sequence, Tuple

from .proposals import (
    MAX_WARNINGS,
    ProposalError,
    ProposalReason,
    _looks_like_path_or_uri,
)
from .safe_algorithm_policy import (
    BOOL,
    CRS,
    DISTANCE,
    ENUM,
    FIELD,
    MULTI_RASTER,
    NUMBER,
    ParamSpec,
    RASTER_LAYER,
    STRING_LABEL,
    SafeAlgorithmPolicy,
    VECTOR_LAYER,
    kind_matches,
)

# Layer kinds a :class:`LayerView` may report.
VECTOR = "vector"
RASTER = "raster"

# A label-safe string binding (an output column name) is far shorter than the
# generic proposal string bound; anything longer is not a label.
MAX_LABEL_STRING_CHARS = 128
# Bounds on what a single plan may touch, mirroring the proposal-level bounds.
MAX_PLAN_INPUT_LAYERS = 25
MAX_PREVIEW_LINES = 40
MAX_PREVIEW_VALUE_CHARS = 120
# Ceiling on the graph a model_run may execute, mirroring the model_patch
# candidate ceiling so a run can never exceed what a patch could build.
MAX_RUN_NODES = 80

# The only non-Processing node kinds a model_run may contain. They are
# application-owned SmartModeler inputs with no side effect of their own.
ALLOWED_SMART_NODES = frozenset(
    {"smart:input_layer", "smart:raster_layer", "smart:number", "smart:slider"}
)

# Which logical parameter kinds each tagged binding form may satisfy. A tag can
# never reach a kind outside this table, so a string can never land on a layer
# parameter and a number can never land on a field.
_TAG_KINDS: Mapping[str, FrozenSet[str]] = {
    "layer": frozenset({VECTOR_LAYER, RASTER_LAYER}),
    "layers": frozenset({MULTI_RASTER}),
    "field": frozenset({FIELD}),
    "number": frozenset({NUMBER}),
    "distance": frozenset({DISTANCE}),
    "bool": frozenset({BOOL}),
    "enum": frozenset({ENUM}),
    "enum_string": frozenset({ENUM}),
    "string": frozenset({STRING_LABEL}),
    "crs": frozenset({CRS}),
}

# Which layer kind each layer-ish parameter kind demands.
_KIND_LAYER_TYPE = {VECTOR_LAYER: VECTOR, RASTER_LAYER: RASTER, MULTI_RASTER: RASTER}


@dataclass(frozen=True)
class LayerView:
    """A QGIS-free view of one live project layer, resolved by id."""

    layer_id: str
    name: str
    kind: str
    field_names: FrozenSet[str] = frozenset()


@dataclass(frozen=True)
class ResolvedBinding:
    """One proposal binding, resolved against the live parameter and project."""

    param: str
    kind: str
    tag: str
    layer_ids: Tuple[str, ...] = ()
    value: Any = None


@dataclass(frozen=True)
class RunPlan:
    """An inert, fully resolved plan for one reviewed algorithm run."""

    algorithm_id: str
    bindings: Tuple[ResolvedBinding, ...] = ()
    destinations: Tuple[str, ...] = ()
    input_layer_ids: Tuple[str, ...] = ()
    preview_lines: Tuple[str, ...] = ()

    def binding_for(self, param: str) -> Optional[ResolvedBinding]:
        for binding in self.bindings:
            if binding.param == param:
                return binding
        return None


@dataclass(frozen=True)
class ModelRunPlan:
    """An inert, fully checked plan for running the current graph."""

    node_count: int = 0
    algorithm_ids: Tuple[str, ...] = ()
    terminal_outputs: Tuple[str, ...] = ()
    preview_lines: Tuple[str, ...] = ()


LayerLookup = Callable[[str], Optional[LayerView]]
# Returns the live parameter set of one algorithm id, or ``None`` when the
# algorithm is not present in the live registry.
ParamsLookup = Callable[[str], Optional[Sequence[ParamSpec]]]


def _reject(message: str, reason: str) -> None:
    raise ProposalError(message, reason)


def _preview_value(value: Any) -> str:
    text = value if isinstance(value, str) else repr(value)
    return text[:MAX_PREVIEW_VALUE_CHARS]


def _resolve_layer(layer_id: str, layer_lookup: LayerLookup, expected_kind: str) -> LayerView:
    view = layer_lookup(layer_id)
    if view is None:
        _reject("An input layer is not in the project.", ProposalReason.TARGET_MISSING)
    wanted = _KIND_LAYER_TYPE.get(expected_kind)
    if wanted is not None and view.kind != wanted:
        _reject(
            "An input layer is not of the type this parameter needs.",
            ProposalReason.VALIDATION_FAILED,
        )
    return view


def _check_common(
    param: str,
    tag: str,
    policy: SafeAlgorithmPolicy,
    record,
    params_by_name: Mapping[str, ParamSpec],
) -> Tuple[str, ParamSpec]:
    """Return the (kind, live spec) a binding must satisfy, or fail closed."""
    # The policy -- not this planner and not the proposal -- decides whether a
    # parameter is bindable at all, and to what kind.
    kind = policy.expected_kind(record, param)
    if kind is None:
        # Not bindable at all: an unknown parameter, or a destination, or a
        # reviewed-but-unbindable parameter such as a raster creation option.
        _reject("This parameter cannot be set by a proposal.", ProposalReason.UNSAFE_PARAMETER)
    spec = params_by_name.get(param)
    if spec is None:
        _reject("This parameter is not part of the algorithm.", ProposalReason.SIGNATURE_MISMATCH)
    if spec.is_destination:
        # Belt and braces: the allowlist never lists a destination as bindable.
        _reject("An output destination cannot be supplied.", ProposalReason.UNSAFE_PARAMETER)
    if not kind_matches(kind, spec):
        _reject("A parameter changed type since review.", ProposalReason.SIGNATURE_MISMATCH)
    if kind not in _TAG_KINDS.get(tag, frozenset()):
        _reject("This value form is not valid for this parameter.", ProposalReason.UNSAFE_PARAMETER)
    return kind, spec


def _plan_number(spec: ParamSpec, value: Any) -> Any:
    number = float(value)
    if spec.minimum is not None and number < float(spec.minimum):
        _reject("A numeric parameter is below its allowed minimum.", ProposalReason.VALIDATION_FAILED)
    if spec.maximum is not None and number > float(spec.maximum):
        _reject("A numeric parameter is above its allowed maximum.", ProposalReason.VALIDATION_FAILED)
    return value


def _plan_enum(spec: ParamSpec, tag: str, value: Any) -> int:
    options = tuple(spec.options or ())
    if not options:
        _reject("This choice parameter has no live options.", ProposalReason.VALIDATION_FAILED)
    if tag == "enum":
        index = int(value)
        if index < 0 or index >= len(options):
            _reject("A choice index is outside the live options.", ProposalReason.VALIDATION_FAILED)
        return index
    wanted = str(value).strip().casefold()
    for index, option in enumerate(options):
        if str(option).strip().casefold() == wanted:
            return index
    _reject("A choice label does not match any live option.", ProposalReason.VALIDATION_FAILED)
    return 0  # pragma: no cover - _reject always raises


def _plan_field(
    binding, layers_by_param: Mapping[str, Tuple[LayerView, ...]]
) -> str:
    views = layers_by_param.get(binding.layer_param)
    if not views:
        _reject(
            "A field was bound to a parameter that is not a bound input layer.",
            ProposalReason.VALIDATION_FAILED,
        )
    if len(views) != 1 or views[0].kind != VECTOR:
        _reject(
            "A field can only be bound to a single vector input layer.",
            ProposalReason.VALIDATION_FAILED,
        )
    if binding.value not in views[0].field_names:
        _reject(
            "A field does not exist on the bound input layer.",
            ProposalReason.VALIDATION_FAILED,
        )
    return binding.value


def plan_processing_run(
    proposal: Any,
    policy: SafeAlgorithmPolicy,
    record: Any,
    params: Sequence[ParamSpec],
    layer_lookup: LayerLookup,
    *,
    active_layer_id: Optional[str] = None,
    require_active_layer: bool = False,
) -> RunPlan:
    """Resolve one ``processing_run`` proposal into an inert :class:`RunPlan`.

    ``record`` is the reviewed allowlist entry (its signature was already
    confirmed against ``params`` by :class:`SafeAlgorithmPolicy`). Raises
    :class:`ProposalError` with a stable reason code for any violation; on
    success every binding is known to reference live, correctly typed state.
    """
    params_by_name: Dict[str, ParamSpec] = {spec.name: spec for spec in params}
    layers_by_param: Dict[str, Tuple[LayerView, ...]] = {}
    resolved: List[ResolvedBinding] = []
    input_layer_ids: List[str] = []
    preview: List[str] = []

    # Pass 1 -- layer bindings only, so a later field binding can be checked
    # against the layer it names without depending on dict ordering.
    for param, binding in proposal.inputs:
        if binding.tag not in ("layer", "layers"):
            continue
        kind, _spec = _check_common(param, binding.tag, policy, record, params_by_name)
        ids = (binding.value,) if binding.tag == "layer" else tuple(binding.value)
        if len(ids) > MAX_PLAN_INPUT_LAYERS:
            _reject("Too many input layers were bound.", ProposalReason.LIMIT_EXCEEDED)
        views = tuple(_resolve_layer(layer_id, layer_lookup, kind) for layer_id in ids)
        layers_by_param[param] = views
        resolved.append(
            ResolvedBinding(
                param=param,
                kind=kind,
                tag=binding.tag,
                layer_ids=tuple(view.layer_id for view in views),
            )
        )
        input_layer_ids.extend(view.layer_id for view in views)
        names = ", ".join(_preview_value(view.name) for view in views)
        preview.append(f"{param}: layer {names}")

    # Pass 2 -- every other tagged binding.
    for param, binding in proposal.inputs:
        if binding.tag in ("layer", "layers"):
            continue
        kind, spec = _check_common(param, binding.tag, policy, record, params_by_name)
        if binding.tag == "field":
            value: Any = _plan_field(binding, layers_by_param)
        elif binding.tag in ("number", "distance"):
            value = _plan_number(spec, binding.value)
        elif binding.tag == "bool":
            value = bool(binding.value)
        elif binding.tag in ("enum", "enum_string"):
            value = _plan_enum(spec, binding.tag, binding.value)
        elif binding.tag == "string":
            if len(str(binding.value)) > MAX_LABEL_STRING_CHARS:
                _reject("A label value is too long.", ProposalReason.LIMIT_EXCEEDED)
            value = str(binding.value)
        else:  # crs -- the authid's validity is confirmed by the QGIS adapter
            value = str(binding.value)
        resolved.append(ResolvedBinding(param=param, kind=kind, tag=binding.tag, value=value))
        preview.append(f"{param}: {_preview_value(value)}")

    # Every reviewed required input must actually be bound.
    bound_names = {binding.param for binding in resolved}
    for required in record.required_layer_params:
        if required not in bound_names:
            _reject("A required input was not provided.", ProposalReason.VALIDATION_FAILED)

    # In active-layer scope the reviewed primary input must be the active layer.
    if require_active_layer:
        primary = record.required_layer_params[0] if record.required_layer_params else ""
        views = layers_by_param.get(primary, ())
        if not active_layer_id or not views or views[0].layer_id != active_layer_id:
            _reject(
                "The run's primary input is not the current active layer.",
                ProposalReason.TARGET_MISSING,
            )

    for warning in tuple(proposal.warnings)[:MAX_WARNINGS]:
        preview.append(f"warning: {_preview_value(warning)}")

    return RunPlan(
        algorithm_id=proposal.algorithm_id,
        bindings=tuple(resolved),
        destinations=tuple(record.destinations),
        input_layer_ids=tuple(dict.fromkeys(input_layer_ids)),
        preview_lines=tuple(preview[:MAX_PREVIEW_LINES]),
    )


def _node_destination_is_safe(value: Any) -> bool:
    """Whether a graph node's configured destination value is safe to run.

    Only an unset value (the engine then forces a temporary output) or the
    literal temporary-output sentinel is acceptable; a configured file, folder,
    database, or network destination is not.
    """
    if value is None:
        return True
    if isinstance(value, str):
        text = value.strip()
        return not text or text == "TEMPORARY_OUTPUT"
    return False


def plan_model_run(
    graph: Any,
    policy: SafeAlgorithmPolicy,
    params_lookup: ParamsLookup,
) -> ModelRunPlan:
    """Check the live current graph node by node and return an inert plan.

    Every Processing node must pass the same deny-by-default policy a single
    ``processing_run`` passes; only application-owned ``smart:*`` input nodes are
    exempt. Any configured file/folder/database/network destination on a node is
    rejected outright rather than silently rewritten.
    """
    if graph is None or not getattr(graph, "nodes", None):
        _reject("There is no current workflow to run.", ProposalReason.VALIDATION_FAILED)
    nodes = list(graph.nodes.values())
    if len(nodes) > MAX_RUN_NODES:
        _reject("The current workflow is too large to run.", ProposalReason.LIMIT_EXCEEDED)
    issues = [issue for issue in graph.validate() if getattr(issue, "level", "") == "error"]
    if issues:
        _reject(
            "The current workflow has validation errors; fix them before running.",
            ProposalReason.VALIDATION_FAILED,
        )

    algorithm_ids: List[str] = []
    for node in nodes:
        algorithm_id = str(getattr(node, "algorithm_id", ""))
        parameters = dict(getattr(node, "parameters", {}) or {})
        if algorithm_id in ALLOWED_SMART_NODES:
            _check_smart_node_values(parameters)
            continue
        specs = params_lookup(algorithm_id)
        if specs is None:
            _reject(
                "The workflow uses an algorithm that is not available.",
                ProposalReason.ALGORITHM_NOT_ALLOWED,
            )
        decision = policy.is_runnable(algorithm_id, specs)
        if not decision.allowed:
            _reject(
                "The workflow uses an algorithm that is not approved for agent runs.",
                decision.reason_code,
            )
        _check_processing_node_values(parameters, specs)
        algorithm_ids.append(algorithm_id)

    terminal_outputs = _terminal_output_names(graph)
    preview = [f"Nodes: {len(nodes)}", f"Processing steps: {len(algorithm_ids)}"]
    if terminal_outputs:
        preview.append("Terminal outputs: " + ", ".join(terminal_outputs[:10]))
    return ModelRunPlan(
        node_count=len(nodes),
        algorithm_ids=tuple(algorithm_ids),
        terminal_outputs=tuple(terminal_outputs),
        preview_lines=tuple(preview[:MAX_PREVIEW_LINES]),
    )


def _check_smart_node_values(parameters: Mapping[str, Any]) -> None:
    """A smart input node may reference a project layer id/name or a number --
    never a path, URI, or connection string."""
    for value in parameters.values():
        if isinstance(value, str) and _looks_like_path_or_uri(value):
            _reject(
                "A workflow input refers to a file or connection rather than a project layer.",
                ProposalReason.UNSAFE_PARAMETER,
            )


def _check_processing_node_values(
    parameters: Mapping[str, Any], specs: Sequence[ParamSpec]
) -> None:
    by_name = {spec.name: spec for spec in specs}
    for name, value in parameters.items():
        spec = by_name.get(name)
        if spec is not None and spec.is_destination:
            if not _node_destination_is_safe(value):
                _reject(
                    "A workflow step writes to a file, database, or network destination.",
                    ProposalReason.UNSAFE_PARAMETER,
                )
            continue
        if isinstance(value, str) and _looks_like_path_or_uri(value):
            _reject(
                "A workflow step parameter refers to a file or connection.",
                ProposalReason.UNSAFE_PARAMETER,
            )


def _terminal_output_names(graph: Any) -> List[str]:
    names: List[str] = []
    for node_id, node in graph.nodes.items():
        if any(True for _edge in graph.outgoing_edges(node_id)):
            continue
        for output_id in getattr(node, "outputs", {}) or {}:
            names.append(str(output_id))
    return names


@dataclass(frozen=True)
class RunResultSummary:
    """The bounded, detached description of a finished run.

    ``to_dict`` travels from the coordinator to the trusted dock only. It holds
    layer names and ids -- the same identifiers the read-only ``layer.list``
    inspection already exposes -- so the dock can render the result and record
    exactly which layers a later Undo may remove. It carries no source path, no
    parameter value, and no feature value, and it is never sent to the provider.
    """

    kind: str
    title: str
    target: str
    layer_names: Tuple[str, ...] = field(default_factory=tuple)
    layer_ids: Tuple[str, ...] = field(default_factory=tuple)
    lines: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "title": self.title,
            "target": self.target,
            "layer_names": list(self.layer_names),
            "layer_ids": list(self.layer_ids),
            "layer_count": len(self.layer_ids),
            "lines": list(self.lines),
        }
