"""Audit every live Processing algorithm through SmartModeler's node boundary.

Run with either OSGeo4W QGIS launcher.  The test is deliberately read-only: it
constructs typed nodes and round-trips their SmartModeler documents, but never
executes arbitrary algorithms (some registry entries have network, database,
filesystem, or project side effects).
"""
from __future__ import annotations

import os
import sys
from collections import Counter
from pathlib import Path

from qgis.core import QgsApplication, QgsProject


def run_matrix() -> str:
    plugins_root = Path(__file__).resolve().parents[2]
    if str(plugins_root) not in sys.path:
        sys.path.insert(0, str(plugins_root))

    from planx_smartmodeler.core.algorithm_catalog import AlgorithmCatalog
    from planx_smartmodeler.core.graph_model import GraphModel, SocketType
    from planx_smartmodeler.core.model3_serializer import Model3Serializer
    from planx_smartmodeler.processing.provider import SmartModelerProcessingProvider

    registry = QgsApplication.processingRegistry()
    provider = None
    if registry.providerById(SmartModelerProcessingProvider.PROVIDER_ID) is None:
        provider = SmartModelerProcessingProvider()
        if not registry.addProvider(provider):
            raise RuntimeError("SmartModeler Processing provider could not be registered.")

    try:
        algorithms = list(registry.algorithms())
        if len(algorithms) < 100:
            raise RuntimeError(
                f"Processing registry is unexpectedly small: {len(algorithms)} algorithms."
            )

        ids = [algorithm.id() for algorithm in algorithms]
        missing_ids = [algorithm.displayName() for algorithm in algorithms if not algorithm.id()]
        duplicates = sorted(
            algorithm_id
            for algorithm_id, count in Counter(ids).items()
            if algorithm_id and count > 1
        )
        if missing_ids or duplicates:
            raise RuntimeError(
                f"Invalid catalog identities; missing={missing_ids[:5]}, "
                f"duplicates={duplicates[:10]}"
            )

        failures = []
        socket_types = {
            value
            for name, value in vars(SocketType).items()
            if name.isupper() and isinstance(value, str)
        }
        audited = list(ids) + sorted(AlgorithmCatalog.SMART_ALGORITHMS)
        for index, algorithm_id in enumerate(audited):
            try:
                node = AlgorithmCatalog.create_node(
                    algorithm_id,
                    node_id=f"matrix_{index}",
                )
                if node.algorithm_id != algorithm_id:
                    raise AssertionError(
                        f"node algorithm id changed to {node.algorithm_id!r}"
                    )
                if len(node.inputs) != len(set(node.inputs)):
                    raise AssertionError("duplicate input port names")
                if len(node.outputs) != len(set(node.outputs)):
                    raise AssertionError("duplicate output port names")
                for port in tuple(node.inputs.values()) + tuple(node.outputs.values()):
                    if port.socket_type not in socket_types:
                        raise AssertionError(
                            f"unknown socket type {port.socket_type!r}"
                        )

                graph = GraphModel(f"Catalog matrix: {algorithm_id}")
                graph.add_node(node)
                encoded = Model3Serializer.export_to_json(graph)
                decoded = Model3Serializer.import_from_json(encoded)
                rebuilt = decoded.nodes.get(node.node_id)
                if rebuilt is None or rebuilt.algorithm_id != algorithm_id:
                    raise AssertionError("SmartModeler JSON round-trip changed the node")
                if set(rebuilt.inputs) != set(node.inputs):
                    raise AssertionError("SmartModeler JSON round-trip changed input ports")
                if set(rebuilt.outputs) != set(node.outputs):
                    raise AssertionError("SmartModeler JSON round-trip changed output ports")
            except Exception as error:
                failures.append(
                    f"{algorithm_id}: {type(error).__name__}: {str(error)[:240]}"
                )

        if failures:
            sample = "\n  ".join(failures[:25])
            raise RuntimeError(
                f"{len(failures)} of {len(audited)} catalog entries failed:\n  {sample}"
            )

        providers = {algorithm.provider().id() for algorithm in algorithms}
        return (
            f"CATALOG MATRIX PASS: {len(audited)} typed nodes across "
            f"{len(providers)} providers; JSON round-trip preserved every port schema."
        )
    finally:
        if provider is not None:
            registry.removeProvider(provider)


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    application = QgsApplication([], False)
    application.initQgis()
    plugins_path = os.path.join(QgsApplication.prefixPath(), "python", "plugins")
    if plugins_path not in sys.path:
        sys.path.append(plugins_path)
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
        print(run_matrix())
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
