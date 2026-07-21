"""PyQt6 QGraphicsView viewport canvas for SmartModeler GIS."""
from qgis.PyQt.QtCore import Qt, QPointF
from qgis.PyQt.QtGui import QPainter, QWheelEvent, QMouseEvent
from qgis.PyQt.QtWidgets import QGraphicsView
from .canvas_scene import CanvasScene
from .port_graphics_item import PortGraphicsItem
from .connection_graphics_item import ConnectionGraphicsItem


class CanvasView(QGraphicsView):
    """Zoomable, pannable hardware-accelerated canvas viewport."""

    def __init__(self, scene: CanvasScene, parent=None):
        super().__init__(scene, parent)
        self.canvas_scene = scene

        self.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.is_panning = False
        self.pan_start_pos = QPointF()

        self.dragging_cable: ConnectionGraphicsItem = None
        self.drag_start_port: PortGraphicsItem = None

    def wheelEvent(self, event: QWheelEvent):
        """Smooth zoom in/out centered at mouse cursor."""
        zoom_in_factor = 1.15
        zoom_out_factor = 1 / zoom_in_factor

        if event.angleDelta().y() > 0:
            zoom = zoom_in_factor
        else:
            zoom = zoom_out_factor

        self.scale(zoom, zoom)

    def mousePressEvent(self, event: QMouseEvent):
        item = self.itemAt(event.pos())

        # Middle mouse or Alt+Left mouse for panning
        if event.button() == Qt.MouseButton.MiddleButton or (event.button() == Qt.MouseButton.LeftButton and event.modifiers() == Qt.KeyboardModifier.AltModifier):
            self.is_panning = True
            self.pan_start_pos = event.pos()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return

        # Start cable connection drag from port
        if event.button() == Qt.MouseButton.LeftButton and isinstance(item, PortGraphicsItem):
            self.drag_start_port = item
            scene_pos = self.mapToScene(event.pos())
            self.dragging_cable = ConnectionGraphicsItem(None, item, None)
            self.canvas_scene.addItem(self.dragging_cable)
            self.dragging_cable.update_path(scene_pos)
            return

        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent):
        if self.is_panning:
            delta = self.mapToScene(event.pos()) - self.mapToScene(self.pan_start_pos)
            self.pan_start_pos = event.pos()
            self.setTransformationAnchor(QGraphicsView.ViewportAnchor.NoAnchor)
            self.translate(delta.x(), delta.y())
            return

        if self.dragging_cable:
            scene_pos = self.mapToScene(event.pos())
            self.dragging_cable.update_path(scene_pos)
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent):
        if event.button() == Qt.MouseButton.MiddleButton or self.is_panning:
            self.is_panning = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            return

        if self.dragging_cable:
            item = self.itemAt(event.pos())
            if isinstance(item, PortGraphicsItem) and self.drag_start_port and item != self.drag_start_port:
                # Complete edge connection
                start_n = self.drag_start_port.node_item.node.node_id
                start_p = self.drag_start_port.port.port_id
                end_n = item.node_item.node.node_id
                end_p = item.port.port_id

                self.canvas_scene.connect_ports(start_n, start_p, end_n, end_p)

            # Cleanup temp cable
            self.canvas_scene.removeItem(self.dragging_cable)
            self.dragging_cable = None
            self.drag_start_port = None
            return

        super().mouseReleaseEvent(event)
