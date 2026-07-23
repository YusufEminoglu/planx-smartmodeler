"""Fail-closed policy engine enforcing the Phase 01 mode/risk matrix.

| Risk        | Ask  | Plan          | Act              |
|-------------|------|---------------|------------------|
| Read-only   | Allow| Allow         | Allow            |
| Reversible  | Deny | Preview only  | Require approval |
| Mutating    | Deny | Preview only  | Require approval |
| Destructive | Deny | Preview only  | Require approval |
| External    | Deny | Preview only  | Require approval |
| Prohibited  | Deny | Deny          | Deny             |

Any unknown mode, scope, or risk value denies. An approval flag can never
convert ``PROHIBITED`` into ``ALLOW``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .contracts import AgentMode, AgentRisk, AgentScope, PolicyOutcome

_MATRIX = {
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

_REASON_BY_OUTCOME = {
    PolicyOutcome.ALLOW: "Read-only actions are always allowed.",
    PolicyOutcome.PREVIEW_ONLY: (
        "Plan mode only previews this action; it is not executed."
    ),
    PolicyOutcome.REQUIRE_APPROVAL: (
        "Act mode requires explicit user approval before this action can run."
    ),
    PolicyOutcome.DENY: "This action is not permitted in Ask mode.",
}


@dataclass(frozen=True)
class PolicyDecision:
    """A policy outcome plus a user-visible reason and a stable reason code."""

    outcome: str
    reason: str
    reason_code: str


def decide(
    risk: str,
    mode: str,
    scope: str,
    allowed_scopes: Iterable[str],
    approved: Any = False,
) -> PolicyDecision:
    """Return a fail-closed :class:`PolicyDecision` for one tool call.

    Unknown mode/scope/risk values deny. A tool restricted to certain scopes
    denies outside of them. Only the boolean singleton ``True`` can upgrade a
    ``REQUIRE_APPROVAL`` outcome to ``ALLOW``. Any other value for
    ``approved`` (a string, an integer, ``None``, a container, or any other
    truthy/falsy non-boolean) never upgrades a decision; when it is supplied
    for a call that would otherwise require approval, it is rejected outright
    with a stable reason code rather than silently treated as "not approved".
    ``approved`` can never upgrade ``PROHIBITED``, ``DENY``, or
    ``PREVIEW_ONLY`` regardless of its value or type.
    """
    if mode not in AgentMode.ALL:
        return PolicyDecision(PolicyOutcome.DENY, "Unknown agent mode.", "unknown_mode")
    if scope not in AgentScope.ALL:
        return PolicyDecision(PolicyOutcome.DENY, "Unknown agent scope.", "unknown_scope")
    if risk not in AgentRisk.ALL:
        return PolicyDecision(PolicyOutcome.DENY, "Unknown tool risk level.", "unknown_risk")

    allowed = tuple(allowed_scopes)
    if scope not in allowed:
        return PolicyDecision(
            PolicyOutcome.DENY,
            "This tool is not valid in the selected scope.",
            "scope_not_allowed",
        )

    outcome = _MATRIX.get((risk, mode))
    if outcome is None:
        return PolicyDecision(PolicyOutcome.DENY, "No policy rule matched.", "no_rule")

    if risk == AgentRisk.PROHIBITED:
        return PolicyDecision(PolicyOutcome.DENY, "This action is prohibited.", "prohibited")

    if outcome == PolicyOutcome.REQUIRE_APPROVAL:
        if isinstance(approved, bool):
            if approved is True:
                return PolicyDecision(PolicyOutcome.ALLOW, "Approved by the user.", "approved")
        else:
            return PolicyDecision(
                PolicyOutcome.DENY,
                "The approval flag must be exactly true or false.",
                "invalid_approval_value",
            )

    return PolicyDecision(outcome, _REASON_BY_OUTCOME[outcome], outcome)
