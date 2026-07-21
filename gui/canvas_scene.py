"""PyQt6 QGraphicsScene for SmartModeler GIS graph editing."""
from qgis.PyQt.QtCore import QRectF, QPointF, Qt, pyqtSignal
from qgis.PyQt.QtGui import QBrush, QColor, QPen, QPainter
from qgis.PyQt.QtWidgets import QGraphicsScene
from ..core.graph_model import GraphModel, NodeDefinition, GraphEdge
from .node_graphics_item import NodeGraphicsItem
from .connection_graphics_item import ConnectionGraphicsItem
from .port_graphics_item import PortGraphicsItem


class CanvasScene(QGraphicsScene):
    """QGraphicsScene backing the interactive node graph canvas."""

    node_selected = pyqtSignal(object)
    connection_created = pyqtSignal(object)

    def __init__(self, graph: GraphModel):
        super().__init__()
        self.graph = graph
        self.setSceneRect(-5000, -5000, 10000, 10000)
        self.setBackgroundBrush(QBrush(QColor("#191C21")))

        self.node_items: dict[str, NodeGraphicsItem] = {}
        self.connection_items: dict[str, ConnectionGraphicsItem] = {}

        self.temp_cable: ConnectionGraphicsItem = None
        self.drag_start_port_item: PortGraphicsItem = None

        self.selectionChanged.connect(self.on_selection_changed)

    def drawBackground(self, painter, rect):
        super().drawBackground(painter, rect)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        # Draw grid lines
        grid_size = 25.0
        left = int(rect.left()) - (int(rect.left()) % int(grid_size))
        top = int(rect.top()) - (int(rect.top()) % int(grid_size))

        pen_sub = QPen(QColor("#232830"), 1.0)
        painter.setPen(pen_sub)

        for x in range(left, int(rect.right()), int(grid_size)):
            painter.drawLine(x, int(rect.top()), x, int(rect.bottom()))
        for y in range(top, int(rect.bottom()), int(grid_size)):
            painter.drawLine(int(rect.left()), y, int(rect.right()), y)

    def add_node_to_scene(self, node: NodeDefinition) -> NodeGraphicsItem:
        self.graph.add_node(node)
        item = NodeGraphicsItem(node)
        self.addItem(item)
        self.node_items[node.node_id] = item
        return item

    def connect_ports(self, start_node_id: str, start_port_id: str, end_node_id: str, end_port_id: str):
        edge = self.graph.add_edge(start_node_id, start_port_id, end_node_id, end_port_id)
        if not edge:
            return

        start_item = self.node_items[start_node_id].output_ports.get(start_port_id)
        end_item = self.node_items[end_node_id].input_ports.get(end_port_id)

        if start_item and end_item:
            conn = ConnectionGraphicsItem(edge, start_item, end_item)
            self.addItem(conn)
            self.connection_items[edge.edge_id] = conn
            self.connection_created.emit(edge)

    def update_node_connections(self, node_id: str):
        for conn in self.connection_items.values():
            if conn.edge.start_node_id == node_id or conn.edge.end_node_id == node_id:
                conn.update_path()

    def on_selection_changed(self):
        selected = self.selectedItems()
        if selected and isinstance(selected[0], NodeGraphicsItem):
            self.node_selected.emit(selected[0].node)
        else:
            self.node_selected.emit(None)
