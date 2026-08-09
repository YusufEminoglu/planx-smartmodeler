"""Headless real-QGIS acceptance test for reviewed Agent expressions."""
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
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsVectorLayer,
)


def _square(x: float, y: float, size: float = 10.0) -> QgsGeometry:
    ring = [
        QgsPointXY(x, y),
        QgsPointXY(x + size, y),
        QgsPointXY(x + size, y + size),
        QgsPointXY(x, y + size),
        QgsPointXY(x, y),
    ]
    return QgsGeometry.fromPolygonXY([ring])


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
        from planx_smartmodeler.core.agent.qgis_expression_policy import (
            validate_qgis_expression,
        )
        from planx_smartmodeler.core.agent.run_coordinator import RunCoordinator
        from planx_smartmodeler.core.agent.runtime_proposals import (
            RuntimeProposalValidator,
        )
        from planx_smartmodeler.core.agent.runtime_tools import build_default_registry

        project = QgsProject.instance()
        before = set(project.mapLayers())
        source = QgsVectorLayer(
            "Polygon?crs=EPSG:5253&field=id:integer",
            "Reprojected buildings",
            "memory",
        )
        features = []
        for index in range(6):
            feature = QgsFeature(source.fields())
            feature.setAttribute("id", index + 1)
            feature.setGeometry(_square(500000 + index * 20, 4250000))
            features.append(feature)
        source.dataProvider().addFeatures(features)
        source.updateExtents()
        project.addMapLayer(source)

        try:
            for formula in ("rand(1, 15)", "$area", "round($area, 2)"):
                check = validate_qgis_expression(formula, source)
                if not check.ok:
                    raise RuntimeError(f"Valid QGIS expression was rejected: {formula}")
            for formula in (
                "rand(1,",
                "env('PATH')",
                'file_exists("id")',
                "'$area'",
            ):
                check = validate_qgis_expression(formula, source)
                if check.ok:
                    raise RuntimeError(f"Unsafe/invalid expression was admitted: {formula}")
            unknown = validate_qgis_expression('"missing_field" + 1', source)
            if unknown.ok:
                raise RuntimeError("An unknown field reference was admitted.")

            tokens = ContextTokenService()
            controller = AgentController(
                build_default_registry(lambda: None, tokens)
            )
            described = controller.execute(
                AgentToolCall(
                    call_id="fieldcalc_describe",
                    tool_name="processing.describe",
                    arguments={"algorithm_id": "native:fieldcalculator"},
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            if (
                described.status != AgentResultStatus.SUCCESS
                or not described.data.get("agent_runnable")
            ):
                raise RuntimeError("native:fieldcalculator is not agent-runnable.")
            function_help = controller.execute(
                AgentToolCall(
                    call_id="rand_help",
                    tool_name="expression.search",
                    arguments={"query": "rand", "limit": 5},
                ),
                AgentMode.PLAN,
                AgentScope.PROJECT,
            )
            functions = function_help.data.get("functions", [])
            if (
                function_help.status != AgentResultStatus.SUCCESS
                or not functions
                or functions[0].get("name") != "rand"
                or "random integer" not in functions[0].get("help", "").casefold()
            ):
                raise RuntimeError("Live QGIS rand() help was not available to Agent.")
            rows = {
                row["name"]: row for row in described.data.get("parameters", [])
            }
            if rows["FORMULA"].get("proposal_binding") != "expression":
                raise RuntimeError("FORMULA is not advertised as an expression binding.")
            integer_index = next(
                (
                    index
                    for index, label in enumerate(rows["FIELD_TYPE"]["enum_options"])
                    if "integer" in str(label).casefold()
                ),
                None,
            )
            if integer_index is None:
                raise RuntimeError("Field Calculator exposes no integer field type.")

            proposal = parse_proposal(
                PROPOSAL_KIND_PROCESSING_RUN,
                json.dumps(
                    {
                        "schema_version": 1,
                        "context_token": described.data["context_token"],
                        "algorithm_id": "native:fieldcalculator",
                        "title": "Add random floor count",
                        "summary": "Create floors with QGIS rand(1, 15).",
                        "inputs": {
                            "INPUT": {"layer": source.id()},
                            "FIELD_NAME": {"string": "floors"},
                            "FIELD_TYPE": {"enum": integer_index},
                            "FORMULA": {"expression": "rand(1, 15)"},
                        },
                        "warnings": [],
                    }
                ),
            )
            validator = RuntimeProposalValidator(lambda: None, tokens)
            validation = validator.validate(
                PROPOSAL_KIND_PROCESSING_RUN,
                proposal,
                AgentMode.ACT,
                AgentScope.PROJECT,
            )
            if not validation.ok:
                raise RuntimeError(
                    f"Field Calculator proposal failed: {validation.reason_code} "
                    f"{validation.message}"
                )
            ingredients = validator.take_last_validated()
            if not ingredients:
                raise RuntimeError("Validated expression retained no run ingredients.")

            finished = []
            failed = []
            coordinator = RunCoordinator(lambda: None)
            coordinator.run_finished.connect(finished.append)
            coordinator.run_failed.connect(
                lambda reason, message: failed.append((reason, message))
            )
            refusal = coordinator.start_processing_run(
                "expression_acceptance",
                proposal.title,
                ingredients["display_name"],
                ingredients["algorithm_id"],
                ingredients["run_parameters"],
                ingredients["destinations"],
            )
            if refusal or failed or len(finished) != 1:
                raise RuntimeError(
                    f"Expression run failed: refusal={refusal!r}, failures={failed!r}"
                )

            added = set(project.mapLayers()) - before - {source.id()}
            if len(added) != 1:
                raise RuntimeError(f"Expected one result layer, got {len(added)}.")
            result = project.mapLayer(next(iter(added)))
            if not isinstance(result, QgsVectorLayer):
                raise RuntimeError("Field Calculator result is not a vector layer.")
            floors_index = result.fields().indexOf("floors")
            if floors_index < 0:
                raise RuntimeError("The floors field was not created.")
            values = [feature["floors"] for feature in result.getFeatures()]
            if len(values) != 6 or not all(
                isinstance(value, int) and 1 <= value <= 15 for value in values
            ):
                raise RuntimeError(f"Unexpected rand(1,15) values: {values!r}")
            if source.fields().indexOf("floors") >= 0:
                raise RuntimeError("The source layer was modified.")
            print(
                "AGENT EXPRESSION SMOKE PASS: QGIS parsed rand/$area, rejected "
                "unsafe formulas, and Field Calculator created six integer values."
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
