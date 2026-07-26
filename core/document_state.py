"""Bounded document revision history for the Workflow Studio."""
from __future__ import annotations

from typing import List, Optional


class DocumentHistory:
    """Track serialized graph revisions and the last successfully saved state."""

    def __init__(self, initial_snapshot: str, max_entries: int = 64) -> None:
        if not isinstance(initial_snapshot, str) or not initial_snapshot:
            raise ValueError("An initial document snapshot is required.")
        if max_entries < 2:
            raise ValueError("Document history must retain at least two entries.")
        self.max_entries = max_entries
        self._entries: List[str] = [initial_snapshot]
        self._index = 0
        self._clean_snapshot: Optional[str] = initial_snapshot

    @property
    def current_snapshot(self) -> str:
        return self._entries[self._index]

    @property
    def clean_snapshot(self) -> Optional[str]:
        return self._clean_snapshot

    @property
    def can_undo(self) -> bool:
        return self._index > 0

    @property
    def can_redo(self) -> bool:
        return self._index + 1 < len(self._entries)

    @property
    def is_dirty(self) -> bool:
        return (
            self._clean_snapshot is None
            or self.current_snapshot != self._clean_snapshot
        )

    def record(self, snapshot: str) -> bool:
        """Append one distinct state, dropping any abandoned redo branch."""
        if not isinstance(snapshot, str) or not snapshot:
            raise ValueError("A document snapshot is required.")
        if snapshot == self.current_snapshot:
            return False
        del self._entries[self._index + 1:]
        self._entries.append(snapshot)
        self._index += 1
        overflow = len(self._entries) - self.max_entries
        if overflow > 0:
            del self._entries[:overflow]
            self._index -= overflow
        return True

    def rollback_current(self, snapshot: str) -> bool:
        """Remove a failed transaction when it restores the preceding state."""
        if self._index > 0 and self._entries[self._index - 1] == snapshot:
            del self._entries[self._index:]
            self._index -= 1
            return True
        return self.record(snapshot)

    def undo(self) -> Optional[str]:
        if not self.can_undo:
            return None
        self._index -= 1
        return self.current_snapshot

    def redo(self) -> Optional[str]:
        if not self.can_redo:
            return None
        self._index += 1
        return self.current_snapshot

    def mark_clean(self) -> None:
        self._clean_snapshot = self.current_snapshot

    def reset(self, snapshot: str, mark_clean: bool = True) -> None:
        if not isinstance(snapshot, str) or not snapshot:
            raise ValueError("A document snapshot is required.")
        self._entries = [snapshot]
        self._index = 0
        self._clean_snapshot = snapshot if mark_clean else None
