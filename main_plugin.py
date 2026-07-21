"""Main plugin class for SmartModeler GIS (QGIS 4+)."""
import os
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QToolBar
from qgis.core import QgsApplication
from .gui.modeler_window import SmartModelerWindow


class SmartModelerPlugin:
    """SmartModeler GIS QGIS 4 plugin manager."""

    def __init__(self, iface):
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action = None
        self.window = None

    def initGui(self):
        icon_path = os.path.join(self.plugin_dir, "icons", "icon.png")
        icon = QIcon(icon_path) if os.path.exists(icon_path) else QgsApplication.getThemeIcon("/mActionProcessingModeler.svg")

        self.action = QAction(
            icon,
            "SmartModeler GIS — Graphical Modeler Studio",
            self.iface.mainWindow()
        )
        self.action.setObjectName("SmartModelerAction")
        self.action.setStatusTip("Open next-generation QGIS 4 Graphical Modeler")
        self.action.triggered.connect(self.run)

        # Add to Processing menu and toolbar
        self.iface.addPluginToVectorMenu("SmartModeler GIS", self.action)
        self.iface.addVectorToolBarIcon(self.action)

    def unload(self):
        if self.action:
            self.iface.removePluginVectorMenu("SmartModeler GIS", self.action)
            self.iface.removeVectorToolBarIcon(self.action)
        if self.window:
            self.window.close()

    def run(self):
        if not self.window:
            self.window = SmartModelerWindow(self.iface, self.iface.mainWindow())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()
