"""PyQt6 AI Prompt Bar widget for SmartModeler GIS (QGIS 4)."""
from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.PyQt.QtWidgets import (
    QFrame, QHBoxLayout, QLineEdit, QPushButton, QLabel, QComboBox
)


class AiPromptWidget(QFrame):
    """Natural language AI prompt input bar for auto-generating visual graphs."""

    prompt_submitted = pyqtSignal(str)

    PRESET_PROMPTS = [
        "Select a sample prompt...",
        "Create 15-minute urban isochrone walkability model with population stats",
        "Generate 3D building massing extrusion from footprint height field",
        "Build MCDA land suitability overlay model using DEM slope",
        "Buffer roads by 50m and intersect with protected forest polygons"
    ]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QFrame {
                background-color: #1A1D24;
                border-bottom: 1px solid #00E5FF;
                padding: 6px;
            }
            QLabel {
                color: #00E5FF;
                font-weight: bold;
                font-size: 11px;
            }
            QLineEdit {
                background-color: #232731;
                color: #FFFFFF;
                border: 1px solid #37474F;
                border-radius: 4px;
                padding: 5px 8px;
                font-size: 11px;
            }
            QLineEdit:focus {
                border: 1px solid #00E5FF;
            }
            QComboBox {
                background-color: #232731;
                color: #B0BEC5;
                border: 1px solid #37474F;
                border-radius: 4px;
                padding: 4px 6px;
                font-size: 11px;
            }
            QPushButton {
                background-color: #00E5FF;
                color: #12141C;
                font-weight: bold;
                border-radius: 4px;
                padding: 5px 12px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #18FFFF;
            }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 4, 10, 4)
        layout.setSpacing(10)

        lbl_icon = QLabel("🤖 AI Model Assistant:")
        layout.addWidget(lbl_icon)

        self.txt_prompt = QLineEdit()
        self.txt_prompt.setPlaceholderText("Describe your GIS workflow prompt (e.g., 'Buffer buildings by 20m and clip with boundary')...")
        self.txt_prompt.returnPressed.connect(self.submit_prompt)
        layout.addWidget(self.txt_prompt, 2)

        self.cmb_presets = QComboBox()
        self.cmb_presets.addItems(self.PRESET_PROMPTS)
        self.cmb_presets.currentIndexChanged.connect(self.on_preset_selected)
        layout.addWidget(self.cmb_presets, 1)

        self.btn_generate = QPushButton("✨ Generate Graph")
        self.btn_generate.clicked.connect(self.submit_prompt)
        layout.addWidget(self.btn_generate)

    def on_preset_selected(self, index: int):
        if index > 0:
            self.txt_prompt.setText(self.cmb_presets.currentText())

    def submit_prompt(self):
        text = self.txt_prompt.text().strip()
        if text:
            self.prompt_submitted.emit(text)
