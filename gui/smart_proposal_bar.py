"""Smart Proposal & Contextual Tip Bar widget for SmartModeler GIS."""
from qgis.PyQt.QtCore import pyqtSignal, Qt
from qgis.PyQt.QtWidgets import QWidget, QHBoxLayout, QLabel, QPushButton, QFrame
from ..core.proposal_engine import SmartProposalEngine, ProposalRecommendation
from ..core.graph_model import NodeDefinition, NodePort, SocketType


class SmartProposalBar(QFrame):
    """Context-aware auto-suggestion tip bar."""

    proposal_selected = pyqtSignal(str)  # Alg ID to insert

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setStyleSheet("""
            QFrame {
                background-color: #1E222A;
                border-bottom: 1px solid #282C34;
                padding: 4px;
            }
            QLabel {
                color: #00E5FF;
                font-weight: bold;
                font-size: 11px;
            }
            QPushButton {
                background-color: #282C34;
                color: #ECEFF1;
                border: 1px solid #3E4451;
                border-radius: 12px;
                padding: 4px 10px;
                font-size: 11px;
            }
            QPushButton:hover {
                background-color: #00E5FF;
                color: #1E222A;
                font-weight: bold;
            }
        """)

        self.layout = QHBoxLayout(self)
        self.layout.setContentsMargins(10, 4, 10, 4)
        self.layout.setSpacing(8)

        self.lbl_title = QLabel("💡 Smart Proposal:")
        self.layout.addWidget(self.lbl_title)

        self.btn_container = QHBoxLayout()
        self.layout.addLayout(self.btn_container)
        self.layout.addStretch()

        self.show_default_proposals()

    def clear_proposals(self):
        while self.btn_container.count():
            item = self.btn_container.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

    def show_default_proposals(self):
        self.clear_proposals()
        defaults = [
            ("native:buffer", "Vector Buffer"),
            ("native:clip", "Clip Boundary"),
            ("native:extractbyattribute", "Filter Attribute"),
            ("native:centroids", "Centroids"),
            ("smart:slider", "+ Value Slider")
        ]
        for alg_id, title in defaults:
            btn = QPushButton(title)
            btn.setToolTip(f"Insert {title} into canvas")
            btn.clicked.connect(lambda checked, a=alg_id: self.proposal_selected.emit(a))
            self.btn_container.addWidget(btn)

    def update_for_node(self, node: NodeDefinition):
        if not node:
            self.show_default_proposals()
            return

        # Fetch output port proposals
        self.clear_proposals()
        if node.outputs:
            first_port = next(iter(node.outputs.values()))
            proposals = SmartProposalEngine.get_proposals_for_port(first_port)
            for p in proposals[:5]:
                btn = QPushButton(f"+ {p.title}")
                btn.setToolTip(p.description)
                btn.clicked.connect(lambda checked, a=p.alg_id: self.proposal_selected.emit(a))
                self.btn_container.addWidget(btn)
        else:
            self.show_default_proposals()
