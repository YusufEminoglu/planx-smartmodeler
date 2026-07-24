"""Reusable per-node parameter form backed by live QGIS Processing widgets.

Extracted from ``NodeParameterDialog`` so the same editors can be shown one
node at a time (double-click on the canvas) and all at once (the Run setup
sheet). Both surfaces must agree on what "configured" means, so the collection
and missing-input rules live here, once.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Tuple

from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QLineEdit,
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


class NodeParameterForm:
    """Builds, reads back and validates one node's parameter editors."""

    def __init__(self, node: NodeDefinition, dialog: QWidget, iface=None) -> None:
        self.node = node
        self._dialog = dialog
        self.editors: Dict[str, Tuple[QWidget, str]] = {}
        self.native_wrappers: Dict[str, Any] = {}
        self.native_values: Dict[str, Any] = {}
        self.processing_context = QgsProcessingContext()
        self.processing_context.setProject(QgsProject.instance())
        self.widget_context = QgsProcessingParameterWidgetContext()
        self.widget_context.setProject(QgsProject.instance())
        if iface is not None and iface.mapCanvas() is not None:
            self.widget_context.setMapCanvas(iface.mapCanvas())

    # -- construction ----------------------------------------------------

    def populate(self, form: QFormLayout, only_unconfigured: bool = False) -> int:
        """Add this node's editors to ``form``; returns how many were added.

        With ``only_unconfigured`` the form shows just the inputs that still
        need a decision, which is what the Run setup sheet wants: a long
        workflow should ask for the four things it is missing, not re-present
        every default of every node.
        """
        if self.node.algorithm_id.startswith("smart:"):
            self._add_smart_editor(form)
            return 1
        registry = QgsApplication.processingRegistry()
        algorithm = registry.algorithmById(self.node.algorithm_id)
        if algorithm is None:
            form.addRow(QLabel("This Processing algorithm is not available."))
            return 0
        added = 0
        for definition in algorithm.parameterDefinitions():
            hidden = definition.flags() & Qgis.ProcessingParameterFlag.Hidden
            if definition.isDestination() or hidden:
                continue
            if only_unconfigured and not self._needs_attention(definition):
                continue
            self._add_definition_editor(form, definition)
            added += 1
        self.finalize()
        return added

    def _needs_attention(self, definition: QgsProcessingParameterDefinition) -> bool:
        """Whether this parameter is a required input the user must still set."""
        name = definition.name()
        port = self.node.inputs.get(name)
        if port is None or port.is_connected() or not port.required:
            return False
        current = self.node.parameters.get(name, port.default_value)
        return not GraphModel.value_is_configured(current)

    def unconfigured_names(self) -> List[str]:
        """Names of required inputs that are neither connected nor set."""
        missing: List[str] = []
        if self.node.algorithm_id in ("smart:input_layer", "smart:raster_layer"):
            if not GraphModel.value_is_configured(self.node.parameters.get("LAYER")):
                missing.append("Project layer")
            return missing
        for port in self.node.inputs.values():
            if (
                port.required
                and not port.is_connected()
                and not GraphModel.value_is_configured(
                    self.node.parameters.get(port.port_id, port.default_value)
                )
            ):
                missing.append(port.name)
        return missing

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
        editor = self.layer_combo(socket_type, self.node.parameters.get("LAYER", ""))
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
            editor = self.layer_combo(socket_type, current)
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
            line = QLineEdit(self.display_value(current))
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
        wrapper.setDialog(self._dialog)
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

    def finalize(self) -> None:
        """Cross-wire the native wrappers, then seed their current values."""
        wrappers = list(self.native_wrappers.values())
        for wrapper in wrappers:
            wrapper.postInitialize(wrappers)
        for name, wrapper in self.native_wrappers.items():
            wrapper.setParameterValue(
                self.native_values.get(name), self.processing_context
            )

    # -- read back -------------------------------------------------------

    def collect(self) -> Dict[str, Any]:
        """Return the node's parameters merged with this form's edits."""
        parameters = dict(self.node.parameters)
        for name, wrapper in self.native_wrappers.items():
            value = self.normalise_native_value(wrapper.parameterValue())
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
        return parameters

    def missing_in(self, parameters: Dict[str, Any]) -> List[str]:
        """Required inputs still unset in ``parameters`` (a collect() result)."""
        missing: List[str] = []
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
        return missing

    def apply(self, parameters: Dict[str, Any]) -> None:
        self.node.parameters = parameters
        self.node.is_dirty = True

    # -- shared helpers --------------------------------------------------

    @staticmethod
    def layer_combo(socket_type: str, current: Any) -> QComboBox:
        combo = QComboBox()
        combo.addItem("Select a project layer...", "")
        choices = AlgorithmCatalog.layer_choices(socket_type)
        for layer_id, name in choices.items():
            combo.addItem(name, layer_id)
        index = combo.findData(str(current or ""))
        combo.setCurrentIndex(index if index >= 0 else 0)
        return combo

    @staticmethod
    def display_value(value: Any) -> str:
        if value in (None, ""):
            return ""
        if isinstance(value, (list, dict)):
            return json.dumps(value, ensure_ascii=False)
        return str(value)

    @staticmethod
    def normalise_native_value(value: Any) -> Any:
        if isinstance(value, QgsMapLayer):
            return value.id()
        if isinstance(value, (list, tuple)):
            return [
                item.id() if isinstance(item, QgsMapLayer) else item
                for item in value
            ]
        return value
