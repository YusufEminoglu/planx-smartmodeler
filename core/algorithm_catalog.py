"""Bridge between the visual graph and the live QGIS Processing registry."""
from __future__ import annotations

import json
import re
import math
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from qgis.core import (
    Qgis,
    QgsApplication,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterDefinition,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
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
from .agent.safe_algorithm_policy import default_policy


@dataclass(frozen=True)
class AlgorithmRecord:
    algorithm_id: str
    name: str
    group: str
    provider: str
    description: str = ""


class AlgorithmCatalog:
    """Discovers algorithms and creates correctly typed graph nodes."""

    # Workflow Studio planning and Agent Chat execution are different trust
    # boundaries.  The Agent's processing_run proposal stays behind the
    # deny-by-default SafeAlgorithmPolicy and its pinned live signatures.
    # Workflow Studio only drafts a reviewable graph, so it may additionally
    # use PlanX's application-owned, local Processing provider plus a small
    # set of explicitly reviewed native graph-building steps.
    AI_WORKFLOW_EXTRA_ALGORITHMS = frozenset(
        {
            "native:randomextract",
        }
    )
    # GDAL is part of that drafting surface. Excluding it made whole classes of
    # analysis impossible to draft: raster distance (`gdal:proximity`) has no
    # native equivalent at all, so "distance to roads" in a raster suitability
    # study could not be expressed. Its algorithms run through the same
    # Processing framework, destinations and approval click as the native ones;
    # the id terms and the id list below still refuse the side-effecting few.
    AI_WORKFLOW_TRUSTED_PREFIXES = ("native:", "qgis:", "gdal:", "planx:", "planx_")
    AI_BLOCKED_ID_TERMS = (
        "command",
        "download",
        "executesql",
        "execute_sql",
        "shell",
        "upload",
        "nominatim",
        "geocoder",
        "postgis",
        "spatialite",
    )
    AI_BLOCKED_ALGORITHM_IDS = frozenset(
        {
            "native:createdirectory",
            "native:layertobookmarks",
            "native:loadlayer",
            "native:setlayerstyle",
            "native:setprojectvariable",
            "qgis:setstyleforrasterlayer",
            "qgis:setstyleforvectorlayer",
            # The GDAL algorithms that edit their *input* instead of producing
            # an output. Everything else in the provider writes to a
            # destination the user chooses when they run the workflow; these
            # four change a file that is already on disk, which no drafted
            # graph should be able to do on the user's behalf.
            "gdal:assignprojection",
            "gdal:overviews",
            "gdal:rasterize_over",
            "gdal:rasterize_over_fixed_value",
        }
    )

    SMART_ALGORITHMS = {
        "smart:input_layer": ("Vector layer input", "Inputs", SocketType.VECTOR),
        "smart:raster_layer": ("Raster layer input", "Inputs", SocketType.RASTER),
        "smart:number": ("Numeric input", "Inputs", SocketType.NUMBER),
        "smart:slider": ("Numeric input", "Inputs", SocketType.NUMBER),
        "smart:boolean": ("Boolean input", "Inputs", SocketType.BOOLEAN),
        "smart:string": ("Text input", "Inputs", SocketType.STRING),
        "smart:field": ("Field input", "Inputs", SocketType.FIELD),
        "smart:crs": ("CRS input", "Inputs", SocketType.CRS),
        "smart:extent": ("Extent input", "Inputs", SocketType.EXTENT),
        "smart:enum": ("Enum input", "Inputs", SocketType.ENUM),
        "smart:map_layer": ("Map layer input", "Inputs", SocketType.ANY),
        "smart:multiple_vector": (
            "Vector layer collection",
            "Inputs",
            SocketType.VECTOR,
        ),
        "smart:multiple_raster": (
            "Raster layer collection",
            "Inputs",
            SocketType.RASTER,
        ),
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
        """Return whether Workflow Studio may place an algorithm in an AI graph.

        This is intentionally broader than Agent Chat's processing-run
        allowlist: a Workflow Studio graph is inert until the user runs it and
        every destination is forced through the normal Processing/output
        boundary.  Agent execution still re-checks ``SafeAlgorithmPolicy`` and
        cannot gain authority from this catalog predicate.
        """
        if algorithm_id in cls.SMART_ALGORITHMS:
            return True
        if not isinstance(algorithm_id, str):
            return False
        normalized = algorithm_id.lower().replace("-", "_")
        if (
            normalized in cls.AI_BLOCKED_ALGORITHM_IDS
            or any(term in normalized for term in cls.AI_BLOCKED_ID_TERMS)
        ):
            return False
        return (
            default_policy().record_for(algorithm_id) is not None
            or algorithm_id in cls.AI_WORKFLOW_EXTRA_ALGORITHMS
            or algorithm_id.startswith(cls.AI_WORKFLOW_TRUSTED_PREFIXES)
        )

    @classmethod
    def ai_parameter_value_allowed(
        cls, node: NodeDefinition, name: str, value: Any
    ) -> bool:
        """Validate provider-supplied literals without accepting paths or URIs."""
        if name not in node.inputs:
            if node.algorithm_id in ("smart:input_layer", "smart:raster_layer"):
                expected = (
                    SocketType.RASTER
                    if node.algorithm_id == "smart:raster_layer"
                    else SocketType.VECTOR
                )
                return (
                    name == "LAYER"
                    and isinstance(value, str)
                    and (not value or value in cls.layer_choices(expected))
                )
            if node.algorithm_id in ("smart:number", "smart:slider"):
                return (
                    name == "VALUE"
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                )
            if node.algorithm_id == "smart:boolean":
                return name == "VALUE" and isinstance(value, bool)
            if node.algorithm_id in (
                "smart:string",
                "smart:field",
                "smart:crs",
                "smart:extent",
            ):
                return (
                    name == "VALUE"
                    and isinstance(value, str)
                    and cls._safe_ai_text(value)
                )
            if node.algorithm_id == "smart:enum":
                values = value if isinstance(value, list) else [value]
                return (
                    name == "VALUE"
                    and bool(values)
                    and all(
                        isinstance(item, int) and not isinstance(item, bool)
                        for item in values
                    )
                )
            if node.algorithm_id in (
                "smart:map_layer",
                "smart:multiple_vector",
                "smart:multiple_raster",
            ):
                expected = (
                    SocketType.RASTER
                    if node.algorithm_id == "smart:multiple_raster"
                    else SocketType.VECTOR
                    if node.algorithm_id == "smart:multiple_vector"
                    else SocketType.ANY
                )
                values = value if isinstance(value, list) else [value]
                choices = cls.layer_choices(expected)
                return (
                    name == "LAYER"
                    and bool(values)
                    and all(
                        isinstance(item, str) and item in choices
                        for item in values
                    )
                )
            return False
        port = node.inputs[name]
        socket_type = port.socket_type
        if socket_type in (SocketType.VECTOR, SocketType.RASTER):
            expected = cls.layer_choices(socket_type)
            values = value if isinstance(value, list) else [value]
            if not values and port.allows_multiple:
                return True
            return all(isinstance(item, str) and item in expected for item in values)
        if socket_type == SocketType.FILE:
            return False
        if socket_type == SocketType.NUMBER:
            return (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        if socket_type == SocketType.BOOLEAN:
            return isinstance(value, bool)
        if socket_type == SocketType.ENUM:
            values = value if isinstance(value, list) else [value]
            return (
                bool(values)
                and len(values) <= 200
                and all(
                    isinstance(item, int)
                    and not isinstance(item, bool)
                    and 0 <= item <= 1_000_000_000
                    for item in values
                )
            )
        if socket_type in (SocketType.FIELD, SocketType.STRING):
            return isinstance(value, str) and cls._safe_ai_text(value)
        if value is None or isinstance(value, bool):
            return True
        if isinstance(value, (int, float)):
            return not isinstance(value, float) or math.isfinite(value)
        return isinstance(value, str) and cls._safe_ai_text(value)

    @staticmethod
    def _safe_ai_text(value: str) -> bool:
        text = value.strip()
        if len(text) > 2000 or "\x00" in text:
            return False
        lowered = text.lower()
        if "://" in lowered or lowered.startswith(("file:", "dbname=", "host=")):
            return False
        if re.match(r"^[a-zA-Z]:[\\/]", text) or text.startswith(("/", "\\\\")):
            return False
        return "\\" not in text and "../" not in text and "..\\" not in text

    @classmethod
    def create_node(
        cls,
        algorithm_id: str,
        node_id: Optional[str] = None,
        title: Optional[str] = None,
        configuration: Optional[Dict[str, Any]] = None,
    ) -> NodeDefinition:
        if algorithm_id in cls.SMART_ALGORITHMS:
            default_title, category, socket_type = cls.SMART_ALGORITHMS[algorithm_id]
            node = NodeDefinition(
                node_id=node_id,
                title=title or default_title,
                category=category,
                algorithm_id=algorithm_id,
            )
            if algorithm_id in ("smart:number", "smart:slider"):
                node.parameters["VALUE"] = 0.0
            elif algorithm_id == "smart:boolean":
                node.parameters["VALUE"] = False
            elif algorithm_id in (
                "smart:input_layer",
                "smart:raster_layer",
                "smart:map_layer",
                "smart:multiple_vector",
                "smart:multiple_raster",
            ):
                node.parameters["LAYER"] = ""
            elif algorithm_id == "smart:enum":
                node.parameters["VALUE"] = 0
            else:
                node.parameters["VALUE"] = ""
            node.add_output("OUTPUT", "Output", socket_type)
            return node

        registry = QgsApplication.processingRegistry()
        algorithm = (
            registry.createAlgorithmById(algorithm_id, configuration or {})
            if registry is not None
            else None
        )
        if algorithm is None:
            raise ValueError(f"Processing algorithm is not available: {algorithm_id}")

        node = NodeDefinition(
            node_id=node_id,
            title=title or algorithm.displayName(),
            category=algorithm.group() or "Processing",
            algorithm_id=algorithm_id,
            description=algorithm.shortDescription() or "",
        )
        node.algorithm_configuration = dict(configuration or {})
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
        if isinstance(definition, QgsProcessingParameterCrs):
            return SocketType.CRS
        if isinstance(definition, QgsProcessingParameterExtent):
            return SocketType.EXTENT
        if isinstance(definition, QgsProcessingParameterEnum):
            return SocketType.ENUM
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
            allowed = records_by_id.get(record.algorithm_id)
            if allowed is None:
                continue
            if allowed.algorithm_id not in seen:
                selected.append(allowed)
                seen.add(allowed.algorithm_id)
            if len(selected) >= max(limit, len(seen)):
                break
        for record in selected:
            try:
                node = cls.create_node(record.algorithm_id)
                registry = QgsApplication.processingRegistry()
                algorithm = (
                    registry.algorithmById(record.algorithm_id)
                    if registry is not None
                    and record.algorithm_id not in cls.SMART_ALGORITHMS
                    else None
                )
                definitions = {
                    definition.name(): definition
                    for definition in (
                        algorithm.parameterDefinitions()
                        if algorithm is not None
                        else ()
                    )
                }
                inputs = ", ".join(
                    cls._compact_ai_input_signature(
                        node, port, definitions.get(port.port_id)
                    )
                    for port in node.inputs.values()
                )
                outputs = ", ".join(
                    f"{port.port_id}:{port.socket_type}"
                    for port in node.outputs.values()
                )
                lines.append(
                    f"- {record.algorithm_id} | {record.name} | "
                    f"inputs=[{inputs}] | outputs=[{outputs}]"
                )
            except (RuntimeError, ValueError):
                continue
        return "\n".join(lines)

    @classmethod
    def _compact_ai_input_signature(
        cls,
        node: NodeDefinition,
        port: Any,
        definition: Any,
    ) -> str:
        """Describe one input with bounded enum meanings and safe defaults.

        Enum indices are otherwise opaque to a provider (``METHOD=0`` versus
        ``METHOD=1``), which made valid-looking AI graphs silently choose
        percentage mode instead of feature-count mode. Option labels are
        JSON-escaped, whitespace-normalized, length-bounded untrusted metadata.
        """
        details: Dict[str, Any] = {}
        if isinstance(definition, QgsProcessingParameterEnum):
            try:
                raw_options = list(definition.options())
            except (AttributeError, TypeError):
                raw_options = []
            option_labels = []
            for index, option in enumerate(raw_options[:30]):
                clean_option = re.sub(r"\s+", " ", str(option)).strip()[:120]
                option_labels.append(f"{index}:{clean_option}")
            details["options"] = option_labels
        default = node.parameters.get(port.port_id)
        if port.socket_type in (
            SocketType.NUMBER,
            SocketType.BOOLEAN,
            SocketType.ENUM,
            SocketType.STRING,
        ) and GraphModel.value_is_configured(default):
            if not isinstance(default, str) or cls._safe_ai_text(default):
                details["default"] = default
        suffix = (
            json.dumps(details, ensure_ascii=False, separators=(",", ":"))
            if details
            else ""
        )
        return f"{port.port_id}:{port.socket_type}{suffix}"

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
