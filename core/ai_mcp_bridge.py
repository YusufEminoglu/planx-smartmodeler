"""AI / Model Context Protocol (MCP) Bridge Engine for SmartModeler GIS (QGIS 4)."""
import json
from typing import Dict, List, Any, Optional
from .graph_model import GraphModel, NodeDefinition, SocketType
from .auto_layout import AutoLayoutEngine


class AiMcpBridge:
    """Interprets natural language prompts or MCP tool requests and builds visual DAG graphs."""

    # Built-in prompt patterns for quick rule matching
    PROMPT_PATTERNS = [
        {
            "keywords": ["isochrone", "15-min", "15 min", "walkability"],
            "title": "15-Minute Urban Isochrone Workflow",
            "nodes": [
                {"id": "input", "title": "Point Locations Input", "alg": "smart:input_layer", "cat": "Parameters", "out_type": SocketType.VECTOR},
                {"id": "buf_800", "title": "800m Walk Buffer", "alg": "native:buffer", "cat": "Vector Geometry", "params": {"DISTANCE": 800.0}},
                {"id": "buf_1600", "title": "1600m Walk Buffer", "alg": "native:buffer", "cat": "Vector Geometry", "params": {"DISTANCE": 1600.0}},
                {"id": "zonal", "title": "Zonal Population Stats", "alg": "native:zonalstatisticsfb", "cat": "Raster Analysis"}
            ],
            "edges": [
                ("input", "out_layer", "buf_800", "in_layer"),
                ("input", "out_layer", "buf_1600", "in_layer"),
                ("buf_800", "out_layer", "zonal", "in_layer")
            ]
        },
        {
            "keywords": ["3d", "extrusion", "height", "massing", "building"],
            "title": "3D Massing & Roof Generation",
            "nodes": [
                {"id": "footprints", "title": "Building Footprints Input", "alg": "smart:input_layer", "cat": "Parameters", "out_type": SocketType.VECTOR},
                {"id": "filter", "title": "Filter Height > 10m", "alg": "native:extractbyattribute", "cat": "Vector Selection", "params": {"FIELD": "height", "OPERATOR": 2, "VALUE": "10"}},
                {"id": "extrude", "title": "3D Polygon Extrude", "alg": "native:extrude", "cat": "Vector Geometry", "params": {"DISTANCE": 15.0}}
            ],
            "edges": [
                ("footprints", "out_layer", "filter", "in_layer"),
                ("filter", "out_layer", "extrude", "in_layer")
            ]
        },
        {
            "keywords": ["suitability", "mcda", "slope", "dem", "land"],
            "title": "MCDA Land Suitability Model",
            "nodes": [
                {"id": "dem", "title": "DEM Surface Input", "alg": "smart:raster_layer", "cat": "Parameters", "out_type": SocketType.RASTER},
                {"id": "slope", "title": "Calculate Slope", "alg": "native:slope", "cat": "Raster Terrain"},
                {"id": "reclass", "title": "Reclassify Slope Values", "alg": "native:reclassifybytable", "cat": "Raster Analysis"}
            ],
            "edges": [
                ("dem", "out_layer", "slope", "in_layer"),
                ("slope", "out_layer", "reclass", "in_layer")
            ]
        }
    ]

    @classmethod
    def generate_graph_from_prompt(cls, prompt_text: str) -> GraphModel:
        """Parses natural language prompt text and generates a auto-laid-out GraphModel."""
        text_lower = prompt_text.lower()
        graph = GraphModel()

        matched_pattern = None
        for pattern in cls.PROMPT_PATTERNS:
            if any(kw in text_lower for kw in pattern["keywords"]):
                matched_pattern = pattern
                break

        if matched_pattern:
            node_map: Dict[str, NodeDefinition] = {}
            for n_info in matched_pattern["nodes"]:
                node = NodeDefinition(node_id=n_info["id"], title=n_info["title"], category=n_info["cat"])
                node.parameters["alg_id"] = n_info["alg"]
                if "params" in n_info:
                    node.parameters.update(n_info["params"])

                in_type = SocketType.VECTOR if n_info.get("out_type") != SocketType.RASTER else SocketType.RASTER
                node.add_input("in_layer", "Input", in_type)
                node.add_output("out_layer", "Output", n_info.get("out_type", SocketType.VECTOR))

                graph.add_node(node)
                node_map[n_info["id"]] = node

            for start_id, start_p, end_id, end_p in matched_pattern["edges"]:
                graph.add_edge(start_id, start_p, end_id, end_p)

        else:
            # General fallback template based on prompt keywords
            n1 = NodeDefinition(title="Vector Input", category="Parameters")
            n1.parameters["alg_id"] = "smart:input_layer"
            n1.add_output("out_layer", "Output", SocketType.VECTOR)
            graph.add_node(n1)

            n2 = NodeDefinition(title=f"AI Tool: {prompt_text[:20]}...", category="Vector Geometry")
            n2.parameters["alg_id"] = "native:buffer"
            n2.add_input("in_layer", "Input", SocketType.VECTOR)
            n2.add_output("out_layer", "Output", SocketType.VECTOR)
            graph.add_node(n2)

            graph.add_edge(n1.node_id, "out_layer", n2.node_id, "in_layer")

        # Automatically apply Sugiyama layout algorithm
        AutoLayoutEngine.apply_layout(graph)
        return graph

    @classmethod
    def get_mcp_tool_schema(cls) -> Dict[str, Any]:
        """Returns MCP tool schema definitions for integration with LLM AI agents."""
        return {
            "name": "generate_qgis_model",
            "description": "Generates a visual QGIS 4 graphical model DAG graph from natural language specifications.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "Spatial workflow requirements prompt"},
                    "target_crs": {"type": "string", "description": "Target coordinate reference system (e.g. EPSG:4326)"}
                },
                "required": ["prompt"]
            }
        }
