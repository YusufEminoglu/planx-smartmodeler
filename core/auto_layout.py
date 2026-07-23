"""Sugiyama-style DAG Auto-Layout Engine for SmartModeler GIS (QGIS 4)."""
from typing import Dict, List, Set
from .graph_model import GraphModel, NodeDefinition


class AutoLayoutEngine:
    """Calculates clean grid coordinates for DAG nodes without overlapping cards or tangled wires."""

    COLUMN_SPACING = 330.0
    ROW_SPACING = 180.0

    @classmethod
    def apply_layout(cls, graph: GraphModel, start_x: float = 0.0, start_y: float = 0.0) -> None:
        """Assigns (x, y) coordinates to all nodes in the graph based on topological ranks."""
        if not graph.nodes:
            return

        # 1. Calculate in-degrees and ranks (layer columns)
        in_degree: Dict[str, int] = {n_id: 0 for n_id in graph.nodes}
        for edge in graph.edges.values():
            if edge.end_node_id in in_degree:
                in_degree[edge.end_node_id] += 1

        ranks: Dict[str, int] = {}
        queue = [n_id for n_id, deg in in_degree.items() if deg == 0]

        for n_id in queue:
            ranks[n_id] = 0

        visited: Set[str] = set(queue)

        while queue:
            curr_id = queue.pop(0)
            curr_rank = ranks[curr_id]

            for edge in graph.edges.values():
                if edge.start_node_id == curr_id:
                    target_id = edge.end_node_id
                    ranks[target_id] = max(ranks.get(target_id, 0), curr_rank + 1)
                    if target_id not in visited:
                        visited.add(target_id)
                        queue.append(target_id)

        # Assign rank 0 to any orphan nodes
        for n_id in graph.nodes:
            if n_id not in ranks:
                ranks[n_id] = 0

        # 2. Group nodes by rank
        columns: Dict[int, List[NodeDefinition]] = {}
        for n_id, rank in ranks.items():
            if rank not in columns:
                columns[rank] = []
            columns[rank].append(graph.nodes[n_id])

        # 3. Position nodes on canvas
        for rank, nodes in columns.items():
            col_x = start_x + (rank * cls.COLUMN_SPACING)
            total_height = (len(nodes) - 1) * cls.ROW_SPACING
            initial_y = start_y - (total_height / 2.0)

            for idx, node in enumerate(nodes):
                node.x = col_x
                node.y = initial_y + (idx * cls.ROW_SPACING)
