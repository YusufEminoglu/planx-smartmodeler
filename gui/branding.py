"""Small, shared SmartModeler branding widgets."""
from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QPixmap
from qgis.PyQt.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout


def build_brand_header(
    parent,
    title: str,
    subtitle: str,
    badge: str,
) -> QFrame:
    """Create the compact branded header shared by the main studio and dock."""
    frame = QFrame(parent)
    frame.setObjectName("brandHeader")
    frame.setAccessibleName(f"SmartModeler {title} header")

    row = QHBoxLayout(frame)
    row.setContentsMargins(14, 11, 14, 11)
    row.setSpacing(11)

    mark = QLabel()
    mark.setObjectName("brandIcon")
    mark.setAccessibleName("SmartModeler workflow icon")
    mark.setFixedSize(48, 48)
    icon_path = Path(__file__).resolve().parent.parent / "icons" / "icon.png"
    pixmap = QPixmap(str(icon_path))
    if not pixmap.isNull():
        mark.setPixmap(
            pixmap.scaled(
                42,
                42,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
    row.addWidget(mark)

    copy = QVBoxLayout()
    copy.setSpacing(1)
    eyebrow = QLabel("PLANX  /  SMARTMODELER GIS")
    eyebrow.setObjectName("brandEyebrow")
    copy.addWidget(eyebrow)
    heading = QLabel(title)
    heading.setObjectName("brandTitle")
    copy.addWidget(heading)
    detail = QLabel(subtitle)
    detail.setObjectName("brandSubtitle")
    detail.setWordWrap(True)
    copy.addWidget(detail)
    row.addLayout(copy, 1)

    badge_label = QLabel(badge)
    badge_label.setObjectName("brandBadge")
    badge_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
    badge_label.setAccessibleName(f"SmartModeler status: {badge}")
    row.addWidget(badge_label, 0, Qt.AlignmentFlag.AlignVCenter)
    return frame
