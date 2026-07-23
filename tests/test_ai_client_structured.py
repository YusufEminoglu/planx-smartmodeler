"""Pure-Python tests for AiNetworkClient's generalized structured-output path.

Stubs the minimal qgis.core / qgis.PyQt surface AiNetworkClient needs at
import/class-definition time, following the same convention already used by
test_ai_contract.py / test_agent_runtime_tools.py, so these run without a
real QGIS installation and never perform a real network request.
"""
from __future__ import annotations

import json
import sys
import types
import unittest


def _install_qgis_stubs() -> None:
    if "qgis.core" in sys.modules and "qgis.PyQt.QtNetwork" in sys.modules:
        return

    qgis_module = sys.modules.setdefault("qgis", types.ModuleType("qgis"))
    core_module = sys.modules.setdefault("qgis.core", types.ModuleType("qgis.core"))
    dummy_core_names = (
        "Qgis",
        "QgsApplication",
        "QgsProcessingParameterBoolean",
        "QgsProcessingParameterDefinition",
        "QgsProcessingParameterFeatureSource",
        "QgsProcessingParameterField",
        "QgsProcessingParameterFile",
        "QgsProcessingParameterMapLayer",
        "QgsProcessingParameterMultipleLayers",
        "QgsProcessingParameterNumber",
        "QgsProcessingParameterRasterDestination",
        "QgsProcessingParameterRasterLayer",
        "QgsProcessingParameterString",
        "QgsProcessingParameterVectorDestination",
        "QgsProcessingParameterVectorLayer",
        "QgsProject",
        "QgsRasterLayer",
        "QgsVectorLayer",
        "QgsNetworkAccessManager",
    )
    for name in dummy_core_names:
        if not hasattr(core_module, name):
            setattr(core_module, name, type(name, (), {}))

    class _FakeSettings:
        def __init__(self) -> None:
            self._data: dict = {}

        def value(self, key: str, default: str = ""):
            return self._data.get(key, default)

        def setValue(self, key: str, val) -> None:
            self._data[key] = val

        def remove(self, key: str) -> None:
            self._data.pop(key, None)

        def contains(self, key: str) -> bool:
            return key in self._data

    setattr(core_module.QgsApplication, "authManager", staticmethod(lambda: None))
    setattr(core_module, "QgsSettings", _FakeSettings)
    qgis_module.core = core_module
    sys.modules["qgis"] = qgis_module
    sys.modules["qgis.core"] = core_module

    class _Signal:
        def __init__(self, *args, **kwargs) -> None:
            self._slots = []

        def connect(self, slot) -> None:
            self._slots.append(slot)

        def emit(self, *args) -> None:
            for slot in list(self._slots):
                slot(*args)

    def pyqtSignal(*_args, **_kwargs):
        return _Signal()

    class QObject:
        def __init__(self, parent=None) -> None:
            self._parent = parent

    class QTimer(QObject):
        def setSingleShot(self, _value) -> None:
            pass

        def start(self, _ms) -> None:
            pass

        def stop(self) -> None:
            pass

        def deleteLater(self) -> None:
            pass

    class QUrl:
        def __init__(self, *_args) -> None:
            pass

    class QByteArray:
        def __init__(self, *_args) -> None:
            pass

    pyqt_module = sys.modules.setdefault("qgis.PyQt", types.ModuleType("qgis.PyQt"))
    qtcore_module = types.ModuleType("qgis.PyQt.QtCore")
    qtcore_module.QObject = QObject
    qtcore_module.QTimer = QTimer
    qtcore_module.QUrl = QUrl
    qtcore_module.QByteArray = QByteArray
    qtcore_module.pyqtSignal = pyqtSignal
    pyqt_module.QtCore = qtcore_module
    sys.modules["qgis.PyQt.QtCore"] = qtcore_module

    class _NetworkErrorEnum:
        NoError = 0
        OperationCanceledError = 1
        ConnectionRefusedError = 2
        HostNotFoundError = 3

    class QNetworkReply:
        NetworkError = _NetworkErrorEnum

    class _RequestAttribute:
        HttpStatusCodeAttribute = 0

    class QNetworkRequest:
        Attribute = _RequestAttribute

        def __init__(self, *_args) -> None:
            pass

        def setRawHeader(self, *_args) -> None:
            pass

    qtnetwork_module = types.ModuleType("qgis.PyQt.QtNetwork")
    qtnetwork_module.QNetworkReply = QNetworkReply
    qtnetwork_module.QNetworkRequest = QNetworkRequest
    pyqt_module.QtNetwork = qtnetwork_module
    sys.modules["qgis.PyQt.QtNetwork"] = qtnetwork_module


_install_qgis_stubs()

from planx_smartmodeler.core.ai_client import (  # noqa: E402
    AiNetworkClient,
    StructuredResponseContract,
)
from planx_smartmodeler.core.ai_mcp_bridge import AiMcpBridge, AiResponseError  # noqa: E402
from planx_smartmodeler.core.ai_settings import AiProfile  # noqa: E402

GRAPH_SCHEMA = AiMcpBridge.response_schema()
AGENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": ["tool_calls", "final"]},
        "assistant_text": {"type": "string"},
        "tool_calls": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["action", "assistant_text", "tool_calls"],
}
AGENT_CONTRACT = StructuredResponseContract(
    schema=AGENT_SCHEMA, name="agent_turn", description="Return the next agent_turn."
)

ALL_PROVIDERS = (
    "openai",
    "anthropic",
    "gemini",
    "deepseek",
    "ollama",
    "openai_compatible",
    "azure_openai",
)


def make_profile(provider_id: str) -> AiProfile:
    profile = AiProfile.create(provider_id, provider_id)
    if provider_id == "azure_openai":
        profile.endpoint = "https://example.openai.azure.com/openai/deployments/gpt/chat/completions"
        profile.api_version = "2024-05-01"
    elif provider_id == "openai_compatible":
        profile.endpoint = "http://localhost:1234/v1/chat/completions"
    return profile


class StructuredResponseContractTests(unittest.TestCase):
    def test_default_contract_matches_the_graph_planning_schema(self) -> None:
        contract = StructuredResponseContract(schema=GRAPH_SCHEMA)
        self.assertEqual(contract.name, "qgis_workflow")
        self.assertTrue(contract.description)

    def test_rejects_empty_schema(self) -> None:
        with self.assertRaises(ValueError):
            StructuredResponseContract(schema={})

    def test_rejects_invalid_name(self) -> None:
        for bad_name in ("", "has space", "1leadingdigit", "x" * 65):
            with self.assertRaises(ValueError):
                StructuredResponseContract(schema=GRAPH_SCHEMA, name=bad_name)

    def test_rejects_invalid_description(self) -> None:
        with self.assertRaises(ValueError):
            StructuredResponseContract(schema=GRAPH_SCHEMA, description="")
        with self.assertRaises(ValueError):
            StructuredResponseContract(schema=GRAPH_SCHEMA, description="x" * 501)


class WorkflowStudioRequestUnchangedTests(unittest.TestCase):
    """Existing Workflow Studio request construction must remain semantically
    unchanged for every provider when no explicit contract is supplied."""

    def test_openai(self) -> None:
        _, _, payload = AiNetworkClient.build_request(
            make_profile("openai"), "key", "sys", "user"
        )
        self.assertEqual(payload["text"]["format"]["name"], "qgis_workflow")
        self.assertEqual(payload["text"]["format"]["schema"], GRAPH_SCHEMA)

    def test_anthropic(self) -> None:
        # NOTE: the pre-generalization code used two different literal
        # default identifiers across providers ("qgis_workflow" for OpenAI/
        # openai_compatible/azure/deepseek's schema name, "create_qgis_
        # workflow" for Anthropic's forced tool name) -- an inconsistency in
        # the original hardcoding, not a documented contract. Generalizing
        # to one shared, validated StructuredResponseContract default name
        # unifies these to "qgis_workflow" everywhere; the actual schema
        # payload that constrains every provider's output is unchanged.
        _, _, payload = AiNetworkClient.build_request(
            make_profile("anthropic"), "key", "sys", "user"
        )
        self.assertEqual(payload["tools"][0]["name"], "qgis_workflow")
        self.assertEqual(payload["tool_choice"], {"type": "tool", "name": "qgis_workflow"})
        self.assertEqual(payload["tools"][0]["input_schema"], GRAPH_SCHEMA)

    def test_gemini(self) -> None:
        _, _, payload = AiNetworkClient.build_request(
            make_profile("gemini"), "key", "sys", "user"
        )
        self.assertEqual(
            payload["generationConfig"]["responseJsonSchema"], GRAPH_SCHEMA
        )

    def test_deepseek(self) -> None:
        _, _, payload = AiNetworkClient.build_request(
            make_profile("deepseek"), "key", "sys", "user"
        )
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_ollama(self) -> None:
        _, _, payload = AiNetworkClient.build_request(
            make_profile("ollama"), "", "sys", "user"
        )
        self.assertEqual(payload["format"], GRAPH_SCHEMA)

    def test_openai_compatible(self) -> None:
        _, _, payload = AiNetworkClient.build_request(
            make_profile("openai_compatible"), "", "sys", "user"
        )
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "qgis_workflow")
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], GRAPH_SCHEMA)

    def test_azure_openai(self) -> None:
        _, _, payload = AiNetworkClient.build_request(
            make_profile("azure_openai"), "key", "sys", "user"
        )
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "qgis_workflow")


class AgentStructuredContractInsertedTests(unittest.TestCase):
    """The agent_turn contract must be inserted correctly for every provider."""

    def test_openai(self) -> None:
        _, _, payload = AiNetworkClient.build_request(
            make_profile("openai"), "key", "sys", "user", contract=AGENT_CONTRACT
        )
        self.assertEqual(payload["text"]["format"]["name"], "agent_turn")
        self.assertEqual(payload["text"]["format"]["schema"], AGENT_SCHEMA)

    def test_anthropic_uses_the_supplied_submission_tool_name(self) -> None:
        _, _, payload = AiNetworkClient.build_request(
            make_profile("anthropic"), "key", "sys", "user", contract=AGENT_CONTRACT
        )
        self.assertEqual(payload["tools"][0]["name"], "agent_turn")
        self.assertEqual(payload["tools"][0]["description"], AGENT_CONTRACT.description)
        self.assertEqual(payload["tool_choice"], {"type": "tool", "name": "agent_turn"})
        self.assertEqual(payload["tools"][0]["input_schema"], AGENT_SCHEMA)

    def test_gemini(self) -> None:
        _, _, payload = AiNetworkClient.build_request(
            make_profile("gemini"), "key", "sys", "user", contract=AGENT_CONTRACT
        )
        self.assertEqual(payload["generationConfig"]["responseJsonSchema"], AGENT_SCHEMA)

    def test_deepseek_compatible_fallback_still_bypasses_schema(self) -> None:
        # DeepSeek always uses json_object mode regardless of contract.
        _, _, payload = AiNetworkClient.build_request(
            make_profile("deepseek"), "key", "sys", "user", contract=AGENT_CONTRACT
        )
        self.assertEqual(payload["response_format"], {"type": "json_object"})

    def test_ollama(self) -> None:
        _, _, payload = AiNetworkClient.build_request(
            make_profile("ollama"), "", "sys", "user", contract=AGENT_CONTRACT
        )
        self.assertEqual(payload["format"], AGENT_SCHEMA)

    def test_openai_compatible(self) -> None:
        _, _, payload = AiNetworkClient.build_request(
            make_profile("openai_compatible"), "", "sys", "user", contract=AGENT_CONTRACT
        )
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "agent_turn")
        self.assertEqual(payload["response_format"]["json_schema"]["schema"], AGENT_SCHEMA)

    def test_azure_openai(self) -> None:
        endpoint, _, payload = AiNetworkClient.build_request(
            make_profile("azure_openai"), "key", "sys", "user", contract=AGENT_CONTRACT
        )
        self.assertEqual(payload["response_format"]["json_schema"]["name"], "agent_turn")
        self.assertIn("api-version=2024-05-01", endpoint)


class ContractsCannotBeConfusedTests(unittest.TestCase):
    def test_graph_and_agent_contract_names_never_mix(self) -> None:
        for provider_id in ("openai", "anthropic", "openai_compatible"):
            _, _, graph_payload = AiNetworkClient.build_request(
                make_profile(provider_id), "key", "sys", "user"
            )
            _, _, agent_payload = AiNetworkClient.build_request(
                make_profile(provider_id), "key", "sys", "user", contract=AGENT_CONTRACT
            )
            self.assertNotEqual(json.dumps(graph_payload), json.dumps(agent_payload))


class CompatibleFallbackScopeTests(unittest.TestCase):
    def test_fallback_still_builds_a_valid_request_for_gemini_and_openai_compatible(self) -> None:
        for provider_id in ("gemini", "openai_compatible"):
            endpoint, headers, payload = AiNetworkClient.build_request(
                make_profile(provider_id), "key", "sys", "user", compatible_fallback=True
            )
            self.assertTrue(endpoint)
            self.assertIsInstance(headers, dict)
            self.assertIsInstance(payload, dict)


class ExtractContentTests(unittest.TestCase):
    def test_anthropic_extraction_uses_the_supplied_submission_name(self) -> None:
        data = {
            "content": [
                {"type": "tool_use", "name": "agent_turn", "input": {"action": "final"}}
            ]
        }
        content = AiNetworkClient.extract_content("anthropic", data, "agent_turn")
        self.assertEqual(json.loads(content), {"action": "final"})

    def test_anthropic_extraction_ignores_a_tool_use_with_a_different_name(self) -> None:
        data = {
            "content": [
                {"type": "tool_use", "name": "create_qgis_workflow", "input": {}},
                {"type": "text", "text": "fallback text"},
            ]
        }
        content = AiNetworkClient.extract_content("anthropic", data, "agent_turn")
        self.assertEqual(content, "fallback text")

    def test_openai_incomplete_status_is_a_sanitized_error(self) -> None:
        with self.assertRaises(AiResponseError):
            AiNetworkClient.extract_content("openai", {"status": "incomplete", "output": []})

    def test_openai_refusal_is_a_sanitized_error(self) -> None:
        data = {"output": [{"content": [{"type": "refusal", "refusal": "I can't help with that"}]}]}
        with self.assertRaises(AiResponseError):
            AiNetworkClient.extract_content("openai", data)

    def test_openai_missing_content_is_a_sanitized_error(self) -> None:
        with self.assertRaises(AiResponseError):
            AiNetworkClient.extract_content("openai", {"output": []})

    def test_anthropic_missing_tool_result_is_a_sanitized_error(self) -> None:
        with self.assertRaises(AiResponseError):
            AiNetworkClient.extract_content("anthropic", {"content": []})


class TransientStateClearedTests(unittest.TestCase):
    def test_clear_sensitive_state_clears_the_contract_too(self) -> None:
        client = AiNetworkClient()
        client._profile = make_profile("openai")
        client._api_key = "secret"
        client._system_prompt = "sys"
        client._user_prompt = "user"
        client._contract = AGENT_CONTRACT
        client._clear_sensitive_state()
        self.assertIsNone(client._profile)
        self.assertEqual(client._api_key, "")
        self.assertEqual(client._system_prompt, "")
        self.assertEqual(client._user_prompt, "")
        self.assertIsNone(client._contract)

    def test_is_busy_reflects_no_outstanding_reply_and_no_network_call_is_made(self) -> None:
        client = AiNetworkClient()
        self.assertFalse(client.is_busy())


class StructuredResponseContractBoundingTests(unittest.TestCase):
    """Finding 4: contracts accept only detached, bounded, finite JSON schemas."""

    def test_rejects_non_json_schema(self) -> None:
        with self.assertRaises(ValueError):
            StructuredResponseContract(schema={"x": object()})

    def test_rejects_non_finite_numbers(self) -> None:
        for bad in (float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                StructuredResponseContract(schema={"x": bad})

    def test_rejects_excessive_depth(self) -> None:
        root: dict = {}
        cursor = root
        for _ in range(40):
            child: dict = {}
            cursor["a"] = child
            cursor = child
        cursor["a"] = "leaf"
        with self.assertRaises(ValueError):
            StructuredResponseContract(schema=root)

    def test_rejects_oversized_schema(self) -> None:
        with self.assertRaises(ValueError):
            StructuredResponseContract(schema={"k": "x" * 300_000})

    def test_detaches_stored_schema_from_caller_mutation(self) -> None:
        schema = {
            "type": "object",
            "additionalProperties": False,
            "properties": {"a": {"type": "string"}},
            "required": [],
        }
        contract = StructuredResponseContract(schema=schema)
        schema["properties"]["a"]["type"] = "number"
        schema["properties"]["injected"] = {"type": "string"}
        self.assertEqual(contract.schema["properties"]["a"]["type"], "string")
        self.assertNotIn("injected", contract.schema["properties"])

    def test_graph_and_agent_schemas_both_accepted_and_distinct(self) -> None:
        graph = StructuredResponseContract(schema=GRAPH_SCHEMA)
        agent = StructuredResponseContract(
            schema=AGENT_SCHEMA, name="agent_turn", description="Return the next agent_turn."
        )
        self.assertNotEqual(
            json.dumps(graph.schema, sort_keys=True), json.dumps(agent.schema, sort_keys=True)
        )


class MalformedEnvelopeTests(unittest.TestCase):
    """Finding 6: malformed provider container shapes fail closed and
    _extract_error never raises."""

    def test_openai_non_dict_output_element_is_a_sanitized_error(self) -> None:
        with self.assertRaises(AiResponseError):
            AiNetworkClient.extract_content("openai", {"output": ["bad"]})

    def test_anthropic_non_dict_content_element_is_a_sanitized_error(self) -> None:
        with self.assertRaises(AiResponseError):
            AiNetworkClient.extract_content("anthropic", {"content": ["bad"]})

    def test_anthropic_named_tool_after_a_text_block_is_still_preferred(self) -> None:
        data = {
            "content": [
                {"type": "text", "text": "chatter"},
                {"type": "tool_use", "name": "agent_turn", "input": {"action": "final"}},
            ]
        }
        content = AiNetworkClient.extract_content("anthropic", data, "agent_turn")
        self.assertEqual(json.loads(content), {"action": "final"})

    def test_non_object_provider_response_is_a_sanitized_error(self) -> None:
        with self.assertRaises(AiResponseError):
            AiNetworkClient.extract_content("openai", ["bad"])  # type: ignore[arg-type]

    def test_extract_error_never_raises_for_any_body_shape(self) -> None:
        self.assertEqual(AiNetworkClient._extract_error('["bad"]'), "")
        self.assertEqual(AiNetworkClient._extract_error("42"), "")
        self.assertEqual(AiNetworkClient._extract_error("not json at all"), "")
        self.assertEqual(
            AiNetworkClient._extract_error(json.dumps({"error": {"message": "boom"}})), "boom"
        )

    def test_extract_error_is_bounded(self) -> None:
        big = json.dumps({"error": {"message": "m" * 5000}})
        self.assertLessEqual(len(AiNetworkClient._extract_error(big)), 1000)


from qgis.PyQt.QtNetwork import QNetworkReply  # noqa: E402 - stubbed by _install_qgis_stubs


class _FakeReply:
    def __init__(self, status, body: bytes, error_code, error_str: str = "network error string") -> None:
        self._status = status
        self._body = body
        self._error = error_code
        self._error_str = error_str

    def attribute(self, _attr):
        return self._status

    def readAll(self):
        return self._body

    def error(self):
        return self._error

    def errorString(self):
        return self._error_str

    def deleteLater(self):
        pass

    def abort(self):
        pass


class _AbortOnlyFakeReply:
    """A fake QNetworkReply whose abort() does not emit a finished signal callback."""

    def __init__(self) -> None:
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True


def _run_on_finished(
    provider_id: str,
    status,
    body: bytes,
    error_code,
    cancelled: bool = False,
    error_str: str = "network error string",
    api_key: str = "secret-key",
):
    client = AiNetworkClient()
    client._profile = make_profile(provider_id)
    client._api_key = api_key
    client._system_prompt = "sys"
    client._user_prompt = "user"
    client._contract = AGENT_CONTRACT
    client._retried_format = False
    client._cancelled = cancelled
    client._timer = None
    client._reply = _FakeReply(status, body, error_code, error_str=error_str)
    posts: list = []
    client._post = lambda *args, **kwargs: posts.append(args)
    fails: list = []
    client.failed.connect(lambda message: fails.append(message))
    client._on_finished()
    return client, posts, fails


class CancellationTerminalBoundaryTests(unittest.TestCase):
    """Finding 5: cancel/timeout must not enter compatibility fallback."""

    _OTHER_ERROR = 2  # any non-NoError, non-OperationCanceled code

    def test_cancelled_gemini_400_starts_no_fallback_request(self) -> None:
        client, posts, fails = _run_on_finished(
            "gemini",
            400,
            b'{"error":{"message":"bad"}}',
            QNetworkReply.NetworkError.OperationCanceledError,
            cancelled=True,
        )
        self.assertEqual(posts, [])
        self.assertTrue(fails)
        self.assertEqual(client._api_key, "")

    def test_timed_out_openai_compatible_422_starts_no_fallback_request(self) -> None:
        client, posts, fails = _run_on_finished(
            "openai_compatible",
            422,
            b'{"error":{"message":"bad"}}',
            QNetworkReply.NetworkError.OperationCanceledError,
            cancelled=False,
        )
        self.assertEqual(posts, [])
        self.assertTrue(fails)
        self.assertEqual(client._api_key, "")

    def test_genuine_gemini_400_still_starts_one_fallback_request(self) -> None:
        # Regression guard: normal (non-cancelled) fallback must still work.
        _client, posts, _fails = _run_on_finished(
            "gemini", 400, b'{"error":{}}', self._OTHER_ERROR, cancelled=False
        )
        self.assertEqual(len(posts), 1)


class ResponseBodyBoundTests(unittest.TestCase):
    """Finding 7: the raw HTTP body is bounded before JSON parsing."""

    def test_oversized_body_yields_one_controlled_failure(self) -> None:
        from planx_smartmodeler.core.ai_client import MAX_RESPONSE_BODY_CHARS

        body = b"x" * (MAX_RESPONSE_BODY_CHARS + 1)
        client, posts, fails = _run_on_finished(
            "openai", 200, body, QNetworkReply.NetworkError.NoError
        )
        self.assertEqual(posts, [])
        self.assertEqual(len(fails), 1)
        self.assertIn("oversized", fails[0].lower())
        self.assertEqual(client._api_key, "")

    def test_body_exactly_at_bound_is_not_rejected_as_oversized(self) -> None:
        from planx_smartmodeler.core.ai_client import MAX_RESPONSE_BODY_CHARS

        body = b" " * MAX_RESPONSE_BODY_CHARS  # not oversized, but invalid JSON
        _client, _posts, fails = _run_on_finished(
            "openai", 200, body, QNetworkReply.NetworkError.NoError
        )
        self.assertEqual(len(fails), 1)
        self.assertNotIn("oversized", fails[0].lower())


class Revision2AdversarialTests(unittest.TestCase):
    """Adversarial tests for Revision 2 blocking corrections."""

    def test_immediate_secret_clearing_on_cancel_with_abort_only_reply(self) -> None:
        client = AiNetworkClient()
        client._profile = make_profile("openai")
        client._api_key = "secret-key-123"
        client._system_prompt = "secret-sys-prompt"
        client._user_prompt = "secret-user-prompt"
        client._contract = AGENT_CONTRACT
        reply = _AbortOnlyFakeReply()
        client._reply = reply  # type: ignore[assignment]

        client.cancel()

        self.assertTrue(reply.aborted)
        self.assertEqual(client._api_key, "")
        self.assertEqual(client._system_prompt, "")
        self.assertEqual(client._user_prompt, "")
        self.assertIsNone(client._profile)
        self.assertIsNone(client._contract)
        self.assertTrue(client._cancelled)
        self.assertTrue(client.is_busy())

    def test_immediate_secret_clearing_on_timeout_with_abort_only_reply(self) -> None:
        client = AiNetworkClient()
        client._profile = make_profile("openai")
        client._api_key = "secret-key-123"
        client._system_prompt = "secret-sys-prompt"
        client._user_prompt = "secret-user-prompt"
        client._contract = AGENT_CONTRACT
        reply = _AbortOnlyFakeReply()
        client._reply = reply  # type: ignore[assignment]

        client._on_timeout()

        self.assertTrue(reply.aborted)
        self.assertEqual(client._api_key, "")
        self.assertEqual(client._system_prompt, "")
        self.assertEqual(client._user_prompt, "")
        self.assertIsNone(client._profile)
        self.assertIsNone(client._contract)
        self.assertTrue(client._cancelled)

    def test_safe_late_on_finished_after_state_cleared(self) -> None:
        client = AiNetworkClient()
        client._profile = make_profile("openai")
        client._api_key = "secret-key-123"
        client._contract = AGENT_CONTRACT
        client._reply = _FakeReply(200, b'{"output_text": "ok"}', QNetworkReply.NetworkError.NoError)
        fails: list = []
        client.failed.connect(lambda msg: fails.append(msg))

        client.cancel()
        client._on_finished()

        self.assertIsNone(client._reply)
        self.assertFalse(client.is_busy())
        self.assertEqual(len(fails), 1)
        self.assertIn("canceled", fails[0])

    def test_mutation_attempts_through_contract_schema(self) -> None:
        contract = StructuredResponseContract(schema=AGENT_SCHEMA)
        s = contract.schema
        s["bad"] = object()
        s["injected"] = True
        self.assertNotIn("bad", contract.schema)
        self.assertNotIn("injected", contract.schema)

        _, _, payload = AiNetworkClient.build_request(
            make_profile("openai"), "key", "sys", "user", contract=contract
        )
        self.assertNotIn("bad", payload["text"]["format"]["schema"])
        self.assertNotIn("injected", payload["text"]["format"]["schema"])

    def test_mutation_attempts_through_built_payload(self) -> None:
        contract = StructuredResponseContract(schema=AGENT_SCHEMA)
        _, _, payload1 = AiNetworkClient.build_request(
            make_profile("openai"), "key", "sys", "user", contract=contract
        )
        payload1["text"]["format"]["schema"]["properties"]["injected"] = {"type": "boolean"}

        _, _, payload2 = AiNetworkClient.build_request(
            make_profile("openai"), "key", "sys", "user", contract=contract
        )
        self.assertNotIn("injected", payload2["text"]["format"]["schema"]["properties"])
        self.assertNotIn("injected", contract.schema["properties"])

    def test_later_payload_independence(self) -> None:
        contract = StructuredResponseContract(schema=AGENT_SCHEMA)
        _, _, payload_anthropic = AiNetworkClient.build_request(
            make_profile("anthropic"), "key", "sys", "user", contract=contract
        )
        payload_anthropic["tools"][0]["input_schema"]["properties"]["tampered"] = {"type": "string"}

        _, _, payload_openai = AiNetworkClient.build_request(
            make_profile("openai"), "key", "sys", "user", contract=contract
        )
        self.assertNotIn("tampered", payload_openai["text"]["format"]["schema"]["properties"])

    def test_controlled_rejection_of_post_construction_invalid_state(self) -> None:
        contract = StructuredResponseContract(schema=AGENT_SCHEMA)
        object.__setattr__(contract, "_schema", {"bad": object()})
        with self.assertRaises(ValueError) as cm:
            AiNetworkClient.build_request(
                make_profile("openai"), "key", "sys", "user", contract=contract
            )
        self.assertIn("Invalid structured response", str(cm.exception))

    def test_plain_text_body_containing_raw_secret_token_is_absent_from_emitted_error(self) -> None:
        _client, _posts, fails = _run_on_finished(
            "openai",
            500,
            b"RAW_SECRET_TOKEN_IN_BODY",
            QNetworkReply.NetworkError.NoError,
            api_key="my-secret-key",
        )
        self.assertEqual(len(fails), 1)
        self.assertNotIn("RAW_SECRET_TOKEN_IN_BODY", fails[0])
        self.assertIn("AI provider request failed (500)", fails[0])

    def test_oversized_error_string_is_bounded(self) -> None:
        huge_error_str = "E" * 250_000
        _client, _posts, fails = _run_on_finished(
            "openai",
            500,
            b"",
            QNetworkReply.NetworkError.NoError,
            error_str=huge_error_str,
        )
        self.assertEqual(len(fails), 1)
        self.assertLessEqual(len(fails[0]), 1200)

    def test_error_json_message_echoing_exact_active_api_key_is_redacted(self) -> None:
        raw_json = b'{"error":{"message":"Invalid key: secret-api-key-999"}}'
        _client, _posts, fails = _run_on_finished(
            "openai",
            400,
            raw_json,
            QNetworkReply.NetworkError.NoError,
            api_key="secret-api-key-999",
        )
        self.assertEqual(len(fails), 1)
        self.assertNotIn("secret-api-key-999", fails[0])
        self.assertIn("[REDACTED]", fails[0])

    def test_openai_output_containing_both_output_text_and_refusal(self) -> None:
        data = {
            "output_text": '{"action": "final", "assistant_text": "ok", "tool_calls": []}',
            "output": [
                {
                    "content": [
                        {"type": "refusal", "refusal": "I refuse to inspect that layer"}
                    ]
                }
            ],
        }
        with self.assertRaises(AiResponseError) as cm:
            AiNetworkClient.extract_content("openai", data)
        self.assertIn("declined to answer", str(cm.exception))

    def test_qgis_smoke_settings_restoration_leaves_store_intact(self) -> None:
        from planx_smartmodeler.core.ai_settings import AiProfile, AiSettingsStore

        store = AiSettingsStore()
        initial_profiles = [p.profile_id for p in store.profiles()]
        initial_active = store.active_profile().profile_id

        offline_profile = AiProfile.create("offline", "Smoke offline profile probe")
        try:
            store.save_profile(offline_profile)
            store.set_active(offline_profile.profile_id)
        finally:
            store.delete_profile(offline_profile.profile_id)
            if initial_active:
                store.set_active(initial_active)

        post_profiles = [p.profile_id for p in store.profiles()]
        post_active = store.active_profile().profile_id

        self.assertEqual(initial_profiles, post_profiles)
        self.assertEqual(initial_active, post_active)


class Revision3AdversarialTests(unittest.TestCase):
    """Adversarial regression tests for Revision 3 hardening."""

    def test_schema_string_at_20000_and_20001_chars(self) -> None:
        valid_schema = {
            "type": "object",
            "properties": {"msg": {"type": "string", "description": "a" * 20_000}},
        }
        contract = StructuredResponseContract(schema=valid_schema)
        self.assertEqual(len(contract.schema["properties"]["msg"]["description"]), 20_000)
        _, _, payload = AiNetworkClient.build_request(
            make_profile("openai"), "key", "sys", "user", contract=contract
        )
        self.assertEqual(
            len(payload["text"]["format"]["schema"]["properties"]["msg"]["description"]), 20_000
        )

        invalid_schema = {
            "type": "object",
            "properties": {"msg": {"type": "string", "description": "a" * 20_001}},
        }
        with self.assertRaises(ValueError):
            StructuredResponseContract(schema=invalid_schema)

    def test_schema_aggregate_within_and_over_200000_policy(self) -> None:
        valid_props = {f"p{i}": {"type": "string", "description": "x" * 20_000} for i in range(9)}
        valid_schema = {"type": "object", "properties": valid_props}
        contract = StructuredResponseContract(schema=valid_schema)
        self.assertIsNotNone(contract.schema)
        _, _, payload = AiNetworkClient.build_request(
            make_profile("openai"), "key", "sys", "user", contract=contract
        )
        self.assertIn("p8", payload["text"]["format"]["schema"]["properties"])

        invalid_props = {f"p{i}": {"type": "string", "description": "x" * 20_000} for i in range(11)}
        invalid_schema = {"type": "object", "properties": invalid_props}
        with self.assertRaises(ValueError):
            StructuredResponseContract(schema=invalid_schema)

    def test_schema_access_and_build_request_same_declared_bounds(self) -> None:
        schema = {
            "type": "object",
            "properties": {"field": {"type": "string", "description": "d" * 5000}},
        }
        contract = StructuredResponseContract(schema=schema)
        view = contract.schema
        self.assertEqual(len(view["properties"]["field"]["description"]), 5000)
        _, _, payload = AiNetworkClient.build_request(
            make_profile("anthropic"), "key", "sys", "user", contract=contract
        )
        self.assertEqual(
            len(payload["tools"][0]["input_schema"]["properties"]["field"]["description"]), 5000
        )

    def test_tampered_schema_name_and_description_all_fail_before_post(self) -> None:
        profile = make_profile("openai")
        contract1 = StructuredResponseContract(schema=AGENT_SCHEMA)
        object.__setattr__(contract1, "_schema", {"bad": object()})
        with self.assertRaises(ValueError) as cm1:
            AiNetworkClient.build_request(profile, "key", "sys", "user", contract=contract1)
        self.assertIn("Invalid structured response contract", str(cm1.exception))

        contract2 = StructuredResponseContract(schema=AGENT_SCHEMA)
        object.__setattr__(contract2, "_name", "invalid name!")
        with self.assertRaises(ValueError) as cm2:
            AiNetworkClient.build_request(profile, "key", "sys", "user", contract=contract2)
        self.assertIn("Invalid structured response contract", str(cm2.exception))

        contract3 = StructuredResponseContract(schema=AGENT_SCHEMA)
        object.__setattr__(contract3, "_description", "")
        with self.assertRaises(ValueError) as cm3:
            AiNetworkClient.build_request(profile, "key", "sys", "user", contract=contract3)
        self.assertIn("Invalid structured response contract", str(cm3.exception))

        contract4 = StructuredResponseContract(schema=AGENT_SCHEMA)
        object.__setattr__(contract4, "_description", "d" * 501)
        with self.assertRaises(ValueError) as cm4:
            AiNetworkClient.build_request(profile, "key", "sys", "user", contract=contract4)
        self.assertIn("Invalid structured response contract", str(cm4.exception))

    def test_mutation_isolation_across_original_public_and_payloads(self) -> None:
        input_dict = {"type": "object", "properties": {"a": {"type": "string"}}}
        contract = StructuredResponseContract(schema=input_dict)
        input_dict["properties"]["a"]["type"] = "mutated_original"

        view1 = contract.schema
        self.assertEqual(view1["properties"]["a"]["type"], "string")
        view1["properties"]["a"]["type"] = "mutated_view1"

        view2 = contract.schema
        self.assertEqual(view2["properties"]["a"]["type"], "string")

        _, _, payload1 = AiNetworkClient.build_request(
            make_profile("openai"), "key", "sys", "user", contract=contract
        )
        payload1["text"]["format"]["schema"]["properties"]["a"]["type"] = "mutated_payload1"

        _, _, payload2 = AiNetworkClient.build_request(
            make_profile("openai"), "key", "sys", "user", contract=contract
        )
        self.assertEqual(payload2["text"]["format"]["schema"]["properties"]["a"]["type"], "string")

    def test_recognized_provider_json_error_echoing_system_and_user_prompts(self) -> None:
        sys_p = "my_secret_system_prompt_99"
        usr_p = "my_secret_user_prompt_88"
        raw_json = json.dumps(
            {"error": {"message": f"Rejected prompts - System: {sys_p}, User: {usr_p}"}}
        ).encode("utf-8")

        client = AiNetworkClient()
        client._profile = make_profile("openai")
        client._api_key = "key-1"
        client._system_prompt = sys_p
        client._user_prompt = usr_p
        client._contract = AGENT_CONTRACT
        client._reply = _FakeReply(400, raw_json, QNetworkReply.NetworkError.NoError)
        fails_actual = []
        client.failed.connect(lambda msg: fails_actual.append(msg))
        client._on_finished()

        self.assertEqual(len(fails_actual), 1)
        err = fails_actual[0]
        self.assertNotIn(sys_p, err)
        self.assertNotIn(usr_p, err)
        self.assertIn("[REDACTED]", err)

    def test_network_error_string_containing_exact_private_endpoint(self) -> None:
        private_url = "https://private.internal.agency.gov/v1/chat"
        client = AiNetworkClient()
        profile = make_profile("openai")
        profile.endpoint = private_url
        client._profile = profile
        client._api_key = "key-1"
        client._system_prompt = "sys-secret"
        client._user_prompt = "user-secret"
        client._contract = AGENT_CONTRACT
        reply = _FakeReply(
            None,
            b"",
            QNetworkReply.NetworkError.ConnectionRefusedError,
            error_str=f"Connection failed to {private_url}: host offline",
        )
        client._reply = reply
        fails = []
        client.failed.connect(lambda msg: fails.append(msg))
        client._on_finished()

        self.assertEqual(len(fails), 1)
        err = fails[0]
        self.assertNotIn(private_url, err)
        self.assertNotIn("private.internal.agency.gov", err)
        self.assertNotIn("sys-secret", err)
        self.assertNotIn("user-secret", err)
        self.assertLessEqual(len(err), 1000)

    def test_port_qualified_endpoint_network_error_hostname_redaction(self) -> None:
        private_url = "https://private.internal.example:8443/v1"
        client = AiNetworkClient()
        profile = make_profile("openai")
        profile.endpoint = private_url
        client._profile = profile
        client._api_key = "key-1"
        client._system_prompt = "sys-secret"
        client._user_prompt = "user-secret"
        client._contract = AGENT_CONTRACT
        reply = _FakeReply(
            None,
            b"",
            QNetworkReply.NetworkError.HostNotFoundError,
            error_str="Host private.internal.example not found",
        )
        client._reply = reply
        fails = []
        client.failed.connect(lambda msg: fails.append(msg))
        client._on_finished()

        self.assertEqual(len(fails), 1)
        err = fails[0]
        self.assertNotIn("private.internal.example", err)
        self.assertNotIn(private_url, err)
        self.assertNotIn("sys-secret", err)
        self.assertNotIn("user-secret", err)
        self.assertLessEqual(len(err), 1000)

    def test_port_qualified_endpoint_json_error_hostname_redaction(self) -> None:
        private_url = "https://private.internal.example:8443/v1"
        raw_json = json.dumps(
            {"error": {"message": "Backend private.internal.example rejected request"}}
        ).encode("utf-8")
        client = AiNetworkClient()
        profile = make_profile("openai")
        profile.endpoint = private_url
        client._profile = profile
        client._api_key = "key-1"
        client._system_prompt = "sys-secret"
        client._user_prompt = "user-secret"
        client._contract = AGENT_CONTRACT
        reply = _FakeReply(
            400,
            raw_json,
            QNetworkReply.NetworkError.NoError,
        )
        client._reply = reply
        fails = []
        client.failed.connect(lambda msg: fails.append(msg))
        client._on_finished()

        self.assertEqual(len(fails), 1)
        err = fails[0]
        self.assertNotIn("private.internal.example", err)
        self.assertNotIn(private_url, err)
        self.assertIn("[REDACTED]", err)
        self.assertLessEqual(len(err), 1000)

    def test_case_variation_and_bracketed_ipv6_hostname_redaction(self) -> None:
        mixed_url = "https://Private.Internal.Example:8443/v1"
        client1 = AiNetworkClient()
        profile1 = make_profile("openai")
        profile1.endpoint = mixed_url
        client1._profile = profile1
        client1._reply = _FakeReply(
            None,
            b"",
            QNetworkReply.NetworkError.HostNotFoundError,
            error_str="Host PRIVATE.INTERNAL.EXAMPLE not found",
        )
        fails1 = []
        client1.failed.connect(lambda msg: fails1.append(msg))
        client1._on_finished()
        self.assertEqual(len(fails1), 1)
        self.assertNotIn("private.internal.example", fails1[0].lower())
        self.assertNotIn(mixed_url.lower(), fails1[0].lower())

        ipv6_url = "http://[2001:db8::1]:8443/v1"
        client2 = AiNetworkClient()
        profile2 = make_profile("openai")
        profile2.endpoint = ipv6_url
        client2._profile = profile2
        client2._reply = _FakeReply(
            None,
            b"",
            QNetworkReply.NetworkError.ConnectionRefusedError,
            error_str="Host [2001:db8::1] not found",
        )
        fails2 = []
        client2.failed.connect(lambda msg: fails2.append(msg))
        client2._on_finished()
        self.assertEqual(len(fails2), 1)
        self.assertNotIn("2001:db8::1", fails2[0])
        self.assertNotIn(ipv6_url, fails2[0])

    def test_case_varied_full_endpoint_redaction_in_recognized_json_error(self) -> None:
        endpoint = "https://Private.Internal.Example:8443/secret/path?tenant=alpha"
        echoed_msg = "Failed at HTTPS://PRIVATE.INTERNAL.EXAMPLE:8443/secret/path?tenant=alpha"
        raw_json = json.dumps({"error": {"message": echoed_msg}}).encode("utf-8")
        client = AiNetworkClient()
        profile = make_profile("openai")
        profile.endpoint = endpoint
        client._profile = profile
        client._api_key = "key-1"
        client._system_prompt = "sys-secret"
        client._user_prompt = "user-secret"
        client._contract = AGENT_CONTRACT
        reply = _FakeReply(
            400,
            raw_json,
            QNetworkReply.NetworkError.NoError,
        )
        client._reply = reply
        fails = []
        client.failed.connect(lambda msg: fails.append(msg))
        client._on_finished()

        self.assertEqual(len(fails), 1)
        err = fails[0]
        self.assertIn("[REDACTED]", err)
        self.assertNotIn("private.internal.example", err.lower())
        self.assertNotIn("8443", err)
        self.assertNotIn("/secret/path", err)
        self.assertNotIn("tenant=alpha", err)
        self.assertNotIn(endpoint.lower(), err.lower())
        self.assertNotIn(echoed_msg.lower(), err.lower())
        self.assertLessEqual(len(err), 1000)

    def test_empty_and_stale_active_settings_restored_with_exact_value_and_key_existence(self) -> None:
        from planx_smartmodeler.core.ai_settings import (
            AiSettingsStore,
            FakeQgsSettings,
            scoped_ai_settings_isolation,
        )

        empty_fake = FakeQgsSettings()
        with scoped_ai_settings_isolation(empty_fake):
            store = AiSettingsStore()
            store.profiles()
            self.assertTrue(empty_fake.contains(AiSettingsStore.PROFILE_KEY))

        stale_fake = FakeQgsSettings()
        stale_fake.setValue(AiSettingsStore.ACTIVE_KEY, "stale_raw_active_id")
        with scoped_ai_settings_isolation(stale_fake):
            store = AiSettingsStore()
            active = store.active_profile()
            self.assertNotEqual(active.profile_id, "stale_raw_active_id")

        self.assertEqual(stale_fake.value(AiSettingsStore.ACTIVE_KEY), "stale_raw_active_id")

    def test_real_smoke_settings_helper_leaving_no_ai_keys_in_fresh_config(self) -> None:
        from planx_smartmodeler.core.ai_settings import (
            AiSettingsStore,
            FakeQgsSettings,
            scoped_ai_settings_isolation,
        )

        real_fake = FakeQgsSettings()
        with scoped_ai_settings_isolation(real_fake):
            s = AiSettingsStore()
            s.profiles()

        self.assertFalse(real_fake.contains(AiSettingsStore.PROFILE_KEY))
        self.assertFalse(real_fake.contains(AiSettingsStore.ACTIVE_KEY))


if __name__ == "__main__":
    unittest.main()
