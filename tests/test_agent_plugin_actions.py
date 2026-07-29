"""Pure contract tests for reviewed cross-plugin Agent actions."""
from __future__ import annotations

import json
import unittest

from planx_smartmodeler.core.agent.plugin_actions import (
    PLUGIN_ACTION_KIND,
    capability_state,
    public_actions,
    reviewed_actions,
)
from planx_smartmodeler.core.agent.proposals import (
    PluginActionProposal,
    ProposalError,
    parse_proposal,
)


class PluginActionContractTests(unittest.TestCase):
    def test_zero2viz_exposes_one_bounded_reviewed_action(self) -> None:
        actions = public_actions("zero2viz")
        self.assertEqual([row["action_id"] for row in actions], ["suggest_chart"])
        self.assertEqual(actions[0]["proposal_kind"], PLUGIN_ACTION_KIND)
        self.assertTrue(actions[0]["requires_vector_layer"])
        self.assertEqual(reviewed_actions("unknown"), {})

    def test_capability_state_changes_when_plugin_readiness_changes(self) -> None:
        ready = capability_state("zero2viz", "1.0", True, True)
        disabled = capability_state("zero2viz", "1.0", False, False)
        self.assertNotEqual(ready, disabled)
        self.assertEqual(ready["actions"], ["suggest_chart"])

    def test_plugin_action_proposal_is_strict_and_drops_token_from_public_tree(self) -> None:
        data = {
            "schema_version": 1,
            "context_token": "fresh-token",
            "package_name": "zero2viz",
            "action_id": "suggest_chart",
            "target_layer_id": "layer_123",
            "title": "Create a smart chart",
            "summary": "Render 02viz's offline suggestion.",
            "warnings": [],
        }
        proposal = parse_proposal(PLUGIN_ACTION_KIND, json.dumps(data))
        self.assertIsInstance(proposal, PluginActionProposal)
        self.assertEqual(proposal.target_layer_id, "layer_123")
        self.assertNotIn("context_token", proposal.to_dict())
        data["extra"] = True
        with self.assertRaises(ProposalError):
            parse_proposal(PLUGIN_ACTION_KIND, json.dumps(data))

    def test_plugin_action_rejects_path_shaped_layer_id(self) -> None:
        data = {
            "schema_version": 1,
            "context_token": "fresh-token",
            "package_name": "zero2viz",
            "action_id": "suggest_chart",
            "target_layer_id": "C:/private/data.gpkg",
            "title": "Create a smart chart",
            "summary": "Render a chart.",
            "warnings": [],
        }
        with self.assertRaises(ProposalError):
            parse_proposal(PLUGIN_ACTION_KIND, json.dumps(data))
