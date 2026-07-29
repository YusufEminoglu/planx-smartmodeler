"""Pure validation and query construction for SmartModeler's OSM downloader.

The AI provider may choose only a plain OSM key/value pair and one of three
application-owned geometry modes.  It cannot supply Overpass QL, an endpoint,
a timeout, a path, or arbitrary request data.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any, Dict, Tuple

OVERPASS_ENDPOINTS: Tuple[str, ...] = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
)
OVERPASS_TIMEOUT_SECONDS = 45
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
MAX_FEATURES = 100_000
MAX_BBOX_AREA_KM2 = 100.0
GEOMETRY_KINDS = ("point", "line", "polygon")

_KEY_RE = re.compile(r"^[A-Za-z0-9_:.~-]{1,80}$")
_UNSAFE_VALUE_RE = re.compile(r"[\x00-\x1f\x7f\"\\;\[\]\(\){}]")


class OsmQueryError(ValueError):
    """A bounded, user-actionable OSM request or response error."""


def normalize_tag(key: object, value: object = "") -> Tuple[str, str]:
    """Return a safe OSM key/value pair.

    ``*`` is accepted as the familiar wildcard spelling and normalized to an
    empty value, which Overpass represents as a key-existence filter.
    """
    key_text = str(key or "").strip()
    value_text = str(value or "").strip()
    if not _KEY_RE.fullmatch(key_text):
        raise OsmQueryError(
            "The OSM key must use only letters, numbers, colon, dot, underscore, "
            "tilde, or hyphen."
        )
    if value_text == "*":
        value_text = ""
    if len(value_text) > 120 or _UNSAFE_VALUE_RE.search(value_text):
        raise OsmQueryError("The OSM value contains unsupported query characters.")
    return key_text, value_text


def validate_bbox(
    min_lat: object,
    min_lon: object,
    max_lat: object,
    max_lon: object,
) -> Tuple[float, float, float, float]:
    """Validate a WGS84 request box and enforce a conservative area ceiling."""
    try:
        south, west, north, east = (
            float(min_lat),
            float(min_lon),
            float(max_lat),
            float(max_lon),
        )
    except (TypeError, ValueError) as exc:
        raise OsmQueryError("The current map extent is not a valid OSM request area.") from exc
    values = (south, west, north, east)
    if not all(math.isfinite(number) for number in values):
        raise OsmQueryError("The current map extent is not finite.")
    if not (-90.0 <= south < north <= 90.0 and -180.0 <= west < east <= 180.0):
        raise OsmQueryError("The current map extent is outside valid WGS84 bounds.")

    mean_lat = math.radians((south + north) / 2.0)
    height_km = (north - south) * 111.32
    width_km = (east - west) * 111.32 * max(0.01, abs(math.cos(mean_lat)))
    area_km2 = height_km * width_km
    if not math.isfinite(area_km2) or area_km2 <= 0.0:
        raise OsmQueryError("The current map extent has no usable area.")
    if area_km2 > MAX_BBOX_AREA_KM2:
        raise OsmQueryError(
            f"The current map extent is about {area_km2:,.1f} km²; zoom in below "
            f"{MAX_BBOX_AREA_KM2:,.0f} km² and retry."
        )
    return south, west, north, east


def build_overpass_query(
    key: object,
    value: object,
    geometry_kind: str,
    bbox: Tuple[object, object, object, object],
) -> str:
    """Build one bounded Overpass query from already constrained primitives."""
    key_text, value_text = normalize_tag(key, value)
    if geometry_kind not in GEOMETRY_KINDS:
        raise OsmQueryError("The requested OSM geometry type is not supported.")
    south, west, north, east = validate_bbox(*bbox)
    box = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    selector = (
        f'["{key_text}"="{value_text}"]'
        if value_text
        else f'["{key_text}"]'
    )
    if geometry_kind == "point":
        body = f'node{selector}({box});'
    elif geometry_kind == "line":
        body = f'way{selector}({box});'
    else:
        body = (
            "(\n"
            f"  way{selector}({box});\n"
            f"  relation{selector}({box});\n"
            ");"
        )
    output_clause = "out body geom;" if geometry_kind == "polygon" else "out tags geom;"
    return (
        f"[out:json][timeout:{OVERPASS_TIMEOUT_SECONDS}];\n"
        f"{body}\n"
        f"{output_clause}"
    )


def validate_payload(payload: Any) -> Dict[str, Any]:
    """Validate the bounded part of the Overpass JSON response contract."""
    if not isinstance(payload, dict):
        raise OsmQueryError("The OSM server returned an invalid response.")
    remark = str(payload.get("remark") or "").strip()
    if remark:
        raise OsmQueryError("The OSM server could not complete this request; zoom in and retry.")
    elements = payload.get("elements")
    if not isinstance(elements, list):
        raise OsmQueryError("The OSM response does not contain an elements list.")
    if len(elements) > MAX_FEATURES:
        raise OsmQueryError(
            f"The OSM response contains more than {MAX_FEATURES:,} elements; zoom in and retry."
        )
    if any(not isinstance(element, dict) for element in elements):
        raise OsmQueryError("The OSM response contains an invalid element.")
    return payload


def compact_tags(tags: object) -> str:
    """Return deterministic bounded JSON for the full OSM tag dictionary."""
    clean = tags if isinstance(tags, dict) else {}
    text = json.dumps(clean, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text[:16_000]
