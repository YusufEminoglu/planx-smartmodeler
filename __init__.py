"""QGIS plugin entry point for SmartModeler GIS (QGIS 4+)."""


def classFactory(iface):
    """Instantiates the SmartModeler GIS plugin."""
    from .main_plugin import SmartModelerPlugin

    return SmartModelerPlugin(iface)
