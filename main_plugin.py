"""QGIS plugin lifecycle for SmartModeler GIS."""
from __future__ import annotations

import os

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QAction, QIcon
from qgis.core import QgsApplication

from .gui.agent_dock import AgentWorkspaceDock
from .gui.modeler_window import SmartModelerWindow


class _ModelWindowApplyAdapter:
    """The one trusted seam through which an approved model-patch action reaches
    the live Workflow Studio graph. It exposes only reading the current graph and
    installing a replacement graph through the window's trusted refresh path; it
    grants no other window control. A model apply with no open studio fails
    closed (``current_graph`` returns ``None``)."""

    def __init__(self, plugin: "SmartModelerPlugin") -> None:
        self._plugin = plugin

    def current_graph(self):
        return self._plugin._current_graph()

    def install_graph(self, graph) -> None:
        window = self._plugin.window
        if window is None or not window.isVisible():
            raise RuntimeError("No open Workflow Studio model to apply to.")
        window.apply_agent_graph(graph)


class SmartModelerPlugin:
    """Registers the QGIS 4 workflow studio action and the Agent Workspace dock."""

    def __init__(self, iface) -> None:
        self.iface = iface
        self.plugin_dir = os.path.dirname(__file__)
        self.action: QAction | None = None
        self.window: SmartModelerWindow | None = None
        self.agent_action: QAction | None = None
        self.agent_dock: AgentWorkspaceDock | None = None

    def initGui(self) -> None:
        icon_path = os.path.join(self.plugin_dir, "icons", "icon.png")
        icon = (
            QIcon(icon_path)
            if os.path.exists(icon_path)
            else QgsApplication.getThemeIcon("/processingModel.svg")
        )
        self.action = QAction(
            icon,
            "SmartModeler GIS - Workflow Studio",
            self.iface.mainWindow(),
        )
        self.action.setObjectName("SmartModelerAction")
        self.action.setStatusTip("Design and run QGIS 4 Processing workflows")
        self.action.triggered.connect(self.run)
        self.iface.addPluginToVectorMenu("SmartModeler GIS", self.action)
        self.iface.addVectorToolBarIcon(self.action)

        self.agent_dock = AgentWorkspaceDock(
            self.iface,
            self._current_graph,
            self.iface.mainWindow(),
            model_apply=_ModelWindowApplyAdapter(self),
        )
        self.iface.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, self.agent_dock)
        self.agent_dock.hide()
        self.agent_action = QAction(
            QgsApplication.getThemeIcon("/mIconModelInput.svg"),
            "SmartModeler GIS - Agent Workspace",
            self.iface.mainWindow(),
        )
        self.agent_action.setObjectName("SmartModelerAgentWorkspaceAction")
        self.agent_action.setStatusTip(
            "Open the read-only Agent Workspace inspection panel"
        )
        self.agent_action.triggered.connect(self.open_agent_workspace)
        self.iface.addPluginToVectorMenu("SmartModeler GIS", self.agent_action)
        self.iface.addVectorToolBarIcon(self.agent_action)

    def unload(self) -> None:
        if self.action is not None:
            self.iface.removePluginVectorMenu("SmartModeler GIS", self.action)
            self.iface.removeVectorToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None
        if self.agent_action is not None:
            self.iface.removePluginVectorMenu("SmartModeler GIS", self.agent_action)
            self.iface.removeVectorToolBarIcon(self.agent_action)
            self.agent_action.deleteLater()
            self.agent_action = None
        if self.agent_dock is not None:
            self.agent_dock.shutdown()
            self.iface.removeDockWidget(self.agent_dock)
            self.agent_dock.deleteLater()
            self.agent_dock = None
        if self.window is not None:
            self.window.close()
            self.window.deleteLater()
            self.window = None

    def run(self) -> None:
        if self.window is None:
            self.window = SmartModelerWindow(self.iface, self.iface.mainWindow())
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def open_agent_workspace(self) -> None:
        if self.agent_dock is None:
            return
        self.agent_dock.show()
        self.agent_dock.raise_()

    def _current_graph(self):
        """Optional model adapter: the live graph, or None when no studio is
        meaningfully open.

        Returns the studio's own graph object through a callback (never a
        copy), so the Agent Workspace never holds a stale reference across a
        studio close/reopen. "Open" means the Workflow Studio window exists
        and is currently visible: never having run the studio, and having
        closed/hidden it, both report no current model. Hiding the window
        never destroys or replaces its graph - the same window instance (and
        graph) is reused and becomes visible again on the next studio open,
        at which point the Agent Workspace reports it as available again.
        """
        if self.window is None or not self.window.isVisible():
            return None
        return self.window.graph
