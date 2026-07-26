from __future__ import annotations

import json
import sys
import types
import unittest


if "qgis.core" not in sys.modules:
    qgis_module = types.ModuleType("qgis")
    core_module = types.ModuleType("qgis.core")
    dummy_names = (
        "Qgis",
        "QgsApplication",
        "QgsFeatureRequest",
        "QgsProcessingParameterBoolean",
        "QgsProcessingParameterCrs",
        "QgsProcessingParameterDefinition",
        "QgsProcessingParameterEnum",
        "QgsProcessingParameterExtent",
        "QgsProcessingParameterFeatureSource",
        "QgsProcessingParameterField",
        "QgsProcessingParameterFile",
        "QgsProcessingParameterMapLayer",
        "QgsProcessingParameterMultipleLayers",
        "QgsProcessingParameterNumber",
        "QgsProcessingParameterRasterDestination",
        "QgsProcessingParameterRasterLayer",
        "QgsProcessingParameterString",
        "QgsProcessingParameterVectorDestination",
        "QgsProcessingParameterVectorLayer",
        "QgsProject",
        "QgsRasterLayer",
        "QgsVectorLayer",
    )
    for name in dummy_names:
        setattr(core_module, name, type(name, (), {}))
    qgis_module.core = core_module
    sys.modules["qgis"] = qgis_module
    sys.modules["qgis.core"] = core_module

from planx_smartmodeler.core.ai_mcp_bridge import AiMcpBridge, AiResponseError
from planx_smartmodeler.core.algorithm_catalog import AlgorithmCatalog
from planx_smartmodeler.core.graph_model import GraphModel, NodeDefinition, SocketType


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

    def test_ai_catalog_blocks_side_effecting_algorithm_ids(self) -> None:
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:filedownloader"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("postgis:executesql"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:fileuploader"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:createdirectory"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:setprojectvariable"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:loadlayer"))
        self.assertFalse(AlgorithmCatalog.ai_algorithm_allowed("native:setlayerstyle"))
        self.assertTrue(AlgorithmCatalog.ai_algorithm_allowed("native:buffer"))

    def test_ai_parameter_literals_reject_paths_uris_and_wrong_types(self) -> None:
        node = NodeDefinition("n1", "Safe", algorithm_id="native:extractbyattribute")
        node.add_input("VALUE", "Value", SocketType.STRING)
        node.add_input("DISTANCE", "Distance", SocketType.NUMBER)
        node.add_input("FLAG", "Flag", SocketType.BOOLEAN)
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
        self.assertFalse(
            AlgorithmCatalog.ai_parameter_value_allowed(node, "FILE", "report.csv")
        )

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


if __name__ == "__main__":
    unittest.main()
