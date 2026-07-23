"""Searchable live QGIS Processing algorithm palette."""
from __future__ import annotations

from collections import defaultdict

from qgis.PyQt.QtCore import Qt, pyqtSignal
from qgis.PyQt.QtWidgets import (
    QGroupBox,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.algorithm_catalog import AlgorithmCatalog
from ..core.proposal_engine import SmartProposalEngine


class NodePaletteWidget(QWidget):
    """Discovers every algorithm currently enabled in QGIS Processing."""

    node_requested = pyqtSignal(str, str, str)
    package_requested = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 8, 10)
        layout.setSpacing(8)

        title = QLabel("ALGORITHM LIBRARY")
        title.setObjectName("panelEyebrow")
        layout.addWidget(title)
        self.search_bar = QLineEdit()
        self.search_bar.setClearButtonEnabled(True)
        self.search_bar.setPlaceholderText("Search installed algorithms...")
        self.search_bar.textChanged.connect(self.filter_tree)
        layout.addWidget(self.search_bar)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.itemDoubleClicked.connect(self.on_item_double_clicked)
        layout.addWidget(self.tree, 1)

        self.count_label = QLabel()
        self.count_label.setObjectName("mutedLabel")
        layout.addWidget(self.count_label)

        presets = QGroupBox("Starter workflows")
        preset_layout = QVBoxLayout(presets)
        self.preset_list = QListWidget()
        self.preset_list.setMaximumHeight(125)
        self.preset_list.itemDoubleClicked.connect(self.on_preset_double_clicked)
        preset_layout.addWidget(self.preset_list)
        layout.addWidget(presets)

        self.populate_tree()
        self.populate_presets()

    def populate_tree(self) -> None:
        self.tree.clear()
        grouped = defaultdict(list)
        records = AlgorithmCatalog.records()
        for record in records:
            grouped[f"{record.provider} / {record.group}"].append(record)
        for group_name in sorted(grouped):
            group_item = QTreeWidgetItem(self.tree, [group_name])
            group_item.setFlags(Qt.ItemFlag.ItemIsEnabled)
            for record in grouped[group_name]:
                child = QTreeWidgetItem(group_item, [record.name])
                child.setData(0, Qt.ItemDataRole.UserRole, record.algorithm_id)
                child.setData(0, Qt.ItemDataRole.UserRole + 1, record.group)
                child.setToolTip(
                    0,
                    f"{record.algorithm_id}\n{record.description}".strip(),
                )
            group_item.setExpanded(group_name.startswith("SmartModeler"))
        self.count_label.setText(f"{len(records)} executable algorithms available")

    def populate_presets(self) -> None:
        self.preset_list.clear()
        for template in SmartProposalEngine.get_starter_templates():
            item = QListWidgetItem(template["name"])
            item.setToolTip(template["description"])
            item.setData(Qt.ItemDataRole.UserRole, template["id"])
            self.preset_list.addItem(item)

    def filter_tree(self, text: str) -> None:
        query = text.strip().lower()
        visible_count = 0
        for index in range(self.tree.topLevelItemCount()):
            group = self.tree.topLevelItem(index)
            group_match = query in group.text(0).lower()
            group_visible = False
            for child_index in range(group.childCount()):
                child = group.child(child_index)
                algorithm_id = str(child.data(0, Qt.ItemDataRole.UserRole) or "")
                match = not query or group_match or query in child.text(
                    0).lower() or query in algorithm_id.lower()
                child.setHidden(not match)
                group_visible = group_visible or match
                visible_count += int(match)
            group.setHidden(not group_visible)
            if query and group_visible:
                group.setExpanded(True)
        self.count_label.setText(f"{visible_count} matching algorithms")

    def on_item_double_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        algorithm_id = item.data(0, Qt.ItemDataRole.UserRole)
        category = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if algorithm_id:
            self.node_requested.emit(str(algorithm_id), item.text(0), str(category))

    def on_preset_double_clicked(self, item: QListWidgetItem) -> None:
        template_id = item.data(Qt.ItemDataRole.UserRole)
        if template_id:
            self.package_requested.emit(str(template_id))
