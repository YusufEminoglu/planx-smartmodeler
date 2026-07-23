"""Asynchronous, dependency-free AI HTTP client using QGIS networking."""
from __future__ import annotations

import json
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, urlencode, urlparse, urlunparse, parse_qsl

from qgis.PyQt.QtCore import QByteArray, QObject, QTimer, QUrl, pyqtSignal
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest
from qgis.core import QgsNetworkAccessManager

from .agent.contracts import ContractError, validate_json_value
from .ai_mcp_bridge import AiMcpBridge, AiResponseError
from .ai_settings import AiProfile, validate_endpoint

_CONTRACT_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
MAX_CONTRACT_DESCRIPTION_LENGTH = 500

# A trusted structured-output schema is bounded, finite, standard-JSON data.
# These bounds are generous for the graph and agent_turn schemas but reject a
# malformed, non-serializable, or absurdly large caller-supplied schema before
# it can reach the QGIS network layer.
MAX_CONTRACT_SCHEMA_STRING_CHARS = 20_000
MAX_CONTRACT_SCHEMA_TOTAL_CHARS = 200_000

# Bound applied to a raw provider HTTP response body before it is parsed as
# JSON at all (defense in depth against an oversized success or error body).
MAX_RESPONSE_BODY_CHARS = 1_000_000

_GRAPH_CONTRACT_NAME = "qgis_workflow"
_GRAPH_CONTRACT_DESCRIPTION = "Return the validated SmartModeler GIS graph."


class StructuredResponseContract:
    """A trusted, bounded structured-output contract for one provider request.

    Carries only the JSON Schema, a conservative submission/schema name, and
    a bounded description -- never a profile, API key, endpoint, QGIS object,
    or callback. ``generate()`` (Workflow Studio graph planning) and
    ``generate_structured()`` (Agent Chat) each build their own contract and
    share the same validated request-construction path below, so neither
    can be confused with the other's response shape.
    """

    @classmethod
    def validate_components(
        cls,
        schema: Any,
        name: Any,
        description: Any,
    ) -> Tuple[Dict[str, Any], str, str]:
        """Validate schema, name, and description using declared contract bounds."""
        if not isinstance(schema, dict) or not schema:
            raise ValueError("Structured response schema must be a non-empty object.")
        try:
            detached = validate_json_value(
                schema,
                max_string_length=MAX_CONTRACT_SCHEMA_STRING_CHARS,
                max_total_chars=MAX_CONTRACT_SCHEMA_TOTAL_CHARS,
            )
        except ContractError as error:
            raise ValueError(f"Invalid structured response schema: {error}") from error
        if not isinstance(detached, dict) or not detached:
            raise ValueError("Structured response schema must be a non-empty object.")
        try:
            serialized = json.dumps(detached, ensure_ascii=False, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid structured response schema: {error}") from error
        if len(serialized) > MAX_CONTRACT_SCHEMA_TOTAL_CHARS:
            raise ValueError("Structured response schema exceeds the size limit.")
        if not isinstance(name, str) or not _CONTRACT_NAME_PATTERN.fullmatch(name):
            raise ValueError(f"Invalid structured response contract name: {name!r}")
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > MAX_CONTRACT_DESCRIPTION_LENGTH
        ):
            raise ValueError("Invalid structured response contract description.")
        return detached, name, description

    def __init__(
        self,
        schema: Dict[str, Any],
        name: str = _GRAPH_CONTRACT_NAME,
        description: str = _GRAPH_CONTRACT_DESCRIPTION,
    ) -> None:
        detached, clean_name, clean_desc = self.validate_components(schema, name, description)
        self._schema = detached
        self._name = clean_name
        self._description = clean_desc

    @property
    def schema(self) -> Dict[str, Any]:
        """Return a fresh, detached JSON schema tree on every access."""
        return validate_json_value(
            self._schema,
            max_string_length=MAX_CONTRACT_SCHEMA_STRING_CHARS,
            max_total_chars=MAX_CONTRACT_SCHEMA_TOTAL_CHARS,
        )

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return self._description


class AiNetworkClient(QObject):
    """Sends one non-blocking structured-output request at a time.

    Used both by Workflow Studio's graph-planning ``generate()`` and by
    Agent Chat's ``generate_structured()``; both funnel through the same
    validated, provider-specific request construction in ``build_request()``.
    """

    succeeded = pyqtSignal(str)
    failed = pyqtSignal(str)
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._reply: QNetworkReply | None = None
        self._timer: QTimer | None = None
        self._profile: AiProfile | None = None
        self._retried_format = False
        self._cancelled = False
        self._api_key = ""
        self._system_prompt = ""
        self._user_prompt = ""
        self._contract: Optional[StructuredResponseContract] = None

    def is_busy(self) -> bool:
        return self._reply is not None

    def cancel(self) -> None:
        # Mark explicit cancellation and clear transient secrets immediately
        # so credentials do not survive cancellation while an async abort completes.
        self._cancelled = True
        self._clear_sensitive_state()
        if self._reply is not None:
            self._reply.abort()

    def generate(
        self,
        profile: AiProfile,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
    ) -> None:
        """Workflow Studio's graph-planning entry point (unchanged contract)."""
        self._start_request(
            profile,
            api_key,
            system_prompt,
            user_prompt,
            StructuredResponseContract(schema=AiMcpBridge.response_schema()),
        )

    def generate_structured(
        self,
        profile: AiProfile,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
        contract: StructuredResponseContract,
    ) -> None:
        """Agent Chat's entry point: an explicit, caller-supplied structured
        response contract (e.g. the ``agent_turn`` envelope schema) rather
        than the graph-planning schema ``generate()`` always uses."""
        self._start_request(profile, api_key, system_prompt, user_prompt, contract)

    def _start_request(
        self,
        profile: AiProfile,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
        contract: StructuredResponseContract,
    ) -> None:
        if self.is_busy():
            self.failed.emit("An AI request is already running.")
            return
        endpoint_error = validate_endpoint(profile.endpoint)
        if endpoint_error:
            self.failed.emit(endpoint_error)
            return
        try:
            endpoint, headers, payload = self.build_request(
                profile, api_key, system_prompt, user_prompt, contract=contract
            )
        except (TypeError, ValueError) as error:
            msg = self._sanitize_error_text(
                str(error), api_key, system_prompt, user_prompt, profile.endpoint
            )[:1000]
            self.failed.emit(msg)
            return
        self._profile = profile
        self._retried_format = False
        self._cancelled = False
        self._api_key = api_key
        self._system_prompt = system_prompt
        self._user_prompt = user_prompt
        self._contract = contract
        self._post(endpoint, headers, payload)

    @classmethod
    def build_request(
        cls,
        profile: AiProfile,
        api_key: str,
        system_prompt: str,
        user_prompt: str,
        compatible_fallback: bool = False,
        contract: Optional[StructuredResponseContract] = None,
    ) -> Tuple[str, Dict[str, str], Dict[str, Any]]:
        contract = contract or StructuredResponseContract(schema=AiMcpBridge.response_schema())
        try:
            if not isinstance(contract, StructuredResponseContract):
                raise ValueError("Invalid structured response contract instance.")
            schema_view = contract.schema
            name_view = contract.name
            desc_view = contract.description
            schema, submission_name, submission_description = (
                StructuredResponseContract.validate_components(
                    schema_view, name_view, desc_view
                )
            )
        except Exception as error:
            raise ValueError(f"Invalid structured response contract: {error}") from error

        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        endpoint = profile.endpoint.strip()

        if profile.provider_id == "openai":
            headers["Authorization"] = f"Bearer {api_key}"
            if profile.organization:
                headers["OpenAI-Organization"] = profile.organization
            payload = {
                "model": profile.model,
                "instructions": system_prompt,
                "input": user_prompt,
                "temperature": profile.temperature,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": submission_name,
                        "strict": True,
                        "schema": schema,
                    }
                },
            }
        elif profile.provider_id == "anthropic":
            headers.update(
                {
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                }
            )
            payload = {
                "model": profile.model,
                "max_tokens": 6000,
                "temperature": profile.temperature,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
                "tools": [
                    {
                        "name": submission_name,
                        "description": submission_description,
                        "input_schema": schema,
                    }
                ],
                "tool_choice": {"type": "tool", "name": submission_name},
            }
        elif profile.provider_id == "gemini":
            headers["x-goog-api-key"] = api_key
            endpoint = endpoint.rstrip("/")
            if ":generateContent" not in endpoint:
                endpoint += f"/models/{quote(profile.model, safe='-._')}:generateContent"
            generation_config: Dict[str, Any] = {
                "temperature": profile.temperature,
                "responseMimeType": "application/json",
            }
            if not compatible_fallback:
                generation_config["responseJsonSchema"] = schema
            payload = {
                "systemInstruction": {"parts": [{"text": system_prompt}]},
                "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                "generationConfig": generation_config,
            }
        elif profile.provider_id == "ollama":
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            payload = {
                "model": profile.model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "format": schema,
                "options": {"temperature": profile.temperature},
            }
        elif profile.provider_id in (
            "deepseek",
            "openai_compatible",
            "azure_openai",
        ):
            if profile.provider_id == "azure_openai":
                headers["api-key"] = api_key
                endpoint = cls._with_api_version(endpoint, profile.api_version)
            elif api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            response_format: Dict[str, Any]
            if profile.provider_id == "deepseek" or compatible_fallback:
                response_format = {"type": "json_object"}
            else:
                response_format = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": submission_name,
                        "strict": True,
                        "schema": schema,
                    },
                }
            payload = {
                "model": profile.model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": profile.temperature,
                "response_format": response_format,
            }
        else:
            raise ValueError(f"Unsupported AI provider: {profile.provider_id}")
        return endpoint, headers, payload

    @staticmethod
    def _with_api_version(endpoint: str, api_version: str) -> str:
        if not api_version:
            return endpoint
        parsed = urlparse(endpoint)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.setdefault("api-version", api_version)
        return urlunparse(parsed._replace(query=urlencode(query)))

    def _post(
        self, endpoint: str, headers: Dict[str, str], payload: Dict[str, Any]
    ) -> None:
        request = QNetworkRequest(QUrl(endpoint))
        for key, value in headers.items():
            request.setRawHeader(
                QByteArray(key.encode("ascii")), QByteArray(value.encode("utf-8"))
            )
        body = QByteArray(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        manager = QgsNetworkAccessManager.instance()
        self._reply = manager.post(request, body)
        self._reply.finished.connect(self._on_finished)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        timeout_ms = int((self._profile.timeout_seconds if self._profile else 90) * 1000)
        self._timer.timeout.connect(self._on_timeout)
        self._timer.start(timeout_ms)
        self.busy_changed.emit(True)

    def _on_timeout(self) -> None:
        self._cancelled = True
        self._clear_sensitive_state()
        if self._reply is not None:
            self._reply.abort()

    def _on_finished(self) -> None:
        reply = self._reply
        if reply is None:
            return
        if self._timer is not None:
            self._timer.stop()
            self._timer.deleteLater()
            self._timer = None
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        raw = bytes(reply.readAll()).decode("utf-8", errors="replace")
        network_error = reply.error()
        error_text = reply.errorString()
        reply.deleteLater()
        self._reply = None

        cancelled_or_timed_out = (
            self._cancelled
            or network_error == QNetworkReply.NetworkError.OperationCanceledError
        )
        if cancelled_or_timed_out:
            self.busy_changed.emit(False)
            detail = "AI request timed out or was canceled."
            self._clear_sensitive_state()
            self.failed.emit(f"AI provider request failed ({status or 'network'}): {detail}")
            return

        # Bound the raw HTTP body before parsing it as JSON at all. An
        # oversized success or error body becomes one controlled, bounded
        # failure rather than an unbounded parse.
        if len(raw) > MAX_RESPONSE_BODY_CHARS:
            self.busy_changed.emit(False)
            self._clear_sensitive_state()
            self.failed.emit("AI provider returned an oversized response.")
            return

        if (
            not cancelled_or_timed_out
            and self._profile is not None
            and self._profile.provider_id in ("gemini", "openai_compatible")
            and not self._retried_format
            and status in (400, 404, 422)
        ):
            self._retried_format = True
            try:
                endpoint, headers, payload = self.build_request(
                    self._profile,
                    self._api_key,
                    self._system_prompt,
                    self._user_prompt,
                    compatible_fallback=True,
                    contract=self._contract,
                )
                self._post(endpoint, headers, payload)
                return
            except (TypeError, ValueError):
                pass

        self.busy_changed.emit(False)
        if network_error != QNetworkReply.NetworkError.NoError or not status or int(status) >= 400:
            endpoint_str = self._profile.endpoint if self._profile else ""
            detail = self._extract_error(
                raw, self._api_key, self._system_prompt, self._user_prompt, endpoint_str
            ) or self._sanitize_network_error(
                error_text, self._api_key, self._system_prompt, self._user_prompt, endpoint_str
            )
            if not detail:
                detail = "HTTP error response received."
            self._clear_sensitive_state()
            msg = f"AI provider request failed ({status or 'network'}): {detail}"
            msg = self._sanitize_error_text(
                msg, self._api_key, self._system_prompt, self._user_prompt, endpoint_str
            )[:1000]
            self.failed.emit(msg)
            return
        try:
            data = json.loads(raw)
            provider_id = self._profile.provider_id if self._profile else ""
            submission_name = self._contract.name if self._contract else _GRAPH_CONTRACT_NAME
            content = self.extract_content(provider_id, data, submission_name)
        except (
            json.JSONDecodeError,
            KeyError,
            IndexError,
            TypeError,
            AttributeError,
            ValueError,
            AiResponseError,
        ) as error:
            endpoint_str = self._profile.endpoint if self._profile else ""
            err_detail = self._sanitize_error_text(
                str(error), self._api_key, self._system_prompt, self._user_prompt, endpoint_str
            )[:500]
            self._clear_sensitive_state()
            msg = f"AI provider returned an unreadable response: {err_detail}"
            self.failed.emit(msg[:1000])
            return
        self._clear_sensitive_state()
        self.succeeded.emit(content)

    def _clear_sensitive_state(self) -> None:
        self._api_key = ""
        self._system_prompt = ""
        self._user_prompt = ""
        self._profile = None
        self._contract = None

    @classmethod
    def extract_content(
        cls,
        provider_id: str,
        data: Dict[str, Any],
        submission_name: str = _GRAPH_CONTRACT_NAME,
    ) -> str:
        # Provider envelopes are untrusted: validate container/element types
        # explicitly and raise a sanitized AiResponseError for a malformed
        # shape rather than letting an AttributeError/TypeError escape.
        if not isinstance(data, dict):
            raise AiResponseError("Provider response was not a JSON object.")
        if provider_id == "openai":
            if data.get("status") == "incomplete":
                raise AiResponseError("OpenAI response was incomplete.")
            output = data.get("output", [])
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    contents = item.get("content", [])
                    if not isinstance(contents, list):
                        continue
                    for content in contents:
                        if not isinstance(content, dict):
                            continue
                        if content.get("type") == "refusal" or "refusal" in content:
                            raise AiResponseError("OpenAI declined to answer this request.")
            if isinstance(data.get("output_text"), str) and data["output_text"]:
                return data["output_text"]
            if isinstance(output, list):
                for item in output:
                    if not isinstance(item, dict):
                        continue
                    contents = item.get("content", [])
                    if not isinstance(contents, list):
                        continue
                    for content in contents:
                        if not isinstance(content, dict):
                            continue
                        if content.get("type") in ("output_text", "text") and content.get("text"):
                            return str(content["text"])
            raise AiResponseError("OpenAI response contained no output text.")
        if provider_id == "anthropic":
            content = data.get("content", [])
            if isinstance(content, list):
                # Prefer the matching named tool result over any text block or
                # a wrong-named tool: search the whole list for it first.
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "tool_use" and item.get("name") == submission_name:
                        return json.dumps(item.get("input"), ensure_ascii=False)
                for item in content:
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "text" and item.get("text"):
                        return str(item["text"])
            raise AiResponseError(
                f"Anthropic response contained no {submission_name!r} tool result."
            )
        if provider_id == "gemini":
            try:
                candidates = data["candidates"]
                parts = candidates[0]["content"]["parts"]
                text = parts[0]["text"]
            except (KeyError, IndexError, TypeError) as error:
                raise AiResponseError("Gemini response shape was invalid.") from error
            if not isinstance(text, str):
                raise AiResponseError("Gemini response text was invalid.")
            return text
        if provider_id == "ollama":
            try:
                text = data["message"]["content"]
            except (KeyError, TypeError) as error:
                raise AiResponseError("Ollama response shape was invalid.") from error
            if not isinstance(text, str):
                raise AiResponseError("Ollama response text was invalid.")
            return text
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise AiResponseError("Provider response shape was invalid.") from error
        if not isinstance(text, str):
            raise AiResponseError("Provider response text was invalid.")
        return text

    @classmethod
    def _extract_endpoint_host_targets(cls, endpoint: str) -> Tuple[str, ...]:
        if not isinstance(endpoint, str) or not endpoint.strip():
            return ()
        try:
            parsed = urlparse(endpoint if "://" in endpoint else "//" + endpoint)
            hostname = parsed.hostname or ""
            netloc = parsed.netloc or ""
        except Exception:  # noqa: BLE001
            return ()

        targets = []
        if netloc and len(netloc) > 3:
            targets.append(netloc)
        if hostname and ":" in hostname and f"[{hostname}]" not in targets:
            targets.append(f"[{hostname}]")
        if hostname and len(hostname) > 3 and hostname not in targets:
            targets.append(hostname)
        return tuple(targets)

    @classmethod
    def _sanitize_error_text(
        cls,
        text: str,
        api_key: str = "",
        system_prompt: str = "",
        user_prompt: str = "",
        endpoint: str = "",
    ) -> str:
        if not isinstance(text, str):
            return ""
        res = text
        secrets = (api_key, system_prompt, user_prompt)
        for secret in secrets:
            if secret and secret in res:
                res = res.replace(secret, "[REDACTED]")
        if isinstance(endpoint, str) and endpoint.strip():
            res = re.sub(re.escape(endpoint), "[REDACTED]", res, flags=re.IGNORECASE)
        for target in cls._extract_endpoint_host_targets(endpoint):
            if target and len(target) > 3:
                res = re.sub(re.escape(target), "[REDACTED]", res, flags=re.IGNORECASE)
        return res.strip()[:1000]

    @classmethod
    def _sanitize_network_error(
        cls,
        text: str,
        api_key: str = "",
        system_prompt: str = "",
        user_prompt: str = "",
        endpoint: str = "",
    ) -> str:
        if not isinstance(text, str) or not text.strip():
            return ""
        text_lower = text.lower()
        if (
            "http://" in text_lower
            or "https://" in text_lower
            or (endpoint and endpoint.lower() in text_lower)
        ):
            return "Network connection error."
        for target in cls._extract_endpoint_host_targets(endpoint):
            if target and len(target) > 3 and target.lower() in text_lower:
                return "Network connection error."
        return cls._sanitize_error_text(text, api_key, system_prompt, user_prompt, endpoint)

    @classmethod
    def _extract_error(
        cls,
        raw: str,
        api_key: str = "",
        system_prompt: str = "",
        user_prompt: str = "",
        endpoint: str = "",
    ) -> str:
        """Return a bounded, sanitized error snippet from recognized JSON object fields.
        Never raises. Returns empty string for unrecognized / raw / scalar / array / malformed bodies.
        """
        if not isinstance(raw, str) or not raw.strip():
            return ""
        try:
            data = json.loads(raw)
        except Exception:  # noqa: BLE001
            return ""

        if not isinstance(data, dict):
            return ""

        message = ""
        error = data.get("error")
        if isinstance(error, dict):
            msg = error.get("message") or error.get("type") or error.get("code")
            if isinstance(msg, str):
                message = msg
        elif isinstance(error, str):
            message = error
        elif isinstance(data.get("message"), str):
            message = data["message"]
        elif isinstance(data.get("error_message"), str):
            message = data["error_message"]

        if not message:
            return ""

        return cls._sanitize_error_text(
            message, api_key, system_prompt, user_prompt, endpoint
        )
