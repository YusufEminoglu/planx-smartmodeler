"""Headless acceptance test for provider aliases and active-layer filtering."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from qgis.core import QgsApplication, QgsFeature, QgsProject, QgsVectorLayer


def _tool_turn() -> str:
    """Real provider variants reported by the plugin owner."""
    return json.dumps(
        {
            "action": "tool_calls",
            "assistant_text": "Inspecting the active layer and filter algorithm.",
            "tool_calls": [
                {
                    "call_id": "list_active#1",
                    "kind": "function",
                    "function": "layer.list",
                    "parameters": {},
                },
                {
                    "tool": "processing.resolve",
                    "input": {
                        "algorithm_id": "native:extractbyattribute",
                    },
                },
            ],
            "proposal_kind": "none",
            "proposal_json": "",
        }
    )


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    source_root = Path(__file__).resolve().parents[1]
    plugins_root = str(source_root.parent)
    if plugins_root not in sys.path:
        sys.path.insert(0, plugins_root)

    application = QgsApplication([], False)
    application.initQgis()
    processing_plugins = os.path.join(
        QgsApplication.prefixPath(), "python", "plugins"
    )
    if processing_plugins not in sys.path:
        sys.path.append(processing_plugins)
    try:
        from processing.core.Processing import Processing

        Processing.initialize()

        from planx_smartmodeler.core.agent.context_tokens import ContextTokenService
        from planx_smartmodeler.core.agent.contracts import AgentMode, AgentScope
        from planx_smartmodeler.core.agent.controller import AgentController
        from planx_smartmodeler.core.agent.run_coordinator import RunCoordinator
        from planx_smartmodeler.core.agent.run_loop import AgentRunLoop, RunEventKind
        from planx_smartmodeler.core.agent.runtime_proposals import (
            RuntimeProposalValidator,
        )
        from planx_smartmodeler.core.agent.runtime_tools import build_default_registry
        from planx_smartmodeler.core.prompt_context import PromptContextLoader

        project = QgsProject.instance()
        before = set(project.mapLayers())
        source = QgsVectorLayer(
            "Point?crs=EPSG:4326&field=id:integer&"
            "field=built_intensity_bin:string(20)",
            "Audit - DOLDURULACAK",
            "memory",
        )
        features = []
        for feature_id, category in ((1, "low"), (2, "high"), (3, "low")):
            feature = QgsFeature(source.fields())
            feature.setAttributes([feature_id, category])
            features.append(feature)
        source.dataProvider().addFeatures(features)
        project.addMapLayer(source)

        try:
            tokens = ContextTokenService()
            registry = build_default_registry(
                lambda: None,
                tokens,
                active_layer_provider=lambda: source,
                power_enabled_provider=lambda: True,
            )
            controller = AgentController(registry)
            validator = RuntimeProposalValidator(
                lambda: None,
                tokens,
                active_layer_provider=lambda: source,
            )
            loop = AgentRunLoop(
                controller,
                "Use the advertised QGIS tools and return one validated proposal.",
                proposal_validator=validator.validate,
                instruction_provider=lambda text, scope, power: (
                    PromptContextLoader(
                        context_dir=source_root / "agent_context"
                    ).agent_context(text, scope, power_enabled=power)
                ),
                power_enabled_provider=lambda: True,
            )
            loop.session_memory.append(
                '"built_intensity_bin" sütun değeri low olanları filtreleyip '
                "yeni bir katman olarak üret",
                "Activate the target layer and tell me when it is ready.",
            )
            request = loop.start(
                "hazır",
                AgentMode.ACT,
                AgentScope.PROJECT,
            )
            advertised = {
                item["name"] for item in json.loads(request.request.user_prompt)["tools"]
            }
            if not {"layer.list", "layer.describe", "processing.resolve"} <= advertised:
                raise RuntimeError(
                    "The Turkish filter continuation did not preserve its Processing pack."
                )
            if advertised & {
                "database.list",
                "database.describe",
                "script.list",
                "script.describe",
            }:
                raise RuntimeError(
                    "A normal filter advertised unrelated Power discovery tools."
                )

            inspected = loop.submit_provider_response(
                request.request.request_token, _tool_turn()
            )
            if inspected.kind != RunEventKind.REQUEST_PROVIDER:
                raise RuntimeError(
                    "Provider alias tool calls did not continue the Agent run."
                )
            payload = json.loads(inspected.request.user_prompt)
            resolve_result = next(
                event["result"]
                for event in payload["current_turn_events"]
                if event.get("kind") == "tool_result"
                and event.get("tool_name") == "processing.resolve"
            )
            if resolve_result.get("status") != "success":
                raise RuntimeError("Extract by attribute could not be resolved.")
            resolved = resolve_result["data"].get("resolved")
            if not isinstance(resolved, dict) or not resolved.get("context_token"):
                raise RuntimeError("Resolved algorithm omitted its proposal receipt.")
            context_token = resolved["context_token"]

            repeated = loop.submit_provider_response(
                inspected.request.request_token,
                json.dumps(
                    {
                        "action": "tool_calls",
                        "assistant_text": "Checking the active layer again.",
                        "tool_calls": [
                            {
                                "function": "layer.list",
                                "parameters": {},
                            }
                        ],
                        "proposal_kind": "none",
                        "proposal_json": "",
                    }
                ),
            )
            if repeated.kind != RunEventKind.REQUEST_PROVIDER:
                raise RuntimeError("A repeated successful inspection ended the run.")
            reused_events = [
                event
                for event in repeated.tool_events
                if event.get("kind") == "tool_result"
            ]
            if (
                loop.tool_calls_used != 2
                or len(reused_events) != 1
                or not reused_events[0].get("reused")
            ):
                raise RuntimeError(
                    "A repeated successful inspection consumed another tool call."
                )

            proposal_json = json.dumps(
                {
                    "schema_version": 1,
                    "context_token": context_token,
                    "algorithm_id": "native:extractbyattribute",
                    "title": "Filter low built intensity",
                    "summary": "Create a temporary layer containing low values.",
                    "inputs": {
                        "INPUT": {"layer": source.id()},
                        "FIELD": {
                            "field": "built_intensity_bin",
                            "layer_param": "INPUT",
                        },
                        "OPERATOR": {"enum": 0},
                        "VALUE": {"string": "low"},
                    },
                    "warnings": [],
                }
            )
            proposal_turn = json.dumps(
                {
                    "action": "tool_calls",
                    "assistant_text": "The active-layer filter is ready to review.",
                    "tool_calls": [
                        {
                            "function": "layer.list",
                            "parameters": {},
                        }
                    ],
                    "proposal_kind": "processing",
                    "proposal_json": proposal_json,
                }
            )
            proposal_event = loop.submit_provider_response(
                repeated.request.request_token, proposal_turn
            )
            if proposal_event.kind != RunEventKind.PROPOSAL:
                raise RuntimeError(
                    "The active-layer filter proposal was rejected: "
                    f"{proposal_event.reason_code} {proposal_event.text}"
                )
            ingredients = validator.take_last_validated()
            if not ingredients:
                raise RuntimeError("The validated filter retained no run ingredients.")

            finished = []
            failed = []
            coordinator = RunCoordinator(lambda: None)
            coordinator.run_finished.connect(finished.append)
            coordinator.run_failed.connect(
                lambda reason, message: failed.append((reason, message))
            )
            refusal = coordinator.start_processing_run(
                "filter_acceptance",
                "Filter low built intensity",
                ingredients["display_name"],
                ingredients["algorithm_id"],
                ingredients["run_parameters"],
                ingredients["destinations"],
            )
            if refusal or failed or len(finished) != 1:
                raise RuntimeError(
                    f"Filter run failed: refusal={refusal!r}, failures={failed!r}"
                )
            added = set(project.mapLayers()) - before - {source.id()}
            if len(added) != 1:
                raise RuntimeError(f"Expected one filtered layer, got {len(added)}.")
            result = project.mapLayer(next(iter(added)))
            values = [
                feature["built_intensity_bin"] for feature in result.getFeatures()
            ]
            if values != ["low", "low"]:
                raise RuntimeError(f"Unexpected filtered values: {values!r}")
            if source.featureCount() != 3:
                raise RuntimeError("The source active layer was modified.")

            # The exact owner-reported request now has a deterministic local
            # proposal path. It must not spend a provider turn or depend on a
            # provider emitting a correctly shaped final proposal.
            result_id = result.id()
            project.removeMapLayer(result_id)
            local_loop = AgentRunLoop(
                controller,
                "Use the advertised QGIS tools and return one validated proposal.",
                proposal_validator=validator.validate,
                instruction_provider=lambda text, scope, power: (
                    PromptContextLoader(
                        context_dir=source_root / "agent_context"
                    ).agent_context(text, scope, power_enabled=power)
                ),
                power_enabled_provider=lambda: False,
            )
            local_event = local_loop.start(
                "Audit - DOLDURULACAK bu katmanda built_intensitiy_bin isimli "
                'sütun değeri "low" lan geometrileri filtreleyip yeni bir '
                "katman olarak kaydet",
                AgentMode.ACT,
                AgentScope.PROJECT,
            )
            if (
                local_event.kind != RunEventKind.PROPOSAL
                or local_event.request is not None
                or local_loop.turns_used != 1
                or local_loop.tool_calls_used != 3
            ):
                raise RuntimeError(
                    "The deterministic active-layer filter did not produce "
                    "a one-turn local proposal."
                )
            proposal_text = json.dumps(
                local_event.proposal or {}, ensure_ascii=False
            )
            if (
                "built_intensitiy_bin" not in proposal_text
                or "built_intensity_bin" not in proposal_text
            ):
                raise RuntimeError(
                    "The unique one-edit field correction was not disclosed "
                    "in the validated proposal."
                )
            local_ingredients = validator.take_last_validated()
            if not local_ingredients:
                raise RuntimeError(
                    "The deterministic filter retained no run ingredients."
                )
            local_finished = []
            local_failed = []
            local_coordinator = RunCoordinator(lambda: None)
            local_coordinator.run_finished.connect(local_finished.append)
            local_coordinator.run_failed.connect(
                lambda reason, message: local_failed.append((reason, message))
            )
            local_refusal = local_coordinator.start_processing_run(
                "local_filter_acceptance",
                "Filter low built intensity locally",
                local_ingredients["display_name"],
                local_ingredients["algorithm_id"],
                local_ingredients["run_parameters"],
                local_ingredients["destinations"],
            )
            if local_refusal or local_failed or len(local_finished) != 1:
                raise RuntimeError(
                    "Deterministic filter run failed: "
                    f"refusal={local_refusal!r}, failures={local_failed!r}"
                )
            local_added = set(project.mapLayers()) - before - {source.id()}
            if len(local_added) != 1:
                raise RuntimeError(
                    f"Expected one local filtered layer, got {len(local_added)}."
                )
            local_result = project.mapLayer(next(iter(local_added)))
            local_values = [
                feature["built_intensity_bin"]
                for feature in local_result.getFeatures()
            ]
            if local_values != ["low", "low"]:
                raise RuntimeError(
                    f"Unexpected local filtered values: {local_values!r}"
                )
            if source.featureCount() != 3:
                raise RuntimeError(
                    "The deterministic filter modified the source active layer."
                )
            print(
                "AGENT FILTER SMOKE PASS: invalid provider call ids and aliases "
                "were normalized; the deterministic local path produced two "
                "low-value features without a provider turn."
            )
            return 0
        finally:
            for layer_id in set(project.mapLayers()) - before:
                project.removeMapLayer(layer_id)
    finally:
        QgsProject.instance().clear()
        application.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
