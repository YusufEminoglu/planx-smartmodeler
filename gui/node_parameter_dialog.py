"""Compact parameter editor backed by live QGIS Processing definitions."""
from __future__ import annotations

import json
import math
from typing import Any, Dict, Tuple

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsMapLayer,
    QgsProcessingContext,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum,
    QgsProcessingParameterField,
    QgsProcessingParameterMapLayer,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsVectorLayer,
)
from qgis.gui import QgsGui, QgsProcessingParameterWidgetContext

from ..core.algorithm_catalog import AlgorithmCatalog
from ..core.graph_model import GraphModel, NodeDefinition, SocketType


class NodeParameterDialog(QDialog):
    """Edits literal values while connected inputs remain graph-controlled."""

    def __init__(
        self,
        node: NodeDefinition,
        parent=None,
        require_complete: bool = False,
    ) -> None:
        super().__init__(parent)
        self.node = node
        self.require_complete = require_complete
        self.editors: Dict[str, Tuple[QWidget, str]] = {}
        self.native_wrappers: Dict[str, Any] = {}
        self.native_values: Dict[str, Any] = {}
        self.processing_context = QgsProcessingContext()
        self.processing_context.setProject(QgsProject.instance())
        self.widget_context = QgsProcessingParameterWidgetContext()
        self.widget_context.setProject(QgsProject.instance())
        iface = getattr(parent, "iface", None)
        if iface is not None and iface.mapCanvas() is not None:
            self.widget_context.setMapCanvas(iface.mapCanvas())
        self.setWindowTitle(f"Configure - {node.title}")
        self.resize(560, 600)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        title = QLabel(self.node.title)
        title.setObjectName("dialogTitle")
        subtitle = QLabel(self.node.algorithm_id)
        subtitle.setObjectName("dialogSubtitle")
        subtitle.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        root.addWidget(title)
        root.addWidget(subtitle)

        self.title_edit = QLineEdit(self.node.title)
        root.addWidget(self.title_edit)

        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        panel = QWidget(scroll)
        form = QFormLayout(panel)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll.setWidget(panel)
        root.addWidget(scroll, 1)

        if self.node.algorithm_id.startswith("smart:"):
            self._add_smart_editor(form)
        else:
            registry = QgsApplication.processingRegistry()
            algorithm = registry.algorithmById(self.node.algorithm_id)
            if algorithm is None:
                form.addRow(QLabel("This Processing algorithm is not available."))
            else:
                for definition in algorithm.parameterDefinitions():
                    hidden = (
                        definition.flags()
                        & Qgis.ProcessingParameterFlag.Hidden
                    )
                    if definition.isDestination() or hidden:
                        continue
                    self._add_definition_editor(form, definition)
                self._finalize_native_wrappers()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _add_smart_editor(self, form: QFormLayout) -> None:
        if self.node.algorithm_id in ("smart:number", "smart:slider"):
            editor = QDoubleSpinBox()
            editor.setRange(-1.0e12, 1.0e12)
            editor.setDecimals(6)
            editor.setValue(float(self.node.parameters.get("VALUE", 0.0)))
            form.addRow("Value", editor)
            self.editors["VALUE"] = (editor, "number")
            return
        socket_type = (
            SocketType.RASTER
            if self.node.algorithm_id == "smart:raster_layer"
            else SocketType.VECTOR
        )
        editor = self._layer_combo(socket_type, self.node.parameters.get("LAYER", ""))
        form.addRow("Project layer", editor)
        self.editors["LAYER"] = (editor, "layer")

    def _add_definition_editor(
        self, form: QFormLayout, definition: QgsProcessingParameterDefinition
    ) -> None:
        name = definition.name()
        port = self.node.inputs.get(name)
        if port is not None and port.is_connected():
            label = QLabel("Connected from upstream node")
            label.setObjectName("connectedValue")
            form.addRow(definition.description(), label)
            return
        current = self.node.parameters.get(name, definition.defaultValue())
        if self._add_native_editor(form, definition, current):
            return
        editor: QWidget
        kind = "text"
        if isinstance(
            definition,
            (QgsProcessingParameterVectorLayer,
             QgsProcessingParameterRasterLayer, QgsProcessingParameterMapLayer),
        ):
            socket_type = AlgorithmCatalog.parameter_socket_type(definition)
            editor = self._layer_combo(socket_type, current)
            kind = "layer"
        elif isinstance(definition, QgsProcessingParameterBoolean):
            editor = QCheckBox()
            editor.setChecked(bool(current))
            kind = "boolean"
        elif isinstance(definition, QgsProcessingParameterNumber):
            spin = QDoubleSpinBox()
            minimum = definition.minimum()
            maximum = definition.maximum()
            spin.setRange(
                minimum if math.isfinite(minimum) else -1.0e12,
                maximum if math.isfinite(maximum) else 1.0e12,
            )
            spin.setDecimals(6)
            if current not in (None, ""):
                spin.setValue(float(current))
            editor = spin
            kind = "number"
        elif isinstance(definition, QgsProcessingParameterEnum) and not definition.allowMultiple():
            combo = QComboBox()
            combo.addItems(definition.options())
            if current not in (None, ""):
                combo.setCurrentIndex(max(0, int(current)))
            editor = combo
            kind = "enum"
        elif isinstance(definition, QgsProcessingParameterField):
            combo = QComboBox()
            combo.setEditable(True)
            fields = set()
            for layer in QgsProject.instance().mapLayers().values():
                if isinstance(layer, QgsVectorLayer):
                    fields.update(field.name() for field in layer.fields())
            combo.addItems(sorted(fields))
            combo.setCurrentText(str(current or ""))
            editor = combo
            kind = "field"
        else:
            line = QLineEdit(self._display_value(current))
            line.setPlaceholderText("Optional" if not port or not port.required else "Required")
            editor = line
        if definition.help():
            editor.setToolTip(definition.help())
        form.addRow(definition.description() or name, editor)
        self.editors[name] = (editor, kind)

    def _add_native_editor(
        self,
        form: QFormLayout,
        definition: QgsProcessingParameterDefinition,
        current: Any,
    ) -> bool:
        registry = QgsGui.processingGuiRegistry()
        if registry is None:
            return False
        wrapper = registry.createParameterWidgetWrapper(
            definition, Qgis.ProcessingMode.Standard
        )
        if wrapper is None:
            return False
        wrapper.setDialog(self)
        wrapper.setWidgetContext(self.widget_context)
        widget = wrapper.createWrappedWidget(self.processing_context)
        if widget is None:
            return False
        label = wrapper.createWrappedLabel()
        if label is None:
            label = QLabel(definition.description() or definition.name())
        if definition.help():
            widget.setToolTip(definition.help())
        form.addRow(label, widget)
        self.native_wrappers[definition.name()] = wrapper
        self.native_values[definition.name()] = current
        return True

    def _finalize_native_wrappers(self) -> None:
        wrappers = list(self.native_wrappers.values())
        for wrapper in wrappers:
            wrapper.postInitialize(wrappers)
        for name, wrapper in self.native_wrappers.items():
            wrapper.setParameterValue(
                self.native_values.get(name), self.processing_context
            )

    @staticmethod
    def _layer_combo(socket_type: str, current: Any) -> QComboBox:
        combo = QComboBox()
        combo.addItem("Select a project layer...", "")
        choices = AlgorithmCatalog.layer_choices(socket_type)
        for layer_id, name in choices.items():
            combo.addItem(name, layer_id)
        index = combo.findData(str(current or ""))
        combo.setCurrentIndex(index if index >= 0 else 0)
        return combo

    @staticmethod
    def _display_value(value: Any) -> str:
        if value in (None, ""):
            return ""
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    def _save(self) -> None:
        parameters = dict(self.node.parameters)
        for name, wrapper in self.native_wrappers.items():
            value = self._normalise_native_value(wrapper.parameterValue())
            if GraphModel.value_is_configured(value):
                parameters[name] = value
            else:
                parameters.pop(name, None)
        for name, (editor, kind) in self.editors.items():
            value: Any
            if kind == "layer":
                value = editor.currentData()
            elif kind == "boolean":
                value = editor.isChecked()
            elif kind == "number":
                value = editor.value()
            elif kind == "enum":
                value = editor.currentIndex()
            elif kind == "field":
                value = editor.currentText().strip()
            else:
                text = editor.text().strip()
                if text.startswith(("[", "{")):
                    try:
                        value = json.loads(text)
                    except json.JSONDecodeError:
                        value = text
                else:
                    value = text
            if GraphModel.value_is_configured(value):
                parameters[name] = value
            else:
                parameters.pop(name, None)

        missing = []
        if self.node.algorithm_id in ("smart:input_layer", "smart:raster_layer"):
            if not GraphModel.value_is_configured(parameters.get("LAYER")):
                missing.append("Project layer")
        for port in self.node.inputs.values():
            if (
                port.required
                and not port.is_connected()
                and not GraphModel.value_is_configured(
                    parameters.get(port.port_id, port.default_value)
                )
            ):
                missing.append(port.name)
        if self.require_complete and missing:
            QMessageBox.warning(
                self,
                "Required inputs are missing",
                "Configure these inputs before saving:\n\n"
                + "\n".join(f"- {name}" for name in missing),
            )
            return

        self.node.title = self.title_edit.text().strip() or self.node.title
        self.node.parameters = parameters
        self.node.is_dirty = True
        self.accept()

    @staticmethod
    def _normalise_native_value(value: Any) -> Any:
        if isinstance(value, QgsMapLayer):
            return value.id()
        if isinstance(value, (list, tuple)):
            return [
                item.id() if isinstance(item, QgsMapLayer) else item
                for item in value
            ]
        return value
