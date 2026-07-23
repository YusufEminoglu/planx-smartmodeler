"""Validated AI planning bridge for SmartModeler GIS."""
from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Tuple

from .algorithm_catalog import AlgorithmCatalog
from .auto_layout import AutoLayoutEngine
from .graph_model import GraphModel, GraphValidationError, NodeDefinition


class AiResponseError(ValueError):
    """Raised when a provider response violates the graph contract."""


@dataclass
class AiGraphResult:
    graph: GraphModel
    summary: str
    warnings: List[str]


class AiMcpBridge:
    """Converts offline templates or untrusted provider JSON into a safe graph."""

    MAX_NODES = 80
    MAX_EDGES = 240
    ID_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")

    @classmethod
    def response_schema(cls) -> Dict[str, Any]:
        value_schema: Dict[str, Any] = {
            "anyOf": [
                {"type": "string"},
                {"type": "number"},
                {"type": "integer"},
                {"type": "boolean"},
                {"type": "null"},
                {"type": "array", "items": {"type": "string"}},
            ]
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "title": {"type": "string"},
                "summary": {"type": "string"},
                "nodes": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "algorithm_id": {"type": "string"},
                            "title": {"type": "string"},
                            "parameters": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "name": {"type": "string"},
                                        "value": value_schema,
                                    },
                                    "required": ["name", "value"],
                                },
                            },
                        },
                        "required": ["id", "algorithm_id", "title", "parameters"],
                    },
                },
                "edges": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "from_node": {"type": "string"},
                            "from_output": {"type": "string"},
                            "to_node": {"type": "string"},
                            "to_input": {"type": "string"},
                        },
                        "required": ["from_node", "from_output", "to_node", "to_input"],
                    },
                },
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "summary", "nodes", "edges", "warnings"],
        }

    @classmethod
    def workflow_context(cls, graph: GraphModel) -> str:
        """Serialize the editable baseline in the same shape expected from AI."""
        payload = {
            "title": graph.name,
            "summary": graph.description or "Current SmartModeler workflow.",
            "nodes": [
                {
                    "id": node.node_id,
                    "algorithm_id": node.algorithm_id,
                    "title": node.title,
                    "parameters": [
                        {
                            "name": name,
                            "value": cls._contract_value(value),
                        }
                        for name, value in sorted(node.parameters.items())
                    ],
                }
                for node in graph.nodes.values()
            ],
            "edges": [
                {
                    "from_node": edge.start_node_id,
                    "from_output": edge.start_port_id,
                    "to_node": edge.end_node_id,
                    "to_input": edge.end_port_id,
                }
                for edge in graph.edges.values()
            ],
            "warnings": [],
        }
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def describe_graph_changes(cls, before: GraphModel, after: GraphModel) -> str:
        """Return a concise, user-reviewable summary of a proposed full graph."""
        before_ids = set(before.nodes)
        after_ids = set(after.nodes)
        added = sorted(after_ids - before_ids)
        removed = sorted(before_ids - after_ids)
        updated = sorted(
            node_id
            for node_id in before_ids & after_ids
            if cls._node_signature(before.nodes[node_id])
            != cls._node_signature(after.nodes[node_id])
        )
        before_edges = cls._edge_signatures(before)
        after_edges = cls._edge_signatures(after)
        lines = []
        for label, ids, graph in (
            ("Added", added, after),
            ("Removed", removed, before),
            ("Updated", updated, after),
        ):
            if ids:
                names = ", ".join(graph.nodes[node_id].title for node_id in ids[:8])
                suffix = f" (+{len(ids) - 8} more)" if len(ids) > 8 else ""
                lines.append(f"{label}: {names}{suffix}")
        added_edges = len(after_edges - before_edges)
        removed_edges = len(before_edges - after_edges)
        if added_edges or removed_edges:
            lines.append(
                f"Connections: +{added_edges} added, -{removed_edges} removed"
            )
        return "\n".join(lines) or "No graph changes were proposed."

    @staticmethod
    def preserve_existing_layout(before: GraphModel, after: GraphModel) -> None:
        """Keep established node positions and place only newly proposed nodes."""
        existing_ids = set(before.nodes) & set(after.nodes)
        for node_id in existing_ids:
            after.nodes[node_id].x = before.nodes[node_id].x
            after.nodes[node_id].y = before.nodes[node_id].y

        new_ids = set(after.nodes) - set(before.nodes)
        if not new_ids:
            return
        right_edge = max(
            (node.x for node in before.nodes.values()),
            default=0.0,
        )
        fallback_row = 0
        for node in after.get_topological_order():
            if node.node_id not in new_ids:
                continue
            parents = [
                after.nodes[edge.start_node_id]
                for edge in after.incoming_edges(node.node_id)
                if edge.start_node_id in after.nodes
            ]
            if parents:
                node.x = max(parent.x for parent in parents) + 300.0
                node.y = sum(parent.y for parent in parents) / len(parents)
                right_edge = max(right_edge, node.x)
            else:
                node.x = right_edge + 300.0
                node.y = fallback_row * 180.0
                right_edge = node.x
                fallback_row += 1

    @staticmethod
    def _contract_value(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, (list, tuple)):
            return [str(item) for item in value[:200]]
        return str(value)

    @classmethod
    def _node_signature(cls, node: NodeDefinition) -> str:
        values = {
            name: cls._contract_value(value)
            for name, value in sorted(node.parameters.items())
        }
        return json.dumps(
            [node.algorithm_id, node.title, values],
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    @staticmethod
    def _edge_signatures(graph: GraphModel) -> set[tuple[str, str, str, str]]:
        return {
            (
                edge.start_node_id,
                edge.start_port_id,
                edge.end_node_id,
                edge.end_port_id,
            )
            for edge in graph.edges.values()
        }

    @classmethod
    def generate_offline(
        cls, prompt_text: str, base_graph: GraphModel | None = None
    ) -> AiGraphResult:
        """Build a conservative executable starter without a network request."""
        if base_graph is not None and base_graph.nodes:
            return cls._improve_offline(prompt_text, base_graph)
        text = prompt_text.lower()
        graph = GraphModel("Offline starter")
        warnings = [
            "Offline mode created a conservative starter. Review inputs and parameters before running."
        ]

        if any(term in text for term in ("dem", "slope", "terrain", "raster")):
            input_node = AlgorithmCatalog.create_node("smart:raster_layer", "raster_input")
            graph.add_node(input_node)
            if AlgorithmCatalog.algorithm_exists("native:slope"):
                slope = AlgorithmCatalog.create_node("native:slope", "slope")
                graph.add_node(slope)
                cls._connect_first_compatible(graph, input_node, slope)
                graph.name = "Terrain analysis starter"
            else:
                graph.name = "Raster workflow starter"
        else:
            input_node = AlgorithmCatalog.create_node("smart:input_layer", "vector_input")
            graph.add_node(input_node)
            requested = []
            if "extract" in text or "filter" in text or "select" in text:
                requested.append("native:extractbyexpression")
            if "buffer" in text or "distance" in text or "walk" in text or "isochrone" in text:
                requested.append("native:buffer")
            if "centroid" in text:
                requested.append("native:centroids")
            if "convex" in text or "hull" in text:
                requested.append("native:convexhull")
            previous = input_node
            for index, algorithm_id in enumerate(requested):
                if not AlgorithmCatalog.algorithm_exists(algorithm_id):
                    warnings.append(f"Requested algorithm is not installed: {algorithm_id}")
                    continue
                node = AlgorithmCatalog.create_node(algorithm_id, f"step_{index + 1}")
                graph.add_node(node)
                if not cls._connect_first_compatible(graph, previous, node):
                    warnings.append(f"Could not auto-connect {previous.title} to {node.title}.")
                previous = node
            graph.name = "Vector analysis starter"

        graph.description = "Generated by the offline SmartModeler planner."
        AutoLayoutEngine.apply_layout(graph)
        return AiGraphResult(graph, graph.description, warnings)

    @classmethod
    def _improve_offline(
        cls, prompt_text: str, base_graph: GraphModel
    ) -> AiGraphResult:
        """Apply a small deterministic edit while preserving the current graph."""
        from .model3_serializer import Model3Serializer

        graph = Model3Serializer.import_from_json(
            Model3Serializer.export_to_json(base_graph)
        )
        if graph is None:
            raise AiResponseError("The current workflow could not be copied safely.")
        text = prompt_text.lower()
        warnings = []
        changed = False
        distance = re.search(
            r"(?<!\w)(\d+(?:[.,]\d+)?)\s*(?:m|metre|meter|metres|meters)\b",
            text,
        )
        existing_buffer = next(
            (
                node
                for node in graph.nodes.values()
                if node.algorithm_id == "native:buffer"
            ),
            None,
        )
        if existing_buffer is not None and distance:
            existing_buffer.parameters["DISTANCE"] = float(
                distance.group(1).replace(",", ".")
            )
            existing_buffer.is_dirty = True
            changed = True

        requested = []
        if ("extract" in text or "filter" in text or "select" in text) and not any(
            node.algorithm_id == "native:extractbyexpression"
            for node in graph.nodes.values()
        ):
            requested.append("native:extractbyexpression")
        if (
            "buffer" in text
            and existing_buffer is None
            and not any(node.algorithm_id == "native:buffer" for node in graph.nodes.values())
        ):
            requested.append("native:buffer")
        if "centroid" in text and not any(
            node.algorithm_id == "native:centroids" for node in graph.nodes.values()
        ):
            requested.append("native:centroids")
        if ("convex" in text or "hull" in text) and not any(
            node.algorithm_id == "native:convexhull" for node in graph.nodes.values()
        ):
            requested.append("native:convexhull")

        terminal_nodes = [
            node
            for node in graph.nodes.values()
            if not any(graph.outgoing_edges(node.node_id))
        ]
        previous = terminal_nodes[-1] if terminal_nodes else None
        next_x = max((node.x for node in graph.nodes.values()), default=0.0) + 280.0
        for algorithm_id in requested:
            if not AlgorithmCatalog.algorithm_exists(algorithm_id):
                warnings.append(f"Requested algorithm is not installed: {algorithm_id}")
                continue
            base_id = algorithm_id.split(":", 1)[-1]
            node_id = base_id
            counter = 2
            while node_id in graph.nodes:
                node_id = f"{base_id}_{counter}"
                counter += 1
            node = AlgorithmCatalog.create_node(algorithm_id, node_id=node_id)
            node.x = next_x
            node.y = previous.y if previous is not None else 0.0
            graph.add_node(node)
            if previous is not None and not cls._connect_first_compatible(
                graph, previous, node
            ):
                warnings.append(
                    f"Added {node.title}, but it needs a manual input connection."
                )
            previous = node
            next_x += 280.0
            changed = True

        if not changed:
            warnings.append(
                "Offline mode could not infer a safe edit. Use a connected AI "
                "profile for open-ended workflow revisions."
            )
        graph.description = (
            "Updated iteratively by the offline SmartModeler planner."
            if changed
            else base_graph.description
        )
        return AiGraphResult(
            graph,
            "Updated the current workflow." if changed else "Kept the current workflow.",
            warnings,
        )

    @classmethod
    def _connect_first_compatible(
        cls, graph: GraphModel, start: NodeDefinition, end: NodeDefinition
    ) -> bool:
        for output in start.outputs.values():
            for input_port in end.inputs.values():
                if GraphModel.socket_types_compatible(
                    output.socket_type, input_port.socket_type
                ):
                    return bool(
                        graph.add_edge(
                            start.node_id,
                            output.port_id,
                            end.node_id,
                            input_port.port_id,
                        )
                    )
        return False

    @classmethod
    def parse_response(cls, response_text: str) -> AiGraphResult:
        data = cls._decode_json(response_text)
        if not isinstance(data, dict):
            raise AiResponseError("AI response must be one JSON object.")
        cls._require_exact_keys(
            data,
            {"title", "summary", "nodes", "edges", "warnings"},
            "response",
        )
        title = cls._bounded_text(data.get("title"), "title", 160)
        summary = cls._bounded_text(data.get("summary"), "summary", 1000)
        nodes_data = data.get("nodes")
        edges_data = data.get("edges")
        warnings_data = data.get("warnings")
        if not isinstance(nodes_data, list) or not isinstance(edges_data, list):
            raise AiResponseError("AI response must contain node and edge arrays.")
        if not isinstance(warnings_data, list):
            raise AiResponseError("AI response warnings must be an array.")
        if len(nodes_data) > cls.MAX_NODES:
            raise AiResponseError(f"Graph exceeds the {cls.MAX_NODES}-node safety limit.")
        if len(edges_data) > cls.MAX_EDGES:
            raise AiResponseError(f"Graph exceeds the {cls.MAX_EDGES}-edge safety limit.")

        graph = GraphModel(title)
        graph.description = summary
        for item in nodes_data:
            if not isinstance(item, dict):
                raise AiResponseError("Every node must be an object.")
            cls._require_exact_keys(
                item,
                {"id", "algorithm_id", "title", "parameters"},
                "node",
            )
            node_id = cls._bounded_text(item.get("id"), "node id", 64)
            if not cls.ID_PATTERN.fullmatch(node_id):
                raise AiResponseError(f"Invalid node id: {node_id!r}")
            algorithm_id = cls._bounded_text(
                item.get("algorithm_id"), "algorithm id", 200
            )
            if not AlgorithmCatalog.algorithm_exists(algorithm_id):
                raise AiResponseError(
                    f"AI proposed an unavailable Processing algorithm: {algorithm_id}"
                )
            if not AlgorithmCatalog.ai_algorithm_allowed(algorithm_id):
                raise AiResponseError(
                    f"AI proposed a restricted Processing algorithm: {algorithm_id}"
                )
            node = AlgorithmCatalog.create_node(
                algorithm_id,
                node_id=node_id,
                title=cls._bounded_text(item.get("title"), "node title", 160),
            )
            for key, value in cls._parameter_pairs(item.get("parameters", [])):
                if key not in node.inputs and key not in ("LAYER", "VALUE"):
                    raise AiResponseError(
                        f"Parameter '{key}' does not exist on {algorithm_id}."
                    )
                node.parameters[key] = value
            if algorithm_id in ("smart:input_layer", "smart:raster_layer"):
                layer_id = str(node.parameters.get("LAYER", "") or "").strip()
                if layer_id:
                    expected_type = (
                        "raster" if algorithm_id == "smart:raster_layer" else "vector"
                    )
                    if layer_id not in AlgorithmCatalog.layer_choices(expected_type):
                        raise AiResponseError(
                            f"AI proposed a project layer id that is not available: {layer_id}"
                        )
            try:
                graph.add_node(node)
            except GraphValidationError as error:
                raise AiResponseError(str(error)) from error

        for item in edges_data:
            if not isinstance(item, dict):
                raise AiResponseError("Every edge must be an object.")
            cls._require_exact_keys(
                item,
                {"from_node", "from_output", "to_node", "to_input"},
                "edge",
            )
            values = [
                cls._bounded_text(item.get(key), key, 100)
                for key in ("from_node", "from_output", "to_node", "to_input")
            ]
            edge = graph.add_edge(*values)
            if edge is None:
                raise AiResponseError(
                    f"Invalid edge {values[0]}.{values[1]} -> {values[2]}.{values[3]}: "
                    f"{graph.last_error}"
                )

        if any(not isinstance(item, str) for item in warnings_data):
            raise AiResponseError("Every warning must be text.")
        warnings = [item[:500] for item in warnings_data[:30]]
        AutoLayoutEngine.apply_layout(graph)
        return AiGraphResult(graph, summary, warnings)

    @staticmethod
    def _decode_json(text: str) -> Any:
        candidate = text.strip()
        if candidate.startswith("```"):
            candidate = re.sub(r"^```(?:json)?\s*", "", candidate, flags=re.I)
            candidate = re.sub(r"\s*```$", "", candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start >= 0 and end > start:
                try:
                    return json.loads(candidate[start: end + 1])
                except json.JSONDecodeError as error:
                    raise AiResponseError(f"Provider returned invalid JSON: {error.msg}") from error
            raise AiResponseError("Provider response did not contain a JSON object.")

    @staticmethod
    def _bounded_text(value: Any, field: str, maximum: int) -> str:
        if not isinstance(value, str) or not value.strip():
            raise AiResponseError(f"Missing or invalid {field}.")
        return value.strip()[:maximum]

    @classmethod
    def _parameter_pairs(cls, value: Any) -> Iterable[Tuple[str, Any]]:
        if not isinstance(value, list):
            raise AiResponseError("Node parameters must be an array.")
        pairs: List[Tuple[str, Any]] = []
        seen = set()
        for item in value:
            if not isinstance(item, dict):
                raise AiResponseError("Every parameter must have name and value fields.")
            cls._require_exact_keys(item, {"name", "value"}, "parameter")
            name = cls._bounded_text(item["name"], "parameter name", 100)
            if name in seen:
                raise AiResponseError(f"Duplicate parameter: {name}")
            cls._validate_parameter_value(item["value"])
            seen.add(name)
            pairs.append((name, item["value"]))
        return pairs

    @staticmethod
    def _require_exact_keys(value: Dict[str, Any], expected: set[str], label: str) -> None:
        actual = set(value)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            details = []
            if missing:
                details.append("missing " + ", ".join(missing))
            if extra:
                details.append("unexpected " + ", ".join(extra))
            raise AiResponseError(f"Invalid {label} fields: {'; '.join(details)}.")

    @staticmethod
    def _validate_parameter_value(value: Any) -> None:
        if value is None or isinstance(value, (str, int, float, bool)):
            if isinstance(value, str) and len(value) > 10000:
                raise AiResponseError("AI parameter text exceeds the safety limit.")
            if isinstance(value, float) and not math.isfinite(value):
                raise AiResponseError("AI parameter numbers must be finite.")
            return
        if isinstance(value, list) and len(value) <= 200 and all(
            isinstance(item, str) and len(item) <= 2000 for item in value
        ):
            return
        raise AiResponseError("AI parameter value has an unsupported type or size.")
