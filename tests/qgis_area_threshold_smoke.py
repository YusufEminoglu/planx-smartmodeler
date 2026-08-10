"""Headless acceptance test for the three silent wrong-answer traps.

Reproduces the owner session that motivated them: OSM buildings arrive in
EPSG:3857, an area column is calculated, and a "smaller than 400 m2" filter
returns nothing at all -- not because no building is that small, but because
Web Mercator inflates area by 1/cos^2(latitude).

Every assertion below is checked against live QGIS, not a stub, because all
three traps are QGIS behaviours rather than plugin logic: Processing really
does execute each of these runs and really does report success.
"""
from __future__ import annotations

import math
import os
import sys

from qgis.core import (
    QgsApplication,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsVectorLayer,
)

# True footprint areas in m^2 for the synthetic buildings below. Chosen so the
# 400 m^2 threshold lands *inside* the set both before and after distortion:
# 4 of 6 are genuinely under 400, but only 3 survive a Mercator measurement.
TRUE_SIDES_M = (8.0, 12.0, 15.0, 18.0, 25.0, 30.0)
THRESHOLD_M2 = 400
LATITUDE = 41.0
LONGITUDE = 29.0


def _square(lon: float, lat: float, side_m: float) -> QgsGeometry:
    """A square of ``side_m`` true metres on the ground at (lon, lat)."""
    dlat = side_m / 111_320.0
    dlon = side_m / (111_320.0 * math.cos(math.radians(lat)))
    return QgsGeometry.fromPolygonXY(
        [[
            QgsPointXY(lon, lat),
            QgsPointXY(lon + dlon, lat),
            QgsPointXY(lon + dlon, lat + dlat),
            QgsPointXY(lon, lat + dlat),
            QgsPointXY(lon, lat),
        ]]
    )


def _buildings(crs_authid: str) -> QgsVectorLayer:
    layer = QgsVectorLayer(f"Polygon?crs={crs_authid}", "buildings", "memory")
    transform = QgsCoordinateTransform(
        QgsCoordinateReferenceSystem("EPSG:4326"),
        QgsCoordinateReferenceSystem(crs_authid),
        QgsProject.instance(),
    )
    features = []
    for index, side in enumerate(TRUE_SIDES_M):
        geometry = _square(LONGITUDE + index * 0.001, LATITUDE, side)
        geometry.transform(transform)
        feature = QgsFeature()
        feature.setGeometry(geometry)
        features.append(feature)
    layer.dataProvider().addFeatures(features)
    layer.updateExtents()
    return layer


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    source_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    plugins_root = os.path.dirname(source_root)
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
        import processing

        Processing.initialize()

        from planx_smartmodeler.core.agent.contracts import AgentToolCall
        from planx_smartmodeler.core.agent.runtime_tools import (
            crs_is_area_safe,
            build_default_registry,
        )

        project = QgsProject.instance()

        # -- 1. The CRS flag must name the real trap ------------------------
        _require(
            not crs_is_area_safe(QgsCoordinateReferenceSystem("EPSG:3857")),
            "Web Mercator was reported as safe to measure area in.",
        )
        _require(
            not crs_is_area_safe(QgsCoordinateReferenceSystem("EPSG:4326")),
            "A geographic CRS was reported as safe to measure area in.",
        )
        for authid in ("EPSG:32635", "EPSG:5254", "EPSG:3035"):
            _require(
                crs_is_area_safe(QgsCoordinateReferenceSystem(authid)),
                f"{authid} measures true metres but was refused.",
            )

        # -- 2. Mercator really does destroy the threshold ------------------
        mercator = _buildings("EPSG:3857")
        project.addMapLayer(mercator)
        distorted = processing.run(
            "native:fieldcalculator",
            {
                "INPUT": mercator,
                "FIELD_NAME": "alan_m2",
                "FIELD_TYPE": 0,
                "FIELD_LENGTH": 20,
                "FIELD_PRECISION": 3,
                "FORMULA": "$area",
                "OUTPUT": "memory:distorted",
            },
        )["OUTPUT"]
        distorted_kept = processing.run(
            "native:extractbyattribute",
            {
                "INPUT": distorted,
                "FIELD": "alan_m2",
                "OPERATOR": 4,
                "VALUE": str(THRESHOLD_M2),
                "OUTPUT": "memory:",
            },
        )["OUTPUT"].featureCount()

        metric = _buildings("EPSG:32635")
        project.addMapLayer(metric)
        true_areas = processing.run(
            "native:fieldcalculator",
            {
                "INPUT": metric,
                "FIELD_NAME": "alan_m2",
                "FIELD_TYPE": 0,
                "FIELD_LENGTH": 20,
                "FIELD_PRECISION": 3,
                "FORMULA": "$area",
                "OUTPUT": "memory:true",
            },
        )["OUTPUT"]
        true_kept = processing.run(
            "native:extractbyattribute",
            {
                "INPUT": true_areas,
                "FIELD": "alan_m2",
                "OPERATOR": 4,
                "VALUE": str(THRESHOLD_M2),
                "OUTPUT": "memory:",
            },
        )["OUTPUT"].featureCount()

        expected_true = sum(
            1 for side in TRUE_SIDES_M if side * side < THRESHOLD_M2
        )
        _require(
            true_kept == expected_true,
            f"A metric CRS should keep {expected_true} buildings, kept {true_kept}.",
        )
        _require(
            distorted_kept < true_kept,
            "Web Mercator did not distort the threshold, so this test proves "
            "nothing; the fixture no longer straddles the cut-off.",
        )

        # -- 3. An ordering comparison on a text field is silently wrong ----
        as_text = processing.run(
            "native:fieldcalculator",
            {
                "INPUT": metric,
                "FIELD_NAME": "alan_txt",
                "FIELD_TYPE": 2,
                "FIELD_LENGTH": 30,
                "FIELD_PRECISION": 3,
                "FORMULA": "$area",
                "OUTPUT": "memory:text",
            },
        )["OUTPUT"]
        text_kept = processing.run(
            "native:extractbyattribute",
            {
                "INPUT": as_text,
                "FIELD": "alan_txt",
                "OPERATOR": 4,
                "VALUE": str(THRESHOLD_M2),
                "OUTPUT": "memory:",
            },
        )["OUTPUT"].featureCount()
        _require(
            text_kept != expected_true,
            "A lexicographic comparison happened to agree with a numeric one, "
            "so the fixture cannot demonstrate the trap.",
        )

        # -- 4. Recalculating a field never changes its type ----------------
        retyped = processing.run(
            "native:fieldcalculator",
            {
                "INPUT": as_text,
                "FIELD_NAME": "alan_txt",
                "FIELD_TYPE": 1,
                "FIELD_LENGTH": 10,
                "FIELD_PRECISION": 0,
                "FORMULA": '"alan_txt"',
                "OUTPUT": "memory:retyped",
            },
        )["OUTPUT"]
        retyped_name = retyped.fields().field("alan_txt").typeName().casefold()
        _require(
            "string" in retyped_name,
            f"QGIS changed an existing field's type after all ({retyped_name!r}); "
            "the run planner's rejection of this case may now be wrong.",
        )

        # -- 5. layer.field_values explains every one of the above ----------
        project.addMapLayer(distorted)
        registry = build_default_registry(lambda: None)
        field_values = registry.get_handler("layer.field_values")
        _require(field_values is not None, "layer.field_values is not registered.")

        def read(layer, name, call_id):
            return field_values(
                AgentToolCall(
                    call_id=call_id,
                    tool_name="layer.field_values",
                    arguments={"layer_id": layer.id(), "field_name": name},
                )
            )

        data = read(distorted, "alan_m2", "values_1")
        _require(bool(data.get("available")), "layer.field_values found no layer.")
        _require(bool(data.get("numeric")), "A double field was not read as numeric.")
        _require(
            data.get("area_safe_crs") is False,
            "layer.field_values did not flag the Mercator source.",
        )
        _require(
            data.get("minimum") is not None
            and data["minimum"] > min(side * side for side in TRUE_SIDES_M),
            "The reported minimum does not show the Mercator inflation, which "
            "is the single fact that would have stopped the agent guessing.",
        )
        _require(
            len(data.get("sample") or []) == len(TRUE_SIDES_M),
            "The value sample did not cover the whole (small) layer.",
        )

        # The tool resolves a layer by project id, so a Processing result has
        # to be in the project before it can be read -- exactly as it is in a
        # real session, where every run adds its output as a temporary layer.
        project.addMapLayer(as_text)
        text_data = read(as_text, "alan_txt", "values_2")
        _require(
            "string" in str(text_data.get("field_type", "")).casefold(),
            "The text field's real type was not reported.",
        )
        _require(
            bool(text_data.get("numeric")) and text_data.get("minimum") is not None,
            "A String field holding only numbers was not recognised as "
            "numeric, so the tool cannot expose the lexicographic trap.",
        )

        missing_data = read(metric, "yok_boyle", "values_3")
        _require(
            missing_data.get("available") is False
            and missing_data.get("field_missing") is True,
            "An absent field was not reported as absent.",
        )

        # -- 5b. a layer named instead of identified must not dead-end -------
        # An owner session lost eight turns here: the model passed the layer's
        # *name* as layer_id, got available:false four times for a layer that
        # was right there and active, and finally told the user to select a
        # layer that was already selected.
        by_name = field_values(
            AgentToolCall(
                call_id="values_by_name",
                tool_name="layer.field_values",
                arguments={
                    "layer_id": distorted.name(),
                    "field_name": "alan_m2",
                },
            )
        )
        _require(
            bool(by_name.get("available")),
            "A layer named exactly, instead of identified, still dead-ended.",
        )
        _require(
            by_name.get("resolved_by") == "name",
            "A name-resolved layer did not say how it was resolved.",
        )
        _require(
            by_name.get("layer_id") == distorted.id(),
            "A name-resolved result did not report the real layer id.",
        )
        nowhere = field_values(
            AgentToolCall(
                call_id="values_nowhere",
                tool_name="layer.field_values",
                arguments={"layer_id": "no such layer", "field_name": "alan_m2"},
            )
        )
        _require(
            nowhere.get("available") is False and bool(nowhere.get("hint")),
            "An unresolvable layer was refused without saying what to do next.",
        )

        # -- 6. "the local CRS" must be answerable, never invented ----------
        suggest = registry.get_handler("layer.suggest_crs")
        _require(suggest is not None, "layer.suggest_crs is not registered.")

        def suggestions(layer, call_id):
            return suggest(
                AgentToolCall(
                    call_id=call_id,
                    tool_name="layer.suggest_crs",
                    arguments={"layer_id": layer.id()},
                )
            )

        merc_crs = suggestions(mercator, "crs_1")
        _require(
            merc_crs.get("current_crs_area_safe") is False,
            "The Mercator source was not flagged when suggesting a CRS.",
        )
        offered = {item["crs"]: item for item in merc_crs.get("suggestions") or ()}
        _require(bool(offered), "No metric CRS was offered for a Mercator layer.")
        # The fixture sits at 29E / 41N, which is UTM zone 35N.
        _require(
            "EPSG:32635" in offered
            and offered["EPSG:32635"]["reason"] == "utm_zone",
            f"The layer's own UTM zone was not offered: {sorted(offered)}",
        )
        for authid, item in offered.items():
            candidate = QgsCoordinateReferenceSystem(authid)
            _require(
                candidate.isValid(),
                f"{authid} was offered but is not a live CRS.",
            )
            _require(
                crs_is_area_safe(candidate),
                f"{authid} was offered as a metric CRS but is not area-safe.",
            )
            bounds = candidate.bounds()
            _require(
                bounds.xMinimum() <= merc_crs["centre_lon"] <= bounds.xMaximum()
                and bounds.yMinimum() <= merc_crs["centre_lat"] <= bounds.yMaximum(),
                f"{authid} was offered but its area of use excludes the layer.",
            )
            _require(
                bool(item.get("description")),
                f"{authid} was offered without a description to choose by.",
            )
        # A layer already in a metric CRS says so instead of demanding a move.
        metric_crs = suggestions(metric, "crs_2")
        _require(
            metric_crs.get("current_crs_area_safe") is True,
            "A UTM layer was reported as unsafe to measure in.",
        )

        print(
            "AREA THRESHOLD SMOKE OK - "
            f"metric kept {true_kept}/{len(TRUE_SIDES_M)}, "
            f"Mercator kept {distorted_kept}, text compare kept {text_kept}, "
            f"CRS offered {sorted(offered)}"
        )
        return 0
    finally:
        application.exitQgis()


if __name__ == "__main__":
    raise SystemExit(main())
