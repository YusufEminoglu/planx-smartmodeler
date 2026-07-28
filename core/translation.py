"""Qt translation lifecycle with an English fallback."""
from __future__ import annotations

from pathlib import Path

from qgis.PyQt.QtCore import (
    QCoreApplication,
    QLocale,
    QSettings,
    QTranslator,
)


class TranslationManager:
    """Install the best shipped locale before any plugin widget is created."""

    CATALOG_PREFIX = "planx_smartmodeler"

    def __init__(self, plugin_dir: str) -> None:
        self._plugin_dir = Path(plugin_dir)
        self._translator: QTranslator | None = None

    def install(self) -> str:
        configured = str(
            QSettings().value(
                "locale/userLocale", QLocale.system().name()
            )
            or ""
        ).replace("-", "_")
        candidates = [configured]
        language = configured.split("_", 1)[0]
        if language and language not in candidates:
            candidates.append(language)
        for locale_name in candidates:
            path = (
                self._plugin_dir
                / "i18n"
                / f"{self.CATALOG_PREFIX}_{locale_name}.qm"
            )
            if not path.is_file():
                continue
            translator = QTranslator()
            if translator.load(str(path)):
                QCoreApplication.installTranslator(translator)
                self._translator = translator
                return locale_name
        return "en"

    def remove(self) -> None:
        if self._translator is None:
            return
        QCoreApplication.removeTranslator(self._translator)
        self._translator = None
