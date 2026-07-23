"""Pure, in-memory, session-bound context-token (freshness receipt) service.

QGIS-free: standard library only. A *context token* is an opaque
HMAC-SHA-256 digest that binds one proposal kind plus a target identity plus a
canonical live-state snapshot to a per-session secret. It is created and
verified exclusively by trusted application code; a provider only ever echoes a
token that a read-only inspection tool already handed it, and can never forge,
invent, upgrade, or reuse a token after the relevant live state has changed.

A token authorizes nothing. It is a *freshness* check: it proves a proposal
was written against the exact state the inspection tool actually observed this
session, and that the session secret has not been rotated (**New chat**) in
between. The signed canonical state may contain the full graph internally (so a
parameter change invalidates the token), but only the opaque digest ever leaves
this service -- the signed state and the secret are never returned, logged, or
persisted.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from typing import Any, Callable, Optional

# A secret shorter than this is refused outright; 16 bytes (128 bits) is the
# floor, the default factory produces 32 bytes (256 bits).
_MIN_SECRET_BYTES = 16
_DEFAULT_SECRET_BYTES = 32


class ContextTokenError(ValueError):
    """Raised for an invalid secret or a non-serializable signing state."""


def _default_secret_factory() -> bytes:
    return secrets.token_bytes(_DEFAULT_SECRET_BYTES)


class ContextTokenService:
    """Issues and verifies opaque, session-bound HMAC freshness receipts.

    The secret lives only in this instance's process memory. ``rotate()`` (the
    **New chat** action) replaces it, which invalidates every previously issued
    token. Tests may inject a deterministic ``secret`` and/or ``secret_factory``
    so token values are reproducible; production uses a cryptographically
    random per-dock secret.
    """

    def __init__(
        self,
        secret: Optional[bytes] = None,
        secret_factory: Optional[Callable[[], bytes]] = None,
    ) -> None:
        if secret_factory is not None and not callable(secret_factory):
            raise ContextTokenError("secret_factory must be callable.")
        self._secret_factory: Callable[[], bytes] = (
            secret_factory or _default_secret_factory
        )
        if secret is None:
            secret = self._secret_factory()
        self._secret = self._coerce_secret(secret)

    @staticmethod
    def _coerce_secret(secret: Any) -> bytes:
        if isinstance(secret, bool) or not isinstance(secret, (bytes, bytearray)):
            raise ContextTokenError("Context-token secret must be raw bytes.")
        if len(secret) < _MIN_SECRET_BYTES:
            raise ContextTokenError(
                f"Context-token secret must be at least {_MIN_SECRET_BYTES} bytes."
            )
        return bytes(secret)

    def rotate(self, secret: Optional[bytes] = None) -> None:
        """Replace the session secret, invalidating all outstanding tokens.

        With no argument a fresh secret is produced by the configured factory;
        tests may pass an explicit ``secret`` to rotate deterministically.
        """
        if secret is None:
            secret = self._secret_factory()
        self._secret = self._coerce_secret(secret)

    def issue(self, kind: str, target_id: str, state: Any) -> str:
        """Return an opaque hex token binding ``kind``/``target_id``/``state``.

        The returned value is a bare HMAC-SHA-256 hex digest: it contains no
        secret and no signed state, so it is safe to hand to the provider and
        to keep in bounded session memory.
        """
        message = self._canonical_message(kind, target_id, state)
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def verify(self, token: Any, kind: str, target_id: str, state: Any) -> bool:
        """Return whether ``token`` matches the current signature of ``state``.

        Uses a constant-time comparison so a caller cannot learn the correct
        token byte by byte through timing. A non-string, empty, foreign, or
        stale token simply returns ``False`` -- verification never raises for
        an untrusted token value.
        """
        if not isinstance(token, str) or not token:
            return False
        try:
            expected = self.issue(kind, target_id, state)
        except ContextTokenError:
            return False
        return hmac.compare_digest(token, expected)

    @staticmethod
    def _canonical_message(kind: str, target_id: str, state: Any) -> bytes:
        if not isinstance(kind, str) or not kind:
            raise ContextTokenError("Token kind must be a non-empty string.")
        if not isinstance(target_id, str):
            raise ContextTokenError("Token target id must be a string.")
        try:
            state_text = json.dumps(
                state,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as error:
            raise ContextTokenError(
                "Token signing state is not canonically serializable."
            ) from error
        envelope = json.dumps(
            {"kind": kind, "target": target_id, "state": state_text},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return envelope.encode("utf-8")
