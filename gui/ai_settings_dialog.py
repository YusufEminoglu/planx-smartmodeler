"""Multi-profile AI provider settings for SmartModeler GIS."""
from __future__ import annotations

from dataclasses import replace

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from ..core.ai_client import AiNetworkClient
from ..core.ai_mcp_bridge import AiMcpBridge, AiResponseError
from ..core.ai_settings import AiProfile, AiSettingsStore, PROVIDERS
from ..core.algorithm_catalog import AlgorithmCatalog
from ..core.prompt_context import PromptContextLoader


class AiSettingsDialog(QDialog):
    """Creates provider profiles without ever persisting plaintext secrets."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("SmartModeler AI Connections")
        self.resize(860, 620)
        self.store = AiSettingsStore()
        self.profiles = self.store.profiles()
        self.current: AiProfile | None = None
        self.loading = False
        self.client = AiNetworkClient(self)
        self.client.succeeded.connect(self._test_succeeded)
        self.client.failed.connect(self._test_failed)
        self.client.busy_changed.connect(self._test_busy)
        self._build_ui()
        self._populate_profiles()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        heading = QLabel("AI connections")
        heading.setObjectName("settingsHeading")
        blurb = QLabel(
            "Connect cloud or local models. Profile metadata stays in QGIS settings; "
            "API keys work in session memory without any password. The optional QGIS "
            "vault keeps a key across restarts."
        )
        blurb.setWordWrap(True)
        blurb.setObjectName("settingsBlurb")
        root.addWidget(heading)
        root.addWidget(blurb)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, 1)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 8, 8, 0)
        self.profile_list = QListWidget()
        self.profile_list.currentItemChanged.connect(self._profile_selected)
        left_layout.addWidget(self.profile_list, 1)
        row = QHBoxLayout()
        add = QPushButton("New")
        add.clicked.connect(self._add_profile)
        duplicate = QPushButton("Duplicate")
        duplicate.clicked.connect(self._duplicate_profile)
        delete = QPushButton("Delete")
        delete.clicked.connect(self._delete_profile)
        row.addWidget(add)
        row.addWidget(duplicate)
        row.addWidget(delete)
        left_layout.addLayout(row)
        splitter.addWidget(left)

        right = QWidget()
        form = QFormLayout(right)
        form.setContentsMargins(18, 8, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        self.name_edit = QLineEdit()
        form.addRow("Profile name", self.name_edit)

        self.provider_combo = QComboBox()
        for provider in PROVIDERS.values():
            self.provider_combo.addItem(provider.name, provider.provider_id)
        self.provider_combo.currentIndexChanged.connect(self._provider_changed)
        form.addRow("Provider", self.provider_combo)

        self.provider_help = QLabel()
        self.provider_help.setWordWrap(True)
        self.provider_help.setObjectName("providerHelp")
        form.addRow("", self.provider_help)

        self.model_edit = QLineEdit()
        self.model_edit.setPlaceholderText("Model or deployment name")
        form.addRow("Model", self.model_edit)

        self.endpoint_edit = QLineEdit()
        self.endpoint_edit.setPlaceholderText("https://provider.example/v1/chat/completions")
        form.addRow("Endpoint", self.endpoint_edit)

        self.api_version_edit = QLineEdit()
        self.api_version_edit.setPlaceholderText("Optional Azure api-version")
        self.api_version_label = QLabel("API version")
        form.addRow(self.api_version_label, self.api_version_edit)

        self.organization_edit = QLineEdit()
        self.organization_label = QLabel("Organization")
        form.addRow(self.organization_label, self.organization_edit)

        key_row = QHBoxLayout()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_edit.setPlaceholderText("Never written to plaintext plugin settings")
        reveal = QPushButton("Show")
        reveal.setCheckable(True)
        reveal.toggled.connect(
            lambda shown: self.key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if shown else QLineEdit.EchoMode.Password
            )
        )
        key_row.addWidget(self.key_edit, 1)
        key_row.addWidget(reveal)
        form.addRow("API key", key_row)

        storage_row = QHBoxLayout()
        self.key_status = QLabel()
        self.key_status.setObjectName("secretStorageStatus")
        self.unlock_button = QPushButton("Unlock vault (optional)")
        self.unlock_button.setToolTip(
            "Uses the QGIS Authentication Database master password, not the AI API key."
        )
        self.unlock_button.clicked.connect(self._unlock_secure_storage)
        storage_row.addWidget(self.key_status, 1)
        storage_row.addWidget(self.unlock_button)
        form.addRow("Key storage", storage_row)

        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        form.addRow("Temperature", self.temperature)

        self.timeout = QSpinBox()
        self.timeout.setRange(10, 600)
        self.timeout.setSuffix(" s")
        form.addRow("Timeout", self.timeout)

        self.include_project = QCheckBox("Send layer ids, names, CRS and fields")
        form.addRow("Project context", self.include_project)
        self.include_catalog = QCheckBox("Send relevant installed algorithm signatures")
        form.addRow("Algorithm context", self.include_catalog)
        self.catalog_limit = QSpinBox()
        self.catalog_limit.setRange(5, 200)
        form.addRow("Algorithm limit", self.catalog_limit)

        self.security_note = QLabel(
            "Remote endpoints must use HTTPS. Plain HTTP is accepted only for localhost. "
            "Project context never includes feature values. The QGIS vault password is "
            "optional and is not your provider API key."
        )
        self.security_note.setWordWrap(True)
        self.security_note.setObjectName("securityNote")
        form.addRow("Security", self.security_note)
        splitter.addWidget(right)
        splitter.setSizes([250, 600])

        actions = QHBoxLayout()
        self.test_button = QPushButton("Test connection")
        self.test_button.clicked.connect(self._test_connection)
        save = QPushButton("Save profile")
        save.setObjectName("primaryButton")
        save.clicked.connect(self._save_profile)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        actions.addWidget(self.test_button)
        actions.addStretch()
        actions.addWidget(close)
        actions.addWidget(save)
        root.addLayout(actions)

    def _populate_profiles(self, selected_id: str = "") -> None:
        self.profile_list.clear()
        active_id = selected_id or self.store.active_profile().profile_id
        selected_row = 0
        for index, profile in enumerate(self.profiles):
            item = QListWidgetItem(profile.name)
            item.setData(Qt.ItemDataRole.UserRole, profile.profile_id)
            item.setToolTip(PROVIDERS[profile.provider_id].name)
            self.profile_list.addItem(item)
            if profile.profile_id == active_id:
                selected_row = index
        self.profile_list.setCurrentRow(selected_row)

    def _profile_selected(self, current: QListWidgetItem | None, _previous) -> None:
        if current is None:
            return
        profile_id = str(current.data(Qt.ItemDataRole.UserRole))
        profile = next(item for item in self.profiles if item.profile_id == profile_id)
        self.current = profile
        self.loading = True
        self.name_edit.setText(profile.name)
        self.provider_combo.setCurrentIndex(self.provider_combo.findData(profile.provider_id))
        self.model_edit.setText(profile.model)
        self.endpoint_edit.setText(profile.endpoint)
        self.api_version_edit.setText(profile.api_version)
        self.organization_edit.setText(profile.organization)
        self.key_edit.setText(self.store.secret(profile.profile_id))
        self.temperature.setValue(profile.temperature)
        self.timeout.setValue(profile.timeout_seconds)
        self.include_project.setChecked(profile.include_project_context)
        self.include_catalog.setChecked(profile.include_algorithm_catalog)
        self.catalog_limit.setValue(profile.max_catalog_algorithms)
        self.loading = False
        self._update_provider_visibility()

    def _provider_changed(self) -> None:
        provider_id = str(self.provider_combo.currentData())
        provider = PROVIDERS[provider_id]
        if not self.loading:
            self.model_edit.setText(provider.default_model)
            self.endpoint_edit.setText(provider.default_endpoint)
        self._update_provider_visibility()

    def _update_provider_visibility(self) -> None:
        provider_id = str(self.provider_combo.currentData())
        provider = PROVIDERS[provider_id]
        self.provider_help.setText(provider.description)
        offline = provider_id == "offline"
        self.model_edit.setEnabled(not offline)
        self.endpoint_edit.setEnabled(not offline and (
            provider.endpoint_editable or not provider.default_endpoint))
        self.key_edit.setEnabled(not offline)
        azure = provider_id == "azure_openai"
        self.api_version_label.setVisible(azure)
        self.api_version_edit.setVisible(azure)
        openai = provider_id == "openai"
        self.organization_label.setVisible(openai)
        self.organization_edit.setVisible(openai)
        self.key_status.setVisible(not offline)
        self.unlock_button.setVisible(not offline)
        self._update_secret_status()

    def _update_secret_status(self) -> None:
        if self.current is None:
            mode = "missing"
        else:
            mode = self.store.secret_storage_mode(self.current.profile_id)
        labels = {
            "encrypted": "● Encrypted in the QGIS vault",
            "encrypted_locked": "● Encrypted key present — unlock vault to use",
            "session": "● Session only — no password required",
            "missing": "○ No API key stored",
        }
        self.key_status.setText(labels[mode])
        self.key_status.setProperty("mode", mode)
        style = self.key_status.style()
        style.unpolish(self.key_status)
        style.polish(self.key_status)

    def _unlock_secure_storage(self) -> None:
        ok, message = self.store.unlock_secure_storage()
        if not ok:
            QMessageBox.information(
                self,
                "QGIS vault was not unlocked",
                message
                + "\n\nThis prompt expects the QGIS Authentication Database master "
                "password, not your AI API key. You can ignore the vault and keep "
                "using the session-only key normally.",
            )
            self._update_secret_status()
            return
        key = self.key_edit.text().strip()
        if self.current is not None and key:
            ok, message = self.store.save_secret(self.current.profile_id, key)
        self._update_secret_status()
        if not ok or (
            self.current is not None
            and self.store.secret_storage_mode(self.current.profile_id) == "session"
        ):
            QMessageBox.warning(self, "API key is session-only", message)
            return
        QMessageBox.information(self, "QGIS vault ready", message)

    def _profile_from_form(self) -> AiProfile:
        profile = self.current or AiProfile.create()
        return replace(
            profile,
            name=self.name_edit.text().strip(),
            provider_id=str(self.provider_combo.currentData()),
            model=self.model_edit.text().strip(),
            endpoint=self.endpoint_edit.text().strip(),
            api_version=self.api_version_edit.text().strip(),
            organization=self.organization_edit.text().strip(),
            temperature=self.temperature.value(),
            timeout_seconds=self.timeout.value(),
            include_project_context=self.include_project.isChecked(),
            include_algorithm_catalog=self.include_catalog.isChecked(),
            max_catalog_algorithms=self.catalog_limit.value(),
        )

    def _save_profile(self) -> bool:
        profile = self._profile_from_form()
        key = self.key_edit.text().strip()
        errors = profile.validate(key)
        if errors:
            QMessageBox.warning(self, "Profile needs attention", "\n".join(errors))
            return False
        ok, message = self.store.save_profile(profile, key)
        if not ok:
            QMessageBox.critical(self, "Could not save API key", message)
            return False
        self.current = profile
        self.profiles = self.store.profiles()
        self._populate_profiles(profile.profile_id)
        if self.store.secret_storage_mode(profile.profile_id) == "session":
            QMessageBox.information(
                self, "Profile saved — session-only key", message
            )
        else:
            QMessageBox.information(self, "Profile saved", message)
        return True

    def _add_profile(self) -> None:
        profile = AiProfile.create("openai_compatible", "New connection")
        self.profiles.append(profile)
        self.current = profile
        self._populate_profiles(profile.profile_id)

    def _duplicate_profile(self) -> None:
        if self.current is None:
            return
        copy = AiProfile.create(self.current.provider_id, self.current.name + " copy")
        copy = replace(
            copy,
            model=self.current.model,
            endpoint=self.current.endpoint,
            api_version=self.current.api_version,
            organization=self.current.organization,
            temperature=self.current.temperature,
            timeout_seconds=self.current.timeout_seconds,
            include_project_context=self.current.include_project_context,
            include_algorithm_catalog=self.current.include_algorithm_catalog,
            max_catalog_algorithms=self.current.max_catalog_algorithms,
        )
        self.profiles.append(copy)
        self._populate_profiles(copy.profile_id)

    def _delete_profile(self) -> None:
        if self.current is None:
            return
        if QMessageBox.question(
            self,
            "Delete profile",
            f"Delete '{self.current.name}' and its stored API key?",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.store.delete_profile(self.current.profile_id)
        self.profiles = self.store.profiles()
        self.current = None
        self._populate_profiles()

    def _test_connection(self) -> None:
        profile = self._profile_from_form()
        key = self.key_edit.text().strip()
        errors = profile.validate(key)
        if errors:
            QMessageBox.warning(self, "Profile needs attention", "\n".join(errors))
            return
        if profile.provider_id == "offline":
            result = AiMcpBridge.generate_offline("Buffer the active vector layer")
            QMessageBox.information(
                self, "Offline planner ready", f"Created {len(result.graph.nodes)} validated nodes."
            )
            return
        project_context = AlgorithmCatalog.project_context() if profile.include_project_context else ""
        catalog = (
            AlgorithmCatalog.compact_ai_catalog(
                "buffer vector layer", profile.max_catalog_algorithms)
            if profile.include_algorithm_catalog
            else ""
        )
        system = PromptContextLoader().build(project_context, catalog)
        self.client.generate(
            profile,
            key,
            system,
            "Create the smallest valid workflow that accepts a vector layer and buffers it. "
            "This is a connection test; leave missing layer inputs unconfigured.",
        )

    def _test_busy(self, busy: bool) -> None:
        self.test_button.setEnabled(not busy)
        self.test_button.setText("Testing..." if busy else "Test connection")

    def _test_succeeded(self, response: str) -> None:
        try:
            result = AiMcpBridge.parse_response(response)
        except AiResponseError as error:
            QMessageBox.warning(
                self,
                "Provider connected, invalid graph",
                f"The provider answered, but did not follow the SmartModeler contract:\n{error}",
            )
            return
        if not result.graph.nodes:
            QMessageBox.warning(
                self,
                "Provider connected, empty workflow",
                "The provider answered with valid JSON but did not create a test workflow.",
            )
            return
        QMessageBox.information(
            self,
            "Connection verified",
            f"Provider returned a validated {len(result.graph.nodes)}-node QGIS workflow.",
        )

    def _test_failed(self, message: str) -> None:
        QMessageBox.critical(self, "Connection failed", message)
