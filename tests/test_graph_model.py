from __future__ import annotations

import unittest

from planx_smartmodeler.core.graph_model import (
    GraphModel,
    NodeDefinition,
    SocketType,
)


def node(node_id: str, input_type: str | None, output_type: str | None) -> NodeDefinition:
    item = NodeDefinition(node_id=node_id, title=node_id, algorithm_id=f"test:{node_id}")
    if input_type:
        item.add_input("INPUT", "Input", input_type)
    if output_type:
        item.add_output("OUTPUT", "Output", output_type)
    return item


class GraphModelTests(unittest.TestCase):
    def test_topological_order_and_cycle_rejection(self) -> None:
        graph = GraphModel()
        for item in (
            node("source", None, SocketType.VECTOR),
            node("buffer", SocketType.VECTOR, SocketType.VECTOR),
            node("sink", SocketType.VECTOR, None),
        ):
            graph.add_node(item)
        self.assertIsNotNone(graph.add_edge("source", "OUTPUT", "buffer", "INPUT"))
        self.assertIsNotNone(graph.add_edge("buffer", "OUTPUT", "sink", "INPUT"))
        self.assertEqual(
            [item.node_id for item in graph.get_topological_order()],
            ["source", "buffer", "sink"],
        )
        sink = graph.nodes["sink"]
        sink.add_output("OUTPUT", "Output", SocketType.VECTOR)
        graph.nodes["source"].add_input("INPUT", "Input", SocketType.VECTOR)
        self.assertIsNone(graph.add_edge("sink", "OUTPUT", "source", "INPUT"))
        self.assertIn("cycle", graph.last_error.lower())

    def test_type_and_single_input_validation(self) -> None:
        graph = GraphModel()
        graph.add_node(node("vector", None, SocketType.VECTOR))
        graph.add_node(node("raster", None, SocketType.RASTER))
        graph.add_node(node("target", SocketType.VECTOR, None))
        self.assertIsNone(graph.add_edge("raster", "OUTPUT", "target", "INPUT"))
        self.assertIn("incompatible", graph.last_error.lower())
        self.assertIsNotNone(graph.add_edge("vector", "OUTPUT", "target", "INPUT"))
        other = node("other", None, SocketType.VECTOR)
        graph.add_node(other)
        self.assertIsNone(graph.add_edge("other", "OUTPUT", "target", "INPUT"))
        self.assertIn("already", graph.last_error.lower())

    def test_remove_node_removes_attached_edges(self) -> None:
        graph = GraphModel()
        graph.add_node(node("source", None, SocketType.NUMBER))
        graph.add_node(node("target", SocketType.NUMBER, None))
        graph.add_edge("source", "OUTPUT", "target", "INPUT")
        graph.remove_node("source")
        self.assertFalse(graph.edges)
        self.assertFalse(graph.nodes["target"].inputs["INPUT"].is_connected())

    def test_smart_layer_input_must_be_configured(self) -> None:
        graph = GraphModel()
        source = node("source", None, SocketType.VECTOR)
        source.algorithm_id = "smart:input_layer"
        source.parameters["LAYER"] = ""
        graph.add_node(source)
        issues = graph.validate()
        self.assertIn("not configured", issues[0].message)
        self.assertEqual(issues[0].code, "missing_input")
        source.parameters["LAYER"] = "project-layer-id"
        self.assertFalse(graph.validate())

    def test_empty_collections_are_not_configured_but_zero_and_false_are(self) -> None:
        self.assertFalse(GraphModel.value_is_configured([]))
        self.assertFalse(GraphModel.value_is_configured({}))
        self.assertFalse(GraphModel.value_is_configured("   "))
        self.assertTrue(GraphModel.value_is_configured(0))
        self.assertTrue(GraphModel.value_is_configured(False))


if __name__ == "__main__":
    unittest.main()
