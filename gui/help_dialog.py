"""In-application quick start, keyboard, privacy, and support guide."""
from __future__ import annotations

from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
)


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
                <h2>Quick start</h2>
                <ol>
                  <li>Add an installed algorithm or starter workflow.</li>
                  <li>Configure a node with Enter or double-click.</li>
                  <li>Connect ports on the canvas or use <b>Connect nodes</b>.</li>
                  <li>Review all inputs in <b>Run setup</b>, then Validate.</li>
                  <li>Run. Results are added only after the full workflow succeeds.</li>
                </ol>
                <p>Save valuable temporary results from the QGIS Layers panel.</p>
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
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _page(html: str) -> QTextBrowser:
        page = QTextBrowser()
        page.setOpenExternalLinks(True)
        page.setHtml(html)
        return page
