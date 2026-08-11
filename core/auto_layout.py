"""Sugiyama-style DAG Auto-Layout Engine for SmartModeler GIS (QGIS 4)."""
from typing import Dict, List, Set
from .graph_model import GraphModel, NodeDefinition


class AutoLayoutEngine:
    """Calculates clean grid coordinates for DAG nodes without overlapping cards or tangled wires."""

    COLUMN_SPACING = 330.0
    ROW_SPACING = 180.0

    @classmethod
    def arrange_patched_graph(cls, before: GraphModel, after: GraphModel) -> None:
        """Give a patched graph its coordinates before it reaches the canvas.

        A ``model_patch`` carries structure, not positions, so every node it
        added arrived at (0, 0) and the cards landed exactly on top of one
        another -- the user had to run Auto layout by hand after each AI edit.

        A graph with nothing arranged yet is laid out whole: that is the ideal
        form for a workflow the AI has just created, and it also repairs a
        graph an earlier patch left stacked at the origin. A graph the user has
        already arranged keeps every position it had; only the new nodes are
        placed, beside the parents that feed them.
        """
        if not any(node.x or node.y for node in before.nodes.values()):
            cls.apply_layout(after)
            return
        cls.place_new_nodes(before, after)

    @classmethod
    def place_new_nodes(cls, before: GraphModel, after: GraphModel) -> None:
        """Keep established node positions and place only newly added nodes."""
        existing_ids = set(before.nodes) & set(after.nodes)
        for node_id in existing_ids:
            after.nodes[node_id].x = before.nodes[node_id].x
            after.nodes[node_id].y = before.nodes[node_id].y

        new_ids = set(after.nodes) - set(before.nodes)
        if not new_ids:
            return
        right_edge = max((node.x for node in before.nodes.values()), default=0.0)
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
                node.x = max(parent.x for parent in parents) + cls.COLUMN_SPACING
                node.y = sum(parent.y for parent in parents) / len(parents)
                right_edge = max(right_edge, node.x)
            else:
                node.x = right_edge + cls.COLUMN_SPACING
                node.y = fallback_row * cls.ROW_SPACING
                right_edge = node.x
                fallback_row += 1

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
