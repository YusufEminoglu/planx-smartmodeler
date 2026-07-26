"""SmartModeler JSON and native QGIS .model3 serialization."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional, Tuple

from qgis.PyQt.QtCore import QPointF
from qgis.core import (
    Qgis,
    QgsApplication,
    QgsProcessing,
    QgsProcessingModelAlgorithm,
    QgsProcessingModelChildAlgorithm,
    QgsProcessingModelChildDependency,
    QgsProcessingModelChildParameterSource,
    QgsProcessingModelOutput,
    QgsProcessingModelParameter,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterCrs,
    QgsProcessingParameterEnum,
    QgsProcessingParameterExtent,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterMapLayer,
    QgsProcessingParameterMultipleLayers,
    QgsProcessingParameterNumber,
    QgsProcessingParameterRasterLayer,
    QgsProcessingParameters,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
)

from .algorithm_catalog import AlgorithmCatalog
from .document_codec import DocumentCodecError, GraphDocumentCodec
from .graph_model import GraphModel, NodeDefinition

# Scoped on purpose: QGIS 4/Qt6 requires the scoped form, and the enum lives on
# QgsProcessing (not Qgis) on both QGIS 3.44 LTR and QGIS 4.2 -- verified on
# both runtimes rather than assumed.
_PYTHON_ALGORITHM_SUBCLASS = (
    QgsProcessing.PythonOutputType.PythonQgsProcessingAlgorithmSubclass
)


class Model3Serializer:
    """Round-trips internal JSON and bridges to QGIS' native model API."""

    FORMAT = GraphDocumentCodec.FORMAT

    @classmethod
    def export_to_json(cls, graph: GraphModel) -> str:
        return GraphDocumentCodec.encode(graph)

    @classmethod
    def import_from_json(cls, json_str: str) -> Optional[GraphModel]:
        try:
            return GraphDocumentCodec.decode(json_str, AlgorithmCatalog.create_node)
        except (DocumentCodecError, TypeError, ValueError):
            return None

    @classmethod
    def build_native_model(
        cls, graph: GraphModel, promote_missing_inputs: bool = True
    ) -> Tuple[Optional[QgsProcessingModelAlgorithm], str, list]:
        """Build the QGIS-native model for ``graph``.

        Returns ``(model, fatal_error, issues)``. ``fatal_error`` is non-empty
        only when no model could be built at all (an unavailable algorithm);
        ``issues`` are QGIS' own validation messages, which are advisory: a
        model with unconfigured inputs is still perfectly writable, exactly as
        it is in QGIS' own Model Designer.

        With ``promote_missing_inputs`` a required child parameter that has
        neither an upstream connection nor a usable literal is turned into a
        **model input** rather than being left unset. That is what makes an
        exported half-configured workflow useful: opening it in QGIS asks for
        the layer instead of refusing to run.
        """
        for contract in graph.outputs.values():
            node = graph.nodes.get(str(contract.get("node_id", "")))
            output_name = str(contract.get("output_name", ""))
            if node is None or not graph.output_is_publishable(node, output_name):
                return (
                    None,
                    "Published results must reference a Processing layer output.",
                    [],
                )
        model = QgsProcessingModelAlgorithm(graph.name, "SmartModeler GIS", "smartmodeler")
        model.setHelpContent({"ALG_DESC": graph.description})
        child_nodes: Dict[str, QgsProcessingModelChildAlgorithm] = {}
        taken_names = set()

        for node in graph.nodes.values():
            if node.algorithm_id.startswith("smart:"):
                definition = cls._model_parameter_for_node(node)
                component = QgsProcessingModelParameter(node.node_id)
                component.setDescription(node.title)
                component.setPosition(QPointF(node.x, node.y))
                model.addModelParameter(definition, component)
                taken_names.add(node.node_id)
                continue
            if not AlgorithmCatalog.algorithm_exists(node.algorithm_id):
                return None, f"Algorithm is unavailable: {node.algorithm_id}", []
            child = QgsProcessingModelChildAlgorithm(node.algorithm_id)
            child.setChildId(node.node_id)
            child.setDescription(node.title)
            child.setPosition(QPointF(node.x, node.y))
            child.setActive(node.is_active)
            child.setConfiguration(node.algorithm_configuration)
            child.setDependencies(
                [
                    QgsProcessingModelChildDependency(
                        dependency,
                        node.dependency_branches.get(dependency, ""),
                    )
                    for dependency in node.dependencies
                ]
            )
            child_nodes[node.node_id] = child

        registry = QgsApplication.processingRegistry()
        for node_id, child in child_nodes.items():
            node = graph.nodes[node_id]
            algorithm = registry.createAlgorithmById(
                node.algorithm_id, node.algorithm_configuration
            )
            for input_name, port in node.inputs.items():
                incoming = [
                    edge
                    for edge in graph.incoming_edges(node_id)
                    if edge.end_port_id == input_name
                ]
                sources = []
                for edge in incoming:
                    source_node = graph.nodes[edge.start_node_id]
                    if source_node.algorithm_id.startswith("smart:"):
                        sources.append(
                            QgsProcessingModelChildParameterSource.fromModelParameter(
                                source_node.node_id
                            )
                        )
                    else:
                        sources.append(
                            QgsProcessingModelChildParameterSource.fromChildOutput(
                                edge.start_node_id, edge.start_port_id
                            )
                        )
                definition = (
                    algorithm.parameterDefinition(input_name)
                    if algorithm is not None
                    else None
                )
                has_source_order = input_name in node.parameter_source_order
                if has_source_order:
                    sources = []
                    for ordered in node.parameter_source_order[input_name]:
                        if ordered.get("kind") == "static":
                            sources.append(
                                QgsProcessingModelChildParameterSource.fromStaticValue(
                                    ordered.get("value")
                                )
                            )
                            continue
                        source_node = graph.nodes.get(ordered.get("node_id"))
                        if source_node is None:
                            continue
                        if source_node.algorithm_id.startswith("smart:"):
                            sources.append(
                                QgsProcessingModelChildParameterSource.fromModelParameter(
                                    source_node.node_id
                                )
                            )
                        else:
                            sources.append(
                                QgsProcessingModelChildParameterSource.fromChildOutput(
                                    source_node.node_id,
                                    str(ordered.get("output_name", "")),
                                )
                            )
                if (
                    not has_source_order
                    and port.allows_multiple
                    and input_name in node.parameters
                ):
                    literal = node.parameters[input_name]
                    values = literal if isinstance(literal, list) else [literal]
                    literal_sources = []
                    for value in values:
                        if definition is None or definition.checkValueIsAcceptable(
                            [value]
                        ):
                            literal_sources.append(
                                QgsProcessingModelChildParameterSource.fromStaticValue(
                                    value
                                ),
                            )
                    sources = literal_sources + sources
                # A literal the algorithm itself rejects is worse than no
                # literal: it makes the whole exported model invalid and the
                # message ("Value for X is not acceptable") names the parameter
                # rather than the value. Drop it and fall through to the
                # promotion below, which produces a model the user can fill in.
                if (
                    not sources
                    and not has_source_order
                    and not port.allows_multiple
                    and input_name in node.parameters
                ):
                    value = node.parameters[input_name]
                    if definition is None or definition.checkValueIsAcceptable(value):
                        sources = [
                            QgsProcessingModelChildParameterSource.fromStaticValue(value)
                        ]
                if (
                    not sources
                    and promote_missing_inputs
                    and port.required
                    and definition is not None
                ):
                    parameter_name = cls._unique_parameter_name(
                        f"{node_id}_{input_name}", taken_names
                    )
                    if cls._add_promoted_parameter(
                        model, definition, parameter_name, node, input_name
                    ):
                        taken_names.add(parameter_name)
                        sources = [
                            QgsProcessingModelChildParameterSource.fromModelParameter(
                                parameter_name
                            )
                        ]
                if sources:
                    child.addParameterSources(input_name, sources)

            if graph.outputs_declared:
                outputs = {}
                for public_name, contract in graph.outputs.items():
                    if contract.get("node_id") != node_id:
                        continue
                    output_name = str(contract.get("output_name", ""))
                    model_output = QgsProcessingModelOutput(
                        public_name,
                        str(contract.get("description", "")),
                    )
                    model_output.setChildId(node_id)
                    model_output.setChildOutputName(output_name)
                    model_output.setMandatory(
                        bool(contract.get("mandatory", False))
                    )
                    model_output.setDefaultValue(contract.get("default"))
                    outputs[public_name] = model_output
                child.setModelOutputs(outputs)
            elif not any(True for _edge in graph.outgoing_edges(node_id)):
                outputs = {}
                for output_name, port in node.outputs.items():
                    if not graph.output_is_publishable(node, output_name):
                        continue
                    model_output = QgsProcessingModelOutput(output_name, port.name)
                    model_output.setChildId(node_id)
                    model_output.setChildOutputName(output_name)
                    outputs[output_name] = model_output
                child.setModelOutputs(outputs)
            model.addChildAlgorithm(child)

        _valid, errors = model.validate()
        return model, "", [str(error) for error in errors]

    @staticmethod
    def _unique_parameter_name(candidate: str, taken_names: set) -> str:
        name = candidate
        suffix = 2
        while name in taken_names:
            name = f"{candidate}_{suffix}"
            suffix += 1
        return name

    @staticmethod
    def _add_promoted_parameter(
        model: QgsProcessingModelAlgorithm,
        definition,
        parameter_name: str,
        node: NodeDefinition,
        input_name: str,
    ) -> bool:
        """Clone one child parameter definition into a model input.

        Returns whether it was added; a definition that cannot be cloned or
        re-registered is skipped, leaving the parameter unset rather than
        aborting the export.
        """
        try:
            promoted = definition.clone()
            if promoted is None:
                return False
            promoted.setName(parameter_name)
            promoted.setDescription(
                f"{node.title}: {definition.description() or input_name}"
            )
            promoted.setFlags(
                promoted.flags() & ~Qgis.ProcessingParameterFlag.Hidden
            )
            component = QgsProcessingModelParameter(parameter_name)
            component.setDescription(promoted.description())
            component.setPosition(QPointF(node.x - 260.0, node.y))
            model.addModelParameter(promoted, component)
        except Exception:  # pragma: no cover - defensive around the C++ clone
            return False
        return True

    @classmethod
    def export_to_model3(
        cls, graph: GraphModel, path: str, allow_invalid: bool = False
    ) -> Tuple[bool, str]:
        """Export through QgsProcessingModelAlgorithm, never hand-written XML.

        With ``allow_invalid`` the file is written even when QGIS still reports
        validation issues, which is what a work-in-progress workflow needs.
        """
        model, fatal, issues = cls.build_native_model(graph)
        if model is None:
            return False, fatal
        if issues and not allow_invalid:
            return False, "\n".join(issues)
        if not model.toFile(path):
            return False, "QGIS could not write the .model3 file."
        return True, ""

    @classmethod
    def export_to_python(cls, graph: GraphModel, path: str) -> Tuple[bool, str]:
        """Write the workflow as a runnable QgsProcessingAlgorithm subclass.

        This is the same code QGIS' Model Designer produces with *Export as
        Python Algorithm*, so the result can be dropped into the Processing
        scripts folder or edited by hand. Validation issues never block it: a
        script of a half-finished workflow is still useful to read.
        """
        model, fatal, _issues = cls.build_native_model(graph)
        if model is None:
            return False, fatal
        try:
            lines = model.asPythonCode(_PYTHON_ALGORITHM_SUBCLASS, 4)
        except Exception as error:  # pragma: no cover - API/enum drift guard
            return False, f"QGIS could not generate Python code: {error}"
        if not lines:
            return False, "QGIS produced no Python code for this workflow."
        try:
            Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
        except OSError as error:
            return False, str(error)
        return True, ""

    @classmethod
    def import_from_model3(cls, path: str) -> Tuple[Optional[GraphModel], str]:
        model = QgsProcessingModelAlgorithm()
        if not model.fromFile(path):
            return None, "The selected file is not a valid QGIS .model3 model."
        graph = GraphModel(model.name() or Path(path).stem)
        help_content = model.helpContent()
        graph.description = str(help_content.get("ALG_DESC", ""))

        parameter_components = model.parameterComponents()
        for definition in model.parameterDefinitions():
            if definition.flags() & Qgis.ProcessingParameterFlag.IsModelOutput:
                continue
            component = parameter_components.get(definition.name())
            if component is None:
                continue
            algorithm_id = cls._smart_algorithm_for_definition(definition)
            if not algorithm_id:
                return (
                    None,
                    "Unsupported QGIS model parameter type: "
                    + definition.__class__.__name__,
                )
            node = AlgorithmCatalog.create_node(
                algorithm_id, definition.name(), definition.description()
            )
            node.model_parameter_definition = dict(definition.toVariantMap())
            node.model_parameter_required = not bool(
                definition.flags() & Qgis.ProcessingParameterFlag.Optional
            )
            node.parameters = {}
            if definition.defaultValue() not in (None, ""):
                key = cls._smart_parameter_key(algorithm_id)
                node.parameters[key] = definition.defaultValue()
            node.x = component.position().x()
            node.y = component.position().y()
            graph.add_node(node)

        pending_edges = []
        for child_id, child in model.childAlgorithms().items():
            configuration = dict(child.configuration())
            try:
                node = AlgorithmCatalog.create_node(
                    child.algorithmId(),
                    child_id,
                    child.description(),
                    configuration,
                )
            except ValueError as error:
                return None, str(error)
            node.x = child.position().x()
            node.y = child.position().y()
            node.is_active = bool(child.isActive())
            node.algorithm_configuration = configuration
            node.dependencies = [
                str(cls._member_value(dependency, "childId"))
                for dependency in child.dependencies()
            ]
            node.dependency_branches = {
                str(cls._member_value(dependency, "childId")): str(
                    cls._member_value(dependency, "conditionalBranch")
                )
                for dependency in child.dependencies()
            }
            for input_name, sources in child.parameterSources().items():
                node.parameter_source_order[input_name] = []
                node.parameters.pop(input_name, None)
                for source in sources:
                    if source.source() == Qgis.ProcessingModelChildParameterSource.StaticValue:
                        static_value = source.staticValue()
                        node.parameter_source_order[input_name].append(
                            {"kind": "static", "value": static_value}
                        )
                        port = node.inputs.get(input_name)
                        if port is not None and port.allows_multiple:
                            existing = node.parameters.get(input_name, [])
                            if not isinstance(existing, list):
                                existing = [existing]
                            existing.append(static_value)
                            node.parameters[input_name] = existing
                        else:
                            node.parameters[input_name] = static_value
                    elif source.source() == Qgis.ProcessingModelChildParameterSource.ModelParameter:
                        node.parameter_source_order[input_name].append(
                            {
                                "kind": "edge",
                                "node_id": source.parameterName(),
                                "output_name": "OUTPUT",
                            }
                        )
                        pending_edges.append(
                            (source.parameterName(), "OUTPUT", child_id, input_name)
                        )
                    elif source.source() == Qgis.ProcessingModelChildParameterSource.ChildOutput:
                        node.parameter_source_order[input_name].append(
                            {
                                "kind": "edge",
                                "node_id": source.outputChildId(),
                                "output_name": source.outputName(),
                            }
                        )
                        pending_edges.append(
                            (
                                source.outputChildId(),
                                source.outputName(),
                                child_id,
                                input_name,
                            )
                        )
                    else:
                        return (
                            None,
                            "Unsupported QGIS model input source on "
                            f"{child_id}.{input_name}.",
                        )
            graph.add_node(node)
        graph._suspend_source_order_invalidation = True
        try:
            for edge in pending_edges:
                if graph.add_edge(*edge) is None:
                    return None, f"Invalid connection in .model3 file: {graph.last_error}"
        finally:
            graph._suspend_source_order_invalidation = False
        graph.outputs_declared = True
        for child_id, child in model.childAlgorithms().items():
            for public_name, output in child.modelOutputs().items():
                output_name = output.childOutputName()
                node = graph.nodes.get(child_id)
                if node is None or output_name not in node.outputs:
                    return None, "A QGIS model output references an unavailable child output."
                if not graph.output_is_publishable(node, output_name):
                    return (
                        None,
                        "A QGIS model output is not a publishable Processing "
                        "layer output.",
                    )
                if public_name in graph.outputs:
                    return None, f"Duplicate QGIS model output: {public_name}"
                graph.outputs[public_name] = {
                    "node_id": child_id,
                    "output_name": output_name,
                    "description": output.description(),
                    "mandatory": bool(output.isMandatory()),
                    "default": output.defaultValue(),
                }
        try:
            graph.get_topological_order()
        except ValueError as error:
            return None, f"Invalid QGIS model dependency: {error}"
        return graph, ""

    @classmethod
    def _model_parameter_for_node(cls, node: NodeDefinition):
        if node.model_parameter_definition:
            definition = QgsProcessingParameters.parameterFromVariantMap(
                node.model_parameter_definition
            )
            if definition is not None:
                definition.setName(node.node_id)
                definition.setDescription(node.title)
                definition.setDefaultValue(
                    node.parameters.get(
                        cls._smart_parameter_key(node.algorithm_id)
                    )
                )
                return definition
        default = node.parameters.get(
            cls._smart_parameter_key(node.algorithm_id)
        )
        if node.algorithm_id == "smart:raster_layer":
            return QgsProcessingParameterRasterLayer(
                node.node_id, node.title, defaultValue=default, optional=False
            )
        if node.algorithm_id in ("smart:number", "smart:slider"):
            return QgsProcessingParameterNumber(
                node.node_id, node.title, defaultValue=default, optional=False
            )
        if node.algorithm_id == "smart:boolean":
            return QgsProcessingParameterBoolean(
                node.node_id, node.title, defaultValue=default, optional=False
            )
        if node.algorithm_id == "smart:string":
            return QgsProcessingParameterString(
                node.node_id, node.title, defaultValue=default, optional=False
            )
        if node.algorithm_id == "smart:field":
            return QgsProcessingParameterField(
                node.node_id, node.title, defaultValue=default, optional=False
            )
        if node.algorithm_id == "smart:crs":
            return QgsProcessingParameterCrs(
                node.node_id, node.title, defaultValue=default, optional=False
            )
        if node.algorithm_id == "smart:extent":
            return QgsProcessingParameterExtent(
                node.node_id, node.title, defaultValue=default, optional=False
            )
        if node.algorithm_id == "smart:enum":
            return QgsProcessingParameterEnum(
                node.node_id,
                node.title,
                options=[],
                defaultValue=default,
                optional=False,
            )
        if node.algorithm_id == "smart:map_layer":
            return QgsProcessingParameterMapLayer(
                node.node_id, node.title, defaultValue=default, optional=False
            )
        if node.algorithm_id in ("smart:multiple_vector", "smart:multiple_raster"):
            layer_type = (
                Qgis.ProcessingSourceType.Raster
                if node.algorithm_id == "smart:multiple_raster"
                else Qgis.ProcessingSourceType.Vector
            )
            return QgsProcessingParameterMultipleLayers(
                node.node_id,
                node.title,
                layerType=layer_type,
                defaultValue=default,
                optional=False,
            )
        return QgsProcessingParameterVectorLayer(
            node.node_id, node.title, defaultValue=default, optional=False
        )

    @staticmethod
    def _smart_parameter_key(algorithm_id: str) -> str:
        return (
            "LAYER"
            if algorithm_id
            in (
                "smart:input_layer",
                "smart:raster_layer",
                "smart:map_layer",
                "smart:multiple_vector",
                "smart:multiple_raster",
            )
            else "VALUE"
        )

    @staticmethod
    def _smart_algorithm_for_definition(definition) -> str:
        if isinstance(definition, QgsProcessingParameterMultipleLayers):
            return (
                "smart:multiple_raster"
                if definition.layerType() == Qgis.ProcessingSourceType.Raster
                else "smart:multiple_vector"
            )
        if isinstance(definition, QgsProcessingParameterRasterLayer):
            return "smart:raster_layer"
        if isinstance(
            definition,
            (QgsProcessingParameterVectorLayer, QgsProcessingParameterFeatureSource),
        ):
            return "smart:input_layer"
        if isinstance(definition, QgsProcessingParameterBoolean):
            return "smart:boolean"
        if isinstance(definition, QgsProcessingParameterNumber):
            return "smart:number"
        if isinstance(definition, QgsProcessingParameterString):
            return "smart:string"
        if isinstance(definition, QgsProcessingParameterField):
            return "smart:field"
        if isinstance(definition, QgsProcessingParameterCrs):
            return "smart:crs"
        if isinstance(definition, QgsProcessingParameterExtent):
            return "smart:extent"
        if isinstance(definition, QgsProcessingParameterEnum):
            return "smart:enum"
        if isinstance(definition, QgsProcessingParameterMapLayer):
            return "smart:map_layer"
        return ""

    @staticmethod
    def _member_value(value, name: str):
        member = getattr(value, name)
        return member() if callable(member) else member
