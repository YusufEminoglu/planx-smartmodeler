from __future__ import annotations

import json
import unittest

from planx_smartmodeler.tests.qgis_stubs import ensure_qgis_core

ensure_qgis_core()

from planx_smartmodeler.core.ai_mcp_bridge import AiMcpBridge, AiResponseError  # noqa: E402
from planx_smartmodeler.core.algorithm_catalog import AlgorithmCatalog  # noqa: E402
from planx_smartmodeler.core.graph_model import (  # noqa: E402
    GraphModel,
    NodeDefinition,
    SocketType,
)


class AiContractTests(unittest.TestCase):
    def test_empty_safety_response_is_valid(self) -> None:
        payload = {
            "title": "Restricted request",
            "summary": "No executable workflow was created.",
            "nodes": [],
            "edges": [],
            "warnings": ["The request is outside the planner trust boundary."],
        }
        result = AiMcpBridge.parse_response(json.dumps(payload))
        self.assertFalse(result.graph.nodes)

    def test_contract_rejects_extra_fields_and_nonfinite_numbers(self) -> None:
        payload = {
            "title": "Invalid",
            "summary": "Contains an extra field.",
            "nodes": [],
            "edges": [],
            "warnings": [],
            "command": "ignored",
        }
        with self.assertRaises(AiResponseError):
            AiMcpBridge.parse_response(json.dumps(payload))
        with self.assertRaises(AiResponseError):
            AiMcpBridge._validate_parameter_value(float("nan"))

    def test_contract_accepts_bounded_enum_index_lists(self) -> None:
        value_schema = AiMcpBridge.response_schema()["properties"]["nodes"][
            "items"
        ]["properties"]["parameters"]["items"]["properties"]["value"]
        array_schema = value_schema["anyOf"][-1]
        self.assertEqual(
            array_schema["items"]["anyOf"],
            [{"type": "string"}, {"type": "integer"}],
        )
        AiMcpBridge._validate_parameter_value([0, 2, 5])
        AiMcpBridge._validate_parameter_value(["roads", "parks"])
        for invalid in ([True], [1.5], [1_000_000_001], ["x" * 2001]):
            with self.subTest(invalid=invalid):
                with self.assertRaises(AiResponseError):
                    AiMcpBridge._validate_parameter_value(invalid)

    def test_ai_catalog_blocks_side_effecting_algorithm_ids(self) -> None:
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:filedownloader"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("postgis:executesql"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:fileuploader"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:createdirectory"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:setprojectvariable"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:loadlayer"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:setlayerstyle"))
        self.assertTrue(AlgorithmCatalog.ai_algorithm_allowed("native:buffer"))

    def test_workflow_ai_catalog_is_broader_than_agent_run_allowlist(self) -> None:
        from planx_smartmodeler.core.agent.safe_algorithm_policy import default_policy

        self.assertTrue(AlgorithmCatalog.ai_algorithm_allowed("native:randomextract"))
        self.assertTrue(AlgorithmCatalog.ai_algorithm_allowed("native:boundary"))
        self.assertTrue(AlgorithmCatalog.ai_algorithm_allowed("qgis:buffer"))
        self.assertTrue(AlgorithmCatalog.ai_algorithm_allowed("planx:preparenetwork"))
        self.assertTrue(
            AlgorithmCatalog.ai_algorithm_allowed("planx_cartolab:quick_style")
        )
        self.assertTrue(AlgorithmCatalog.ai_algorithm_allowed("planx:serviceareas"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("thirdparty:unreviewed"))
        self.assertIsNotNone(default_policy().record_for("native:randomextract"))
        self.assertIsNone(default_policy().record_for("planx:preparenetwork"))
        self.assertIsNone(default_policy().record_for("planx:serviceareas"))

    def test_ai_parameter_literals_reject_paths_uris_and_wrong_types(self) -> None:
        node = NodeDefinition("n1", "Safe", algorithm_id="native:extractbyattribute")
        node.add_input("VALUE", "Value", SocketType.STRING)
        node.add_input("DISTANCE", "Distance", SocketType.NUMBER)
        node.add_input("FLAG", "Flag", SocketType.BOOLEAN)
        node.add_input("PREDICATE", "Predicate", SocketType.ENUM)
        node.add_input("FILE", "File", SocketType.FILE)
        self.assertTrue(AlgorithmCatalog.ai_parameter_value_allowed(node, "VALUE", "bus_stop"))
        self.assertFalse(
            AlgorithmCatalog.ai_parameter_value_allowed(node, "VALUE", "file:///secret.csv")
        )
        self.assertFalse(
            AlgorithmCatalog.ai_parameter_value_allowed(node, "VALUE", r"C:\secret.csv")
        )
        self.assertTrue(AlgorithmCatalog.ai_parameter_value_allowed(node, "DISTANCE", 12.5))
        self.assertFalse(AlgorithmCatalog.ai_parameter_value_allowed(node, "DISTANCE", True))
        self.assertTrue(AlgorithmCatalog.ai_parameter_value_allowed(node, "FLAG", False))
        self.assertFalse(AlgorithmCatalog.ai_parameter_value_allowed(node, "FLAG", 0))
        self.assertTrue(
            AlgorithmCatalog.ai_parameter_value_allowed(
                node, "PREDICATE", [0, 2]
            )
        )
        self.assertTrue(
            AlgorithmCatalog.ai_parameter_value_allowed(node, "PREDICATE", 0)
        )
        self.assertFalse(
            AlgorithmCatalog.ai_parameter_value_allowed(
                node, "PREDICATE", [True]
            )
        )
        self.assertFalse(
            AlgorithmCatalog.ai_parameter_value_allowed(
                node, "PREDICATE", [-1]
            )
        )
        self.assertFalse(
            AlgorithmCatalog.ai_parameter_value_allowed(node, "FILE", "report.csv")
        )

    def test_rejected_parameter_identifies_only_algorithm_and_port(self) -> None:
        payload = {
            "title": "Invalid numeric input",
            "summary": "Invalid parameter type.",
            "nodes": [
                {
                    "id": "distance",
                    "algorithm_id": "smart:number",
                    "title": "Distance",
                    "parameters": [{"name": "VALUE", "value": "far"}],
                }
            ],
            "edges": [],
            "warnings": [],
        }
        with self.assertRaisesRegex(
            AiResponseError,
            r"smart:number\.VALUE",
        ):
            AiMcpBridge.parse_response(json.dumps(payload))

    def test_current_workflow_context_and_change_summary(self) -> None:
        before = GraphModel("Current")
        source = NodeDefinition("source", "Roads", algorithm_id="smart:input_layer")
        source.parameters["LAYER"] = "roads-id"
        source.add_output("OUTPUT", "Output", SocketType.VECTOR)
        source.x = 125.0
        source.y = 240.0
        before.add_node(source)
        context = AiMcpBridge.workflow_context(before)
        self.assertIn('"id":"source"', context)
        self.assertNotIn("roads-id", context)
        self.assertIn(AiMcpBridge.LOCAL_PARAMETER_MARKER, context)

        after = GraphModel("Current")
        after.add_node(source)
        buffer_node = NodeDefinition("buffer", "Buffer", algorithm_id="native:buffer")
        buffer_node.parameters["DISTANCE"] = 50
        buffer_node.add_input("INPUT", "Input", SocketType.VECTOR, required=True)
        buffer_node.add_output("OUTPUT", "Output", SocketType.VECTOR)
        after.add_node(buffer_node)
        after.add_edge("source", "OUTPUT", "buffer", "INPUT")
        AiMcpBridge.preserve_existing_layout(before, after)
        self.assertEqual(
            (after.nodes["source"].x, after.nodes["source"].y),
            (125.0, 240.0),
        )
        self.assertEqual((buffer_node.x, buffer_node.y), (425.0, 240.0))
        summary = AiMcpBridge.describe_graph_changes(before, after)
        self.assertIn("Added: Buffer", summary)
        self.assertIn("Connections: +1", summary)

    def test_workflow_context_redacts_every_local_parameter_value(self) -> None:
        graph = GraphModel("Private")
        node = NodeDefinition("source", "Source", algorithm_id="smart:input_layer")
        node.parameters.update(
            {
                "LAYER": r"C:\private\source.gpkg",
                "EXPRESSION": "salary > 50000",
                "LIMIT": 50_000,
            }
        )
        graph.add_node(node)

        context = AiMcpBridge.workflow_context(graph)

        self.assertNotIn("private", context)
        self.assertNotIn("salary", context)
        self.assertNotIn("50000", context)
        self.assertEqual(context.count(AiMcpBridge.LOCAL_PARAMETER_MARKER), 3)

    def test_local_parameter_token_requires_matching_baseline(self) -> None:
        payload = {
            "title": "Invalid",
            "summary": "Invalid token use.",
            "nodes": [
                {
                    "id": "new_node",
                    "algorithm_id": "smart:input_layer",
                    "title": "Input",
                    "parameters": [
                        {
                            "name": "LAYER",
                            "value": AiMcpBridge.LOCAL_PARAMETER_MARKER,
                        }
                    ],
                }
            ],
            "edges": [],
            "warnings": [],
        }
        with self.assertRaisesRegex(AiResponseError, "local-value token"):
            AiMcpBridge.parse_response(json.dumps(payload))

    def test_null_parameter_is_an_explicit_unconfigured_value(self) -> None:
        baseline = GraphModel("Baseline")
        source = AlgorithmCatalog.create_node(
            "smart:input_layer", "source", "Source"
        )
        source.parameters["LAYER"] = "private-layer-id"
        baseline.add_node(source)
        payload = {
            "title": "Clear source",
            "summary": "Leave the source for guided setup.",
            "nodes": [
                {
                    "id": "source",
                    "algorithm_id": "smart:input_layer",
                    "title": "Source",
                    "parameters": [{"name": "LAYER", "value": None}],
                }
            ],
            "edges": [],
            "warnings": ["Select the input layer."],
        }
        parsed = AiMcpBridge.parse_response(
            json.dumps(payload), base_graph=baseline
        )
        self.assertEqual(parsed.graph.nodes["source"].parameters["LAYER"], "")


if __name__ == "__main__":
    unittest.main()
