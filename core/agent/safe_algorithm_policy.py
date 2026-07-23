"""Application-owned, deny-by-default safe-algorithm policy for Phase 05.

A `processing_run` / `model_run` may execute an algorithm **only** if it is in
this shipped, hardcoded allowlist *and* its live signature still matches the
reviewed record. Provider output and user text can never extend the allowlist.

The policy is deliberately QGIS-free: it reasons over a small immutable
:class:`ParamSpec` view of each live parameter definition (name, destination
flag, type-name set, optional flag, default presence). The trusted runtime
boundary (`runtime_proposals.py`, which already imports QGIS) builds those views
from the live ``QgsProcessingParameterDefinition`` objects; unit tests build them
directly. This keeps the security policy testable without a Processing registry.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Mapping, Optional, Sequence, Tuple

from .proposals import ProposalReason

# -- parameter kinds --------------------------------------------------------

# Logical kinds a proposal binding may satisfy, each mapped to the concrete
# QgsProcessingParameter* class names the live definition's MRO must include.
VECTOR_LAYER = "vector_layer"
RASTER_LAYER = "raster_layer"
MULTI_RASTER = "multi_raster"
FIELD = "field"
NUMBER = "number"
DISTANCE = "distance"
BOOL = "bool"
ENUM = "enum"
CRS = "crs"
STRING_LABEL = "string_label"

_KIND_CLASS_NAMES: Mapping[str, FrozenSet[str]] = {
    VECTOR_LAYER: frozenset(
        {"QgsProcessingParameterFeatureSource", "QgsProcessingParameterVectorLayer"}
    ),
    RASTER_LAYER: frozenset({"QgsProcessingParameterRasterLayer"}),
    MULTI_RASTER: frozenset({"QgsProcessingParameterMultipleLayers"}),
    FIELD: frozenset({"QgsProcessingParameterField"}),
    NUMBER: frozenset({"QgsProcessingParameterNumber"}),
    # A Distance parameter is-a Number, so either class name satisfies it.
    DISTANCE: frozenset({"QgsProcessingParameterDistance", "QgsProcessingParameterNumber"}),
    BOOL: frozenset({"QgsProcessingParameterBoolean"}),
    ENUM: frozenset({"QgsProcessingParameterEnum"}),
    CRS: frozenset({"QgsProcessingParameterCrs"}),
    STRING_LABEL: frozenset({"QgsProcessingParameterString"}),
}

# Blocked id terms mirrored from AlgorithmCatalog.AI_BLOCKED_ID_TERMS so the
# policy can fail closed without importing the QGIS-bound catalog.
_BLOCKED_ID_TERMS = ("command", "download", "executesql", "execute_sql", "shell")


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
    return bool(accepted & param.type_names)


def _id_has_blocked_term(algorithm_id: str) -> bool:
    normalized = algorithm_id.lower().replace("-", "_")
    return any(term in normalized for term in _BLOCKED_ID_TERMS)


class SafeAlgorithmPolicy:
    """Deny-by-default gate over a fixed, reviewed native-algorithm allowlist."""

    def __init__(self, allowlist: Optional[Mapping[str, AllowedAlgorithm]] = None) -> None:
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

    def is_runnable(self, algorithm_id: str, params: Sequence[ParamSpec]) -> PolicyDecision:
        """Return an allow/deny decision for a live algorithm's current signature.

        ``params`` is the live parameter set viewed as :class:`ParamSpec`. The
        caller must already have confirmed the algorithm exists in the live
        registry. Deny reasons never leak parameter detail.
        """
        record = self._allowed.get(algorithm_id)
        if record is None:
            return _deny(ProposalReason.ALGORITHM_NOT_ALLOWED)
        if _id_has_blocked_term(algorithm_id):
            return _deny(ProposalReason.ALGORITHM_NOT_ALLOWED)

        by_name = {param.name: param for param in params}

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

        known = set(record.bindable) | set(record.destinations)
        for pname, param in by_name.items():
            # An unpinned destination (e.g. a newly added file/HTML output) is a
            # signature drift: deny until individually reviewed.
            if param.is_destination and pname not in record.destinations:
                return _deny(ProposalReason.SIGNATURE_MISMATCH)
            if pname in known:
                continue
            # A newly added parameter we do not bind, which is mandatory and has
            # no default, changes the run contract: deny until reviewed.
            if not param.is_optional and not param.has_default:
                return _deny(ProposalReason.SIGNATURE_MISMATCH)

        return PolicyDecision(allowed=True, record=record)


# -- the shipped, reviewed initial allowlist (owner decision 2026-07-23) -----
# Focused core of twelve native algorithms; signatures probed live on QGIS
# 4.2.0 and 3.44.12 LTR. Bindable holds only the safe, cross-version inputs a
# proposal may set; every destination is forced to a temporary output.

def _alg(
    algorithm_id: str,
    bindable: Mapping[str, str],
    required: Tuple[str, ...],
    destinations: Tuple[str, ...] = ("OUTPUT",),
) -> AllowedAlgorithm:
    return AllowedAlgorithm(
        algorithm_id=algorithm_id,
        bindable=dict(bindable),
        required_layer_params=required,
        destinations=destinations,
    )


_DEFAULT_ALLOWLIST: Mapping[str, AllowedAlgorithm] = {
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
    return SafeAlgorithmPolicy(_DEFAULT_ALLOWLIST)
