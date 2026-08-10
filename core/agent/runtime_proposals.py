"""Trusted runtime boundary that validates one inert proposal against live state.

This is the only place a parsed proposal draft meets the live QgsProject, the
live SmartModeler graph, and the shared context-token service. It performs no
mutation whatsoever: a `model_patch` is applied to a **detached clone** through
the trusted serializer and never to the live graph, and a `layer_style` is
checked against the live layer's fields/kind without ever calling
``setRenderer``/``setLabeling``, touching opacity, repainting, or marking the
project dirty.

It returns a bounded, detached, JSON-compatible validated preview (never
applied) or a bounded controlled failure carrying a stable reason code -- never
a raw traceback, raw provider JSON, QGIS object repr, or a source path.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Dict, List, Optional

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsProcessing,
    QgsProcessingContext,
    QgsProcessingUtils,
    QgsProject,
    QgsReferencedRectangle,
    QgsRasterLayer,
    QgsVectorLayer,
)

from . import context as agent_context
from .context_tokens import ContextTokenService
from .contracts import AgentMode, AgentScope
from ..graph_model import GraphModel
from .proposals import (
    LayerStyleProposal,
    ModelPatchProposal,
    ModelRunProposal,
    PluginActionProposal,
    WorkspacePatchProposal,
    PythonRunProposal,
    PROPOSAL_KIND_LAYER_STYLE,
    PROPOSAL_KIND_MODEL_PATCH,
    PROPOSAL_KIND_MODEL_RUN,
    PROPOSAL_KIND_PROCESSING_RUN,
    PROPOSAL_KIND_PLUGIN_ACTION,
    PROPOSAL_KIND_WORKSPACE_PATCH,
    PROPOSAL_KIND_PYTHON_RUN,
    PROPOSAL_KIND_SQL_RUN,
    PROPOSAL_KIND_TRUSTED_SCRIPT_RUN,
    ProcessingRunProposal,
    SqlRunProposal,
    TrustedScriptRunProposal,
    ProposalError,
    ProposalReason,
    ProposalValidation,
    _within_one_edit,
    build_model_patch_preview,
)
from .plugin_actions import (
    PLUGIN_ACTION_KIND,
    capability_state as plugin_capability_state,
    reviewed_actions,
)
from .run_planner import (
    LayerView,
    RASTER,
    VECTOR,
    plan_model_run,
    plan_processing_run,
)
from .qgis_expression_policy import validate_qgis_expression
from .safe_algorithm_policy import SafeAlgorithmPolicy, default_policy
from .runtime_tools import (
    MODEL_TARGET_ID,
    MODEL_PROPOSAL_KIND,
    PROCESSING_PROPOSAL_KIND,
    STYLE_PROPOSAL_KIND,
    STYLE_STATE_LIMIT,
    algorithm_signature_state,
    build_output_specs,
    build_param_specs,
    crs_is_area_safe,
    extract_layer_style_state,
)
from .workspace import WorkspaceError, WorkspaceManager

# Ceilings on the candidate graph a model_patch may produce; identical to the
# existing AiMcpBridge safety limits so a proposal can never exceed them.
MAX_CANDIDATE_NODES = 80
MAX_CANDIDATE_EDGES = 240

MAX_PREVIEW_MESSAGE_CHARS = 400
ModelProvider = Callable[[], Optional[Any]]

# Defense-in-depth: which application-owned scope each proposal kind is
# compatible with. The run loop enforces this too; the validator re-checks so a
# direct/foreign caller cannot bypass it.
_KIND_SCOPES = {
    PROPOSAL_KIND_MODEL_PATCH: (AgentScope.CURRENT_MODEL,),
    PROPOSAL_KIND_LAYER_STYLE: (AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
    PROPOSAL_KIND_PROCESSING_RUN: (AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
    PROPOSAL_KIND_MODEL_RUN: (AgentScope.CURRENT_MODEL,),
    PROPOSAL_KIND_PLUGIN_ACTION: (AgentScope.PROJECT, AgentScope.ACTIVE_LAYER),
    PROPOSAL_KIND_SQL_RUN: (AgentScope.PROJECT,),
    PROPOSAL_KIND_TRUSTED_SCRIPT_RUN: (AgentScope.PROJECT,),
    PROPOSAL_KIND_PYTHON_RUN: (AgentScope.PROJECT,),
    PROPOSAL_KIND_WORKSPACE_PATCH: (AgentScope.WORKSPACE,),
}

# Stable, detail-free sentences for each policy denial. The reason code carries
# the machine-readable outcome; the text never names the parameter, its type, or
# the reviewed signature, so a probing provider learns nothing from a rejection.
_POLICY_MESSAGES = {
    ProposalReason.ALGORITHM_NOT_ALLOWED: (
        "That algorithm does not pass the local safe-run policy."
    ),
    ProposalReason.PROVIDER_NOT_TRUSTED: (
        "That third-party provider has no reviewed Agent execution adapter."
    ),
    ProposalReason.SIDE_EFFECT_BLOCKED: (
        "That algorithm has a known network, project, database, or external-command side effect."
    ),
    ProposalReason.UNSUPPORTED_PARAMETER: (
        "That algorithm uses an input type the Agent cannot bind safely yet."
    ),
    ProposalReason.UNSAFE_DESTINATION: (
        "That algorithm requires a file, folder, database, or other non-layer destination."
    ),
    ProposalReason.NO_LAYER_OUTPUT: (
        "That algorithm has no temporary map-layer destination for an Agent run."
    ),
    ProposalReason.SIGNATURE_MISMATCH: (
        "That algorithm's live signature is not safe for an Agent run."
    ),
    ProposalReason.UNSAFE_PARAMETER: (
        "One of the requested settings is not something a run proposal may set."
    ),
}

# How each destination parameter class is described to the human. Anything not
# listed is reported generically -- never as a path.
_DESTINATION_LABELS = (
    ("QgsProcessingParameterRasterDestination", "temporary raster layer"),
    ("QgsProcessingParameterVectorDestination", "temporary vector layer"),
    ("QgsProcessingParameterFeatureSink", "temporary vector layer"),
)


class _AlgorithmCatalogPort:
    """Adapts the live :class:`AlgorithmCatalog` to the pure applier's needs.

    Imported lazily so the pure proposal contracts stay QGIS-free and so tests
    can inject a fake catalog without a Processing registry.
    """

    def __init__(self) -> None:
        from ..algorithm_catalog import AlgorithmCatalog

        self._catalog = AlgorithmCatalog

    def algorithm_exists(self, algorithm_id: str) -> bool:
        return bool(self._catalog.algorithm_exists(algorithm_id))

    def ai_algorithm_allowed(self, algorithm_id: str) -> bool:
        return bool(self._catalog.ai_algorithm_allowed(algorithm_id))

    def create_node(self, algorithm_id: str, node_id: str, title: str):
        return self._catalog.create_node(algorithm_id, node_id=node_id, title=title)

    def layer_choices(self, socket_type: str) -> Dict[str, str]:
        return self._catalog.layer_choices(socket_type)

    def autobind_unique_project_layers(self, graph: GraphModel) -> int:
        return int(self._catalog.autobind_unique_project_layers(graph))


def _serializer_clone(graph: GraphModel) -> GraphModel:
    """Return a detached clone of ``graph`` through the trusted serializer.

    The live graph is never handed to the applier -- only this fresh copy is,
    so no operation can reach live state even on a mid-operation failure.
    """
    from ..model3_serializer import Model3Serializer

    clone = Model3Serializer.import_from_json(Model3Serializer.export_to_json(graph))
    if clone is None:
        raise ProposalError(
            "The current model could not be copied for validation.",
            ProposalReason.VALIDATION_FAILED,
        )
    return clone


def _sanitize(message: str) -> str:
    text = message if isinstance(message, str) else str(message)
    return text[:MAX_PREVIEW_MESSAGE_CHARS]


class RuntimeProposalValidator:
    """Validates a parsed proposal draft against live state, without mutation."""

    def __init__(
        self,
        model_provider: ModelProvider,
        token_service: ContextTokenService,
        active_layer_provider: Optional[Callable[[], Any]] = None,
        catalog: Optional[Any] = None,
        clone_fn: Optional[Callable[[GraphModel], GraphModel]] = None,
        policy: Optional[SafeAlgorithmPolicy] = None,
        map_extent_provider: Optional[Callable[[], Any]] = None,
        power_runtime: Optional[Any] = None,
        workspace_root_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._model_provider = model_provider
        self._token_service = token_service
        self._active_layer_provider = active_layer_provider or (lambda: None)
        self._catalog = catalog
        self._clone_fn = clone_fn or _serializer_clone
        # The shipped deny-by-default policy. Injectable only so a test can
        # narrow it; nothing reachable from a provider, project, setting, or
        # user message can widen or replace it.
        self._policy = policy or default_policy()
        self._map_extent_provider = map_extent_provider or (lambda: None)
        self._power_runtime = power_runtime
        self._workspace_root_provider = workspace_root_provider or (lambda: None)
        # On a successful validation the trusted boundary retains the detached
        # parsed proposal plus its target/token so the dock can build the single
        # pending action for an Act-mode apply. This never reaches the provider
        # or session memory; the dock consumes it via take_last_validated().
        self._last_validated: Optional[Dict[str, Any]] = None

    def _catalog_port(self) -> Any:
        # Bound lazily so a QGIS-free import of this module (or a fake-catalog
        # test) never forces the Processing registry to load at construction.
        if self._catalog is None:
            self._catalog = _AlgorithmCatalogPort()
        return self._catalog

    def take_last_validated(self) -> Optional[Dict[str, Any]]:
        """Return and clear the retained ingredients of the last successful
        validation (parsed proposal, preview, target identity, token, mode,
        scope). The dock uses this to build the single pending action for an
        Act-mode apply; it is never sent to the provider or session memory."""
        pending = self._last_validated
        self._last_validated = None
        return pending

    def validate(self, kind: str, proposal: Any, mode: str, scope: str) -> ProposalValidation:
        # A new validation always clears any stale retained ingredients first, so
        # a rejected/failed proposal can never leave an appliable pending behind.
        self._last_validated = None
        try:
            # Defense-in-depth mode/scope/kind gates: the run loop already
            # enforces these, but the trusted boundary must fail closed too.
            if mode not in (AgentMode.PLAN, AgentMode.ACT):
                return ProposalValidation.failure(
                    ProposalReason.VALIDATION_FAILED, "Proposals require Plan or Act mode."
                )
            if scope not in _KIND_SCOPES.get(kind, ()):
                return ProposalValidation.failure(
                    ProposalReason.SCOPE_MISMATCH,
                    "This proposal is not compatible with the selected scope.",
                )
            if kind == PROPOSAL_KIND_MODEL_PATCH and isinstance(proposal, ModelPatchProposal):
                return self._validate_model_patch(proposal, scope)
            if kind == PROPOSAL_KIND_LAYER_STYLE and isinstance(proposal, LayerStyleProposal):
                return self._validate_layer_style(proposal, scope)
            if kind == PROPOSAL_KIND_PROCESSING_RUN and isinstance(
                proposal, ProcessingRunProposal
            ):
                return self._validate_processing_run(proposal, scope)
            if kind == PROPOSAL_KIND_MODEL_RUN and isinstance(proposal, ModelRunProposal):
                return self._validate_model_run(proposal)
            if kind == PROPOSAL_KIND_PLUGIN_ACTION and isinstance(
                proposal, PluginActionProposal
            ):
                return self._validate_plugin_action(proposal, scope)
            if kind == PROPOSAL_KIND_WORKSPACE_PATCH and isinstance(
                proposal, WorkspacePatchProposal
            ):
                return self._validate_workspace_patch(proposal)
            if kind in (
                PROPOSAL_KIND_SQL_RUN,
                PROPOSAL_KIND_TRUSTED_SCRIPT_RUN,
                PROPOSAL_KIND_PYTHON_RUN,
            ) and isinstance(
                proposal,
                (SqlRunProposal, TrustedScriptRunProposal, PythonRunProposal),
            ):
                return self._validate_power(proposal)
        except ProposalError as error:
            return ProposalValidation.failure(error.reason_code, _sanitize(str(error)))
        except Exception:  # noqa: BLE001 - a validator failure must be sanitized
            return ProposalValidation.failure(
                ProposalReason.VALIDATION_FAILED, "The proposal could not be validated."
            )
        return ProposalValidation.failure(
            ProposalReason.UNKNOWN_KIND, "Unknown or mismatched proposal kind."
        )

    def _workspace_manager(self) -> WorkspaceManager:
        root = self._workspace_root_provider()
        if not root:
            raise ProposalError(
                "The workspace is unavailable.", ProposalReason.TARGET_MISSING
            )
        try:
            return WorkspaceManager(root)
        except WorkspaceError as error:
            raise ProposalError(str(error), ProposalReason.TARGET_MISSING) from error

    def _validate_workspace_patch(
        self, proposal: WorkspacePatchProposal
    ) -> ProposalValidation:
        manager = self._workspace_manager()
        state = manager.state(operation.path for operation in proposal.operations)
        if not self._token_service.verify(
            proposal.context_token, "workspace_patch", manager.workspace_id, state
        ):
            return ProposalValidation.failure(
                ProposalReason.STALE_CONTEXT,
                "The workspace changed since this patch was prepared. Inspect it again.",
            )
        preview = manager.preview(proposal)
        self._last_validated = {
            "kind": proposal.kind,
            "proposal": proposal,
            "preview": preview,
            "target_identity": f"workspace:{manager.workspace_id}",
            "context_token": proposal.context_token,
        }
        return ProposalValidation.success(preview)

    def _validate_power(self, proposal: Any) -> ProposalValidation:
        if self._power_runtime is None:
            return ProposalValidation.failure(
                ProposalReason.SIDE_EFFECT_BLOCKED, "Power Mode is unavailable."
            )
        ok, preview, ingredients, message = self._power_runtime.validate(proposal)
        if not ok:
            return ProposalValidation.failure(
                ProposalReason.VALIDATION_FAILED,
                _sanitize(message or "The Power Mode proposal was rejected."),
            )
        self._last_validated = {
            "kind": proposal.kind,
            "proposal": proposal,
            "preview": preview,
            "target_identity": str(preview.get("target", "")),
            "context_token": proposal.context_token,
            "display_name": str(ingredients.get("display_name", "")),
            "power_ingredients": ingredients,
        }
        return ProposalValidation.success(preview)

    # -- model_patch -------------------------------------------------------

    def _validate_model_patch(
        self, proposal: ModelPatchProposal, scope: str
    ) -> ProposalValidation:
        graph = self._model_provider()
        canonical = agent_context.canonical_model_state(graph)
        if not self._token_service.verify(
            proposal.context_token, MODEL_PROPOSAL_KIND, MODEL_TARGET_ID, canonical
        ):
            return ProposalValidation.failure(
                ProposalReason.STALE_CONTEXT,
                "The model changed since this proposal was prepared. Inspect it again.",
            )
        base = graph if graph is not None else GraphModel("New workflow")
        body = build_model_patch_preview(
            base,
            proposal,
            self._catalog_port(),
            clone_fn=self._clone_fn,
            max_nodes=MAX_CANDIDATE_NODES,
            max_edges=MAX_CANDIDATE_EDGES,
        )
        target = (
            agent_context.bound_text(graph.name, agent_context.MAX_DISPLAY_NAME)
            if graph is not None
            else "New model (none open)"
        )
        preview = {
            "kind": PROPOSAL_KIND_MODEL_PATCH,
            "title": proposal.title,
            "target": target,
            "summary": proposal.summary,
            "warnings": list(proposal.warnings),
            "applied": False,
        }
        preview.update(body)
        self._last_validated = {
            "kind": PROPOSAL_KIND_MODEL_PATCH,
            "proposal": proposal,
            "preview": preview,
            "target_identity": MODEL_TARGET_ID,
            "context_token": proposal.context_token,
        }
        return ProposalValidation.success(preview)

    # -- reviewed cross-plugin action ------------------------------------

    def _validate_plugin_action(
        self, proposal: PluginActionProposal, scope: str
    ) -> ProposalValidation:
        actions = reviewed_actions(proposal.package_name)
        definition = actions.get(proposal.action_id)
        if definition is None:
            return ProposalValidation.failure(
                ProposalReason.ALGORITHM_NOT_ALLOWED,
                "That plugin action has no reviewed SmartModeler adapter.",
            )
        try:
            import qgis.utils as qgis_utils
        except ImportError:
            return ProposalValidation.failure(
                ProposalReason.TARGET_MISSING,
                "The QGIS plugin registry is unavailable.",
            )
        available = set(getattr(qgis_utils, "available_plugins", []) or [])
        active = set(getattr(qgis_utils, "active_plugins", []) or [])
        loaded_plugins = dict(getattr(qgis_utils, "plugins", {}) or {})
        if (
            proposal.package_name not in available | active | set(loaded_plugins)
            or proposal.package_name not in active
            or proposal.package_name not in loaded_plugins
        ):
            return ProposalValidation.failure(
                ProposalReason.TARGET_MISSING,
                "The reviewed plugin must be installed, enabled, and loaded.",
            )
        version = ""
        with _suppress():
            version = str(
                qgis_utils.pluginMetadata(proposal.package_name, "version") or ""
            )
        state = plugin_capability_state(
            proposal.package_name, version, True, True
        )
        if not self._token_service.verify(
            proposal.context_token,
            PLUGIN_ACTION_KIND,
            proposal.package_name,
            state,
        ):
            return ProposalValidation.failure(
                ProposalReason.STALE_CONTEXT,
                "The plugin capability changed since inspection. Inspect it again.",
            )
        project = QgsProject.instance()
        layer = (
            project.mapLayer(proposal.target_layer_id)
            if project is not None
            else None
        )
        if not isinstance(layer, QgsVectorLayer):
            return ProposalValidation.failure(
                ProposalReason.TARGET_MISSING,
                "The requested project vector layer is no longer available.",
            )
        if scope == AgentScope.ACTIVE_LAYER:
            active_layer = self._active_layer_provider()
            if active_layer is None or active_layer.id() != layer.id():
                return ProposalValidation.failure(
                    ProposalReason.TARGET_MISSING,
                    "The target is not the current active layer.",
                )
        preview = {
            "kind": PROPOSAL_KIND_PLUGIN_ACTION,
            "title": proposal.title,
            "target": "02viz - " + agent_context.bound_text(
                layer.name(), agent_context.MAX_DISPLAY_NAME
            ),
            "summary": proposal.summary,
            "warnings": list(proposal.warnings),
            "applied": False,
            "destructive": False,
            "changes": [
                "Open the installed 02viz Studio dock",
                "Select the inspected project layer",
                "Use 02viz's offline assistant to choose and render one chart",
            ],
            "network_access": False,
            "temporary_file": True,
        }
        self._last_validated = {
            "kind": PROPOSAL_KIND_PLUGIN_ACTION,
            "proposal": proposal,
            "preview": preview,
            "target_identity": (
                f"{proposal.package_name}:{proposal.action_id}:"
                f"{proposal.target_layer_id}"
            ),
            "context_token": proposal.context_token,
        }
        return ProposalValidation.success(preview)

    # -- layer_style -------------------------------------------------------

    def _validate_layer_style(
        self, proposal: LayerStyleProposal, scope: str
    ) -> ProposalValidation:
        project = QgsProject.instance()
        layer = project.mapLayer(proposal.target_layer_id) if project is not None else None
        if layer is None:
            return ProposalValidation.failure(
                ProposalReason.TARGET_MISSING, "The target layer is not in the project."
            )
        if scope == AgentScope.ACTIVE_LAYER:
            active = self._active_layer_provider()
            if active is None or active.id() != layer.id():
                return ProposalValidation.failure(
                    ProposalReason.TARGET_MISSING,
                    "The proposal target is not the current active layer.",
                )
        if not self._token_service.verify(
            proposal.context_token,
            STYLE_PROPOSAL_KIND,
            layer.id(),
            extract_layer_style_state(layer, STYLE_STATE_LIMIT),
        ):
            return ProposalValidation.failure(
                ProposalReason.STALE_CONTEXT,
                "The layer style changed since this proposal was prepared. Inspect it again.",
            )

        is_vector = isinstance(layer, QgsVectorLayer)
        is_raster = isinstance(layer, QgsRasterLayer)
        if proposal.is_vector_family and not is_vector:
            return ProposalValidation.failure(
                ProposalReason.VALIDATION_FAILED,
                "A vector renderer family is not valid for this layer.",
            )
        if proposal.is_raster_family and not is_raster:
            return ProposalValidation.failure(
                ProposalReason.VALIDATION_FAILED,
                "A raster renderer family is not valid for this layer.",
            )

        field_names = set()
        if is_vector:
            with _suppress():
                field_names = {field.name() for field in layer.fields()}
        renderer = proposal.renderer
        if renderer.field and renderer.field not in field_names:
            # One-edit correction, the same rule the attribute filter already
            # uses: "alanm2" for a live "alan_m2" is a typo, not a different
            # request, and failing it cost a turn every time. Ambiguity still
            # fails closed, and the correction is surfaced as a warning so the
            # human sees it on the approval card before clicking Apply.
            near = [name for name in field_names if _within_one_edit(renderer.field, name)]
            if len(near) == 1:
                proposal = replace(
                    proposal,
                    renderer=replace(renderer, field=near[0]),
                    warnings=tuple(proposal.warnings)
                    + (
                        f"Interpreted renderer field {renderer.field!r} as "
                        f"{near[0]!r}; review this correction before Apply.",
                    ),
                )
                renderer = proposal.renderer
            else:
                return ProposalValidation.failure(
                    ProposalReason.VALIDATION_FAILED,
                    f"The renderer field {renderer.field!r} is not on the target "
                    f"layer. Live fields: "
                    f"{', '.join(sorted(field_names)[:20]) or '(none)'}.",
                )
        if proposal.labels.enabled:
            if not is_vector:
                return ProposalValidation.failure(
                    ProposalReason.VALIDATION_FAILED,
                    "Labels can be enabled only for a vector layer.",
                )
            if proposal.labels.field not in field_names:
                return ProposalValidation.failure(
                    ProposalReason.VALIDATION_FAILED,
                    "The label field does not match a field on the target layer.",
                )

        preview = {
            "kind": PROPOSAL_KIND_LAYER_STYLE,
            "title": proposal.title,
            "target": agent_context.bound_text(layer.name(), agent_context.MAX_DISPLAY_NAME),
            "target_layer_id": agent_context.bound_text(layer.id(), 128),
            "summary": proposal.summary,
            "warnings": list(proposal.warnings),
            "applied": False,
            "renderer": renderer.to_dict(),
            "labels": proposal.labels.to_dict(),
            "changes": _style_change_lines(proposal),
        }
        self._last_validated = {
            "kind": PROPOSAL_KIND_LAYER_STYLE,
            "proposal": proposal,
            "preview": preview,
            "target_identity": layer.id(),
            "context_token": proposal.context_token,
        }
        return ProposalValidation.success(preview)

    # -- processing_run ----------------------------------------------------

    def _live_algorithm(self, algorithm_id: str) -> Optional[Any]:
        registry = QgsApplication.processingRegistry()
        return registry.algorithmById(algorithm_id) if registry is not None else None

    def _params_lookup(self, algorithm_id: str) -> Optional[list]:
        """The live parameter views for ``algorithm_id``, or ``None`` if absent."""
        algorithm = self._live_algorithm(algorithm_id)
        if algorithm is None:
            return None
        return build_param_specs(algorithm)

    @staticmethod
    def _layer_view(layer_id: str) -> Optional[LayerView]:
        """Resolve one project layer **by id** into a QGIS-free view.

        A path, URI, or layer *name* never resolves here: only an exact live
        project layer id does, so a proposal can never point the run at a file.
        """
        project = QgsProject.instance()
        layer = project.mapLayer(layer_id) if project is not None else None
        if layer is None:
            return None
        if isinstance(layer, QgsVectorLayer):
            kind = VECTOR
            fields = frozenset()
            field_types: Dict[str, str] = {}
            with _suppress():
                fields = frozenset(field.name() for field in layer.fields())
            with _suppress():
                field_types = {
                    field.name(): str(field.typeName() or "").casefold()
                    for field in layer.fields()
                }
        elif isinstance(layer, QgsRasterLayer):
            kind = RASTER
            fields = frozenset()
            field_types = {}
        else:
            return None
        area_safe = True
        with _suppress():
            area_safe = crs_is_area_safe(layer.crs())
        return LayerView(
            layer_id=agent_context.bound_text(layer.id(), 200),
            name=agent_context.bound_text(layer.name(), agent_context.MAX_DISPLAY_NAME),
            kind=kind,
            field_names=fields,
            field_types=field_types,
            area_safe_crs=area_safe,
        )

    def _materialize(self, plan: Any) -> Dict[str, Any]:
        """Turn an inert plan into the live parameter map for one run.

        This is the only place a resolved binding becomes a live QGIS object,
        and the only place a destination is set -- always to the application's
        temporary output, never to a value that came from the proposal.
        """
        project = QgsProject.instance()
        parameters: Dict[str, Any] = {}
        for binding in plan.bindings:
            if binding.tag == "layer":
                layer = project.mapLayer(binding.layer_ids[0]) if project is not None else None
                if layer is None:
                    raise ProposalError(
                        "An input layer is not in the project.", ProposalReason.TARGET_MISSING
                    )
                parameters[binding.param] = layer
            elif binding.tag == "layers":
                layers = []
                for layer_id in binding.layer_ids:
                    layer = project.mapLayer(layer_id) if project is not None else None
                    if layer is None:
                        raise ProposalError(
                            "An input layer is not in the project.", ProposalReason.TARGET_MISSING
                        )
                    layers.append(layer)
                parameters[binding.param] = layers
            elif binding.tag == "crs":
                crs = QgsCoordinateReferenceSystem(binding.value)
                if not crs.isValid():
                    raise ProposalError(
                        "The target coordinate reference system is not recognised.",
                        ProposalReason.VALIDATION_FAILED,
                    )
                parameters[binding.param] = crs
            elif binding.tag == "map_extent":
                current = self._map_extent_provider()
                if (
                    not isinstance(current, tuple)
                    or len(current) != 2
                    or current[0] is None
                    or current[1] is None
                ):
                    raise ProposalError(
                        "The current map extent is not available.",
                        ProposalReason.TARGET_MISSING,
                    )
                parameters[binding.param] = QgsReferencedRectangle(
                    current[0], current[1]
                )
            elif binding.tag == "layer_extent":
                layer = (
                    project.mapLayer(binding.layer_ids[0])
                    if project is not None and binding.layer_ids
                    else None
                )
                if layer is None:
                    raise ProposalError(
                        "The extent source layer is not in the project.",
                        ProposalReason.TARGET_MISSING,
                    )
                parameters[binding.param] = QgsReferencedRectangle(
                    layer.extent(),
                    layer.crs(),
                )
            else:
                parameters[binding.param] = binding.value
        for name, value in plan.fixed_values:
            parameters[name] = value
        for name in plan.parameter_destinations:
            parameters[name] = QgsProcessing.TEMPORARY_OUTPUT
        for name in plan.internal_destinations:
            # QuickOSM's file destination mishandles the generic
            # TEMPORARY_OUTPUT sentinel on Windows (mixed separators). Generate
            # a QGIS-owned temporary GeoPackage path explicitly; the provider
            # never sees or supplies this path.
            parameters[name] = QgsProcessingUtils.generateTempFilename(
                "smartmodeler_quickosm.gpkg"
            )
        return parameters

    @staticmethod
    def _output_lines(
        algorithm: Any, destinations: tuple, temporary_file: bool = False
    ) -> List[str]:
        lines: List[str] = []
        by_name = {
            definition.name(): definition
            for definition in algorithm.parameterDefinitions()
        }
        output_by_name = {
            definition.name(): definition
            for definition in algorithm.outputDefinitions()
        }
        for name in destinations:
            definition = by_name.get(name)
            label = "temporary output layer"
            if definition is not None:
                type_names = {cls.__name__ for cls in type(definition).__mro__}
                for class_name, text in _DESTINATION_LABELS:
                    if class_name in type_names:
                        label = text
                        break
            elif name in output_by_name:
                label = "temporary vector result layer"
            storage = (
                "added to the project from application temporary storage"
                if temporary_file
                else "added to the project, never written to disk"
            )
            lines.append(f"{name}: {label} ({storage})")
        return lines

    def _validate_processing_run(
        self, proposal: ProcessingRunProposal, scope: str
    ) -> ProposalValidation:
        algorithm = self._live_algorithm(proposal.algorithm_id)
        if algorithm is None:
            return ProposalValidation.failure(
                ProposalReason.ALGORITHM_NOT_ALLOWED,
                _POLICY_MESSAGES[ProposalReason.ALGORITHM_NOT_ALLOWED],
            )
        specs = build_param_specs(algorithm)
        # Deny by default *before* anything else is considered, so an algorithm
        # outside the pinned/structural policy is refused before its bindings
        # or freshness receipt are examined.
        decision = self._policy.is_runnable(
            proposal.algorithm_id, specs, build_output_specs(algorithm)
        )
        if not decision.allowed:
            return ProposalValidation.failure(
                decision.reason_code,
                _POLICY_MESSAGES.get(
                    decision.reason_code, "That algorithm cannot be run from here."
                ),
            )
        if not self._token_service.verify(
            proposal.context_token,
            PROCESSING_PROPOSAL_KIND,
            proposal.algorithm_id,
            algorithm_signature_state(algorithm),
        ):
            return ProposalValidation.failure(
                ProposalReason.STALE_CONTEXT,
                "This algorithm was not inspected in this session, or it changed since. "
                "Inspect it again.",
            )
        active = self._active_layer_provider() if scope == AgentScope.ACTIVE_LAYER else None
        active_id = ""
        if active is not None:
            with _suppress():
                active_id = active.id()
        plan = plan_processing_run(
            proposal,
            self._policy,
            decision.record,
            specs,
            self._layer_view,
            active_layer_id=active_id,
            require_active_layer=(scope == AgentScope.ACTIVE_LAYER),
        )
        parameters = self._materialize(plan)
        for binding in plan.bindings:
            if binding.tag != "expression":
                continue
            expression_check = validate_qgis_expression(
                binding.value, parameters.get("INPUT")
            )
            if not expression_check.ok:
                return ProposalValidation.failure(
                    ProposalReason.VALIDATION_FAILED,
                    expression_check.message,
                )
        # Validation only: build a throwaway context and ask the algorithm
        # whether the map is acceptable. Nothing is executed here. A raised
        # check fails *closed* -- ``valid`` starts False and is only set by a
        # check that actually completed.
        context = QgsProcessingContext()
        project = QgsProject.instance()
        if project is not None:
            context.setProject(project)
            with _suppress():
                context.setTransformContext(project.transformContext())
        valid = False
        with _suppress():
            valid = bool(algorithm.checkParameterValues(parameters, context)[0])
        if not valid:
            return ProposalValidation.failure(
                ProposalReason.VALIDATION_FAILED,
                "The requested settings are not valid for this algorithm.",
            )
        display_name = agent_context.bound_text(
            algorithm.displayName(), agent_context.MAX_DISPLAY_NAME
        )
        outputs = self._output_lines(
            algorithm, plan.destinations, temporary_file=plan.temporary_file
        )
        changes = list(plan.preview_lines)
        warnings = list(proposal.warnings)
        if plan.network_access:
            changes.append(
                "Network: contact the reviewed Overpass endpoint after explicit approval"
            )
            warnings.append(
                "This run sends the current map bounding box and OSM tags to Overpass."
            )
        if plan.temporary_file:
            changes.append(
                "Storage: create an application-owned temporary GeoPackage"
            )
        preview = {
            "kind": PROPOSAL_KIND_PROCESSING_RUN,
            "title": proposal.title,
            "target": display_name,
            "summary": proposal.summary,
            "warnings": warnings,
            "applied": False,
            "destructive": False,
            "algorithm_id": proposal.algorithm_id,
            "changes": changes,
            "outputs": outputs,
            "network_access": bool(plan.network_access),
            "temporary_file": bool(plan.temporary_file),
        }
        self._last_validated = {
            "kind": PROPOSAL_KIND_PROCESSING_RUN,
            "proposal": proposal,
            "preview": preview,
            "target_identity": proposal.algorithm_id,
            "context_token": proposal.context_token,
            # Execution ingredients for the coordinator. They exist only between
            # this successful validation and the human's Run click (the dock
            # consumes them immediately) and never reach the provider, the
            # session memory, or the ledger.
            "algorithm_id": proposal.algorithm_id,
            "display_name": display_name,
            "run_parameters": parameters,
            "destinations": tuple(plan.destinations),
            "network_access": bool(plan.network_access),
        }
        return ProposalValidation.success(preview)

    # -- model_run ---------------------------------------------------------

    def _validate_model_run(self, proposal: ModelRunProposal) -> ProposalValidation:
        graph = self._model_provider()
        canonical = agent_context.canonical_model_state(graph)
        if not self._token_service.verify(
            proposal.context_token, MODEL_PROPOSAL_KIND, MODEL_TARGET_ID, canonical
        ):
            return ProposalValidation.failure(
                ProposalReason.STALE_CONTEXT,
                "The model changed since this proposal was prepared. Inspect it again.",
            )
        if graph is None or not graph.nodes:
            return ProposalValidation.failure(
                ProposalReason.TARGET_MISSING, "There is no current workflow to run."
            )
        # Check a detached clone carrying the same unique-layer autobinding the
        # execution engine will perform, so what is approved is what will run --
        # while the live graph stays untouched by validation.
        candidate = self._clone_fn(graph)
        with _suppress():
            self._catalog_port().autobind_unique_project_layers(candidate)
        plan = plan_model_run(candidate, self._policy, self._params_lookup)
        target = agent_context.bound_text(graph.name, agent_context.MAX_DISPLAY_NAME)
        preview = {
            "kind": PROPOSAL_KIND_MODEL_RUN,
            "title": proposal.title,
            "target": target,
            "summary": proposal.summary,
            "warnings": list(proposal.warnings),
            "applied": False,
            "destructive": False,
            "changes": list(plan.preview_lines),
            "outputs": [
                "Terminal outputs are added as temporary project layers, never written to disk."
            ],
            "node_count": plan.node_count,
        }
        self._last_validated = {
            "kind": PROPOSAL_KIND_MODEL_RUN,
            "proposal": proposal,
            "preview": preview,
            "target_identity": MODEL_TARGET_ID,
            "context_token": proposal.context_token,
            "display_name": target,
            "node_count": plan.node_count,
        }
        return ProposalValidation.success(preview)


def _style_change_lines(proposal: LayerStyleProposal) -> list:
    renderer = proposal.renderer
    lines = [f"Renderer family: {renderer.family}"]
    if renderer.field:
        lines.append(f"Classification field: {renderer.field}")
    if renderer.class_count:
        lines.append(f"Class count: {renderer.class_count}")
    if renderer.palette:
        lines.append(f"Palette: {len(renderer.palette)} colour(s)")
    lines.append(f"Opacity: {renderer.opacity}")
    if proposal.labels.enabled:
        lines.append(f"Labels on field: {proposal.labels.field}")
    else:
        lines.append("Labels: disabled")
    return lines


def _suppress():
    import contextlib

    return contextlib.suppress(Exception)
