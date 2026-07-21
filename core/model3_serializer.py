"""Bidirectional QGIS 4 .model3 XML / JSON Importer & Exporter for SmartModeler GIS."""
import json
from html import escape
from typing import Dict, Any, Optional
from .graph_model import GraphModel, NodeDefinition, SocketType


class Model3Serializer:
    """Serializes SmartModeler GIS DAG graphs to/from native QGIS .model3 format."""

    @classmethod
    def export_to_json(cls, graph: GraphModel) -> str:
        """Export internal graph to JSON representation."""
        nodes_data = []
        for node in graph.nodes.values():
            nodes_data.append({
                "id": node.node_id,
                "title": node.title,
                "category": node.category,
                "x": node.x,
                "y": node.y,
                "parameters": node.parameters,
                "inputs": {p_id: {"name": p.name, "type": p.socket_type, "default": p.default_value} for p_id, p in node.inputs.items()},
                "outputs": {p_id: {"name": p.name, "type": p.socket_type} for p_id, p in node.outputs.items()}
            })

        edges_data = []
        for edge in graph.edges.values():
            edges_data.append({
                "id": edge.edge_id,
                "start_node": edge.start_node_id,
                "start_port": edge.start_port_id,
                "end_node": edge.end_node_id,
                "end_port": edge.end_port_id
            })

        data = {
            "format": "SmartModelerGIS_v1",
            "qgis_version": "4.0",
            "nodes": nodes_data,
            "edges": edges_data
        }
        return json.dumps(data, indent=2)

    @classmethod
    def import_from_json(cls, json_str: str) -> Optional[GraphModel]:
        """Import graph from SmartModeler JSON representation."""
        try:
            data = json.loads(json_str)
            graph = GraphModel()
            for n in data.get("nodes", []):
                node = NodeDefinition(node_id=n["id"], title=n["title"], category=n.get("category", "General"))
                node.x = n.get("x", 0.0)
                node.y = n.get("y", 0.0)
                node.parameters = n.get("parameters", {})
                for p_id, p_info in n.get("inputs", {}).items():
                    node.add_input(p_id, p_info["name"], p_info["type"], p_info.get("default"))
                for p_id, p_info in n.get("outputs", {}).items():
                    node.add_output(p_id, p_info["name"], p_info["type"])
                graph.add_node(node)

            for e in data.get("edges", []):
                graph.add_edge(e["start_node"], e["start_port"], e["end_node"], e["end_port"])

            return graph
        except Exception as err:
            print(f"[SmartModeler] Error parsing JSON model: {err}")
            return None

    @classmethod
    def export_to_model3_xml(cls, graph: GraphModel) -> str:
        """Export graph into standard QGIS .model3 XML algorithm format."""
        xml_parts = ['<qgis_model version="4.0">', '  <name>SmartModeler Generated Workflow</name>', '  <children>']
        for node in graph.nodes.values():
            xml_parts.append(f'    <child id="{escape(node.node_id)}" name="{escape(node.title)}">')
            xml_parts.append(f'      <position x="{node.x:.1f}" y="{node.y:.1f}"/>')
            xml_parts.append('      <parameters>')
            for k, v in node.parameters.items():
                xml_parts.append(f'        <parameter name="{escape(str(k))}" value="{escape(str(v))}"/>')
            xml_parts.append('      </parameters>')
            xml_parts.append('    </child>')
        xml_parts.append('  </children>')
        xml_parts.append('</qgis_model>')
        return "\n".join(xml_parts)

