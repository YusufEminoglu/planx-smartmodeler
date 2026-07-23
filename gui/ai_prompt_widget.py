"""Natural-language workflow prompt bar."""
from __future__ import annotations

from qgis.PyQt.QtCore import QEvent, Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)


class AiPromptWidget(QFrame):
    prompt_submitted = pyqtSignal(str, str)

    PRESET_PROMPTS = [
        "Example prompts...",
        "Buffer the active roads layer by 50 metres and dissolve overlaps",
        "Extract residential parcels, calculate area, then keep parcels over 1000 square metres",
        "Calculate slope from the DEM and classify it into planning suitability bands",
        "Clip all project vector layers to the study boundary",
    ]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("aiPromptPanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(14, 10, 14, 10)
        outer.setSpacing(7)

        top = QHBoxLayout()
        label = QLabel("AI WORKFLOW COPILOT")
        label.setObjectName("panelEyebrow")
        self.provider_label = QLabel("Offline")
        self.provider_label.setObjectName("providerPill")
        top.addWidget(label)
        top.addStretch()
        top.addWidget(self.provider_label)
        outer.addLayout(top)

        row = QHBoxLayout()
        self.prompt_edit = QPlainTextEdit()
        self.prompt_edit.setPlaceholderText(
            "Describe the GIS result you need. Include layers, fields, distances and outputs when known..."
        )
        self.prompt_edit.setFixedHeight(62)
        self.prompt_edit.installEventFilter(self)
        row.addWidget(self.prompt_edit, 1)

        right = QVBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Improve current", "improve")
        self.mode_combo.addItem("Build new", "new")
        self.mode_combo.currentIndexChanged.connect(self._update_mode_ui)
        right.addWidget(self.mode_combo)
        self.preset_combo = QComboBox()
        self.preset_combo.addItems(self.PRESET_PROMPTS)
        self.preset_combo.currentIndexChanged.connect(self._preset_selected)
        right.addWidget(self.preset_combo)
        self.generate_button = QPushButton()
        self.generate_button.setObjectName("primaryButton")
        self.generate_button.clicked.connect(self.submit_prompt)
        right.addWidget(self.generate_button)
        row.addLayout(right)
        outer.addLayout(row)

        self.hint = QLabel(
            "Ctrl+Enter to build. AI output is validated against installed QGIS "
            "algorithms before it reaches the canvas."
        )
        self.hint.setObjectName("mutedLabel")
        outer.addWidget(self.hint)
        self.set_workflow_available(False)

    def eventFilter(self, watched, event):
        if watched is self.prompt_edit and event.type() == QEvent.Type.KeyPress:
            enter_pressed = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
            if enter_pressed and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self.submit_prompt()
                return True
        return super().eventFilter(watched, event)

    def _preset_selected(self, index: int) -> None:
        if index > 0:
            self.prompt_edit.setPlainText(self.preset_combo.currentText())

    def submit_prompt(self) -> None:
        prompt = self.prompt_edit.toPlainText().strip()
        if prompt:
            self.prompt_submitted.emit(prompt, str(self.mode_combo.currentData()))

    def set_provider_name(self, name: str) -> None:
        self.provider_label.setText(name)

    def set_busy(self, busy: bool) -> None:
        self.generate_button.setEnabled(not busy)
        self.prompt_edit.setEnabled(not busy)
        self.mode_combo.setEnabled(not busy)
        self.preset_combo.setEnabled(not busy)
        if busy:
            self.generate_button.setText("Thinking...")
        else:
            self._update_mode_ui()

    def set_workflow_available(self, available: bool) -> None:
        mode = "improve" if available else "new"
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(mode))
        self._update_mode_ui()

    def _update_mode_ui(self, _index: int = -1) -> None:
        improve = self.mode_combo.currentData() == "improve"
        self.generate_button.setText(
            "Improve workflow" if improve else "Build workflow"
        )
        self.hint.setText(
            "Ctrl+Enter to improve the current canvas. Existing nodes, parameters "
            "and connections are sent as the editable baseline."
            if improve
            else
            "Ctrl+Enter to build a new workflow. AI output is validated against "
            "installed QGIS algorithms before it reaches the canvas."
        )
