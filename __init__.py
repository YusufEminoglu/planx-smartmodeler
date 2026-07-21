"""QGIS plugin entry point for SmartModeler GIS (QGIS 4+)."""
from .main_plugin import SmartModelerPlugin


def classFactory(iface):
    """Instantiates the SmartModeler GIS plugin."""
    return SmartModelerPlugin(iface)
