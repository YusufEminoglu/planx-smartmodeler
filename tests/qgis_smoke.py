"""Run with OSGeo4W python-qgis to verify live QGIS 4 integration."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from qgis.PyQt.QtGui import QIcon
from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingOutputString,
    QgsProject,
    QgsVectorLayer,
    Qgis,
)


def run_checks() -> str:
    plugin_root = Path(__file__).resolve().parents[2]
    plugin_root_text = str(plugin_root)
    while plugin_root_text in sys.path:
        sys.path.remove(plugin_root_text)
    sys.path.insert(0, plugin_root_text)
    for module_name in list(sys.modules):
        if module_name == "planx_smartmodeler" or module_name.startswith(
            "planx_smartmodeler."
        ):
            del sys.modules[module_name]

    import qgis.utils as qgis_utils_probe

    from planx_smartmodeler.core.agent.contracts import (
        AgentMode,
        AgentResultStatus,
        AgentScope,
        AgentToolCall,
    )
    from planx_smartmodeler.core.agent.controller import AgentController
    from planx_smartmodeler.core.agent.runtime_tools import build_default_registry
    from planx_smartmodeler.core.algorithm_catalog import AlgorithmCatalog
    from planx_smartmodeler.core.ai_client import AiNetworkClient
    from planx_smartmodeler.core.ai_mcp_bridge import AiMcpBridge
    from planx_smartmodeler.core.ai_settings import (
        AiProfile,
        AiSettingsStore,
        scoped_ai_settings_isolation,
    )
    from planx_smartmodeler.core.execution_engine import GraphExecutionEngine
    from planx_smartmodeler.core.graph_model import GraphModel
    from planx_smartmodeler.core.model3_serializer import Model3Serializer
    from planx_smartmodeler.gui.agent_dock import AgentWorkspaceDock
    from planx_smartmodeler.gui.ai_prompt_widget import AiPromptWidget
    from planx_smartmodeler.gui.ai_settings_dialog import AiSettingsDialog
    from planx_smartmodeler.gui.canvas_scene import CanvasScene
    from planx_smartmodeler.gui.canvas_view import CanvasView
    from planx_smartmodeler.gui.node_parameter_dialog import NodeParameterDialog
    from planx_smartmodeler.gui.node_palette_widget import NodePaletteWidget
    from planx_smartmodeler.main_plugin import SmartModelerPlugin

    with scoped_ai_settings_isolation():
        records = AlgorithmCatalog.records()
        if len(records) < 10:
            raise RuntimeError("Processing registry did not load enough algorithms.")
        if not AlgorithmCatalog.algorithm_exists("native:buffer"):
            raise RuntimeError("native:buffer is unavailable.")
        preserved_catalog = AlgorithmCatalog.compact_ai_catalog(
            "add a report", 5, ["native:buffer"]
        )
        if "native:buffer" not in preserved_catalog:
            raise RuntimeError("Existing workflow algorithms were omitted from AI context.")

        gemini_profile = AiProfile.create("gemini", "Gemini smoke")
        _endpoint, _headers, gemini_payload = AiNetworkClient.build_request(
            gemini_profile, "credential", "Return JSON.", "Build a workflow."
        )
        gemini_config = gemini_payload["generationConfig"]
        if (
            gemini_config.get("responseMimeType") != "application/json"
            or "responseJsonSchema" not in gemini_config
            or "responseFormat" in gemini_config
        ):
            raise RuntimeError("Gemini request contract is invalid.")

        deepseek_profile = AiProfile.create("deepseek", "DeepSeek smoke")
        deepseek_endpoint, _headers, deepseek_payload = AiNetworkClient.build_request(
            deepseek_profile, "credential", "Return JSON.", "Build a workflow."
        )
        if (
            deepseek_endpoint != "https://api.deepseek.com/chat/completions"
            or deepseek_payload.get("model") != "deepseek-v4-flash"
            or deepseek_payload.get("response_format") != {"type": "json_object"}
        ):
            raise RuntimeError("DeepSeek request contract is invalid.")

        graph = GraphModel("SmartModeler smoke test")
        source = AlgorithmCatalog.create_node("smart:input_layer", "source")
        buffer_node = AlgorithmCatalog.create_node("native:buffer", "buffer")
        project = QgsProject.instance()
        original_layer_ids = set(project.mapLayers())
        input_layer = QgsVectorLayer("Point?crs=EPSG:3857", "smoke_points", "memory")
        feature = QgsFeature()
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0, 0)))
        input_layer.dataProvider().addFeature(feature)
        input_layer.updateExtents()
        project.addMapLayer(input_layer)

        auto_graph = GraphModel("Automatic layer binding")
        auto_buffer = AlgorithmCatalog.create_node("native:buffer", "auto_buffer")
        auto_graph.add_node(auto_buffer)
        if (
            AlgorithmCatalog.autobind_unique_project_layers(auto_graph) != 1
            or auto_buffer.parameters.get("INPUT") != input_layer.id()
        ):
            raise RuntimeError("A unique compatible project layer was not auto-bound.")

        source.parameters["LAYER"] = input_layer.id()
        buffer_node.parameters["DISTANCE"] = 10.0
        graph.add_node(source)
        graph.add_node(buffer_node)
        edge = graph.add_edge("source", "OUTPUT", "buffer", "INPUT")
        if edge is None:
            raise RuntimeError(graph.last_error)

        report = GraphExecutionEngine().execute(graph)
        if report.executed_nodes != 2 or not report.added_layers:
            raise RuntimeError("Processing execution did not load the buffer result.")

        scene = CanvasScene(graph)
        for node in graph.nodes.values():
            scene.add_node_to_scene(node)
        for graph_edge in graph.edges.values():
            scene.add_connection_to_scene(graph_edge)
        view = CanvasView(scene)
        prompt = AiPromptWidget()
        prompt.set_workflow_available(True)
        palette = NodePaletteWidget()
        settings_dialog = AiSettingsDialog()
        icon_path = plugin_root / "planx_smartmodeler" / "icons" / "icon.png"
        icon = QIcon(str(icon_path))
        if (
            not view.scene()
            or not prompt.isEnabled()
            or prompt.mode_combo.currentData() != "improve"
            or prompt.generate_button.text() != "Improve workflow"
            or palette.tree.topLevelItemCount() == 0
            or settings_dialog.key_status.text() == ""
            or icon.isNull()
        ):
            raise RuntimeError("Qt widgets did not initialize.")
        settings_dialog.close()

        class _FakeIface:
            def mainWindow(self):
                return None

            def addPluginToVectorMenu(self, _name, _action):
                pass

            def removePluginVectorMenu(self, _name, _action):
                pass

            def addVectorToolBarIcon(self, _action):
                pass

            def removeVectorToolBarIcon(self, _action):
                pass

            def addDockWidget(self, _area, _dock):
                pass

            def removeDockWidget(self, _dock):
                pass

        fake_iface = _FakeIface()
        lifecycle_plugin = SmartModelerPlugin(fake_iface)
        if lifecycle_plugin._current_graph() is not None:
            raise RuntimeError(
                "Agent Workspace reported a model before the studio was ever constructed."
            )
        lifecycle_plugin.initGui()
        try:
            if lifecycle_plugin._current_graph() is not None:
                raise RuntimeError(
                    "Agent Workspace reported a model before Workflow Studio ever ran."
                )
            lifecycle_plugin.run()
            if lifecycle_plugin._current_graph() is None:
                raise RuntimeError(
                    "Agent Workspace did not report the model while the studio was visible."
                )
            lifecycle_plugin.window.hide()
            if lifecycle_plugin._current_graph() is not None:
                raise RuntimeError(
                    "Agent Workspace still reported a model after the studio was hidden."
                )
            if lifecycle_plugin.window is None:
                raise RuntimeError("Hiding the studio window destroyed it instead of hiding it.")
            lifecycle_plugin.window.show()
            if lifecycle_plugin._current_graph() is None:
                raise RuntimeError(
                    "Agent Workspace did not report the model again after the studio was reopened."
                )
        finally:
            lifecycle_plugin.unload()
        if lifecycle_plugin._current_graph() is not None:
            raise RuntimeError("Agent Workspace reported a model after unload().")

        expected_agent_tools = {
            "project.summary",
            "layer.list",
            "layer.describe",
            "processing.search",
            "processing.describe",
            "model.summary",
            "model.validate",
            "plugin.list",
            "layer.style",
            "model.describe",
            "plugin.describe",
        }
        empty_dock = AgentWorkspaceDock(None, lambda: None)
        registry_tool_names = {spec.name for spec in empty_dock.registry.list_specs()}
        if registry_tool_names != expected_agent_tools:
            raise RuntimeError(
                "The Agent Workspace registry must contain exactly eleven tools."
            )
        if empty_dock.scope_combo.count() != 4 or empty_dock.mode_combo.count() != 3:
            raise RuntimeError("Agent Workspace dock did not construct its selectors under Qt 6.")
        if empty_dock.mode_combo.itemData(2) != AgentMode.ACT or empty_dock.mode_combo.itemText(
            2
        ) != "Act (approve to apply)":
            raise RuntimeError("The Act option is not presented honestly (approve to apply).")
        from qgis.PyQt.QtWidgets import QPushButton

        # Phase 04 adds exactly one explicit-approval Apply plus Reject and a
        # single-level Undo. Any auto/bulk/execution action remains forbidden.
        forbidden_actions = (
            "accept", "approve", "execute", "run", "commit", "export", "save",
            "approve all", "apply all",
        )
        for button in empty_dock.findChildren(QPushButton):
            label = button.text().strip().lower()
            if any(word == label or label.startswith(word + " ") for word in forbidden_actions):
                raise RuntimeError(f"Agent Workspace exposed a forbidden action button: {button.text()!r}")
        # A fresh dock has no pending action: Apply/Reject are disabled and Apply
        # is never the default button; the approval card is hidden.
        if empty_dock.apply_button.isEnabled() or empty_dock.reject_button.isEnabled():
            raise RuntimeError("Apply/Reject were enabled with no pending action.")
        if empty_dock.apply_button.isDefault() or empty_dock.apply_button.autoDefault():
            raise RuntimeError("Apply must never be the default button.")
        if empty_dock.approval_group.isVisible() or empty_dock.undo_button.isEnabled():
            raise RuntimeError("The approval card/Undo were active with no pending action.")
        if empty_dock.proposal_view is None or empty_dock.proposal_group is None:
            raise RuntimeError("Agent Workspace did not construct its read-only proposal preview.")

        no_model_controller = AgentController(empty_dock.registry)
        no_model_result = no_model_controller.execute(
            AgentToolCall(call_id="smoke_model_missing", tool_name="model.summary"),
            AgentMode.ASK,
            AgentScope.CURRENT_MODEL,
        )
        if (
            no_model_result.status != AgentResultStatus.SUCCESS
            or no_model_result.data.get("available") is not False
        ):
            raise RuntimeError("model.summary did not report an absent model provider correctly.")

        live_registry = build_default_registry(lambda: graph)
        live_controller = AgentController(live_registry)
        live_model_result = live_controller.execute(
            AgentToolCall(call_id="smoke_model_present", tool_name="model.summary"),
            AgentMode.ASK,
            AgentScope.CURRENT_MODEL,
        )
        if (
            live_model_result.status != AgentResultStatus.SUCCESS
            or live_model_result.data.get("available") is not True
            or live_model_result.data.get("node_count") != 2
        ):
            raise RuntimeError("model.summary did not describe the open SmartModeler graph.")

        project_result = live_controller.execute(
            AgentToolCall(call_id="smoke_project_summary", tool_name="project.summary"),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        if project_result.status != AgentResultStatus.SUCCESS or "title" not in project_result.data:
            raise RuntimeError("project.summary failed against the smoke project.")

        layer_list_result = live_controller.execute(
            AgentToolCall(call_id="smoke_layer_list", tool_name="layer.list"),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        if layer_list_result.status != AgentResultStatus.SUCCESS or not layer_list_result.data["layers"]:
            raise RuntimeError("layer.list did not return the smoke project layer.")
        layer_entry_text = str(layer_list_result.data)
        if "memory?" in layer_entry_text or "Point?crs=" in layer_entry_text:
            raise RuntimeError("layer.list leaked a source URI into the agent context.")

        layer_describe_result = live_controller.execute(
            AgentToolCall(
                call_id="smoke_layer_describe",
                tool_name="layer.describe",
                arguments={"layer_id": input_layer.id()},
            ),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        if (
            layer_describe_result.status != AgentResultStatus.SUCCESS
            or layer_describe_result.data.get("available") is not True
        ):
            raise RuntimeError("layer.describe failed against the smoke layer.")
        describe_text = str(layer_describe_result.data)
        if "memory?" in describe_text or "POINT(" in describe_text.upper():
            raise RuntimeError("layer.describe leaked a source URI or feature value.")

        missing_layer_id_result = live_controller.execute(
            AgentToolCall(call_id="smoke_layer_describe_missing", tool_name="layer.describe"),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        if (
            missing_layer_id_result.status != AgentResultStatus.FAILED
            or missing_layer_id_result.reason_code != "invalid_arguments"
        ):
            raise RuntimeError(
                "layer.describe did not fail closed on a missing required argument."
            )
        oversized_query_result = live_controller.execute(
            AgentToolCall(
                call_id="smoke_processing_search_oversized",
                tool_name="processing.search",
                arguments={"query": "buffer" * 100},
            ),
            AgentMode.ASK,
            AgentScope.PROJECT,
        )
        if (
            oversized_query_result.status != AgentResultStatus.FAILED
            or oversized_query_result.reason_code != "invalid_arguments"
        ):
            raise RuntimeError(
                "processing.search did not fail closed on an over-length query argument."
            )

        available_names = set(getattr(qgis_utils_probe, "available_plugins", []) or [])
        active_names = set(getattr(qgis_utils_probe, "active_plugins", []) or [])
        plugin_list_result = live_controller.execute(
            AgentToolCall(
                call_id="smoke_plugin_list", tool_name="plugin.list", arguments={"limit": 100}
            ),
            AgentMode.ASK,
            AgentScope.PLUGINS,
        )
        if plugin_list_result.status != AgentResultStatus.SUCCESS:
            raise RuntimeError("plugin.list failed in the smoke environment.")
        listed_plugins = {item["package_name"]: item for item in plugin_list_result.data["plugins"]}
        if not plugin_list_result.data["truncated"]:
            missing_available = available_names - set(listed_plugins)
            if missing_available:
                raise RuntimeError(
                    "plugin.list omitted available plugin package(s): "
                    f"{sorted(missing_available)[:5]}"
                )
        inactive_available = available_names - active_names
        reported_inactive = [
            name
            for name in inactive_available
            if name in listed_plugins and listed_plugins[name]["enabled"] is False
        ]
        if inactive_available and not reported_inactive:
            raise RuntimeError(
                "plugin.list did not report any available-but-inactive plugin as enabled: false."
            )

        # -- Phase 03: rich read-only tools + inert validated proposals ----
        import json as _json

        from qgis.core import (
            QgsCategorizedSymbolRenderer,
            QgsPalLayerSettings,
            QgsRendererCategory,
            QgsSymbol,
            QgsVectorLayerSimpleLabeling,
        )

        from planx_smartmodeler.core.agent.run_loop import RunEventKind

        def _agent_turn(action, assistant_text="", tool_calls=None, kind="none", proposal_json=""):
            return _json.dumps(
                {
                    "action": action,
                    "assistant_text": assistant_text,
                    "tool_calls": tool_calls or [],
                    "proposal_kind": kind,
                    "proposal_json": proposal_json,
                }
            )

        style_layer = QgsVectorLayer(
            "Polygon?crs=EPSG:3857&field=name:string&field=pop:integer", "style_probe", "memory"
        )
        symbol = QgsSymbol.defaultSymbol(style_layer.geometryType())
        category = QgsRendererCategory(1, symbol, "SENTINEL_CATEGORY_LABEL")
        style_layer.setRenderer(QgsCategorizedSymbolRenderer("pop", [category]))
        label_settings = QgsPalLayerSettings()
        label_settings.fieldName = "concat('SENTINEL_LABEL_EXPRESSION', \"name\")"
        label_settings.isExpression = True
        style_layer.setLabeling(QgsVectorLayerSimpleLabeling(label_settings))
        style_layer.setLabelsEnabled(True)
        project.addMapLayer(style_layer)

        proposal_dock = AgentWorkspaceDock(fake_iface, lambda: graph)

        style_result = proposal_dock.controller.execute(
            AgentToolCall(
                call_id="smoke_layer_style",
                tool_name="layer.style",
                arguments={"layer_id": style_layer.id()},
            ),
            AgentMode.PLAN,
            AgentScope.PROJECT,
        )
        if style_result.status != AgentResultStatus.SUCCESS or not style_result.data.get("context_token"):
            raise RuntimeError("layer.style did not return a bounded context token.")
        style_text = str(style_result.data)
        for leaked in ("SENTINEL_CATEGORY_LABEL", "SENTINEL_LABEL_EXPRESSION", "Polygon?crs=", "memory?"):
            if leaked in style_text:
                raise RuntimeError(f"layer.style leaked forbidden content: {leaked}")
        if style_result.data.get("classification_field") != "pop":
            raise RuntimeError("layer.style did not detect the real classification field.")
        if style_result.data.get("label_expression_present") is not True:
            raise RuntimeError("layer.style did not flag a label expression without exposing it.")

        model_describe_result = proposal_dock.controller.execute(
            AgentToolCall(call_id="smoke_model_describe", tool_name="model.describe"),
            AgentMode.PLAN,
            AgentScope.CURRENT_MODEL,
        )
        if model_describe_result.status != AgentResultStatus.SUCCESS or not model_describe_result.data.get(
            "context_token"
        ):
            raise RuntimeError("model.describe did not return topology plus a context token.")
        describe_text = str(model_describe_result.data)
        if input_layer.id() in describe_text or "10.0" in describe_text:
            raise RuntimeError("model.describe leaked a baseline parameter/path value.")
        model_token = model_describe_result.data["context_token"]

        plugin_describe_result = proposal_dock.controller.execute(
            AgentToolCall(
                call_id="smoke_plugin_describe",
                tool_name="plugin.describe",
                arguments={"package_name": "planx_smartmodeler"},
            ),
            AgentMode.ASK,
            AgentScope.PLUGINS,
        )
        if plugin_describe_result.status != AgentResultStatus.SUCCESS:
            raise RuntimeError("plugin.describe failed for the SmartModeler package.")

        model_serialization_before = Model3Serializer.export_to_json(graph)
        valid_patch = _json.dumps(
            {
                "schema_version": 1,
                "context_token": model_token,
                "title": "Rename the model",
                "summary": "Give the workflow a clearer name.",
                "operations": [
                    {"op": "set_model_metadata", "name": "Renamed by proposal", "description": "d"}
                ],
                "warnings": [],
            }
        )

        def _feed_proposal(mode, scope, kind, proposal_json):
            start = proposal_dock.run_loop.start("propose", mode, scope)
            return proposal_dock.run_loop.submit_provider_response(
                start.request.request_token,
                _agent_turn("proposal", "Here.", kind=kind, proposal_json=proposal_json),
            )

        proposal_event = _feed_proposal(
            AgentMode.PLAN, AgentScope.CURRENT_MODEL, "model_patch", valid_patch
        )
        if proposal_event is None or proposal_event.kind != RunEventKind.PROPOSAL:
            raise RuntimeError("A valid model proposal did not reach a PROPOSAL event in Plan mode.")
        if Model3Serializer.export_to_json(graph) != model_serialization_before:
            raise RuntimeError("Validating a model proposal changed the live graph.")
        if graph.name == "Renamed by proposal":
            raise RuntimeError("A proposal was applied to the live graph.")

        # A stale proposal (graph changed after the token was issued) must reject.
        graph.nodes["buffer"].title = "Buffer (touched)"
        model_serialization_after_touch = Model3Serializer.export_to_json(graph)
        stale_event = _feed_proposal(
            AgentMode.PLAN, AgentScope.CURRENT_MODEL, "model_patch", valid_patch
        )
        if (
            stale_event is None
            or stale_event.kind != RunEventKind.FAILED
            or stale_event.reason_code != "stale_proposal_context"
        ):
            raise RuntimeError("A stale model proposal was not rejected after a graph change.")

        # A valid style proposal must render without changing renderer/labels/opacity/dirty.
        style_token = style_result.data["context_token"]
        renderer_before = style_layer.renderer()
        opacity_before = style_layer.opacity()
        labels_before = style_layer.labelsEnabled()
        dirty_before = project.isDirty()
        style_proposal = _json.dumps(
            {
                "schema_version": 1,
                "context_token": style_token,
                "target_layer_id": style_layer.id(),
                "title": "Keep current style",
                "summary": "No structural change, just confirm.",
                "renderer": {"family": "keep", "field": "", "class_count": 0, "palette": [], "opacity": 1.0},
                "labels": {"enabled": False, "field": ""},
                "warnings": [],
            }
        )
        style_event = _feed_proposal(
            AgentMode.PLAN, AgentScope.PROJECT, "layer_style", style_proposal
        )
        if style_event is None or style_event.kind != RunEventKind.PROPOSAL:
            raise RuntimeError("A valid style proposal did not reach a PROPOSAL event.")
        if (
            style_layer.renderer() is not renderer_before
            or style_layer.opacity() != opacity_before
            or style_layer.labelsEnabled() != labels_before
            or project.isDirty() != dirty_before
        ):
            raise RuntimeError("A style proposal changed live renderer/labels/opacity/project state.")

        # An Ask-mode proposal must be rejected before any live validation.
        ask_event = _feed_proposal(
            AgentMode.ASK, AgentScope.CURRENT_MODEL, "model_patch", valid_patch
        )
        if (
            ask_event is None
            or ask_event.kind != RunEventKind.FAILED
            or ask_event.reason_code != "proposal_not_allowed_in_ask"
        ):
            raise RuntimeError("An Ask-mode proposal was not rejected.")

        # A style proposal in ACTIVE_LAYER scope must reject when the fake iface
        # has no matching active layer (target mismatch, before any mutation).
        active_mismatch = _feed_proposal(
            AgentMode.PLAN, AgentScope.ACTIVE_LAYER, "layer_style", style_proposal
        )
        if (
            active_mismatch is None
            or active_mismatch.kind != RunEventKind.FAILED
            or active_mismatch.reason_code != "proposal_target_missing"
        ):
            raise RuntimeError("An ACTIVE_LAYER style target mismatch was not rejected.")

        # An old style token must be rejected after a represented style change.
        style_layer.setOpacity(0.42)
        stale_style = _feed_proposal(
            AgentMode.PLAN, AgentScope.PROJECT, "layer_style", style_proposal
        )
        if (
            stale_style is None
            or stale_style.kind != RunEventKind.FAILED
            or stale_style.reason_code != "stale_proposal_context"
        ):
            raise RuntimeError("A stale style proposal was not rejected after a style change.")
        style_layer.setOpacity(opacity_before)

        # Defense-in-depth: the runtime validator itself rejects an invalid mode
        # and an incompatible scope, independently of the run loop.
        from planx_smartmodeler.core.agent.proposals import parse_proposal as _parse_proposal

        parsed_patch = _parse_proposal("model_patch", valid_patch)
        bad_mode = proposal_dock._proposal_validator.validate(
            "model_patch", parsed_patch, "invalid_mode", AgentScope.CURRENT_MODEL
        )
        bad_scope = proposal_dock._proposal_validator.validate(
            "model_patch", parsed_patch, AgentMode.PLAN, AgentScope.PROJECT
        )
        if bad_mode.ok or bad_scope.ok or bad_scope.reason_code != "proposal_scope_mismatch":
            raise RuntimeError("The runtime validator did not fail closed on mode/scope.")

        # A deterministic in-memory raster fixture: a raster layer.style summary
        # plus a compatible inert raster_gray proposal.
        import tempfile as _tempfile

        from osgeo import gdal as _gdal
        from osgeo import osr as _osr

        raster_tmp = _tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        raster_tmp.close()
        raster_id = None
        try:
            driver = _gdal.GetDriverByName("GTiff")
            dataset = driver.Create(raster_tmp.name, 2, 2, 1, _gdal.GDT_Byte)
            dataset.SetGeoTransform((0.0, 1.0, 0.0, 2.0, 0.0, -1.0))
            srs = _osr.SpatialReference()
            srs.ImportFromEPSG(3857)
            dataset.SetProjection(srs.ExportToWkt())
            dataset.GetRasterBand(1).Fill(1)
            dataset = None
            from qgis.core import QgsRasterLayer

            raster_layer = QgsRasterLayer(raster_tmp.name, "raster_probe", "gdal")
            if not raster_layer.isValid():
                raise RuntimeError("The in-memory raster fixture was not valid.")
            project.addMapLayer(raster_layer)
            raster_id = raster_layer.id()
            raster_style = proposal_dock.controller.execute(
                AgentToolCall(
                    call_id="smoke_raster_style",
                    tool_name="layer.style",
                    arguments={"layer_id": raster_id},
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            if (
                raster_style.status != AgentResultStatus.SUCCESS
                or raster_style.data.get("kind") != "raster"
                or not raster_style.data.get("context_token")
            ):
                raise RuntimeError("layer.style did not summarize the raster fixture.")
            if raster_tmp.name in str(raster_style.data):
                raise RuntimeError("layer.style leaked the raster source path.")
            raster_renderer_before = raster_layer.renderer()
            raster_proposal = _json.dumps(
                {
                    "schema_version": 1,
                    "context_token": raster_style.data["context_token"],
                    "target_layer_id": raster_id,
                    "title": "Grayscale raster",
                    "summary": "Render the raster as single-band gray.",
                    "renderer": {
                        "family": "raster_gray",
                        "field": "",
                        "class_count": 0,
                        "palette": [],
                        "opacity": 1.0,
                    },
                    "labels": {"enabled": False, "field": ""},
                    "warnings": [],
                }
            )
            raster_event = _feed_proposal(
                AgentMode.PLAN, AgentScope.PROJECT, "layer_style", raster_proposal
            )
            if raster_event is None or raster_event.kind != RunEventKind.PROPOSAL:
                raise RuntimeError("A valid raster style proposal did not reach a PROPOSAL event.")
            if raster_layer.renderer() is not raster_renderer_before:
                raise RuntimeError("A raster style proposal changed the live renderer.")
        finally:
            if raster_id is not None:
                project.removeMapLayer(raster_id)
            os.unlink(raster_tmp.name)

        # After every rejection/validation path the live graph is unchanged.
        if Model3Serializer.export_to_json(graph) != model_serialization_after_touch:
            raise RuntimeError("A rejected proposal path changed the live graph.")

        # -- Phase 04: explicit approval, atomic apply and safe Undo ---------
        class _SmokeModelAdapter:
            def __init__(self, model):
                self._graph = model
                self.fail = False

            def current_graph(self):
                return self._graph

            def install_graph(self, model):
                if self.fail:
                    raise RuntimeError("injected install failure")
                self._graph = model

        apply_graph = Model3Serializer.import_from_json(Model3Serializer.export_to_json(graph))
        adapter = _SmokeModelAdapter(apply_graph)
        apply_dock = AgentWorkspaceDock(fake_iface, adapter.current_graph, model_apply=adapter)

        def _feed_act(dock, scope, kind, proposal_json):
            start = dock.run_loop.start("propose", AgentMode.ACT, scope)
            event = dock.run_loop.submit_provider_response(
                start.request.request_token,
                _agent_turn("proposal", "Here.", kind=kind, proposal_json=proposal_json),
            )
            dock._handle_run_event(event)
            return event

        def _model_token(dock):
            md = dock.controller.execute(
                AgentToolCall(call_id="p4_md", tool_name="model.describe"),
                AgentMode.PLAN, AgentScope.CURRENT_MODEL,
            )
            return md.data["context_token"]

        def _rename_patch(token, name):
            return _json.dumps({
                "schema_version": 1, "context_token": token, "title": "Rename model",
                "summary": "Give the workflow a clearer name.",
                "operations": [{"op": "set_model_metadata", "name": name, "description": "d"}],
                "warnings": [],
            })

        # No Apply/Accept/Run widget exists on the read-only preview and the Apply
        # button is never the default/auto-focused control.
        for forbidden in ("accept_button", "run_button", "commit_button", "approve_all_button"):
            if hasattr(apply_dock, forbidden):
                raise RuntimeError(f"An unexpected {forbidden} exists on the dock.")
        if apply_dock.apply_button.isDefault() or apply_dock.apply_button.autoDefault():
            raise RuntimeError("Apply must never be the default button.")

        # Ask/Plan create no pending action.
        plan_start = apply_dock.run_loop.start("q", AgentMode.PLAN, AgentScope.CURRENT_MODEL)
        plan_event = apply_dock.run_loop.submit_provider_response(
            plan_start.request.request_token,
            _agent_turn("proposal", "Here.", kind="model_patch",
                        proposal_json=_rename_patch(_model_token(apply_dock), "Plan only")),
        )
        apply_dock._handle_run_event(plan_event)
        if apply_dock._pending_action is not None:
            raise RuntimeError("A Plan-mode proposal created a pending action.")

        # Act model proposal creates a pending action; nothing changes before Apply.
        name_before = adapter.current_graph().name
        _feed_act(apply_dock, AgentScope.CURRENT_MODEL, "model_patch",
                  _rename_patch(_model_token(apply_dock), "Applied by agent"))
        if apply_dock._pending_action is None:
            raise RuntimeError("An Act model proposal did not create a pending action.")
        if not apply_dock.apply_button.isEnabled():
            raise RuntimeError("The approval card Apply button was not enabled.")
        if adapter.current_graph().name != name_before:
            raise RuntimeError("A pending model action mutated the graph before Apply.")

        # Explicit Apply mutates atomically and enables Undo.
        model_pre = Model3Serializer.export_to_json(adapter.current_graph())
        apply_dock._on_apply_clicked()
        if adapter.current_graph().name != "Applied by agent":
            raise RuntimeError("Apply did not change the live model.")
        if apply_dock._pending_action is not None or apply_dock.apply_button.isEnabled():
            raise RuntimeError("The pending action was not cleared after Apply.")
        if apply_dock._last_applied is None or not apply_dock.undo_button.isEnabled():
            raise RuntimeError("Undo was not enabled after a successful model apply.")
        # A second click does nothing (one-shot).
        apply_dock._on_apply_clicked()
        if adapter.current_graph().name != "Applied by agent":
            raise RuntimeError("A repeated Apply click mutated the model again.")

        # Undo restores the exact prior model.
        apply_dock._on_undo_clicked()
        if Model3Serializer.export_to_json(adapter.current_graph()) != model_pre:
            raise RuntimeError("Undo did not restore the prior model.")

        # Atomic rollback: an injected install failure leaves the model unchanged.
        adapter.fail = True
        name_pre_fail = adapter.current_graph().name
        _feed_act(apply_dock, AgentScope.CURRENT_MODEL, "model_patch",
                  _rename_patch(_model_token(apply_dock), "Should not stick"))
        apply_dock._on_apply_clicked()
        if adapter.current_graph().name != name_pre_fail:
            raise RuntimeError("A failed model apply changed the live model.")
        adapter.fail = False

        # A mode/scope change fails a pending action closed.
        _feed_act(apply_dock, AgentScope.CURRENT_MODEL, "model_patch",
                  _rename_patch(_model_token(apply_dock), "Superseded"))
        if apply_dock._pending_action is None:
            raise RuntimeError("Expected a pending action before the mode change.")
        apply_dock._on_mode_or_scope_changed()
        if apply_dock._pending_action is not None or apply_dock.apply_button.isEnabled():
            raise RuntimeError("A mode/scope change did not clear the pending action.")

        # Layer-style Act apply + Undo on the real vector layer.
        style_tok = apply_dock.controller.execute(
            AgentToolCall(call_id="p4_style", tool_name="layer.style",
                          arguments={"layer_id": style_layer.id()}),
            AgentMode.PLAN, AgentScope.PROJECT,
        ).data["context_token"]
        renderer_type_before = style_layer.renderer().type()
        single_symbol = _json.dumps({
            "schema_version": 1, "context_token": style_tok,
            "target_layer_id": style_layer.id(), "title": "Single symbol",
            "summary": "One symbol for the whole layer.",
            "renderer": {"family": "single_symbol", "field": "", "class_count": 1,
                         "palette": ["#3366CC"], "opacity": 1.0},
            "labels": {"enabled": False, "field": ""}, "warnings": [],
        })
        _feed_act(apply_dock, AgentScope.PROJECT, "layer_style", single_symbol)
        if apply_dock._pending_action is None:
            raise RuntimeError("An Act style proposal did not create a pending action.")
        apply_dock._on_apply_clicked()
        if style_layer.renderer().type() != "singleSymbol":
            raise RuntimeError("Apply did not install the single-symbol renderer.")
        if not apply_dock.undo_button.isEnabled():
            raise RuntimeError("Undo was not enabled after a style apply.")
        apply_dock._on_undo_clicked()
        if style_layer.renderer().type() != renderer_type_before:
            raise RuntimeError("Undo did not restore the prior renderer.")

        # The ledger records outcomes and leaks no raw values.
        ledger_text = "\n".join(str(e.to_dict()) for e in apply_dock.action_ledger.entries())
        for leaked in ("SENTINEL_CATEGORY_LABEL", "SENTINEL_LABEL_EXPRESSION", "#3366CC"):
            if leaked in ledger_text:
                raise RuntimeError(f"The action ledger leaked forbidden content: {leaked}")
        if not any(e.status == "applied" for e in apply_dock.action_ledger.entries()):
            raise RuntimeError("The ledger did not record an applied action.")

        # Shutdown clears all pending/applied/ledger state.
        apply_dock.shutdown()
        if (apply_dock._pending_action is not None or apply_dock._last_applied is not None
                or apply_dock.action_ledger.entries()):
            raise RuntimeError("shutdown did not clear Phase 04 action state.")

        # -- Phase 05: approved safe Processing / current-model execution ----
        run_graph = Model3Serializer.import_from_json(Model3Serializer.export_to_json(graph))
        run_adapter = _SmokeModelAdapter(run_graph)
        run_dock = AgentWorkspaceDock(fake_iface, run_adapter.current_graph, model_apply=run_adapter)

        def _alg_token(algorithm_id, scope=AgentScope.PROJECT):
            described = run_dock.controller.execute(
                AgentToolCall(call_id="p5_describe", tool_name="processing.describe",
                              arguments={"algorithm_id": algorithm_id}),
                AgentMode.PLAN, scope,
            )
            if described.status != AgentResultStatus.SUCCESS:
                raise RuntimeError(f"processing.describe failed for {algorithm_id}.")
            token = described.data.get("context_token")
            if not token:
                raise RuntimeError("processing.describe issued no run freshness receipt.")
            return token

        def _run_json(token, algorithm_id, inputs, title="Agent run"):
            return _json.dumps({
                "schema_version": 1, "context_token": token, "algorithm_id": algorithm_id,
                "title": title, "summary": "Run a reviewed algorithm on a project layer.",
                "inputs": inputs, "warnings": [],
            })

        def _model_run_json(token):
            return _json.dumps({
                "schema_version": 1, "context_token": token, "title": "Run the workflow",
                "summary": "Run the current workflow and add its outputs.", "warnings": [],
            })

        # A processing_run proposal is inert until an explicit Run click.
        layers_before = set(project.mapLayers())
        buffer_run = _run_json(
            _alg_token("native:buffer"), "native:buffer",
            {"INPUT": {"layer": input_layer.id()}, "DISTANCE": {"distance": 5}},
        )
        _feed_act(run_dock, AgentScope.PROJECT, "processing_run", buffer_run)
        if run_dock._pending_action is None:
            raise RuntimeError("An Act processing_run proposal created no pending action.")
        if run_dock.apply_button.text() != "Run":
            raise RuntimeError("The run approval card did not offer a Run action.")
        if run_dock.apply_button.isDefault() or run_dock.apply_button.autoDefault():
            raise RuntimeError("Run must never be the default button.")
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("A pending run added a layer before approval.")

        # The explicit click executes exactly one run and adds one temp layer.
        run_dock._on_apply_clicked()
        added = set(project.mapLayers()) - layers_before
        if len(added) != 1:
            raise RuntimeError(f"A buffer run added {len(added)} layers instead of one.")
        buffer_result_id = next(iter(added))
        if not run_dock.undo_button.isEnabled():
            raise RuntimeError("Undo was not offered after a successful run.")
        # A repeated click is a no-op: the one-shot nonce was already consumed.
        run_dock._on_apply_clicked()
        if set(project.mapLayers()) - layers_before != added:
            raise RuntimeError("A repeated Run click executed the run again.")
        run_dock._on_undo_clicked()
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("Undo did not remove exactly the buffer result layer.")
        del buffer_result_id

        # A second reviewed algorithm runs the same way.
        centroids_run = _run_json(
            _alg_token("native:centroids"), "native:centroids",
            {"INPUT": {"layer": input_layer.id()}},
        )
        _feed_act(run_dock, AgentScope.PROJECT, "processing_run", centroids_run)
        run_dock._on_apply_clicked()
        centroid_added = set(project.mapLayers()) - layers_before
        if len(centroid_added) != 1:
            raise RuntimeError("A centroids run did not add exactly one temporary layer.")
        # A user-modified result blocks the destructive Undo.
        centroid_layer = project.mapLayer(next(iter(centroid_added)))
        centroid_layer.setName("Kept by the user")
        if run_dock._apply_coordinator.can_undo(run_dock._last_applied):
            raise RuntimeError("Undo stayed available after the user renamed the result.")
        for layer_id in centroid_added:
            project.removeMapLayer(layer_id)
        run_dock._last_applied = None

        # A Plan-mode run proposal previews only: no pending action, no Run.
        plan_run_start = run_dock.run_loop.start("preview", AgentMode.PLAN, AgentScope.PROJECT)
        plan_run_event = run_dock.run_loop.submit_provider_response(
            plan_run_start.request.request_token,
            _agent_turn("proposal", "Here.", kind="processing_run",
                        proposal_json=_run_json(
                            _alg_token("native:buffer"), "native:buffer",
                            {"INPUT": {"layer": input_layer.id()}})),
        )
        run_dock._handle_run_event(plan_run_event)
        if plan_run_event.kind != RunEventKind.PROPOSAL:
            raise RuntimeError("A valid Plan-mode run proposal did not validate.")
        if run_dock._pending_action is not None or run_dock.apply_button.isEnabled():
            raise RuntimeError("A Plan-mode run proposal created an approvable action.")
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("A Plan-mode run proposal executed something.")

        # Cancelling during the run adds no layer and revives nothing: the
        # coordinator emits its first progress signal before Processing starts,
        # so cancelling there proves a late result is discarded.
        if run_dock.run_coordinator.is_running():
            raise RuntimeError("The coordinator reported a run before one started.")
        run_dock.run_coordinator.cancel()  # terminal + idempotent with no run
        cancel_events: list = []
        run_dock.run_coordinator.run_canceled.connect(lambda: cancel_events.append("canceled"))
        run_dock.run_coordinator.run_progress.connect(
            lambda _p, _t: run_dock.run_coordinator.cancel()
        )
        _feed_act(run_dock, AgentScope.PROJECT, "processing_run",
                  _run_json(_alg_token("native:buffer"), "native:buffer",
                            {"INPUT": {"layer": input_layer.id()},
                             "DISTANCE": {"distance": 7}}))
        run_dock._on_apply_clicked()
        if not cancel_events:
            raise RuntimeError("Cancelling during a run did not report a cancellation.")
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("A cancelled run added a layer to the project.")
        if run_dock._last_applied is not None:
            raise RuntimeError("A cancelled run offered an Undo target.")
        if run_dock.run_coordinator.is_running():
            raise RuntimeError("A cancelled run stayed in the running state.")
        run_dock.run_coordinator.run_progress.disconnect()
        run_dock.run_coordinator.run_canceled.disconnect()

        # One running action maximum.
        occupied = run_dock.run_coordinator._state.start("busy", "processing_run", "Busy")
        refusal = run_dock.run_coordinator.start_processing_run(
            "second", "Second", "Buffer", "native:buffer", {}, ("OUTPUT",)
        )
        if refusal != "proposal_run_in_progress":
            raise RuntimeError("A second run was not refused while one was running.")
        run_dock.run_coordinator._state.finish(occupied, "finished")

        # A non-allowlisted algorithm is refused even with a valid receipt.
        blocked_id = "native:pixelstopoints"
        if AlgorithmCatalog.algorithm_exists(blocked_id):
            blocked_run = _run_json(
                _alg_token(blocked_id), blocked_id, {"INPUT_RASTER": {"layer": input_layer.id()}}
            )
            blocked_event = _feed_act(
                run_dock, AgentScope.PROJECT, "processing_run", blocked_run
            )
            if blocked_event.kind == RunEventKind.PROPOSAL:
                raise RuntimeError("A non-allowlisted algorithm reached a validated proposal.")
            if run_dock._pending_action is not None:
                raise RuntimeError("A non-allowlisted algorithm created a pending action.")

        # A destination binding is refused, and a path never parses at all.
        dest_run = _run_json(
            _alg_token("native:buffer"), "native:buffer",
            {"INPUT": {"layer": input_layer.id()}, "OUTPUT": {"string": "result"}},
        )
        if _feed_act(run_dock, AgentScope.PROJECT, "processing_run",
                     dest_run).kind == RunEventKind.PROPOSAL:
            raise RuntimeError("A proposal supplying an output destination was validated.")
        path_run = _run_json(
            _alg_token("native:buffer"), "native:buffer",
            {"INPUT": {"layer": input_layer.id()}, "OUTPUT": {"string": "C:/tmp/out.gpkg"}},
        )
        if _feed_act(run_dock, AgentScope.PROJECT, "processing_run",
                     path_run).kind == RunEventKind.PROPOSAL:
            raise RuntimeError("A proposal supplying an output path was validated.")
        # A stale receipt (rotated session secret) is refused.
        stale_token = _alg_token("native:buffer")
        run_dock.token_service.rotate()
        stale_run = _run_json(
            stale_token, "native:buffer", {"INPUT": {"layer": input_layer.id()}}
        )
        if _feed_act(run_dock, AgentScope.PROJECT, "processing_run",
                     stale_run).kind == RunEventKind.PROPOSAL:
            raise RuntimeError("A stale run receipt was accepted.")
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("A refused run proposal changed the project's layers.")

        # Real raster run: native:cellstatistics into a temporary raster.
        raster_run_tmp = _tempfile.NamedTemporaryFile(suffix=".tif", delete=False)
        raster_run_tmp.close()
        raster_run_id = None
        try:
            from osgeo import gdal as _run_gdal
            from osgeo import osr as _run_osr
            from qgis.core import QgsRasterLayer as _QgsRasterLayer

            run_driver = _run_gdal.GetDriverByName("GTiff")
            run_dataset = run_driver.Create(raster_run_tmp.name, 4, 4, 1, _run_gdal.GDT_Byte)
            run_dataset.SetGeoTransform((0.0, 1.0, 0.0, 4.0, 0.0, -1.0))
            run_srs = _run_osr.SpatialReference()
            run_srs.ImportFromEPSG(3857)
            run_dataset.SetProjection(run_srs.ExportToWkt())
            run_dataset.GetRasterBand(1).Fill(3)
            run_dataset = None
            run_raster = _QgsRasterLayer(raster_run_tmp.name, "run_raster", "gdal")
            if not run_raster.isValid():
                raise RuntimeError("The raster run fixture was not valid.")
            project.addMapLayer(run_raster)
            raster_run_id = run_raster.id()
            raster_before = set(project.mapLayers())
            cellstats_run = _run_json(
                _alg_token("native:cellstatistics"), "native:cellstatistics",
                {
                    "INPUT": {"layers": [raster_run_id]},
                    "REFERENCE_LAYER": {"layer": raster_run_id},
                    "STATISTIC": {"enum_string": "Mean"},
                },
                title="Cell statistics",
            )
            _feed_act(run_dock, AgentScope.PROJECT, "processing_run", cellstats_run)
            if run_dock._pending_action is None:
                raise RuntimeError("A raster run proposal created no pending action.")
            run_dock._on_apply_clicked()
            raster_added = set(project.mapLayers()) - raster_before
            if len(raster_added) != 1:
                raise RuntimeError("A cellstatistics run did not add one temporary raster.")
            if not run_dock.undo_button.isEnabled():
                raise RuntimeError("Undo was not offered after the raster run.")
            run_dock._on_undo_clicked()
            if set(project.mapLayers()) != raster_before:
                raise RuntimeError("Undo did not remove the raster result layer.")
            if raster_run_tmp.name in run_dock.transcript.toPlainText():
                raise RuntimeError("The run surface leaked the raster source path.")
        finally:
            if raster_run_id is not None:
                project.removeMapLayer(raster_run_id)
            os.unlink(raster_run_tmp.name)

        # model_run: the current 2-node workflow, approved and undone.
        model_before = set(project.mapLayers())
        model_token = run_dock.controller.execute(
            AgentToolCall(call_id="p5_md", tool_name="model.describe"),
            AgentMode.PLAN, AgentScope.CURRENT_MODEL,
        ).data["context_token"]
        _feed_act(run_dock, AgentScope.CURRENT_MODEL, "model_run", _model_run_json(model_token))
        if run_dock._pending_action is None:
            raise RuntimeError("An Act model_run proposal created no pending action.")
        if set(project.mapLayers()) != model_before:
            raise RuntimeError("A pending model_run added a layer before approval.")
        run_dock._on_apply_clicked()
        model_added = set(project.mapLayers()) - model_before
        if not model_added:
            raise RuntimeError("A model_run added no terminal output layer.")
        if not run_dock.undo_button.isEnabled():
            raise RuntimeError("Undo was not offered after a model_run.")
        run_dock._on_undo_clicked()
        if set(project.mapLayers()) != model_before:
            raise RuntimeError("Undo did not remove the model_run result layers.")

        # The ledger records the execution outcomes and leaks no raw values.
        run_ledger = [e.to_dict() for e in run_dock.action_ledger.entries()]
        run_statuses = {entry["status"] for entry in run_ledger}
        for expected in ("proposed", "approved", "running", "completed", "undone"):
            if expected not in run_statuses:
                raise RuntimeError(f"The ledger did not record a {expected!r} run entry.")
        run_ledger_text = "\n".join(str(entry) for entry in run_ledger)
        for leaked in (input_layer.id(), "TEMPORARY_OUTPUT", "EPSG:"):
            if leaked in run_ledger_text:
                raise RuntimeError(f"The run ledger leaked forbidden content: {leaked}")

        # Shutdown cancels and tears the run coordinator down.
        run_dock.shutdown()
        if (run_dock.run_coordinator.is_running() or run_dock._running_action is not None
                or run_dock._pending_action is not None or run_dock.action_ledger.entries()):
            raise RuntimeError("shutdown did not clear Phase 05 run state.")
        if set(project.mapLayers()) != layers_before:
            raise RuntimeError("Phase 05 execution left layers behind in the project.")

        proposal_dock.shutdown()
        project.removeMapLayer(style_layer.id())

        layer_ids_before_agent_chat = set(project.mapLayers())

        def _fake_generate_structured(responses, dock):
            def _handler(_profile, _api_key, _system_prompt, _user_prompt, _contract):
                dock.ai_client.succeeded.emit(responses.pop(0))

            return _handler

        chat_dock = AgentWorkspaceDock(fake_iface, lambda: None)
        if (
            not chat_dock.profile_label.text().startswith("Profile:")
            or chat_dock.ai_settings_button is None
        ):
            raise RuntimeError("Agent Workspace profile label/settings controls did not construct.")

        first_turn_raw = _json.dumps(
            {
                "action": "tool_calls",
                "assistant_text": "Checking your project.",
                "tool_calls": [
                    {"call_id": "smoke1", "tool_name": "project.summary", "arguments_json": "{}"}
                ],
                "proposal_kind": "none",
                "proposal_json": "",
            }
        )
        final_turn_raw = _json.dumps(
            {
                "action": "final",
                "assistant_text": "Your project has been inspected.",
                "tool_calls": [],
                "proposal_kind": "none",
                "proposal_json": "",
            }
        )
        chat_dock._active_profile = AiProfile.create("openai_compatible", "Smoke chat profile")
        chat_dock._active_api_key = "smoke-key"
        chat_dock.ai_client.generate_structured = _fake_generate_structured(
            [first_turn_raw, final_turn_raw], chat_dock
        )
        start_event = chat_dock.run_loop.start(
            "What is my project called?", AgentMode.ASK, AgentScope.PROJECT
        )
        chat_dock._handle_run_event(start_event)

        if chat_dock.run_loop.is_active():
            raise RuntimeError("The two-turn Agent Chat run did not reach a terminal state.")
        if chat_dock.run_loop.mode != AgentMode.ASK or chat_dock.run_loop.scope != AgentScope.PROJECT:
            raise RuntimeError("Agent Chat did not keep the captured mode/scope for the run.")
        transcript_text = chat_dock.transcript.toPlainText()
        if "Your project has been inspected." not in transcript_text:
            raise RuntimeError("Agent Chat did not render its final answer.")
        if "[tool: project.summary] success" not in transcript_text:
            raise RuntimeError("Agent Chat did not execute the real project.summary tool.")
        if "arguments_json" in transcript_text or "smoke-key" in transcript_text:
            raise RuntimeError("Agent Chat transcript leaked raw provider/argument/secret text.")
        if set(project.mapLayers()) != layer_ids_before_agent_chat:
            raise RuntimeError("Agent Chat mutated the project's layers.")
        final_status = chat_dock.status_label.text()
        if "tool call" not in final_status or "turn" not in final_status.lower():
            raise RuntimeError("Agent Chat did not render turn/tool-call usage in its status.")
        if chat_dock._active_api_key != "" or chat_dock._active_profile is not None:
            raise RuntimeError("Agent Chat did not clear its transient key/profile after finishing.")

        cancel_dock = AgentWorkspaceDock(fake_iface, lambda: None)
        cancel_dock._active_profile = AiProfile.create("openai_compatible", "Smoke cancel profile")
        cancel_dock._active_api_key = "smoke-key"
        cancel_sent: list = []
        cancel_dock.ai_client.generate_structured = (
            lambda *_args, **_kwargs: cancel_sent.append(True)
        )
        cancel_start_event = cancel_dock.run_loop.start("hello", AgentMode.ASK, AgentScope.PROJECT)
        cancel_dock._handle_run_event(cancel_start_event)
        if not cancel_dock.run_loop.is_active() or not cancel_sent:
            raise RuntimeError("Agent Chat did not start a run for the cancel probe.")
        cancel_dock._on_stop_clicked()
        if cancel_dock.run_loop.is_active():
            raise RuntimeError("Stop did not cancel the active Agent Chat run.")
        if cancel_dock.stop_button.isEnabled() or not cancel_dock.send_button.isEnabled():
            raise RuntimeError("Stop did not restore the Agent Workspace dock controls.")
        if cancel_dock._active_api_key != "" or cancel_dock._active_profile is not None:
            raise RuntimeError("Stop did not clear the dock's transient API key/profile.")
        cancel_dock.ai_client.succeeded.emit(final_turn_raw)
        if cancel_dock.run_loop.is_active() or cancel_dock.run_loop.turns_used > 1:
            raise RuntimeError("A late provider callback after cancel revived the run.")

        shutdown_dock = AgentWorkspaceDock(fake_iface, lambda: None)
        shutdown_dock._active_profile = AiProfile.create("openai_compatible", "Smoke shutdown profile")
        shutdown_dock._active_api_key = "smoke-key"
        shutdown_dock.ai_client.generate_structured = lambda *_args, **_kwargs: None
        shutdown_start_event = shutdown_dock.run_loop.start(
            "hello", AgentMode.ASK, AgentScope.PROJECT
        )
        shutdown_dock._handle_run_event(shutdown_start_event)
        if not shutdown_dock.run_loop.is_active():
            raise RuntimeError("Agent Chat did not start a run for the shutdown probe.")
        shutdown_dock.shutdown()
        if shutdown_dock.run_loop.is_active():
            raise RuntimeError("shutdown() did not cancel the active Agent Chat run.")
        if shutdown_dock._active_api_key != "" or shutdown_dock._active_profile is not None:
            raise RuntimeError("shutdown() did not clear the dock's transient API key/profile.")
        shutdown_dock.ai_client.succeeded.emit(final_turn_raw)
        if shutdown_dock.run_loop.is_active() or shutdown_dock.run_loop.turns_used > 1:
            raise RuntimeError("A late provider callback after shutdown revived the run.")

        # Offline Send must not start network activity.
        offline_store = AiSettingsStore()
        offline_profile = AiProfile.create("offline", "Smoke offline profile")
        offline_store.save_profile(offline_profile)
        offline_store.set_active(offline_profile.profile_id)
        offline_dock = AgentWorkspaceDock(fake_iface, lambda: None)
        offline_network_calls: list = []
        offline_dock.ai_client.generate_structured = (
            lambda *_args, **_kwargs: offline_network_calls.append(True)
        )
        offline_dock.prompt_input.setPlainText("hello")
        offline_dock._on_send_clicked()
        if offline_network_calls or offline_dock.run_loop.is_active():
            raise RuntimeError("Offline Agent Chat started network activity or a run.")

        workflow_context = AiMcpBridge.workflow_context(graph)
        if '"id":"source"' not in workflow_context or '"id":"buffer"' not in workflow_context:
            raise RuntimeError("The current workflow was not serialized for iterative AI.")

        suitability_id = "planx_suitability_lab:data_harmonizer"
        if AlgorithmCatalog.algorithm_exists(suitability_id):
            harmonizer = AlgorithmCatalog.create_node(suitability_id, "harmonizer")
            input_rasters = harmonizer.inputs.get("INPUT_RASTERS")
            if (
                input_rasters is None
                or not input_rasters.required
                or not input_rasters.allows_multiple
            ):
                raise RuntimeError(
                    "The suitability raster collection was not modeled as required."
                )
            parameter_dialog = NodeParameterDialog(harmonizer)
            wrapper = parameter_dialog.native_wrappers.get("INPUT_RASTERS")
            if wrapper is None or wrapper.wrappedWidget() is None:
                raise RuntimeError(
                    "The native QGIS multiple-raster parameter widget was not created."
                )
            parameter_dialog.close()

        model_path = Path(__file__).with_name("_smoke.model3")
        try:
            ok, error = Model3Serializer.export_to_model3(graph, str(model_path))
            if not ok:
                raise RuntimeError(error)
            imported, error = Model3Serializer.import_from_model3(str(model_path))
            if imported is None or len(imported.nodes) != 2:
                details = "none" if imported is None else ", ".join(
                    f"{node.node_id}:{node.algorithm_id}" for node in imported.nodes.values()
                )
                raise RuntimeError(
                    error or f"QGIS model round-trip nodes were [{details}]."
                )
        finally:
            model_path.unlink(missing_ok=True)
            for layer_id in set(project.mapLayers()) - original_layer_ids:
                project.removeMapLayer(layer_id)
        return f"QGIS {Qgis.QGIS_VERSION}: {len(records)} algorithms; smoke test passed"


class SmartModelerSmokeAlgorithm(QgsProcessingAlgorithm):
    """Makes the smoke suite runnable through QGIS' qgis_process executable."""

    def name(self) -> str:
        return "smartmodeler_smoke"

    def displayName(self) -> str:
        return "SmartModeler smoke test"

    def group(self) -> str:
        return "Tests"

    def groupId(self) -> str:
        return "tests"

    def createInstance(self):
        return SmartModelerSmokeAlgorithm()

    def initAlgorithm(self, _configuration=None) -> None:
        self.addOutput(QgsProcessingOutputString("RESULT", "Smoke test result"))

    def processAlgorithm(self, _parameters, _context, _feedback):
        return {"RESULT": run_checks()}


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QgsApplication([], False)
    application.initQgis()
    # After initQgis the prefix is known; the bundled Processing framework
    # lives under <prefix>/python/plugins and is not on sys.path by default
    # when this script is launched directly through python-qgis(-ltr).bat.
    plugins_path = os.path.join(QgsApplication.prefixPath(), "python", "plugins")
    if plugins_path not in sys.path:
        sys.path.append(plugins_path)
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
        print(run_checks())
        return 0
    finally:
        application.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
