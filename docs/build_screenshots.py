"""Render the guide's screenshots from the real widgets, headless.

Every image in ``docs/GUIDE.html`` is produced by this script from the actual
Workflow Studio window and Agent Workspace dock -- no mock-ups and no cropped
marketing shots, so a screenshot cannot quietly drift away from the interface
it documents. Only the *content* is scripted (a demo workflow, an example
conversation); the widgets, styling and layout are the shipped ones.

Run it after a UI change:

    set QT_QPA_PLATFORM=offscreen
    C:\\OSGeo4W\\bin\\python-qgis-ltr.bat planx_smartmodeler\\docs\\build_screenshots.py

Output: ``docs/images/*.png``. Neither this script nor the images ship in the
plugin zip (see ``.zipignore``); the Hub package stays lean and the images are
served from GitHub Pages.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

HERE = Path(__file__).resolve().parent
PLUGIN_ROOT = HERE.parent
IMAGES = HERE / "images"

sys.path.insert(0, r"C:\OSGeo4W\apps\qgis-ltr\python\plugins")
if str(PLUGIN_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT.parent))

from qgis.core import QgsApplication  # noqa: E402


def _grab(widget, name: str, width: int = 0, height: int = 0) -> None:
    """Show, settle and save one widget as a PNG."""
    from qgis.PyQt.QtCore import QCoreApplication

    if width and height:
        widget.resize(width, height)
    widget.show()
    for _ in range(6):
        QCoreApplication.processEvents()
    IMAGES.mkdir(parents=True, exist_ok=True)
    path = IMAGES / f"{name}.png"
    widget.grab().save(str(path))
    print(f"wrote {path.relative_to(PLUGIN_ROOT)} ({path.stat().st_size // 1024} KB)", flush=True)


def _demo_graph():
    """A small, readable suitability workflow -- the guide's worked example."""
    from planx_smartmodeler.core.algorithm_catalog import AlgorithmCatalog
    from planx_smartmodeler.core.auto_layout import AutoLayoutEngine
    from planx_smartmodeler.core.graph_model import GraphModel

    graph = GraphModel("Slope suitability")
    dem = AlgorithmCatalog.create_node("smart:raster_layer", "dem", "DEM input")
    slope = AlgorithmCatalog.create_node("native:slope", "slope1", "Slope")
    reclass = AlgorithmCatalog.create_node(
        "native:reclassifybytable", "bands1", "Suitability bands"
    )
    for node in (dem, slope, reclass):
        graph.add_node(node)
    graph.add_edge("dem", "OUTPUT", "slope1", "INPUT")
    graph.add_edge("slope1", "OUTPUT", "bands1", "INPUT_RASTER")
    AutoLayoutEngine.apply_layout(graph)
    return graph


def shoot_workflow_studio() -> None:
    from planx_smartmodeler.gui.modeler_window import SmartModelerWindow

    window = SmartModelerWindow(None)
    _grab(window, "studio-01-empty", 1280, 800)

    window._set_graph(_demo_graph(), fit=True)
    _grab(window, "studio-02-workflow", 1280, 800)

    palette = getattr(window, "palette_widget", None) or getattr(
        window, "node_palette", None
    )
    if palette is not None:
        _grab(palette, "studio-03-palette", 340, 620)

    inspector = getattr(window, "inspector_widget", None)
    if inspector is not None:
        node = window.graph.nodes.get("slope1")
        if node is not None:
            inspector.inspect_node(node)
        _grab(inspector, "studio-04-inspector", 380, 520)


def _example_conversation() -> str:
    return "\n".join(
        (
            "> download the buildings in the map extent",
            "",
            "[assistant] I will inspect the project, then propose one reviewed"
            " download of building polygons for the current map extent.",
            "[tool: project.summary] success",
            "[tool: layer.list] success",
            "[tool: processing.resolve] success",
            "[proposal] Validated. Review the approval card and click Run to"
            " proceed.",
            "",
            "[run] Finished. Added as temporary layer(s): Buildings.",
            "",
            "> reproject Buildings to the local metric CRS",
            "",
            "[assistant] Web Mercator inflates area by about 1.76x at this"
            " latitude, so I will reproject before anything is measured.",
            "[tool: layer.suggest_crs] success",
            "[proposal] Validated. Review the approval card and click Run to"
            " proceed.",
        )
    )


def shoot_agent_workspace() -> None:
    from planx_smartmodeler.core.agent.contracts import AgentMode, AgentScope
    from planx_smartmodeler.gui.agent_dock import AgentWorkspaceDock

    dock = AgentWorkspaceDock(None, lambda: None)
    dock.scope_combo.setCurrentIndex(dock.scope_combo.findData(AgentScope.PROJECT))
    dock.mode_combo.setCurrentIndex(dock.mode_combo.findData(AgentMode.ACT))
    _grab(dock, "agent-01-empty", 720, 1000)

    dock.transcript.setPlainText(_example_conversation())
    dock.transcript.verticalScrollBar().setValue(
        dock.transcript.verticalScrollBar().maximum()
    )
    _grab(dock, "agent-02-conversation", 720, 1000)

    # The approval card, filled with the same shape a validated proposal
    # produces. Nothing is executed: this is the card the user reads.
    dock.approval_group.setVisible(True)
    dock.approval_status_label.setText(
        "Waiting for your approval - nothing has run yet."
    )
    dock.risk_badge_label.setText("Risk: creates a new temporary layer")
    dock.risk_badge_label.setVisible(True)
    dock.approval_view.setPlainText(
        "\n".join(
            (
                "Reproject Buildings to EPSG:32635",
                "",
                "Algorithm: native:reprojectlayer",
                "INPUT:      Buildings",
                "TARGET_CRS: EPSG:32635",
                "OUTPUT:     temporary layer",
                "",
                "Scope: Project    Mode: Act",
            )
        )
    )
    dock.apply_button.setText("Run")
    dock.apply_button.setEnabled(True)
    dock.reject_button.setEnabled(True)
    _grab(dock, "agent-03-approval", 720, 1060)

    dock.scope_combo.setCurrentIndex(
        dock.scope_combo.findData(AgentScope.CURRENT_MODEL)
    )
    dock.mode_combo.setCurrentIndex(dock.mode_combo.findData(AgentMode.PLAN))
    dock.approval_status_label.setText(
        "Waiting for your approval - the workflow has not changed yet."
    )
    dock.risk_badge_label.setText("Risk: edits the open workflow")
    dock.approval_view.setPlainText(
        "\n".join(
            (
                "Slope suitability workflow",
                "",
                "Add node 'Slope' (native:slope) as slope1",
                "Add node 'Suitability bands' (native:reclassifybytable) as bands1",
                "Connect slope1.OUTPUT -> bands1.INPUT_RASTER",
                "",
                "Candidate: 3 nodes, 2 connections",
            )
        )
    )
    dock.apply_button.setText("Apply")
    _grab(dock, "agent-04-workflow-patch", 720, 1060)


def shoot_guides() -> None:
    from planx_smartmodeler.gui.help_dialog import AgentQuickStartDialog, HelpDialog

    _grab(AgentQuickStartDialog(), "guide-01-agent-quick-start", 700, 660)
    _grab(HelpDialog(), "guide-02-help", 800, 620)


def _use_the_desktop_ui_font(app) -> None:
    """Pin the UI font the way a real desktop session has it.

    The offscreen platform ships no fonts, so text renders as nothing at all;
    pointing ``QT_QPA_FONTDIR`` at the system fonts fixes that but leaves Qt to
    pick a default family alphabetically, which produced an Arabic-script face
    for an English interface. The screenshots have to show what a user sees.
    """
    from qgis.PyQt.QtGui import QFont, QFontDatabase

    families = set(QFontDatabase().families())
    for candidate in ("Segoe UI", "Tahoma", "Verdana", "Arial", "DejaVu Sans"):
        if candidate in families:
            app.setFont(QFont(candidate, 9))
            print(f"ui font: {candidate}", flush=True)
            return
    print(f"ui font: falling back to Qt default ({len(families)} families)", flush=True)


def main() -> int:
    app = QgsApplication([], True)
    app.initQgis()
    _use_the_desktop_ui_font(app)
    try:
        from processing.core.Processing import Processing

        Processing.initialize()
        shoot_workflow_studio()
        shoot_agent_workspace()
        shoot_guides()
        print("SCREENSHOTS OK", flush=True)
        return 0
    finally:
        sys.stdout.flush()
        globals()["_QGIS_APPLICATION"] = app
        app.exitQgis()
        sys.stdout.flush()
        os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
