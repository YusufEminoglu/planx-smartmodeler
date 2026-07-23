"""AI provider profiles and secure credential persistence."""
from __future__ import annotations

import json
import re
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

from qgis.core import QgsApplication, QgsSettings


@dataclass(frozen=True)
class ProviderDefinition:
    provider_id: str
    name: str
    default_endpoint: str
    default_model: str
    description: str
    requires_key: bool = True
    endpoint_editable: bool = False


PROVIDERS: Dict[str, ProviderDefinition] = {
    "offline": ProviderDefinition(
        "offline",
        "SmartModeler Offline",
        "",
        "",
        "Deterministic local GIS templates. No network and no API key.",
        requires_key=False,
    ),
    "openai": ProviderDefinition(
        "openai",
        "OpenAI Responses API",
        "https://api.openai.com/v1/responses",
        "gpt-5.6-luna",
        "Native Responses API with strict structured output.",
    ),
    "anthropic": ProviderDefinition(
        "anthropic",
        "Anthropic Messages API",
        "https://api.anthropic.com/v1/messages",
        "claude-sonnet-4-5",
        "Claude Messages API with schema-constrained tool output.",
    ),
    "gemini": ProviderDefinition(
        "gemini",
        "Google Gemini API",
        "https://generativelanguage.googleapis.com/v1beta",
        "gemini-3.5-flash",
        "Gemini generateContent with JSON Schema output.",
    ),
    "deepseek": ProviderDefinition(
        "deepseek",
        "DeepSeek API",
        "https://api.deepseek.com/chat/completions",
        "deepseek-v4-flash",
        "Direct DeepSeek Chat Completions connection with JSON output.",
    ),
    "ollama": ProviderDefinition(
        "ollama",
        "Ollama (local or cloud)",
        "http://localhost:11434/api/chat",
        "gpt-oss",
        "Local-first Ollama chat endpoint with structured output.",
        requires_key=False,
        endpoint_editable=True,
    ),
    "openai_compatible": ProviderDefinition(
        "openai_compatible",
        "OpenAI-compatible endpoint",
        "http://localhost:1234/v1/chat/completions",
        "",
        "Groq, OpenRouter, Together, Mistral, DeepSeek, LM Studio, vLLM and compatible gateways.",
        requires_key=False,
        endpoint_editable=True,
    ),
    "azure_openai": ProviderDefinition(
        "azure_openai",
        "Azure OpenAI",
        "",
        "",
        "Full Azure deployment endpoint; API key is sent in the api-key header.",
        endpoint_editable=True,
    ),
}


@dataclass
class AiProfile:
    profile_id: str
    name: str
    provider_id: str = "offline"
    model: str = ""
    endpoint: str = ""
    api_version: str = ""
    organization: str = ""
    temperature: float = 0.1
    timeout_seconds: int = 90
    include_project_context: bool = True
    include_algorithm_catalog: bool = True
    max_catalog_algorithms: int = 50

    @classmethod
    def create(cls, provider_id: str = "offline", name: str = "Offline") -> "AiProfile":
        provider = PROVIDERS[provider_id]
        return cls(
            profile_id=uuid.uuid4().hex,
            name=name,
            provider_id=provider_id,
            model=provider.default_model,
            endpoint=provider.default_endpoint,
        )

    @classmethod
    def from_dict(cls, data: Dict[str, object]) -> "AiProfile":
        allowed = set(cls.__dataclass_fields__)
        values = {key: value for key, value in data.items() if key in allowed}
        profile = cls(**values)
        if not isinstance(profile.profile_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_-]{8,80}", profile.profile_id
        ):
            profile.profile_id = uuid.uuid4().hex
        if profile.provider_id not in PROVIDERS:
            profile.provider_id = "openai_compatible"
        return profile

    def validate(self, api_key: str = "") -> List[str]:
        errors: List[str] = []
        provider = PROVIDERS[self.provider_id]
        if not self.name.strip():
            errors.append("Profile name is required.")
        if self.provider_id != "offline" and not self.model.strip():
            errors.append("Model or deployment name is required.")
        if self.provider_id != "offline":
            endpoint_error = validate_endpoint(self.endpoint)
            if endpoint_error:
                errors.append(endpoint_error)
        if provider.requires_key and not api_key.strip():
            errors.append("This provider requires an API key.")
        if not 10 <= int(self.timeout_seconds) <= 600:
            errors.append("Timeout must be between 10 and 600 seconds.")
        if not 5 <= int(self.max_catalog_algorithms) <= 200:
            errors.append("Algorithm context size must be between 5 and 200.")
        return errors


def validate_endpoint(endpoint: str) -> str:
    """Reject unsafe clear-text remote endpoints while permitting local runtimes."""
    try:
        parsed = urlparse(endpoint.strip())
    except ValueError:
        return "Endpoint URL is invalid."
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return "Endpoint must be an absolute HTTP(S) URL."
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and parsed.hostname.lower() not in local_hosts:
        return "Remote AI endpoints must use HTTPS; HTTP is allowed only for localhost."
    if parsed.username or parsed.password:
        return "Do not place credentials in the endpoint URL."
    return ""


class AiSettingsStore:
    """Stores profile metadata in QgsSettings and secrets in the QGIS vault.

    If the QGIS authentication vault is locked or disabled, a key is retained only
    in process memory. This keeps the connection usable without ever downgrading to
    plaintext persistence.
    """

    SETTINGS_PREFIX = "SmartModelerGIS/AI/v2/"
    PROFILE_KEY = SETTINGS_PREFIX + "profiles"
    ACTIVE_KEY = SETTINGS_PREFIX + "active_profile"
    AUTH_PREFIX = "smartmodeler/ai/profile/"
    LEGACY_PREFIX = "SmartModelerGIS/AI/"
    _SESSION_SECRETS: Dict[str, str] = {}

    def __init__(self, settings: Optional[QgsSettings] = None) -> None:
        self.settings = settings or QgsSettings()
        self._migrate_legacy_settings()

    def profiles(self) -> List[AiProfile]:
        raw = self.settings.value(self.PROFILE_KEY, "")
        profiles: List[AiProfile] = []
        if raw:
            try:
                values = json.loads(str(raw))
                profiles = [AiProfile.from_dict(value) for value in values]
            except (TypeError, ValueError):
                profiles = []
        if not profiles:
            profiles = [AiProfile.create()]
            self._write_profiles(profiles)
            self.settings.setValue(self.ACTIVE_KEY, profiles[0].profile_id)
        return profiles

    def active_profile(self) -> AiProfile:
        profiles = self.profiles()
        active_id = str(self.settings.value(self.ACTIVE_KEY, ""))
        return next((item for item in profiles if item.profile_id == active_id), profiles[0])

    def set_active(self, profile_id: str) -> None:
        if any(profile.profile_id == profile_id for profile in self.profiles()):
            self.settings.setValue(self.ACTIVE_KEY, profile_id)

    def save_profile(self, profile: AiProfile, api_key: Optional[str] = None) -> Tuple[bool, str]:
        profiles = self.profiles()
        updated = False
        for index, existing in enumerate(profiles):
            if existing.profile_id == profile.profile_id:
                profiles[index] = profile
                updated = True
                break
        if not updated:
            profiles.append(profile)

        storage_message = ""
        if api_key is not None:
            ok, storage_message = self._store_secret(
                profile.profile_id, api_key.strip()
            )
            if not ok:
                return False, storage_message
        self._write_profiles(profiles)
        self.set_active(profile.profile_id)
        return True, storage_message or "Profile saved."

    def delete_profile(self, profile_id: str) -> None:
        profiles = [item for item in self.profiles() if item.profile_id != profile_id]
        if not profiles:
            profiles = [AiProfile.create()]
        self._write_profiles(profiles)
        self._remove_secret(profile_id)
        if str(self.settings.value(self.ACTIVE_KEY, "")) == profile_id:
            self.settings.setValue(self.ACTIVE_KEY, profiles[0].profile_id)

    def secret(self, profile_id: str) -> str:
        session_value = self._SESSION_SECRETS.get(profile_id, "")
        if session_value:
            return session_value
        manager = self._unlocked_auth_manager()
        if manager is not None:
            value = manager.authSetting(self.AUTH_PREFIX + profile_id, "", True)
            if value:
                return str(value)
        return ""

    def secret_storage_mode(self, profile_id: str) -> str:
        """Return the secure storage state without triggering a password prompt."""
        if profile_id in self._SESSION_SECRETS:
            return "session"
        manager = self._usable_auth_manager()
        key = self.AUTH_PREFIX + profile_id
        if manager is not None and manager.existsAuthSetting(key):
            return (
                "encrypted"
                if manager.masterPasswordIsSet()
                else "encrypted_locked"
            )
        return "missing"

    def save_secret(self, profile_id: str, value: str) -> Tuple[bool, str]:
        """Store a key securely, with an explicit memory-only safety fallback."""
        return self._store_secret(profile_id, value.strip())

    def unlock_secure_storage(self) -> Tuple[bool, str]:
        """Ask QGIS to unlock or initialize its authentication vault."""
        manager = QgsApplication.authManager()
        if manager is None:
            return False, "QGIS Authentication Manager is unavailable."
        if not manager.ensureInitialized():
            return False, "QGIS could not initialize its authentication storage."
        if manager.isDisabled():
            return False, manager.disabledMessage() or "QGIS authentication is disabled."
        if not manager.masterPasswordIsSet() and manager.passwordHelperEnabled():
            manager.passwordHelperSync()
        if not manager.masterPasswordIsSet() and not manager.setMasterPassword(True):
            return False, "The QGIS secure vault remains locked."
        if not manager.masterPasswordIsSet():
            return False, "The QGIS secure vault remains locked."
        return True, "QGIS secure storage is unlocked."

    def _store_secret(self, profile_id: str, value: str) -> Tuple[bool, str]:
        key = self.AUTH_PREFIX + profile_id
        if not value:
            manager = self._unlocked_auth_manager()
            if manager is not None:
                manager.removeAuthSetting(key)
            self._SESSION_SECRETS.pop(profile_id, None)
            return True, "API key removed."

        manager = self._unlocked_auth_manager()
        if manager is not None:
            if manager.storeAuthSetting(key, value, True):
                self._SESSION_SECRETS.pop(profile_id, None)
                return True, "API key encrypted in the QGIS Authentication Database."

        self._SESSION_SECRETS[profile_id] = value
        return True, (
            "Profile saved. The API key is available for this QGIS session only; "
            "no authentication password is required. Optionally unlock the QGIS "
            "vault to keep the key after restart."
        )

    def _remove_secret(self, profile_id: str) -> None:
        self._SESSION_SECRETS.pop(profile_id, None)
        manager = self._unlocked_auth_manager()
        if manager is not None:
            manager.removeAuthSetting(self.AUTH_PREFIX + profile_id)

    @staticmethod
    def _usable_auth_manager():
        manager = QgsApplication.authManager()
        if manager is None or not manager.ensureInitialized() or manager.isDisabled():
            return None
        return manager

    @classmethod
    def _unlocked_auth_manager(cls):
        """Return the vault only when QGIS has already unlocked it explicitly."""
        manager = cls._usable_auth_manager()
        if manager is None or not manager.masterPasswordIsSet():
            return None
        return manager

    def _write_profiles(self, profiles: List[AiProfile]) -> None:
        payload = json.dumps([asdict(profile) for profile in profiles], separators=(",", ":"))
        self.settings.setValue(self.PROFILE_KEY, payload)

    def _migrate_legacy_settings(self) -> None:
        """Move the old single-provider settings and purge the plaintext key."""
        if self.settings.value(self.PROFILE_KEY, ""):
            return
        legacy_key_name = self.LEGACY_PREFIX + "api_key"
        legacy_provider_name = self.LEGACY_PREFIX + "provider_idx"
        legacy_endpoint_name = self.LEGACY_PREFIX + "endpoint"
        has_legacy = any(
            self.settings.contains(key)
            for key in (legacy_key_name, legacy_provider_name, legacy_endpoint_name)
        )
        if not has_legacy:
            return

        try:
            legacy_index = int(self.settings.value(legacy_provider_name, 0))
        except (TypeError, ValueError):
            legacy_index = 0
        provider_id = {0: "offline", 1: "openai", 2: "ollama"}.get(
            legacy_index, "offline"
        )
        provider = PROVIDERS[provider_id]
        profile = AiProfile.create(provider_id, provider.name)
        legacy_endpoint = str(self.settings.value(legacy_endpoint_name, "") or "").strip()
        if legacy_endpoint and provider.endpoint_editable:
            if provider_id == "ollama" and legacy_endpoint.endswith("/api/generate"):
                legacy_endpoint = legacy_endpoint[: -len("/api/generate")] + "/api/chat"
            profile.endpoint = legacy_endpoint
        self._write_profiles([profile])
        self.settings.setValue(self.ACTIVE_KEY, profile.profile_id)

        legacy_key = str(self.settings.value(legacy_key_name, "") or "").strip()
        if legacy_key:
            self._store_secret(profile.profile_id, legacy_key)
        for key in (legacy_key_name, legacy_provider_name, legacy_endpoint_name):
            self.settings.remove(key)


class FakeQgsSettings:
    """In-memory QgsSettings-compatible fake for settings-neutral test isolation."""

    def __init__(self) -> None:
        self._store: Dict[str, Any] = {}

    def value(self, key: str, default_value: Any = None, type: Any = None) -> Any:
        if key in self._store:
            val = self._store[key]
            if type is str and val is not None:
                return str(val)
            return val
        return default_value

    def setValue(self, key: str, value: Any) -> None:
        self._store[key] = value

    def contains(self, key: str) -> bool:
        return key in self._store

    def remove(self, key: str) -> None:
        self._store.pop(key, None)

    def clear(self) -> None:
        self._store.clear()


@contextmanager
def scoped_ai_settings_isolation(shared_fake: Optional[FakeQgsSettings] = None):
    """Isolate all AiSettingsStore instances created during the block using an in-memory settings fake.

    Restores original QgsSettings class binding and verifies/restores real settings existence
    and exact raw values upon exit.
    """
    import planx_smartmodeler.core.ai_settings as ai_settings_mod

    original_qgs_settings = ai_settings_mod.QgsSettings
    target_settings = shared_fake if shared_fake is not None else original_qgs_settings()
    fake = shared_fake if shared_fake is not None else FakeQgsSettings()

    target_keys = (
        ai_settings_mod.AiSettingsStore.PROFILE_KEY,
        ai_settings_mod.AiSettingsStore.ACTIVE_KEY,
        ai_settings_mod.AiSettingsStore.LEGACY_PREFIX + "api_key",
        ai_settings_mod.AiSettingsStore.LEGACY_PREFIX + "provider_idx",
        ai_settings_mod.AiSettingsStore.LEGACY_PREFIX + "endpoint",
    )
    snapshot = {}
    for k in target_keys:
        exists = target_settings.contains(k)
        val = target_settings.value(k, None) if exists else None
        snapshot[k] = (exists, val)

    ai_settings_mod.QgsSettings = lambda: fake
    try:
        yield fake
    finally:
        ai_settings_mod.QgsSettings = original_qgs_settings
        for k, (exists, val) in snapshot.items():
            if exists:
                target_settings.setValue(k, val)
            else:
                target_settings.remove(k)

