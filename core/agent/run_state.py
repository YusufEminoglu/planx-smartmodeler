"""Pure, QGIS-free state machine for the ONE running agent action.

The execution coordinator is a ``QObject`` bound to QGIS Processing, so its
safety-critical bookkeeping lives here instead, where it is unit-testable
without Qt: the one-run-max rule, the monotonic run id that makes a late
callback inert, the terminal-and-idempotent cancel, and the sanitizer that keeps
a raw Processing exception (which routinely embeds a source path or a connection
string) out of every user-visible surface.

Nothing here runs, adds, or removes anything.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from . import context as agent_context

# Every message that reaches the UI, the ledger, or a signal is bounded to this.
MAX_RUN_MESSAGE_CHARS = 240
MAX_RUN_LABEL_CHARS = 160

IDLE = "idle"
RUNNING = "running"
FINISHED = "finished"
FAILED = "failed"
CANCELED = "canceled"

# Anything that looks like a path, URI, connection string, or credential is
# replaced wholesale rather than trimmed: a Processing failure message is
# untrusted text and is never shown verbatim.
_UNSAFE_MESSAGE = re.compile(
    r"""(?xi)
    (?: [A-Za-z]:[\\/] )                 # C:\ or C:/
    | (?: [\\/]{2} )                     # UNC or //
    | (?: \b[A-Za-z][A-Za-z0-9+.\-]*:// ) # scheme://
    | (?: \b(?:dbname|host|user|password|pwd|authcfg|apikey|api_key|token|
             service|sslmode|connection)\s*[=:] )
    | (?: \.(?:gpkg|shp|tif|tiff|sqlite|db|gdb|csv|json|geojson|xml|vrt|kml)\b )
    """
)
_GENERIC_FAILURE = "The run failed. See the QGIS Processing log for details."


def sanitize_run_message(message: object) -> str:
    """Return a bounded, path-free, credential-free message for one run failure.

    A Processing exception string that mentions any path/URI/connection/data
    file is discarded entirely in favour of a fixed generic sentence, because
    partially redacting such a string reliably leaks the rest of it.
    """
    text = message if isinstance(message, str) else str(message)
    text = " ".join(text.split())
    if not text:
        return _GENERIC_FAILURE
    if _UNSAFE_MESSAGE.search(text):
        return _GENERIC_FAILURE
    return agent_context.bound_text(text, MAX_RUN_MESSAGE_CHARS)


@dataclass(frozen=True)
class RunTicket:
    """The identity of one started run. A callback carrying a stale ticket is
    ignored, so a late Processing result can never add a layer after cancel."""

    run_id: int
    action_id: str
    kind: str
    title: str


class RunState:
    """Tracks the single running action: start/cancel/finish and late results."""

    def __init__(self) -> None:
        self._run_id = 0
        self._ticket: Optional[RunTicket] = None
        self._status = IDLE
        self._canceled = False

    @property
    def status(self) -> str:
        return self._status

    @property
    def ticket(self) -> Optional[RunTicket]:
        return self._ticket

    @property
    def canceled(self) -> bool:
        return self._canceled

    def is_running(self) -> bool:
        return self._status == RUNNING

    def start(self, action_id: str, kind: str, title: str) -> Optional[RunTicket]:
        """Claim the single run slot, or return ``None`` when one is running."""
        if self._status == RUNNING:
            return None
        self._run_id += 1
        self._canceled = False
        self._status = RUNNING
        self._ticket = RunTicket(
            run_id=self._run_id,
            action_id=agent_context.bound_text(str(action_id), 64),
            kind=agent_context.bound_text(str(kind), 40),
            title=agent_context.bound_text(str(title), MAX_RUN_LABEL_CHARS),
        )
        return self._ticket

    def cancel(self) -> bool:
        """Mark the current run canceled. Terminal and idempotent.

        Returns whether this call is the one that performed the cancellation, so
        the coordinator emits exactly one ``run_canceled``.
        """
        if self._status != RUNNING or self._canceled:
            return False
        self._canceled = True
        return True

    def accepts(self, ticket: Optional[RunTicket]) -> bool:
        """Whether a result carrying ``ticket`` may still take effect.

        A result is accepted only for the run that is still the current one and
        was not canceled -- so a Processing call that returns after cancel or
        after teardown adds no layer and revives nothing.
        """
        if ticket is None or self._ticket is None:
            return False
        if self._status != RUNNING or self._canceled:
            return False
        return ticket.run_id == self._ticket.run_id

    def finish(self, ticket: Optional[RunTicket], status: str) -> bool:
        """Close out ``ticket``'s run with a terminal ``status``.

        Returns whether the transition happened; a stale ticket closes nothing,
        so a late callback cannot reopen or overwrite a terminal state.
        """
        if status not in (FINISHED, FAILED, CANCELED):
            status = FAILED
        if self._ticket is None or ticket is None or ticket.run_id != self._ticket.run_id:
            return False
        if self._status != RUNNING:
            return False
        self._status = status
        return True

    def reset(self) -> None:
        """Tear down for shutdown/New chat: any in-flight result is now stale."""
        if self._status == RUNNING:
            self._canceled = True
        self._status = IDLE
        self._ticket = None
        self._run_id += 1
