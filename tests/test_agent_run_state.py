"""QGIS-free tests for the single-running-action state machine and sanitizer.

These prove the guarantees the execution coordinator relies on: one run at a
time, a cancel that is terminal and idempotent, a late result that cannot add
anything after cancel or teardown, and a failure message that never carries a
path, URI, connection string, or credential.
"""
from __future__ import annotations

import unittest

from planx_smartmodeler.core.agent.run_state import (
    CANCELED,
    FAILED,
    FINISHED,
    IDLE,
    MAX_RUN_MESSAGE_CHARS,
    RUNNING,
    RunState,
    RunTicket,
    sanitize_run_message,
)


class RunStateTests(unittest.TestCase):
    def setUp(self):
        self.state = RunState()

    def test_a_fresh_state_is_idle(self):
        self.assertEqual(self.state.status, IDLE)
        self.assertFalse(self.state.is_running())
        self.assertIsNone(self.state.ticket)

    def test_start_claims_the_single_run_slot(self):
        ticket = self.state.start("a1", "processing_run", "Buffer roads")
        self.assertIsNotNone(ticket)
        self.assertEqual(self.state.status, RUNNING)
        self.assertEqual(ticket.action_id, "a1")

    def test_a_second_start_while_running_is_refused(self):
        self.state.start("a1", "processing_run", "One")
        self.assertIsNone(self.state.start("a2", "processing_run", "Two"))

    def test_a_new_run_is_allowed_after_the_previous_one_finished(self):
        first = self.state.start("a1", "processing_run", "One")
        self.state.finish(first, FINISHED)
        second = self.state.start("a2", "processing_run", "Two")
        self.assertIsNotNone(second)
        self.assertGreater(second.run_id, first.run_id)

    def test_cancel_is_terminal_and_idempotent(self):
        self.state.start("a1", "processing_run", "One")
        self.assertTrue(self.state.cancel())
        self.assertFalse(self.state.cancel())
        self.assertTrue(self.state.canceled)

    def test_cancel_with_no_run_does_nothing(self):
        self.assertFalse(self.state.cancel())

    def test_a_result_is_accepted_only_for_the_current_uncancelled_run(self):
        ticket = self.state.start("a1", "processing_run", "One")
        self.assertTrue(self.state.accepts(ticket))
        self.state.cancel()
        self.assertFalse(self.state.accepts(ticket))

    def test_a_late_result_from_a_previous_run_is_refused(self):
        first = self.state.start("a1", "processing_run", "One")
        self.state.finish(first, FINISHED)
        self.state.start("a2", "processing_run", "Two")
        self.assertFalse(self.state.accepts(first))

    def test_a_result_after_teardown_is_refused(self):
        ticket = self.state.start("a1", "processing_run", "One")
        self.state.reset()
        self.assertFalse(self.state.accepts(ticket))
        self.assertEqual(self.state.status, IDLE)

    def test_teardown_marks_an_in_flight_run_cancelled(self):
        self.state.start("a1", "processing_run", "One")
        self.state.reset()
        self.assertTrue(self.state.canceled)

    def test_finish_with_a_stale_ticket_changes_nothing(self):
        ticket = self.state.start("a1", "processing_run", "One")
        stale = RunTicket(run_id=ticket.run_id - 1, action_id="a0", kind="k", title="t")
        self.assertFalse(self.state.finish(stale, FINISHED))
        self.assertEqual(self.state.status, RUNNING)

    def test_finish_happens_at_most_once(self):
        ticket = self.state.start("a1", "processing_run", "One")
        self.assertTrue(self.state.finish(ticket, FINISHED))
        self.assertFalse(self.state.finish(ticket, FAILED))
        self.assertEqual(self.state.status, FINISHED)

    def test_an_unknown_terminal_status_becomes_failed(self):
        ticket = self.state.start("a1", "processing_run", "One")
        self.state.finish(ticket, "something_else")
        self.assertEqual(self.state.status, FAILED)

    def test_a_cancelled_run_can_still_be_closed_as_cancelled(self):
        ticket = self.state.start("a1", "processing_run", "One")
        self.state.cancel()
        self.assertTrue(self.state.finish(ticket, CANCELED))
        self.assertEqual(self.state.status, CANCELED)

    def test_ticket_labels_are_bounded(self):
        ticket = self.state.start("a" * 500, "processing_run", "t" * 500)
        self.assertLessEqual(len(ticket.action_id), 64)
        self.assertLessEqual(len(ticket.title), 160)


class SanitizeRunMessageTests(unittest.TestCase):
    def assert_generic(self, message):
        cleaned = sanitize_run_message(message)
        self.assertNotIn("secret", cleaned.lower())
        self.assertEqual(cleaned, sanitize_run_message("C:\\x\\y"))

    def test_a_windows_path_is_discarded(self):
        self.assert_generic(r"Could not write C:\Users\secret\out.gpkg")

    def test_a_unc_path_is_discarded(self):
        self.assert_generic(r"\\fileserver\secret\share failed")

    def test_a_uri_is_discarded(self):
        self.assert_generic("postgres://user:secret@host/db is unreachable")

    def test_a_connection_string_is_discarded(self):
        self.assert_generic("dbname='secret' host=10.0.0.1 could not connect")

    def test_a_credential_keyword_is_discarded(self):
        self.assert_generic("authcfg=secret rejected")

    def test_a_data_file_name_is_discarded(self):
        self.assert_generic("input secret.shp has no features")

    def test_a_plain_message_survives_bounded(self):
        self.assertEqual(
            sanitize_run_message("The algorithm reported invalid geometry."),
            "The algorithm reported invalid geometry.",
        )

    def test_a_long_plain_message_is_bounded(self):
        cleaned = sanitize_run_message("word " * 500)
        self.assertLessEqual(len(cleaned), MAX_RUN_MESSAGE_CHARS)

    def test_an_empty_or_non_string_message_becomes_generic(self):
        self.assertEqual(sanitize_run_message(""), sanitize_run_message("   "))
        self.assertTrue(sanitize_run_message(None))

    def test_an_exception_object_is_accepted(self):
        self.assertTrue(sanitize_run_message(RuntimeError("plain failure")))


if __name__ == "__main__":
    unittest.main()
