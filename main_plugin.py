"""QGIS plugin lifecycle for SmartModeler GIS."""
from __future__ import annotations

import os

from qgis.PyQt.QtCore import QTimer, Qt
from qgis.PyQt.QtGui import QAction, QIcon
from qgis.core import QgsApplication

from .core.translation import TranslationManager
from .gui.agent_dock import AgentWorkspaceDock
from .gui.help_dialog import HelpDialog
from .gui.modeler_window import SmartModelerWindow
from .processing.provider import SmartModelerProcessingProvider


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
        self.translation = TranslationManager(self.plugin_dir)
        self.translation.install()
        self.action: QAction | None = None
        self.window: SmartModelerWindow | None = None
        self.agent_action: QAction | None = None
        self.agent_dock: AgentWorkspaceDock | None = None
        self.help_action: QAction | None = None
        self.help_dialog: HelpDialog | None = None
        self.processing_provider: SmartModelerProcessingProvider | None = None

    def initProcessing(self) -> None:
        """Register the provider in desktop QGIS and headless qgis_process."""
        if self.processing_provider is not None:
            return
        registry = QgsApplication.processingRegistry()
        if registry.providerById(SmartModelerProcessingProvider.PROVIDER_ID) is None:
            provider = SmartModelerProcessingProvider()
            if registry.addProvider(provider):
                self.processing_provider = provider

    def initGui(self) -> None:
        self.initProcessing()
        if self.iface is None:
            return

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
        self.iface.addPluginToMenu("SmartModeler GIS", self.action)
        self.iface.addVectorToolBarIcon(self.action)

        self.agent_dock = AgentWorkspaceDock(
            self.iface,
            self._current_graph,
            self.iface.mainWindow(),
            model_apply=_ModelWindowApplyAdapter(self),
            external_run_active=self._studio_run_active,
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
            "Open supervised inspections and explicit-approval actions"
        )
        self.agent_action.triggered.connect(self.open_agent_workspace)
        self.iface.addPluginToMenu("SmartModeler GIS", self.agent_action)
        self.iface.addVectorToolBarIcon(self.agent_action)

        self.help_action = QAction(
            QgsApplication.getThemeIcon("/mActionHelpContents.svg"),
            "SmartModeler GIS - Help and Safety",
            self.iface.mainWindow(),
        )
        self.help_action.setObjectName("SmartModelerHelpAction")
        self.help_action.setStatusTip(
            "Open quick start, keyboard, privacy, and support guidance"
        )
        self.help_action.triggered.connect(self.open_help)
        self.iface.addPluginToMenu("SmartModeler GIS", self.help_action)

    def unload(self) -> None:
        if self.processing_provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.processing_provider)
            self.processing_provider = None
        if self.action is not None:
            self.iface.removePluginMenu("SmartModeler GIS", self.action)
            self.iface.removeVectorToolBarIcon(self.action)
            self.action.deleteLater()
            self.action = None
        if self.agent_action is not None:
            self.iface.removePluginMenu("SmartModeler GIS", self.agent_action)
            self.iface.removeVectorToolBarIcon(self.agent_action)
            self.agent_action.deleteLater()
            self.agent_action = None
        if self.help_action is not None:
            self.iface.removePluginMenu(
                "SmartModeler GIS", self.help_action
            )
            self.help_action.deleteLater()
            self.help_action = None
        if self.help_dialog is not None:
            self.help_dialog.close()
            self.help_dialog.deleteLater()
            self.help_dialog = None
        if self.agent_dock is not None:
            self.agent_dock.shutdown()
            self.iface.removeDockWidget(self.agent_dock)
            self.agent_dock.deleteLater()
            self.agent_dock = None
        if self.window is not None:
            retiring_window = self.window
            self.window = None
            retiring_window.prepare_for_shutdown()
            self._dispose_window_when_idle(retiring_window)
        self.translation.remove()

    def _dispose_window_when_idle(self, window: SmartModelerWindow) -> None:
        """Never delete a window while its synchronous run stack is unwinding."""
        if window._is_executing:
            QTimer.singleShot(
                25, lambda: self._dispose_window_when_idle(window)
            )
            return
        window.close()
        window.deleteLater()

    def run(self) -> None:
        if self.window is None:
            self.window = SmartModelerWindow(
                self.iface,
                self.iface.mainWindow(),
                external_run_active=self._agent_run_active,
            )
        self.window.show()
        self.window.raise_()
        self.window.activateWindow()

    def open_agent_workspace(self) -> None:
        if self.agent_dock is None:
            return
        self.agent_dock.show()
        self.agent_dock.raise_()
        self.agent_dock.prompt_input.setFocus()

    def open_ai_connections(self) -> bool:
        """Open the shared AI profile editor for trusted companion plugins."""
        if self.agent_dock is None:
            return False
        self.agent_dock.open_ai_connections()
        return True

    def agent_connection_info(self) -> dict:
        """Return display-only profile state without reading an API secret."""
        from .core.ai_settings import AiSettingsStore, PROVIDERS

        profile = AiSettingsStore().active_profile()
        provider = PROVIDERS[profile.provider_id]
        return {
            "profile_name": profile.name or provider.name,
            "provider_id": profile.provider_id,
            "provider_name": provider.name,
            "model": profile.model,
            "agent_chat_enabled": profile.provider_id != "offline",
        }

    def open_help(self) -> None:
        if self.help_dialog is None:
            self.help_dialog = HelpDialog(self.iface.mainWindow())
        self.help_dialog.show()
        self.help_dialog.raise_()
        self.help_dialog.activateWindow()

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

    def _studio_run_active(self) -> bool:
        return bool(self.window is not None and self.window._is_executing)

    def _agent_run_active(self) -> bool:
        return bool(
            self.agent_dock is not None
            and self.agent_dock.run_coordinator.is_running()
        )
