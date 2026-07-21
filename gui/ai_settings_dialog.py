"""AI LLM API & Provider Settings Dialog for SmartModeler GIS (QGIS 4)."""
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QComboBox, QPushButton, QGroupBox, QMessageBox
)
from qgis.core import QgsSettings


class AiSettingsDialog(QDialog):
    """Configures LLM Provider (OpenAI, Gemini, Ollama) & API Keys for AI graph generation."""

    SETTINGS_PREFIX = "SmartModelerGIS/AI/"

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙️ SmartModeler AI Engine Settings")
        self.resize(480, 320)
        self.settings = QgsSettings()

        self.init_ui()
        self.load_settings()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # Provider Selector
        grp_provider = QGroupBox("AI Model Provider")
        p_layout = QVBoxLayout(grp_provider)

        lbl_prov = QLabel("Select Provider:")
        p_layout.addWidget(lbl_prov)

        self.cmb_provider = QComboBox()
        self.cmb_provider.addItems([
            "Built-in Heuristic Engine (Offline / No Key Needed)",
            "OpenAI API (GPT-4o / GPT-4o-mini)",
            "Google Gemini API (Gemini 1.5 Pro / Flash)",
            "Local Ollama Server (http://localhost:11434)"
        ])
        self.cmb_provider.currentIndexChanged.connect(self.on_provider_changed)
        p_layout.addWidget(self.cmb_provider)
        layout.addWidget(grp_provider)

        # API Keys & Endpoints Group
        self.grp_keys = QGroupBox("Provider Credentials & Endpoint")
        k_layout = QVBoxLayout(self.grp_keys)

        self.lbl_key = QLabel("API Key:")
        k_layout.addWidget(self.lbl_key)

        self.txt_key = QLineEdit()
        self.txt_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.txt_key.setPlaceholderText("Enter your API key (e.g. sk-proj-... or AIzaSy...)")
        k_layout.addWidget(self.txt_key)

        self.lbl_endpoint = QLabel("Server Endpoint URL:")
        k_layout.addWidget(self.lbl_endpoint)

        self.txt_endpoint = QLineEdit()
        self.txt_endpoint.setPlaceholderText("http://localhost:11434/api/generate")
        k_layout.addWidget(self.txt_endpoint)

        layout.addWidget(self.grp_keys)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_save = QPushButton("💾 Save Settings")
        btn_save.clicked.connect(self.save_settings)
        btn_cancel = QPushButton("Cancel")
        btn_cancel.clicked.connect(self.reject)

        btn_layout.addStretch()
        btn_layout.addWidget(btn_cancel)
        btn_layout.addWidget(btn_save)
        layout.addLayout(btn_layout)

    def on_provider_changed(self, idx: int):
        if idx == 0:
            self.grp_keys.setEnabled(False)
        elif idx in (1, 2):
            self.grp_keys.setEnabled(True)
            self.lbl_key.setVisible(True)
            self.txt_key.setVisible(True)
            self.lbl_endpoint.setVisible(False)
            self.txt_endpoint.setVisible(False)
        elif idx == 3:
            self.grp_keys.setEnabled(True)
            self.lbl_key.setVisible(False)
            self.txt_key.setVisible(False)
            self.lbl_endpoint.setVisible(True)
            self.txt_endpoint.setVisible(True)

    def load_settings(self):
        prov_idx = int(self.settings.value(self.SETTINGS_PREFIX + "provider_idx", 0))
        self.cmb_provider.setCurrentIndex(prov_idx)
        self.txt_key.setText(str(self.settings.value(self.SETTINGS_PREFIX + "api_key", "")))
        self.txt_endpoint.setText(str(self.settings.value(self.SETTINGS_PREFIX + "endpoint", "http://localhost:11434/api/generate")))
        self.on_provider_changed(prov_idx)

    def save_settings(self):
        self.settings.setValue(self.SETTINGS_PREFIX + "provider_idx", self.cmb_provider.currentIndex())
        self.settings.setValue(self.SETTINGS_PREFIX + "api_key", self.txt_key.text().strip())
        self.settings.setValue(self.SETTINGS_PREFIX + "endpoint", self.txt_endpoint.text().strip())
        QMessageBox.information(self, "Settings Saved", "AI Engine settings updated successfully.")
        self.accept()
