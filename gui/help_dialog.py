"""In-application quick start, keyboard, privacy, and support guide."""
from __future__ import annotations

from qgis.PyQt.QtCore import QUrl
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
)

REFERENCE_MANUAL_URL = (
    "https://yusufeminoglu.github.io/planx-smartmodeler/"
    "SMARTMODELER_REFERENCE_MANUAL.html"
)
# The illustrated walkthrough. Screenshots belong on a web page, not in a
# plugin zip that every user downloads, so the in-application guides stay short
# and hand off here for the pictures.
STEP_BY_STEP_GUIDE_URL = (
    "https://yusufeminoglu.github.io/planx-smartmodeler/GUIDE.html"
)


class AgentQuickStartDialog(QDialog):
    """One page that gets somebody productive, then hands off to the manual.

    Deliberately not a second manual. The online reference is fifteen chapters
    long, which is the right size for looking something up and the wrong size
    for a first click: someone who has just opened the dock needs five rules and
    a sentence they can paste, not an architecture chapter. Everything here is
    the shortest form of a lesson that cost a real session to learn.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Agent Workspace - Quick start")
        self.setAccessibleName("Agent Workspace quick start")
        self.resize(660, 620)

        layout = QVBoxLayout(self)
        page = QTextBrowser()
        page.setOpenExternalLinks(True)
        page.setAccessibleName("Agent Workspace quick start")
        page.setHtml(
            """
            <h2>Your first request</h2>
            <ol>
              <li>Set <b>Mode: Act</b> and <b>Scope: Project</b> above.</li>
              <li>Type <i>one</i> operation and press <b>Send</b>.</li>
              <li>Read the approval card, then click <b>Run</b> or <b>Apply</b>.
                  Nothing touches your project until you do.</li>
            </ol>

            <h2>Five things that make it work</h2>
            <ol>
              <li><b>One operation per message.</b> A message asking for four
                  things fails as a whole; one step at a time fails cheaply.</li>
              <li><b>Name the layer exactly</b> as it appears in the Layers
                  panel. After a few runs you will have several similarly named
                  temporary layers, and "this layer" stops meaning anything.</li>
              <li><b>Reproject before you measure.</b> Downloaded data usually
                  arrives in Web Mercator, where areas are inflated by about
                  1.76&times; at Turkish latitudes. Say <i>"reproject to the
                  local metric CRS"</i> first; the plugin will refuse to measure
                  otherwise, and that refusal is protecting your numbers.</li>
              <li><b>Questions are free.</b> Switch to <b>Ask</b> mode for
                  "what is the minimum of this field?" - it costs nothing from
                  the ten-action budget and tells you whether a threshold makes
                  sense before you filter with it.</li>
              <li><b>Results are temporary layers.</b> They disappear when QGIS
                  closes. Right-click anything worth keeping and choose
                  <b>Make permanent</b>.</li>
            </ol>

            <h2>Try these</h2>
            <p style="color:#666">Copy one, change the names to match your project.</p>
            <blockquote>
            <p>download the buildings in the map extent</p>
            <p>reproject <i>Buildings</i> to the local metric CRS</p>
            <p>add a decimal column alan_m2 with the area in square metres</p>
            <p>what are the minimum and maximum of alan_m2?</p>
            <p>make a new layer with only the buildings where alan_m2 is 300 or less</p>
            <p>classify that layer by alan_m2 with jenks into 5 classes</p>
            </blockquote>

            <h2>Building a workflow instead of running one step</h2>
            <p>Set <b>Scope: Current model</b> with Workflow Studio open, and the
            same dock edits the open graph instead of running anything. Ask for
            the whole analysis in one sentence - <i>"calculate slope from the DEM
            and classify it into planning suitability bands"</i> - and the
            approval card lists the exact nodes and connections it wants to add.
            Click <b>Apply</b> and they arrive already laid out.</p>
            <p>Follow-ups work the same way: <i>"now add distance to roads and
            combine it with the slope bands"</i>. Nodes you positioned yourself
            keep their places. Set the thresholds yourself - the structure is
            proposed, the numbers are yours.</p>

            <h2>If something is refused</h2>
            <p>Most refusals name a specific fact - a CRS, a field type, a list
            of valid options - and stop a run that would otherwise have
            succeeded and handed you a wrong answer. Read the message and
            correct <i>the fact</i>: "the field is alan_m2, not alanm2" works,
            "do it properly" does not.</p>
            <p>If a layer cannot be found, click <b>Layers</b> in the quick
            inspection row and paste the exact layer id.</p>

            <hr>
            <p>The illustrated guide walks through both halves of the plugin
            with screenshots of every step; the reference manual has the full
            protocol and every error message with its cause and fix.</p>
            """
        )
        layout.addWidget(page, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        guide_button = buttons.addButton(
            "Open the illustrated guide", QDialogButtonBox.ButtonRole.ActionRole
        )
        guide_button.setToolTip(STEP_BY_STEP_GUIDE_URL)
        guide_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(STEP_BY_STEP_GUIDE_URL))
        )
        manual_button = buttons.addButton(
            "Open the full manual", QDialogButtonBox.ButtonRole.ActionRole
        )
        manual_button.setToolTip(REFERENCE_MANUAL_URL)
        manual_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(REFERENCE_MANUAL_URL))
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class HelpDialog(QDialog):
    """Small local guide so core safety rules are discoverable without a web page."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SmartModeler GIS Help and Safety")
        self.setAccessibleName("SmartModeler GIS help and safety")
        self.resize(760, 590)

        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        tabs.setAccessibleName("Help topics")
        tabs.addTab(
            self._page(
                """
                <h2>Build a workflow, step by step</h2>
                <ol>
                  <li><b>Add nodes.</b> Search the algorithm library
                      (<b>Ctrl+F</b>) and press <b>Enter</b> to add the
                      highlighted algorithm. Or load one of the example
                      workflows below the library and take it apart.</li>
                  <li><b>Connect them.</b> Drag from an output port to an input
                      port, or use <b>Connect nodes</b> (<b>Ctrl+Shift+C</b>) if
                      you would rather not drag. Types must match: a vector
                      output will not attach to a raster input.</li>
                  <li><b>Configure each node.</b> Select it and press
                      <b>Enter</b>, or double-click it. The Node Inspector on
                      the right shows what is still unset.</li>
                  <li><b>Run setup.</b> One dialog listing every input the
                      workflow needs. This is where you choose the actual
                      layers - nothing binds your data for you.</li>
                  <li><b>Validate</b>, then <b>Run</b> (<b>Ctrl+R</b>). Results
                      are added only after the whole workflow succeeds;
                      <b>Esc</b> cancels a run in progress.</li>
                  <li><b>Save.</b> The workflow is written as a native QGIS
                      <b>.model3</b> file, so it opens in the Processing
                      modeler too.</li>
                </ol>

                <h2>Letting the AI draft it</h2>
                <ol>
                  <li>Type what you need in the <b>AI Workflow Copilot</b> bar
                      at the top - for example <i>"calculate slope from the DEM
                      and classify it into planning suitability bands"</i> - and
                      press <b>Ctrl+Enter</b>.</li>
                  <li>Read the approval card. It lists the exact nodes and
                      connections it wants to add. Nothing changes until you
                      click <b>Apply</b>.</li>
                  <li>The new nodes arrive already laid out in data-flow order.
                      Nodes you positioned yourself are never moved.</li>
                  <li>Set the thresholds yourself. The AI proposes a structure;
                      the numbers in it are yours to own.</li>
                </ol>
                <p>Agent Workspace in <b>Current model</b> scope edits this same
                graph, and is the better place for a follow-up such as
                <i>"now add distance to roads"</i>.</p>

                <p>Results are temporary layers. Right-click anything worth
                keeping in the QGIS Layers panel and choose
                <b>Make permanent</b>.</p>
                """
            ),
            "Quick start",
        )
        tabs.addTab(
            self._page(
                """
                <h2>Keyboard</h2>
                <table>
                  <tr><td><b>Ctrl+F</b></td><td>Focus algorithm search</td></tr>
                  <tr><td><b>Enter</b></td><td>Add a palette item or configure a selected node</td></tr>
                  <tr><td><b>Ctrl+Shift+C</b></td><td>Connect nodes without pointer dragging</td></tr>
                  <tr><td><b>Delete</b></td><td>Remove selected canvas items</td></tr>
                  <tr><td><b>F</b></td><td>Fit graph when the canvas has focus</td></tr>
                  <tr><td><b>Ctrl+Shift+F</b></td><td>Fit graph from anywhere in Studio</td></tr>
                  <tr><td><b>Ctrl+R</b></td><td>Run workflow</td></tr>
                  <tr><td><b>Esc</b></td><td>Cancel an active workflow</td></tr>
                </table>
                <p>The Node Inspector includes a screen-reader-friendly workflow outline.</p>
                """
            ),
            "Keyboard",
        )
        tabs.addTab(
            self._page(
                """
                <h2>Privacy and safety</h2>
                <p>Offline planning and quick inspections make no provider request.</p>
                <p>When you submit to a connected provider, SmartModeler sends your
                typed message, its static instructions, and only the metadata enabled
                in that profile. Improve mode also sends a redacted workflow structure.
                SmartModeler does not automatically collect feature values, source
                paths, data-source URIs, project paths, or credentials.</p>
                <p>Anything you type yourself is part of your request. Provider
                retention is governed by that provider, not by SmartModeler.</p>
                <p>Agent proposals remain inert until an explicit Apply or Run click.
                Runs are restricted, cancellable, and temporary-output only.</p>
                """
            ),
            "Privacy and safety",
        )
        tabs.addTab(
            self._page(
                """
                <h2>Support</h2>
                <p><a href="https://github.com/YusufEminoglu/planx-smartmodeler/issues">
                Report a bug or request a feature</a></p>
                <p><a href="https://github.com/YusufEminoglu/planx-smartmodeler">
                Project documentation and source</a></p>
                <p>Include the QGIS version, SmartModeler version, exact steps,
                and the relevant Processing log. Remove private paths and
                connection details before posting logs.</p>
                """
            ),
            "Support",
        )
        layout.addWidget(tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        guide_button = buttons.addButton(
            "Open the illustrated guide", QDialogButtonBox.ButtonRole.ActionRole
        )
        guide_button.setToolTip(STEP_BY_STEP_GUIDE_URL)
        guide_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(STEP_BY_STEP_GUIDE_URL))
        )
        manual_button = buttons.addButton(
            "Reference manual", QDialogButtonBox.ButtonRole.ActionRole
        )
        manual_button.setToolTip(REFERENCE_MANUAL_URL)
        manual_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(REFERENCE_MANUAL_URL))
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _page(html: str) -> QTextBrowser:
        page = QTextBrowser()
        page.setOpenExternalLinks(True)
        page.setHtml(html)
        return page
