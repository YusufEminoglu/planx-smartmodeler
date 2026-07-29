import json
import unittest

from planx_smartmodeler.core.document_codec import (
    DocumentCodecError,
    GraphDocumentCodec,
)
from planx_smartmodeler.core.graph_model import (
    GraphModel,
    NodeDefinition,
    SocketType,
)


def node_factory(
    algorithm_id: str,
    node_id: str,
    title: str,
    configuration=None,
) -> NodeDefinition:
    node = NodeDefinition(node_id, title, algorithm_id=algorithm_id)
    node.algorithm_configuration = dict(configuration or {})
    if algorithm_id == "smart:number":
        node.parameters["VALUE"] = 0.0
        node.add_output("OUTPUT", "Output", SocketType.NUMBER)
        return node
    if algorithm_id == "native:test":
        node.add_input("INPUT", "Input", SocketType.NUMBER, required=True)
        node.add_input("OPTIONS", "Options", SocketType.ANY)
        node.add_output("OUTPUT", "Output", SocketType.VECTOR)
        return node
    if algorithm_id == "grass:test":
        node.add_input("-z", "Ignore zero cells", SocketType.BOOLEAN)
        node.add_output("OUTPUT", "Output", SocketType.RASTER)
        return node
    raise ValueError("Unavailable algorithm")


class GraphDocumentCodecTests(unittest.TestCase):
    def graph(self) -> GraphModel:
        graph = GraphModel("Typed document")
        source = node_factory("smart:number", "source", "Source")
        source.parameters["VALUE"] = 12.5
        source.model_parameter_definition = {
            "parameter_type": "number",
            "metadata": {"unit": "m"},
        }
        source.model_parameter_required = True
        target = node_factory("native:test", "target", "Target")
        target.parameters["OPTIONS"] = {
            "tuple": ("a", 2),
            "flags": [True, False],
        }
        target.dependencies = ["source"]
        target.dependency_branches["source"] = "OUTPUT"
        target.is_active = False
        target.algorithm_configuration = {
            "mode": "preserve",
            "nested": {"enabled": True},
        }
        graph.add_node(source)
        graph.add_node(target)
        self.assertIsNotNone(
            graph.add_edge("source", "OUTPUT", "target", "INPUT")
        )
        target.parameter_source_order["INPUT"] = [
            {
                "kind": "edge",
                "node_id": "source",
                "output_name": "OUTPUT",
            }
        ]
        target.parameter_source_order["OPTIONS"] = [
            {"kind": "static", "value": target.parameters["OPTIONS"]}
        ]
        graph.outputs["RESULT"] = {
            "node_id": "target",
            "output_name": "OUTPUT",
            "description": "Declared result",
        }
        return graph

    def test_v3_round_trip_preserves_typed_values_dependencies_and_outputs(self):
        encoded = GraphDocumentCodec.encode(self.graph())
        decoded = GraphDocumentCodec.decode(encoded, node_factory)
        self.assertEqual(decoded.nodes["source"].parameters["VALUE"], 12.5)
        self.assertTrue(decoded.nodes["source"].model_parameter_required)
        self.assertEqual(
            decoded.nodes["target"].parameters["OPTIONS"]["tuple"],
            ("a", 2),
        )
        self.assertEqual(decoded.nodes["target"].dependencies, ["source"])
        self.assertEqual(
            decoded.nodes["target"].dependency_branches["source"], "OUTPUT"
        )
        self.assertFalse(decoded.nodes["target"].is_active)
        self.assertEqual(
            decoded.nodes["target"].algorithm_configuration["mode"],
            "preserve",
        )
        self.assertEqual(
            decoded.nodes["target"].parameter_source_order["INPUT"][0]["kind"],
            "edge",
        )
        self.assertEqual(decoded.outputs["RESULT"]["node_id"], "target")
        self.assertNotIn('"inputs"', encoded)
        self.assertNotIn('"outputs": {', encoded)

    def test_round_trip_accepts_command_style_qgis_parameter_names(self):
        graph = GraphModel("Provider flag")
        node = node_factory("grass:test", "grass_node", "GRASS test")
        node.parameters["-z"] = False
        graph.add_node(node)

        decoded = GraphDocumentCodec.decode(
            GraphDocumentCodec.encode(graph),
            node_factory,
        )

        self.assertIn("-z", decoded.nodes["grass_node"].inputs)
        self.assertIs(decoded.nodes["grass_node"].parameters["-z"], False)

    def test_qgis_crs_and_qt_color_defaults_are_portable_text(self):
        class QgsCoordinateReferenceSystem:
            def authid(self):
                return "EPSG:4326"

        class QColor:
            def red(self):
                return 10

            def green(self):
                return 20

            def blue(self):
                return 30

            def alpha(self):
                return 128

        graph = self.graph()
        graph.nodes["target"].parameters["OPTIONS"] = {
            "crs": QgsCoordinateReferenceSystem(),
            "color": QColor(),
        }
        graph.nodes["target"].parameter_source_order["OPTIONS"] = [
            {"kind": "static", "value": graph.nodes["target"].parameters["OPTIONS"]}
        ]

        decoded = GraphDocumentCodec.decode(
            GraphDocumentCodec.encode(graph),
            node_factory,
        )

        self.assertEqual(
            decoded.nodes["target"].parameters["OPTIONS"],
            {"crs": "EPSG:4326", "color": "#800a141e"},
        )

    def test_rejects_unknown_version_and_nonfinite_number(self):
        payload = json.loads(GraphDocumentCodec.encode(self.graph()))
        payload["version"] = 999
        with self.assertRaisesRegex(DocumentCodecError, "version"):
            GraphDocumentCodec.decode(json.dumps(payload), node_factory)
        text = GraphDocumentCodec.encode(self.graph()).replace("12.5", "NaN")
        with self.assertRaisesRegex(DocumentCodecError, "Invalid JSON number"):
            GraphDocumentCodec.decode(text, node_factory)

    def test_rejects_oversized_and_deep_documents(self):
        with self.assertRaisesRegex(DocumentCodecError, "4 MiB"):
            GraphDocumentCodec.decode(" " * (GraphDocumentCodec.MAX_BYTES + 1), node_factory)
        graph = self.graph()
        nested = "leaf"
        for _index in range(GraphDocumentCodec.MAX_VALUE_DEPTH + 2):
            nested = [nested]
        graph.nodes["target"].set_parameter("OPTIONS", nested)
        with self.assertRaisesRegex(DocumentCodecError, "deeply"):
            GraphDocumentCodec.encode(graph)

    def test_rejects_parameter_not_in_live_signature(self):
        payload = json.loads(GraphDocumentCodec.encode(self.graph()))
        payload["nodes"][1]["parameters"]["FORGED"] = 1
        with self.assertRaisesRegex(DocumentCodecError, "live algorithm signature"):
            GraphDocumentCodec.decode(json.dumps(payload), node_factory)

    def test_legacy_ports_cannot_forge_the_live_contract(self):
        payload = {
            "format": GraphDocumentCodec.LEGACY_FORMAT,
            "qgis_minimum_version": "4.0",
            "name": "Legacy",
            "description": "",
            "nodes": [
                {
                    "id": "source",
                    "title": "Source",
                    "category": "Inputs",
                    "algorithm_id": "smart:number",
                    "description": "",
                    "x": 0,
                    "y": 0,
                    "parameters": {"VALUE": 1},
                    "inputs": {},
                    "outputs": {
                        "FORGED": {
                            "name": "Forged",
                            "type": "any",
                            "description": "",
                        }
                    },
                },
                {
                    "id": "target",
                    "title": "Target",
                    "category": "Test",
                    "algorithm_id": "native:test",
                    "description": "",
                    "x": 1,
                    "y": 0,
                    "parameters": {},
                    "inputs": {
                        "FORGED": {
                            "name": "Forged",
                            "type": "any",
                            "required": False,
                        }
                    },
                    "outputs": {},
                },
            ],
            "edges": [
                {
                    "id": "forged",
                    "start_node": "source",
                    "start_port": "FORGED",
                    "end_node": "target",
                    "end_port": "FORGED",
                }
            ],
        }
        with self.assertRaisesRegex(DocumentCodecError, "Invalid document connection"):
            GraphDocumentCodec.decode(json.dumps(payload), node_factory)

    def test_valid_v2_document_migrates_through_the_live_node_factory(self):
        payload = {
            "format": GraphDocumentCodec.LEGACY_FORMAT,
            "qgis_minimum_version": "4.0",
            "name": "Legacy",
            "description": "Migrated",
            "nodes": [
                {
                    "id": "source",
                    "title": "Source",
                    "category": "Inputs",
                    "algorithm_id": "smart:number",
                    "description": "",
                    "x": 10,
                    "y": 20,
                    "parameters": {"VALUE": 7},
                    "inputs": {},
                    "outputs": {
                        "OUTPUT": {
                            "name": "Output",
                            "type": "number",
                            "description": "",
                        }
                    },
                }
            ],
            "edges": [],
        }
        graph = GraphDocumentCodec.decode(json.dumps(payload), node_factory)
        self.assertEqual(graph.name, "Legacy")
        self.assertEqual(graph.nodes["source"].parameters["VALUE"], 7)
        migrated = json.loads(GraphDocumentCodec.encode(graph))
        self.assertEqual(migrated["format"], GraphDocumentCodec.FORMAT)
        self.assertEqual(migrated["version"], GraphDocumentCodec.VERSION)

    def test_missing_dependency_and_invalid_declared_output_fail_closed(self):
        payload = json.loads(GraphDocumentCodec.encode(self.graph()))
        payload["nodes"][1]["dependencies"] = [
            {"child_id": "missing", "conditional_branch": ""}
        ]
        with self.assertRaisesRegex(DocumentCodecError, "missing node"):
            GraphDocumentCodec.decode(json.dumps(payload), node_factory)
        payload = json.loads(GraphDocumentCodec.encode(self.graph()))
        payload["outputs"][0]["output_name"] = "MISSING"
        with self.assertRaisesRegex(DocumentCodecError, "declared workflow output"):
            GraphDocumentCodec.decode(json.dumps(payload), node_factory)

    def test_duplicate_and_extra_json_fields_fail_closed(self):
        encoded = GraphDocumentCodec.encode(self.graph())
        duplicate = encoded.replace(
            '"version": 3,',
            '"version": 3, "version": 3,',
            1,
        )
        with self.assertRaisesRegex(DocumentCodecError, "Duplicate JSON field"):
            GraphDocumentCodec.decode(duplicate, node_factory)
        payload = json.loads(encoded)
        payload["command"] = "ignored"
        with self.assertRaisesRegex(DocumentCodecError, "document fields"):
            GraphDocumentCodec.decode(json.dumps(payload), node_factory)

    def test_parameter_source_order_must_match_literals_and_edges(self):
        payload = json.loads(GraphDocumentCodec.encode(self.graph()))
        payload["nodes"][1]["parameter_source_order"]["INPUT"] = [
            {"kind": "static", "value": "forged"}
        ]
        with self.assertRaisesRegex(DocumentCodecError, "graph connections"):
            GraphDocumentCodec.decode(json.dumps(payload), node_factory)
        payload = json.loads(GraphDocumentCodec.encode(self.graph()))
        payload["nodes"][1]["parameter_source_order"]["OPTIONS"][0][
            "value"
        ] = "different"
        with self.assertRaisesRegex(DocumentCodecError, "stored literals"):
            GraphDocumentCodec.decode(json.dumps(payload), node_factory)

    def test_encode_rejects_dangling_output_and_graph_limit(self):
        graph = self.graph()
        graph.outputs["RESULT"]["output_name"] = "MISSING"
        with self.assertRaisesRegex(DocumentCodecError, "declared workflow output"):
            GraphDocumentCodec.encode(graph)
        oversized = GraphModel("Too many nodes")
        for index in range(GraphDocumentCodec.MAX_NODES + 1):
            oversized.add_node(
                node_factory("smart:number", f"node_{index}", "Node")
            )
        with self.assertRaisesRegex(DocumentCodecError, "graph limits"):
            GraphDocumentCodec.encode(oversized)

    def test_scalar_and_smart_outputs_cannot_be_published(self):
        graph = GraphModel("Invalid result")
        source = node_factory("smart:number", "source", "Source")
        graph.add_node(source)
        graph.outputs_declared = True
        graph.outputs["RESULT"] = {
            "node_id": "source",
            "output_name": "OUTPUT",
            "description": "",
        }
        with self.assertRaisesRegex(DocumentCodecError, "publishable"):
            GraphDocumentCodec.encode(graph)

        payload = json.loads(GraphDocumentCodec.encode(self.graph()))
        payload["outputs"][0]["node_id"] = "source"
        with self.assertRaisesRegex(DocumentCodecError, "publishable"):
            GraphDocumentCodec.decode(json.dumps(payload), node_factory)

    def test_legacy_malformed_parameter_container_returns_a_codec_error(self):
        payload = {
            "format": GraphDocumentCodec.LEGACY_FORMAT,
            "name": "Legacy",
            "description": "",
            "nodes": [{"id": "node", "parameters": []}],
            "edges": [],
        }
        with self.assertRaisesRegex(DocumentCodecError, "parameters"):
            GraphDocumentCodec.decode(json.dumps(payload), node_factory)


if __name__ == "__main__":
    unittest.main()
