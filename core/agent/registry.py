"""Deterministic, fail-closed registry mapping tool specs to trusted handlers."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from .contracts import AgentToolCall, AgentToolSpec

ToolHandler = Callable[[AgentToolCall], Any]


class ToolRegistrationError(ValueError):
    """Raised when a tool cannot be registered safely."""


class AgentToolRegistry:
    """Holds tool specs and their trusted Python handlers.

    Handlers are registered directly by trusted plugin code at import/setup
    time. The registry never imports or resolves a handler by a name supplied
    by model/provider output, so an unknown or malicious tool name can only
    ever miss a lookup, never trigger arbitrary code.
    """

    def __init__(self) -> None:
        self._specs: Dict[str, AgentToolSpec] = {}
        self._handlers: Dict[str, ToolHandler] = {}

    def register(self, spec: AgentToolSpec, handler: ToolHandler) -> None:
        if not isinstance(spec, AgentToolSpec):
            raise ToolRegistrationError("spec must be an AgentToolSpec instance.")
        if spec.name in self._specs:
            raise ToolRegistrationError(f"Duplicate tool name: {spec.name}")
        if not callable(handler):
            raise ToolRegistrationError(f"Handler for {spec.name} must be callable.")
        self._specs[spec.name] = spec
        self._handlers[spec.name] = handler

    def get_spec(self, name: str) -> Optional[AgentToolSpec]:
        return self._specs.get(name)

    def get_handler(self, name: str) -> Optional[ToolHandler]:
        return self._handlers.get(name)

    def has_tool(self, name: str) -> bool:
        return name in self._specs

    def list_specs(self) -> List[AgentToolSpec]:
        """Return registered tool specs in a stable, deterministic order."""
        return [self._specs[name] for name in sorted(self._specs)]

    def public_tool_descriptions(self) -> List[Dict[str, Any]]:
        """Return copy-safe, JSON-compatible descriptions for UI/discovery."""
        return [spec.public_description() for spec in self.list_specs()]
