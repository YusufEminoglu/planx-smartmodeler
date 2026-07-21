"""Main Model Designer Window for SmartModeler GIS (QGIS 4+)."""
from qgis.PyQt.QtCore import Qt, QSize
from qgis.PyQt.QtGui import QIcon, QAction
from qgis.PyQt.QtWidgets import (
    QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QToolBar,
    QSplitter, QMessageBox, QFileDialog
)
from ..core.graph_model import GraphModel, NodeDefinition, SocketType
from ..core.model3_serializer import Model3Serializer
from .canvas_scene import CanvasScene
from .canvas_view import CanvasView
from .smart_proposal_bar import SmartProposalBar
from .node_palette_widget import NodePaletteWidget
from .wire_inspector_widget import WireInspectorWidget


class SmartModelerWindow(QMainWindow):
    """Next-generation Graphical Modeler main application window."""

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle("SmartModeler GIS — Next-Gen QGIS 4 Model Designer")
        self.resize(1280, 800)

        self.graph = GraphModel()
        self.scene = CanvasScene(self.graph)
        self.view = CanvasView(self.scene, self)

        self.init_ui()
        self.connect_signals()

    def init_ui(self):
        # Central widget layout
        main_widget = QWidget(self)
        self.setCentralWidget(main_widget)
        main_layout = QVBoxLayout(main_widget)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # Smart Proposal Tip Bar
        self.proposal_bar = SmartProposalBar(self)
        main_layout.addWidget(self.proposal_bar)

        # Main Splitter (Left Palette | Canvas View | Right Inspector)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)

        self.palette_widget = NodePaletteWidget(self)
        self.inspector_widget = WireInspectorWidget(self)

        self.splitter.addWidget(self.palette_widget)
        self.splitter.addWidget(self.view)
        self.splitter.addWidget(self.inspector_widget)

        self.splitter.setSizes([260, 760, 260])
        main_layout.addWidget(self.splitter)

        # Toolbar setup
        self.setup_toolbar()

    def setup_toolbar(self):
        toolbar = QToolBar("SmartModeler Controls", self)
        toolbar.setIconSize(QSize(20, 20))
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, toolbar)

        act_run = QAction("▶ Run Model", self)
        act_run.setStatusTip("Execute current visual graph model")
        act_run.triggered.connect(self.run_model)
        toolbar.addAction(act_run)

        toolbar.addSeparator()

        act_export_m3 = QAction("💾 Export .model3", self)
        act_export_m3.setStatusTip("Export model to standard QGIS .model3 format")
        act_export_m3.triggered.connect(self.export_model3)
        toolbar.addAction(act_export_m3)

        act_import_m3 = QAction("📂 Open Model", self)
        act_import_m3.setStatusTip("Open existing model JSON/model3 file")
        act_import_m3.triggered.connect(self.import_model)
        toolbar.addAction(act_import_m3)

        toolbar.addSeparator()

        act_clear = QAction("🗑 Clear Canvas", self)
        act_clear.triggered.connect(self.clear_canvas)
        toolbar.addAction(act_clear)

    def connect_signals(self):
        self.palette_widget.node_requested.connect(self.add_node_by_alg)
        self.palette_widget.package_requested.connect(self.load_preset_package)
        self.proposal_bar.proposal_selected.connect(self.add_node_by_alg)
        self.scene.node_selected.connect(self.on_node_selected)

    def add_node_by_alg(self, alg_id: str, title: str = None, category: str = "General"):
        title = title or alg_id.split(":")[-1].replace("_", " ").title()
        node = NodeDefinition(title=title, category=category)
        node.parameters["alg_id"] = alg_id

        # Standard ports setup
        node.add_input("in_layer", "Input Layer", SocketType.VECTOR)
        node.add_output("out_layer", "Output Layer", SocketType.VECTOR)

        # Position node near center of current view
        view_center = self.view.mapToScene(self.view.viewport().rect().center())
        node.x = view_center.x() + (len(self.graph.nodes) * 20.0)
        node.y = view_center.y() + (len(self.graph.nodes) * 20.0)

        item = self.scene.add_node_to_scene(node)
        self.scene.clearSelection()
        item.setSelected(True)

    def load_preset_package(self, tpl_id: str):
        self.clear_canvas()
        if tpl_id == "tpl_isochrone":
            n1 = NodeDefinition(title="Vector Layer Input", category="Parameters")
            n1.add_output("out", "Vector", SocketType.VECTOR)
            n1.x, n1.y = -300, 0
            self.scene.add_node_to_scene(n1)

            n2 = NodeDefinition(title="Buffer (800m)", category="Vector Geometry")
            n2.add_input("in", "Input", SocketType.VECTOR)
            n2.add_output("out", "Buffered", SocketType.VECTOR)
            n2.parameters["distance"] = 800.0
            n2.x, n2.y = -50, 0
            self.scene.add_node_to_scene(n2)

            n3 = NodeDefinition(title="Zonal Statistics", category="Raster Analysis")
            n3.add_input("in_vec", "Zones", SocketType.VECTOR)
            n3.add_output("out_stat", "Output", SocketType.VECTOR)
            n3.x, n3.y = 200, 0
            self.scene.add_node_to_scene(n3)

            self.scene.connect_ports(n1.node_id, "out", n2.node_id, "in")
            self.scene.connect_ports(n2.node_id, "out", n3.node_id, "in_vec")

        elif tpl_id == "tpl_extrusion_3d":
            n1 = NodeDefinition(title="Building Footprints", category="Parameters")
            n1.add_output("out", "Footprints", SocketType.VECTOR)
            n1.x, n1.y = -250, 0
            self.scene.add_node_to_scene(n1)

            n2 = NodeDefinition(title="Extrude 3D Massing", category="Vector Geometry")
            n2.add_input("in", "Input", SocketType.VECTOR)
            n2.add_output("out_3d", "3D Mesh", SocketType.VECTOR)
            n2.parameters["height"] = 15.0
            n2.x, n2.y = 0, 0
            self.scene.add_node_to_scene(n2)

            self.scene.connect_ports(n1.node_id, "out", n2.node_id, "in")

        QMessageBox.information(self, "Preset Loaded", f"Loaded preset micro-package template: {tpl_id}")

    def on_node_selected(self, node: NodeDefinition):
        self.proposal_bar.update_for_node(node)
        self.inspector_widget.inspect_node(node)

    def run_model(self):
        order = self.graph.get_topological_order()
        if not order:
            QMessageBox.warning(self, "Empty Model", "No nodes present on the canvas to execute.")
            return

        QMessageBox.information(
            self, "Executing Model",
            f"SmartModeler DAG Engine initialized.\nTopological execution order ({len(order)} steps):\n" +
            "\n".join([f"{idx+1}. {n.title} [{n.node_id}]" for idx, n in enumerate(order)])
        )

    def export_model3(self):
        xml_str = Model3Serializer.export_to_model3_xml(self.graph)
        file_path, _ = QFileDialog.getSaveFileName(self, "Save QGIS Model", "", "QGIS Model (*.model3)")
        if file_path:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(xml_str)
            QMessageBox.information(self, "Export Complete", f"Successfully exported model to:\n{file_path}")

    def import_model(self):
        file_path, _ = QFileDialog.getOpenFileName(self, "Open Model File", "", "SmartModeler JSON (*.json)")
        if file_path:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            graph = Model3Serializer.import_from_json(content)
            if graph:
                self.graph = graph
                self.scene = CanvasScene(self.graph)
                self.view.setScene(self.scene)
                self.connect_signals()
                QMessageBox.information(self, "Model Loaded", f"Successfully loaded model from:\n{file_path}")

    def clear_canvas(self):
        self.graph = GraphModel()
        self.scene = CanvasScene(self.graph)
        self.view.setScene(self.scene)
        self.connect_signals()
