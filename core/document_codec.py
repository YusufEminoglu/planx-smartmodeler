"""Bounded, versioned SmartModeler document codec."""
from __future__ import annotations

import json
import math
import re
from typing import Any, Callable, Dict

from .graph_model import GraphModel, GraphValidationError, NodeDefinition


class DocumentCodecError(ValueError):
    """Raised when a SmartModeler document violates the storage contract."""


class GraphDocumentCodec:
    """Encode and decode editable graphs without trusting stored port schemas."""

    FORMAT = "SmartModelerGIS"
    VERSION = 3
    LEGACY_FORMAT = "SmartModelerGIS_v2"
    MAX_BYTES = 4 * 1024 * 1024
    MAX_NODES = 500
    MAX_EDGES = 2_000
    MAX_OUTPUTS = 500
    MAX_PARAMETERS = 300
    MAX_COLLECTION_ITEMS = 2_000
    MAX_VALUE_DEPTH = 10
    MAX_TEXT = 100_000
    ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    TYPE_KEY = "$smartmodeler_type"

    @classmethod
    def encode(cls, graph: GraphModel) -> str:
        if len(graph.nodes) > cls.MAX_NODES or len(graph.edges) > cls.MAX_EDGES:
            raise DocumentCodecError("The workflow exceeds the document graph limits.")
        if len(graph.outputs) > cls.MAX_OUTPUTS:
            raise DocumentCodecError("The workflow declares too many outputs.")
        try:
            graph.get_topological_order()
        except GraphValidationError as error:
            raise DocumentCodecError(str(error)) from error
        cls._validate_source_order(graph)
        for value in graph.outputs.values():
            if not isinstance(value, dict):
                raise DocumentCodecError("A declared workflow output is invalid.")
            node = graph.nodes.get(value.get("node_id"))
            if (
                node is None
                or value.get("output_name") not in node.outputs
            ):
                raise DocumentCodecError("A declared workflow output is invalid.")
        payload = {
            "format": cls.FORMAT,
            "version": cls.VERSION,
            "qgis_minimum_version": "4.0",
            "name": cls._text(graph.name, "workflow name", 300),
            "description": cls._text(
                graph.description, "workflow description", 20_000
            ),
            "nodes": [cls._encode_node(node) for node in graph.nodes.values()],
            "edges": [
                {
                    "start_node": edge.start_node_id,
                    "start_port": edge.start_port_id,
                    "end_node": edge.end_node_id,
                    "end_port": edge.end_port_id,
                }
                for edge in graph.edges.values()
            ],
            "outputs": [
                cls._encode_output(name, value)
                for name, value in graph.outputs.items()
            ],
            "outputs_declared": bool(graph.outputs_declared),
        }
        encoded = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > cls.MAX_BYTES:
            raise DocumentCodecError("The SmartModeler document exceeds 4 MiB.")
        return encoded

    @classmethod
    def decode(
        cls,
        text: str,
        node_factory: Callable[[str, str, str], NodeDefinition],
    ) -> GraphModel:
        if not isinstance(text, str) or len(text.encode("utf-8")) > cls.MAX_BYTES:
            raise DocumentCodecError("The SmartModeler document exceeds 4 MiB.")
        try:
            data = json.loads(
                text,
                parse_constant=lambda value: cls._reject_constant(value),
                object_pairs_hook=cls._unique_object,
            )
        except (json.JSONDecodeError, UnicodeError) as error:
            raise DocumentCodecError("The SmartModeler document is not valid JSON.") from error
        if not isinstance(data, dict):
            raise DocumentCodecError("The SmartModeler document must be an object.")
        legacy = data.get("format") == cls.LEGACY_FORMAT
        if not legacy and (
            data.get("format") != cls.FORMAT
            or data.get("version") != cls.VERSION
        ):
            raise DocumentCodecError("Unsupported SmartModeler document version.")
        if not legacy:
            cls._exact_keys(
                data,
                {
                    "format",
                    "version",
                    "qgis_minimum_version",
                    "name",
                    "description",
                    "nodes",
                    "edges",
                    "outputs",
                    "outputs_declared",
                },
                "document",
            )
            if data.get("qgis_minimum_version") != "4.0":
                raise DocumentCodecError("Invalid QGIS minimum version marker.")
        nodes_data = data.get("nodes")
        edges_data = data.get("edges", [])
        outputs_data = data.get("outputs", [])
        if not isinstance(nodes_data, list) or not isinstance(edges_data, list):
            raise DocumentCodecError("Document nodes and edges must be arrays.")
        if not isinstance(outputs_data, list):
            raise DocumentCodecError("Document outputs must be an array.")
        if len(nodes_data) > cls.MAX_NODES or len(edges_data) > cls.MAX_EDGES:
            raise DocumentCodecError("The workflow exceeds the document graph limits.")
        if len(outputs_data) > cls.MAX_OUTPUTS:
            raise DocumentCodecError("The workflow declares too many outputs.")

        graph = GraphModel(cls._text(data.get("name", "Imported workflow"), "name", 300))
        graph.description = cls._text(
            data.get("description", ""), "description", 20_000
        )
        for item in nodes_data:
            cls._decode_node(graph, item, node_factory, legacy)
        graph._suspend_source_order_invalidation = True
        try:
            for item in edges_data:
                if not isinstance(item, dict):
                    raise DocumentCodecError("Every connection must be an object.")
                if not legacy:
                    cls._exact_keys(
                        item,
                        {"start_node", "start_port", "end_node", "end_port"},
                        "connection",
                    )
                try:
                    values = (
                        cls._identifier(item["start_node"], "start node"),
                        cls._identifier(item["start_port"], "start port"),
                        cls._identifier(item["end_node"], "end node"),
                        cls._identifier(item["end_port"], "end port"),
                    )
                except KeyError as error:
                    raise DocumentCodecError("A connection field is missing.") from error
                if graph.add_edge(*values) is None:
                    raise DocumentCodecError(
                        f"Invalid document connection: {graph.last_error}"
                    )
        finally:
            graph._suspend_source_order_invalidation = False
        cls._validate_source_order(graph)
        cls._decode_outputs(graph, outputs_data)
        declared = data.get("outputs_declared", False)
        if not isinstance(declared, bool):
            raise DocumentCodecError("Document output declaration flag is invalid.")
        graph.outputs_declared = declared
        try:
            graph.get_topological_order()
        except GraphValidationError as error:
            raise DocumentCodecError(str(error)) from error
        return graph

    @classmethod
    def _encode_node(cls, node: NodeDefinition) -> Dict[str, Any]:
        return {
            "id": cls._identifier(node.node_id, "node id"),
            "title": cls._text(node.title, "node title", 300),
            "category": cls._text(node.category, "node category", 300),
            "algorithm_id": cls._identifier(node.algorithm_id, "algorithm id"),
            "description": cls._text(node.description, "node description", 20_000),
            "x": cls._finite_number(node.x, "node x"),
            "y": cls._finite_number(node.y, "node y"),
            "parameters": cls._encode_mapping(node.parameters, "parameters"),
            "model_parameter_definition": cls._encode_mapping(
                node.model_parameter_definition, "model parameter definition"
            ),
            "model_parameter_required": bool(node.model_parameter_required),
            "active": bool(node.is_active),
            "algorithm_configuration": cls._encode_mapping(
                node.algorithm_configuration, "algorithm configuration"
            ),
            "parameter_source_order": cls._encode_source_order(
                node.parameter_source_order
            ),
            "dependencies": [
                {
                    "child_id": cls._identifier(value, "dependency"),
                    "conditional_branch": cls._text(
                        node.dependency_branches.get(value, ""),
                        "dependency branch",
                        300,
                    ),
                }
                for value in node.dependencies
            ],
        }

    @classmethod
    def _decode_node(
        cls,
        graph: GraphModel,
        item: Any,
        node_factory: Callable[[str, str, str], NodeDefinition],
        legacy: bool,
    ) -> None:
        if not isinstance(item, dict):
            raise DocumentCodecError("Every node must be an object.")
        if not legacy:
            cls._exact_keys(
                item,
                {
                    "id",
                    "title",
                    "category",
                    "algorithm_id",
                    "description",
                    "x",
                    "y",
                    "parameters",
                    "model_parameter_definition",
                    "model_parameter_required",
                    "active",
                    "algorithm_configuration",
                    "parameter_source_order",
                    "dependencies",
                },
                "node",
            )
        raw_parameters = item.get("parameters", {})
        if not isinstance(raw_parameters, dict):
            raise DocumentCodecError("Node parameters must be an object.")
        try:
            node_id = cls._identifier(item["id"], "node id")
            algorithm_id = cls._identifier(
                item.get("algorithm_id")
                or raw_parameters.get("alg_id", ""),
                "algorithm id",
            )
            title = cls._text(item.get("title", algorithm_id), "node title", 300)
            node = node_factory(algorithm_id, node_id, title)
        except (KeyError, TypeError, ValueError) as error:
            raise DocumentCodecError("A document node is invalid or unavailable.") from error
        node.title = title
        node.category = cls._text(
            item.get("category", node.category), "node category", 300
        )
        node.description = cls._text(
            item.get("description", node.description), "node description", 20_000
        )
        node.x = cls._finite_number(item.get("x", 0.0), "node x")
        node.y = cls._finite_number(item.get("y", 0.0), "node y")
        parameters = cls._decode_mapping(raw_parameters, "parameters")
        parameters.pop("alg_id", None)
        allowed = set(node.inputs)
        if algorithm_id.startswith("smart:"):
            allowed.update({"LAYER", "VALUE"})
        if any(name not in allowed for name in parameters):
            raise DocumentCodecError(
                "A stored parameter is not part of the live algorithm signature."
            )
        node.parameters = parameters
        definition = item.get("model_parameter_definition", {})
        node.model_parameter_definition = (
            cls._decode_mapping(definition, "model parameter definition")
            if not legacy
            else {}
        )
        required = item.get("model_parameter_required", False)
        node.model_parameter_required = cls._boolean(
            required, "model parameter required flag"
        )
        node.is_active = cls._boolean(
            item.get("active", True), "node active flag"
        )
        configuration = item.get("algorithm_configuration", {})
        node.algorithm_configuration = (
            cls._decode_mapping(configuration, "algorithm configuration")
            if not legacy
            else {}
        )
        source_order = item.get("parameter_source_order", {})
        node.parameter_source_order = (
            cls._decode_source_order(source_order)
            if not legacy
            else {}
        )
        dependencies = item.get("dependencies", [])
        if not isinstance(dependencies, list) or len(dependencies) > cls.MAX_NODES:
            raise DocumentCodecError("Node dependencies are invalid.")
        node.dependencies = []
        node.dependency_branches = {}
        for dependency in dependencies:
            if isinstance(dependency, str) and legacy:
                child_id = cls._identifier(dependency, "dependency")
                branch = ""
            elif isinstance(dependency, dict):
                child_id = cls._identifier(
                    dependency.get("child_id"), "dependency"
                )
                branch = cls._text(
                    dependency.get("conditional_branch", ""),
                    "dependency branch",
                    300,
                )
            else:
                raise DocumentCodecError("Node dependency entries are invalid.")
            if child_id in node.dependencies:
                raise DocumentCodecError("Node dependencies contain a duplicate.")
            node.dependencies.append(child_id)
            node.dependency_branches[child_id] = branch
        graph.add_node(node)

    @classmethod
    def _decode_outputs(cls, graph: GraphModel, outputs: list) -> None:
        for item in outputs:
            if not isinstance(item, dict):
                raise DocumentCodecError("Every workflow output must be an object.")
            cls._exact_keys(
                item,
                {
                    "name",
                    "node_id",
                    "output_name",
                    "description",
                    "mandatory",
                    "default",
                },
                "workflow output",
            )
            name = cls._identifier(item.get("name"), "output name")
            node_id = cls._identifier(item.get("node_id"), "output node")
            output_name = cls._identifier(
                item.get("output_name"), "child output name"
            )
            node = graph.nodes.get(node_id)
            if node is None or output_name not in node.outputs or name in graph.outputs:
                raise DocumentCodecError("A declared workflow output is invalid.")
            graph.outputs[name] = {
                "node_id": node_id,
                "output_name": output_name,
                "description": cls._text(
                    item.get("description", ""), "output description", 500
                ),
                "mandatory": cls._boolean(
                    item.get("mandatory", False), "output mandatory flag"
                ),
                "default": cls._decode_value(item.get("default"), 0),
            }

    @classmethod
    def _encode_output(cls, name: str, value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict):
            raise DocumentCodecError("A declared workflow output is invalid.")
        return {
            "name": cls._identifier(name, "output name"),
            "node_id": cls._identifier(value.get("node_id"), "output node"),
            "output_name": cls._identifier(
                value.get("output_name"), "child output name"
            ),
            "description": cls._text(
                value.get("description", ""), "output description", 500
            ),
            "mandatory": cls._boolean(
                value.get("mandatory", False), "output mandatory flag"
            ),
            "default": cls._encode_value(value.get("default"), 0),
        }

    @classmethod
    def _validate_source_order(cls, graph: GraphModel) -> None:
        for node in graph.nodes.values():
            for input_name, entries in node.parameter_source_order.items():
                port = node.inputs.get(input_name)
                if port is None or not isinstance(entries, list):
                    raise DocumentCodecError("A parameter source order is invalid.")
                if len(entries) > cls.MAX_COLLECTION_ITEMS:
                    raise DocumentCodecError("A parameter source order is too large.")
                expected_edges = {
                    (edge.start_node_id, edge.start_port_id)
                    for edge in graph.incoming_edges(node.node_id)
                    if edge.end_port_id == input_name
                }
                ordered_edges = set()
                static_values = []
                for source in entries:
                    if not isinstance(source, dict):
                        raise DocumentCodecError("A parameter source is invalid.")
                    kind = source.get("kind")
                    if kind == "static" and set(source) == {"kind", "value"}:
                        static_values.append(source["value"])
                    elif kind == "edge" and set(source) == {
                        "kind",
                        "node_id",
                        "output_name",
                    }:
                        identity = (
                            cls._identifier(source["node_id"], "source node"),
                            cls._identifier(
                                source["output_name"], "source output"
                            ),
                        )
                        if identity in ordered_edges:
                            raise DocumentCodecError(
                                "A parameter source edge is duplicated."
                            )
                        ordered_edges.add(identity)
                    else:
                        raise DocumentCodecError("Unknown parameter source kind.")
                if ordered_edges != expected_edges:
                    raise DocumentCodecError(
                        "Parameter source order does not match graph connections."
                    )
                configured = node.parameters.get(input_name)
                configured_values = (
                    configured if isinstance(configured, list) else [configured]
                )
                if static_values and static_values != configured_values:
                    raise DocumentCodecError(
                        "Parameter source order does not match stored literals."
                    )

    @classmethod
    def _encode_source_order(cls, value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict) or len(value) > cls.MAX_PARAMETERS:
            raise DocumentCodecError("Invalid parameter source order.")
        result = {}
        for input_name, entries in value.items():
            name = cls._identifier(input_name, "parameter source input")
            if not isinstance(entries, list):
                raise DocumentCodecError("A parameter source order is invalid.")
            cls._check_collection(entries)
            encoded_entries = []
            for source in entries:
                if not isinstance(source, dict):
                    raise DocumentCodecError("A parameter source is invalid.")
                if source.get("kind") == "static" and set(source) == {
                    "kind",
                    "value",
                }:
                    encoded_entries.append(
                        {
                            "kind": "static",
                            "value": cls._encode_value(source["value"], 0),
                        }
                    )
                elif source.get("kind") == "edge" and set(source) == {
                    "kind",
                    "node_id",
                    "output_name",
                }:
                    encoded_entries.append(
                        {
                            "kind": "edge",
                            "node_id": cls._identifier(
                                source["node_id"], "source node"
                            ),
                            "output_name": cls._identifier(
                                source["output_name"], "source output"
                            ),
                        }
                    )
                else:
                    raise DocumentCodecError("Unknown parameter source kind.")
            result[name] = encoded_entries
        return result

    @classmethod
    def _decode_source_order(cls, value: Any) -> Dict[str, Any]:
        if not isinstance(value, dict) or len(value) > cls.MAX_PARAMETERS:
            raise DocumentCodecError("Invalid parameter source order.")
        result = {}
        for input_name, entries in value.items():
            name = cls._identifier(input_name, "parameter source input")
            if not isinstance(entries, list):
                raise DocumentCodecError("A parameter source order is invalid.")
            cls._check_collection(entries)
            decoded_entries = []
            for source in entries:
                if not isinstance(source, dict):
                    raise DocumentCodecError("A parameter source is invalid.")
                if source.get("kind") == "static" and set(source) == {
                    "kind",
                    "value",
                }:
                    decoded_entries.append(
                        {
                            "kind": "static",
                            "value": cls._decode_value(source["value"], 0),
                        }
                    )
                elif source.get("kind") == "edge" and set(source) == {
                    "kind",
                    "node_id",
                    "output_name",
                }:
                    decoded_entries.append(
                        {
                            "kind": "edge",
                            "node_id": cls._identifier(
                                source["node_id"], "source node"
                            ),
                            "output_name": cls._identifier(
                                source["output_name"], "source output"
                            ),
                        }
                    )
                else:
                    raise DocumentCodecError("Unknown parameter source kind.")
            result[name] = decoded_entries
        return result

    @classmethod
    def _encode_mapping(cls, value: Any, label: str) -> Dict[str, Any]:
        if not isinstance(value, dict) or len(value) > cls.MAX_PARAMETERS:
            raise DocumentCodecError(f"Invalid {label}.")
        result = {}
        for key, item in value.items():
            name = cls._identifier(key, f"{label} key")
            result[name] = cls._encode_value(item, 0)
        return result

    @classmethod
    def _decode_mapping(cls, value: Any, label: str) -> Dict[str, Any]:
        if not isinstance(value, dict) or len(value) > cls.MAX_PARAMETERS:
            raise DocumentCodecError(f"Invalid {label}.")
        result = {}
        for key, item in value.items():
            name = cls._identifier(key, f"{label} key")
            result[name] = cls._decode_value(item, 0)
        return result

    @classmethod
    def _encode_value(cls, value: Any, depth: int) -> Any:
        cls._check_depth(depth)
        if type(value).__name__ == "QVariant":
            is_null = getattr(value, "isNull", None)
            if callable(is_null) and is_null():
                return None
            unwrap = getattr(value, "value", None)
            if callable(unwrap):
                unwrapped = unwrap()
                if unwrapped is not value:
                    return cls._encode_value(unwrapped, depth + 1)
            raise DocumentCodecError("Unsupported Qt variant parameter value.")
        if value is None or isinstance(value, (str, bool, int)):
            if isinstance(value, str):
                return cls._text(value, "parameter text", cls.MAX_TEXT)
            return value
        if isinstance(value, float):
            return cls._finite_number(value, "parameter number")
        if isinstance(value, list):
            cls._check_collection(value)
            return [cls._encode_value(item, depth + 1) for item in value]
        if isinstance(value, tuple):
            cls._check_collection(value)
            return {
                cls.TYPE_KEY: "tuple",
                "items": [cls._encode_value(item, depth + 1) for item in value],
            }
        if isinstance(value, dict):
            cls._check_collection(value)
            if any(not isinstance(key, str) for key in value):
                raise DocumentCodecError("Parameter mapping keys must be text.")
            return {
                cls.TYPE_KEY: "dict",
                "items": [
                    [
                        cls._text(key, "mapping key", 500),
                        cls._encode_value(item, depth + 1),
                    ]
                    for key, item in value.items()
                ],
            }
        raise DocumentCodecError(
            f"Unsupported parameter value type: {type(value).__name__}."
        )

    @classmethod
    def _decode_value(cls, value: Any, depth: int) -> Any:
        cls._check_depth(depth)
        if value is None or isinstance(value, (str, bool, int)):
            if isinstance(value, str):
                return cls._text(value, "parameter text", cls.MAX_TEXT)
            return value
        if isinstance(value, float):
            return cls._finite_number(value, "parameter number")
        if isinstance(value, list):
            cls._check_collection(value)
            return [cls._decode_value(item, depth + 1) for item in value]
        if isinstance(value, dict) and value.get(cls.TYPE_KEY) in ("tuple", "dict"):
            kind = value.get(cls.TYPE_KEY)
            items = value.get("items")
            if not isinstance(items, list):
                raise DocumentCodecError("Invalid typed parameter value.")
            cls._check_collection(items)
            if kind == "tuple":
                return tuple(cls._decode_value(item, depth + 1) for item in items)
            result = {}
            for pair in items:
                if not isinstance(pair, list) or len(pair) != 2:
                    raise DocumentCodecError("Invalid typed mapping value.")
                key = cls._text(pair[0], "mapping key", 500)
                if key in result:
                    raise DocumentCodecError("Duplicate typed mapping key.")
                result[key] = cls._decode_value(pair[1], depth + 1)
            return result
        raise DocumentCodecError("Unsupported encoded parameter value.")

    @classmethod
    def _identifier(cls, value: Any, label: str) -> str:
        if not isinstance(value, str) or not cls.ID_PATTERN.fullmatch(value):
            raise DocumentCodecError(f"Invalid {label}.")
        return value

    @staticmethod
    def _text(value: Any, label: str, maximum: int) -> str:
        if not isinstance(value, str) or len(value) > maximum or "\x00" in value:
            raise DocumentCodecError(f"Invalid {label}.")
        return value

    @staticmethod
    def _finite_number(value: Any, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise DocumentCodecError(f"Invalid {label}.")
        number = float(value)
        if not math.isfinite(number) or abs(number) > 1.0e15:
            raise DocumentCodecError(f"Invalid {label}.")
        return number

    @staticmethod
    def _boolean(value: Any, label: str) -> bool:
        if not isinstance(value, bool):
            raise DocumentCodecError(f"Invalid {label}.")
        return value

    @staticmethod
    def _unique_object(pairs: list) -> Dict[str, Any]:
        result = {}
        for key, value in pairs:
            if key in result:
                raise DocumentCodecError(f"Duplicate JSON field: {key}.")
            result[key] = value
        return result

    @staticmethod
    def _exact_keys(value: Dict[str, Any], expected: set, label: str) -> None:
        if set(value) != expected:
            raise DocumentCodecError(f"Invalid {label} fields.")

    @classmethod
    def _check_collection(cls, value: Any) -> None:
        if len(value) > cls.MAX_COLLECTION_ITEMS:
            raise DocumentCodecError("A parameter collection exceeds the limit.")

    @classmethod
    def _check_depth(cls, depth: int) -> None:
        if depth > cls.MAX_VALUE_DEPTH:
            raise DocumentCodecError("A parameter value is nested too deeply.")

    @staticmethod
    def _reject_constant(value: str) -> None:
        raise DocumentCodecError(f"Invalid JSON number: {value}.")
