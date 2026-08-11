"""Offline chained-workflow matrix for reviewed Agent Processing runs.

The staged workflow smoke covers one four-stage chain. This matrix covers the
harder shapes that chain exposes only in combination: two-layer overlays, a
field created in one stage and consumed several stages later, geometry-type
transitions, and a multi-layer input under ACTIVE_LAYER scope.

Every scenario runs through the real validator and RunCoordinator with no
provider and no network, so a failure here is a plugin defect rather than a
model mistake. Each stage asserts what the *next* stage depends on -- feature
count, geometry type, CRS and field survival -- because the failure mode this
suite exists for is a run that succeeds while quietly producing nothing usable.
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Dict, Optional, Tuple

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)

POINT, LINE, POLYGON = 0, 1, 2
# The seeded district is in Izmir, so UTM zone 35N is the local metric CRS.
METRIC_CRS = "EPSG:32635"


@dataclass
class Stage:
    """One reviewed run plus the invariants the rest of the chain relies on."""

    name: str
    algorithm_id: str
    # Built from the layers produced so far, so a stage can bind a secondary
    # input that is deliberately not the active layer.
    inputs: Callable[[Dict[str, QgsVectorLayer]], Dict[str, Any]]
    # Which named layer must be active; the planner forces the reviewed primary
    # input to it under ACTIVE_LAYER scope.
    active: str
    produces: str
    geometry: Optional[int] = None
    min_features: int = 1
    exact_features: Optional[int] = None
    required_fields: Tuple[str, ...] = ()
    populated_fields: Tuple[str, ...] = ()
    crs: str = ""


@dataclass
class Scenario:
    name: str
    stages: Tuple[Stage, ...] = dataclass_field(default_factory=tuple)


def _polygon(lon: float, lat: float, width: float = 0.02, height: float = 0.01) -> QgsGeometry:
    return QgsGeometry.fromPolygonXY(
        [[
            QgsPointXY(lon, lat),
            QgsPointXY(lon + width, lat),
            QgsPointXY(lon + width, lat + height),
            QgsPointXY(lon, lat + height),
            QgsPointXY(lon, lat),
        ]]
    )


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


def _seed_osm_response(extent: QgsVectorLayer) -> None:
    from zero2agent_osm_downloader.core.query import TagSpec, build_query
    from zero2agent_osm_downloader.processing.osm_algorithms import _CACHE

    bbox = extent.extent()
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
        ring = _polygon(lon, lat).asPolygon()[0]
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
                    {"lat": point.y(), "lon": point.x()} for point in ring
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


def _run_stage(stage: Stage, layers: Dict[str, QgsVectorLayer]) -> QgsVectorLayer:
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

    active = layers[stage.active]
    tokens = ContextTokenService()
    controller = AgentController(
        build_default_registry(lambda: None, tokens, active_layer_provider=lambda: active)
    )
    described = controller.execute(
        AgentToolCall(
            call_id="describe",
            tool_name="processing.describe",
            arguments={"algorithm_id": stage.algorithm_id},
        ),
        AgentMode.PLAN,
        AgentScope.ACTIVE_LAYER,
    )
    if described.status != AgentResultStatus.SUCCESS:
        raise RuntimeError(f"{stage.name}: describe failed: {described.message}")

    proposal = parse_proposal(
        PROPOSAL_KIND_PROCESSING_RUN,
        json.dumps(
            {
                "schema_version": 1,
                "context_token": described.data["context_token"],
                "algorithm_id": stage.algorithm_id,
                "title": stage.name,
                "summary": f"Chained matrix stage: {stage.name}.",
                "inputs": stage.inputs(layers),
                "warnings": [],
            }
        ),
    )
    validator = RuntimeProposalValidator(
        lambda: None, tokens, active_layer_provider=lambda: active
    )
    validation = validator.validate(
        PROPOSAL_KIND_PROCESSING_RUN, proposal, AgentMode.ACT, AgentScope.ACTIVE_LAYER
    )
    if not validation.ok:
        raise RuntimeError(
            f"{stage.name}: proposal rejected: {validation.reason_code} {validation.message}"
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
        f"chain_{stage.name}",
        stage.name,
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
            f"{stage.name}: run failed: refusal={refusal!r}, failures={failed!r}"
        )

    candidates = []
    for layer_id in set(project.mapLayers()) - before:
        layer = project.mapLayer(layer_id)
        if not isinstance(layer, QgsVectorLayer):
            project.removeMapLayer(layer_id)
            continue
        if stage.geometry is not None and layer.geometryType() != stage.geometry:
            project.removeMapLayer(layer_id)
            continue
        candidates.append(layer)
    if len(candidates) != 1:
        raise RuntimeError(
            f"{stage.name}: expected one result layer, got {len(candidates)}"
        )
    result = candidates[0]

    count = result.featureCount()
    if count < stage.min_features:
        raise RuntimeError(
            f"{stage.name}: produced {count} features, expected at least "
            f"{stage.min_features}"
        )
    if stage.exact_features is not None and count != stage.exact_features:
        raise RuntimeError(
            f"{stage.name}: produced {count} features, expected exactly "
            f"{stage.exact_features}"
        )
    names = {field.name() for field in result.fields()}
    missing = [name for name in stage.required_fields if name not in names]
    if missing:
        raise RuntimeError(
            f"{stage.name}: lost required fields {missing!r}; present={sorted(names)!r}"
        )
    for field_name in stage.populated_fields:
        for feature in result.getFeatures():
            value = feature[field_name]
            if value is None or (hasattr(value, "isNull") and value.isNull()):
                raise RuntimeError(f"{stage.name}: {field_name!r} holds NULL")
            try:
                float(value)
            except (TypeError, ValueError):
                raise RuntimeError(
                    f"{stage.name}: {field_name!r} is not numeric"
                ) from None
    if stage.crs and result.crs().authid() != stage.crs:
        raise RuntimeError(
            f"{stage.name}: CRS is {result.crs().authid()}, expected {stage.crs}"
        )
    result.setName(f"{stage.produces}")
    return result


def _download_inputs(extent_key: str) -> Callable[[Dict[str, QgsVectorLayer]], Dict[str, Any]]:
    def build(layers: Dict[str, QgsVectorLayer]) -> Dict[str, Any]:
        return {
            "MATCH_MODE": {"enum_string": "Match all tags (AND)"},
            "GEOMETRY": {"enum_string": "Polygons"},
            "KEY_1": {"osm_tag": "boundary"},
            "VALUE_1": {"osm_tag": "administrative"},
            "KEY_2": {"osm_tag": "admin_level"},
            "VALUE_2": {"osm_tag": "10"},
            "EXTENT": {"layer_extent": layers[extent_key].id()},
        }

    return build


def _scenarios() -> Tuple[Scenario, ...]:
    download = Stage(
        name="download neighborhood polygons",
        algorithm_id="zero2agentosm:download_advanced",
        inputs=_download_inputs("extent"),
        active="extent",
        produces="neighborhoods",
        geometry=POLYGON,
        exact_features=3,
        required_fields=("name",),
    )
    # "to metres" has to mean *true* metres: EPSG:3857 reports metres inflated
    # by 1/cos^2(latitude), so measuring $area in it is the exact mistake the
    # run planner now refuses. UTM 35N covers the seeded Izmir district.
    reproject = Stage(
        name="reproject to metres",
        algorithm_id="native:reprojectlayer",
        inputs=lambda l: {
            "INPUT": {"layer": l["neighborhoods"].id()},
            "TARGET_CRS": {"crs": METRIC_CRS},
        },
        active="neighborhoods",
        produces="projected",
        geometry=POLYGON,
        exact_features=3,
        required_fields=("name",),
        crs=METRIC_CRS,
    )
    measure = Stage(
        name="calculate area",
        algorithm_id="native:fieldcalculator",
        inputs=lambda l: {
            "INPUT": {"layer": l["projected"].id()},
            "FIELD_NAME": {"string": "area_m2"},
            "FIELD_TYPE": {"enum_string": "Decimal (double)"},
            "FORMULA": {"expression": "$area"},
        },
        active="projected",
        produces="measured",
        geometry=POLYGON,
        exact_features=3,
        required_fields=("name", "area_m2"),
        populated_fields=("area_m2",),
        crs=METRIC_CRS,
    )

    return (
        Scenario(
            "measure then reshape",
            (
                download,
                reproject,
                measure,
                # A field created three stages earlier must survive a buffer,
                # a geometry-type change and a bounding-box rebuild.
                Stage(
                    name="buffer measured polygons",
                    algorithm_id="native:buffer",
                    inputs=lambda l: {
                        "INPUT": {"layer": l["measured"].id()},
                        "DISTANCE": {"distance": 150},
                        "DISSOLVE": {"bool": False},
                    },
                    active="measured",
                    produces="buffered",
                    geometry=POLYGON,
                    exact_features=3,
                    required_fields=("name", "area_m2"),
                    populated_fields=("area_m2",),
                    crs=METRIC_CRS,
                ),
                Stage(
                    name="centroids of buffers",
                    algorithm_id="native:centroids",
                    inputs=lambda l: {"INPUT": {"layer": l["buffered"].id()}},
                    active="buffered",
                    produces="centres",
                    geometry=POINT,
                    exact_features=3,
                    required_fields=("name", "area_m2"),
                    populated_fields=("area_m2",),
                ),
                Stage(
                    name="bounding boxes of centroids",
                    algorithm_id="native:boundingboxes",
                    inputs=lambda l: {"INPUT": {"layer": l["centres"].id()}},
                    active="centres",
                    produces="boxes",
                    exact_features=3,
                    required_fields=("name", "area_m2"),
                ),
            ),
        ),
        Scenario(
            "two-layer overlay",
            (
                download,
                reproject,
                measure,
                Stage(
                    name="buffer for overlay",
                    algorithm_id="native:buffer",
                    inputs=lambda l: {
                        "INPUT": {"layer": l["measured"].id()},
                        "DISTANCE": {"distance": 300},
                        "DISSOLVE": {"bool": False},
                    },
                    active="measured",
                    produces="rings",
                    geometry=POLYGON,
                    exact_features=3,
                    crs=METRIC_CRS,
                ),
                # The secondary input is deliberately NOT the active layer:
                # ACTIVE_LAYER scope pins the primary only, and a chain is
                # wrong in a way no receipt shows if the overlay silently
                # resolves to the active layer too.
                Stage(
                    name="difference against measured",
                    algorithm_id="native:difference",
                    inputs=lambda l: {
                        "INPUT": {"layer": l["rings"].id()},
                        "OVERLAY": {"layer": l["measured"].id()},
                    },
                    active="rings",
                    produces="ring_only",
                    geometry=POLYGON,
                    exact_features=3,
                    crs=METRIC_CRS,
                ),
                Stage(
                    name="fix ring geometries",
                    algorithm_id="native:fixgeometries",
                    inputs=lambda l: {"INPUT": {"layer": l["ring_only"].id()}},
                    active="ring_only",
                    produces="fixed_rings",
                    geometry=POLYGON,
                    min_features=1,
                    crs=METRIC_CRS,
                ),
            ),
        ),
        Scenario(
            "count points in polygons",
            (
                download,
                reproject,
                measure,
                Stage(
                    name="centroids to count",
                    algorithm_id="native:centroids",
                    inputs=lambda l: {"INPUT": {"layer": l["measured"].id()}},
                    active="measured",
                    produces="points",
                    geometry=POINT,
                    exact_features=3,
                ),
                Stage(
                    name="buffer to hold points",
                    algorithm_id="native:buffer",
                    inputs=lambda l: {
                        "INPUT": {"layer": l["measured"].id()},
                        "DISTANCE": {"distance": 100},
                        "DISSOLVE": {"bool": False},
                    },
                    active="measured",
                    produces="cells",
                    geometry=POLYGON,
                    exact_features=3,
                    crs=METRIC_CRS,
                ),
                # FIELD names a *new* field, so this also covers the
                # new_field_params normalization on a second algorithm.
                Stage(
                    name="count points per cell",
                    algorithm_id="native:countpointsinpolygon",
                    inputs=lambda l: {
                        "POLYGONS": {"layer": l["cells"].id()},
                        "POINTS": {"layer": l["points"].id()},
                        "FIELD": {"string": "  point_count "},
                    },
                    active="cells",
                    produces="counted",
                    geometry=POLYGON,
                    exact_features=3,
                    required_fields=("point_count",),
                    populated_fields=("point_count",),
                    crs=METRIC_CRS,
                ),
                Stage(
                    name="density from counted field",
                    algorithm_id="native:fieldcalculator",
                    inputs=lambda l: {
                        "INPUT": {"layer": l["counted"].id()},
                        "FIELD_NAME": {"string": "density"},
                        "FIELD_TYPE": {"enum_string": "Decimal (double)"},
                        "FORMULA": {"expression": '"point_count" / ($area / 1000000)'},
                    },
                    active="counted",
                    produces="density",
                    geometry=POLYGON,
                    exact_features=3,
                    required_fields=("point_count", "density"),
                    populated_fields=("density",),
                    crs=METRIC_CRS,
                ),
            ),
        ),
        Scenario(
            "filter twice then merge",
            (
                download,
                reproject,
                measure,
                Stage(
                    name="extract Alsancak",
                    algorithm_id="native:extractbyattribute",
                    inputs=lambda l: {
                        "INPUT": {"layer": l["measured"].id()},
                        "FIELD": {"field": "name", "layer_param": "INPUT"},
                        "OPERATOR": {"enum_string": "="},
                        "VALUE": {"string": "Alsancak"},
                    },
                    active="measured",
                    produces="alsancak",
                    geometry=POLYGON,
                    exact_features=1,
                    crs=METRIC_CRS,
                ),
                Stage(
                    name="extract Goztepe",
                    algorithm_id="native:extractbyattribute",
                    inputs=lambda l: {
                        "INPUT": {"layer": l["measured"].id()},
                        "FIELD": {"field": "name", "layer_param": "INPUT"},
                        "OPERATOR": {"enum_string": "="},
                        "VALUE": {"string": "Goztepe"},
                    },
                    active="measured",
                    produces="goztepe",
                    geometry=POLYGON,
                    exact_features=1,
                    crs=METRIC_CRS,
                ),
                # A multi-layer primary under ACTIVE_LAYER scope. Pinning it to
                # the single active layer silently produced a one-layer merge,
                # so this stage fails on 1 feature instead of 2.
                Stage(
                    name="merge both extractions",
                    algorithm_id="native:mergevectorlayers",
                    inputs=lambda l: {
                        "LAYERS": {
                            "layers": [l["alsancak"].id(), l["goztepe"].id()]
                        }
                    },
                    active="alsancak",
                    produces="merged",
                    geometry=POLYGON,
                    exact_features=2,
                    required_fields=("name", "area_m2"),
                    populated_fields=("area_m2",),
                ),
            ),
        ),
        Scenario(
            "filter then locate",
            (
                download,
                reproject,
                measure,
                Stage(
                    name="extract one neighborhood",
                    algorithm_id="native:extractbyattribute",
                    inputs=lambda l: {
                        "INPUT": {"layer": l["measured"].id()},
                        "FIELD": {"field": "name", "layer_param": "INPUT"},
                        "OPERATOR": {"enum_string": "="},
                        "VALUE": {"string": "Alsancak"},
                    },
                    active="measured",
                    produces="one",
                    geometry=POLYGON,
                    exact_features=1,
                    required_fields=("name", "area_m2"),
                    crs=METRIC_CRS,
                ),
                Stage(
                    name="locate measured within the extracted one",
                    algorithm_id="native:extractbylocation",
                    inputs=lambda l: {
                        "INPUT": {"layer": l["measured"].id()},
                        "PREDICATE": {"enum_string": "intersect"},
                        "INTERSECT": {"layer": l["one"].id()},
                    },
                    active="measured",
                    produces="located",
                    geometry=POLYGON,
                    exact_features=1,
                    required_fields=("name", "area_m2"),
                    crs=METRIC_CRS,
                ),
            ),
        ),
    )


def run_matrix() -> str:
    project = QgsProject.instance()
    passed = 0
    failures = []
    stage_total = 0
    for scenario in _scenarios():
        for layer_id in list(project.mapLayers()):
            project.removeMapLayer(layer_id)
        layers: Dict[str, QgsVectorLayer] = {}
        extent = _extent_layer()
        _seed_osm_response(extent)
        layers["extent"] = extent
        try:
            for stage in scenario.stages:
                layers[stage.produces] = _run_stage(stage, layers)
                stage_total += 1
            passed += 1
            print(
                f"  PASS {scenario.name} ({len(scenario.stages)} stages)",
                flush=True,
            )
        except Exception as error:  # noqa: BLE001
            failures.append(f"{scenario.name}: {type(error).__name__}: {error}")
            print(f"  FAIL {scenario.name}: {error}", flush=True)

    for layer_id in list(project.mapLayers()):
        project.removeMapLayer(layer_id)
    total = len(_scenarios())
    if failures:
        raise RuntimeError(
            f"{len(failures)}/{total} chained scenarios failed: " + "; ".join(failures)
        )
    return (
        f"AGENT CHAINED MATRIX PASS: {passed}/{total} scenarios, "
        f"{stage_total} reviewed stages, offline and temporary outputs only."
    )


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QgsApplication([], False)
    application.initQgis()
    source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plugins_root = os.path.dirname(source_root)
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
            print(run_matrix(), flush=True)
            return 0
        finally:
            for provider in reversed(added):
                registry.removeProvider(provider)
            sibling_plugin.unload()
            qgis_utils.plugins.pop(sibling_package, None)
            if sibling_package in qgis_utils.active_plugins:
                qgis_utils.active_plugins.remove(sibling_package)
    except Exception as error:  # noqa: BLE001
        print(f"AGENT CHAINED MATRIX FAIL: {type(error).__name__}: {error}", flush=True)
        return 1
    finally:
        QgsProject.instance().clear()
        application.exitQgis()
        # Keep the QgsApplication referenced past this frame. When ``main``
        # returned, dropping the last reference ran the C++ destructor, and
        # on Windows that can sit forever after a suite has already passed --
        # the verify gate then waits on a process with nothing left to do.
        # The module-level ``os._exit`` is the real end of this process.
        globals()["_QGIS_APPLICATION"] = application


if __name__ == "__main__":
    _code = main()
    # Flush, then leave immediately. A headless QgsApplication can sit in
    # Qt/GDAL static teardown after the suite has already printed its
    # result and returned -- observed on Windows, on an unmodified
    # checkout, with every assertion passed -- and the verify gate then
    # waits forever on a process with nothing left to do. The exit code
    # is the suite's own, so a failure still fails.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)
