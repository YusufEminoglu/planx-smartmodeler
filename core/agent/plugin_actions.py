"""Pure registry for explicitly reviewed cross-plugin Agent actions.

QGIS plugins do not share a universal command API. SmartModeler therefore
never guesses at buttons or calls arbitrary methods: each supported bridge is
identified here by exact package/action ids and has a bounded public contract.
New plugins can be added without changing the provider protocol, but every
adapter still requires an application review and an explicit approval card.
"""
from __future__ import annotations

from typing import Any, Dict, List

from . import context as agent_context

PLUGIN_ACTION_KIND = "plugin_action"

_ACTIONS = {
    "zero2viz": {
        "suggest_chart": {
            "title": "Create a smart 02viz chart",
            "description": (
                "Open 02viz for one project vector layer, let its offline "
                "assistant choose a suitable chart, and render it in the dock."
            ),
            "requires_vector_layer": True,
        }
    }
}


def reviewed_actions(package_name: str) -> Dict[str, Dict[str, Any]]:
    return dict(_ACTIONS.get(str(package_name), {}))


def public_actions(package_name: str) -> List[Dict[str, Any]]:
    rows = []
    for action_id, definition in reviewed_actions(package_name).items():
        rows.append(
            {
                "action_id": action_id,
                "title": agent_context.bound_text(definition["title"], 160),
                "description": agent_context.bound_text(
                    definition["description"], 500
                ),
                "requires_vector_layer": bool(
                    definition.get("requires_vector_layer")
                ),
                "proposal_kind": PLUGIN_ACTION_KIND,
            }
        )
    return rows


def capability_state(
    package_name: str, version: str, enabled: bool, loaded: bool
) -> Dict[str, Any]:
    """Canonical freshness state signed by inspection and checked at Apply."""
    return {
        "package_name": agent_context.bound_text(package_name, 128),
        "version": agent_context.bound_text(version, 64),
        "enabled": bool(enabled),
        "loaded": bool(loaded),
        "actions": sorted(reviewed_actions(package_name)),
    }
