"""Headless real-QGIS acceptance test for reviewed direct OSM acquisition."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProcessingOutputString,
    QgsProject,
    QgsVectorLayer,
)


class SmartModelerAgentOsmSmoke(QgsProcessingAlgorithm):
    """Validate and execute an Agent OSM run using a named layer's extent."""

    def name(self) -> str:
        return "smartmodeler_agent_osm_smoke"

    def displayName(self) -> str:
        return "SmartModeler Agent direct OSM smoke test"

    def group(self) -> str:
        return "Tests"

    def groupId(self) -> str:
        return "tests"

    def createInstance(self):
        return SmartModelerAgentOsmSmoke()

    def initAlgorithm(self, _configuration=None) -> None:
        self.addOutput(QgsProcessingOutputString("RESULT", "Smoke test result"))

    def processAlgorithm(self, _parameters, _context, _feedback):
        source_root = Path(os.environ["SMARTMODELER_SOURCE_ROOT"]).resolve()
        plugin_parent = str(source_root.parent)
        if plugin_parent not in sys.path:
            sys.path.insert(0, plugin_parent)

        from planx_smartmodeler.core.agent.context_tokens import ContextTokenService
        from planx_smartmodeler.core.agent.contracts import (
            AgentMode,
            AgentResultStatus,
            AgentScope,
            AgentToolCall,
        )
        from planx_smartmodeler.core.agent.controller import AgentController
        from planx_smartmodeler.core.agent.proposals import (
            PROPOSAL_KIND_PROCESSING_RUN,
            parse_proposal,
        )
        from planx_smartmodeler.core.agent.run_coordinator import RunCoordinator
        from planx_smartmodeler.core.agent.runtime_proposals import (
            RuntimeProposalValidator,
        )
        from planx_smartmodeler.core.agent.runtime_tools import build_default_registry
        from planx_smartmodeler.core.osm_query import build_overpass_query
        from planx_smartmodeler.processing.osm_download import _SESSION_CACHE

        project = QgsProject.instance()
        before = set(project.mapLayers())
        extent_layer = QgsVectorLayer(
            "LineString?crs=EPSG:4326",
            "Network Centrality - EDGES",
            "memory",
        )
        extent_feature = QgsFeature()
        extent_feature.setGeometry(
            QgsGeometry.fromPolylineXY(
                [
                    QgsPointXY(27.1200, 38.4100),
                    QgsPointXY(27.1260, 38.4160),
                ]
            )
        )
        extent_layer.dataProvider().addFeature(extent_feature)
        extent_layer.updateExtents()
        project.addMapLayer(extent_layer)

        try:
            token_service = ContextTokenService()
            controller = AgentController(
                build_default_registry(
                    lambda: None,
                    token_service,
                    active_layer_provider=lambda: extent_layer,
                )
            )
            listed = controller.execute(
                AgentToolCall(
                    call_id="active_layer_list",
                    tool_name="layer.list",
                    arguments={},
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            rows = listed.data.get("layers", [])
            if (
                listed.status != AgentResultStatus.SUCCESS
                or not rows
                or rows[0].get("layer_id") != extent_layer.id()
                or rows[0].get("active") is not True
            ):
                raise RuntimeError("layer.list did not identify the active layer first.")
            described = controller.execute(
                AgentToolCall(
                    call_id="osm_describe",
                    tool_name="processing.describe",
                    arguments={
                        "algorithm_id": "smartmodeler:osm_download_polygons"
                    },
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            if (
                described.status != AgentResultStatus.SUCCESS
                or not described.data.get("agent_runnable")
            ):
                raise RuntimeError("The direct OSM polygon algorithm is not runnable.")

            parameters = {
                row["name"]: row
                for row in described.data.get("parameters", [])
            }
            if not parameters["KEY"]["required"] or parameters["VALUE"]["required"]:
                raise RuntimeError("The OSM key/value required-input contract is wrong.")
            if parameters["EXTENT"].get("alternative_binding") != "layer_extent":
                raise RuntimeError("The extent parameter does not expose layer_extent.")

            curated = controller.execute(
                AgentToolCall(
                    call_id="curated_osm_describe",
                    tool_name="processing.describe",
                    arguments={
                        "algorithm_id": "zero2agentosm:download_preset"
                    },
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            if (
                curated.status != AgentResultStatus.SUCCESS
                or not curated.data.get("agent_runnable")
            ):
                raise RuntimeError(
                    "02Agent OSM preset is not available to SmartModeler."
                )
            curated_parameters = {
                row["name"]: row
                for row in curated.data.get("parameters", [])
            }
            if (
                curated_parameters["PRESET"]["proposal_binding"] != "enum"
                or curated_parameters["EXTENT"]["alternative_binding"]
                != "layer_extent"
            ):
                raise RuntimeError(
                    "02Agent OSM Agent bindings do not match the live signature."
                )
            urban_index = next(
                (
                    index
                    for index, label in enumerate(
                        curated_parameters["PRESET"]["enum_options"]
                    )
                    if "roads, buildings & trees" in str(label).casefold()
                ),
                None,
            )
            if urban_index is None:
                raise RuntimeError(
                    "02Agent OSM does not expose the combined urban-context preset."
                )
            urban_proposal = parse_proposal(
                PROPOSAL_KIND_PROCESSING_RUN,
                json.dumps(
                    {
                        "schema_version": 1,
                        "context_token": curated.data["context_token"],
                        "algorithm_id": "zero2agentosm:download_preset",
                        "title": "Download roads, buildings and trees",
                        "summary": (
                            "Create temporary point, line and polygon layers "
                            "from one bounded urban-context request."
                        ),
                        "inputs": {
                            "PRESET": {"enum": urban_index},
                            "EXTENT": {"layer_extent": extent_layer.id()},
                        },
                        "warnings": [],
                    }
                ),
            )
            urban_validator = RuntimeProposalValidator(lambda: None, token_service)
            urban_validation = urban_validator.validate(
                PROPOSAL_KIND_PROCESSING_RUN,
                urban_proposal,
                AgentMode.ACT,
                AgentScope.PROJECT,
            )
            if not urban_validation.ok:
                raise RuntimeError(
                    "Combined 02Agent OSM proposal validation failed: "
                    f"{urban_validation.reason_code}"
                )
            urban_ingredients = urban_validator.take_last_validated()
            if (
                not urban_ingredients
                or tuple(urban_ingredients.get("destinations", ()))
                != ("OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POLYGONS")
            ):
                raise RuntimeError(
                    "The urban-context proposal did not retain all geometry outputs."
                )

            advanced = controller.execute(
                AgentToolCall(
                    call_id="advanced_osm_describe",
                    tool_name="processing.describe",
                    arguments={
                        "algorithm_id": "zero2agentosm:download_advanced"
                    },
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            if (
                advanced.status != AgentResultStatus.SUCCESS
                or not advanced.data.get("agent_runnable")
            ):
                raise RuntimeError(
                    "02Agent OSM advanced endpoint is not runnable."
                )
            advanced_parameters = {
                row["name"]: row
                for row in advanced.data.get("parameters", [])
            }
            if (
                advanced_parameters["MATCH_MODE"]["proposal_binding"] != "enum"
                or advanced_parameters["KEY_1"]["proposal_binding"] != "osm_tag"
                or advanced_parameters["EXTENT"]["alternative_binding"]
                != "layer_extent"
            ):
                raise RuntimeError(
                    "02Agent OSM advanced bindings do not match the reviewed signature."
                )
            advanced_proposal = parse_proposal(
                PROPOSAL_KIND_PROCESSING_RUN,
                json.dumps(
                    {
                        "schema_version": 1,
                        "context_token": advanced.data["context_token"],
                        "algorithm_id": "zero2agentosm:download_advanced",
                        "title": "Download accessible schools",
                        "summary": (
                            "Create temporary polygons matching school and "
                            "wheelchair-access tags in one bounded request."
                        ),
                        "inputs": {
                            "MATCH_MODE": {"enum": 1},
                            "GEOMETRY": {"enum": 3},
                            "KEY_1": {"osm_tag": "amenity"},
                            "VALUE_1": {"osm_tag": "school"},
                            "KEY_2": {"osm_tag": "wheelchair"},
                            "VALUE_2": {"osm_tag": "yes"},
                            "EXTENT": {"layer_extent": extent_layer.id()},
                        },
                        "warnings": [],
                    }
                ),
            )
            advanced_validator = RuntimeProposalValidator(
                lambda: None, token_service
            )
            advanced_validation = advanced_validator.validate(
                PROPOSAL_KIND_PROCESSING_RUN,
                advanced_proposal,
                AgentMode.ACT,
                AgentScope.PROJECT,
            )
            if not advanced_validation.ok:
                raise RuntimeError(
                    "Advanced 02Agent OSM proposal validation failed: "
                    f"{advanced_validation.reason_code}"
                )
            advanced_ingredients = advanced_validator.take_last_validated()
            if (
                not advanced_ingredients
                or tuple(advanced_ingredients.get("destinations", ()))
                != ("OUTPUT_POINTS", "OUTPUT_LINES", "OUTPUT_POLYGONS")
            ):
                raise RuntimeError(
                    "The advanced OSM proposal did not retain temporary outputs."
                )

            planx_algorithm = QgsApplication.processingRegistry().algorithmById(
                "planx:networkcentrality"
            )
            if planx_algorithm is not None:
                planx_described = controller.execute(
                    AgentToolCall(
                        call_id="planx_describe",
                        tool_name="processing.describe",
                        arguments={"algorithm_id": "planx:networkcentrality"},
                    ),
                    AgentMode.PLAN,
                    AgentScope.PROJECT,
                )
                planx_parameters = {
                    row["name"]: row
                    for row in planx_described.data.get("parameters", [])
                }
                for name in ("RADIUS", "SAMPLES"):
                    row = planx_parameters[name]
                    if row["required"] or not row["has_default"]:
                        raise RuntimeError(f"{name} is not treated as a QGIS default.")

            proposal = parse_proposal(
                PROPOSAL_KIND_PROCESSING_RUN,
                json.dumps(
                    {
                        "schema_version": 1,
                        "context_token": described.data["context_token"],
                        "algorithm_id": "smartmodeler:osm_download_polygons",
                        "title": "Download buildings for the network extent",
                        "summary": "Create one temporary OSM building polygon layer.",
                        "inputs": {
                            "KEY": {"osm_tag": "building"},
                            "EXTENT": {"layer_extent": extent_layer.id()},
                        },
                        "warnings": [],
                    }
                ),
            )
            validator = RuntimeProposalValidator(lambda: None, token_service)
            validation = validator.validate(
                PROPOSAL_KIND_PROCESSING_RUN,
                proposal,
                AgentMode.ACT,
                AgentScope.PROJECT,
            )
            if not validation.ok:
                raise RuntimeError(
                    "Agent OSM proposal validation failed: "
                    f"{validation.reason_code}"
                )
            ingredients = validator.take_last_validated()
            if not ingredients or not ingredients.get("network_access"):
                raise RuntimeError("The OSM run lost its network-risk marker.")

            # qgis_process is already executing this smoke test as a Processing
            # algorithm. A second nested blocking network loop is not a faithful
            # desktop-Agent execution context, so seed the downloader's bounded
            # session cache and test the full proposal/materialization/result
            # path deterministically. A separate qgis_process invocation tests
            # the real network path.
            query = build_overpass_query(
                "building",
                "",
                "polygon",
                (38.4100, 27.1200, 38.4160, 27.1260),
            )
            _SESSION_CACHE[query] = (
                time.monotonic(),
                {
                    "elements": [
                        {
                            "type": "way",
                            "id": 123,
                            "tags": {"building": "yes", "name": "Smoke building"},
                            "geometry": [
                                {"lat": 38.4110, "lon": 27.1210},
                                {"lat": 38.4110, "lon": 27.1220},
                                {"lat": 38.4120, "lon": 27.1220},
                                {"lat": 38.4120, "lon": 27.1210},
                                {"lat": 38.4110, "lon": 27.1210},
                            ],
                        }
                    ]
                },
            )

            finished = []
            failed = []
            coordinator = RunCoordinator(lambda: None)
            coordinator.run_finished.connect(finished.append)
            coordinator.run_failed.connect(
                lambda reason, message: failed.append((reason, message))
            )
            refusal = coordinator.start_processing_run(
                "osm_acceptance",
                proposal.title,
                ingredients["display_name"],
                ingredients["algorithm_id"],
                ingredients["run_parameters"],
                ingredients["destinations"],
            )
            if refusal or failed or len(finished) != 1:
                raise RuntimeError(
                    f"Agent OSM run failed: refusal={refusal!r}, failures={failed!r}"
                )

            added = set(project.mapLayers()) - before - {extent_layer.id()}
            if len(added) != 1:
                raise RuntimeError(f"Expected one OSM result layer, got {len(added)}.")
            result = project.mapLayer(next(iter(added)))
            if not isinstance(result, QgsVectorLayer) or result.featureCount() <= 0:
                raise RuntimeError("The OSM result is not a populated vector layer.")
            return {
                "RESULT": (
                    "Agent validated layer_extent and downloaded "
                    f"{result.featureCount()} temporary OSM building polygons."
                )
            }
        finally:
            for layer_id in set(project.mapLayers()) - before:
                project.removeMapLayer(layer_id)


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    source_root = Path(__file__).resolve().parents[1]
    os.environ["SMARTMODELER_SOURCE_ROOT"] = str(source_root)
    plugins_root = str(source_root.parent)
    if plugins_root not in sys.path:
        sys.path.insert(0, plugins_root)

    application = QgsApplication([], False)
    application.initQgis()
    processing_plugins = os.path.join(
        QgsApplication.prefixPath(),
        "python",
        "plugins",
    )
    if processing_plugins not in sys.path:
        sys.path.append(processing_plugins)

    added_providers = []
    registry = QgsApplication.processingRegistry()
    try:
        from processing.core.Processing import Processing
        from planx_smartmodeler.processing.provider import (
            SmartModelerProcessingProvider,
        )
        from zero2agent_osm_downloader.processing.provider import AgentOsmProvider

        Processing.initialize()
        for provider_type in (
            SmartModelerProcessingProvider,
            AgentOsmProvider,
        ):
            if registry.providerById(provider_type.PROVIDER_ID) is None:
                provider = provider_type()
                if not registry.addProvider(provider):
                    raise RuntimeError(
                        f"Could not register smoke provider {provider_type.PROVIDER_ID}."
                    )
                added_providers.append(provider)

        algorithm = SmartModelerAgentOsmSmoke()
        algorithm.initAlgorithm()
        result = algorithm.processAlgorithm(
            {},
            QgsProcessingContext(),
            QgsProcessingFeedback(),
        )
        print(f"AGENT OSM SMOKE PASS: {result['RESULT']}")
        return 0
    finally:
        for provider in reversed(added_providers):
            registry.removeProvider(provider)
        QgsProject.instance().clear()
        application.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
