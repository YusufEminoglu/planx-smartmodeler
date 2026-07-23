"""Context-aware next-step proposal bar."""
from __future__ import annotations

from qgis.PyQt.QtCore import pyqtSignal
from qgis.PyQt.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from ..core.algorithm_catalog import AlgorithmCatalog
from ..core.graph_model import NodeDefinition
from ..core.proposal_engine import SmartProposalEngine


class SmartProposalBar(QFrame):
    proposal_selected = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("proposalBar")
        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(14, 6, 14, 6)
        self.layout.setSpacing(8)
        title = QLabel("NEXT STEP")
        title.setObjectName("panelEyebrow")
        self.layout.addWidget(title)
        self.button_layout = QHBoxLayout()
        self.layout.addLayout(self.button_layout)
        self.layout.addStretch()
        self.show_default_proposals()

    def clear_proposals(self) -> None:
        while self.button_layout.count():
            item = self.button_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def _add_button(self, algorithm_id: str, title: str, description: str = "") -> None:
        if not AlgorithmCatalog.algorithm_exists(algorithm_id):
            return
        button = QPushButton(title)
        button.setToolTip(description or f"Add {title} to the canvas")
        button.clicked.connect(
            lambda _checked=False, value=algorithm_id: self.proposal_selected.emit(value)
        )
        self.button_layout.addWidget(button)

    def show_default_proposals(self) -> None:
        self.clear_proposals()
        defaults = [
            ("native:buffer", "Buffer"),
            ("native:clip", "Clip"),
            ("native:extractbyexpression", "Filter"),
            ("native:centroids", "Centroids"),
            ("smart:number", "Numeric input"),
        ]
        for algorithm_id, title in defaults:
            self._add_button(algorithm_id, title)

    def update_for_node(self, node: NodeDefinition | None) -> None:
        if node is None or not node.outputs:
            self.show_default_proposals()
            return
        self.clear_proposals()
        output = next(iter(node.outputs.values()))
        proposals = SmartProposalEngine.get_proposals_for_port(output)
        for proposal in proposals[:5]:
            self._add_button(proposal.alg_id, proposal.title, proposal.description)
        if not proposals:
            self.show_default_proposals()
