"""The decision, made in code. The risk reader supplies flags; it never decides.

Rules, checked top to bottom (first match wins):

| # | Condition                                              | Action  | Strike |
|---|--------------------------------------------------------|---------|--------|
| 1 | Agent already contained                                | BLOCK   | no     |
| 2 | Same request_id seen before                            | BLOCK   | no     |
| 3 | Anomaly (hard violation or risk severity 3) and the    | CONTAIN | yes    |
|   | agent already has a strike inside the window           |         |        |
| 4 | Anomaly                                                | BLOCK   | yes    |
|   | (a human is notified with the evidence)                |         |        |
| 5 | Risk severity 2 (suspicious, not conclusive)           | STEP_UP | no     |
| 6 | Amount above the trust-scaled auto-approve limit       | STEP_UP | no     |
| 7 | Otherwise                                              | ALLOW   | no     |

A replayed request is refused without a strike: a retry after a timeout is a
bug to absorb, not an attack. A STEP_UP is not a strike either; if the human
denies it, the engine records the strike then.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Sequence

from warden.models import Action, RiskReport

AGENT_CONTAINED = "AGENT_CONTAINED"
DUPLICATE = "DUPLICATE_REQUEST"


@dataclass(frozen=True)
class Verdict:
    action: Action
    reasons: list[str]
    strike: bool


def auto_approve_limit(per_txn_cap: Decimal, trust: float) -> Decimal:
    return (per_txn_cap * Decimal(str(trust))).quantize(Decimal("0.01"))


def decide(violations: Sequence[str], risk: RiskReport, trust: float, prior_strikes: int,
           amount: Decimal, per_txn_cap: Decimal) -> Verdict:
    if AGENT_CONTAINED in violations:
        return Verdict(Action.BLOCK, ["Agent is contained; nothing it proposes is executed."], strike=False)
    if DUPLICATE in violations:
        return Verdict(Action.BLOCK, ["Request already processed; Warden never pays twice."], strike=False)

    hard = list(violations)
    if hard or risk.severity >= 3:
        why = []
        if hard:
            why.append(f"Mandate violated: {', '.join(hard)}.")
        if risk.severity >= 3:
            why.append(f"Risk reader: likely manipulation ({', '.join(risk.flags)}).")
        if prior_strikes >= 1:
            return Verdict(Action.CONTAIN, why + ["Second strike: agent contained on every rail."], strike=True)
        return Verdict(Action.BLOCK, why + ["Blocked and sent to a human with the evidence."], strike=True)

    if risk.severity == 2:
        return Verdict(Action.STEP_UP, [f"Risk reader flagged: {', '.join(risk.flags)}. A human must approve."],
                       strike=False)

    limit = auto_approve_limit(per_txn_cap, trust)
    if amount > limit:
        return Verdict(Action.STEP_UP, [f"{amount} is above the auto-approve limit of {limit} at trust {trust}."],
                       strike=False)

    reasons = ["Within mandate and auto-approve limit."]
    if risk.flags:
        reasons.append(f"Noted: {', '.join(risk.flags)}.")
    return Verdict(Action.ALLOW, reasons, strike=False)
