"""The mandate: hard spending rules a human set, enforced in plain code.

No model is consulted here. If a rule should give the same answer every time,
it belongs in this file, not in a prompt.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from warden.models import Proposal

WEEK_SECONDS = 7 * 24 * 3600
HOUR_SECONDS = 3600

# Rails that pay a named counterparty must use an allowlisted payee; rails that
# buy from merchants are limited by merchant category instead.
PAYEE_RAILS = {"transfer", "x402"}
MERCHANT_RAILS = {"card", "vic_checkout"}


@dataclass(frozen=True)
class Mandate:
    per_txn_cap: Decimal = Decimal("500")
    weekly_cap: Decimal = Decimal("2000")
    currencies: frozenset[str] = frozenset({"USD"})
    allowed_mccs: frozenset[str] = frozenset()
    payee_allowlist: frozenset[str] = frozenset()
    reserve_floor: Decimal = Decimal("0")
    velocity_per_hour: int = 10


@dataclass
class SpendState:
    """What Warden has already let through. Kept by the engine, read by the mandate."""

    executions: dict[str, list[tuple[float, Decimal]]] = field(default_factory=dict)
    seen_request_ids: set[str] = field(default_factory=set)
    balance: Decimal | None = None  # available balance in the mandate currency, if known

    def spent_since(self, agent_id: str, since: float) -> Decimal:
        return sum((amt for ts, amt in self.executions.get(agent_id, []) if ts >= since), Decimal("0"))

    def count_since(self, agent_id: str, since: float) -> int:
        return sum(1 for ts, _ in self.executions.get(agent_id, []) if ts >= since)

    def record(self, agent_id: str, amount: Decimal, now: float) -> None:
        self.executions.setdefault(agent_id, []).append((now, amount))


def check(mandate: Mandate, proposal: Proposal, state: SpendState, now: float,
          ignore_duplicate: bool = False) -> list[str]:
    """Return every rule the proposal breaks. An empty list means the mandate allows it."""
    violations: list[str] = []
    amount = proposal.amount

    if not ignore_duplicate and proposal.request_id in state.seen_request_ids:
        violations.append("DUPLICATE_REQUEST")
    if amount <= 0:
        violations.append("NON_POSITIVE_AMOUNT")
    if proposal.currency not in mandate.currencies:
        violations.append("CURRENCY_NOT_ALLOWED")
    # Inclusive, like Airwallex card limits: a payment equal to the cap is allowed.
    if amount > mandate.per_txn_cap:
        violations.append("OVER_TXN_CAP")
    if state.spent_since(proposal.agent_id, now - WEEK_SECONDS) + amount > mandate.weekly_cap:
        violations.append("OVER_WEEKLY_CAP")
    if proposal.rail in PAYEE_RAILS and proposal.payee_id not in mandate.payee_allowlist:
        violations.append("PAYEE_NOT_ALLOWLISTED")
    if proposal.rail in MERCHANT_RAILS and proposal.merchant_category not in mandate.allowed_mccs:
        violations.append("MERCHANT_CATEGORY_NOT_ALLOWED")
    if state.balance is not None and state.balance - amount < mandate.reserve_floor:
        violations.append("RESERVE_FLOOR")
    if state.count_since(proposal.agent_id, now - HOUR_SECONDS) >= mandate.velocity_per_hour:
        violations.append("VELOCITY")
    return violations
