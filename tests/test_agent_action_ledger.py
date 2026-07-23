"""Pure tests for the bounded in-session action ledger."""
from __future__ import annotations

import unittest

from planx_smartmodeler.core.agent.action_ledger import (
    ActionLedger,
    ActionStatus,
    LedgerEntry,
)


class ActionLedgerTests(unittest.TestCase):
    def test_records_bounded_safe_fields_only(self) -> None:
        ledger = ActionLedger()
        entry = ledger.record(
            "a1", "model_patch", "My Model", "Rename", ActionStatus.APPLIED,
            is_destructive=True, reason_code="",
        )
        self.assertIsInstance(entry, LedgerEntry)
        d = entry.to_dict()
        self.assertEqual(d["status"], "applied")
        self.assertTrue(d["destructive"])
        self.assertEqual(set(d), {
            "seq", "action_id", "kind", "target", "title", "status",
            "destructive", "reason_code",
        })

    def test_unknown_status_falls_back_to_failed(self) -> None:
        ledger = ActionLedger()
        entry = ledger.record("a1", "k", "t", "ti", "not-a-status")
        self.assertEqual(entry.status, ActionStatus.FAILED)

    def test_fields_are_bounded(self) -> None:
        ledger = ActionLedger()
        entry = ledger.record("a" * 500, "k" * 500, "t" * 500, "ti" * 500,
                              ActionStatus.APPLIED, reason_code="r" * 500)
        self.assertLessEqual(len(entry.action_id), 64)
        self.assertLessEqual(len(entry.target), 200)
        self.assertLessEqual(len(entry.title), 160)
        self.assertLessEqual(len(entry.reason_code), 80)

    def test_ledger_is_capped_and_ordered(self) -> None:
        ledger = ActionLedger(max_entries=3)
        for i in range(5):
            ledger.record(f"a{i}", "k", "t", "ti", ActionStatus.PROPOSED)
        entries = ledger.entries()
        self.assertEqual(len(entries), 3)
        # Oldest dropped; sequence is monotonic and preserved.
        self.assertEqual([e.action_id for e in entries], ["a2", "a3", "a4"])
        self.assertEqual([e.seq for e in entries], [3, 4, 5])

    def test_latest_and_clear(self) -> None:
        ledger = ActionLedger()
        ledger.record("a1", "k", "t", "ti", ActionStatus.PROPOSED)
        ledger.record("a2", "k", "t", "ti", ActionStatus.APPLIED)
        self.assertEqual(ledger.latest().action_id, "a2")
        ledger.clear()
        self.assertEqual(ledger.entries(), [])
        self.assertIsNone(ledger.latest())

    def test_execution_statuses_are_accepted_verbatim(self) -> None:
        ledger = ActionLedger()
        for status in (ActionStatus.RUNNING, ActionStatus.COMPLETED, ActionStatus.CANCELED):
            entry = ledger.record("a1", "processing_run", "Buffer", "Buffer roads", status)
            self.assertEqual(entry.status, status)
            self.assertIn(status, ActionStatus.ALL)

    def test_entries_is_detached(self) -> None:
        ledger = ActionLedger()
        ledger.record("a1", "k", "t", "ti", ActionStatus.PROPOSED)
        got = ledger.entries()
        got.clear()
        self.assertEqual(len(ledger.entries()), 1)


if __name__ == "__main__":
    unittest.main()
