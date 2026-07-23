"""Trusted runtime coordinator that atomically applies/undoes one agent action.

This is the only place a human-approved :class:`~.pending_action.PendingAction`
mutates live state. It re-derives everything from the retained detached parsed
proposal (never from the UI preview dict), re-verifies the freshness token and
the proposal digest at the click boundary, and performs exactly one atomic
mutation:

- a ``model_patch`` replaces the live SmartModeler graph through one trusted
  model-window adapter, capturing the pre-state so any failure is rolled back;
- a ``layer_style`` builds every replacement renderer/labeling object first and
  swaps them in one guarded main-thread transaction, restoring the captured
  renderer/labeling/opacity/dirty state on any failure.

Undo is single-level and state-fingerprinted: it is offered only when the live
target still matches the action's recorded post-state, so it can never overwrite
a later user edit. Every result is a bounded value object -- never a raw
traceback, QGIS object repr, source path, feature/category value, or secret.
"""
from __future__ import annotations

import contextlib
import hashlib
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional, Sequence, Tuple

from . import context as agent_context
from .context_tokens import ContextTokenService
from .pending_action import PendingAction, proposal_digest
from .identifiers import (
    MODEL_PROPOSAL_KIND,
    MODEL_TARGET_ID,
    STYLE_PROPOSAL_KIND,
    STYLE_STATE_LIMIT,
)
from .proposals import (
    LayerStyleProposal,
    ModelPatchProposal,
    PROPOSAL_KIND_LAYER_STYLE,
    PROPOSAL_KIND_MODEL_PATCH,
    PROPOSAL_KIND_MODEL_RUN,
    PROPOSAL_KIND_PROCESSING_RUN,
    ProposalError,
    ProposalReason,
    apply_model_patch_to_clone,
)

# The two execution kinds whose Undo removes the run's own result layers.
RUN_KINDS = (PROPOSAL_KIND_PROCESSING_RUN, PROPOSAL_KIND_MODEL_RUN)
# A single reviewed run cannot legitimately produce more results than this.
MAX_UNDOABLE_RESULT_LAYERS = 20

# Same ceilings the Phase 03 validator uses; a candidate can never exceed them.
MAX_CANDIDATE_NODES = 80
MAX_CANDIDATE_EDGES = 240
_MAX_MESSAGE_CHARS = 300

# Style families that Phase 04 can actually apply. The remaining Phase 03
# preview families stay preview-only and fail closed on apply.
APPLIABLE_STYLE_FAMILIES = frozenset(
    {"keep", "single_symbol", "categorized", "graduated", "raster_gray"}
)
# Hard cap on unique categories a categorized apply will build locally.
MAX_CATEGORY_VALUES = 60


class ApplyReason:
    """Stable, user-safe reason codes for apply/undo failures."""

    TARGET_MISSING = "apply_target_missing"
    STATE_CHANGED = "apply_state_changed"
    DIGEST_MISMATCH = "apply_integrity_failed"
    UNSUPPORTED = "apply_unsupported"
    FAILED = "apply_failed"
    UNDO_NOT_ELIGIBLE = "undo_not_eligible"


def _sanitize(message: Any) -> str:
    text = message if isinstance(message, str) else str(message)
    return text[:_MAX_MESSAGE_CHARS]


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _style_fingerprint(layer: Any) -> str:
    """A deterministic hash of the layer's bounded safe style state."""
    import json

    from .runtime_tools import extract_layer_style_state

    state = extract_layer_style_state(layer, STYLE_STATE_LIMIT)
    state.pop("context_token", None)  # never present here, but be explicit
    canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _hash_text(canonical)


def result_layer_fingerprint(layer: Any) -> str:
    """A deterministic identity hash for one run-created result layer.

    Covers the layer's id, name, kind, CRS and size. If the human renames it,
    edits it, reprojects it, or replaces it with something else under the same
    id, the fingerprint no longer matches and the destructive Undo is refused
    rather than forced -- Undo must never remove a layer the human made theirs.
    """
    import json

    state: Dict[str, Any] = {}
    with contextlib.suppress(Exception):
        state["id"] = layer.id()
    with contextlib.suppress(Exception):
        state["name"] = layer.name()
    with contextlib.suppress(Exception):
        state["kind"] = type(layer).__name__
    with contextlib.suppress(Exception):
        state["crs"] = layer.crs().authid()
    with contextlib.suppress(Exception):  # vector only
        state["features"] = int(layer.featureCount())
    with contextlib.suppress(Exception):  # raster only
        state["size"] = [int(layer.width()), int(layer.height()), int(layer.bandCount())]
    canonical = json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _hash_text(canonical)


@dataclass
class AppliedAction:
    """What Undo needs after a successful apply or run. Application memory only."""

    action_id: str
    kind: str
    target_identity: str
    title: str
    is_destructive: bool
    post_fingerprint: str
    model_pre_json: Optional[str] = None
    style_pre: Optional[Dict[str, Any]] = field(default=None)
    # For a run: the (layer id, identity fingerprint) of every layer THIS run
    # added. Undo removes exactly these and nothing else -- never an input,
    # never a layer the run did not create.
    result_layers: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)


@dataclass
class ApplyResult:
    ok: bool
    reason_code: str = ""
    message: str = ""
    applied_action: Optional[AppliedAction] = None


@dataclass
class UndoResult:
    ok: bool
    reason_code: str = ""
    message: str = ""


class RuntimeApplyCoordinator:
    """Atomically applies/undoes one validated, human-approved pending action."""

    def __init__(
        self,
        model_adapter: Any,
        token_service: ContextTokenService,
        active_layer_provider: Optional[Callable[[], Any]] = None,
        catalog: Optional[Any] = None,
        clone_fn: Optional[Callable[[Any], Any]] = None,
        export_fn: Optional[Callable[[Any], str]] = None,
        import_fn: Optional[Callable[[str], Any]] = None,
        project_provider: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._model_adapter = model_adapter
        self._token_service = token_service
        self._active_layer_provider = active_layer_provider or (lambda: None)
        self._catalog = catalog
        self._clone_fn = clone_fn
        self._export_fn = export_fn
        self._import_fn = import_fn
        # Injectable only so the run/undo bookkeeping is testable without a QGIS
        # runtime; production always resolves the live QgsProject singleton.
        self._project_provider = project_provider

    def _project(self) -> Any:
        if self._project_provider is not None:
            return self._project_provider()
        from qgis.core import QgsProject

        return QgsProject.instance()

    # -- lazy trusted bindings --------------------------------------------

    def _serializer(self):
        export = self._export_fn
        import_ = self._import_fn
        if export is None or import_ is None:
            from ..model3_serializer import Model3Serializer

            export = export or Model3Serializer.export_to_json
            import_ = import_ or Model3Serializer.import_from_json
        return export, import_

    def _catalog_port(self) -> Any:
        if self._catalog is None:
            from .runtime_proposals import _AlgorithmCatalogPort

            self._catalog = _AlgorithmCatalogPort()
        return self._catalog

    def _clone(self):
        if self._clone_fn is not None:
            return self._clone_fn
        from .runtime_proposals import _serializer_clone

        return _serializer_clone

    # -- apply -------------------------------------------------------------

    def apply(self, pending: PendingAction) -> ApplyResult:
        """Atomically apply one pending action, or fail closed with rollback."""
        try:
            if proposal_digest(pending.proposal) != pending.digest:
                return ApplyResult(False, ApplyReason.DIGEST_MISMATCH, "Proposal integrity check failed.")
            if pending.kind == PROPOSAL_KIND_MODEL_PATCH and isinstance(
                pending.proposal, ModelPatchProposal
            ):
                return self._apply_model_patch(pending)
            if pending.kind == PROPOSAL_KIND_LAYER_STYLE and isinstance(
                pending.proposal, LayerStyleProposal
            ):
                return self._apply_layer_style(pending)
        except ProposalError as error:
            return ApplyResult(False, error.reason_code, _sanitize(str(error)))
        except Exception:  # noqa: BLE001 - any apply failure must be sanitized
            return ApplyResult(False, ApplyReason.FAILED, "The action could not be applied.")
        return ApplyResult(False, ProposalReason.UNKNOWN_KIND, "Unknown or mismatched action kind.")

    def _apply_model_patch(self, pending: PendingAction) -> ApplyResult:
        graph = self._model_adapter.current_graph()
        if graph is None:
            return ApplyResult(
                False, ApplyReason.TARGET_MISSING, "No open model to apply the change to."
            )
        canonical = agent_context.canonical_model_state(graph)
        if not self._token_service.verify(
            pending.context_token, MODEL_PROPOSAL_KIND, MODEL_TARGET_ID, canonical
        ):
            return ApplyResult(
                False, ProposalReason.STALE_CONTEXT,
                "The model changed since this was prepared. Inspect it again.",
            )
        export, import_ = self._serializer()
        pre_json = export(graph)
        # Rebuild and validate the candidate on a detached clone again.
        candidate = apply_model_patch_to_clone(
            graph,
            pending.proposal,
            self._catalog_port(),
            clone_fn=self._clone(),
            max_nodes=MAX_CANDIDATE_NODES,
            max_edges=MAX_CANDIDATE_EDGES,
        )
        candidate_json = export(candidate)
        installed = import_(candidate_json)
        if installed is None:
            return ApplyResult(
                False, ProposalReason.VALIDATION_FAILED,
                "The candidate model could not be prepared for apply.",
            )
        try:
            self._model_adapter.install_graph(installed)
        except Exception:  # noqa: BLE001 - roll back to the exact pre-state
            self._restore_model(pre_json)
            return ApplyResult(
                False, ApplyReason.FAILED,
                "The model change could not be applied and was rolled back.",
            )
        # The mutation is committed. Post-commit bookkeeping must never turn a
        # successful apply into a reported failure (which would imply rollback);
        # a fingerprint error only makes Undo unavailable.
        post_fingerprint = ""
        with contextlib.suppress(Exception):
            live = self._model_adapter.current_graph()
            if live is not None:
                post_fingerprint = _hash_text(export(live))
        applied = AppliedAction(
            action_id=pending.action_id,
            kind=PROPOSAL_KIND_MODEL_PATCH,
            target_identity=MODEL_TARGET_ID,
            title=agent_context.bound_text(pending.proposal.title, 160),
            is_destructive=pending.is_destructive,
            post_fingerprint=post_fingerprint,
            model_pre_json=pre_json,
        )
        return ApplyResult(True, applied_action=applied)

    def _restore_model(self, pre_json: str) -> None:
        export, import_ = self._serializer()
        restored = import_(pre_json)
        if restored is not None:
            with contextlib.suppress(Exception):  # best-effort restore; never raise
                self._model_adapter.install_graph(restored)

    def _apply_layer_style(self, pending: PendingAction) -> ApplyResult:
        from qgis.core import QgsProject, QgsRasterLayer, QgsVectorLayer

        from .runtime_tools import extract_layer_style_state

        proposal: LayerStyleProposal = pending.proposal
        family = proposal.renderer.family
        if family not in APPLIABLE_STYLE_FAMILIES:
            return ApplyResult(
                False, ApplyReason.UNSUPPORTED,
                "This renderer family can be previewed but not applied in this version.",
            )
        project = QgsProject.instance()
        layer = project.mapLayer(proposal.target_layer_id) if project is not None else None
        if layer is None:
            return ApplyResult(False, ApplyReason.TARGET_MISSING, "The target layer is not in the project.")
        from .contracts import AgentScope

        if pending.scope == AgentScope.ACTIVE_LAYER:
            active = self._active_layer_provider()
            if active is None or active.id() != layer.id():
                return ApplyResult(
                    False, ApplyReason.TARGET_MISSING,
                    "The proposal target is not the current active layer.",
                )
        if not self._token_service.verify(
            pending.context_token,
            STYLE_PROPOSAL_KIND,
            layer.id(),
            extract_layer_style_state(layer, STYLE_STATE_LIMIT),
        ):
            return ApplyResult(
                False, ProposalReason.STALE_CONTEXT,
                "The layer style changed since this was prepared. Inspect it again.",
            )
        is_vector = isinstance(layer, QgsVectorLayer)
        is_raster = isinstance(layer, QgsRasterLayer)
        if proposal.is_vector_family and not is_vector:
            return ApplyResult(False, ProposalReason.VALIDATION_FAILED, "Not a vector layer.")
        if proposal.is_raster_family and not is_raster:
            return ApplyResult(False, ProposalReason.VALIDATION_FAILED, "Not a raster layer.")

        # Capture the exact pre-state before touching the layer.
        style_pre = self._capture_style_state(layer, project)
        try:
            self._install_style(layer, proposal, is_vector)
        except ProposalError as error:
            self._restore_style(layer, project, style_pre)
            return ApplyResult(False, error.reason_code, _sanitize(str(error)))
        except Exception:  # noqa: BLE001 - roll back every captured component
            self._restore_style(layer, project, style_pre)
            return ApplyResult(
                False, ApplyReason.FAILED,
                "The style change could not be applied and was rolled back.",
            )
        # The mutation is committed. Repaint and fingerprinting are post-commit
        # bookkeeping and must never report failure; a fingerprint error only
        # makes Undo unavailable.
        with contextlib.suppress(Exception):  # repaint is best-effort after success
            layer.triggerRepaint()
        post_fingerprint = ""
        with contextlib.suppress(Exception):
            post_fingerprint = _style_fingerprint(layer)
        applied = AppliedAction(
            action_id=pending.action_id,
            kind=PROPOSAL_KIND_LAYER_STYLE,
            target_identity=layer.id(),
            title=agent_context.bound_text(proposal.title, 160),
            is_destructive=False,
            post_fingerprint=post_fingerprint,
            style_pre=style_pre,
        )
        return ApplyResult(True, applied_action=applied)

    # -- style construction (main thread) ---------------------------------

    def _capture_style_state(self, layer: Any, project: Any) -> Dict[str, Any]:
        state: Dict[str, Any] = {"was_dirty": None, "renderer": None, "labeling": None,
                                 "labels_enabled": None, "opacity": None}
        with contextlib.suppress(Exception):
            state["was_dirty"] = bool(project.isDirty()) if project is not None else None
        with contextlib.suppress(Exception):
            renderer = layer.renderer()
            state["renderer"] = renderer.clone() if renderer is not None else None
        with contextlib.suppress(Exception):
            state["opacity"] = float(layer.opacity())
        with contextlib.suppress(Exception):  # raster or unsupported labeling API
            labeling = layer.labeling()
            state["labeling"] = labeling.clone() if labeling is not None else None
            state["labels_enabled"] = bool(layer.labelsEnabled())
        return state

    def _restore_style(self, layer: Any, project: Any, style_pre: Dict[str, Any]) -> None:
        renderer = style_pre.get("renderer")
        if renderer is not None:
            with contextlib.suppress(Exception):
                layer.setRenderer(renderer.clone())
        opacity = style_pre.get("opacity")
        if opacity is not None:
            with contextlib.suppress(Exception):
                layer.setOpacity(opacity)
        labels_enabled = style_pre.get("labels_enabled")
        if labels_enabled is not None:
            with contextlib.suppress(Exception):
                layer.setLabeling(style_pre.get("labeling"))
                layer.setLabelsEnabled(bool(labels_enabled))
        was_dirty = style_pre.get("was_dirty")
        if was_dirty is not None and project is not None:
            with contextlib.suppress(Exception):
                project.setDirty(bool(was_dirty))
        with contextlib.suppress(Exception):
            layer.triggerRepaint()

    def _install_style(self, layer: Any, proposal: LayerStyleProposal, is_vector: bool) -> None:
        family = proposal.renderer.family
        opacity = proposal.renderer.opacity
        if family != "keep":
            renderer = self._build_renderer(layer, proposal, is_vector)
            layer.setRenderer(renderer)
        with contextlib.suppress(Exception):  # opacity is best-effort per layer kind
            layer.setOpacity(float(opacity))
        if is_vector:
            self._apply_labels(layer, proposal)

    def _build_renderer(self, layer: Any, proposal: LayerStyleProposal, is_vector: bool) -> Any:
        family = proposal.renderer.family
        if family == "single_symbol":
            return self._single_symbol_renderer(layer, proposal)
        if family == "categorized":
            return self._categorized_renderer(layer, proposal)
        if family == "graduated":
            return self._graduated_renderer(layer, proposal)
        if family == "raster_gray":
            return self._raster_gray_renderer(layer)
        raise ProposalError("Unsupported renderer family for apply.", ApplyReason.UNSUPPORTED)

    def _single_symbol_renderer(self, layer: Any, proposal: LayerStyleProposal) -> Any:
        from qgis.core import QgsSingleSymbolRenderer, QgsSymbol
        from qgis.PyQt.QtGui import QColor

        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        if symbol is None:
            raise ProposalError("No default symbol for this layer geometry.", ApplyReason.FAILED)
        if proposal.renderer.palette:
            symbol.setColor(QColor(proposal.renderer.palette[0]))
        return QgsSingleSymbolRenderer(symbol)

    def _categorized_renderer(self, layer: Any, proposal: LayerStyleProposal) -> Any:
        from qgis.core import (
            QgsCategorizedSymbolRenderer,
            QgsRendererCategory,
            QgsSymbol,
        )
        from qgis.PyQt.QtGui import QColor

        field = proposal.renderer.field
        idx = layer.fields().indexOf(field)
        if idx < 0:
            raise ProposalError("The classification field is not on the layer.", ProposalReason.VALIDATION_FAILED)
        # Read unique values locally; they never leave this process.
        values = list(layer.uniqueValues(idx, limit=MAX_CATEGORY_VALUES + 1))
        if len(values) > proposal.renderer.class_count or len(values) > MAX_CATEGORY_VALUES:
            raise ProposalError(
                "The field has more distinct values than the proposed class count.",
                ProposalReason.VALIDATION_FAILED,
            )
        palette = list(proposal.renderer.palette)
        categories = []
        for i, value in enumerate(values):
            symbol = QgsSymbol.defaultSymbol(layer.geometryType())
            if symbol is None:
                raise ProposalError("No default symbol for this layer geometry.", ApplyReason.FAILED)
            if palette:
                symbol.setColor(QColor(palette[i % len(palette)]))
            categories.append(QgsRendererCategory(value, symbol, str(value)))
        if not categories:
            raise ProposalError("No categories could be built.", ProposalReason.VALIDATION_FAILED)
        return QgsCategorizedSymbolRenderer(field, categories)

    def _graduated_renderer(self, layer: Any, proposal: LayerStyleProposal) -> Any:
        from qgis.core import (
            QgsClassificationEqualInterval,
            QgsGradientColorRamp,
            QgsGraduatedSymbolRenderer,
            QgsSymbol,
        )
        from qgis.PyQt.QtGui import QColor

        field = proposal.renderer.field
        idx = layer.fields().indexOf(field)
        if idx < 0:
            raise ProposalError("The classification field is not on the layer.", ProposalReason.VALIDATION_FAILED)
        if not layer.fields().at(idx).isNumeric():
            raise ProposalError("A graduated renderer needs a numeric field.", ProposalReason.VALIDATION_FAILED)
        renderer = QgsGraduatedSymbolRenderer(field)
        symbol = QgsSymbol.defaultSymbol(layer.geometryType())
        if symbol is None:
            raise ProposalError("No default symbol for this layer geometry.", ApplyReason.FAILED)
        renderer.setSourceSymbol(symbol)
        palette = list(proposal.renderer.palette)
        ramp = QgsGradientColorRamp(
            QColor(palette[0]), QColor(palette[-1])
        ) if palette else None
        with contextlib.suppress(Exception):  # older API fallback below
            renderer.setClassificationMethod(QgsClassificationEqualInterval())
        renderer.updateClasses(
            layer, QgsGraduatedSymbolRenderer.Mode.EqualInterval, proposal.renderer.class_count
        )
        if ramp is not None:
            with contextlib.suppress(Exception):
                renderer.updateColorRamp(ramp)
        return renderer

    def _raster_gray_renderer(self, layer: Any) -> Any:
        from qgis.core import QgsContrastEnhancement, QgsSingleBandGrayRenderer

        provider = layer.dataProvider()
        if provider is None or layer.bandCount() < 1:
            raise ProposalError("The raster has no readable band.", ProposalReason.VALIDATION_FAILED)
        renderer = QgsSingleBandGrayRenderer(provider, 1)
        with contextlib.suppress(Exception):  # a plain gray renderer is still valid
            enhancement = QgsContrastEnhancement(provider.dataType(1))
            enhancement.setContrastEnhancementAlgorithm(
                QgsContrastEnhancement.ContrastEnhancementAlgorithm.StretchToMinimumMaximum
            )
            stats = provider.bandStatistics(1)
            enhancement.setMinimumValue(stats.minimumValue)
            enhancement.setMaximumValue(stats.maximumValue)
            renderer.setContrastEnhancement(enhancement)
        return renderer

    def _apply_labels(self, layer: Any, proposal: LayerStyleProposal) -> None:
        from qgis.core import QgsPalLayerSettings, QgsVectorLayerSimpleLabeling

        if not proposal.labels.enabled:
            layer.setLabelsEnabled(False)
            return
        settings = QgsPalLayerSettings()
        settings.fieldName = proposal.labels.field
        settings.enabled = True
        layer.setLabeling(QgsVectorLayerSimpleLabeling(settings))
        layer.setLabelsEnabled(True)

    # -- run results -------------------------------------------------------

    def record_run_result(
        self,
        action_id: str,
        kind: str,
        target_identity: str,
        title: str,
        layer_ids: Sequence[str],
    ) -> Optional[AppliedAction]:
        """Fingerprint a finished run's result layers so Undo can remove them.

        Returns ``None`` when the run added nothing (there is then nothing to
        undo) or when a recorded layer cannot be fingerprinted, so Undo is
        offered only for a result whose identity is actually known.
        """
        if kind not in RUN_KINDS:
            return None
        recorded = []
        for layer_id in list(layer_ids)[:MAX_UNDOABLE_RESULT_LAYERS]:
            layer = self._resolve_layer(layer_id)
            if layer is None:
                return None
            try:
                recorded.append((layer.id(), result_layer_fingerprint(layer)))
            except Exception:  # noqa: BLE001 - an unfingerprintable result blocks Undo
                return None
        if not recorded:
            return None
        return AppliedAction(
            action_id=action_id,
            kind=kind,
            target_identity=target_identity,
            title=agent_context.bound_text(str(title), 160),
            # Removing layers the run itself created is reversible in intent but
            # destructive in effect; the card labels it accordingly.
            is_destructive=True,
            post_fingerprint="",
            result_layers=tuple(recorded),
        )

    def _run_results_intact(self, applied: AppliedAction) -> bool:
        """Whether every recorded result layer still exists with its identity."""
        if not applied.result_layers:
            return False
        for layer_id, fingerprint in applied.result_layers:
            layer = self._resolve_layer(layer_id)
            if layer is None:
                return False
            try:
                if result_layer_fingerprint(layer) != fingerprint:
                    return False
            except Exception:  # noqa: BLE001
                return False
        return True

    def _undo_run(self, applied: AppliedAction) -> UndoResult:
        project = self._project()
        if project is None:
            return UndoResult(False, ApplyReason.TARGET_MISSING, "The project is not available.")
        for layer_id, _fingerprint in applied.result_layers:
            project.removeMapLayer(layer_id)
        return UndoResult(True)

    # -- undo --------------------------------------------------------------

    def can_undo(self, applied: Optional[AppliedAction]) -> bool:
        if applied is None:
            return False
        if applied.kind in RUN_KINDS:
            return self._run_results_intact(applied)
        if applied.kind == PROPOSAL_KIND_MODEL_PATCH:
            live = self._model_adapter.current_graph()
            if live is None:
                return False
            export, _ = self._serializer()
            try:
                return _hash_text(export(live)) == applied.post_fingerprint
            except Exception:  # noqa: BLE001
                return False
        if applied.kind == PROPOSAL_KIND_LAYER_STYLE:
            layer = self._resolve_layer(applied.target_identity)
            if layer is None:
                return False
            try:
                return _style_fingerprint(layer) == applied.post_fingerprint
            except Exception:  # noqa: BLE001
                return False
        return False

    def undo(self, applied: Optional[AppliedAction]) -> UndoResult:
        if not self.can_undo(applied):
            return UndoResult(
                False, ApplyReason.UNDO_NOT_ELIGIBLE,
                "The target changed since this action, so Undo is disabled.",
            )
        try:
            if applied.kind in RUN_KINDS:
                return self._undo_run(applied)
            if applied.kind == PROPOSAL_KIND_MODEL_PATCH:
                return self._undo_model(applied)
            if applied.kind == PROPOSAL_KIND_LAYER_STYLE:
                return self._undo_style(applied)
        except Exception:  # noqa: BLE001 - an undo failure must be sanitized
            return UndoResult(False, ApplyReason.FAILED, "The action could not be undone.")
        return UndoResult(False, ProposalReason.UNKNOWN_KIND, "Unknown action kind.")

    def _undo_model(self, applied: AppliedAction) -> UndoResult:
        _, import_ = self._serializer()
        restored = import_(applied.model_pre_json or "")
        if restored is None:
            return UndoResult(False, ApplyReason.FAILED, "The prior model could not be restored.")
        self._model_adapter.install_graph(restored)
        return UndoResult(True)

    def _undo_style(self, applied: AppliedAction) -> UndoResult:
        from qgis.core import QgsProject

        layer = self._resolve_layer(applied.target_identity)
        if layer is None:
            return UndoResult(False, ApplyReason.TARGET_MISSING, "The target layer is gone.")
        project = QgsProject.instance()
        self._restore_style(layer, project, applied.style_pre or {})
        return UndoResult(True)

    def _resolve_layer(self, layer_id: str) -> Any:
        project = self._project()
        return project.mapLayer(layer_id) if project is not None else None
