"""Pure, QGIS-free proposal contracts, strict parsers, and render data.

A *proposal* is inert data for human review: a `model_patch` (a set of graph
edit operations) or a `layer_style` (a cartographic-intent brief). This module
owns only the standard-library parsing/validation of the two proposal shapes
and the QGIS-free structural application of a model patch onto a *detached*
graph clone. It never touches a live QgsProject, layer, renderer, or the live
SmartModeler graph, never applies anything, and never resolves a context token
-- those belong to the trusted runtime boundary (`runtime_proposals.py`).

Everything here fails closed: an unknown kind, an extra/missing key at any
depth, a duplicate key, a non-finite number, an out-of-bounds string/list, an
invalid id/colour, or an unknown operation rejects the whole proposal. No
provider object is ever stored or returned; every public ``to_dict()`` is a
freshly built, detached, JSON-compatible tree.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .contracts import ContractError, validate_json_value
from ..graph_model import GraphModel, GraphValidationError, NodeDefinition, SocketType

# -- kinds -----------------------------------------------------------------

PROPOSAL_KIND_NONE = "none"
PROPOSAL_KIND_MODEL_PATCH = "model_patch"
PROPOSAL_KIND_LAYER_STYLE = "layer_style"
PROPOSAL_KIND_PROCESSING_RUN = "processing_run"
PROPOSAL_KIND_MODEL_RUN = "model_run"
ALL_PROPOSAL_KINDS: Tuple[str, ...] = (
    PROPOSAL_KIND_NONE,
    PROPOSAL_KIND_MODEL_PATCH,
    PROPOSAL_KIND_LAYER_STYLE,
    PROPOSAL_KIND_PROCESSING_RUN,
    PROPOSAL_KIND_MODEL_RUN,
)
PROPOSABLE_KINDS: Tuple[str, ...] = (
    PROPOSAL_KIND_MODEL_PATCH,
    PROPOSAL_KIND_LAYER_STYLE,
    PROPOSAL_KIND_PROCESSING_RUN,
    PROPOSAL_KIND_MODEL_RUN,
)


class ProposalReason:
    """Stable, user-safe reason codes for every controlled proposal failure."""

    NOT_ALLOWED_IN_ASK = "proposal_not_allowed_in_ask"
    SCOPE_MISMATCH = "proposal_scope_mismatch"
    UNKNOWN_KIND = "unknown_proposal_kind"
    MALFORMED = "malformed_proposal"
    STALE_CONTEXT = "stale_proposal_context"
    TARGET_MISSING = "proposal_target_missing"
    VALIDATION_FAILED = "proposal_validation_failed"
    LIMIT_EXCEEDED = "proposal_limit_exceeded"
    # Phase 05 -- safe Processing / current-model execution.
    ALGORITHM_NOT_ALLOWED = "proposal_algorithm_not_allowed"
    UNSAFE_PARAMETER = "proposal_unsafe_parameter"
    SIGNATURE_MISMATCH = "proposal_signature_mismatch"
    EXECUTION_FAILED = "proposal_execution_failed"
    EXECUTION_CANCELED = "proposal_execution_canceled"
    RUN_IN_PROGRESS = "proposal_run_in_progress"


class ProposalError(ValueError):
    """Raised for a controlled proposal failure, carrying a stable reason code."""

    def __init__(self, message: str, reason_code: str = ProposalReason.MALFORMED) -> None:
        super().__init__(message)
        self.reason_code = reason_code


# -- bounds ----------------------------------------------------------------

MAX_PROPOSAL_JSON_CHARS = 60_000
MIN_TITLE_CHARS = 1
MAX_TITLE_CHARS = 160
MIN_SUMMARY_CHARS = 1
MAX_SUMMARY_CHARS = 2_000
MAX_WARNINGS = 20
MAX_WARNING_CHARS = 500
MAX_OPERATIONS = 40
MAX_TOKEN_CHARS = 256
MAX_PARAMS_PER_NODE = 60
MAX_PARAM_NAME_CHARS = 100
MAX_PARAM_STRING_CHARS = 10_000
MAX_PARAM_LIST_ITEMS = 200
MAX_PARAM_LIST_STRING_CHARS = 2_000
MAX_ALGORITHM_ID_CHARS = 200
MAX_LAYER_ID_CHARS = 200
MAX_FIELD_CHARS = 128
MAX_PALETTE_COLORS = 12
MIN_CLASSES = 2
MAX_CLASSES = 12

# Phase 05 run-proposal bounds.
MAX_RUN_BINDINGS = 30
MAX_RUN_LAYERS = 25
MAX_RUN_STRING_CHARS = 2_000
MAX_CRS_CHARS = 64
MAX_ENUM_INDEX = 255
MAX_RUN_NUMBER_ABS = 1e12

ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
# A Processing algorithm id -- exactly ``provider:name``; never path/URI-shaped.
ALG_ID_PATTERN = re.compile(r"^[A-Za-z0-9]+:[A-Za-z0-9_]+$")
# A CRS authority id such as ``EPSG:3857``, ``ESRI:102100``, ``OGC:CRS84``.
CRS_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9]*:[A-Za-z0-9.:\-]+$")

_ALLOWED_RENDERER_FAMILIES = (
    "keep",
    "single_symbol",
    "categorized",
    "graduated",
    "raster_gray",
    "raster_pseudocolor",
    "raster_multiband",
)
_VECTOR_FAMILIES = frozenset(
    {"keep", "single_symbol", "categorized", "graduated"}
)
_RASTER_FAMILIES = frozenset(
    {"raster_gray", "raster_pseudocolor", "raster_multiband"}
)
_FIELD_REQUIRED_FAMILIES = frozenset({"categorized", "graduated"})


# -- strict JSON ------------------------------------------------------------


def _reject_duplicate_keys(pairs):
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProposalError(f"duplicate object key: {key!r}", ProposalReason.MALFORMED)
        result[key] = value
    return result


def _strict_object(text: str) -> Dict[str, Any]:
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except RecursionError:
        raise ProposalError(
            "Proposal JSON nesting is too deep to parse safely.", ProposalReason.MALFORMED
        ) from None
    except ValueError as error:
        raise ProposalError(
            f"Proposal was not valid JSON: {error}", ProposalReason.MALFORMED
        ) from error
    if not isinstance(data, dict):
        raise ProposalError("A proposal must be a single JSON object.", ProposalReason.MALFORMED)
    try:
        validate_json_value(
            data, max_string_length=MAX_PARAM_STRING_CHARS, max_total_chars=MAX_PROPOSAL_JSON_CHARS
        )
    except ContractError as error:
        raise ProposalError(str(error), ProposalReason.MALFORMED) from error
    return data


def _require_exact_keys(value: Dict[str, Any], expected: set, label: str) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if extra:
            details.append("unexpected " + ", ".join(extra))
        raise ProposalError(f"Invalid {label} fields: {'; '.join(details)}.", ProposalReason.MALFORMED)


def _bounded_text(value: Any, label: str, minimum: int, maximum: int) -> str:
    if not isinstance(value, str):
        raise ProposalError(f"{label} must be text.", ProposalReason.MALFORMED)
    if len(value) < minimum or len(value) > maximum:
        raise ProposalError(
            f"{label} must be {minimum}..{maximum} characters.", ProposalReason.MALFORMED
        )
    return value


def _token(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > MAX_TOKEN_CHARS:
        raise ProposalError("Missing or invalid context_token.", ProposalReason.MALFORMED)
    return value


def _schema_version(value: Any) -> int:
    # Exact integer singleton 1 only: a bool, a float (e.g. 1.0), or a string
    # "1" all fail closed rather than being coerced.
    if isinstance(value, bool) or not isinstance(value, int) or value != 1:
        raise ProposalError("Only integer schema_version 1 is supported.", ProposalReason.MALFORMED)
    return 1


def _warnings(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, list):
        raise ProposalError("warnings must be an array.", ProposalReason.MALFORMED)
    if len(value) > MAX_WARNINGS:
        raise ProposalError(
            f"warnings exceeds the {MAX_WARNINGS}-item limit.", ProposalReason.LIMIT_EXCEEDED
        )
    out: List[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ProposalError("Every warning must be text.", ProposalReason.MALFORMED)
        # Reject an overlong warning rather than silently slicing it.
        if len(item) > MAX_WARNING_CHARS:
            raise ProposalError(
                f"A warning exceeds the {MAX_WARNING_CHARS}-character limit.",
                ProposalReason.LIMIT_EXCEEDED,
            )
        out.append(item)
    return tuple(out)


def _validate_parameter_value(value: Any) -> Any:
    """Enforce the existing safe graph-planner value subset (no objects/paths
    beyond the bounds already used by ``AiMcpBridge._validate_parameter_value``)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ProposalError("Parameter numbers must be finite.", ProposalReason.MALFORMED)
        return value
    if isinstance(value, str):
        if len(value) > MAX_PARAM_STRING_CHARS:
            raise ProposalError("Parameter text exceeds the safety limit.", ProposalReason.MALFORMED)
        return value
    if isinstance(value, list):
        if len(value) > MAX_PARAM_LIST_ITEMS or not all(
            isinstance(item, str) and len(item) <= MAX_PARAM_LIST_STRING_CHARS for item in value
        ):
            raise ProposalError(
                "Parameter list value has an unsupported type or size.", ProposalReason.MALFORMED
            )
        return list(value)
    raise ProposalError("Parameter value has an unsupported type.", ProposalReason.MALFORMED)


def _parameter_pairs(value: Any) -> Tuple[Tuple[str, Any], ...]:
    if not isinstance(value, list):
        raise ProposalError("Node parameters must be an array.", ProposalReason.MALFORMED)
    if len(value) > MAX_PARAMS_PER_NODE:
        raise ProposalError(
            f"A node exceeds the {MAX_PARAMS_PER_NODE}-parameter limit.", ProposalReason.LIMIT_EXCEEDED
        )
    seen: set = set()
    pairs: List[Tuple[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ProposalError("Every parameter must be an object.", ProposalReason.MALFORMED)
        _require_exact_keys(item, {"name", "value"}, "parameter")
        name = _bounded_text(item["name"], "parameter name", 1, MAX_PARAM_NAME_CHARS)
        if name in seen:
            raise ProposalError(f"Duplicate parameter name: {name}.", ProposalReason.MALFORMED)
        seen.add(name)
        pairs.append((name, _validate_parameter_value(item["value"])))
    return tuple(pairs)


# -- model-patch operation contracts ---------------------------------------


@dataclass(frozen=True)
class _Operation:
    """Base marker for a single validated, inert model-patch operation."""

    op: str = ""

    @property
    def is_destructive(self) -> bool:
        return False


@dataclass(frozen=True)
class AddNodeOp(_Operation):
    node_id: str = ""
    algorithm_id: str = ""
    title: str = ""
    parameters: Tuple[Tuple[str, Any], ...] = ()

    def apply(self, graph: GraphModel, catalog: Any) -> None:
        if self.node_id in graph.nodes:
            raise ProposalError(
                f"Duplicate node id in the patch: {self.node_id}.", ProposalReason.VALIDATION_FAILED
            )
        if not catalog.algorithm_exists(self.algorithm_id):
            raise ProposalError(
                f"Unavailable algorithm: {self.algorithm_id}.", ProposalReason.VALIDATION_FAILED
            )
        if not catalog.ai_algorithm_allowed(self.algorithm_id):
            raise ProposalError(
                f"Restricted algorithm: {self.algorithm_id}.", ProposalReason.VALIDATION_FAILED
            )
        node = catalog.create_node(self.algorithm_id, self.node_id, self.title)
        for name, value in self.parameters:
            _bind_parameter(node, name, value, catalog)
        try:
            graph.add_node(node)
        except GraphValidationError as error:
            raise ProposalError(str(error), ProposalReason.VALIDATION_FAILED) from error

    def describe(self) -> str:
        params = ", ".join(f"{name}={_short_value(value)}" for name, value in self.parameters)
        detail = f" [{params}]" if params else ""
        return f"Add node '{self.title}' ({self.algorithm_id}) as {self.node_id}{detail}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op": "add_node",
            "node_id": self.node_id,
            "algorithm_id": self.algorithm_id,
            "title": self.title,
            "parameters": [{"name": name, "value": value} for name, value in self.parameters],
        }


@dataclass(frozen=True)
class RemoveNodeOp(_Operation):
    node_id: str = ""

    @property
    def is_destructive(self) -> bool:
        return True

    def apply(self, graph: GraphModel, catalog: Any) -> None:
        if self.node_id not in graph.nodes:
            raise ProposalError(
                f"Unknown node: {self.node_id}.", ProposalReason.VALIDATION_FAILED
            )
        graph.remove_node(self.node_id)

    def describe(self) -> str:
        return f"Remove node {self.node_id} (destructive if applied)"

    def to_dict(self) -> Dict[str, Any]:
        return {"op": "remove_node", "node_id": self.node_id}


@dataclass(frozen=True)
class SetParameterOp(_Operation):
    node_id: str = ""
    name: str = ""
    value: Any = None

    def apply(self, graph: GraphModel, catalog: Any) -> None:
        node = graph.nodes.get(self.node_id)
        if node is None:
            raise ProposalError(
                f"Unknown node: {self.node_id}.", ProposalReason.VALIDATION_FAILED
            )
        _bind_parameter(node, self.name, self.value, catalog)

    def describe(self) -> str:
        return f"Set parameter {self.name}={_short_value(self.value)} on {self.node_id}"

    def to_dict(self) -> Dict[str, Any]:
        return {"op": "set_parameter", "node_id": self.node_id, "name": self.name, "value": self.value}


@dataclass(frozen=True)
class ConnectOp(_Operation):
    from_node: str = ""
    from_output: str = ""
    to_node: str = ""
    to_input: str = ""

    def apply(self, graph: GraphModel, catalog: Any) -> None:
        edge = graph.add_edge(self.from_node, self.from_output, self.to_node, self.to_input)
        if edge is None:
            raise ProposalError(
                f"Invalid connection {self.from_node}.{self.from_output} -> "
                f"{self.to_node}.{self.to_input}: {graph.last_error}",
                ProposalReason.VALIDATION_FAILED,
            )

    def describe(self) -> str:
        return (
            f"Connect {self.from_node}.{self.from_output} -> {self.to_node}.{self.to_input}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "op": "connect",
            "from_node": self.from_node,
            "from_output": self.from_output,
            "to_node": self.to_node,
            "to_input": self.to_input,
        }


@dataclass(frozen=True)
class DisconnectOp(_Operation):
    edge_id: str = ""

    @property
    def is_destructive(self) -> bool:
        return True

    def apply(self, graph: GraphModel, catalog: Any) -> None:
        if self.edge_id not in graph.edges:
            raise ProposalError(
                f"Unknown edge: {self.edge_id}.", ProposalReason.VALIDATION_FAILED
            )
        graph.remove_edge(self.edge_id)

    def describe(self) -> str:
        return f"Disconnect edge {self.edge_id} (destructive if applied)"

    def to_dict(self) -> Dict[str, Any]:
        return {"op": "disconnect", "edge_id": self.edge_id}


@dataclass(frozen=True)
class RenameNodeOp(_Operation):
    node_id: str = ""
    title: str = ""

    def apply(self, graph: GraphModel, catalog: Any) -> None:
        node = graph.nodes.get(self.node_id)
        if node is None:
            raise ProposalError(
                f"Unknown node: {self.node_id}.", ProposalReason.VALIDATION_FAILED
            )
        node.title = self.title

    def describe(self) -> str:
        return f"Rename node {self.node_id} to '{self.title}'"

    def to_dict(self) -> Dict[str, Any]:
        return {"op": "rename_node", "node_id": self.node_id, "title": self.title}


@dataclass(frozen=True)
class SetModelMetadataOp(_Operation):
    name: str = ""
    description: str = ""

    def apply(self, graph: GraphModel, catalog: Any) -> None:
        graph.name = self.name
        graph.description = self.description

    def describe(self) -> str:
        return f"Set model metadata (name='{self.name}')"

    def to_dict(self) -> Dict[str, Any]:
        return {"op": "set_model_metadata", "name": self.name, "description": self.description}


# Path/URI/connection/credential-shaped text that a Phase 03 proposal must
# never introduce into a parameter value. All checks are conservative and never
# echo the offending value back into a preview or error message.
_ABS_WINDOWS_PATH = re.compile(r"^[A-Za-z]:[\\/]")
# Any syntactically valid URI scheme prefix (``scheme:``), whether or not it is
# followed by ``//``. This deliberately catches hierarchical schemes such as
# ``https:`` and ``file:`` as well as opaque ones such as ``mailto:``, ``ssh:``,
# ``urn:`` and ``data:``. A leading drive letter (``C:``) is caught here too,
# which is harmless because such values are never legitimate parameter content.
_URI_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
# A ``..`` path-traversal segment anywhere in a relative path form.
_RELATIVE_TRAVERSAL = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
_CONNECTION_FRAGMENT = re.compile(
    r"(?i)(?:^|[;&\s])(password|passwd|pwd|user|uid|host|hostaddr|server|"
    r"dbname|sslmode|port|account|api[_-]?key|apikey|secret|token|auth|"
    r"access[_-]?key)\s*="
)
_CREDENTIAL_WORD = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|apikey|auth|"
    r"credential|access[_-]?key|private[_-]?key)\b"
)
# Any ASCII control character (the C0 range plus DEL). A parameter value must be
# plain single-line text; a newline/carriage-return/tab/NUL/DEL can otherwise
# smuggle a path or URI onto a later line past a character-zero-anchored check.
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
# Any path separator. Rejecting both forms also rejects ordinary relative
# filesystem values (e.g. ``folder/private.gpkg`` or ``folder\private.gpkg``)
# that are not otherwise absolute- or scheme-shaped.
_PATH_SEPARATOR = re.compile(r"[\\/]")


def _looks_like_path_or_uri(text: str) -> bool:
    if _ABS_WINDOWS_PATH.match(text):
        return True
    if text.startswith("\\\\") or text.startswith("//"):  # UNC / protocol-relative
        return True
    if text.startswith("/") or text.startswith("./") or text.startswith(".\\"):  # POSIX / relative
        return True
    if _RELATIVE_TRAVERSAL.search(text):  # a ``..`` traversal segment
        return True
    # Any URI scheme prefix, including opaque schemes without ``//`` such as
    # ``mailto:``, ``ssh:``, ``urn:``, ``data:`` and ``file:``.
    return bool(_URI_SCHEME.match(text))


def _is_unsafe_text(text: str) -> bool:
    """Return whether ``text`` is path/URI/connection/credential-shaped."""
    return (
        _looks_like_path_or_uri(text)
        or bool(_CONNECTION_FRAGMENT.search(text))
        or bool(_CREDENTIAL_WORD.search(text))
    )


def _is_credential_name(name: str) -> bool:
    return bool(_CREDENTIAL_WORD.search(name))


def _short_value(value: Any) -> str:
    """Render an *already-validated* safe value for a preview line.

    Never called for a rejected value: ``build_model_patch_preview`` only calls
    ``describe()`` after ``apply()`` accepted every operation, so no unsafe
    string reaches a preview through here.
    """
    if isinstance(value, str):
        return value[:120]
    if isinstance(value, list):
        return "[" + ", ".join(str(item)[:40] for item in value[:6]) + "]"
    return str(value)[:120]


def _reject_value(reason: str = "This parameter value is not permitted.") -> None:
    # Deliberately never includes the offending value.
    raise ProposalError(reason, ProposalReason.VALIDATION_FAILED)


def _require_safe_text(value: Any, maximum: int) -> None:
    if not isinstance(value, str):
        _reject_value("A text parameter value is required.")
    if len(value) > maximum:
        _reject_value("A parameter value exceeds the safety limit.")
    # Reject any ASCII control character (newline, carriage return, tab, NUL,
    # DEL, ...) so a multi-line value cannot carry a path/URI on a later line.
    if _CONTROL_CHARS.search(value):
        _reject_value("Control characters are not permitted in a parameter value.")
    # Classify on a whitespace-trimmed *inspection* form so leading/trailing
    # whitespace cannot slip a scheme or absolute path past the checks. The
    # provider value is only inspected here, never silently normalized and
    # accepted: the original value is still what gets rejected.
    inspection = value.strip()
    if _is_unsafe_text(inspection):
        _reject_value("Path, URI, connection, or credential values are not permitted.")
    # Conservative Phase 03 rule: reject any path separator, which also rejects
    # ordinary relative filesystem-looking values in STRING/FIELD/ANY text.
    if _PATH_SEPARATOR.search(inspection):
        _reject_value("Path-separator characters are not permitted in a parameter value.")


def _require_layer_binding(value: Any, expected: str, allows_multiple: bool, catalog: Any) -> None:
    choices = catalog.layer_choices(expected)

    def _check_one(candidate: Any) -> None:
        if not isinstance(candidate, str) or not candidate.strip():
            _reject_value("A project layer id is required for this input.")
        if candidate not in choices:
            _reject_value("A layer id does not resolve to a compatible project layer.")

    if allows_multiple:
        if not isinstance(value, list):
            _reject_value("A list of project layer ids is required for this input.")
        if not value or len(value) > MAX_PARAM_LIST_ITEMS:
            _reject_value("An out-of-range list of project layer ids was supplied.")
        for candidate in value:
            _check_one(candidate)
    else:
        # An empty string clears the input, which is safe; any other value must
        # resolve to a compatible live layer.
        if isinstance(value, str) and not value.strip():
            return
        _check_one(value)


def _validate_by_socket(value: Any, socket: str, allows_multiple: bool, catalog: Any) -> None:
    if socket == SocketType.FILE:
        _reject_value("File/path parameters are not permitted in a Phase 03 proposal.")
    elif socket == SocketType.NUMBER:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _reject_value("A numeric parameter value is required.")
        if isinstance(value, float) and value != value:  # NaN
            _reject_value("A numeric parameter value must be finite.")
    elif socket == SocketType.BOOLEAN:
        if not isinstance(value, bool):
            _reject_value("A boolean parameter value is required.")
    elif socket == SocketType.FIELD:
        _require_safe_text(value, MAX_FIELD_CHARS)
    elif socket == SocketType.STRING:
        _require_safe_text(value, MAX_PARAM_STRING_CHARS)
    elif socket in (SocketType.VECTOR, SocketType.RASTER):
        expected = "raster" if socket == SocketType.RASTER else "vector"
        _require_layer_binding(value, expected, allows_multiple, catalog)
    else:  # SocketType.ANY, SocketType.TABLE, or any future scalar socket
        if value is None or isinstance(value, bool) or isinstance(value, int):
            return
        if isinstance(value, float):
            if value != value:
                _reject_value("A numeric parameter value must be finite.")
            return
        if isinstance(value, str):
            _require_safe_text(value, MAX_PARAM_STRING_CHARS)
            return
        _reject_value("This parameter value type is not permitted.")


# Off-port synthetic parameters are permitted only for the exact trusted
# ``smart:*`` node contract that declares them. There is deliberately no ``ANY``
# fallback for an arbitrary algorithm, so ``LAYER``/``VALUE`` (or any other
# unknown name) is rejected on a native or unrelated node.
_SMART_OFF_PORT_PARAMETERS = frozenset(
    {
        ("smart:input_layer", "LAYER"),
        ("smart:raster_layer", "LAYER"),
        ("smart:number", "VALUE"),
        ("smart:slider", "VALUE"),
    }
)


def _bind_parameter(node: NodeDefinition, name: str, value: Any, catalog: Any) -> None:
    """Set one validated parameter on a *detached-clone* node.

    Enforces the target input socket's value type and refuses unknown parameter
    names, file/path/URI/connection/credential values, credential-like parameter
    names, and layer bindings that do not resolve to a compatible live project
    layer -- for native algorithm inputs as well as the ``smart:*`` synthetic
    inputs. A name that exists neither as a real input port nor as an exact
    trusted ``smart:*`` off-port contract is rejected with a fixed safe error
    that never echoes the unknown/unsafe parameter name.
    """
    if _is_credential_name(name):
        _reject_value("A credential-like parameter is not permitted.")
    port = node.inputs.get(name)
    if port is None:
        if (node.algorithm_id, name) not in _SMART_OFF_PORT_PARAMETERS:
            # Fixed safe error: never interpolate the (possibly path/URI-shaped)
            # unknown parameter name into the message.
            _reject_value("This parameter is not permitted on the target node.")
        _validate_smart_parameter(node, name, value, catalog)
    else:
        _validate_by_socket(value, port.socket_type, bool(port.allows_multiple), catalog)
    node.parameters[name] = value


def _validate_smart_parameter(node: NodeDefinition, name: str, value: Any, catalog: Any) -> None:
    """Validate a synthetic ``LAYER``/``VALUE`` parameter on a ``smart:*`` node.

    Only ever reached for the exact ``(algorithm_id, name)`` pairs in
    ``_SMART_OFF_PORT_PARAMETERS``; still fails closed for any other pair.
    """
    if node.algorithm_id in ("smart:input_layer", "smart:raster_layer") and name == "LAYER":
        expected = "raster" if node.algorithm_id == "smart:raster_layer" else "vector"
        _require_layer_binding(value, expected, False, catalog)
        return
    if node.algorithm_id in ("smart:number", "smart:slider") and name == "VALUE":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            _reject_value("A numeric value is required.")
        if isinstance(value, float) and value != value:
            _reject_value("A numeric value must be finite.")
        return
    # Defense in depth: no ANY fallback for an off-contract off-port parameter.
    _reject_value("This parameter is not permitted on the target node.")


_ADD_NODE_KEYS = {"op", "node_id", "algorithm_id", "title", "parameters"}
_REMOVE_NODE_KEYS = {"op", "node_id"}
_SET_PARAMETER_KEYS = {"op", "node_id", "name", "value"}
_CONNECT_KEYS = {"op", "from_node", "from_output", "to_node", "to_input"}
_DISCONNECT_KEYS = {"op", "edge_id"}
_RENAME_NODE_KEYS = {"op", "node_id", "title"}
_SET_MODEL_METADATA_KEYS = {"op", "name", "description"}


def _valid_id(value: Any, label: str, maximum: int = 64) -> str:
    if not isinstance(value, str) or len(value) > maximum or not ID_PATTERN.fullmatch(value):
        raise ProposalError(f"Invalid {label}: {value!r}.", ProposalReason.MALFORMED)
    return value


def _port_id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 100:
        raise ProposalError(f"Invalid {label}.", ProposalReason.MALFORMED)
    return value


def _parse_operation(item: Any) -> _Operation:
    if not isinstance(item, dict):
        raise ProposalError("Every operation must be an object.", ProposalReason.MALFORMED)
    op = item.get("op")
    if op == "add_node":
        _require_exact_keys(item, _ADD_NODE_KEYS, "add_node operation")
        return AddNodeOp(
            op="add_node",
            node_id=_valid_id(item["node_id"], "node id"),
            algorithm_id=_bounded_text(item["algorithm_id"], "algorithm_id", 1, MAX_ALGORITHM_ID_CHARS),
            title=_bounded_text(item["title"], "node title", MIN_TITLE_CHARS, MAX_TITLE_CHARS),
            parameters=_parameter_pairs(item["parameters"]),
        )
    if op == "remove_node":
        _require_exact_keys(item, _REMOVE_NODE_KEYS, "remove_node operation")
        return RemoveNodeOp(op="remove_node", node_id=_valid_id(item["node_id"], "node id"))
    if op == "set_parameter":
        _require_exact_keys(item, _SET_PARAMETER_KEYS, "set_parameter operation")
        return SetParameterOp(
            op="set_parameter",
            node_id=_valid_id(item["node_id"], "node id"),
            name=_bounded_text(item["name"], "parameter name", 1, MAX_PARAM_NAME_CHARS),
            value=_validate_parameter_value(item["value"]),
        )
    if op == "connect":
        _require_exact_keys(item, _CONNECT_KEYS, "connect operation")
        return ConnectOp(
            op="connect",
            from_node=_valid_id(item["from_node"], "from_node"),
            from_output=_port_id(item["from_output"], "from_output"),
            to_node=_valid_id(item["to_node"], "to_node"),
            to_input=_port_id(item["to_input"], "to_input"),
        )
    if op == "disconnect":
        _require_exact_keys(item, _DISCONNECT_KEYS, "disconnect operation")
        edge_id = item["edge_id"]
        if not isinstance(edge_id, str) or not edge_id or len(edge_id) > 300:
            raise ProposalError("Invalid edge_id.", ProposalReason.MALFORMED)
        return DisconnectOp(op="disconnect", edge_id=edge_id)
    if op == "rename_node":
        _require_exact_keys(item, _RENAME_NODE_KEYS, "rename_node operation")
        return RenameNodeOp(
            op="rename_node",
            node_id=_valid_id(item["node_id"], "node id"),
            title=_bounded_text(item["title"], "node title", MIN_TITLE_CHARS, MAX_TITLE_CHARS),
        )
    if op == "set_model_metadata":
        _require_exact_keys(item, _SET_MODEL_METADATA_KEYS, "set_model_metadata operation")
        return SetModelMetadataOp(
            op="set_model_metadata",
            name=_bounded_text(item["name"], "model name", MIN_TITLE_CHARS, MAX_TITLE_CHARS),
            description=_bounded_text(item["description"], "model description", 0, MAX_SUMMARY_CHARS),
        )
    raise ProposalError(f"Unknown operation: {op!r}.", ProposalReason.MALFORMED)


@dataclass(frozen=True)
class ModelPatchProposal:
    """A validated, inert model-patch proposal draft (no live graph access)."""

    context_token: str
    title: str
    summary: str
    operations: Tuple[_Operation, ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = 1

    @property
    def kind(self) -> str:
        return PROPOSAL_KIND_MODEL_PATCH

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": PROPOSAL_KIND_MODEL_PATCH,
            "title": self.title,
            "summary": self.summary,
            "operations": [op.to_dict() for op in self.operations],
            "warnings": list(self.warnings),
        }


_MODEL_PATCH_KEYS = {
    "schema_version",
    "context_token",
    "title",
    "summary",
    "operations",
    "warnings",
}


def _parse_model_patch(data: Dict[str, Any]) -> ModelPatchProposal:
    _require_exact_keys(data, _MODEL_PATCH_KEYS, "model_patch")
    operations_data = data["operations"]
    if not isinstance(operations_data, list):
        raise ProposalError("operations must be an array.", ProposalReason.MALFORMED)
    if len(operations_data) > MAX_OPERATIONS:
        raise ProposalError(
            f"operations exceeds the {MAX_OPERATIONS}-item limit.", ProposalReason.LIMIT_EXCEEDED
        )
    if not operations_data:
        raise ProposalError("A model_patch must contain at least one operation.", ProposalReason.MALFORMED)
    operations = tuple(_parse_operation(item) for item in operations_data)
    return ModelPatchProposal(
        schema_version=_schema_version(data["schema_version"]),
        context_token=_token(data["context_token"]),
        title=_bounded_text(data["title"], "title", MIN_TITLE_CHARS, MAX_TITLE_CHARS),
        summary=_bounded_text(data["summary"], "summary", MIN_SUMMARY_CHARS, MAX_SUMMARY_CHARS),
        operations=operations,
        warnings=_warnings(data["warnings"]),
    )


# -- layer-style contracts --------------------------------------------------


@dataclass(frozen=True)
class RendererIntent:
    family: str
    field: str
    class_count: int
    palette: Tuple[str, ...]
    opacity: float

    def to_dict(self) -> Dict[str, Any]:
        return {
            "family": self.family,
            "field": self.field,
            "class_count": self.class_count,
            "palette": list(self.palette),
            "opacity": self.opacity,
        }


@dataclass(frozen=True)
class LabelIntent:
    enabled: bool
    field: str

    def to_dict(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "field": self.field}


@dataclass(frozen=True)
class LayerStyleProposal:
    context_token: str
    target_layer_id: str
    title: str
    summary: str
    renderer: RendererIntent
    labels: LabelIntent
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = 1

    @property
    def kind(self) -> str:
        return PROPOSAL_KIND_LAYER_STYLE

    @property
    def is_vector_family(self) -> bool:
        return self.renderer.family in _VECTOR_FAMILIES

    @property
    def is_raster_family(self) -> bool:
        return self.renderer.family in _RASTER_FAMILIES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": PROPOSAL_KIND_LAYER_STYLE,
            "target_layer_id": self.target_layer_id,
            "title": self.title,
            "summary": self.summary,
            "renderer": self.renderer.to_dict(),
            "labels": self.labels.to_dict(),
            "warnings": list(self.warnings),
        }


_LAYER_STYLE_KEYS = {
    "schema_version",
    "context_token",
    "target_layer_id",
    "title",
    "summary",
    "renderer",
    "labels",
    "warnings",
}
_RENDERER_KEYS = {"family", "field", "class_count", "palette", "opacity"}
_LABELS_KEYS = {"enabled", "field"}


def _field_name(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise ProposalError(f"{label} must be text.", ProposalReason.MALFORMED)
    if len(value) > MAX_FIELD_CHARS:
        raise ProposalError(f"{label} exceeds the field-name length limit.", ProposalReason.MALFORMED)
    return value


def _class_count(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_CLASSES:
        raise ProposalError("class_count is out of range.", ProposalReason.MALFORMED)
    return value


def _opacity(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProposalError("opacity must be a number.", ProposalReason.MALFORMED)
    number = float(value)
    if number != number or number < 0.0 or number > 1.0:
        raise ProposalError("opacity must be between 0.0 and 1.0.", ProposalReason.MALFORMED)
    return number


def _palette(value: Any) -> Tuple[str, ...]:
    from .context import normalize_hex_color

    if not isinstance(value, list):
        raise ProposalError("palette must be an array.", ProposalReason.MALFORMED)
    if len(value) > MAX_PALETTE_COLORS:
        raise ProposalError(
            f"palette exceeds the {MAX_PALETTE_COLORS}-colour limit.", ProposalReason.LIMIT_EXCEEDED
        )
    colors: List[str] = []
    for item in value:
        normalized = normalize_hex_color(item)
        if normalized is None:
            raise ProposalError(
                "palette colours must be exactly #RRGGBB or #RRGGBBAA.", ProposalReason.MALFORMED
            )
        colors.append(normalized)
    return tuple(colors)


def _parse_renderer(data: Any) -> RendererIntent:
    if not isinstance(data, dict):
        raise ProposalError("renderer must be an object.", ProposalReason.MALFORMED)
    _require_exact_keys(data, _RENDERER_KEYS, "renderer")
    family = data["family"]
    if family not in _ALLOWED_RENDERER_FAMILIES:
        raise ProposalError(f"Unknown renderer family: {family!r}.", ProposalReason.MALFORMED)
    renderer = RendererIntent(
        family=family,
        field=_field_name(data["field"], "renderer field"),
        class_count=_class_count(data["class_count"]),
        palette=_palette(data["palette"]),
        opacity=_opacity(data["opacity"]),
    )
    _validate_renderer_shape(renderer)
    return renderer


def _validate_renderer_shape(renderer: RendererIntent) -> None:
    family = renderer.family
    field_present = bool(renderer.field)
    count = renderer.class_count
    palette_len = len(renderer.palette)
    if family == "keep":
        if field_present or count != 0 or palette_len != 0:
            raise ProposalError(
                "keep requires no field, class_count 0, and an empty palette.",
                ProposalReason.MALFORMED,
            )
    elif family == "single_symbol":
        if field_present or count != 1 or palette_len != 1:
            raise ProposalError(
                "single_symbol requires no field, class_count 1, and one palette colour.",
                ProposalReason.MALFORMED,
            )
    elif family in _FIELD_REQUIRED_FAMILIES:
        if not field_present:
            raise ProposalError(f"{family} requires a field.", ProposalReason.MALFORMED)
        if count < MIN_CLASSES or count > MAX_CLASSES or palette_len != count:
            raise ProposalError(
                f"{family} requires class_count 2..12 and a matching palette length.",
                ProposalReason.MALFORMED,
            )
    elif family == "raster_pseudocolor":
        if field_present or count < MIN_CLASSES or count > MAX_CLASSES or palette_len != count:
            raise ProposalError(
                "raster_pseudocolor requires no field, class_count 2..12, and a matching palette.",
                ProposalReason.MALFORMED,
            )
    else:  # raster_gray, raster_multiband
        if field_present or count != 0 or palette_len != 0:
            raise ProposalError(
                f"{family} requires no field, class_count 0, and an empty palette.",
                ProposalReason.MALFORMED,
            )


def _parse_labels(data: Any) -> LabelIntent:
    if not isinstance(data, dict):
        raise ProposalError("labels must be an object.", ProposalReason.MALFORMED)
    _require_exact_keys(data, _LABELS_KEYS, "labels")
    enabled = data["enabled"]
    if not isinstance(enabled, bool):
        raise ProposalError("labels.enabled must be a boolean.", ProposalReason.MALFORMED)
    field_value = _field_name(data["field"], "label field")
    if enabled and not field_value:
        raise ProposalError("Enabled labels require a field.", ProposalReason.MALFORMED)
    if not enabled and field_value:
        raise ProposalError("Disabled labels require an empty field.", ProposalReason.MALFORMED)
    return LabelIntent(enabled=enabled, field=field_value)


def _parse_layer_style(data: Dict[str, Any]) -> LayerStyleProposal:
    _require_exact_keys(data, _LAYER_STYLE_KEYS, "layer_style")
    proposal = LayerStyleProposal(
        schema_version=_schema_version(data["schema_version"]),
        context_token=_token(data["context_token"]),
        target_layer_id=_bounded_text(data["target_layer_id"], "target_layer_id", 1, MAX_LAYER_ID_CHARS),
        title=_bounded_text(data["title"], "title", MIN_TITLE_CHARS, MAX_TITLE_CHARS),
        summary=_bounded_text(data["summary"], "summary", MIN_SUMMARY_CHARS, MAX_SUMMARY_CHARS),
        renderer=_parse_renderer(data["renderer"]),
        labels=_parse_labels(data["labels"]),
        warnings=_warnings(data["warnings"]),
    )
    if proposal.is_raster_family and proposal.labels.enabled:
        raise ProposalError(
            "Labels may be enabled only for a vector renderer family.", ProposalReason.MALFORMED
        )
    return proposal


# -- run contracts (processing_run / model_run) -----------------------------


def _safe_id_text(value: Any, maximum: int, label: str) -> str:
    """Bound an opaque identifier (project layer id) to plain, non-path text.

    The live runtime validator resolves the id against the project; here we only
    reject an oversized, control-character, path-separator, or URI-scheme value
    so a source path can never masquerade as a layer id.
    """
    if not isinstance(value, str) or not value.strip():
        raise ProposalError(f"{label} is required.", ProposalReason.MALFORMED)
    if len(value) > maximum:
        raise ProposalError(f"{label} exceeds the length limit.", ProposalReason.MALFORMED)
    if _CONTROL_CHARS.search(value):
        raise ProposalError(f"{label} may not contain control characters.", ProposalReason.MALFORMED)
    if _PATH_SEPARATOR.search(value) or _URI_SCHEME.match(value.strip()):
        raise ProposalError(f"{label} must be a plain project identifier.", ProposalReason.VALIDATION_FAILED)
    return value


def _run_number(value: Any, *, allow_negative: bool) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProposalError("A numeric value is required.", ProposalReason.MALFORMED)
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise ProposalError("A numeric value must be finite.", ProposalReason.MALFORMED)
    if abs(number) > MAX_RUN_NUMBER_ABS:
        raise ProposalError("A numeric value is out of range.", ProposalReason.LIMIT_EXCEEDED)
    if not allow_negative and number < 0:
        raise ProposalError("A distance value must not be negative.", ProposalReason.MALFORMED)
    return value


def _enum_index(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > MAX_ENUM_INDEX:
        raise ProposalError("An enum index is out of range.", ProposalReason.MALFORMED)
    return value


def _run_label(value: Any) -> str:
    """A bounded, single-line, path/URI/credential-free label (enum option or
    output column name). Reuses the Phase 03 safe-text rejection rules."""
    _require_safe_text(value, MAX_RUN_STRING_CHARS)
    return value


def _crs_value(value: Any) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_CRS_CHARS:
        raise ProposalError("A CRS authority id is required.", ProposalReason.MALFORMED)
    if not CRS_PATTERN.match(value.strip()):
        raise ProposalError("A CRS must look like AUTHORITY:CODE.", ProposalReason.MALFORMED)
    return value


def _layer_id_list(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, list) or not value or len(value) > MAX_RUN_LAYERS:
        raise ProposalError("An out-of-range list of layer ids was supplied.", ProposalReason.MALFORMED)
    return tuple(_safe_id_text(item, MAX_LAYER_ID_CHARS, "layer id") for item in value)


@dataclass(frozen=True)
class RunBinding:
    """One tagged, inert Processing input binding.

    The mandatory tag keeps a string from ever being reinterpreted as a path or a
    destination: a binding is exactly one of ``layer``/``layers``/``field``/
    ``number``/``bool``/``enum``/``enum_string``/``string``/``crs``/``distance``.
    A destination/output binding cannot be expressed at all -- destinations are
    always application-forced to a temporary output downstream.
    """

    tag: str
    value: Any = None
    layer_param: str = ""

    def to_dict(self) -> Dict[str, Any]:
        if self.tag == "layers":
            return {"layers": list(self.value)}
        if self.tag == "field":
            return {"field": self.value, "layer_param": self.layer_param}
        return {self.tag: self.value}


_BINDING_SINGLE_KEYS = frozenset(
    {"layer", "layers", "number", "bool", "enum", "enum_string", "string", "crs", "distance"}
)
_FIELD_BINDING_KEYS = {"field", "layer_param"}


def _parse_binding(item: Any) -> RunBinding:
    if not isinstance(item, dict) or not item:
        raise ProposalError("Each input binding must be a non-empty object.", ProposalReason.MALFORMED)
    keys = set(item)
    if keys == _FIELD_BINDING_KEYS:
        field_value = _field_name(item["field"], "field binding")
        if not field_value:
            raise ProposalError("A field binding requires a field name.", ProposalReason.MALFORMED)
        _require_safe_text(field_value, MAX_FIELD_CHARS)
        layer_param = _bounded_text(item["layer_param"], "layer_param", 1, MAX_PARAM_NAME_CHARS)
        return RunBinding("field", field_value, layer_param)
    if len(keys) != 1:
        raise ProposalError("An input binding must use exactly one tagged form.", ProposalReason.MALFORMED)
    (tag,) = tuple(keys)
    if tag not in _BINDING_SINGLE_KEYS:
        raise ProposalError("Unknown input binding form.", ProposalReason.MALFORMED)
    value = item[tag]
    if tag == "layer":
        return RunBinding("layer", _safe_id_text(value, MAX_LAYER_ID_CHARS, "layer id"))
    if tag == "layers":
        return RunBinding("layers", _layer_id_list(value))
    if tag == "number":
        return RunBinding("number", _run_number(value, allow_negative=True))
    if tag == "distance":
        return RunBinding("distance", _run_number(value, allow_negative=False))
    if tag == "bool":
        if not isinstance(value, bool):
            raise ProposalError("A boolean value is required.", ProposalReason.MALFORMED)
        return RunBinding("bool", value)
    if tag == "enum":
        return RunBinding("enum", _enum_index(value))
    if tag == "enum_string":
        return RunBinding("enum_string", _run_label(value))
    if tag == "string":
        return RunBinding("string", _run_label(value))
    return RunBinding("crs", _crs_value(value))


def _parse_run_inputs(value: Any) -> Tuple[Tuple[str, RunBinding], ...]:
    if not isinstance(value, dict):
        raise ProposalError("inputs must be an object.", ProposalReason.MALFORMED)
    if len(value) > MAX_RUN_BINDINGS:
        raise ProposalError(
            f"inputs exceeds the {MAX_RUN_BINDINGS}-binding limit.", ProposalReason.LIMIT_EXCEEDED
        )
    bindings: List[Tuple[str, RunBinding]] = []
    for name, item in value.items():
        param = _bounded_text(name, "input parameter name", 1, MAX_PARAM_NAME_CHARS)
        if _is_credential_name(param):
            _reject_value("A credential-like parameter is not permitted.")
        bindings.append((param, _parse_binding(item)))
    return tuple(bindings)


def _algorithm_id(value: Any) -> str:
    text = _bounded_text(value, "algorithm_id", 1, MAX_ALGORITHM_ID_CHARS)
    if not ALG_ID_PATTERN.match(text):
        raise ProposalError("algorithm_id must look like provider:name.", ProposalReason.MALFORMED)
    return text


@dataclass(frozen=True)
class ProcessingRunProposal:
    """A validated, inert request to run exactly one reviewed safe algorithm.

    Carries no destination/output binding and no source path: the algorithm's
    runnability, the live parameter types, and the forced temporary output are
    all decided by the trusted runtime boundary, never by this data.
    """

    context_token: str
    algorithm_id: str
    title: str
    summary: str
    inputs: Tuple[Tuple[str, RunBinding], ...] = field(default_factory=tuple)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = 1

    @property
    def kind(self) -> str:
        return PROPOSAL_KIND_PROCESSING_RUN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": PROPOSAL_KIND_PROCESSING_RUN,
            "algorithm_id": self.algorithm_id,
            "title": self.title,
            "summary": self.summary,
            "inputs": {name: binding.to_dict() for name, binding in self.inputs},
            "warnings": list(self.warnings),
        }


_PROCESSING_RUN_KEYS = {
    "schema_version",
    "context_token",
    "algorithm_id",
    "title",
    "summary",
    "inputs",
    "warnings",
}


def _parse_processing_run(data: Dict[str, Any]) -> ProcessingRunProposal:
    _require_exact_keys(data, _PROCESSING_RUN_KEYS, "processing_run")
    return ProcessingRunProposal(
        schema_version=_schema_version(data["schema_version"]),
        context_token=_token(data["context_token"]),
        algorithm_id=_algorithm_id(data["algorithm_id"]),
        title=_bounded_text(data["title"], "title", MIN_TITLE_CHARS, MAX_TITLE_CHARS),
        summary=_bounded_text(data["summary"], "summary", MIN_SUMMARY_CHARS, MAX_SUMMARY_CHARS),
        inputs=_parse_run_inputs(data["inputs"]),
        warnings=_warnings(data["warnings"]),
    )


@dataclass(frozen=True)
class ModelRunProposal:
    """A validated, inert request to run the *current* SmartModeler graph.

    It names no algorithm and no parameters: which nodes run comes only from the
    live validated graph, so the provider cannot inject nodes/params/destinations
    into a model run.
    """

    context_token: str
    title: str
    summary: str
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    schema_version: int = 1

    @property
    def kind(self) -> str:
        return PROPOSAL_KIND_MODEL_RUN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "kind": PROPOSAL_KIND_MODEL_RUN,
            "title": self.title,
            "summary": self.summary,
            "warnings": list(self.warnings),
        }


_MODEL_RUN_KEYS = {"schema_version", "context_token", "title", "summary", "warnings"}


def _parse_model_run(data: Dict[str, Any]) -> ModelRunProposal:
    _require_exact_keys(data, _MODEL_RUN_KEYS, "model_run")
    return ModelRunProposal(
        schema_version=_schema_version(data["schema_version"]),
        context_token=_token(data["context_token"]),
        title=_bounded_text(data["title"], "title", MIN_TITLE_CHARS, MAX_TITLE_CHARS),
        summary=_bounded_text(data["summary"], "summary", MIN_SUMMARY_CHARS, MAX_SUMMARY_CHARS),
        warnings=_warnings(data["warnings"]),
    )


# -- public entry point -----------------------------------------------------


def parse_proposal(kind: str, proposal_json: str):
    """Parse ``proposal_json`` for ``kind`` into a validated, inert draft.

    Returns a :class:`ModelPatchProposal` or :class:`LayerStyleProposal`.
    Raises :class:`ProposalError` (with a stable reason code) for an unknown
    kind, an oversized/non-object/duplicate-key payload, or any structural
    violation. This never resolves a live layer/graph or a context token.
    """
    if kind not in PROPOSABLE_KINDS:
        raise ProposalError(f"Unknown proposal kind: {kind!r}.", ProposalReason.UNKNOWN_KIND)
    if not isinstance(proposal_json, str):
        raise ProposalError("proposal_json must be a string.", ProposalReason.MALFORMED)
    if len(proposal_json) > MAX_PROPOSAL_JSON_CHARS:
        raise ProposalError(
            f"proposal_json exceeds the {MAX_PROPOSAL_JSON_CHARS}-character limit.",
            ProposalReason.LIMIT_EXCEEDED,
        )
    if not proposal_json.strip():
        raise ProposalError("proposal_json was empty.", ProposalReason.MALFORMED)
    data = _strict_object(proposal_json)
    if kind == PROPOSAL_KIND_MODEL_PATCH:
        return _parse_model_patch(data)
    if kind == PROPOSAL_KIND_LAYER_STYLE:
        return _parse_layer_style(data)
    if kind == PROPOSAL_KIND_PROCESSING_RUN:
        return _parse_processing_run(data)
    return _parse_model_run(data)


# -- detached model-patch application/preview -------------------------------


def _apply_operations_to_clone(
    base_graph: GraphModel,
    proposal: ModelPatchProposal,
    catalog: Any,
    *,
    clone_fn,
    max_nodes: int,
    max_edges: int,
) -> Tuple[GraphModel, List[Dict[str, Any]]]:
    """Apply every operation to a detached clone of ``base_graph`` and return the
    candidate plus the bounded per-operation summaries. ``base_graph`` is never
    mutated: a mid-operation failure only ever affects the throwaway clone.
    """
    candidate = clone_fn(base_graph)
    preview_ops: List[Dict[str, Any]] = []
    for op in proposal.operations:
        op.apply(candidate, catalog)
        preview_ops.append({"summary": op.describe(), "destructive": op.is_destructive})
    if len(candidate.nodes) > max_nodes:
        raise ProposalError(
            f"The resulting graph exceeds the {max_nodes}-node safety limit.",
            ProposalReason.LIMIT_EXCEEDED,
        )
    if len(candidate.edges) > max_edges:
        raise ProposalError(
            f"The resulting graph exceeds the {max_edges}-edge safety limit.",
            ProposalReason.LIMIT_EXCEEDED,
        )
    return candidate, preview_ops


def apply_model_patch_to_clone(
    base_graph: GraphModel,
    proposal: ModelPatchProposal,
    catalog: Any,
    *,
    clone_fn,
    max_nodes: int,
    max_edges: int,
) -> GraphModel:
    """Return the **detached candidate graph** with ``proposal`` applied.

    Identical operation/limit semantics to :func:`build_model_patch_preview`,
    but returns the applied clone so a trusted apply coordinator can install it.
    ``base_graph`` is never mutated.
    """
    candidate, _ = _apply_operations_to_clone(
        base_graph,
        proposal,
        catalog,
        clone_fn=clone_fn,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    return candidate


def build_model_patch_preview(
    base_graph: GraphModel,
    proposal: ModelPatchProposal,
    catalog: Any,
    *,
    clone_fn,
    max_nodes: int,
    max_edges: int,
    max_issues: int = 40,
) -> Dict[str, Any]:
    """Apply ``proposal`` to a **detached clone** of ``base_graph`` and return a
    bounded, JSON-compatible preview. ``base_graph`` is never mutated: every
    operation runs on ``clone_fn(base_graph)``, so a mid-operation failure
    leaves the original graph untouched. Raises :class:`ProposalError` on the
    first structural violation or when a bound is exceeded.
    """
    candidate, preview_ops = _apply_operations_to_clone(
        base_graph,
        proposal,
        catalog,
        clone_fn=clone_fn,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )
    issues = candidate.validate()
    issue_lines = [f"{issue.level}: {issue.message}"[:300] for issue in issues[:max_issues]]
    return {
        "operations": preview_ops,
        "operation_count": len(preview_ops),
        "candidate_node_count": len(candidate.nodes),
        "candidate_edge_count": len(candidate.edges),
        "validation_issues": issue_lines,
        "validation_issues_truncated": len(issues) > len(issue_lines),
        "incomplete": bool(issue_lines),
        "destructive": any(op.is_destructive for op in proposal.operations),
    }


@dataclass(frozen=True)
class ProposalValidation:
    """The narrow result the run loop receives from the runtime validator."""

    ok: bool
    preview: Optional[Dict[str, Any]] = None
    reason_code: str = ""
    message: str = ""

    @classmethod
    def success(cls, preview: Dict[str, Any]) -> "ProposalValidation":
        return cls(ok=True, preview=preview)

    @classmethod
    def failure(cls, reason_code: str, message: str) -> "ProposalValidation":
        return cls(ok=False, reason_code=reason_code, message=message)
