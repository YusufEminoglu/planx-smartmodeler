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
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.MinimalViewportUpdate)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.is_panning = False
        self.pan_start_pos = QPointF()

        self.dragging_cable: ConnectionGraphicsItem = None
        self.drag_start_port: PortGraphicsItem = None
        self._zoom_level = 0

    def set_canvas_scene(self, scene: CanvasScene):
        self.canvas_scene = scene
        super().setScene(scene)

    def wheelEvent(self, event: QWheelEvent):
        """Smooth zoom in/out centered at mouse cursor."""
        zoom_in_factor = 1.15
        zoom_out_factor = 1 / zoom_in_factor

        if event.angleDelta().y() > 0:
            if self._zoom_level >= 20:
                return
            zoom = zoom_in_factor
            self._zoom_level += 1
        else:
            if self._zoom_level <= -18:
                return
            zoom = zoom_out_factor
            self._zoom_level -= 1

        self.scale(zoom, zoom)

    def mousePressEvent(self, event: QMouseEvent):
        item = self.itemAt(event.pos())

        # Middle mouse or Alt+Left mouse for panning
        if event.button() == Qt.MouseButton.MiddleButton or (
            event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & Qt.KeyboardModifier.AltModifier
        ):
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
                start_item = self.drag_start_port
                end_item = item
                if not start_item.port.is_output and end_item.port.is_output:
                    start_item, end_item = end_item, start_item
                if start_item.port.is_output and not end_item.port.is_output:
                    self.canvas_scene.connect_ports(
                        start_item.node_item.node.node_id,
                        start_item.port.port_id,
                        end_item.node_item.node.node_id,
                        end_item.port.port_id,
                    )

            # Cleanup temp cable
            self.canvas_scene.removeItem(self.dragging_cable)
            self.dragging_cable = None
            self.drag_start_port = None
            return

        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.canvas_scene.remove_selected_items()
            event.accept()
            return
        if event.key() == Qt.Key.Key_F:
            items_rect = self.canvas_scene.itemsBoundingRect()
            if not items_rect.isEmpty():
                self.fitInView(items_rect.adjusted(-80, -80, 80, 80),
                               Qt.AspectRatioMode.KeepAspectRatio)
            event.accept()
            return
        super().keyPressEvent(event)
