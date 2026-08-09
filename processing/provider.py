"""Processing provider for SmartModeler's reviewed network utilities."""
from __future__ import annotations

import os

from qgis.PyQt.QtGui import QIcon
from qgis.core import QgsProcessingProvider

from .osm_download import (
    DownloadOsmLinesAlgorithm,
    DownloadOsmPointsAlgorithm,
    DownloadOsmPolygonsAlgorithm,
)
from .reference_extract import ExtractByReferenceAttributeAlgorithm


class SmartModelerProcessingProvider(QgsProcessingProvider):
    """Small first-party provider whose network surface is signature-pinned."""

    PROVIDER_ID = "smartmodeler"

    def id(self) -> str:
        return self.PROVIDER_ID

    def name(self) -> str:
        return "SmartModeler GIS"

    def longName(self) -> str:
        return "SmartModeler GIS — Reviewed Data Acquisition"

    def icon(self) -> QIcon:
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "icons", "icon.png")
        return QIcon(path)

    def loadAlgorithms(self) -> None:
        self.addAlgorithm(ExtractByReferenceAttributeAlgorithm())
        self.addAlgorithm(DownloadOsmPointsAlgorithm())
        self.addAlgorithm(DownloadOsmLinesAlgorithm())
        self.addAlgorithm(DownloadOsmPolygonsAlgorithm())
