"""Pure tests for the Phase 04 pending-action / approval-nonce / digest contract.

QGIS-free: a fake proposal object with a ``to_dict()`` exercises the digest and
the pending-action card without a Processing registry or a live layer/graph.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any, Dict

from planx_smartmodeler.core.agent.pending_action import (
    ApprovalState,
    DEFAULT_TTL_SECONDS,
    build_pending_action,
    proposal_digest,
)


@dataclass(frozen=True)
class FakeProposal:
    title: str
    value: Any = 0

    def to_dict(self) -> Dict[str, Any]:
        return {"title": self.title, "value": self.value}


def _preview(**over):
    base = {
        "kind": "model_patch",
        "title": "T",
        "target": "Model",
        "summary": "S",
        "warnings": [],
        "destructive": False,
        "operations": [{"summary": "Add node", "destructive": False}],
        "operation_count": 1,
    }
    base.update(over)
    return base


class ProposalDigestTests(unittest.TestCase):
    def test_digest_is_deterministic(self) -> None:
        p = FakeProposal("A", 1)
        self.assertEqual(proposal_digest(p), proposal_digest(FakeProposal("A", 1)))

    def test_digest_changes_when_content_changes(self) -> None:
        self.assertNotEqual(proposal_digest(FakeProposal("A", 1)), proposal_digest(FakeProposal("A", 2)))
        self.assertNotEqual(proposal_digest(FakeProposal("A", 1)), proposal_digest(FakeProposal("B", 1)))

    def test_digest_is_hex_sha256_and_carries_no_secret(self) -> None:
        digest = proposal_digest(FakeProposal("A", 1))
        self.assertEqual(len(digest), 64)
        int(digest, 16)  # hex


class ApprovalNonceTests(unittest.TestCase):
    def test_nonce_consumes_once_for_exact_pair(self) -> None:
        state = ApprovalState("act-1")
        self.assertFalse(state.used)
        self.assertTrue(state.consume("act-1", state.nonce))
        self.assertTrue(state.used)
        # A replay of the exact same pair fails after first use.
        self.assertFalse(state.consume("act-1", state.nonce))

    def test_foreign_or_absent_nonce_fails_closed(self) -> None:
        state = ApprovalState("act-1")
        self.assertFalse(state.consume("act-1", "deadbeef"))  # wrong nonce
        self.assertFalse(state.consume("other", state.nonce))  # wrong action id
        self.assertFalse(state.consume("act-1", None))  # provider-style missing
        self.assertFalse(state.consume(None, state.nonce))
        # None of the failed attempts consumed the single use.
        self.assertTrue(state.consume("act-1", state.nonce))

    def test_distinct_states_have_distinct_nonces(self) -> None:
        self.assertNotEqual(ApprovalState("a").nonce, ApprovalState("b").nonce)

    def test_action_id_required(self) -> None:
        with self.assertRaises(ValueError):
            ApprovalState("")


class PendingActionTests(unittest.TestCase):
    def _build(self, now=1000.0, **over):
        kwargs = dict(
            kind="model_patch",
            proposal=FakeProposal("A", 1),
            preview=_preview(**over),
            target_identity="current_model",
            context_token="tok",
            mode="act",
            scope="current_model",
            now=now,
        )
        return build_pending_action(**kwargs)

    def test_build_sets_digest_expiry_and_nonce(self) -> None:
        pending = self._build(now=1000.0)
        self.assertEqual(pending.digest, proposal_digest(FakeProposal("A", 1)))
        self.assertEqual(pending.expires_at, 1000.0 + DEFAULT_TTL_SECONDS)
        self.assertFalse(pending.is_expired(1000.0))
        self.assertTrue(pending.is_expired(1000.0 + DEFAULT_TTL_SECONDS))
        self.assertIsInstance(pending.approval, ApprovalState)
        self.assertEqual(pending.approval.action_id, pending.action_id)

    def test_public_card_is_bounded_and_hides_internals(self) -> None:
        pending = self._build(destructive=True)
        card = pending.to_public_card()
        # No parsed proposal object, token, or digest leaks into the card.
        self.assertNotIn("proposal", card)
        self.assertNotIn("context_token", card)
        self.assertNotIn("digest", card)
        self.assertNotIn("approval", card)
        self.assertTrue(card["destructive"])
        self.assertFalse(card["applied"])
        self.assertEqual(card["operations"], [{"summary": "Add node", "destructive": False}])

    def test_public_card_is_frozen_source_detached(self) -> None:
        pending = self._build()
        card = pending.to_public_card()
        card["title"] = "mutated"
        # The pending action is frozen; its preview is unaffected by card edits.
        self.assertNotEqual(pending.preview.get("title"), "mutated")


if __name__ == "__main__":
    unittest.main()
