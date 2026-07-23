"""Pure, QGIS-free, bounded in-session ledger of agent action outcomes.

Records only *what happened* to an action -- proposed, approved, rejected,
applied, failed, undone, expired or superseded -- with a bounded safe target
label and a stable reason code. It never stores raw parameters, feature or
category values, secrets, paths, tokens, digests, or raw proposal JSON, and it
never persists to a file, project property, log or setting. It is cleared on
**New chat** and dock shutdown.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional

from . import context as agent_context

MAX_LEDGER_ENTRIES = 100
_MAX_TARGET = 200
_MAX_TITLE = 160
_MAX_REASON = 80


class ActionStatus:
    """Stable, user-safe status labels for a ledger entry."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    APPLIED = "applied"
    FAILED = "failed"
    UNDONE = "undone"
    EXPIRED = "expired"
    SUPERSEDED = "superseded"
    # Phase 05 -- approved safe Processing / current-model execution.
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELED = "canceled"
    ALL = (
        PROPOSED,
        APPROVED,
        REJECTED,
        APPLIED,
        FAILED,
        UNDONE,
        EXPIRED,
        SUPERSEDED,
        RUNNING,
        COMPLETED,
        CANCELED,
    )


@dataclass(frozen=True)
class LedgerEntry:
    """One bounded, JSON-compatible action-outcome record (no raw values)."""

    seq: int
    action_id: str
    kind: str
    target: str
    title: str
    status: str
    is_destructive: bool
    reason_code: str

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "action_id": self.action_id,
            "kind": self.kind,
            "target": self.target,
            "title": self.title,
            "status": self.status,
            "destructive": self.is_destructive,
            "reason_code": self.reason_code,
        }


class ActionLedger:
    """A bounded FIFO of :class:`LedgerEntry`; oldest entries drop past the cap."""

    def __init__(self, max_entries: int = MAX_LEDGER_ENTRIES) -> None:
        self._max = max(1, int(max_entries))
        self._entries: Deque[LedgerEntry] = deque(maxlen=self._max)
        self._seq = 0

    def record(
        self,
        action_id: str,
        kind: str,
        target: str,
        title: str,
        status: str,
        *,
        is_destructive: bool = False,
        reason_code: str = "",
    ) -> LedgerEntry:
        if status not in ActionStatus.ALL:
            status = ActionStatus.FAILED
        self._seq += 1
        entry = LedgerEntry(
            seq=self._seq,
            action_id=agent_context.bound_text(str(action_id), 64),
            kind=agent_context.bound_text(str(kind), 40),
            target=agent_context.bound_text(str(target), _MAX_TARGET),
            title=agent_context.bound_text(str(title), _MAX_TITLE),
            status=status,
            is_destructive=bool(is_destructive),
            reason_code=agent_context.bound_text(str(reason_code), _MAX_REASON),
        )
        self._entries.append(entry)
        return entry

    def entries(self) -> List[LedgerEntry]:
        """Return a detached list, oldest first."""
        return list(self._entries)

    def latest(self) -> Optional[LedgerEntry]:
        return self._entries[-1] if self._entries else None

    def clear(self) -> None:
        self._entries.clear()
