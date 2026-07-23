"""Pure-Python directed acyclic graph used by SmartModeler GIS."""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple


class SocketType:
    """Stable socket identifiers shared by the core, serializer and GUI."""

    VECTOR = "vector"
    RASTER = "raster"
    NUMBER = "number"
    STRING = "string"
    BOOLEAN = "boolean"
    FIELD = "field"
    TABLE = "table"
    FILE = "file"
    ANY = "any"


class GraphValidationError(ValueError):
    """Raised when a graph cannot be executed or serialized safely."""


@dataclass
class GraphIssue:
    level: str
    message: str
    node_id: str = ""
    code: str = ""


class NodePort:
    """Input or output socket on a :class:`NodeDefinition`."""

    def __init__(
        self,
        port_id: str,
        name: str,
        socket_type: str,
        is_output: bool = False,
        default_value: Any = None,
        required: bool = False,
        allows_multiple: bool = False,
        description: str = "",
    ) -> None:
        self.port_id = port_id
        self.name = name
        self.socket_type = socket_type
        self.is_output = is_output
        self.default_value = default_value
        self.required = required
        self.allows_multiple = allows_multiple
        self.description = description
        self.connected_edges: List[GraphEdge] = []

    def is_connected(self) -> bool:
        return bool(self.connected_edges)


class NodeDefinition:
    """Serializable visual node representing a Processing algorithm or input."""

    def __init__(
        self,
        node_id: Optional[str] = None,
        title: str = "Algorithm Node",
        category: str = "General",
        algorithm_id: str = "",
        description: str = "",
    ) -> None:
        self.node_id = node_id or str(uuid.uuid4())[:8]
        self.title = title
        self.category = category
        self.algorithm_id = algorithm_id
        self.description = description
        self.inputs: Dict[str, NodePort] = {}
        self.outputs: Dict[str, NodePort] = {}
        self.parameters: Dict[str, Any] = {}
        self.is_dirty = True
        self.cached_results: Dict[str, Any] = {}
        self.execution_state = "idle"
        self.execution_message = ""
        self.x = 0.0
        self.y = 0.0

    def add_input(
        self,
        port_id: str,
        name: str,
        socket_type: str,
        default_value: Any = None,
        required: bool = False,
        allows_multiple: bool = False,
        description: str = "",
    ) -> NodePort:
        port = NodePort(
            port_id,
            name,
            socket_type,
            is_output=False,
            default_value=default_value,
            required=required,
            allows_multiple=allows_multiple,
            description=description,
        )
        self.inputs[port_id] = port
        return port

    def add_output(
        self,
        port_id: str,
        name: str,
        socket_type: str,
        description: str = "",
    ) -> NodePort:
        port = NodePort(
            port_id,
            name,
            socket_type,
            is_output=True,
            description=description,
        )
        self.outputs[port_id] = port
        return port

    def set_parameter(self, key: str, value: Any) -> None:
        self.parameters[key] = value
        self.is_dirty = True


class GraphEdge:
    """Directed connection from one output socket to one input socket."""

    def __init__(
        self,
        edge_id: str,
        start_node_id: str,
        start_port_id: str,
        end_node_id: str,
        end_port_id: str,
    ) -> None:
        self.edge_id = edge_id or str(uuid.uuid4())[:8]
        self.start_node_id = start_node_id
        self.start_port_id = start_port_id
        self.end_node_id = end_node_id
        self.end_port_id = end_port_id


class GraphModel:
    """DAG representation with strict, typed connection validation."""

    def __init__(self, name: str = "Untitled workflow") -> None:
        self.name = name
        self.description = ""
        self.nodes: Dict[str, NodeDefinition] = {}
        self.edges: Dict[str, GraphEdge] = {}
        self.last_error = ""

    def add_node(self, node: NodeDefinition) -> str:
        if not node.node_id:
            raise GraphValidationError("A node must have a non-empty id.")
        existing = self.nodes.get(node.node_id)
        if existing is not None and existing is not node:
            raise GraphValidationError(f"Duplicate node id: {node.node_id}")
        self.nodes[node.node_id] = node
        return node.node_id

    def remove_node(self, node_id: str) -> None:
        if node_id not in self.nodes:
            return
        attached = [
            edge_id
            for edge_id, edge in self.edges.items()
            if edge.start_node_id == node_id or edge.end_node_id == node_id
        ]
        for edge_id in attached:
            self.remove_edge(edge_id)
        del self.nodes[node_id]

    @staticmethod
    def socket_types_compatible(start_type: str, end_type: str) -> bool:
        return start_type == end_type or SocketType.ANY in (start_type, end_type)

    def validate_connection(
        self,
        start_node_id: str,
        start_port_id: str,
        end_node_id: str,
        end_port_id: str,
    ) -> Tuple[bool, str]:
        start_node = self.nodes.get(start_node_id)
        end_node = self.nodes.get(end_node_id)
        if start_node is None or end_node is None:
            return False, "Both connection nodes must exist."
        start_port = start_node.outputs.get(start_port_id)
        end_port = end_node.inputs.get(end_port_id)
        if start_port is None:
            return False, f"Output port '{start_port_id}' does not exist."
        if end_port is None:
            return False, f"Input port '{end_port_id}' does not exist."
        if not self.socket_types_compatible(start_port.socket_type, end_port.socket_type):
            return False, (
                f"Incompatible sockets: {start_port.socket_type} cannot feed "
                f"{end_port.socket_type}."
            )
        if end_port.is_connected() and not end_port.allows_multiple:
            return False, f"Input '{end_port.name}' already has a connection."
        if self.creates_cycle(start_node_id, end_node_id):
            return False, "This connection would create a cycle."
        return True, ""

    def add_edge(
        self,
        start_node_id: str,
        start_port_id: str,
        end_node_id: str,
        end_port_id: str,
    ) -> Optional[GraphEdge]:
        valid, reason = self.validate_connection(
            start_node_id, start_port_id, end_node_id, end_port_id
        )
        self.last_error = reason
        if not valid:
            return None
        edge_id = f"e_{start_node_id}_{start_port_id}__to__{end_node_id}_{end_port_id}"
        if edge_id in self.edges:
            return self.edges[edge_id]
        edge = GraphEdge(edge_id, start_node_id, start_port_id, end_node_id, end_port_id)
        self.edges[edge_id] = edge
        self.nodes[start_node_id].outputs[start_port_id].connected_edges.append(edge)
        self.nodes[end_node_id].inputs[end_port_id].connected_edges.append(edge)
        self.mark_dirty_from(end_node_id)
        return edge

    def remove_edge(self, edge_id: str) -> None:
        edge = self.edges.get(edge_id)
        if edge is None:
            return
        start_node = self.nodes.get(edge.start_node_id)
        end_node = self.nodes.get(edge.end_node_id)
        if start_node is not None:
            port = start_node.outputs.get(edge.start_port_id)
            if port is not None and edge in port.connected_edges:
                port.connected_edges.remove(edge)
        if end_node is not None:
            port = end_node.inputs.get(edge.end_port_id)
            if port is not None and edge in port.connected_edges:
                port.connected_edges.remove(edge)
        del self.edges[edge_id]
        if end_node is not None:
            self.mark_dirty_from(end_node.node_id)

    def outgoing_edges(self, node_id: str) -> Iterable[GraphEdge]:
        return (edge for edge in self.edges.values() if edge.start_node_id == node_id)

    def incoming_edges(self, node_id: str) -> Iterable[GraphEdge]:
        return (edge for edge in self.edges.values() if edge.end_node_id == node_id)

    def mark_dirty_from(self, node_id: str) -> None:
        queue = [node_id]
        visited: Set[str] = set()
        while queue:
            current = queue.pop(0)
            if current in visited:
                continue
            visited.add(current)
            node = self.nodes.get(current)
            if node is not None:
                node.is_dirty = True
                node.execution_state = "idle"
                node.execution_message = ""
                for edge in self.outgoing_edges(current):
                    queue.append(edge.end_node_id)

    def creates_cycle(self, start_node_id: str, end_node_id: str) -> bool:
        """Return whether adding ``start -> end`` would create a cycle."""
        if start_node_id == end_node_id:
            return True
        visited: Set[str] = set()
        queue = [end_node_id]
        while queue:
            current = queue.pop(0)
            if current == start_node_id:
                return True
            if current in visited:
                continue
            visited.add(current)
            queue.extend(edge.end_node_id for edge in self.outgoing_edges(current))
        return False

    def get_topological_order(self) -> List[NodeDefinition]:
        in_degree = {node_id: 0 for node_id in self.nodes}
        for edge in self.edges.values():
            if edge.end_node_id in in_degree:
                in_degree[edge.end_node_id] += 1
        queue = [node_id for node_id, degree in in_degree.items() if degree == 0]
        order: List[NodeDefinition] = []
        while queue:
            current = queue.pop(0)
            order.append(self.nodes[current])
            for edge in self.outgoing_edges(current):
                in_degree[edge.end_node_id] -= 1
                if in_degree[edge.end_node_id] == 0:
                    queue.append(edge.end_node_id)
        if len(order) != len(self.nodes):
            raise GraphValidationError("The graph contains a cycle.")
        return order

    @staticmethod
    def value_is_configured(value: Any) -> bool:
        """Return whether a literal parameter contains a meaningful value."""
        if value is None:
            return False
        if isinstance(value, str):
            return bool(value.strip())
        if isinstance(value, (list, tuple, dict, set)):
            return bool(value)
        return True

    def validate(self) -> List[GraphIssue]:
        issues: List[GraphIssue] = []
        try:
            self.get_topological_order()
        except GraphValidationError as error:
            issues.append(GraphIssue("error", str(error), code="cycle"))
        for node in self.nodes.values():
            if not node.algorithm_id:
                issues.append(
                    GraphIssue(
                        "error",
                        "Node has no algorithm id.",
                        node.node_id,
                        "algorithm",
                    )
                )
            if node.algorithm_id in ("smart:input_layer", "smart:raster_layer") and not str(
                node.parameters.get("LAYER", "")
            ).strip():
                issues.append(
                    GraphIssue(
                        "error",
                        "Project layer input is not configured.",
                        node.node_id,
                        "missing_input",
                    )
                )
            for port in node.inputs.values():
                if (
                    port.required
                    and not port.is_connected()
                    and not self.value_is_configured(
                        node.parameters.get(port.port_id, port.default_value)
                    )
                ):
                    issues.append(
                        GraphIssue(
                            "error",
                            f"Required input '{port.name}' is not configured.",
                            node.node_id,
                            "missing_input",
                        )
                    )
        return issues
