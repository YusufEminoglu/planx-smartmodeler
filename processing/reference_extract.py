"""Reviewed spatial extraction using a reference-layer attribute."""
from __future__ import annotations

from typing import Callable, List

from qgis.core import (
    QgsFeatureSink,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterEnum,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterString,
)


class ExtractByReferenceAttributeAlgorithm(QgsProcessingAlgorithm):
    """Keep input features spatially related to reference features by label."""

    INPUT = "INPUT"
    REFERENCE = "REFERENCE"
    REFERENCE_FIELD = "REFERENCE_FIELD"
    REFERENCE_VALUE = "REFERENCE_VALUE"
    PREDICATE = "PREDICATE"
    OUTPUT = "OUTPUT"
    PREDICATES = ("intersects", "contains", "within", "touches")

    def name(self) -> str:
        return "extractbyreferenceattribute"

    def displayName(self) -> str:
        return "Extract by reference layer attribute"

    def group(self) -> str:
        return "Layer extraction"

    def groupId(self) -> str:
        return "layerextraction"

    def shortHelpString(self) -> str:
        return (
            "Keeps input features that satisfy a spatial predicate against "
            "reference-layer features whose selected field equals the supplied "
            "value. The output is a temporary Processing layer."
        )

    def createInstance(self):
        return type(self)()

    def initAlgorithm(self, _configuration=None) -> None:
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT,
                "Input layer",
                [QgsProcessing.TypeVectorAnyGeometry],
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.REFERENCE,
                "Reference layer",
                [QgsProcessing.TypeVectorAnyGeometry],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.REFERENCE_FIELD,
                "Reference attribute",
                type=QgsProcessingParameterField.Any,
                parentLayerParameterName=self.REFERENCE,
            )
        )
        self.addParameter(
            QgsProcessingParameterString(self.REFERENCE_VALUE, "Reference value")
        )
        self.addParameter(
            QgsProcessingParameterEnum(
                self.PREDICATE,
                "Spatial predicate",
                options=list(self.PREDICATES),
                defaultValue=0,
            )
        )
        self.addParameter(QgsProcessingParameterFeatureSink(self.OUTPUT, "Output layer"))

    @staticmethod
    def _relation(predicate: str) -> Callable:
        if predicate == "contains":
            return lambda input_geometry, reference_geometry: reference_geometry.contains(input_geometry)
        if predicate == "within":
            return lambda input_geometry, reference_geometry: input_geometry.within(reference_geometry)
        if predicate == "touches":
            return lambda input_geometry, reference_geometry: input_geometry.touches(reference_geometry)
        return lambda input_geometry, reference_geometry: input_geometry.intersects(reference_geometry)

    def processAlgorithm(self, parameters, context, feedback):
        input_source = self.parameterAsSource(parameters, self.INPUT, context)
        reference_source = self.parameterAsSource(parameters, self.REFERENCE, context)
        if input_source is None or reference_source is None:
            raise QgsProcessingException("Both input and reference layers are required.")
        field_name = self.parameterAsString(parameters, self.REFERENCE_FIELD, context)
        value = self.parameterAsString(parameters, self.REFERENCE_VALUE, context).strip().casefold()
        field_index = reference_source.fields().indexOf(field_name)
        if not field_name or not value or field_index < 0:
            raise QgsProcessingException("A valid reference field and value are required.")

        predicate_index = self.parameterAsInt(parameters, self.PREDICATE, context)
        predicate = self.PREDICATES[predicate_index] if 0 <= predicate_index < len(self.PREDICATES) else self.PREDICATES[0]
        relation = self._relation(predicate)
        reference_geometries: List = []
        for feature in reference_source.getFeatures():
            if feedback.isCanceled():
                return {}
            if str(feature.attribute(field_index)).strip().casefold() != value:
                continue
            geometry = feature.geometry()
            if geometry is not None and not geometry.isEmpty():
                reference_geometries.append(geometry)

        sink, destination = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            input_source.fields(),
            input_source.wkbType(),
            input_source.sourceCrs(),
        )
        if sink is None:
            raise QgsProcessingException("The temporary output layer could not be created.")
        total = input_source.featureCount()
        for index, feature in enumerate(input_source.getFeatures()):
            if feedback.isCanceled():
                break
            geometry = feature.geometry()
            if geometry is not None and not geometry.isEmpty() and any(
                relation(geometry, reference_geometry)
                for reference_geometry in reference_geometries
            ):
                sink.addFeature(feature, QgsFeatureSink.FastInsert)
            if total > 0:
                feedback.setProgress(int(index * 100 / total))
        return {self.OUTPUT: destination}
