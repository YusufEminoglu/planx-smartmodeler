"""Headless gallery and render acceptance for shipped showcase workflows."""
from __future__ import annotations

import os
import sys

from qgis.PyQt.QtCore import QEvent
from qgis.PyQt.QtWidgets import QApplication
from qgis.core import QgsApplication


SHOWCASE_IDS = (
    "showcase_walkable_city",
    "showcase_blue_green_resilience",
    "showcase_urban_morphology",
    "showcase_flood_readiness",
    "showcase_growth_constraints",
    "showcase_planx_network_centrality",
    "showcase_urban_resilience_heat",
    "showcase_planx_settlement_fabric",
    "showcase_transit_15_minute_city",
    "showcase_suitability_constraints",
)


class _MemorySettings:
    """Minimal isolated settings store; never touches the user's QGIS profile."""

    def __init__(self) -> None:
        self.values = {}

    def value(self, key, default=None, **_kwargs):
        return self.values.get(key, default)

    def setValue(self, key, value) -> None:
        self.values[key] = value

    def remove(self, prefix) -> None:
        for key in list(self.values):
            if key.startswith(prefix):
                self.values.pop(key)

    def sync(self) -> None:
        return None


def run_checks() -> str:
    from planx_smartmodeler.core.micro_packages import MicroPackageCatalog
    from planx_smartmodeler.core.model3_serializer import Model3Serializer
    from planx_smartmodeler.gui import modeler_window

    original_settings = modeler_window.QgsSettings
    modeler_window.QgsSettings = _MemorySettings
    try:
        window = modeler_window.SmartModelerWindow(None)
    finally:
        modeler_window.QgsSettings = original_settings
    try:
        preset_list = window.palette_widget.preset_list
        preset_names = [
            preset_list.item(index).text()
            for index in range(preset_list.count())
        ]
        if len(preset_names) != 15 or not all(
            name.startswith("Showcase · ") and " nodes" in name
            for name in preset_names[:10]
        ):
            raise RuntimeError(
                "The showcase-first example workflow gallery did not construct."
            )

        rendered = []
        for package_id in SHOWCASE_IDS:
            print(f"Rendering {package_id}...", flush=True)
            graph = MicroPackageCatalog.instantiate(package_id)
            native_model, fatal, issues = Model3Serializer.build_native_model(graph)
            if native_model is None or fatal or issues:
                raise RuntimeError(
                    f"Showcase native export failed: {package_id}: "
                    f"{fatal or issues}"
                )
            window._set_graph(graph)
            QApplication.processEvents()
            rect = window.scene.itemsBoundingRect()
            positions = {(node.x, node.y) for node in graph.nodes.values()}
            if (
                len(window.scene.node_items) != len(graph.nodes)
                or len(window.scene.connection_items) != len(graph.edges)
                or window.inspector_widget.outline.topLevelItemCount()
                != len(graph.nodes)
                or len(positions) != len(graph.nodes)
                or rect.width() < 900
                or rect.height() < 300
            ):
                raise RuntimeError(
                    f"Showcase did not form a complete branching scene: {package_id}"
                )

            rendered.append(
                f"{package_id}={len(graph.nodes)}n/{len(graph.edges)}e"
            )
            print(f"Rendered {package_id}.", flush=True)
        return "SHOWCASE SMOKE PASS: " + ", ".join(rendered)
    finally:
        window.prepare_for_shutdown()
        window.close()
        window.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        QApplication.processEvents()


def main() -> int:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    app = QgsApplication([], False)
    app.initQgis()
    plugins_path = os.path.normpath(
        os.path.join(QgsApplication.prefixPath(), "python", "plugins")
    )
    if plugins_path not in sys.path:
        sys.path.insert(0, plugins_path)
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
        print("Processing initialized; constructing showcase gallery.", flush=True)
        print(run_checks(), flush=True)
        return 0
    finally:
        app.exitQgis()
        # Keep the QgsApplication referenced past this frame. When ``main``
        # returned, dropping the last reference ran the C++ destructor, and
        # on Windows that can sit forever after a suite has already passed --
        # the verify gate then waits on a process with nothing left to do.
        # The module-level ``os._exit`` is the real end of this process.
        globals()["_QGIS_APPLICATION"] = app


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
