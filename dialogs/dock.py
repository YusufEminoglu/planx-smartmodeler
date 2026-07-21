# -*- coding: utf-8 -*-
"""Dockable widget hosting categorized tool buttons.

Pattern: each button is a checkable QToolButton; a QButtonGroup ensures
single-selection across categories. Click emits `tool_activated(key)`
which main_plugin maps to a QgsMapTool.
"""
from __future__ import annotations

import os

from qgis.PyQt.QtCore import Qt, QSize, pyqtSignal
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import (
    QButtonGroup,
    QDockWidget,
    QGridLayout,
    QGroupBox,
    QLabel,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


_BUTTON_QSS = """
QToolButton {
    border: 1px solid transparent;
    border-radius: 6px;
    background: transparent;
    padding: 4px;
}
QToolButton:hover  { background: #e8eaf6; border: 1px solid #c5cae9; }
QToolButton:checked{ background: #c5cae9; border: 1px solid #7986cb; }
"""


class ToolButton(QToolButton):
    def __init__(self, icon_path: str, tooltip: str, parent=None):
        super().__init__(parent)
        if os.path.exists(icon_path):
            self.setIcon(QIcon(icon_path))
        self.setToolTip(tooltip)
        self.setIconSize(QSize(28, 28))
        self.setFixedSize(44, 44)
        self.setCheckable(True)
        self.setStyleSheet(_BUTTON_QSS)


# Edit this to add/rename tool categories and buttons. Each tuple is
# (key, icon_filename, tooltip). The key is what main_plugin._create_tool
# receives.
CATEGORIES: dict[str, list[tuple[str, str, str]]] = {
    "Çizim": [
        ("draw_line", "icon.png", "Çizgi çiz"),
        ("draw_poly", "icon.png", "Poligon çiz"),
    ],
    "Düzenleme": [
        ("edit_move", "icon.png", "Taşı"),
    ],
}


class PluginDockWidget(QDockWidget):
    tool_activated = pyqtSignal(str)

    def __init__(self, iface, icon_dir: str, parent=None):
        super().__init__("{{PLUGIN_NAME}}", parent)
        self.iface = iface
        self.icon_dir = icon_dir
        self.setAllowedAreas(Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea)
        self._button_group = QButtonGroup(self)
        self._button_group.setExclusive(True)
        self._build_ui()

    def _build_ui(self) -> None:
        container = QWidget()
        root = QVBoxLayout(container)
        root.setContentsMargins(8, 8, 8, 8)

        for cat_name, items in CATEGORIES.items():
            box = QGroupBox(cat_name)
            grid = QGridLayout(box)
            grid.setSpacing(4)
            for idx, (key, icon_name, tip) in enumerate(items):
                btn = ToolButton(os.path.join(self.icon_dir, icon_name), tip, box)
                btn.clicked.connect(lambda _checked, k=key: self.tool_activated.emit(k))
                self._button_group.addButton(btn)
                grid.addWidget(btn, idx // 6, idx % 6)
            root.addWidget(box)

        self.status = QLabel("Hazır")
        self.status.setStyleSheet("color: #555; padding: 4px;")
        root.addWidget(self.status)
        root.addStretch(1)
        self.setWidget(container)

    def set_status(self, text: str) -> None:
        self.status.setText(text)
