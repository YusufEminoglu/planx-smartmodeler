from __future__ import annotations

import sys
import types
import unittest


qgis_module = sys.modules.setdefault("qgis", types.ModuleType("qgis"))
core_module = sys.modules.setdefault("qgis.core", types.ModuleType("qgis.core"))
if not hasattr(core_module, "QgsApplication"):
    core_module.QgsApplication = type("QgsApplication", (), {})
if not hasattr(core_module, "QgsSettings"):
    core_module.QgsSettings = type("QgsSettings", (), {})
qgis_module.core = core_module

from qgis.core import QgsApplication

from planx_smartmodeler.core.ai_settings import AiSettingsStore, PROVIDERS


class FakeAuthManager:
    def __init__(self, can_store: bool = False, unlocked: bool = False) -> None:
        self.can_store = can_store
        self.unlocked = unlocked
        self.values = {}
        self.auth_reads = 0
        self.store_calls = 0

    def ensureInitialized(self) -> bool:
        return True

    def isDisabled(self) -> bool:
        return False

    def disabledMessage(self) -> str:
        return ""

    def passwordHelperEnabled(self) -> bool:
        return False

    def passwordHelperSync(self) -> bool:
        return False

    def masterPasswordIsSet(self) -> bool:
        return self.unlocked

    def setMasterPassword(self, _verify: bool) -> bool:
        self.unlocked = True
        return True

    def authSetting(self, key: str, default: str, _decrypt: bool):
        self.auth_reads += 1
        return self.values.get(key, default)

    def existsAuthSetting(self, key: str) -> bool:
        return key in self.values

    def storeAuthSetting(self, key: str, value: str, _encrypt: bool) -> bool:
        self.store_calls += 1
        if not self.can_store:
            return False
        self.values[key] = value
        return True

    def removeAuthSetting(self, key: str) -> bool:
        self.values.pop(key, None)
        return True


class AiSettingsSecretTests(unittest.TestCase):
    def setUp(self) -> None:
        AiSettingsStore._SESSION_SECRETS.clear()
        self.store = object.__new__(AiSettingsStore)

    def tearDown(self) -> None:
        AiSettingsStore._SESSION_SECRETS.clear()

    @staticmethod
    def _use_manager(manager: FakeAuthManager) -> None:
        QgsApplication.authManager = staticmethod(lambda: manager)

    def test_locked_vault_uses_session_memory_without_failing_save(self) -> None:
        manager = FakeAuthManager(can_store=True)
        self._use_manager(manager)
        ok, message = self.store.save_secret("profile123", "credential-value")
        self.assertTrue(ok)
        self.assertIn("session only", message)
        self.assertEqual(self.store.secret_storage_mode("profile123"), "session")
        self.assertEqual(self.store.secret("profile123"), "credential-value")
        self.assertEqual(manager.store_calls, 0)
        self.assertEqual(manager.auth_reads, 0)

    def test_locked_encrypted_key_is_reported_without_decryption_prompt(self) -> None:
        manager = FakeAuthManager()
        manager.values["smartmodeler/ai/profile/profile123"] = "encrypted-value"
        self._use_manager(manager)
        self.assertEqual(
            self.store.secret_storage_mode("profile123"), "encrypted_locked"
        )
        self.assertEqual(self.store.secret("profile123"), "")
        self.assertEqual(manager.auth_reads, 0)

    def test_encrypted_save_replaces_session_copy(self) -> None:
        self._use_manager(FakeAuthManager())
        self.store.save_secret("profile123", "credential-value")
        manager = FakeAuthManager(can_store=True, unlocked=True)
        self._use_manager(manager)
        ok, _message = self.store.save_secret("profile123", "credential-value")
        self.assertTrue(ok)
        self.assertEqual(self.store.secret_storage_mode("profile123"), "encrypted")
        self.assertNotIn("profile123", AiSettingsStore._SESSION_SECRETS)

    def test_unlock_uses_qgis_master_password_flow(self) -> None:
        manager = FakeAuthManager()
        self._use_manager(manager)
        ok, message = self.store.unlock_secure_storage()
        self.assertTrue(ok)
        self.assertTrue(manager.masterPasswordIsSet())
        self.assertIn("unlocked", message)

    def test_deepseek_has_a_direct_current_api_preset(self) -> None:
        provider = PROVIDERS["deepseek"]
        self.assertEqual(provider.default_endpoint, "https://api.deepseek.com/chat/completions")
        self.assertEqual(provider.default_model, "deepseek-v4-flash")
        self.assertTrue(provider.requires_key)


if __name__ == "__main__":
    unittest.main()
