from __future__ import annotations

import json
import unittest

from planx_smartmodeler.core.osm_query import (
    MAX_BBOX_AREA_KM2,
    MAX_FEATURES,
    OVERPASS_ENDPOINTS,
    OsmQueryError,
    build_overpass_query,
    compact_tags,
    normalize_tag,
    validate_bbox,
    validate_payload,
)


SMALL_BBOX = (38.40, 27.08, 38.42, 27.11)


class OsmTagTests(unittest.TestCase):
    def test_plain_and_wildcard_tags(self) -> None:
        self.assertEqual(normalize_tag("building", "*"), ("building", ""))
        self.assertEqual(
            normalize_tag("public_transport", "platform"),
            ("public_transport", "platform"),
        )

    def test_query_language_injection_is_rejected(self) -> None:
        for key in ('building"];out body;', "a/b", "x y", ""):
            with self.subTest(key=key), self.assertRaises(OsmQueryError):
                normalize_tag(key, "")
        for value in ('x"];out body;', "a;b", "x\nnode"):
            with self.subTest(value=value), self.assertRaises(OsmQueryError):
                normalize_tag("building", value)


class OsmQueryTests(unittest.TestCase):
    def test_geometry_specific_queries_are_bounded(self) -> None:
        point = build_overpass_query("highway", "bus_stop", "point", SMALL_BBOX)
        line = build_overpass_query("highway", "*", "line", SMALL_BBOX)
        polygon = build_overpass_query("building", "*", "polygon", SMALL_BBOX)
        self.assertIn('node["highway"="bus_stop"]', point)
        self.assertIn('way["highway"]', line)
        self.assertIn('way["building"]', polygon)
        self.assertIn('relation["building"]', polygon)
        for query in (point, line, polygon):
            self.assertIn("geom;", query)
            self.assertNotIn("http", query)
        self.assertIn("out tags geom;", point)
        self.assertIn("out tags geom;", line)
        self.assertIn("out body geom;", polygon)

    def test_invalid_geometry_kind_is_rejected(self) -> None:
        with self.assertRaises(OsmQueryError):
            build_overpass_query("building", "", "raster", SMALL_BBOX)

    def test_extent_limit_is_enforced(self) -> None:
        self.assertEqual(validate_bbox(*SMALL_BBOX), SMALL_BBOX)
        with self.assertRaisesRegex(OsmQueryError, str(int(MAX_BBOX_AREA_KM2))):
            validate_bbox(38.0, 26.0, 40.0, 29.0)

    def test_endpoints_are_pinned_https_mirrors(self) -> None:
        self.assertEqual(len(OVERPASS_ENDPOINTS), 3)
        self.assertTrue(all(url.startswith("https://") for url in OVERPASS_ENDPOINTS))


class OsmPayloadTests(unittest.TestCase):
    def test_payload_schema(self) -> None:
        payload = {"elements": [{"type": "node", "id": 1}]}
        self.assertIs(validate_payload(payload), payload)
        for bad in (None, {}, {"elements": [None]}, {"remark": "timeout", "elements": []}):
            with self.subTest(payload=bad), self.assertRaises(OsmQueryError):
                validate_payload(bad)

    def test_feature_limit(self) -> None:
        with self.assertRaises(OsmQueryError):
            validate_payload({"elements": [{}] * (MAX_FEATURES + 1)})

    def test_compact_tags_is_deterministic_and_bounded(self) -> None:
        first = compact_tags({"name": "İzmir", "building": "yes"})
        second = compact_tags({"building": "yes", "name": "İzmir"})
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first)["name"], "İzmir")
        self.assertLessEqual(len(compact_tags({"x": "y" * 20_000})), 16_000)


if __name__ == "__main__":
    unittest.main()
