"""Bridge between the visual graph and the live QGIS Processing registry."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterMapLayer,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterDestination,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorDestination,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsRasterLayer,
    QgsVectorLayer,
)

from .graph_model import GraphModel, NodeDefinition, SocketType


@dataclass(frozen=True)
class AlgorithmRecord:
    algorithm_id: str
    name: str
    group: str
    provider: str
    description: str = ""


class AlgorithmCatalog:
    """Discovers algorithms and creates correctly typed graph nodes."""

    AI_BLOCKED_ID_TERMS = (
        "command",
        "download",
        "executesql",
        "execute_sql",
        "shell",
    )

    SMART_ALGORITHMS = {
        "smart:input_layer": ("Vector layer input", "Inputs", SocketType.VECTOR),
        "smart:raster_layer": ("Raster layer input", "Inputs", SocketType.RASTER),
        "smart:number": ("Numeric input", "Inputs", SocketType.NUMBER),
        "smart:slider": ("Numeric input", "Inputs", SocketType.NUMBER),
    }

    @classmethod
    def records(cls) -> List[AlgorithmRecord]:
        records = [
            AlgorithmRecord(key, value[0], value[1], "SmartModeler")
            for key, value in cls.SMART_ALGORITHMS.items()
            if key != "smart:slider"
        ]
        registry = QgsApplication.processingRegistry()
        if registry is None:
            return records
        for algorithm in registry.algorithms():
            provider = algorithm.provider()
            provider_name = provider.name() if provider is not None else "Processing"
            records.append(
                AlgorithmRecord(
                    algorithm.id(),
                    algorithm.displayName(),
                    algorithm.group() or provider_name,
                    provider_name,
                    algorithm.shortDescription() or "",
                )
            )
        return sorted(records, key=lambda item: (item.provider, item.group, item.name))

    @classmethod
    def algorithm_exists(cls, algorithm_id: str) -> bool:
        if algorithm_id in cls.SMART_ALGORITHMS:
            return True
        registry = QgsApplication.processingRegistry()
        return bool(registry and registry.algorithmById(algorithm_id))

    @classmethod
    def ai_algorithm_allowed(cls, algorithm_id: str) -> bool:
        """Exclude Processing actions which can bypass the planner trust boundary."""
        normalized = algorithm_id.lower().replace("-", "_")
        return not any(term in normalized for term in cls.AI_BLOCKED_ID_TERMS)

    @classmethod
    def create_node(
        cls,
        algorithm_id: str,
        node_id: Optional[str] = None,
        title: Optional[str] = None,
    ) -> NodeDefinition:
        if algorithm_id in cls.SMART_ALGORITHMS:
            default_title, category, socket_type = cls.SMART_ALGORITHMS[algorithm_id]
            node = NodeDefinition(
                node_id=node_id,
                title=title or default_title,
                category=category,
                algorithm_id=algorithm_id,
            )
            if socket_type == SocketType.NUMBER:
                node.parameters["VALUE"] = 0.0
            else:
                node.parameters["LAYER"] = ""
            node.add_output("OUTPUT", "Output", socket_type)
            return node

        registry = QgsApplication.processingRegistry()
        algorithm = registry.algorithmById(algorithm_id) if registry is not None else None
        if algorithm is None:
            raise ValueError(f"Processing algorithm is not available: {algorithm_id}")

        node = NodeDefinition(
            node_id=node_id,
            title=title or algorithm.displayName(),
            category=algorithm.group() or "Processing",
            algorithm_id=algorithm_id,
            description=algorithm.shortDescription() or "",
        )
        for definition in algorithm.parameterDefinitions():
            if definition.flags() & Qgis.ProcessingParameterFlag.Hidden:
                continue
            if definition.isDestination():
                continue
            default = definition.defaultValue()
            required = not bool(
                definition.flags() & Qgis.ProcessingParameterFlag.Optional
            ) and not GraphModel.value_is_configured(default)
            node.add_input(
                definition.name(),
                definition.description() or definition.name(),
                cls.parameter_socket_type(definition),
                default_value=default,
                required=required,
                allows_multiple=isinstance(definition, QgsProcessingParameterMultipleLayers),
                description=definition.help() or "",
            )
            if GraphModel.value_is_configured(default):
                node.parameters[definition.name()] = default

        seen_outputs = set()
        for output in algorithm.outputDefinitions():
            node.add_output(
                output.name(),
                output.description() or output.name(),
                cls.output_socket_type(output),
            )
            seen_outputs.add(output.name())
        for definition in algorithm.destinationParameterDefinitions():
            if definition.name() not in seen_outputs:
                node.add_output(
                    definition.name(),
                    definition.description() or definition.name(),
                    cls.parameter_socket_type(definition),
                )
        if not node.outputs:
            node.add_output("OUTPUT", "Result", SocketType.ANY)
        return node

    @staticmethod
    def parameter_socket_type(definition: QgsProcessingParameterDefinition) -> str:
        if isinstance(definition, QgsProcessingParameterMultipleLayers):
            if definition.layerType() == Qgis.ProcessingSourceType.Raster:
                return SocketType.RASTER
            if definition.layerType() in (
                Qgis.ProcessingSourceType.Vector,
                Qgis.ProcessingSourceType.VectorAnyGeometry,
                Qgis.ProcessingSourceType.VectorPoint,
                Qgis.ProcessingSourceType.VectorLine,
                Qgis.ProcessingSourceType.VectorPolygon,
            ):
                return SocketType.VECTOR
            return SocketType.ANY
        if isinstance(
            definition,
            (
                QgsProcessingParameterFeatureSource,
                QgsProcessingParameterVectorLayer,
                QgsProcessingParameterVectorDestination,
            ),
        ):
            return SocketType.VECTOR
        if isinstance(
            definition, (QgsProcessingParameterRasterLayer, QgsProcessingParameterRasterDestination)
        ):
            return SocketType.RASTER
        if isinstance(definition, QgsProcessingParameterNumber):
            return SocketType.NUMBER
        if isinstance(definition, QgsProcessingParameterBoolean):
            return SocketType.BOOLEAN
        if isinstance(definition, QgsProcessingParameterField):
            return SocketType.FIELD
        if isinstance(definition, QgsProcessingParameterFile):
            return SocketType.FILE
        if isinstance(definition, QgsProcessingParameterMapLayer):
            return SocketType.ANY
        if isinstance(definition, QgsProcessingParameterString):
            return SocketType.STRING
        return SocketType.ANY

    @classmethod
    def autobind_unique_project_layers(cls, graph: GraphModel) -> int:
        """Bind unambiguous project-layer inputs without guessing among choices."""
        bound = 0
        for node in graph.nodes.values():
            if node.algorithm_id in ("smart:input_layer", "smart:raster_layer"):
                if GraphModel.value_is_configured(node.parameters.get("LAYER")):
                    continue
                socket_type = (
                    SocketType.RASTER
                    if node.algorithm_id == "smart:raster_layer"
                    else SocketType.VECTOR
                )
                choices = cls.layer_choices(socket_type)
                if len(choices) == 1:
                    node.parameters["LAYER"] = next(iter(choices))
                    node.is_dirty = True
                    bound += 1
                continue

            for port in node.inputs.values():
                if (
                    not port.required
                    or port.is_connected()
                    or GraphModel.value_is_configured(
                        node.parameters.get(port.port_id, port.default_value)
                    )
                    or port.socket_type not in (SocketType.VECTOR, SocketType.RASTER)
                ):
                    continue
                choices = cls.layer_choices(port.socket_type)
                if len(choices) != 1:
                    continue
                layer_id = next(iter(choices))
                node.parameters[port.port_id] = [layer_id] if port.allows_multiple else layer_id
                node.is_dirty = True
                bound += 1
        return bound

    @staticmethod
    def output_socket_type(output: Any) -> str:
        type_name = output.__class__.__name__.lower()
        if "raster" in type_name:
            return SocketType.RASTER
        if "vector" in type_name or "feature" in type_name:
            return SocketType.VECTOR
        if "number" in type_name or "distance" in type_name:
            return SocketType.NUMBER
        if "boolean" in type_name:
            return SocketType.BOOLEAN
        if "string" in type_name:
            return SocketType.STRING
        if "file" in type_name or "folder" in type_name:
            return SocketType.FILE
        return SocketType.ANY

    @classmethod
    def relevant_records(cls, prompt: str, limit: int = 50) -> List[AlgorithmRecord]:
        terms = {
            term
            for term in re.findall(r"[a-z0-9_]+", prompt.lower())
            if len(term) > 2
        }
        scored = []
        for record in cls.records():
            if not cls.ai_algorithm_allowed(record.algorithm_id):
                continue
            haystack = " ".join(
                (record.algorithm_id, record.name, record.group, record.description)
            ).lower()
            score = sum(3 if term in record.algorithm_id.lower()
                        else 1 for term in terms if term in haystack)
            if score or record.provider == "SmartModeler":
                scored.append((score, record))
        scored.sort(key=lambda pair: (-pair[0], pair[1].name))
        return [record for _score, record in scored[:limit]]

    @classmethod
    def compact_ai_catalog(
        cls,
        prompt: str,
        limit: int = 50,
        required_ids: Iterable[str] = (),
    ) -> str:
        lines = []
        records_by_id = {
            record.algorithm_id: record
            for record in cls.records()
            if cls.ai_algorithm_allowed(record.algorithm_id)
        }
        selected = []
        seen = set()
        for algorithm_id in required_ids:
            record = records_by_id.get(algorithm_id)
            if record is not None and record.algorithm_id not in seen:
                selected.append(record)
                seen.add(record.algorithm_id)
        for record in cls.relevant_records(prompt, limit):
            if record.algorithm_id not in seen:
                selected.append(record)
                seen.add(record.algorithm_id)
            if len(selected) >= max(limit, len(seen)):
                break
        for record in selected:
            try:
                node = cls.create_node(record.algorithm_id)
                inputs = ", ".join(
                    f"{port.port_id}:{port.socket_type}" for port in node.inputs.values()
                )
                outputs = ", ".join(
                    f"{port.port_id}:{port.socket_type}" for port in node.outputs.values()
                )
                lines.append(
                    f"- {record.algorithm_id} | {record.name} | inputs=[{inputs}] | outputs=[{outputs}]"
                )
            except (RuntimeError, ValueError):
                continue
        return "\n".join(lines)

    @staticmethod
    def project_context() -> str:
        project = QgsProject.instance()
        if project is None or not project.mapLayers():
            return "No layers are currently loaded in the QGIS project."
        lines = []
        for layer in project.mapLayers().values():
            layer_type = "vector" if isinstance(layer, QgsVectorLayer) else "raster" if isinstance(
                layer, QgsRasterLayer) else "other"
            crs = layer.crs().authid() if layer.crs().isValid() else "unknown CRS"
            fields = ""
            if isinstance(layer, QgsVectorLayer):
                fields = ", fields=" + ",".join(field.name() for field in layer.fields())
            lines.append(
                f"- id={layer.id()}, name={layer.name()}, type={layer_type}, crs={crs}{fields}"
            )
        return "\n".join(lines)

    @staticmethod
    def layer_choices(socket_type: str = SocketType.ANY) -> Dict[str, str]:
        choices: Dict[str, str] = {}
        project = QgsProject.instance()
        if project is None:
            return choices
        for layer in project.mapLayers().values():
            if socket_type == SocketType.VECTOR and not isinstance(layer, QgsVectorLayer):
                continue
            if socket_type == SocketType.RASTER and not isinstance(layer, QgsRasterLayer):
                continue
            choices[layer.id()] = layer.name()
        return choices
