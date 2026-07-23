"""Shared Qt style for the SmartModeler studio."""

STUDIO_STYLE = """
QMainWindow, QDialog, QWidget {
    background: #111722;
    color: #EAF0F7;
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 10pt;
}
QToolBar {
    background: #151D29;
    border: none;
    border-bottom: 1px solid #29364A;
    spacing: 5px;
    padding: 6px 10px;
}
QToolButton {
    color: #DCE6F2;
    background: transparent;
    border: 1px solid transparent;
    border-radius: 6px;
    padding: 5px 8px;
}
QToolButton:hover { background: #202B3B; border-color: #34445C; }
QSplitter::handle { background: #29364A; width: 1px; }
QLineEdit, QPlainTextEdit, QComboBox, QSpinBox, QDoubleSpinBox {
    background: #182130;
    color: #F3F7FB;
    border: 1px solid #34445C;
    border-radius: 6px;
    padding: 6px 8px;
    selection-background-color: #2B6FE8;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus,
QSpinBox:focus, QDoubleSpinBox:focus { border-color: #72A7FF; }
QPushButton {
    background: #202B3B;
    color: #EAF0F7;
    border: 1px solid #34445C;
    border-radius: 6px;
    padding: 6px 12px;
}
QPushButton:hover { background: #29384C; border-color: #557093; }
QPushButton:disabled { color: #637083; background: #171E29; }
QPushButton#primaryButton { background: #2B6FE8; border-color: #4F8BFA; font-weight: 600; }
QPushButton#primaryButton:hover { background: #397CF0; }
QTreeWidget, QListWidget, QTableWidget {
    background: #141C28;
    alternate-background-color: #182231;
    border: 1px solid #29364A;
    border-radius: 6px;
    outline: none;
}
QTreeWidget::item, QListWidget::item { padding: 5px 4px; }
QTreeWidget::item:selected, QListWidget::item:selected {
    background: #244F89;
    color: white;
}
QHeaderView::section {
    background: #202B3B;
    color: #AFC0D5;
    border: none;
    border-right: 1px solid #34445C;
    padding: 6px;
}
QGroupBox {
    color: #C8D5E5;
    border: 1px solid #29364A;
    border-radius: 7px;
    margin-top: 9px;
    padding-top: 8px;
}
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
QLabel#panelEyebrow { color: #7F95B2; font-size: 8pt; font-weight: 700; letter-spacing: 1px; }
QLabel#mutedLabel, QLabel#settingsBlurb, QLabel#dialogSubtitle { color: #8293A9; font-size: 9pt; }
QLabel#settingsHeading, QLabel#dialogTitle { font-size: 18pt; font-weight: 650; color: #F6F9FC; }
QLabel#inspectorTitle { font-size: 13pt; font-weight: 650; color: #F6F9FC; }
QLabel#providerPill {
    color: #BBD5FF; background: #203A61; border: 1px solid #315B94;
    border-radius: 9px; padding: 3px 9px;
}
QLabel#providerHelp { color: #AFC0D5; background: #182130; border-radius: 6px; padding: 8px; }
QLabel#securityNote { color: #9DB4D0; background: #182638; border-left: 3px solid #4F8BFA; padding: 8px; }
QLabel#secretStorageStatus { color: #8293A9; font-size: 9pt; }
QLabel#secretStorageStatus[mode="encrypted"] { color: #57D3A0; }
QLabel#secretStorageStatus[mode="session"] { color: #F7B955; }
QLabel#connectedValue { color: #57D3A0; }
QFrame#aiPromptPanel { background: #151D29; border-bottom: 1px solid #29364A; }
QFrame#proposalBar { background: #121A25; border-bottom: 1px solid #253247; }
QStatusBar { background: #151D29; color: #9FB0C5; border-top: 1px solid #29364A; }
QProgressBar { border: none; background: #202B3B; border-radius: 3px; max-height: 6px; }
QProgressBar::chunk { background: #57D3A0; border-radius: 3px; }
QScrollBar:vertical { background: #141C28; width: 10px; }
QScrollBar::handle:vertical { background: #34445C; border-radius: 5px; min-height: 28px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
"""
