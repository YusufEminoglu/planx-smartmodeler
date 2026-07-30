"""Application-owned, deny-by-default safe-algorithm policy for Agent runs.

A `processing_run` / `model_run` may execute either a signature-pinned reviewed
algorithm or a first-party QGIS/PlanX algorithm whose *live* signature passes
the structural policy: constrained tagged inputs only, temporary map-layer
destinations only, and no known network/project/database side effects except a
signature-pinned, application-owned adapter such as bounded QuickOSM extent
acquisition.
Provider output and user text can never extend these rules.

The policy is deliberately QGIS-free: it reasons over a small immutable
:class:`ParamSpec` view of each live parameter definition (name, destination
flag, type-name set, optional flag, default presence). The trusted runtime
boundary (`runtime_proposals.py`, which already imports QGIS) builds those views
from the live ``QgsProcessingParameterDefinition`` objects; unit tests build them
directly. This keeps the security policy testable without a Processing registry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Mapping, Optional, Sequence, Tuple

from .proposals import ProposalReason

# -- parameter kinds --------------------------------------------------------

# Logical kinds a proposal binding may satisfy, each mapped to the concrete
# QgsProcessingParameter* class names the live definition's MRO must include.
VECTOR_LAYER = "vector_layer"
RASTER_LAYER = "raster_layer"
MULTI_RASTER = "multi_raster"
MULTI_VECTOR = "multi_vector"
FIELD = "field"
NUMBER = "number"
DISTANCE = "distance"
BOOL = "bool"
ENUM = "enum"
CRS = "crs"
STRING_LABEL = "string_label"
STRING_TEXT = "string_text"
MAP_EXTENT = "map_extent"
OSM_TAG = "osm_tag"
EXPRESSION = "expression"

_KIND_CLASS_NAMES: Mapping[str, FrozenSet[str]] = {
    VECTOR_LAYER: frozenset(
        {"QgsProcessingParameterFeatureSource", "QgsProcessingParameterVectorLayer"}
    ),
    RASTER_LAYER: frozenset({"QgsProcessingParameterRasterLayer"}),
    # Both multi kinds are the same QGIS class; they differ only in the layer
    # *type* the run planner then demands (raster vs vector). Keeping them as
    # separate kinds is what lets an allowlist entry pin which one it means.
    MULTI_RASTER: frozenset({"QgsProcessingParameterMultipleLayers"}),
    MULTI_VECTOR: frozenset({"QgsProcessingParameterMultipleLayers"}),
    FIELD: frozenset({"QgsProcessingParameterField"}),
    NUMBER: frozenset({"QgsProcessingParameterNumber"}),
    # A Distance parameter is-a Number, so either class name satisfies it.
    DISTANCE: frozenset({"QgsProcessingParameterDistance", "QgsProcessingParameterNumber"}),
    BOOL: frozenset({"QgsProcessingParameterBoolean"}),
    ENUM: frozenset({"QgsProcessingParameterEnum"}),
    CRS: frozenset({"QgsProcessingParameterCrs"}),
    STRING_LABEL: frozenset({"QgsProcessingParameterString"}),
    STRING_TEXT: frozenset({"QgsProcessingParameterString"}),
    MAP_EXTENT: frozenset({"QgsProcessingParameterExtent"}),
    OSM_TAG: frozenset({"QgsProcessingParameterString"}),
    EXPRESSION: frozenset({"QgsProcessingParameterExpression"}),
}

# Blocked id terms mirrored from AlgorithmCatalog.AI_BLOCKED_ID_TERMS so the
# policy can fail closed without importing the QGIS-bound catalog.
_BLOCKED_ID_TERMS = (
    "command",
    "download",
    "executesql",
    "execute_sql",
    "shell",
    "upload",
    "nominatim",
    "geocoder",
    "postgis",
    "spatialite",
)
_BLOCKED_ALGORITHM_IDS = frozenset(
    {
        "native:createdirectory",
        "native:layertobookmarks",
        "native:loadlayer",
        "native:setlayerstyle",
        "native:setprojectvariable",
        "qgis:setstyleforrasterlayer",
        "qgis:setstyleforvectorlayer",
    }
)

# First-party providers whose live signatures may be admitted by the structural
# safe-run policy below. External command providers (GDAL/GRASS/PDAL), arbitrary
# third-party plugins, and scripts remain deny-by-default.
_STRUCTURALLY_TRUSTED_PREFIXES = (
    "native:",
    "qgis:",
    "planx:",
    "planx_",
)

_SAFE_DESTINATION_CLASS_NAMES = frozenset(
    {
        "QgsProcessingParameterFeatureSink",
        "QgsProcessingParameterRasterDestination",
        "QgsProcessingParameterVectorDestination",
    }
)
_SAFE_INTERNAL_DESTINATION_CLASS_NAMES = frozenset(
    {"QgsProcessingParameterFileDestination"}
)

# PlanX's embedded algorithms use bounded strings for domain settings such as
# radii ("800, n"), class breaks and scenario labels. These are ordinary
# values, not expressions or resource locators. Parameter names with external
# resource/code semantics remain ineligible even inside this first-party
# provider.
_UNSAFE_TEXT_PARAM_TERMS = (
    "command",
    "code",
    "connection",
    "credential",
    "database",
    "directory",
    "expression",
    "filter",
    "file",
    "folder",
    "formula",
    "host",
    "password",
    "path",
    "query",
    "script",
    "server",
    "sql",
    "template",
    "token",
    "uri",
    "url",
)


@dataclass(frozen=True)
class ParamSpec:
    """A QGIS-free view of one live parameter definition.

    The first five fields are what the signature gate reasons about. The last
    three carry the live *value domain* the run planner checks a binding
    against (choice labels and numeric bounds); they default to "unknown" so an
    existing policy test can build a spec from the signature alone.
    """

    name: str
    is_destination: bool
    type_names: FrozenSet[str]
    is_optional: bool
    has_default: bool
    options: Tuple[str, ...] = ()
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    source_type: str = ""


@dataclass(frozen=True)
class OutputSpec:
    """A QGIS-free view of one live Processing output definition."""

    name: str
    type_names: FrozenSet[str]


@dataclass(frozen=True)
class AllowedAlgorithm:
    """One reviewed, side-effect-safe algorithm and its pinned signature."""

    algorithm_id: str
    # Every parameter a proposal MAY bind, mapped to the kind it must satisfy.
    bindable: Mapping[str, str]
    # Inputs that must be present and bound for the run to be meaningful.
    required_layer_params: Tuple[str, ...]
    # Destination parameters, always forced to a temporary output.
    destinations: Tuple[str, ...]
    # Required bindable parameters that are not necessarily project layers.
    required_params: Tuple[str, ...] = ()
    # Reviewed optional map-layer sinks that may exist in the live signature
    # but are deliberately left unset. This avoids clutter such as an empty
    # FAIL_OUTPUT layer while still treating any unknown destination as
    # signature drift.
    optional_destinations: Tuple[str, ...] = ()
    # Reviewed non-layer destinations needed internally by an adapter. They are
    # never supplied by a provider and are always forced to an application-owned
    # temporary path.
    internal_destinations: Tuple[str, ...] = ()
    # Result dictionary keys that must resolve to map layers. Normally these are
    # the same names as ``destinations``; adapters such as QuickOSM expose vector
    # results separately from their internal download-file parameter.
    result_outputs: Tuple[str, ...] = ()
    # Application-owned values pinned by the adapter (for example a fixed
    # Overpass endpoint and timeout). A proposal cannot override them.
    fixed_values: Optional[Mapping[str, Any]] = None
    # A reviewed network adapter is visible as high risk and always needs the
    # normal explicit Run approval.
    network_access: bool = False

    @property
    def label_safe_string_params(self) -> Tuple[str, ...]:
        return tuple(name for name, kind in self.bindable.items() if kind == STRING_LABEL)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    reason_code: str = ""
    record: Optional[AllowedAlgorithm] = None


def _deny(reason_code: str) -> PolicyDecision:
    return PolicyDecision(allowed=False, reason_code=reason_code)


def kind_matches(kind: str, param: ParamSpec) -> bool:
    accepted = _KIND_CLASS_NAMES.get(kind)
    if not accepted:
        return False
    if not accepted & param.type_names:
        return False
    if kind == MULTI_RASTER:
        return param.source_type == "raster"
    if kind == MULTI_VECTOR:
        return param.source_type == "vector"
    return True


def _fixed_value_matches(value: Any, param: ParamSpec) -> bool:
    if isinstance(value, bool):
        return kind_matches(BOOL, param)
    if isinstance(value, (int, float)):
        return kind_matches(NUMBER, param)
    if isinstance(value, str):
        return kind_matches(STRING_TEXT, param)
    return False


def _id_has_blocked_term(algorithm_id: str) -> bool:
    normalized = algorithm_id.lower().replace("-", "_")
    return (
        normalized in _BLOCKED_ALGORITHM_IDS
        or any(term in normalized for term in _BLOCKED_ID_TERMS)
    )


def _safe_first_party_text(algorithm_id: str, param: ParamSpec) -> bool:
    """Whether a trusted QGIS/PlanX string is ordinary bounded domain text.

    Names suggesting code, expressions, resources, credentials or connections
    stay blocked. This admits harmless first-party settings such as output
    column names, labels, radii and class breaks without maintaining one-off
    algorithm entries.
    """
    if not algorithm_id.startswith(_STRUCTURALLY_TRUSTED_PREFIXES):
        return False
    normalized = param.name.strip().lower().replace("-", "_")
    return not any(term in normalized for term in _UNSAFE_TEXT_PARAM_TERMS)


def _structural_kind(algorithm_id: str, param: ParamSpec) -> Optional[str]:
    """Return the narrow tagged-binding kind for a safe live input."""
    # Order matters: Distance subclasses Number, and the two multiple-layer
    # kinds share one QGIS class but differ by their live layerType.
    for kind in (
        DISTANCE,
        MAP_EXTENT,
        MULTI_RASTER,
        MULTI_VECTOR,
        VECTOR_LAYER,
        RASTER_LAYER,
        FIELD,
        NUMBER,
        BOOL,
        ENUM,
        CRS,
    ):
        if kind_matches(kind, param):
            return kind
    if kind_matches(STRING_TEXT, param) and _safe_first_party_text(
        algorithm_id, param
    ):
        return STRING_TEXT
    return None


def _structural_record(
    algorithm_id: str,
    params: Sequence[ParamSpec],
) -> Optional[AllowedAlgorithm]:
    """Build a safe record from a trusted provider's *live* signature.

    This broadens useful QGIS coverage without creating a "run anything" path:
    every input must have a constrained tagged representation, every output
    must be an application-owned temporary map-layer destination, algorithms
    with project/network/database side effects are blocked by id, and the same
    live structure is re-evaluated at the human's Run click.
    """
    if not algorithm_id.startswith(_STRUCTURALLY_TRUSTED_PREFIXES):
        return None
    if _id_has_blocked_term(algorithm_id):
        return None

    bindable = {}
    required_layers = []
    destinations = []
    layer_kinds = frozenset(
        {VECTOR_LAYER, RASTER_LAYER, MULTI_VECTOR, MULTI_RASTER}
    )
    for param in params:
        if param.is_destination:
            if not (_SAFE_DESTINATION_CLASS_NAMES & param.type_names):
                return None
            destinations.append(param.name)
            continue
        kind = _structural_kind(algorithm_id, param)
        if kind is None:
            # Even an optional opaque string/file/database parameter is not
            # silently inherited: its default may carry a path or side effect.
            return None
        bindable[param.name] = kind
        if kind in layer_kinds and not param.is_optional and not param.has_default:
            required_layers.append(param.name)

    if not destinations:
        return None
    return AllowedAlgorithm(
        algorithm_id=algorithm_id,
        bindable=bindable,
        required_layer_params=tuple(required_layers),
        destinations=tuple(destinations),
    )


class SafeAlgorithmPolicy:
    """Deny-by-default gate over pinned and structurally safe algorithms."""

    def __init__(self, allowlist: Optional[Mapping[str, AllowedAlgorithm]] = None) -> None:
        self._allow_structural = allowlist is None
        self._allowed: Mapping[str, AllowedAlgorithm] = (
            dict(allowlist) if allowlist is not None else dict(_DEFAULT_ALLOWLIST)
        )

    # Deliberately no "list the allowlist" accessor: the allowlist is never an
    # enumerable capability, so nothing can advertise it to a provider or grow a
    # suggestion loop around it. Membership is only ever *tested*, one id at a
    # time, by trusted code.

    def record_for(self, algorithm_id: str) -> Optional[AllowedAlgorithm]:
        return self._allowed.get(algorithm_id)

    def expected_kind(self, record: AllowedAlgorithm, param_name: str) -> Optional[str]:
        """The kind a bound parameter must satisfy, or ``None`` if not bindable."""
        return record.bindable.get(param_name)

    def is_runnable(
        self,
        algorithm_id: str,
        params: Sequence[ParamSpec],
        outputs: Sequence[OutputSpec] = (),
    ) -> PolicyDecision:
        """Return an allow/deny decision for a live algorithm's current signature.

        ``params`` is the live parameter set viewed as :class:`ParamSpec`. The
        caller must already have confirmed the algorithm exists in the live
        registry. Deny reasons never leak parameter detail.
        """
        record = self._allowed.get(algorithm_id)
        if record is None and self._allow_structural:
            record = _structural_record(algorithm_id, params)
        if record is None:
            if not self._allow_structural:
                return _deny(ProposalReason.ALGORITHM_NOT_ALLOWED)
            if not algorithm_id.startswith(_STRUCTURALLY_TRUSTED_PREFIXES):
                return _deny(ProposalReason.PROVIDER_NOT_TRUSTED)
            if _id_has_blocked_term(algorithm_id):
                return _deny(ProposalReason.SIDE_EFFECT_BLOCKED)
            if any(
                param.is_destination
                and not (_SAFE_DESTINATION_CLASS_NAMES & param.type_names)
                for param in params
            ):
                return _deny(ProposalReason.UNSAFE_DESTINATION)
            if not any(param.is_destination for param in params):
                return _deny(ProposalReason.NO_LAYER_OUTPUT)
            return _deny(ProposalReason.UNSUPPORTED_PARAMETER)
        if _id_has_blocked_term(algorithm_id) and not record.network_access:
            return _deny(ProposalReason.SIDE_EFFECT_BLOCKED)

        by_name = {param.name: param for param in params}

        # Every bindable parameter is part of the reviewed signature, not just
        # the required layer inputs. Retyping an enum/string/number must fail
        # closed before a provider can bind a value under the old contract.
        for pname, kind in record.bindable.items():
            param = by_name.get(pname)
            if (
                param is None
                or param.is_destination
                or not kind_matches(kind, param)
            ):
                return _deny(ProposalReason.SIGNATURE_MISMATCH)

        # Required inputs must exist, be non-destinations, and match their kind.
        for pname in record.required_layer_params:
            param = by_name.get(pname)
            if param is None or param.is_destination:
                return _deny(ProposalReason.SIGNATURE_MISMATCH)
            kind = record.bindable.get(pname)
            if kind is None or not kind_matches(kind, param):
                return _deny(ProposalReason.SIGNATURE_MISMATCH)

        # Every pinned destination must exist and actually be a destination.
        for dname in record.destinations:
            param = by_name.get(dname)
            if param is None or not param.is_destination:
                return _deny(ProposalReason.SIGNATURE_MISMATCH)

        for dname in record.optional_destinations:
            param = by_name.get(dname)
            if (
                param is None
                or not param.is_destination
                or not param.is_optional
                or not (_SAFE_DESTINATION_CLASS_NAMES & param.type_names)
            ):
                return _deny(ProposalReason.SIGNATURE_MISMATCH)

        for dname in record.internal_destinations:
            param = by_name.get(dname)
            if (
                param is None
                or not param.is_destination
                or not param.is_optional
                or not (_SAFE_INTERNAL_DESTINATION_CLASS_NAMES & param.type_names)
            ):
                return _deny(ProposalReason.SIGNATURE_MISMATCH)

        for pname, value in (record.fixed_values or {}).items():
            param = by_name.get(pname)
            if (
                param is None
                or param.is_destination
                or not _fixed_value_matches(value, param)
            ):
                return _deny(ProposalReason.SIGNATURE_MISMATCH)

        if record.result_outputs:
            output_by_name = {output.name: output for output in outputs}
            for name in record.result_outputs:
                output = output_by_name.get(name)
                if (
                    output is None
                    or "QgsProcessingOutputVectorLayer" not in output.type_names
                ):
                    return _deny(ProposalReason.SIGNATURE_MISMATCH)

        known = (
            set(record.bindable)
            | set(record.destinations)
            | set(record.optional_destinations)
            | set(record.internal_destinations)
            | set(record.fixed_values or {})
        )
        known_destinations = set(record.destinations) | set(
            record.optional_destinations
        ) | set(record.internal_destinations)
        for pname, param in by_name.items():
            # An unpinned destination (e.g. a newly added file/HTML output) is a
            # signature drift: deny until individually reviewed.
            if param.is_destination and pname not in known_destinations:
                return _deny(ProposalReason.SIGNATURE_MISMATCH)
            if pname in known:
                continue
            # A newly added parameter we do not bind, which is mandatory and has
            # no default, changes the run contract: deny until reviewed.
            if not param.is_optional and not param.has_default:
                return _deny(ProposalReason.SIGNATURE_MISMATCH)

        return PolicyDecision(allowed=True, record=record)


# -- the shipped, reviewed initial allowlist (owner decision 2026-07-23) -----
# Focused core of seventeen native algorithms; signatures probed live on QGIS
# 4.2.0 and 3.44.12 LTR. Bindable holds only the safe, cross-version inputs a
# proposal may set; every destination is forced to a temporary output.

def _alg(
    algorithm_id: str,
    bindable: Mapping[str, str],
    required: Tuple[str, ...],
    destinations: Tuple[str, ...] = ("OUTPUT",),
    optional_destinations: Tuple[str, ...] = (),
) -> AllowedAlgorithm:
    return AllowedAlgorithm(
        algorithm_id=algorithm_id,
        bindable=dict(bindable),
        required_layer_params=required,
        destinations=destinations,
        optional_destinations=optional_destinations,
    )


_DEFAULT_ALLOWLIST: Mapping[str, AllowedAlgorithm] = {
    # Optional 02Agent OSM Downloader integration. These exact Processing
    # signatures are accepted only while the separately installed provider
    # reports them live. Every output is application-forced temporary.
    "zero2agentosm:download_preset": AllowedAlgorithm(
        algorithm_id="zero2agentosm:download_preset",
        bindable={"PRESET": ENUM, "EXTENT": MAP_EXTENT},
        required_layer_params=(),
        required_params=("PRESET", "EXTENT"),
        destinations=(
            "OUTPUT_POINTS",
            "OUTPUT_LINES",
            "OUTPUT_POLYGONS",
        ),
        network_access=True,
    ),
    "zero2agentosm:download_custom_tag": AllowedAlgorithm(
        algorithm_id="zero2agentosm:download_custom_tag",
        bindable={
            "KEY": OSM_TAG,
            "VALUE": OSM_TAG,
            "GEOMETRY": ENUM,
            "EXTENT": MAP_EXTENT,
        },
        required_layer_params=(),
        required_params=("KEY", "GEOMETRY", "EXTENT"),
        destinations=(
            "OUTPUT_POINTS",
            "OUTPUT_LINES",
            "OUTPUT_POLYGONS",
        ),
        network_access=True,
    ),
    # SmartModeler's own direct OSM acquisition algorithms.  Each algorithm
    # fixes its geometry family in code and exposes only a plain key/value pair
    # plus a QGIS-owned canvas or project-layer extent. Endpoints, Overpass QL,
    # timeout and output
    # destination are not parameters, and OUTPUT is always forced temporary.
    "smartmodeler:osm_download_points": AllowedAlgorithm(
        algorithm_id="smartmodeler:osm_download_points",
        bindable={"KEY": OSM_TAG, "VALUE": OSM_TAG, "EXTENT": MAP_EXTENT},
        required_layer_params=(),
        required_params=("KEY", "EXTENT"),
        destinations=("OUTPUT",),
        network_access=True,
    ),
    "smartmodeler:osm_download_lines": AllowedAlgorithm(
        algorithm_id="smartmodeler:osm_download_lines",
        bindable={"KEY": OSM_TAG, "VALUE": OSM_TAG, "EXTENT": MAP_EXTENT},
        required_layer_params=(),
        required_params=("KEY", "EXTENT"),
        destinations=("OUTPUT",),
        network_access=True,
    ),
    "smartmodeler:osm_download_polygons": AllowedAlgorithm(
        algorithm_id="smartmodeler:osm_download_polygons",
        bindable={"KEY": OSM_TAG, "VALUE": OSM_TAG, "EXTENT": MAP_EXTENT},
        required_layer_params=(),
        required_params=("KEY", "EXTENT"),
        destinations=("OUTPUT",),
        network_access=True,
    ),
    # Reviewed QuickOSM adapter. The provider may supply only a plain OSM
    # key/value pair and request the *current map canvas extent*. The endpoint,
    # timeout and download destination are application-owned; arbitrary
    # Overpass queries, URLs and file paths cannot be expressed.
    "quickosm:downloadosmdataextentquery": AllowedAlgorithm(
        algorithm_id="quickosm:downloadosmdataextentquery",
        bindable={"KEY": OSM_TAG, "VALUE": OSM_TAG, "EXTENT": MAP_EXTENT},
        required_layer_params=(),
        required_params=("KEY", "EXTENT"),
        destinations=(),
        internal_destinations=("FILE",),
        result_outputs=("OUTPUT_MULTIPOLYGONS",),
        fixed_values={
            "TIMEOUT": 25,
            "SERVER": "https://overpass-api.de/api/interpreter",
        },
        network_access=True,
    ),
    "native:buffer": _alg(
        "native:buffer",
        {"INPUT": VECTOR_LAYER, "DISTANCE": DISTANCE, "SEGMENTS": NUMBER, "DISSOLVE": BOOL},
        ("INPUT",),
    ),
    "native:centroids": _alg(
        "native:centroids",
        {"INPUT": VECTOR_LAYER, "ALL_PARTS": BOOL},
        ("INPUT",),
    ),
    "native:clip": _alg(
        "native:clip",
        {"INPUT": VECTOR_LAYER, "OVERLAY": VECTOR_LAYER},
        ("INPUT", "OVERLAY"),
    ),
    "native:dissolve": _alg(
        "native:dissolve",
        {"INPUT": VECTOR_LAYER, "FIELD": FIELD},
        ("INPUT",),
    ),
    # Extract-by-attribute is the "filter these features into a new layer"
    # request users actually ask for. It is side-effect-safe like the rest:
    # it reads one vector layer and writes a forced temporary output, and every
    # binding is a constrained kind (a field name, an enum index, a plain
    # comparison value) -- never a path or an expression. Signatures verified
    # identical on QGIS 3.44.12 LTR and 4.2.0.
    "native:extractbyattribute": _alg(
        "native:extractbyattribute",
        {"INPUT": VECTOR_LAYER, "FIELD": FIELD, "OPERATOR": ENUM, "VALUE": STRING_LABEL},
        ("INPUT",),
        # FAIL_OUTPUT remains part of the reviewed signature but is optional
        # and deliberately left unset, so a normal extraction adds only the
        # matching-features layer the user asked for.
        destinations=("OUTPUT",),
        optional_destinations=("FAIL_OUTPUT",),
    ),
    # "Extract the features of X that intersect / are within / touch Y" -- the
    # spatial sibling of extract-by-attribute. Reads two vector layers, writes a
    # forced temporary output, and PREDICATE is bound only as a live option
    # index (a single int satisfies its multi-value enum on both runtimes).
    "native:extractbylocation": _alg(
        "native:extractbylocation",
        {"INPUT": VECTOR_LAYER, "PREDICATE": ENUM, "INTERSECT": VECTOR_LAYER},
        ("INPUT", "INTERSECT"),
    ),
    # "Join the attributes of layer B onto layer A where a field matches" -- a
    # table join, no expression and no path. FIELD binds to INPUT and FIELD_2 to
    # INPUT_2 via each field binding's layer_param. Both sinks are pinned so the
    # signature gate accepts it; each is forced to a temporary output, so the run
    # adds the joined layer and a non-matching (NON_MATCHING) layer. The optional
    # multi-field FIELDS_TO_COPY and the PREFIX string are deliberately left
    # unbindable.
    "native:joinattributestable": _alg(
        "native:joinattributestable",
        {
            "INPUT": VECTOR_LAYER,
            "FIELD": FIELD,
            "INPUT_2": VECTOR_LAYER,
            "FIELD_2": FIELD,
            "METHOD": ENUM,
            "DISCARD_NONMATCHING": BOOL,
        },
        ("INPUT", "INPUT_2"),
        destinations=("OUTPUT",),
        optional_destinations=("NON_MATCHING",),
    ),
    # "Merge all these layers into one." LAYERS is a *vector* multilayer, so it
    # is pinned as MULTI_VECTOR (the run planner then demands vector inputs);
    # CRS is an authid the QGIS adapter validates, never a path.
    "native:mergevectorlayers": _alg(
        "native:mergevectorlayers",
        {"LAYERS": MULTI_VECTOR, "CRS": CRS, "ADD_SOURCE_FIELDS": BOOL},
        ("LAYERS",),
    ),
    # "Randomly take N features and create a new layer." This is deliberately
    # randomextract, not randomselection: the latter mutates the input layer's
    # selection state and has no output sink. METHOD is pinned as a live enum
    # and NUMBER as a bounded numeric value; OUTPUT is always temporary.
    # Signatures verified identical on QGIS 3.44.12 LTR and 4.2.0.
    "native:randomextract": _alg(
        "native:randomextract",
        {"INPUT": VECTOR_LAYER, "METHOD": ENUM, "NUMBER": NUMBER},
        ("INPUT",),
    ),
    "native:difference": _alg(
        "native:difference",
        {"INPUT": VECTOR_LAYER, "OVERLAY": VECTOR_LAYER},
        ("INPUT", "OVERLAY"),
    ),
    "native:intersection": _alg(
        "native:intersection",
        {"INPUT": VECTOR_LAYER, "OVERLAY": VECTOR_LAYER},
        ("INPUT", "OVERLAY"),
    ),
    "native:convexhull": _alg(
        "native:convexhull",
        {"INPUT": VECTOR_LAYER},
        ("INPUT",),
    ),
    "native:reprojectlayer": _alg(
        "native:reprojectlayer",
        {"INPUT": VECTOR_LAYER, "TARGET_CRS": CRS},
        ("INPUT", "TARGET_CRS"),
    ),
    "native:fixgeometries": _alg(
        "native:fixgeometries",
        {"INPUT": VECTOR_LAYER, "METHOD": ENUM},
        ("INPUT",),
    ),
    "native:boundingboxes": _alg(
        "native:boundingboxes",
        {"INPUT": VECTOR_LAYER},
        ("INPUT",),
    ),
    "native:countpointsinpolygon": _alg(
        "native:countpointsinpolygon",
        {"POLYGONS": VECTOR_LAYER, "POINTS": VECTOR_LAYER, "FIELD": STRING_LABEL},
        ("POLYGONS", "POINTS"),
    ),
    # QGIS Field Calculator is the reviewed expression execution boundary.
    # FORMULA is not ordinary text: runtime_proposals validates it with the
    # live QgsExpression parser, checks referenced columns against INPUT, and
    # rejects custom/dynamic/environment/filesystem introspection functions
    # before this signature can reach an approval card.
    "native:fieldcalculator": AllowedAlgorithm(
        algorithm_id="native:fieldcalculator",
        bindable={
            "INPUT": VECTOR_LAYER,
            "FIELD_NAME": STRING_LABEL,
            "FIELD_TYPE": ENUM,
            "FIELD_LENGTH": NUMBER,
            "FIELD_PRECISION": NUMBER,
            "FORMULA": EXPRESSION,
        },
        required_layer_params=("INPUT",),
        required_params=("FIELD_NAME", "FIELD_TYPE", "FORMULA"),
        destinations=("OUTPUT",),
    ),
    "native:cellstatistics": _alg(
        "native:cellstatistics",
        {
            "INPUT": MULTI_RASTER,
            "REFERENCE_LAYER": RASTER_LAYER,
            "STATISTIC": ENUM,
            "IGNORE_NODATA": BOOL,
            "OUTPUT_NODATA_VALUE": NUMBER,
        },
        ("INPUT", "REFERENCE_LAYER"),
    ),
}


def default_policy() -> SafeAlgorithmPolicy:
    return SafeAlgorithmPolicy()
