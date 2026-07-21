"""PyQt6 QGraphicsItem socket port representation for SmartModeler GIS."""
from qgis.PyQt.QtCore import QRectF, QPointF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QPen
from qgis.PyQt.QtWidgets import QGraphicsItem
from ..core.graph_model import NodePort, SocketType


class PortGraphicsItem(QGraphicsItem):
    """Visual socket pin on a node card."""

    RADIUS = 6.0

    COLOR_MAP = {
        SocketType.VECTOR: QColor("#4CAF50"),      # Vibrant Green
        SocketType.RASTER: QColor("#2196F3"),      # Bright Blue
        SocketType.NUMBER: QColor("#FF9800"),      # Orange
        SocketType.STRING: QColor("#E91E63"),      # Pink
        SocketType.BOOLEAN: QColor("#9C27B0"),     # Purple
        SocketType.FIELD: QColor("#00BCD4"),       # Cyan
        SocketType.ANY: QColor("#B0BEC5")          # Gray
    }

    def __init__(self, port: NodePort, parent_node_item: QGraphicsItem):
        super().__init__(parent_node_item)
        self.port = port
        self.node_item = parent_node_item
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemSendsScenePositionChanges, True)
        self.setAcceptHoverEvents(True)
        self.is_hovered = False

    def boundingRect(self) -> QRectF:
        r = self.RADIUS + 4.0
        return QRectF(-r, -r, 2 * r, 2 * r)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        color = self.COLOR_MAP.get(self.port.socket_type, QColor("#B0BEC5"))
        if self.is_hovered:
            color = color.lighter(130)

        painter.setBrush(QBrush(color))
        painter.setPen(QPen(QColor("#1E1E24"), 1.5))
        painter.drawEllipse(QPointF(0, 0), self.RADIUS, self.RADIUS)

        # Socket center dot if connected
        if self.port.is_connected():
            painter.setBrush(QBrush(QColor("#FFFFFF")))
            painter.drawEllipse(QPointF(0, 0), 2.5, 2.5)

    def hoverEnterEvent(self, event):
        self.is_hovered = True
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self.is_hovered = False
        self.update()
        super().hoverLeaveEvent(event)

    def get_center_scene_pos(self) -> QPointF:
        return self.mapToScene(QPointF(0, 0))
