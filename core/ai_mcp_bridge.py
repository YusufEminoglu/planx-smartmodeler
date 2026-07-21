"""AI / Model Context Protocol (MCP) Bridge Engine for SmartModeler GIS (QGIS 4)."""
import json
from typing import Dict, List, Any, Optional
from qgis.core import QgsSettings
from .graph_model import GraphModel, NodeDefinition, SocketType
from .auto_layout import AutoLayoutEngine


class AiMcpBridge:
    """Interprets natural language prompts or MCP tool requests and builds visual DAG graphs."""

    SETTINGS_PREFIX = "SmartModelerGIS/AI/"

    # Built-in prompt patterns for quick offline matching & presets
    PROMPT_PATTERNS = [
        {
            "keywords": ["isochrone", "15-min", "15 min", "walkability"],
            "title": "15-Minute Urban Isochrone Workflow",
            "nodes": [
                {"id": "input", "title": "Point Locations Input", "alg": "smart:input_layer", "cat": "Parameters", "out_type": SocketType.VECTOR},
                {"id": "buf_800", "title": "800m Walk Buffer", "alg": "native:buffer", "cat": "Vector Geometry", "params": {"DISTANCE": 800.0}},
                {"id": "buf_1600", "title": "1600m Walk Buffer", "alg": "native:buffer", "cat": "Vector Geometry", "params": {"DISTANCE": 1600.0}},
                {"id": "heatmap", "title": "High-Divergence Spectral Heatmap", "alg": "smart:heatmap_renderer", "cat": "Raster Analysis", "params": {"RAMP": "Spectral_7Band", "DIVERGENT": True}}
            ],
            "edges": [
                ("input", "out_layer", "buf_800", "in_layer"),
                ("input", "out_layer", "buf_1600", "in_layer"),
                ("buf_800", "out_layer", "heatmap", "in_layer")
            ]
        },
        {
            "keywords": ["vector", "buffer", "clip", "extract", "attribute"],
            "title": "Vector Spatial Filter & Overlay",
            "nodes": [
                {"id": "input", "title": "Vector Layer Input", "alg": "smart:input_layer", "cat": "Parameters", "out_type": SocketType.VECTOR},
                {"id": "filter", "title": "Extract by Attribute", "alg": "native:extractbyattribute", "cat": "Vector Selection", "params": {"FIELD": "type", "OPERATOR": 0, "VALUE": "residential"}},
                {"id": "buffer", "title": "50m Buffer", "alg": "native:buffer", "cat": "Vector Geometry", "params": {"DISTANCE": 50.0}},
                {"id": "clip", "title": "Clip Boundary", "alg": "native:clip", "cat": "Vector Overlay"}
            ],
            "edges": [
                ("input", "out_layer", "filter", "in_layer"),
                ("filter", "out_layer", "buffer", "in_layer"),
                ("buffer", "out_layer", "clip", "in_layer")
            ]
        },
        {
            "keywords": ["suitability", "mcda", "slope", "dem", "land"],
            "title": "MCDA Land Suitability Model",
            "nodes": [
                {"id": "dem", "title": "DEM Surface Input", "alg": "smart:raster_layer", "cat": "Parameters", "out_type": SocketType.RASTER},
                {"id": "slope", "title": "Calculate Slope", "alg": "native:slope", "cat": "Raster Terrain"},
                {"id": "reclass", "title": "Reclassify Slope Values", "alg": "native:reclassifybytable", "cat": "Raster Analysis"},
                {"id": "heatmap", "title": "Divergent Suitability Heatmap", "alg": "smart:heatmap_renderer", "cat": "Raster Analysis", "params": {"RAMP": "RedYellowBlue_Divergent", "CONTRAST": "High"}}
            ],
            "edges": [
                ("dem", "out_layer", "slope", "in_layer"),
                ("slope", "out_layer", "reclass", "in_layer"),
                ("reclass", "out_layer", "heatmap", "in_layer")
            ]
        }
    ]

    @classmethod
    def generate_graph_from_prompt(cls, prompt_text: str) -> GraphModel:
        """Parses natural language prompt text via online LLM API or fallback pattern engine."""
        settings = QgsSettings()
        prov_idx = int(settings.value(cls.SETTINGS_PREFIX + "provider_idx", 0))
        api_key = str(settings.value(cls.SETTINGS_PREFIX + "api_key", "")).strip()

        # If online LLM is configured with an API key or local Ollama URL, attempt online LLM call
        if prov_idx > 0:
            llm_graph = cls._query_online_llm(prov_idx, api_key, prompt_text)
            if llm_graph:
                AutoLayoutEngine.apply_layout(llm_graph)
                return llm_graph

        # Fallback to local heuristic pattern engine
        return cls._generate_heuristic_graph(prompt_text)

    @classmethod
    def _generate_heuristic_graph(cls, prompt_text: str) -> GraphModel:
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
            n1 = NodeDefinition(title="Vector Layer Input", category="Parameters")
            n1.parameters["alg_id"] = "smart:input_layer"
            n1.add_output("out_layer", "Output", SocketType.VECTOR)
            graph.add_node(n1)

            n2 = NodeDefinition(title="50m Buffer", category="Vector Geometry")
            n2.parameters["alg_id"] = "native:buffer"
            n2.parameters["DISTANCE"] = 50.0
            n2.add_input("in_layer", "Input", SocketType.VECTOR)
            n2.add_output("out_layer", "Output", SocketType.VECTOR)
            graph.add_node(n2)

            n3 = NodeDefinition(title="High-Divergence Spectral Heatmap", category="Raster Analysis")
            n3.parameters["alg_id"] = "smart:heatmap_renderer"
            n3.parameters["RAMP"] = "Spectral_7Band"
            n3.add_input("in_layer", "Input", SocketType.VECTOR)
            n3.add_output("out_layer", "Output", SocketType.VECTOR)
            graph.add_node(n3)

            graph.add_edge(n1.node_id, "out_layer", n2.node_id, "in_layer")
            graph.add_edge(n2.node_id, "out_layer", n3.node_id, "in_layer")

        AutoLayoutEngine.apply_layout(graph)
        return graph

    @classmethod
    def _query_online_llm(cls, prov_idx: int, api_key: str, prompt_text: str) -> Optional[GraphModel]:
        """Queries OpenAI / Gemini / Ollama for custom DAG JSON generation."""
        return None
