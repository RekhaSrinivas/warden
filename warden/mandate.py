"""The mandate: hard spending rules a human set, enforced in plain code.

No model is consulted here. If a rule should give the same answer every time,
it belongs in this file, not in a prompt.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

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
    # Who the mandate belongs to and what the human said it is for. Not rules, but
    # part of the hash, so a receipt proves which instruction a payment ran under.
    mandate_id: str = ""
    agent_id: str = ""
    purpose: str = ""
    instruction: str = ""
    # Payments above this always go to a human, whatever the agent's trust. None means per_txn_cap.
    approval_threshold: Decimal | None = None
    human_required: tuple[str, ...] = ()  # the human's own words, e.g. "new bank details"

    @property
    def auto_approve_cap(self) -> Decimal:
        return min(self.per_txn_cap, self.approval_threshold) if self.approval_threshold is not None else self.per_txn_cap

    def to_dict(self) -> dict[str, Any]:
        return {
            "mandate_id": self.mandate_id, "agent_id": self.agent_id, "purpose": self.purpose,
            "instruction": self.instruction, "per_txn_cap": str(self.per_txn_cap), "weekly_cap": str(self.weekly_cap),
            "currencies": sorted(self.currencies), "allowed_mccs": sorted(self.allowed_mccs),
            "payee_allowlist": sorted(self.payee_allowlist), "reserve_floor": str(self.reserve_floor),
            "velocity_per_hour": self.velocity_per_hour,
            "approval_threshold": None if self.approval_threshold is None else str(self.approval_threshold),
            "human_required": list(self.human_required),
        }


def mandate_hash(mandate: Mandate) -> str:
    """sha256 of the canonical mandate: the fingerprint every receipt carries."""
    canonical = json.dumps(mandate.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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
