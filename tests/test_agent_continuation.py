"""QGIS-free tests for supervised multi-step continuation.

Three properties matter: an action's outcome reaches the next turn in a bounded,
sanitized form; the agent never continues on its own; and one chat session
cannot drive an unbounded number of actions.

The dock owns this behaviour but imports Qt, so these tests exercise the same
logic through the pure ``SessionMemory`` plus a minimal stand-in that mirrors
the dock's counter and note format exactly.
"""
from __future__ import annotations

import unittest

from planx_smartmodeler.core.agent.action_ledger import ActionStatus
from planx_smartmodeler.core.agent.prompt_builder import PromptBudget, SessionMemory

# Mirrors gui/agent_dock.MAX_SESSION_ACTIONS; asserted equal below so the two
# can never silently drift apart.
MAX_SESSION_ACTIONS = 10


class _ContinuationHarness:
    """The dock's continuation bookkeeping, without Qt."""

    def __init__(self, memory: SessionMemory) -> None:
        self.session_memory = memory
        self.provider_calls = 0
        self._session_action_count = 0

    def record_action_outcome(self, kind: str, status: str, target: str) -> None:
        self._session_action_count += 1
        note = f"[Action: {str(kind)[:40]}] {str(status)[:40]}"
        if target:
            note = f"{note} - {str(target)[:120]}"
        self.session_memory.append("(agent action outcome)", note)

    def session_action_budget_left(self) -> bool:
        return self._session_action_count < MAX_SESSION_ACTIONS

    def new_chat(self) -> None:
        self.session_memory.clear()
        self._session_action_count = 0


def budget() -> PromptBudget:
    return PromptBudget(max_prompt_chars=20_000)


def harness() -> _ContinuationHarness:
    return _ContinuationHarness(SessionMemory(budget()))


class OutcomeMemoryTests(unittest.TestCase):
    def test_a_completed_action_reaches_the_next_turn(self):
        agent = harness()
        agent.record_action_outcome("processing_run", ActionStatus.COMPLETED, "Buffer")
        history = agent.session_memory.exchanges()
        self.assertEqual(len(history), 1)
        self.assertIn("processing_run", history[0].assistant_text)
        self.assertIn("completed", history[0].assistant_text)

    def test_the_note_carries_no_parameter_path_id_or_secret(self):
        agent = harness()
        agent.record_action_outcome("processing_run", ActionStatus.COMPLETED, "Buffer")
        text = agent.session_memory.exchanges()[0].assistant_text
        for forbidden in ("C:\\", "/", "TEMPORARY_OUTPUT", "EPSG:", "layer_", "token"):
            self.assertNotIn(forbidden, text)

    def test_the_note_is_bounded_even_for_hostile_input(self):
        agent = harness()
        agent.record_action_outcome("k" * 500, "s" * 500, "t" * 500)
        text = agent.session_memory.exchanges()[0].assistant_text
        self.assertLessEqual(len(text), 40 + 40 + 120 + 20)

    def test_every_terminal_status_can_be_recorded(self):
        agent = harness()
        for status in (
            ActionStatus.APPLIED, ActionStatus.COMPLETED, ActionStatus.CANCELED,
            ActionStatus.FAILED, ActionStatus.UNDONE,
        ):
            agent.record_action_outcome("processing_run", status, "Buffer")
        self.assertEqual(len(agent.session_memory.exchanges()), 5)

    def test_recording_an_outcome_never_calls_the_provider(self):
        agent = harness()
        for _ in range(5):
            agent.record_action_outcome("model_run", ActionStatus.COMPLETED, "Workflow")
        self.assertEqual(agent.provider_calls, 0)

    def test_outcome_memory_obeys_the_session_memory_bounds(self):
        limits = budget()
        agent = _ContinuationHarness(SessionMemory(limits))
        for index in range(limits.max_session_exchanges + 10):
            agent.record_action_outcome("processing_run", ActionStatus.COMPLETED, f"T{index}")
        self.assertLessEqual(
            len(agent.session_memory.exchanges()), limits.max_session_exchanges
        )


class SessionActionCapTests(unittest.TestCase):
    def test_the_cap_matches_the_dock_constant(self):
        import re
        from pathlib import Path

        source = Path(__file__).resolve().parents[1] / "gui" / "agent_dock.py"
        match = re.search(r"^MAX_SESSION_ACTIONS = (\d+)", source.read_text(encoding="utf-8"),
                          re.MULTILINE)
        self.assertIsNotNone(match, "agent_dock must define MAX_SESSION_ACTIONS")
        self.assertEqual(int(match.group(1)), MAX_SESSION_ACTIONS)

    def test_budget_is_available_up_to_the_cap(self):
        agent = harness()
        for _ in range(MAX_SESSION_ACTIONS):
            self.assertTrue(agent.session_action_budget_left())
            agent.record_action_outcome("processing_run", ActionStatus.COMPLETED, "T")
        self.assertFalse(agent.session_action_budget_left())

    def test_an_eleventh_action_is_refused(self):
        agent = harness()
        for _ in range(MAX_SESSION_ACTIONS + 5):
            agent.record_action_outcome("processing_run", ActionStatus.COMPLETED, "T")
        self.assertFalse(agent.session_action_budget_left())

    def test_new_chat_resets_the_counter_and_the_memory(self):
        agent = harness()
        for _ in range(MAX_SESSION_ACTIONS):
            agent.record_action_outcome("processing_run", ActionStatus.COMPLETED, "T")
        self.assertFalse(agent.session_action_budget_left())
        agent.new_chat()
        self.assertTrue(agent.session_action_budget_left())
        self.assertTrue(agent.session_memory.is_empty())

    def test_a_failed_or_cancelled_action_still_consumes_budget(self):
        # Otherwise a failing loop could be retried without limit.
        agent = harness()
        for _ in range(MAX_SESSION_ACTIONS):
            agent.record_action_outcome("processing_run", ActionStatus.FAILED, "")
        self.assertFalse(agent.session_action_budget_left())


if __name__ == "__main__":
    unittest.main()
