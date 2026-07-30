"""Headless real-QGIS acceptance test for Agent Chat random extraction."""
from __future__ import annotations

import json
import os
import sys
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


class SmartModelerAgentRandomExtractSmoke(QgsProcessingAlgorithm):
    """Validate, approve, and execute the reviewed one-step Agent run."""

    def name(self) -> str:
        return "smartmodeler_agent_randomextract_smoke"

    def displayName(self) -> str:
        return "SmartModeler Agent random-extract smoke test"

    def group(self) -> str:
        return "Tests"

    def groupId(self) -> str:
        return "tests"

    def createInstance(self):
        return SmartModelerAgentRandomExtractSmoke()

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

        project = QgsProject.instance()
        before = set(project.mapLayers())
        source = QgsVectorLayer(
            "Point?crs=EPSG:32635",
            "Rastgele Seçilen 15 Durak",
            "memory",
        )
        features = []
        for index in range(15):
            feature = QgsFeature()
            feature.setGeometry(
                QgsGeometry.fromPointXY(QgsPointXY(500000 + index, 4250000 + index))
            )
            features.append(feature)
        source.dataProvider().addFeatures(features)
        source.updateExtents()
        project.addMapLayer(source)

        try:
            token_service = ContextTokenService()
            controller = AgentController(
                build_default_registry(lambda: None, token_service)
            )
            described = controller.execute(
                AgentToolCall(
                    call_id="randomextract_describe",
                    tool_name="processing.describe",
                    arguments={"algorithm_id": "native:randomextract"},
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            if (
                described.status != AgentResultStatus.SUCCESS
                or not described.data.get("agent_runnable")
            ):
                raise RuntimeError("native:randomextract is not agent-runnable.")

            searched = controller.execute(
                AgentToolCall(
                    call_id="randomextract_search",
                    tool_name="processing.search",
                    arguments={"query": "random extract"},
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            rows = searched.data.get("algorithms", [])
            if (
                searched.status != AgentResultStatus.SUCCESS
                or not rows
                or rows[0].get("algorithm_id") != "native:randomextract"
                or not rows[0].get("agent_runnable")
                or len(rows) > 8
            ):
                raise RuntimeError(
                    "Processing search did not prioritize a compact runnable result."
                )

            dynamic_safe = controller.execute(
                AgentToolCall(
                    call_id="boundary_describe",
                    tool_name="processing.describe",
                    arguments={"algorithm_id": "native:boundary"},
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            if not dynamic_safe.data.get("agent_runnable"):
                raise RuntimeError("A structurally safe native algorithm was not admitted.")
            opaque = controller.execute(
                AgentToolCall(
                    call_id="fieldcalc_describe",
                    tool_name="processing.describe",
                    arguments={"algorithm_id": "native:fieldcalculator"},
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            if not opaque.data.get("agent_runnable"):
                raise RuntimeError("The reviewed Field Calculator was not admitted.")
            formula = next(
                row
                for row in opaque.data.get("parameters", [])
                if row.get("name") == "FORMULA"
            )
            if formula.get("proposal_binding") != "expression":
                raise RuntimeError("Field Calculator did not expose its typed expression binding.")

            proposal = parse_proposal(
                PROPOSAL_KIND_PROCESSING_RUN,
                json.dumps(
                    {
                        "schema_version": 1,
                        "context_token": described.data["context_token"],
                        "algorithm_id": "native:randomextract",
                        "title": "Rastgele 3 durağı yeni katmana çıkar",
                        "summary": "Kaynak katmandan üç rastgele nokta üret.",
                        "inputs": {
                            "INPUT": {"layer": source.id()},
                            "METHOD": {"enum": 0},
                            "NUMBER": {"number": 3},
                        },
                        "warnings": [],
                    }
                ),
            )
            validator = RuntimeProposalValidator(
                lambda: None,
                token_service,
            )
            validation = validator.validate(
                PROPOSAL_KIND_PROCESSING_RUN,
                proposal,
                AgentMode.ACT,
                AgentScope.PROJECT,
            )
            if not validation.ok:
                raise RuntimeError(
                    f"Agent proposal validation failed: {validation.reason_code}"
                )
            ingredients = validator.take_last_validated()
            if not ingredients:
                raise RuntimeError("Validated Agent run retained no execution ingredients.")

            finished = []
            failed = []
            coordinator = RunCoordinator(lambda: None)
            coordinator.run_finished.connect(finished.append)
            coordinator.run_failed.connect(lambda reason, message: failed.append((reason, message)))
            refusal = coordinator.start_processing_run(
                "randomextract_acceptance",
                proposal.title,
                ingredients["display_name"],
                ingredients["algorithm_id"],
                ingredients["run_parameters"],
                ingredients["destinations"],
            )
            if refusal or failed or len(finished) != 1:
                raise RuntimeError(
                    f"Agent run failed: refusal={refusal!r}, failures={failed!r}"
                )

            added = set(project.mapLayers()) - before - {source.id()}
            if len(added) != 1:
                raise RuntimeError(f"Expected one result layer, got {len(added)}.")
            result = project.mapLayer(next(iter(added)))
            if not isinstance(result, QgsVectorLayer) or result.featureCount() != 3:
                raise RuntimeError("Result is not a three-feature vector layer.")
            if source.selectedFeatureCount() != 0:
                raise RuntimeError("The source layer selection was unexpectedly changed.")
            return {
                "RESULT": (
                    "Agent Chat validated and executed native:randomextract: "
                    "15 input points -> 3-point temporary layer; source selection unchanged."
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
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
        algorithm = SmartModelerAgentRandomExtractSmoke()
        algorithm.initAlgorithm()
        result = algorithm.processAlgorithm(
            {},
            QgsProcessingContext(),
            QgsProcessingFeedback(),
        )
        print(f"AGENT RANDOM EXTRACT SMOKE PASS: {result['RESULT']}")
        return 0
    finally:
        QgsProject.instance().clear()
        application.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
