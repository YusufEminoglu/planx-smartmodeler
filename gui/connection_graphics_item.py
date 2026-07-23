"""PyQt6 smooth Bezier cable connection item for SmartModeler GIS."""
from qgis.PyQt.QtCore import QPointF, Qt
from qgis.PyQt.QtGui import QPainterPath, QPen, QColor
from qgis.PyQt.QtWidgets import QGraphicsPathItem
from ..core.graph_model import GraphEdge


class ConnectionGraphicsItem(QGraphicsPathItem):
    """Visual curved cable connecting an output port to an input port."""

    def __init__(self, edge: GraphEdge, start_port_item, end_port_item):
        super().__init__()
        self.edge = edge
        self.start_port_item = start_port_item
        self.end_port_item = end_port_item
        self.setZValue(-1.0)  # Render cables under nodes
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsPathItem.GraphicsItemFlag.ItemIsSelectable, edge is not None)
        self.is_hovered = False
        self.update_path()

    def update_path(self, override_end_pos: QPointF = None):
        if not self.start_port_item:
            return

        p1 = self.start_port_item.get_center_scene_pos()
        p2 = override_end_pos if override_end_pos else (
            self.end_port_item.get_center_scene_pos() if self.end_port_item else p1)

        path = QPainterPath(p1)
        dx = abs(p2.x() - p1.x()) * 0.5
        dx = max(dx, 40.0)

        c1 = QPointF(p1.x() + dx, p1.y())
        c2 = QPointF(p2.x() - dx, p2.y())

        path.cubicTo(c1, c2, p2)
        self.setPath(path)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        highlighted = self.is_hovered or self.isSelected()
        pen_color = QColor("#57D3A0") if highlighted else QColor("#637083")
        pen_width = 3.0 if highlighted else 2.0

        painter.setPen(QPen(pen_color, pen_width, Qt.PenStyle.SolidLine))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())

    def hoverEnterEvent(self, event):
        self.is_hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        self.update()
        super().hoverLeaveEvent(event)
