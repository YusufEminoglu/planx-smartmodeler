import unittest

from planx_smartmodeler.core.document_state import DocumentHistory


class DocumentHistoryTests(unittest.TestCase):
    def test_distinct_revisions_support_undo_and_redo(self) -> None:
        history = DocumentHistory("one")
        self.assertTrue(history.record("two"))
        self.assertTrue(history.record("three"))
        self.assertEqual(history.undo(), "two")
        self.assertEqual(history.undo(), "one")
        self.assertIsNone(history.undo())
        self.assertEqual(history.redo(), "two")
        self.assertEqual(history.redo(), "three")
        self.assertIsNone(history.redo())

    def test_duplicate_state_does_not_spend_history(self) -> None:
        history = DocumentHistory("one")
        self.assertFalse(history.record("one"))
        self.assertFalse(history.can_undo)

    def test_new_edit_discards_the_redo_branch(self) -> None:
        history = DocumentHistory("one")
        history.record("two")
        history.record("three")
        self.assertEqual(history.undo(), "two")
        history.record("replacement")
        self.assertFalse(history.can_redo)
        self.assertEqual(history.undo(), "two")

    def test_dirty_state_tracks_the_saved_revision(self) -> None:
        history = DocumentHistory("one")
        self.assertFalse(history.is_dirty)
        history.record("two")
        self.assertTrue(history.is_dirty)
        history.mark_clean()
        self.assertFalse(history.is_dirty)
        history.record("three")
        self.assertTrue(history.is_dirty)
        self.assertEqual(history.undo(), "two")
        self.assertFalse(history.is_dirty)

    def test_failed_transaction_can_remove_its_candidate(self) -> None:
        history = DocumentHistory("one")
        history.record("candidate")
        self.assertTrue(history.rollback_current("one"))
        self.assertEqual(history.current_snapshot, "one")
        self.assertFalse(history.can_undo)
        self.assertFalse(history.can_redo)

    def test_history_is_bounded(self) -> None:
        history = DocumentHistory("zero", max_entries=3)
        for index in range(1, 6):
            history.record(str(index))
        self.assertEqual(history.current_snapshot, "5")
        self.assertEqual(history.undo(), "4")
        self.assertEqual(history.undo(), "3")
        self.assertIsNone(history.undo())

    def test_recovered_reset_is_dirty_and_has_no_undo(self) -> None:
        history = DocumentHistory("empty")
        history.reset("recovered", mark_clean=False)
        self.assertTrue(history.is_dirty)
        self.assertFalse(history.can_undo)
        self.assertFalse(history.can_redo)

    def test_invalid_configuration_fails_closed(self) -> None:
        with self.assertRaises(ValueError):
            DocumentHistory("")
        with self.assertRaises(ValueError):
            DocumentHistory("one", max_entries=1)


if __name__ == "__main__":
    unittest.main()
