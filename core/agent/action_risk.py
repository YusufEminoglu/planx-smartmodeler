"""Pure, QGIS-free risk classification and mode wording for the approval card.

Phase 07 (§9.2) requires "risk badges and exact target/action summaries" and
"visible Plan vs Act semantics". Both are **presentation over facts the trusted
boundary already computed** -- this module turns a proposal *kind* and the
already-validated *destructive* flag into a short badge, and a mode into one
plain sentence.

Two properties matter more than the wording:

- **Nothing here is an input to a decision.** No caller may gate apply, run,
  undo, policy or scope on a risk level; it exists to tell the human what they
  are about to authorize. Changing a level changes a label, never an outcome.
- **Nothing here comes from the provider.** :func:`assess_risk` accepts a kind
  string and a boolean, both produced by trusted validation, and an unknown kind
  fails **closed** to the highest level rather than to a reassuring default.
"""
from __future__ import annotations

from dataclasses import dataclass

from .identifiers import (
    MODEL_PROPOSAL_KIND,
    MODEL_RUN_KIND,
    PROCESSING_PROPOSAL_KIND,
    STYLE_PROPOSAL_KIND,
)

RISK_LOW = "low"
RISK_MEDIUM = "medium"
RISK_HIGH = "high"

#: Ordered least to most severe, so a caller can compare or sort without
#: hard-coding the strings.
RISK_LEVELS = (RISK_LOW, RISK_MEDIUM, RISK_HIGH)

_LABELS = {
    RISK_LOW: "Low risk",
    RISK_MEDIUM: "Medium risk",
    RISK_HIGH: "High risk",
}


@dataclass(frozen=True)
class RiskAssessment:
    """A bounded, display-only risk badge for one pending action."""

    level: str
    label: str
    reason: str
    reversible: bool

    def badge(self) -> str:
        """Return the single line shown next to the action's target."""
        suffix = "reversible with Undo" if self.reversible else "not reversible here"
        return f"{self.label} - {self.reason} ({suffix})."


def assess_risk(kind: str, destructive: bool) -> RiskAssessment:
    """Classify one pending action from its kind and validated destructive flag.

    ``destructive`` is the flag the Phase 03 preview computed inside the trusted
    boundary, not a claim the provider made about itself. An unrecognized kind is
    treated as high risk and non-reversible, because a kind this module has not
    been taught about is precisely the case where a soothing badge would mislead.
    """
    is_destructive = bool(destructive)
    if kind == STYLE_PROPOSAL_KIND:
        return RiskAssessment(
            level=RISK_LOW,
            label=_LABELS[RISK_LOW],
            reason="changes only how one layer is drawn, never its data",
            reversible=True,
        )
    if kind == MODEL_PROPOSAL_KIND:
        if is_destructive:
            return RiskAssessment(
                level=RISK_HIGH,
                label=_LABELS[RISK_HIGH],
                reason="replaces or removes parts of your current workflow",
                reversible=True,
            )
        return RiskAssessment(
            level=RISK_MEDIUM,
            label=_LABELS[RISK_MEDIUM],
            reason="edits your current workflow without removing existing steps",
            reversible=True,
        )
    if kind in (PROCESSING_PROPOSAL_KIND, MODEL_RUN_KIND):
        return RiskAssessment(
            level=RISK_MEDIUM,
            label=_LABELS[RISK_MEDIUM],
            reason=(
                "runs an analysis and adds temporary result layers; your source "
                "data and files are not written to"
            ),
            reversible=True,
        )
    return RiskAssessment(
        level=RISK_HIGH,
        label=_LABELS[RISK_HIGH],
        reason="this action kind is not recognized, so it is treated as unsafe",
        reversible=False,
    )


#: What each mode is allowed to reach, in the user's terms. Phrased so the two
#: lines the human most needs -- "review only" and "nothing happens until you
#: click" -- are impossible to miss.
_MODE_HINTS = {
    "ask": "Ask: read-only inspection. The agent answers and proposes nothing.",
    "plan": (
        "Plan: the agent may propose a change. It is shown for review only - "
        "there is no Apply or Run."
    ),
    "act": (
        "Act: the agent may prepare one action. Nothing happens until you click "
        "Apply or Run on its approval card."
    ),
}


def mode_hint(mode: str) -> str:
    """Return one plain sentence describing what ``mode`` can reach.

    An unknown mode falls back to the **most restrictive** wording rather than
    the most permissive, so a future mode cannot silently inherit an Act promise.
    """
    key = str(mode).strip().lower()
    return _MODE_HINTS.get(key, _MODE_HINTS["ask"])
