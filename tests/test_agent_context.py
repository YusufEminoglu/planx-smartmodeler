from __future__ import annotations

import itertools
import unittest

from planx_smartmodeler.core.agent.context import (
    FieldSummary,
    LayerSummary,
    MAX_FIELD_VALUES,
    MAX_LIST_ITEMS,
    ModelNodeSummary,
    PluginSummary,
    bound_list,
    bound_text,
    build_field_values,
    build_layer_description,
    build_layer_list,
    build_model_summary,
    build_plugin_list,
    build_project_summary,
)


class BoundTextTests(unittest.TestCase):
    def test_truncates_long_text(self) -> None:
        self.assertEqual(bound_text("x" * 300, 200), "x" * 200)

    def test_none_becomes_empty_string(self) -> None:
        self.assertEqual(bound_text(None, 10), "")

    def test_is_deterministic(self) -> None:
        self.assertEqual(bound_text("abc", 2), bound_text("abc", 2))


class BoundListTests(unittest.TestCase):
    def test_truncation_flag_is_explicit(self) -> None:
        items, truncated = bound_list(range(10), 5)
        self.assertEqual(items, [0, 1, 2, 3, 4])
        self.assertTrue(truncated)

    def test_no_truncation_when_within_limit(self) -> None:
        items, truncated = bound_list([1, 2], 5)
        self.assertEqual(items, [1, 2])
        self.assertFalse(truncated)

    def test_limit_is_hard_clamped(self) -> None:
        items, truncated = bound_list(range(500), 10_000)
        self.assertEqual(len(items), MAX_LIST_ITEMS)
        self.assertTrue(truncated)

    def test_negative_limit_is_clamped_to_zero(self) -> None:
        items, truncated = bound_list([1, 2, 3], -5)
        self.assertEqual(items, [])
        self.assertTrue(truncated)


class BoundListLazinessTests(unittest.TestCase):
    """Regression coverage for required finding 6: bound_list() must consume
    at most ``limit + 1`` items from its input iterator, never the full
    iterable, so it is safe to pass a large or unbounded source directly."""

    class _GuardedIterator:
        """Raises if pulled more than ``allowed_calls`` times."""

        def __init__(self, allowed_calls: int) -> None:
            self._remaining = allowed_calls

        def __iter__(self):
            return self

        def __next__(self):
            if self._remaining <= 0:
                raise AssertionError("bound_list consumed beyond limit + 1 items.")
            self._remaining -= 1
            return self._remaining

    def test_never_consumes_beyond_limit_plus_one(self) -> None:
        # Exactly limit+1 pulls are allowed: islice consumes `limit`, then one
        # extra next() call determines the truncation flag.
        guarded = self._GuardedIterator(allowed_calls=6)
        items, truncated = bound_list(guarded, 5)
        self.assertEqual(len(items), 5)
        self.assertTrue(truncated)

    def test_never_fully_consumes_an_infinite_iterator(self) -> None:
        infinite = itertools.count()
        items, truncated = bound_list(infinite, 5)
        self.assertEqual(items, [0, 1, 2, 3, 4])
        self.assertTrue(truncated)
        # Only the limit+1'th item (5) was peeked and discarded; the
        # iterator's next value proves nothing beyond that was consumed.
        self.assertEqual(next(infinite), 6)

    def test_deterministic_order_and_truncation_are_preserved(self) -> None:
        items, truncated = bound_list(iter(range(3)), 10)
        self.assertEqual(items, [0, 1, 2])
        self.assertFalse(truncated)


class ProjectSummaryTests(unittest.TestCase):
    def test_only_allowed_keys_are_present(self) -> None:
        summary = build_project_summary("My Project", "EPSG:4326", 3)
        self.assertEqual(set(summary), {"title", "crs", "layer_count"})
        self.assertEqual(summary["title"], "My Project")
        self.assertEqual(summary["layer_count"], 3)

    def test_never_accepts_a_path_parameter(self) -> None:
        # The builder signature has no path/URI argument at all: this is a
        # structural guarantee, not a runtime filter.
        import inspect

        parameters = inspect.signature(build_project_summary).parameters
        self.assertNotIn("path", parameters)
        self.assertNotIn("uri", parameters)


class LayerSummaryTests(unittest.TestCase):
    def test_layer_summary_has_no_forbidden_fields(self) -> None:
        layer = LayerSummary(
            layer_id="layer1",
            name="Roads",
            kind="vector",
            geometry_type="LineGeometry",
            crs="EPSG:4326",
            visible=True,
            provider_key="ogr",
        )
        data = layer.to_dict()
        forbidden = {"source", "uri", "path", "feature", "value", "attribute"}
        self.assertFalse(forbidden & set(data))

    def test_build_layer_list_is_bounded_and_truncation_is_explicit(self) -> None:
        layers = [
            LayerSummary(layer_id=f"l{i}", name=f"Layer {i}", kind="vector")
            for i in range(5)
        ]
        result = build_layer_list(layers, limit=2)
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["layers"]), 2)

    def test_build_layer_description_includes_only_name_and_type_for_fields(self) -> None:
        layer = LayerSummary(layer_id="l1", name="Parcels", kind="vector")
        fields = [FieldSummary("owner_name", "String"), FieldSummary("area", "Real")]
        result = build_layer_description(layer, fields, limit=10)
        for field_dict in result["fields"]:
            self.assertEqual(set(field_dict), {"name", "field_type"})
        self.assertFalse(result["fields_truncated"])


class PluginListTests(unittest.TestCase):
    def test_build_plugin_list_bounds_and_shape(self) -> None:
        plugins = [
            PluginSummary(
                package_name=f"pkg{i}",
                display_name=f"Plugin {i}",
                version="1.0.0",
                enabled=bool(i % 2),
                has_processing_provider=False,
            )
            for i in range(3)
        ]
        result = build_plugin_list(plugins, limit=2)
        self.assertEqual(result["count"], 2)
        self.assertTrue(result["truncated"])
        for entry in result["plugins"]:
            self.assertEqual(
                set(entry),
                {"package_name", "display_name", "version", "enabled", "has_processing_provider"},
            )


class ModelSummaryTests(unittest.TestCase):
    def test_no_graph_reports_unavailable(self) -> None:
        result = build_model_summary(False)
        self.assertEqual(result, {"available": False})

    def test_empty_graph_is_available_with_zero_counts(self) -> None:
        result = build_model_summary(True, "Empty workflow", (), 0, ())
        self.assertTrue(result["available"])
        self.assertEqual(result["node_count"], 0)
        self.assertEqual(result["edge_count"], 0)
        self.assertEqual(result["nodes"], [])

    def test_populated_graph_reports_bounded_nodes_and_issues(self) -> None:
        nodes = [ModelNodeSummary("n1", "Buffer", "native:buffer")]
        result = build_model_summary(True, "Workflow", nodes, 1, ["error: bad input"], limit=10)
        self.assertEqual(result["node_count"], 1)
        self.assertEqual(result["edge_count"], 1)
        self.assertEqual(result["validation_issues"], ["error: bad input"])
        self.assertFalse(result["validation_issues_truncated"])

    def test_truncation_is_deterministic_across_calls(self) -> None:
        nodes = [ModelNodeSummary(f"n{i}", f"Node {i}", "native:buffer") for i in range(10)]
        first = build_model_summary(True, "W", nodes, 0, (), limit=3)
        second = build_model_summary(True, "W", nodes, 0, (), limit=3)
        self.assertEqual(first, second)
        self.assertTrue(first["nodes_truncated"])


class FieldValuesTests(unittest.TestCase):
    def test_values_are_ordered_by_count_and_carry_the_totals(self) -> None:
        result = build_field_values(
            "layer_1",
            "highway",
            [("crossing", 1), ("bus_stop", 3)],
            feature_count=4,
            scanned=4,
            complete=True,
        )
        self.assertEqual(
            result["values"],
            [{"value": "bus_stop", "count": 3}, {"value": "crossing", "count": 1}],
        )
        self.assertEqual(result["feature_count"], 4)
        self.assertEqual(result["features_scanned"], 4)
        self.assertTrue(result["count_is_complete"])
        self.assertFalse(result["values_truncated"])

    def test_a_partial_scan_never_claims_to_be_complete(self) -> None:
        result = build_field_values(
            "layer_1", "highway", [("a", 5)], feature_count=900, scanned=100, complete=False
        )
        self.assertFalse(result["count_is_complete"])
        self.assertEqual(result["features_scanned"], 100)

    def test_distinct_values_are_bounded_and_flagged(self) -> None:
        pairs = [(f"value_{index}", index + 1) for index in range(MAX_FIELD_VALUES + 40)]
        result = build_field_values(
            "layer_1", "field", pairs, feature_count=1, scanned=1, complete=True
        )
        self.assertEqual(len(result["values"]), MAX_FIELD_VALUES)
        self.assertTrue(result["values_truncated"])

    def test_a_requested_limit_can_only_narrow_the_bound(self) -> None:
        pairs = [(f"value_{index}", 1) for index in range(MAX_FIELD_VALUES + 10)]
        result = build_field_values(
            "layer_1",
            "field",
            pairs,
            feature_count=1,
            scanned=1,
            complete=True,
            limit=MAX_FIELD_VALUES + 1000,
        )
        self.assertEqual(len(result["values"]), MAX_FIELD_VALUES)

    def test_ordering_is_deterministic_for_equal_counts(self) -> None:
        pairs = [("b", 2), ("a", 2), ("c", 2)]
        first = build_field_values("l", "f", pairs, 6, 6, True)
        second = build_field_values("l", "f", list(reversed(pairs)), 6, 6, True)
        self.assertEqual(first, second)
        self.assertEqual([item["value"] for item in first["values"]], ["a", "b", "c"])


class LayerDescriptionFeatureCountTests(unittest.TestCase):
    def test_a_feature_count_is_included_when_known(self) -> None:
        layer = LayerSummary("id", "Roads", "vector", "Line", "EPSG:4326", True, "ogr")
        result = build_layer_description(layer, (), feature_count=812)
        self.assertEqual(result["feature_count"], 812)

    def test_an_unknown_or_negative_count_is_omitted_not_guessed(self) -> None:
        layer = LayerSummary("id", "Roads", "vector", "Line", "EPSG:4326", True, "ogr")
        self.assertNotIn("feature_count", build_layer_description(layer, ()))
        self.assertNotIn(
            "feature_count", build_layer_description(layer, (), feature_count=-1)
        )


if __name__ == "__main__":
    unittest.main()
