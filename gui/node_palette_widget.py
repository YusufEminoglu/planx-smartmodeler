"""Sidebar Node Palette & Micro-Package Presets widget for SmartModeler GIS."""
from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.PyQt.QtWidgets import (
    QWidget, QVBoxLayout, QTreeWidget, QTreeWidgetItem,
    QLineEdit, QLabel, QPushButton, QGroupBox, QListWidget, QListWidgetItem
)
from ..core.proposal_engine import SmartProposalEngine


class NodePaletteWidget(QWidget):
    """Sidebar palette for discovering and dragging nodes and starter packages."""

    node_requested = pyqtSignal(str, str, str)  # alg_id, title, category
    package_requested = pyqtSignal(str)          # template_id

    CATEGORIZED_NODES = {
        "Vector Geometry": [
            ("native:buffer", "Buffer"),
            ("smart:roof_generator", "Parametric Roof Generator (Gable/Hip/Pyramid)"),
            ("native:centroids", "Centroids"),
            ("native:convexpolygon", "Convex Hull"),
            ("native:voronoipolygons", "Voronoi Polygons"),
            ("native:simplifygeometries", "Simplify Geometry"),
        ],
        "Vector Overlay": [
            ("native:clip", "Clip"),
            ("native:intersection", "Intersection"),
            ("native:difference", "Difference"),
            ("native:union", "Union"),
        ],
        "Vector Selection & Table": [
            ("native:extractbyattribute", "Extract by Attribute"),
            ("native:fieldcalculator", "Field Calculator"),
            ("native:joinattributestable", "Join Attributes by Field"),
        ],
        "Raster & Visualization": [
            ("smart:heatmap_renderer", "High-Divergence Spectral Heatmap"),
            ("gdal:contour", "Contour Lines"),
            ("native:slope", "Slope Calculation"),
            ("native:rastercalculator", "Raster Calculator"),
        ],
        "Controls & Parameters": [
            ("smart:slider", "Numeric Value Slider"),
            ("smart:input_layer", "Vector Layer Input"),
            ("smart:raster_layer", "Raster Layer Input"),
        ]
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout = QVBoxLayout(self)
        self.layout.setContentsMargins(6, 6, 6, 6)

        # Search Bar
        self.search_bar = QLineEdit()
        self.search_bar.setPlaceholderText("🔍 Search nodes & tools...")
        self.search_bar.textChanged.connect(self.filter_tree)
        self.layout.addWidget(self.search_bar)

        # Node Tree
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.itemDoubleClicked.connect(self.on_item_double_clicked)
        self.layout.addWidget(self.tree)

        # Micro-Package Presets Group
        self.grp_presets = QGroupBox("⚡ Micro-Package Presets")
        self.grp_layout = QVBoxLayout(self.grp_presets)
        self.lst_presets = QListWidget()
        self.lst_presets.itemDoubleClicked.connect(self.on_preset_double_clicked)
        self.grp_layout.addWidget(self.lst_presets)
        self.layout.addWidget(self.grp_presets)

        self.populate_tree()
        self.populate_presets()

    def populate_tree(self):
        self.tree.clear()
        for cat, items in self.CATEGORIZED_NODES.items():
            cat_item = QTreeWidgetItem(self.tree, [cat])
            cat_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for alg_id, title in items:
                child = QTreeWidgetItem(cat_item, [title])
                child.setData(0, Qt.ItemDataRole.UserRole, alg_id)
                child.setData(0, Qt.ItemDataRole.UserRole + 1, cat)
            cat_item.setExpanded(True)

    def populate_presets(self):
        self.lst_presets.clear()
        templates = SmartProposalEngine.get_starter_templates()
        for tpl in templates:
            item = QListWidgetItem(f"📦 {tpl['name']}")
            item.setToolTip(tpl['description'])
            item.setData(Qt.ItemDataRole.UserRole, tpl['id'])
            self.lst_presets.addItem(item)

    def filter_tree(self, text: str):
        text = text.lower()
        for i in range(self.tree.topLevelItemCount()):
            cat_item = self.tree.topLevelItem(i)
            any_visible = False
            for j in range(cat_item.childCount()):
                child = cat_item.child(j)
                matches = text in child.text(0).lower()
                child.setHidden(not matches)
                if matches:
                    any_visible = True
            cat_item.setHidden(not any_visible)

    def on_item_double_clicked(self, item: QTreeWidgetItem, column: int):
        alg_id = item.data(0, Qt.ItemDataRole.UserRole)
        cat = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if alg_id:
            self.node_requested.emit(alg_id, item.text(0), cat)

    def on_preset_double_clicked(self, item: QListWidgetItem):
        tpl_id = item.data(Qt.ItemDataRole.UserRole)
        if tpl_id:
            self.package_requested.emit(tpl_id)
