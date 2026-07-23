"""SmartModeler JSON and native QGIS .model3 serialization."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

from qgis.PyQt.QtCore import QPointF
from qgis.core import (
    Qgis,
    QgsProcessingModelAlgorithm,
    QgsProcessingModelChildAlgorithm,
    QgsProcessingModelChildParameterSource,
    QgsProcessingModelOutput,
    QgsProcessingModelParameter,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterVectorLayer,
)

from .algorithm_catalog import AlgorithmCatalog
from .graph_model import GraphModel, NodeDefinition, SocketType


class Model3Serializer:
    """Round-trips internal JSON and bridges to QGIS' native model API."""

    FORMAT = "SmartModelerGIS_v2"

    @classmethod
    def export_to_json(cls, graph: GraphModel) -> str:
        nodes_data = []
        for node in graph.nodes.values():
            nodes_data.append(
                {
                    "id": node.node_id,
                    "title": node.title,
                    "category": node.category,
                    "algorithm_id": node.algorithm_id,
                    "description": node.description,
                    "x": node.x,
                    "y": node.y,
                    "parameters": node.parameters,
                    "inputs": {
                        port_id: {
                            "name": port.name,
                            "type": port.socket_type,
                            "default": port.default_value,
                            "required": port.required,
                            "allows_multiple": port.allows_multiple,
                            "description": port.description,
                        }
                        for port_id, port in node.inputs.items()
                    },
                    "outputs": {
                        port_id: {
                            "name": port.name,
                            "type": port.socket_type,
                            "description": port.description,
                        }
                        for port_id, port in node.outputs.items()
                    },
                }
            )
        edges_data = [
            {
                "id": edge.edge_id,
                "start_node": edge.start_node_id,
                "start_port": edge.start_port_id,
                "end_node": edge.end_node_id,
                "end_port": edge.end_port_id,
            }
            for edge in graph.edges.values()
        ]
        return json.dumps(
            {
                "format": cls.FORMAT,
                "qgis_minimum_version": "4.0",
                "name": graph.name,
                "description": graph.description,
                "nodes": nodes_data,
                "edges": edges_data,
            },
            indent=2,
            ensure_ascii=False,
            default=str,
        )

    @classmethod
    def import_from_json(cls, json_str: str) -> Optional[GraphModel]:
        try:
            data = json.loads(json_str)
            if not isinstance(data, dict) or not isinstance(data.get("nodes"), list):
                return None
            graph = GraphModel(str(data.get("name", "Imported workflow")))
            graph.description = str(data.get("description", ""))
            for item in data["nodes"]:
                algorithm_id = str(
                    item.get("algorithm_id") or item.get("parameters", {}).get("alg_id", "")
                )
                node = NodeDefinition(
                    node_id=str(item["id"]),
                    title=str(item.get("title", algorithm_id)),
                    category=str(item.get("category", "General")),
                    algorithm_id=algorithm_id,
                    description=str(item.get("description", "")),
                )
                node.x = float(item.get("x", 0.0))
                node.y = float(item.get("y", 0.0))
                node.parameters = dict(item.get("parameters", {}))
                node.parameters.pop("alg_id", None)
                for port_id, port in item.get("inputs", {}).items():
                    node.add_input(
                        str(port_id),
                        str(port.get("name", port_id)),
                        str(port.get("type", SocketType.ANY)),
                        port.get("default"),
                        bool(port.get("required", False)),
                        bool(port.get("allows_multiple", False)),
                        str(port.get("description", "")),
                    )
                for port_id, port in item.get("outputs", {}).items():
                    node.add_output(
                        str(port_id),
                        str(port.get("name", port_id)),
                        str(port.get("type", SocketType.ANY)),
                        str(port.get("description", "")),
                    )
                graph.add_node(node)
            for item in data.get("edges", []):
                if graph.add_edge(
                    str(item["start_node"]),
                    str(item["start_port"]),
                    str(item["end_node"]),
                    str(item["end_port"]),
                ) is None:
                    return None
            return graph
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    @classmethod
    def export_to_model3(cls, graph: GraphModel, path: str) -> Tuple[bool, str]:
        """Export through QgsProcessingModelAlgorithm, never hand-written XML."""
        model = QgsProcessingModelAlgorithm(graph.name, "SmartModeler GIS", "smartmodeler")
        model.setHelpContent({"ALG_DESC": graph.description})
        child_nodes: Dict[str, QgsProcessingModelChildAlgorithm] = {}

        for node in graph.nodes.values():
            if node.algorithm_id.startswith("smart:"):
                definition = cls._model_parameter_for_node(node)
                component = QgsProcessingModelParameter(node.node_id)
                component.setDescription(node.title)
                component.setPosition(QPointF(node.x, node.y))
                model.addModelParameter(definition, component)
                continue
            if not AlgorithmCatalog.algorithm_exists(node.algorithm_id):
                return False, f"Algorithm is unavailable: {node.algorithm_id}"
            child = QgsProcessingModelChildAlgorithm(node.algorithm_id)
            child.setChildId(node.node_id)
            child.setDescription(node.title)
            child.setPosition(QPointF(node.x, node.y))
            child_nodes[node.node_id] = child

        for node_id, child in child_nodes.items():
            node = graph.nodes[node_id]
            for input_name in node.inputs:
                incoming = [
                    edge
                    for edge in graph.incoming_edges(node_id)
                    if edge.end_port_id == input_name
                ]
                sources = []
                for edge in incoming:
                    source_node = graph.nodes[edge.start_node_id]
                    if source_node.algorithm_id.startswith("smart:"):
                        sources.append(
                            QgsProcessingModelChildParameterSource.fromModelParameter(
                                source_node.node_id
                            )
                        )
                    else:
                        sources.append(
                            QgsProcessingModelChildParameterSource.fromChildOutput(
                                edge.start_node_id, edge.start_port_id
                            )
                        )
                if not sources and input_name in node.parameters:
                    sources = [
                        QgsProcessingModelChildParameterSource.fromStaticValue(
                            node.parameters[input_name]
                        )
                    ]
                if sources:
                    child.addParameterSources(input_name, sources)

            if not any(True for _edge in graph.outgoing_edges(node_id)):
                outputs = {}
                for output_name, port in node.outputs.items():
                    model_output = QgsProcessingModelOutput(output_name, port.name)
                    model_output.setChildId(node_id)
                    model_output.setChildOutputName(output_name)
                    outputs[output_name] = model_output
                child.setModelOutputs(outputs)
            model.addChildAlgorithm(child)

        valid, errors = model.validate()
        if not valid:
            return False, "\n".join(str(error) for error in errors)
        if not model.toFile(path):
            return False, "QGIS could not write the .model3 file."
        return True, ""

    @classmethod
    def import_from_model3(cls, path: str) -> Tuple[Optional[GraphModel], str]:
        model = QgsProcessingModelAlgorithm()
        if not model.fromFile(path):
            return None, "The selected file is not a valid QGIS .model3 model."
        graph = GraphModel(model.name() or Path(path).stem)
        help_content = model.helpContent()
        graph.description = str(help_content.get("ALG_DESC", ""))

        parameter_components = model.parameterComponents()
        for definition in model.parameterDefinitions():
            if definition.flags() & Qgis.ProcessingParameterFlag.IsModelOutput:
                continue
            component = parameter_components.get(definition.name())
            if component is None:
                continue
            socket_type = AlgorithmCatalog.parameter_socket_type(definition)
            if socket_type == SocketType.RASTER:
                algorithm_id = "smart:raster_layer"
            elif socket_type == SocketType.NUMBER:
                algorithm_id = "smart:number"
            else:
                algorithm_id = "smart:input_layer"
            node = AlgorithmCatalog.create_node(
                algorithm_id, definition.name(), definition.description()
            )
            if definition.defaultValue() not in (None, ""):
                key = "VALUE" if algorithm_id == "smart:number" else "LAYER"
                node.parameters[key] = definition.defaultValue()
            node.x = component.position().x()
            node.y = component.position().y()
            graph.add_node(node)

        pending_edges = []
        for child_id, child in model.childAlgorithms().items():
            try:
                node = AlgorithmCatalog.create_node(
                    child.algorithmId(), child_id, child.description()
                )
            except ValueError as error:
                return None, str(error)
            node.x = child.position().x()
            node.y = child.position().y()
            for input_name, sources in child.parameterSources().items():
                for source in sources:
                    if source.source() == Qgis.ProcessingModelChildParameterSource.StaticValue:
                        node.parameters[input_name] = source.staticValue()
                    elif source.source() == Qgis.ProcessingModelChildParameterSource.ModelParameter:
                        pending_edges.append(
                            (source.parameterName(), "OUTPUT", child_id, input_name)
                        )
                    elif source.source() == Qgis.ProcessingModelChildParameterSource.ChildOutput:
                        pending_edges.append(
                            (
                                source.outputChildId(),
                                source.outputName(),
                                child_id,
                                input_name,
                            )
                        )
            graph.add_node(node)
        for edge in pending_edges:
            if graph.add_edge(*edge) is None:
                return None, f"Invalid connection in .model3 file: {graph.last_error}"
        return graph, ""

    @staticmethod
    def _model_parameter_for_node(node: NodeDefinition):
        default = node.parameters.get(
            "VALUE" if node.algorithm_id in ("smart:number", "smart:slider") else "LAYER"
        )
        if node.algorithm_id == "smart:raster_layer":
            return QgsProcessingParameterRasterLayer(
                node.node_id, node.title, defaultValue=default, optional=False
            )
        if node.algorithm_id in ("smart:number", "smart:slider"):
            return QgsProcessingParameterNumber(
                node.node_id, node.title, defaultValue=default, optional=False
            )
        return QgsProcessingParameterVectorLayer(
            node.node_id, node.title, defaultValue=default, optional=False
        )
