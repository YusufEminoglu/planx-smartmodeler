"""Direct, dependency-free OSM key/value download algorithms.

These algorithms deliberately expose no endpoint, Overpass QL, URL, path, or
timeout parameter.  They use QGIS' proxy-aware blocking network request with
the Processing feedback object for cancellation, try three application-owned
mirrors, cap request extent/response size/feature count, and write only to a
normal Processing feature sink.
"""
from __future__ import annotations

import json
import time
from typing import Dict, List, Optional

from qgis.PyQt.QtCore import QByteArray, QMetaType, QUrl, QUrlQuery
from qgis.PyQt.QtNetwork import QNetworkRequest
from qgis.core import (
    QgsBlockingNetworkRequest,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterString,
    QgsProject,
    QgsWkbTypes,
)

from ..core.osm_query import (
    MAX_RESPONSE_BYTES,
    OVERPASS_ENDPOINTS,
    OVERPASS_TIMEOUT_SECONDS,
    OsmQueryError,
    build_overpass_query,
    compact_tags,
    normalize_tag,
    validate_payload,
)

USER_AGENT = (
    "SmartModeler-GIS-QGIS-Plugin/0.15 "
    "(https://github.com/YusufEminoglu/planx-smartmodeler)"
)
_SESSION_CACHE: Dict[str, tuple] = {}
_SESSION_CACHE_TTL_SECONDS = 15 * 60
_SESSION_CACHE_LIMIT = 6


def _known_header(name: str):
    enum = getattr(QNetworkRequest, "KnownHeaders", None)
    return getattr(enum, name) if enum is not None else getattr(QNetworkRequest, name)


def _status_attribute():
    enum = getattr(QNetworkRequest, "Attribute", None)
    if enum is not None:
        return enum.HttpStatusCodeAttribute
    return QNetworkRequest.HttpStatusCodeAttribute


def _fetch_json(query: str, feedback) -> Dict:
    """POST to pinned mirrors through QGIS networking and return validated JSON."""
    cached = _SESSION_CACHE.get(query)
    if cached is not None and time.monotonic() - cached[0] <= _SESSION_CACHE_TTL_SECONDS:
        feedback.pushInfo("Using the SmartModeler session OSM cache ...")
        return cached[1]
    _SESSION_CACHE.pop(query, None)

    encoded = QUrlQuery()
    encoded.addQueryItem("data", query)
    body = QByteArray(encoded.query(QUrl.ComponentFormattingOption.FullyEncoded).encode("ascii"))
    errors: List[str] = []
    for index, endpoint in enumerate(OVERPASS_ENDPOINTS):
        if feedback.isCanceled():
            raise QgsProcessingException("The OSM download was canceled.")
        host = QUrl(endpoint).host()
        feedback.pushInfo(
            f"Querying {host} ..." if index == 0 else f"Trying mirror {host} ..."
        )
        request = QNetworkRequest(QUrl(endpoint))
        if hasattr(request, "setTransferTimeout"):
            request.setTransferTimeout((OVERPASS_TIMEOUT_SECONDS + 5) * 1000)
        request.setHeader(
            _known_header("ContentTypeHeader"),
            "application/x-www-form-urlencoded",
        )
        request.setRawHeader(b"Accept", b"application/json")
        request.setRawHeader(b"User-Agent", USER_AGENT.encode("ascii"))
        client = QgsBlockingNetworkRequest()
        code = client.post(request, body, False, feedback)
        if code != QgsBlockingNetworkRequest.NoError:
            # QGIS' raw error often embeds the endpoint URL. Keep the surfaced
            # failure useful but path/URL-free so Agent Chat can display it.
            errors.append("network request failed")
            continue
        reply = client.reply()
        status = reply.attribute(_status_attribute())
        try:
            status_code = int(status)
        except (TypeError, ValueError):
            status_code = 0
        if status_code and not 200 <= status_code < 300:
            errors.append(f"HTTP {status_code}")
            continue
        payload_bytes = bytes(reply.content())
        if len(payload_bytes) > MAX_RESPONSE_BYTES:
            raise QgsProcessingException(
                f"The OSM response exceeded {MAX_RESPONSE_BYTES // (1024 * 1024)} MB; "
                "zoom in and retry."
            )
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
            payload = validate_payload(payload)
            if len(_SESSION_CACHE) >= _SESSION_CACHE_LIMIT:
                oldest = min(_SESSION_CACHE, key=lambda item: _SESSION_CACHE[item][0])
                _SESSION_CACHE.pop(oldest, None)
            _SESSION_CACHE[query] = (time.monotonic(), payload)
            return payload
        except (UnicodeDecodeError, json.JSONDecodeError, OsmQueryError) as exc:
            errors.append(str(exc))
    detail = errors[-1] if errors else "no server answered"
    raise QgsProcessingException(
        f"All OSM servers failed ({detail}). Zoom in or retry in a minute."
    )


def _points(coordinates: object) -> List[QgsPointXY]:
    if not isinstance(coordinates, list):
        return []
    points: List[QgsPointXY] = []
    for coordinate in coordinates:
        if not isinstance(coordinate, dict):
            return []
        try:
            lat = float(coordinate["lat"])
            lon = float(coordinate["lon"])
        except (KeyError, TypeError, ValueError):
            return []
        points.append(QgsPointXY(lon, lat))
    return points


def _closed_ring(coordinates: object) -> List[QgsPointXY]:
    ring = _points(coordinates)
    if len(ring) < 3:
        return []
    if ring[0] != ring[-1]:
        ring.append(QgsPointXY(ring[0]))
    return ring if len(ring) >= 4 else []


def _relation_polygon(element: Dict) -> Optional[QgsGeometry]:
    members = element.get("members")
    if not isinstance(members, list):
        return None
    outers: List[QgsGeometry] = []
    inners: List[QgsGeometry] = []
    for member in members:
        if not isinstance(member, dict) or member.get("type") != "way":
            continue
        ring = _closed_ring(member.get("geometry"))
        if not ring:
            continue
        polygon = QgsGeometry.fromPolygonXY([ring])
        if polygon.isEmpty():
            continue
        if member.get("role") == "inner":
            inners.append(polygon)
        else:
            outers.append(polygon)
    if not outers:
        return None
    geometry = QgsGeometry.unaryUnion(outers)
    if inners and not geometry.isEmpty():
        geometry = geometry.difference(QgsGeometry.unaryUnion(inners))
    if geometry.isEmpty():
        return None
    geometry.convertToMultiType()
    return geometry


def _geometry(element: Dict, kind: str) -> Optional[QgsGeometry]:
    element_type = element.get("type")
    if kind == "point":
        if element_type != "node":
            return None
        try:
            return QgsGeometry.fromPointXY(
                QgsPointXY(float(element["lon"]), float(element["lat"]))
            )
        except (KeyError, TypeError, ValueError):
            return None
    if kind == "line":
        if element_type != "way":
            return None
        points = _points(element.get("geometry"))
        return QgsGeometry.fromPolylineXY(points) if len(points) >= 2 else None
    if element_type == "relation":
        return _relation_polygon(element)
    if element_type != "way":
        return None
    ring = _closed_ring(element.get("geometry"))
    if not ring:
        return None
    geometry = QgsGeometry.fromPolygonXY([ring])
    geometry.convertToMultiType()
    return geometry


def _fields() -> QgsFields:
    fields = QgsFields()
    for name in (
        "osm_id",
        "osm_type",
        "name",
        "osm_key",
        "osm_value",
        "building",
        "highway",
        "amenity",
        "landuse",
        "leisure",
        "natural",
        "public_transport",
        "shop",
        "tourism",
        "height",
        "building_levels",
        "tags_json",
    ):
        fields.append(QgsField(name, QMetaType.Type.QString))
    return fields


def _attributes(element: Dict, key: str, value: str) -> List[str]:
    tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
    return [
        str(element.get("id", "")),
        str(element.get("type", "")),
        str(tags.get("name", "")),
        key,
        str(tags.get(key, value)),
        str(tags.get("building", "")),
        str(tags.get("highway", "")),
        str(tags.get("amenity", "")),
        str(tags.get("landuse", "")),
        str(tags.get("leisure", "")),
        str(tags.get("natural", "")),
        str(tags.get("public_transport", "")),
        str(tags.get("shop", "")),
        str(tags.get("tourism", "")),
        str(tags.get("height", "")),
        str(tags.get("building:levels", "")),
        compact_tags(tags),
    ]


class _DownloadOsmAlgorithm(QgsProcessingAlgorithm):
    KEY = "KEY"
    VALUE = "VALUE"
    EXTENT = "EXTENT"
    OUTPUT = "OUTPUT"
    GEOMETRY_KIND = ""
    ALGORITHM_NAME = ""
    DISPLAY_NAME = ""
    WKB_TYPE = QgsWkbTypes.Type.Unknown

    def name(self) -> str:
        return self.ALGORITHM_NAME

    def displayName(self) -> str:
        return self.DISPLAY_NAME

    def group(self) -> str:
        return "OpenStreetMap"

    def groupId(self) -> str:
        return "openstreetmap"

    def shortHelpString(self) -> str:
        return (
            "Downloads one plain OSM key/value filter for the selected extent. "
            "The endpoint, query language, timeout, and output destination are controlled "
            "by SmartModeler. The request is limited to 100 km² and temporary outputs are "
            "recommended."
        )

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, _configuration=None) -> None:
        self.addParameter(QgsProcessingParameterString(self.KEY, "OSM tag key"))
        self.addParameter(
            QgsProcessingParameterString(
                self.VALUE,
                "OSM tag value (blank or * means any value)",
                defaultValue="",
                optional=True,
            )
        )
        self.addParameter(QgsProcessingParameterExtent(self.EXTENT, "Download extent"))
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, "OSM result"))

    def processAlgorithm(self, parameters, context, feedback):
        key, value = normalize_tag(
            self.parameterAsString(parameters, self.KEY, context),
            self.parameterAsString(parameters, self.VALUE, context),
        )
        extent = self.parameterAsExtent(parameters, self.EXTENT, context)
        extent_crs = self.parameterAsExtentCrs(parameters, self.EXTENT, context)
        project = context.project() or QgsProject.instance()
        if not extent_crs.isValid() and project is not None:
            extent_crs = project.crs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        if not extent_crs.isValid():
            extent_crs = wgs84
        try:
            if extent_crs != wgs84:
                transform = QgsCoordinateTransform(
                    extent_crs,
                    wgs84,
                    context.transformContext(),
                )
                wgs_extent = transform.transformBoundingBox(extent)
            else:
                wgs_extent = extent
            bbox = (
                wgs_extent.yMinimum(),
                wgs_extent.xMinimum(),
                wgs_extent.yMaximum(),
                wgs_extent.xMaximum(),
            )
            query = build_overpass_query(key, value, self.GEOMETRY_KIND, bbox)
        except OsmQueryError as exc:
            raise QgsProcessingException(str(exc)) from exc

        feedback.setProgress(5)
        payload = _fetch_json(query, feedback)
        if feedback.isCanceled():
            raise QgsProcessingException("The OSM download was canceled.")
        feedback.setProgress(45)

        fields = _fields()
        sink, destination = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            self.WKB_TYPE,
            extent_crs,
        )
        if sink is None:
            raise QgsProcessingException("QGIS could not create the temporary OSM layer.")

        to_target = (
            QgsCoordinateTransform(wgs84, extent_crs, context.transformContext())
            if extent_crs != wgs84
            else None
        )
        elements = payload.get("elements", [])
        added = 0
        for index, element in enumerate(elements):
            if feedback.isCanceled():
                raise QgsProcessingException("The OSM download was canceled.")
            geometry = _geometry(element, self.GEOMETRY_KIND)
            if geometry is None or geometry.isEmpty():
                continue
            if to_target is not None and geometry.transform(to_target) != 0:
                continue
            feature = QgsFeature(fields)
            feature.setGeometry(geometry)
            feature.setAttributes(_attributes(element, key, value))
            if not sink.addFeature(feature, QgsFeatureSink.Flag.FastInsert):
                raise QgsProcessingException("QGIS could not write an OSM feature.")
            added += 1
            if index % 250 == 0:
                feedback.setProgress(45 + int(50 * (index + 1) / max(1, len(elements))))
        if added == 0:
            raise QgsProcessingException(
                f"OSM returned no {self.GEOMETRY_KIND} features for {key}="
                f"{value or '*'} in this extent."
            )
        feedback.pushInfo(f"Created {added:,} OSM {self.GEOMETRY_KIND} feature(s).")
        feedback.setProgress(100)
        return {self.OUTPUT: destination}


class DownloadOsmPointsAlgorithm(_DownloadOsmAlgorithm):
    GEOMETRY_KIND = "point"
    ALGORITHM_NAME = "osm_download_points"
    DISPLAY_NAME = "Download OSM points from current map extent"
    WKB_TYPE = QgsWkbTypes.Type.Point


class DownloadOsmLinesAlgorithm(_DownloadOsmAlgorithm):
    GEOMETRY_KIND = "line"
    ALGORITHM_NAME = "osm_download_lines"
    DISPLAY_NAME = "Download OSM lines from current map extent"
    WKB_TYPE = QgsWkbTypes.Type.LineString


class DownloadOsmPolygonsAlgorithm(_DownloadOsmAlgorithm):
    GEOMETRY_KIND = "polygon"
    ALGORITHM_NAME = "osm_download_polygons"
    DISPLAY_NAME = "Download OSM polygons from current map extent"
    WKB_TYPE = QgsWkbTypes.Type.MultiPolygon
