"""Execute thirty small operations through both SmartModeler entry points.

This is an offline acceptance matrix.  It never calls an AI provider: the
Agent side uses the same reviewed ``processing.describe`` and proposal
validation boundary that a provider response must pass, then executes the
validated proposal.  The Modeler side builds a real ``GraphModel`` node and
runs it through ``GraphExecutionEngine``.  Each case uses only in-memory
layers and temporary Processing outputs.
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingContext,
    QgsProcessingFeedback,
    QgsProject,
    QgsVectorLayer,
)


@dataclass(frozen=True)
class Case:
    title: str
    algorithm_id: str
    bindings: tuple[tuple[str, str, Any], ...]


def _layer(tag: str, layers: dict[str, QgsVectorLayer]) -> QgsVectorLayer:
    return layers[tag]


def _cases() -> tuple[Case, ...]:
    # The third item is either a scalar or a symbolic layer name.  Field
    # bindings carry the source parameter as their fourth value.
    return (
        Case("Buffer points", "native:buffer", (("layer", "INPUT", "points"), ("distance", "DISTANCE", 5.0), ("number", "SEGMENTS", 5), ("bool", "DISSOLVE", False))),
        Case("Buffer lines", "native:buffer", (("layer", "INPUT", "lines"), ("distance", "DISTANCE", 2.0), ("number", "SEGMENTS", 3), ("bool", "DISSOLVE", False))),
        Case("Dissolved buffer", "native:buffer", (("layer", "INPUT", "points"), ("distance", "DISTANCE", 3.0), ("number", "SEGMENTS", 4), ("bool", "DISSOLVE", True))),
        Case("Centroids", "native:centroids", (("layer", "INPUT", "polygons"), ("bool", "ALL_PARTS", False))),
        Case("All-part centroids", "native:centroids", (("layer", "INPUT", "polygons"), ("bool", "ALL_PARTS", True))),
        Case("Convex hull", "native:convexhull", (("layer", "INPUT", "points"),)),
        Case("Bounding boxes", "native:boundingboxes", (("layer", "INPUT", "points"),)),
        Case("Fix polygon geometries", "native:fixgeometries", (("layer", "INPUT", "polygons"), ("enum", "METHOD", 0))),
        Case("Split multipart features", "native:multiparttosingleparts", (("layer", "INPUT", "polygons"),)),
        Case("Extract category even", "native:extractbyattribute", (("layer", "INPUT", "points"), ("field", "FIELD", "category", "INPUT"), ("enum", "OPERATOR", 0), ("string", "VALUE", "even"))),
        Case("Extract category odd", "native:extractbyattribute", (("layer", "INPUT", "points"), ("field", "FIELD", "category", "INPUT"), ("enum", "OPERATOR", 0), ("string", "VALUE", "odd"))),
        Case("Extract points by location", "native:extractbylocation", (("layer", "INPUT", "points"), ("enum", "PREDICATE", 0), ("layer", "INTERSECT", "district"))),
        Case("Extract Konak reference features", "smartmodeler:extractbyreferenceattribute", (("layer", "INPUT", "points"), ("layer", "REFERENCE", "district"), ("field", "REFERENCE_FIELD", "district", "REFERENCE"), ("string", "REFERENCE_VALUE", "KONAK"), ("enum", "PREDICATE", 0))),
        Case("Clip points", "native:clip", (("layer", "INPUT", "points"), ("layer", "OVERLAY", "district"))),
        Case("Clip lines", "native:clip", (("layer", "INPUT", "lines"), ("layer", "OVERLAY", "district"))),
        Case("Difference polygons", "native:difference", (("layer", "INPUT", "polygons"), ("layer", "OVERLAY", "cutout"))),
        Case("Intersect polygons", "native:intersection", (("layer", "INPUT", "polygons"), ("layer", "OVERLAY", "cutout"))),
        Case("Union polygons", "native:union", (("layer", "INPUT", "polygons"), ("layer", "OVERLAY", "cutout"))),
        Case("Reproject points", "native:reprojectlayer", (("layer", "INPUT", "points"), ("crs", "TARGET_CRS", "EPSG:3857"))),
        Case("Count points in polygons", "native:countpointsinpolygon", (("layer", "POLYGONS", "district"), ("layer", "POINTS", "points"), ("string", "FIELD", "point_count"))),
        Case("Calculate doubled id", "native:fieldcalculator", (("layer", "INPUT", "points"), ("string", "FIELD_NAME", "double_id"), ("enum", "FIELD_TYPE", 1), ("number", "FIELD_LENGTH", 10), ("number", "FIELD_PRECISION", 0), ("expression", "FORMULA", "\"id\" * 2"))),
        Case("Join point attributes", "native:joinattributestable", (("layer", "INPUT", "points"), ("field", "FIELD", "id", "INPUT"), ("layer", "INPUT_2", "table"), ("field", "FIELD_2", "id", "INPUT_2"), ("enum", "METHOD", 1), ("bool", "DISCARD_NONMATCHING", False))),
        Case("Merge point layers", "native:mergevectorlayers", (("layers", "LAYERS", ("points", "points_2")), ("crs", "CRS", "EPSG:4326"), ("bool", "ADD_SOURCE_FIELDS", True))),
        Case("Random three points", "native:randomextract", (("layer", "INPUT", "points"), ("enum", "METHOD", 0), ("number", "NUMBER", 3))),
        Case("Random five points", "native:randomextract", (("layer", "INPUT", "points"), ("enum", "METHOD", 0), ("number", "NUMBER", 5))),
        Case("Reference-layer Konak touch", "smartmodeler:extractbyreferenceattribute", (("layer", "INPUT", "lines"), ("layer", "REFERENCE", "district"), ("field", "REFERENCE_FIELD", "district", "REFERENCE"), ("string", "REFERENCE_VALUE", "KONAK"), ("enum", "PREDICATE", 0))),
        Case("Polygon boundary", "native:boundary", (("layer", "INPUT", "polygons"),)),
        Case("Polygon to lines", "native:polygonstolines", (("layer", "INPUT", "polygons"),)),
        Case("Lines to polygons", "qgis:linestopolygons", (("layer", "INPUT", "lines"),)),
        Case("Point on surface", "native:pointonsurface", (("layer", "INPUT", "polygons"),)),
    )


def _make_layers(project: QgsProject) -> dict[str, QgsVectorLayer]:
    points = QgsVectorLayer(
        "Point?crs=EPSG:4326&field=id:integer&field=category:string(20)",
        "matrix_points", "memory"
    )
    point_features = []
    for index in range(1, 7):
        feature = QgsFeature(points.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(index * 0.01, index * 0.01)))
        feature.setAttributes([index, "even" if index % 2 == 0 else "odd"])
        point_features.append(feature)
    points.dataProvider().addFeatures(point_features)
    points.updateExtents()

    points_2 = QgsVectorLayer(
        "Point?crs=EPSG:4326&field=id:integer&field=category:string(20)",
        "matrix_points_2", "memory"
    )
    feature = QgsFeature(points_2.fields())
    feature.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(0.08, 0.01)))
    feature.setAttributes([8, "even"])
    points_2.dataProvider().addFeature(feature)
    points_2.updateExtents()

    lines = QgsVectorLayer("LineString?crs=EPSG:4326", "matrix_lines", "memory")
    for x in (0.01, 0.04, 0.07):
        feature = QgsFeature(lines.fields())
        feature.setGeometry(QgsGeometry.fromPolylineXY([QgsPointXY(x, 0.0), QgsPointXY(x, 0.06)]))
        lines.dataProvider().addFeature(feature)
    lines.updateExtents()

    polygon_schema = "Polygon?crs=EPSG:4326&field=district:string(20)&field=zone:string(20)"
    district = QgsVectorLayer(polygon_schema, "matrix_district", "memory")
    feature = QgsFeature(district.fields())
    feature.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(-0.01, -0.01), QgsPointXY(0.055, -0.01), QgsPointXY(0.055, 0.055), QgsPointXY(-0.01, 0.055), QgsPointXY(-0.01, -0.01)]]))
    feature.setAttributes(["KONAK", "urban"])
    district.dataProvider().addFeature(feature)
    district.updateExtents()

    polygons = QgsVectorLayer(polygon_schema, "matrix_polygons", "memory")
    for index, x in enumerate((0.0, 0.06), 1):
        feature = QgsFeature(polygons.fields())
        feature.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(x, 0.0), QgsPointXY(x + 0.04, 0.0), QgsPointXY(x + 0.04, 0.04), QgsPointXY(x, 0.04), QgsPointXY(x, 0.0)]]))
        feature.setAttributes(["KONAK", f"zone_{index}"])
        polygons.dataProvider().addFeature(feature)
    polygons.updateExtents()

    cutout = QgsVectorLayer("Polygon?crs=EPSG:4326", "matrix_cutout", "memory")
    feature = QgsFeature(cutout.fields())
    feature.setGeometry(QgsGeometry.fromPolygonXY([[QgsPointXY(0.015, 0.015), QgsPointXY(0.03, 0.015), QgsPointXY(0.03, 0.03), QgsPointXY(0.015, 0.03), QgsPointXY(0.015, 0.015)]]))
    cutout.dataProvider().addFeature(feature)
    cutout.updateExtents()

    table = QgsVectorLayer("None?field=id:integer&field=label:string(20)", "matrix_table", "memory")
    for index in range(1, 7):
        feature = QgsFeature(table.fields())
        feature.setAttributes([index, f"label_{index}"])
        table.dataProvider().addFeature(feature)

    layers = {"points": points, "points_2": points_2, "lines": lines, "district": district, "polygons": polygons, "cutout": cutout, "table": table}
    for item in layers.values():
        project.addMapLayer(item)
    return layers


def _materialize(value: Any, layers: dict[str, QgsVectorLayer]) -> Any:
    if isinstance(value, str) and value in layers:
        return layers[value].id()
    if isinstance(value, tuple):
        return [_materialize(item, layers) for item in value]
    return value


def _graph_parameters(case: Case, layers: dict[str, QgsVectorLayer]) -> dict[str, Any]:
    return {
        name: _materialize(value, layers)
        for binding in case.bindings
        for kind, name, value, *rest in (binding,)
        if kind not in ("field",)
    } | {
        name: value
        for kind, name, value, *_rest in case.bindings
        if kind == "field"
    }


def _agent_bindings(case: Case, layers: dict[str, QgsVectorLayer]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for binding in case.bindings:
        kind, name, value, *rest = binding
        if kind == "layer":
            result[name] = {"layer": layers[value].id()}
        elif kind == "layers":
            result[name] = {"layers": [layers[item].id() for item in value]}
        elif kind == "field":
            result[name] = {"field": value, "layer_param": rest[0]}
        else:
            result[name] = {kind: value}
    return result


def _run_modeler(case: Case, layers: dict[str, QgsVectorLayer]) -> None:
    from planx_smartmodeler.core.algorithm_catalog import AlgorithmCatalog
    from planx_smartmodeler.core.execution_engine import GraphExecutionEngine
    from planx_smartmodeler.core.graph_model import GraphModel

    graph = GraphModel(f"30-case Modeler: {case.title}")
    node = AlgorithmCatalog.create_node(case.algorithm_id, "operation")
    node.parameters.update(_graph_parameters(case, layers))
    graph.add_node(node)
    report = GraphExecutionEngine().execute(graph)
    if not report.succeeded or not report.added_layer_ids:
        raise RuntimeError(f"Modeler failed: {case.title}: {report.message}")


def _run_agent(case: Case, layers: dict[str, QgsVectorLayer]) -> None:
    import json
    from planx_smartmodeler.core.agent.context_tokens import ContextTokenService
    from planx_smartmodeler.core.agent.contracts import AgentMode, AgentScope, AgentToolCall, AgentResultStatus
    from planx_smartmodeler.core.agent.controller import AgentController
    from planx_smartmodeler.core.agent.run_coordinator import RunCoordinator
    from planx_smartmodeler.core.agent.runtime_proposals import RuntimeProposalValidator
    from planx_smartmodeler.core.agent.runtime_tools import build_default_registry
    from planx_smartmodeler.core.agent.proposals import parse_proposal, PROPOSAL_KIND_PROCESSING_RUN

    tokens = ContextTokenService()
    controller = AgentController(build_default_registry(lambda: None, tokens))
    described = controller.execute(
        AgentToolCall(call_id="describe", tool_name="processing.describe", arguments={"algorithm_id": case.algorithm_id}),
        AgentMode.PLAN, AgentScope.PROJECT,
    )
    if described.status != AgentResultStatus.SUCCESS or not described.data.get("agent_runnable"):
        raise RuntimeError(f"Agent did not admit {case.algorithm_id}: {described.data}")
    proposal = parse_proposal(PROPOSAL_KIND_PROCESSING_RUN, json.dumps({
        "schema_version": 1,
        "context_token": described.data["context_token"],
        "algorithm_id": case.algorithm_id,
        "title": case.title,
        "summary": f"Run the small operation: {case.title}.",
        "inputs": _agent_bindings(case, layers),
        "warnings": [],
    }))
    validator = RuntimeProposalValidator(lambda: None, tokens)
    validation = validator.validate(PROPOSAL_KIND_PROCESSING_RUN, proposal, AgentMode.ACT, AgentScope.PROJECT)
    if not validation.ok:
        raise RuntimeError(f"Agent proposal rejected for {case.title}: {validation.reason_code}")
    ingredients = validator.take_last_validated()
    if not ingredients:
        raise RuntimeError(f"Agent retained no run ingredients for {case.title}")

    finished: list[Any] = []
    failed: list[Any] = []
    coordinator = RunCoordinator(lambda: None)
    coordinator.run_finished.connect(finished.append)
    coordinator.run_failed.connect(lambda reason, message: failed.append((reason, message)))
    refusal = coordinator.start_processing_run(
        f"matrix_{case.title}", case.title, ingredients["display_name"],
        ingredients["algorithm_id"], ingredients["run_parameters"], ingredients["destinations"],
    )
    deadline = time.time() + 15.0
    while not finished and not failed and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    if refusal or failed or len(finished) != 1:
        raise RuntimeError(f"Agent execution failed: {case.title}: refusal={refusal!r}, failures={failed!r}")


def run_matrix() -> str:
    source_root = Path(__file__).resolve().parents[1]
    plugins_root = str(source_root.parent)
    if plugins_root not in sys.path:
        sys.path.insert(0, plugins_root)
    processing_plugins = os.path.join(
        QgsApplication.prefixPath(), "python", "plugins"
    )
    if processing_plugins not in sys.path:
        sys.path.append(processing_plugins)
    from processing.core.Processing import Processing

    Processing.initialize()
    registry = QgsApplication.processingRegistry()
    if registry.providerById("smartmodeler") is None:
        from planx_smartmodeler.processing.provider import SmartModelerProcessingProvider

        if not registry.addProvider(SmartModelerProcessingProvider()):
            raise RuntimeError("SmartModeler Processing provider could not be registered.")
    project = QgsProject.instance()
    cases = _cases()
    if len(cases) != 30:
        raise RuntimeError(f"The acceptance matrix must contain 30 cases, got {len(cases)}.")
    layers = _make_layers(project)
    try:
        modeler_passed = 0
        agent_passed = 0
        for case in cases:
            _run_modeler(case, layers)
            modeler_passed += 1
            for layer_id in list(project.mapLayers()):
                if layer_id not in {layer.id() for layer in layers.values()}:
                    project.removeMapLayer(layer_id)
        for case in cases:
            _run_agent(case, layers)
            agent_passed += 1
            for layer_id in list(project.mapLayers()):
                if layer_id not in {layer.id() for layer in layers.values()}:
                    project.removeMapLayer(layer_id)
        return f"30 OPERATION MATRIX PASS: Modeler {modeler_passed}/30; Agent Workflow {agent_passed}/30; offline, temporary in-memory outputs only."
    finally:
        for layer in list(layers.values()):
            project.removeMapLayer(layer.id())


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QgsApplication([], False)
    application.initQgis()
    try:
        print(run_matrix())
        return 0
    finally:
        QgsProject.instance().clear()
        application.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
