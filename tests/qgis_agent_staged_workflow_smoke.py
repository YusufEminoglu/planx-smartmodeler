"""Offline acceptance test for the staged OSM -> area -> analysis workflow.

This is the deterministic twin of ``qgis_deepseek_hard_live``. It drives the
same four reviewed Processing stages through the same validator and coordinator,
but binds every parameter itself instead of asking a provider. That separates
the two things the live test conflates: whether the *plugin* can carry a staged
workflow correctly, and whether the *model* chooses the right bindings.

It exists because a live run once failed three times blaming the Field
Calculator when the OSM stage had silently returned zero features: the geometry
scope was bound to Lines, every later stage succeeded on an empty layer, and the
receipt for each one was clean. Nothing offline covered that path.

No network: the sibling downloader's cache is seeded with the exact query the
reviewed bindings produce, so a miss would be a real defect rather than a
silent Overpass call.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)

AREA_FIELD = "area_m2"


def _extent_layer() -> QgsVectorLayer:
    layer = QgsVectorLayer("Polygon?crs=EPSG:4326", "Konak district extent", "memory")
    feature = QgsFeature(layer.fields())
    feature.setGeometry(
        QgsGeometry.fromPolygonXY(
            [[
                QgsPointXY(27.05, 38.38),
                QgsPointXY(27.15, 38.38),
                QgsPointXY(27.15, 38.46),
                QgsPointXY(27.05, 38.46),
                QgsPointXY(27.05, 38.38),
            ]]
        )
    )
    layer.dataProvider().addFeature(feature)
    layer.updateExtents()
    QgsProject.instance().addMapLayer(layer)
    return layer


def _seed_osm_response(layer: QgsVectorLayer) -> None:
    from zero2agent_osm_downloader.core.query import TagSpec, build_query
    from zero2agent_osm_downloader.processing.osm_algorithms import _CACHE

    bbox = layer.extent()
    query = build_query(
        (
            TagSpec("boundary", "administrative", "polygon"),
            TagSpec("admin_level", "10", "polygon"),
        ),
        (bbox.yMinimum(), bbox.xMinimum(), bbox.yMaximum(), bbox.xMaximum()),
        "all",
    )
    elements = []
    for osm_id, name, lon, lat in (
        (901, "Alsancak", 27.08, 38.42),
        (902, "Goztepe", 27.09, 38.38),
        (903, "Basmane", 27.11, 38.43),
    ):
        elements.append(
            {
                "type": "way",
                "id": osm_id,
                "tags": {
                    "boundary": "administrative",
                    "admin_level": "10",
                    "name": name,
                },
                "geometry": [
                    {"lat": lat, "lon": lon},
                    {"lat": lat, "lon": lon + 0.02},
                    {"lat": lat + 0.01, "lon": lon + 0.02},
                    {"lat": lat + 0.01, "lon": lon},
                    {"lat": lat, "lon": lon},
                ],
            }
        )
    _CACHE[query] = (time.monotonic(), {"elements": elements})


def _activate_sibling_plugin(plugins_root: str):
    import qgis.utils as qgis_utils

    if plugins_root not in qgis_utils.plugin_paths:
        qgis_utils.plugin_paths.append(plugins_root)
    qgis_utils.updateAvailablePlugins()
    from zero2agent_osm_downloader import classFactory

    plugin = classFactory(None)
    plugin.initGui()
    package = "zero2agent_osm_downloader"
    qgis_utils.plugins[package] = plugin
    if package not in qgis_utils.active_plugins:
        qgis_utils.active_plugins.append(package)
    return plugin, qgis_utils, package


def _run_stage(
    source: QgsVectorLayer,
    algorithm_id: str,
    inputs: dict,
    output_kind: str,
    min_features: int,
) -> QgsVectorLayer:
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
    from planx_smartmodeler.core.agent.runtime_proposals import RuntimeProposalValidator
    from planx_smartmodeler.core.agent.runtime_tools import build_default_registry

    tokens = ContextTokenService()
    controller = AgentController(
        build_default_registry(lambda: None, tokens, active_layer_provider=lambda: source)
    )
    described = controller.execute(
        AgentToolCall(
            call_id="describe",
            tool_name="processing.describe",
            arguments={"algorithm_id": algorithm_id},
        ),
        AgentMode.PLAN,
        AgentScope.ACTIVE_LAYER,
    )
    if described.status != AgentResultStatus.SUCCESS:
        raise RuntimeError(f"{algorithm_id} could not be described: {described.message}")

    proposal = parse_proposal(
        PROPOSAL_KIND_PROCESSING_RUN,
        json.dumps(
            {
                "schema_version": 1,
                "context_token": described.data["context_token"],
                "algorithm_id": algorithm_id,
                "title": f"Staged {algorithm_id}",
                "summary": "One reviewed stage of the offline staged workflow.",
                "inputs": inputs,
                "warnings": [],
            }
        ),
    )
    validator = RuntimeProposalValidator(
        lambda: None, tokens, active_layer_provider=lambda: source
    )
    validation = validator.validate(
        PROPOSAL_KIND_PROCESSING_RUN, proposal, AgentMode.ACT, AgentScope.ACTIVE_LAYER
    )
    if not validation.ok:
        raise RuntimeError(
            f"{algorithm_id} proposal rejected: {validation.reason_code} {validation.message}"
        )
    ingredients = validator.take_last_validated()

    project = QgsProject.instance()
    before = set(project.mapLayers())
    finished: list = []
    failed: list = []
    coordinator = RunCoordinator(lambda: None)
    coordinator.run_finished.connect(finished.append)
    coordinator.run_failed.connect(lambda reason, message: failed.append((reason, message)))
    refusal = coordinator.start_processing_run(
        f"staged_{algorithm_id}",
        f"Staged {algorithm_id}",
        ingredients["display_name"],
        ingredients["algorithm_id"],
        ingredients["run_parameters"],
        ingredients["destinations"],
    )
    deadline = time.time() + 60.0
    while not finished and not failed and time.time() < deadline:
        QCoreApplication.processEvents()
        time.sleep(0.01)
    if refusal or failed or len(finished) != 1:
        raise RuntimeError(
            f"{algorithm_id} stage failed: refusal={refusal!r}, failures={failed!r}"
        )

    candidates = []
    for layer_id in set(project.mapLayers()) - before:
        layer = project.mapLayer(layer_id)
        if isinstance(layer, QgsVectorLayer):
            if output_kind == "polygon" and layer.geometryType() != 2:
                project.removeMapLayer(layer_id)
                continue
            candidates.append(layer)
        else:
            project.removeMapLayer(layer_id)
    if len(candidates) != 1:
        raise RuntimeError(
            f"{algorithm_id} produced {len(candidates)} {output_kind} outputs, expected one"
        )
    result = candidates[0]
    # The defect this test exists for: an empty result is not a Processing
    # failure, so only an explicit count catches it before the next stage.
    if result.featureCount() < min_features:
        raise RuntimeError(
            f"{algorithm_id} produced {result.featureCount()} features, "
            f"expected at least {min_features}"
        )
    return result


def run_workflow() -> str:
    extent = _extent_layer()
    _seed_osm_response(extent)

    neighborhoods = _run_stage(
        extent,
        "zero2agentosm:download_advanced",
        {
            # Bound by label exactly as the tool protocol asks. "Polygons" is
            # index 3; a miscounted 2 means Lines and returns nothing here.
            "MATCH_MODE": {"enum_string": "Match all tags (AND)"},
            "GEOMETRY": {"enum_string": "Polygons"},
            "KEY_1": {"osm_tag": "boundary"},
            "VALUE_1": {"osm_tag": "administrative"},
            "KEY_2": {"osm_tag": "admin_level"},
            "VALUE_2": {"osm_tag": "10"},
            "EXTENT": {"layer_extent": extent.id()},
        },
        "polygon",
        min_features=1,
    )
    if neighborhoods.featureCount() != 3:
        raise RuntimeError(
            f"Expected the three seeded neighborhoods, got {neighborhoods.featureCount()}"
        )

    projected = _run_stage(
        neighborhoods,
        "native:reprojectlayer",
        {
            "INPUT": {"layer": neighborhoods.id()},
            "TARGET_CRS": {"crs": "EPSG:3857"},
        },
        "polygon",
        min_features=3,
    )
    if projected.crs().authid() != "EPSG:3857":
        raise RuntimeError(f"Reprojection produced {projected.crs().authid()}")

    measured = _run_stage(
        projected,
        "native:fieldcalculator",
        {
            "INPUT": {"layer": projected.id()},
            "FIELD_NAME": {"string": AREA_FIELD},
            "FIELD_TYPE": {"enum_string": "Decimal (double)"},
            "FORMULA": {"expression": "$area"},
        },
        "polygon",
        min_features=3,
    )
    if measured.fields().indexOf(AREA_FIELD) < 0:
        raise RuntimeError(
            f"{AREA_FIELD} was not created; fields="
            f"{[field.name() for field in measured.fields()]!r}"
        )
    areas = []
    for feature in measured.getFeatures():
        value = feature[AREA_FIELD]
        if value is None or (hasattr(value, "isNull") and value.isNull()):
            raise RuntimeError(f"{AREA_FIELD} holds NULL for at least one feature")
        areas.append(float(value))
    if len(areas) != 3 or not all(area > 0 for area in areas):
        raise RuntimeError(f"Unexpected $area values: {areas!r}")

    analyzed = _run_stage(
        measured,
        "native:centroids",
        {"INPUT": {"layer": measured.id()}},
        "vector",
        min_features=3,
    )
    if analyzed.fields().indexOf(AREA_FIELD) < 0:
        raise RuntimeError("The analysis stage lost the calculated area field.")

    mean = sum(areas) / len(areas)
    return (
        f"AGENT STAGED WORKFLOW SMOKE PASS: 3 neighborhoods downloaded offline, "
        f"reprojected to EPSG:3857, {AREA_FIELD} populated "
        f"(mean {mean:.2f} m2), centroids carried the field."
    )


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QgsApplication([], False)
    application.initQgis()
    source_root = Path(__file__).resolve().parents[1]
    plugins_root = str(source_root.parent)
    if plugins_root not in sys.path:
        sys.path.insert(0, plugins_root)
    plugin_python = os.path.normpath(
        os.path.join(QgsApplication.prefixPath(), "python", "plugins")
    )
    if plugin_python not in sys.path:
        sys.path.insert(0, plugin_python)
    try:
        from processing.core.Processing import Processing
        from planx_smartmodeler.processing.provider import SmartModelerProcessingProvider
        from zero2agent_osm_downloader.processing.provider import AgentOsmProvider

        Processing.initialize()
        registry = QgsApplication.processingRegistry()
        sibling_plugin, qgis_utils, sibling_package = _activate_sibling_plugin(plugins_root)
        added = []
        for provider_type in (SmartModelerProcessingProvider, AgentOsmProvider):
            if registry.providerById(provider_type.PROVIDER_ID) is None:
                provider = provider_type()
                registry.addProvider(provider)
                added.append(provider)
        try:
            print(run_workflow(), flush=True)
            return 0
        finally:
            for provider in reversed(added):
                registry.removeProvider(provider)
            sibling_plugin.unload()
            qgis_utils.plugins.pop(sibling_package, None)
            if sibling_package in qgis_utils.active_plugins:
                qgis_utils.active_plugins.remove(sibling_package)
    except Exception as error:  # noqa: BLE001
        print(f"AGENT STAGED WORKFLOW SMOKE FAIL: {type(error).__name__}: {error}", flush=True)
        return 1
    finally:
        QgsProject.instance().clear()
        application.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
