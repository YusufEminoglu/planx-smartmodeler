"""Agent Workspace: typed contracts, registry, policy, bounded context,
read-only tools, a deterministic controller, and a provider-neutral bounded
multi-turn Agent Chat loop (protocol, prompt builder, run loop).

This package still contains no arbitrary Python execution, no MCP, no CLI
agent, and no mutating/reversible/destructive tool -- every tool is
read-only and every call passes through the trusted registry and the
fail-closed policy engine. Agent Chat's own network operation happens
entirely in ``core/ai_client.py`` through the existing QGIS network manager;
this package never opens a socket itself.
"""
