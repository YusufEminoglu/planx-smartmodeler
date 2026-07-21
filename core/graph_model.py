"""DAG (Directed Acyclic Graph) engine for SmartModeler GIS (QGIS 4)."""
import uuid
from typing import Dict, List, Any, Optional, Set


class SocketType:
    VECTOR = "vector"
    RASTER = "raster"
    NUMBER = "number"
    STRING = "string"
    BOOLEAN = "boolean"
    FIELD = "field"
    ANY = "any"


class NodePort:
    """Input or Output port socket on a Node."""

    def __init__(self, port_id: str, name: str, socket_type: str, is_output: bool = False, default_value: Any = None):
        self.port_id = port_id
        self.name = name
        self.socket_type = socket_type
        self.is_output = is_output
        self.default_value = default_value
        self.connected_edges: List['GraphEdge'] = []

    def is_connected(self) -> bool:
        return len(self.connected_edges) > 0


class NodeDefinition:
    """Base model node representing an operation or QGIS algorithm."""

    def __init__(self, node_id: Optional[str] = None, title: str = "Algorithm Node", category: str = "General"):
        self.node_id = node_id or str(uuid.uuid4())[:8]
        self.title = title
        self.category = category
        self.inputs: Dict[str, NodePort] = {}
        self.outputs: Dict[str, NodePort] = {}
        self.parameters: Dict[str, Any] = {}
        self.is_dirty = True
        self.cached_results: Dict[str, Any] = {}
        self.x = 0.0
        self.y = 0.0

    def add_input(self, port_id: str, name: str, socket_type: str, default_value: Any = None) -> NodePort:
        port = NodePort(port_id, name, socket_type, is_output=False, default_value=default_value)
        self.inputs[port_id] = port
        return port

    def add_output(self, port_id: str, name: str, socket_type: str) -> NodePort:
        port = NodePort(port_id, name, socket_type, is_output=True)
        self.outputs[port_id] = port
        return port

    def set_parameter(self, key: str, value: Any):
        self.parameters[key] = value
        self.is_dirty = True


class GraphEdge:
    """Connection line between an output port and an input port."""

    def __init__(self, edge_id: str, start_node_id: str, start_port_id: str, end_node_id: str, end_port_id: str):
        self.edge_id = edge_id or str(uuid.uuid4())[:8]
        self.start_node_id = start_node_id
        self.start_port_id = start_port_id
        self.end_node_id = end_node_id
        self.end_port_id = end_port_id


class GraphModel:
    """Complete DAG representation managing nodes, edges, and topological sort."""

    def __init__(self):
        self.nodes: Dict[str, NodeDefinition] = {}
        self.edges: Dict[str, GraphEdge] = {}

    def add_node(self, node: NodeDefinition) -> str:
        self.nodes[node.node_id] = node
        return node.node_id

    def remove_node(self, node_id: str):
        if node_id not in self.nodes:
            return
        # Remove attached edges
        edges_to_remove = [
            e_id for e_id, e in self.edges.items()
            if e.start_node_id == node_id or e.end_node_id == node_id
        ]
        for e_id in edges_to_remove:
            self.remove_edge(e_id)
        del self.nodes[node_id]

    def add_edge(self, start_node_id: str, start_port_id: str, end_node_id: str, end_port_id: str) -> Optional[GraphEdge]:
        if start_node_id not in self.nodes or end_node_id not in self.nodes:
            return None

        # Cycle check
        if self.creates_cycle(start_node_id, end_node_id):
            return None

        edge_id = f"e_{start_node_id}_{start_port_id}__to__{end_node_id}_{end_port_id}"
        edge = GraphEdge(edge_id, start_node_id, start_port_id, end_node_id, end_port_id)
        self.edges[edge_id] = edge

        start_port = self.nodes[start_node_id].outputs.get(start_port_id)
        end_port = self.nodes[end_node_id].inputs.get(end_port_id)

        if start_port:
            start_port.connected_edges.append(edge)
        if end_port:
            end_port.connected_edges.append(edge)

        self.nodes[end_node_id].is_dirty = True
        return edge

    def remove_edge(self, edge_id: str):
        if edge_id not in self.edges:
            return
        edge = self.edges[edge_id]
        if edge.start_node_id in self.nodes:
            port = self.nodes[edge.start_node_id].outputs.get(edge.start_port_id)
            if port and edge in port.connected_edges:
                port.connected_edges.remove(edge)
        if edge.end_node_id in self.nodes:
            port = self.nodes[edge.end_node_id].inputs.get(edge.end_port_id)
            if port and edge in port.connected_edges:
                port.connected_edges.remove(edge)
        del self.edges[edge_id]

    def creates_cycle(self, start_node_id: str, end_node_id: str) -> bool:
        """Check if connecting start_node -> end_node would form a cycle in the DAG."""
        if start_node_id == end_node_id:
            return True
        visited: Set[str] = set()
        queue = [end_node_id]

        while queue:
            curr = queue.pop(0)
            if curr == start_node_id:
                return True
            if curr not in visited:
                visited.add(curr)
                # Find all downstream nodes from curr
                for edge in self.edges.values():
                    if edge.start_node_id == curr:
                        queue.append(edge.end_node_id)
        return False

    def get_topological_order(self) -> List[NodeDefinition]:
        """Return nodes ordered topologically for sequential evaluation."""
        in_degree: Dict[str, int] = {n_id: 0 for n_id in self.nodes}
        for edge in self.edges.values():
            if edge.end_node_id in in_degree:
                in_degree[edge.end_node_id] += 1

        queue = [n_id for n_id, deg in in_degree.items() if deg == 0]
        order: List[NodeDefinition] = []

        while queue:
            curr_id = queue.pop(0)
            order.append(self.nodes[curr_id])
            for edge in self.edges.values():
                if edge.start_node_id == curr_id:
                    target_id = edge.end_node_id
                    in_degree[target_id] -= 1
                    if in_degree[target_id] == 0:
                        queue.append(target_id)
        return order
