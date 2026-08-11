"""QGIS smoke test for AI-facing reference-layer spatial extraction."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from qgis.core import (
    QgsApplication,
    QgsFeature,
    QgsGeometry,
    QgsProcessingContext,
    QgsProcessing,
    QgsProject,
    QgsVectorLayer,
)


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    plugin_root = Path(__file__).resolve().parents[1]
    monorepo_root = plugin_root.parent
    if str(monorepo_root) not in sys.path:
        sys.path.insert(0, str(monorepo_root))
    application = QgsApplication([], False)
    application.initQgis()
    try:
        processing_plugins = str(Path(QgsApplication.prefixPath()) / "python" / "plugins")
        if processing_plugins not in sys.path:
            sys.path.append(processing_plugins)
        from processing.core.Processing import Processing
        from planx_smartmodeler.processing.provider import SmartModelerProcessingProvider

        Processing.initialize()
        project = QgsProject.instance()
        registry = QgsApplication.processingRegistry()
        provider = None
        if registry.providerById(SmartModelerProcessingProvider.PROVIDER_ID) is None:
            provider = SmartModelerProcessingProvider()
            if not registry.addProvider(provider):
                raise RuntimeError("SmartModeler provider could not be registered.")

        target = QgsVectorLayer(
            "Polygon?crs=EPSG:4326&field=name:string",
            "MAHALLE",
            "memory",
        )
        reference = QgsVectorLayer(
            "Polygon?crs=EPSG:4326&field=ADINUMARASI:string",
            "ILCEALANI",
            "memory",
        )
        target_feature = QgsFeature(target.fields())
        target_feature.setAttributes(["Konak neighbourhood"])
        target_feature.setGeometry(QgsGeometry.fromWkt("POLYGON ((0 0, 1 0, 1 1, 0 1, 0 0))"))
        outside_feature = QgsFeature(target.fields())
        outside_feature.setAttributes(["Outside"])
        outside_feature.setGeometry(QgsGeometry.fromWkt("POLYGON ((3 3, 4 3, 4 4, 3 4, 3 3))"))
        target.dataProvider().addFeatures([target_feature, outside_feature])

        konak = QgsFeature(reference.fields())
        konak.setAttributes(["KONAK"])
        konak.setGeometry(QgsGeometry.fromWkt("POLYGON ((-1 -1, 2 -1, 2 2, -1 2, -1 -1))"))
        other = QgsFeature(reference.fields())
        other.setAttributes(["OTHER"])
        other.setGeometry(QgsGeometry.fromWkt("POLYGON ((2 2, 5 2, 5 5, 2 5, 2 2))"))
        reference.dataProvider().addFeatures([konak, other])
        project.addMapLayer(target)
        project.addMapLayer(reference)

        algorithm = registry.algorithmById(
            "smartmodeler:extractbyreferenceattribute"
        )
        if algorithm is None:
            raise RuntimeError("The reference extraction algorithm was not registered.")
        context = QgsProcessingContext()
        context.setProject(project)
        import processing

        result = processing.run(
            algorithm,
            {
                "INPUT": target,
                "REFERENCE": reference,
                "REFERENCE_FIELD": "ADINUMARASI",
                "REFERENCE_VALUE": "KONAK",
                "PREDICATE": 0,
                "OUTPUT": QgsProcessing.TEMPORARY_OUTPUT,
            },
            context=context,
            is_child_algorithm=True,
        )
        output = result.get("OUTPUT")
        if not isinstance(output, str) or not output:
            raise RuntimeError("The reference extraction returned no output layer.")
        output_layer = output if hasattr(output, "featureCount") else context.takeResultLayer(output)
        if output_layer is None or output_layer.featureCount() != 1:
            raise RuntimeError("The reference extraction kept the wrong features.")
        print(
            "REFERENCE EXTRACT SMOKE PASS: Konak reference attribute filtered "
            "the neighbourhood layer to one temporary feature."
        )
        return 0
    finally:
        QgsProject.instance().clear()
        application.exitQgis()
        # Keep the QgsApplication referenced past this frame. When ``main``
        # returned, dropping the last reference ran the C++ destructor, and
        # on Windows that can sit forever after a suite has already passed --
        # the verify gate then waits on a process with nothing left to do.
        # The module-level ``os._exit`` is the real end of this process.
        globals()["_QGIS_APPLICATION"] = application


if __name__ == "__main__":
    _code = main()
    # Flush, then leave immediately. A headless QgsApplication can sit in
    # Qt/GDAL static teardown after the suite has already printed its
    # result and returned -- observed on Windows, on an unmodified
    # checkout, with every assertion passed -- and the verify gate then
    # waits forever on a process with nothing left to do. The exit code
    # is the suite's own, so a failure still fails.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_code)
