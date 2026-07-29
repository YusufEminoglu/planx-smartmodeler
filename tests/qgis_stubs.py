"""Order-independent QGIS module stubs for pure-Python tests.

Each caller names the symbols it needs.  Existing modules are augmented rather
than replaced so a single pytest/unittest process remains deterministic no
matter which test module is imported first.
"""
from __future__ import annotations

import sys
import types
from collections.abc import Iterable


CORE_SYMBOLS = (
    "Qgis",
    "QgsApplication",
    "QgsFeatureRequest",
    "QgsProcessingParameterBoolean",
    "QgsProcessingParameterCrs",
    "QgsProcessingParameterDefinition",
    "QgsProcessingParameterEnum",
    "QgsProcessingParameterExtent",
    "QgsProcessingParameterFeatureSource",
    "QgsProcessingParameterField",
    "QgsProcessingParameterFile",
    "QgsProcessingParameterMapLayer",
    "QgsProcessingParameterMultipleLayers",
    "QgsProcessingParameterNumber",
    "QgsProcessingParameterRasterDestination",
    "QgsProcessingParameterRasterLayer",
    "QgsProcessingParameterString",
    "QgsProcessingParameterVectorDestination",
    "QgsProcessingParameterVectorLayer",
    "QgsProject",
    "QgsRasterLayer",
    "QgsVectorLayer",
)


def ensure_qgis_core(symbols: Iterable[str] = CORE_SYMBOLS):
    """Return a minimal ``qgis.core`` module containing every named symbol."""
    qgis_module = sys.modules.setdefault("qgis", types.ModuleType("qgis"))
    core_module = sys.modules.setdefault("qgis.core", types.ModuleType("qgis.core"))
    for name in symbols:
        if not hasattr(core_module, name):
            setattr(core_module, name, type(name, (), {}))
    qgis_module.core = core_module
    return qgis_module, core_module
