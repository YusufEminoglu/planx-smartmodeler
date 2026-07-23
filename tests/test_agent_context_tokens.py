"""Pure-Python tests for the session-bound context-token freshness service."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from planx_smartmodeler.core.agent.context_tokens import (
    ContextTokenError,
    ContextTokenService,
)

SECRET_A = b"0123456789abcdef0123456789abcdef"
SECRET_B = b"fedcba9876543210fedcba9876543210"


class DeterminismTests(unittest.TestCase):
    def test_injected_secret_is_deterministic(self) -> None:
        one = ContextTokenService(secret=SECRET_A)
        two = ContextTokenService(secret=SECRET_A)
        token_one = one.issue("model_patch", "current_model", {"n": 1})
        token_two = two.issue("model_patch", "current_model", {"n": 1})
        self.assertEqual(token_one, token_two)
        self.assertTrue(one.verify(token_one, "model_patch", "current_model", {"n": 1}))

    def test_different_kind_target_or_state_yield_different_tokens(self) -> None:
        service = ContextTokenService(secret=SECRET_A)
        base = service.issue("model_patch", "current_model", {"n": 1})
        self.assertNotEqual(base, service.issue("layer_style", "current_model", {"n": 1}))
        self.assertNotEqual(base, service.issue("model_patch", "other", {"n": 1}))
        self.assertNotEqual(base, service.issue("model_patch", "current_model", {"n": 2}))

    def test_state_change_invalidates_a_token(self) -> None:
        service = ContextTokenService(secret=SECRET_A)
        token = service.issue("model_patch", "current_model", {"nodes": ["a"]})
        self.assertFalse(
            service.verify(token, "model_patch", "current_model", {"nodes": ["a", "b"]})
        )

    def test_no_model_state_is_stable(self) -> None:
        service = ContextTokenService(secret=SECRET_A)
        token = service.issue("model_patch", "current_model", {"model": None})
        self.assertTrue(service.verify(token, "model_patch", "current_model", {"model": None}))


class VerificationTests(unittest.TestCase):
    def test_verify_uses_constant_time_comparison(self) -> None:
        service = ContextTokenService(secret=SECRET_A)
        token = service.issue("layer_style", "L1", {"a": 1})
        with patch(
            "planx_smartmodeler.core.agent.context_tokens.hmac.compare_digest",
            wraps=__import__("hmac").compare_digest,
        ) as spy:
            self.assertTrue(service.verify(token, "layer_style", "L1", {"a": 1}))
        spy.assert_called_once()

    def test_foreign_and_malformed_tokens_reject(self) -> None:
        service = ContextTokenService(secret=SECRET_A)
        other = ContextTokenService(secret=SECRET_B)
        foreign = other.issue("layer_style", "L1", {"a": 1})
        self.assertFalse(service.verify(foreign, "layer_style", "L1", {"a": 1}))
        self.assertFalse(service.verify("", "layer_style", "L1", {"a": 1}))
        self.assertFalse(service.verify(None, "layer_style", "L1", {"a": 1}))
        self.assertFalse(service.verify("deadbeef", "layer_style", "L1", {"a": 1}))
        self.assertFalse(service.verify(123, "layer_style", "L1", {"a": 1}))

    def test_secret_rotation_invalidates_earlier_tokens(self) -> None:
        service = ContextTokenService(secret=SECRET_A)
        token = service.issue("model_patch", "current_model", {"n": 1})
        service.rotate(secret=SECRET_B)
        self.assertFalse(service.verify(token, "model_patch", "current_model", {"n": 1}))
        fresh = service.issue("model_patch", "current_model", {"n": 1})
        self.assertTrue(service.verify(fresh, "model_patch", "current_model", {"n": 1}))

    def test_default_rotation_changes_the_secret(self) -> None:
        seeds = [SECRET_A, SECRET_B]
        service = ContextTokenService(secret_factory=lambda: seeds.pop(0))
        first = service.issue("model_patch", "t", {"n": 1})
        service.rotate()
        second = service.issue("model_patch", "t", {"n": 1})
        self.assertNotEqual(first, second)


class OpacityAndConstructorTests(unittest.TestCase):
    def test_public_token_contains_no_secret_or_signed_state(self) -> None:
        service = ContextTokenService(secret=SECRET_A)
        token = service.issue("layer_style", "L1", {"secret_field": "sentinel-value"})
        # A bare hex digest: no secret bytes, no state text.
        self.assertNotIn("sentinel-value", token)
        self.assertNotIn(SECRET_A.decode(), token)
        int(token, 16)  # must be pure hexadecimal
        self.assertEqual(len(token), 64)

    def test_invalid_constructor_inputs_fail_closed(self) -> None:
        with self.assertRaises(ContextTokenError):
            ContextTokenService(secret="not-bytes")  # type: ignore[arg-type]
        with self.assertRaises(ContextTokenError):
            ContextTokenService(secret=b"tooshort")
        with self.assertRaises(ContextTokenError):
            ContextTokenService(secret=True)  # type: ignore[arg-type]
        with self.assertRaises(ContextTokenError):
            ContextTokenService(secret_factory="nope")  # type: ignore[arg-type]

    def test_unserializable_state_fails_closed(self) -> None:
        service = ContextTokenService(secret=SECRET_A)
        with self.assertRaises(ContextTokenError):
            service.issue("model_patch", "t", {"bad": object()})


if __name__ == "__main__":
    unittest.main()
