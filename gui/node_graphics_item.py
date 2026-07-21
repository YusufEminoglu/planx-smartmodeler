"""PyQt6 QGraphicsItem node card rendering for SmartModeler GIS."""
from qgis.PyQt.QtCore import QRectF, QPointF, Qt
from qgis.PyQt.QtGui import QBrush, QColor, QPen, QFont, QLinearGradient
from qgis.PyQt.QtWidgets import QGraphicsItem, QGraphicsTextItem
from ..core.graph_model import NodeDefinition
from .port_graphics_item import PortGraphicsItem


class NodeGraphicsItem(QGraphicsItem):
    """Sleek modern node card item on the canvas."""

    WIDTH = 180.0
    HEADER_HEIGHT = 28.0
    PORT_SPACING = 22.0

    CATEGORY_COLORS = {
        "Vector Geometry": "#2E7D32",
        "Vector Overlay": "#1565C0",
        "Vector Selection": "#6A1B9A",
        "Raster Analysis": "#D84315",
        "Raster Terrain": "#E65100",
        "Table Operations": "#00838F",
        "Parameters": "#F57F17",
        "General": "#424242"
    }

    def __init__(self, node: NodeDefinition):
        super().__init__()
        self.node = node
        self.setPos(node.x, node.y)
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable |
            QGraphicsItem.GraphicsItemFlag.ItemIsSelectable |
            QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.input_ports: dict[str, PortGraphicsItem] = {}
        self.output_ports: dict[str, PortGraphicsItem] = {}
        self.build_ports()

    def calculate_height(self) -> float:
        max_ports = max(len(self.node.inputs), len(self.node.outputs), 1)
        return self.HEADER_HEIGHT + (max_ports * self.PORT_SPACING) + 12.0

    def boundingRect(self) -> QRectF:
        return QRectF(0, 0, self.WIDTH, self.calculate_height())

    def build_ports(self):
        # Input ports on left edge
        y_offset = self.HEADER_HEIGHT + 14.0
        for p_id, port in self.node.inputs.items():
            port_item = PortGraphicsItem(port, self)
            port_item.setPos(0, y_offset)
            self.input_ports[p_id] = port_item
            y_offset += self.PORT_SPACING

        # Output ports on right edge
        y_offset = self.HEADER_HEIGHT + 14.0
        for p_id, port in self.node.outputs.items():
            port_item = PortGraphicsItem(port, self)
            port_item.setPos(self.WIDTH, y_offset)
            self.output_ports[p_id] = port_item
            y_offset += self.PORT_SPACING

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        rect = self.boundingRect()
        height = rect.height()

        # Background Card
        bg_color = QColor("#263238")
        border_color = QColor("#00E5FF") if self.isSelected() else QColor("#37474F")
        painter.setBrush(QBrush(bg_color))
        painter.setPen(QPen(border_color, 2.0 if self.isSelected() else 1.2))
        painter.drawRoundedRect(rect, 8.0, 8.0)

        # Header bar gradient
        hdr_color = QColor(self.CATEGORY_COLORS.get(self.node.category, "#424242"))
        hdr_rect = QRectF(0, 0, self.WIDTH, self.HEADER_HEIGHT)
        hdr_grad = QLinearGradient(0, 0, 0, self.HEADER_HEIGHT)
        hdr_grad.setColorAt(0.0, hdr_color.lighter(115))
        hdr_grad.setColorAt(1.0, hdr_color)
        painter.setBrush(QBrush(hdr_grad))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(hdr_rect, 8.0, 8.0)
        # Cover bottom corners of header
        painter.drawRect(QRectF(0.0, self.HEADER_HEIGHT - 6.0, self.WIDTH, 6.0))

        # Title Text
        painter.setFont(QFont("Segoe UI", 9, QFont.Weight.Bold))
        painter.setPen(QPen(QColor("#FFFFFF")))
        painter.drawText(QRectF(10, 0, self.WIDTH - 20, self.HEADER_HEIGHT), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, self.node.title)

        # Port Labels
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(QPen(QColor("#B0BEC5")))

        y_offset = self.HEADER_HEIGHT + 18.0
        for p_id, port in self.node.inputs.items():
            painter.drawText(QRectF(12, y_offset - 10, 80, 16), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, port.name)
            y_offset += self.PORT_SPACING

        y_offset = self.HEADER_HEIGHT + 18.0
        for p_id, port in self.node.outputs.items():
            painter.drawText(QRectF(self.WIDTH - 92, y_offset - 10, 80, 16), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, port.name)
            y_offset += self.PORT_SPACING

    def itemChange(self, change, value):
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.node.x = self.pos().x()
            self.node.y = self.pos().y()
            if self.scene():
                self.scene().update_node_connections(self.node.node_id)
        return super().itemChange(change, value)
