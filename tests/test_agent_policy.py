from __future__ import annotations

import unittest

from planx_smartmodeler.core.agent.contracts import AgentMode, AgentRisk, AgentScope, PolicyOutcome
from planx_smartmodeler.core.agent.policy import decide

EXPECTED_MATRIX = {
    (AgentRisk.READ_ONLY, AgentMode.ASK): PolicyOutcome.ALLOW,
    (AgentRisk.READ_ONLY, AgentMode.PLAN): PolicyOutcome.ALLOW,
    (AgentRisk.READ_ONLY, AgentMode.ACT): PolicyOutcome.ALLOW,
    (AgentRisk.REVERSIBLE, AgentMode.ASK): PolicyOutcome.DENY,
    (AgentRisk.REVERSIBLE, AgentMode.PLAN): PolicyOutcome.PREVIEW_ONLY,
    (AgentRisk.REVERSIBLE, AgentMode.ACT): PolicyOutcome.REQUIRE_APPROVAL,
    (AgentRisk.MUTATING, AgentMode.ASK): PolicyOutcome.DENY,
    (AgentRisk.MUTATING, AgentMode.PLAN): PolicyOutcome.PREVIEW_ONLY,
    (AgentRisk.MUTATING, AgentMode.ACT): PolicyOutcome.REQUIRE_APPROVAL,
    (AgentRisk.DESTRUCTIVE, AgentMode.ASK): PolicyOutcome.DENY,
    (AgentRisk.DESTRUCTIVE, AgentMode.PLAN): PolicyOutcome.PREVIEW_ONLY,
    (AgentRisk.DESTRUCTIVE, AgentMode.ACT): PolicyOutcome.REQUIRE_APPROVAL,
    (AgentRisk.EXTERNAL, AgentMode.ASK): PolicyOutcome.DENY,
    (AgentRisk.EXTERNAL, AgentMode.PLAN): PolicyOutcome.PREVIEW_ONLY,
    (AgentRisk.EXTERNAL, AgentMode.ACT): PolicyOutcome.REQUIRE_APPROVAL,
    (AgentRisk.PROHIBITED, AgentMode.ASK): PolicyOutcome.DENY,
    (AgentRisk.PROHIBITED, AgentMode.PLAN): PolicyOutcome.DENY,
    (AgentRisk.PROHIBITED, AgentMode.ACT): PolicyOutcome.DENY,
}


class PolicyMatrixTests(unittest.TestCase):
    def test_every_matrix_cell(self) -> None:
        for (risk, mode), expected_outcome in EXPECTED_MATRIX.items():
            with self.subTest(risk=risk, mode=mode):
                decision = decide(risk, mode, AgentScope.PROJECT, AgentScope.ALL)
                self.assertEqual(decision.outcome, expected_outcome)

    def test_all_risks_and_modes_are_covered(self) -> None:
        self.assertEqual(len(EXPECTED_MATRIX), len(AgentRisk.ALL) * len(AgentMode.ALL))


class FailClosedTests(unittest.TestCase):
    def test_unknown_mode_denies(self) -> None:
        decision = decide(AgentRisk.READ_ONLY, "unknown_mode", AgentScope.PROJECT, AgentScope.ALL)
        self.assertEqual(decision.outcome, PolicyOutcome.DENY)
        self.assertEqual(decision.reason_code, "unknown_mode")

    def test_unknown_scope_denies(self) -> None:
        decision = decide(AgentRisk.READ_ONLY, AgentMode.ASK, "unknown_scope", AgentScope.ALL)
        self.assertEqual(decision.outcome, PolicyOutcome.DENY)
        self.assertEqual(decision.reason_code, "unknown_scope")

    def test_unknown_risk_denies(self) -> None:
        decision = decide("unknown_risk", AgentMode.ASK, AgentScope.PROJECT, AgentScope.ALL)
        self.assertEqual(decision.outcome, PolicyOutcome.DENY)
        self.assertEqual(decision.reason_code, "unknown_risk")

    def test_scope_restricted_tool_denies_outside_its_scopes(self) -> None:
        decision = decide(
            AgentRisk.READ_ONLY, AgentMode.ASK, AgentScope.PLUGINS, (AgentScope.CURRENT_MODEL,)
        )
        self.assertEqual(decision.outcome, PolicyOutcome.DENY)
        self.assertEqual(decision.reason_code, "scope_not_allowed")

    def test_scope_restricted_tool_allows_inside_its_scopes(self) -> None:
        decision = decide(
            AgentRisk.READ_ONLY, AgentMode.ASK, AgentScope.CURRENT_MODEL, (AgentScope.CURRENT_MODEL,)
        )
        self.assertEqual(decision.outcome, PolicyOutcome.ALLOW)


class ApprovalTests(unittest.TestCase):
    def test_approval_upgrades_require_approval_to_allow(self) -> None:
        decision = decide(
            AgentRisk.MUTATING, AgentMode.ACT, AgentScope.PROJECT, AgentScope.ALL, approved=True
        )
        self.assertEqual(decision.outcome, PolicyOutcome.ALLOW)
        self.assertEqual(decision.reason_code, "approved")

    def test_approval_cannot_override_prohibited(self) -> None:
        for mode in AgentMode.ALL:
            decision = decide(
                AgentRisk.PROHIBITED, mode, AgentScope.PROJECT, AgentScope.ALL, approved=True
            )
            self.assertEqual(decision.outcome, PolicyOutcome.DENY)
            self.assertEqual(decision.reason_code, "prohibited")

    def test_approval_does_not_affect_allow_or_deny_outcomes(self) -> None:
        allow_decision = decide(
            AgentRisk.READ_ONLY, AgentMode.ASK, AgentScope.PROJECT, AgentScope.ALL, approved=True
        )
        self.assertEqual(allow_decision.outcome, PolicyOutcome.ALLOW)
        deny_decision = decide(
            AgentRisk.MUTATING, AgentMode.ASK, AgentScope.PROJECT, AgentScope.ALL, approved=True
        )
        self.assertEqual(deny_decision.outcome, PolicyOutcome.DENY)

    def test_approval_does_not_affect_preview_only(self) -> None:
        decision = decide(
            AgentRisk.MUTATING, AgentMode.PLAN, AgentScope.PROJECT, AgentScope.ALL, approved=True
        )
        self.assertEqual(decision.outcome, PolicyOutcome.PREVIEW_ONLY)


class ApprovalTypeConfusionTests(unittest.TestCase):
    """Regression coverage for blocking finding 1: only the boolean singleton
    ``True`` may authorize an approval-gated (REQUIRE_APPROVAL) tool call."""

    NON_BOOLEAN_APPROVALS = ("false", "true", 1, 0, None, ["x"], {"a": 1}, "True", "False")

    def test_non_boolean_values_never_upgrade_require_approval(self) -> None:
        for value in self.NON_BOOLEAN_APPROVALS:
            with self.subTest(approved=value):
                decision = decide(
                    AgentRisk.MUTATING,
                    AgentMode.ACT,
                    AgentScope.PROJECT,
                    AgentScope.ALL,
                    approved=value,
                )
                self.assertNotEqual(decision.outcome, PolicyOutcome.ALLOW)
                self.assertEqual(decision.reason_code, "invalid_approval_value")

    def test_real_true_upgrades_require_approval(self) -> None:
        decision = decide(
            AgentRisk.MUTATING, AgentMode.ACT, AgentScope.PROJECT, AgentScope.ALL, approved=True
        )
        self.assertEqual(decision.outcome, PolicyOutcome.ALLOW)
        self.assertEqual(decision.reason_code, "approved")

    def test_real_false_requires_approval_normally(self) -> None:
        decision = decide(
            AgentRisk.MUTATING, AgentMode.ACT, AgentScope.PROJECT, AgentScope.ALL, approved=False
        )
        self.assertEqual(decision.outcome, PolicyOutcome.REQUIRE_APPROVAL)

    def test_non_boolean_approval_cannot_override_prohibited(self) -> None:
        for value in self.NON_BOOLEAN_APPROVALS:
            with self.subTest(approved=value):
                decision = decide(
                    AgentRisk.PROHIBITED,
                    AgentMode.ACT,
                    AgentScope.PROJECT,
                    AgentScope.ALL,
                    approved=value,
                )
                self.assertEqual(decision.outcome, PolicyOutcome.DENY)
                self.assertEqual(decision.reason_code, "prohibited")

    def test_non_boolean_approval_does_not_affect_outcomes_where_it_is_irrelevant(self) -> None:
        # A malformed approval value must not spuriously fail a call where
        # approval was never going to matter (read-only ALLOW, or plain DENY
        # in Ask mode) - it only matters, and is validated, where it could
        # actually upgrade the decision.
        allow_decision = decide(
            AgentRisk.READ_ONLY,
            AgentMode.ASK,
            AgentScope.PROJECT,
            AgentScope.ALL,
            approved="not-a-bool",
        )
        self.assertEqual(allow_decision.outcome, PolicyOutcome.ALLOW)
        deny_decision = decide(
            AgentRisk.MUTATING,
            AgentMode.ASK,
            AgentScope.PROJECT,
            AgentScope.ALL,
            approved="not-a-bool",
        )
        self.assertEqual(deny_decision.outcome, PolicyOutcome.DENY)


if __name__ == "__main__":
    unittest.main()
