"""Data types shared by every part of Warden."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

RailKind = Literal["transfer", "card", "vic_checkout", "x402"]


class Action(StrEnum):
    ALLOW = "ALLOW"
    STEP_UP = "STEP_UP"
    BLOCK = "BLOCK"
    CONTAIN = "CONTAIN"


@dataclass(frozen=True)
class Evidence:
    """Something the agent read. `trusted` is False for anything an outsider can write."""

    source: str  # e.g. "email:billing@acme-supplies.com", "invoice:INV-1001", "web:https://..."
    trusted: bool
    text: str


@dataclass(frozen=True)
class Proposal:
    """A payment the agent wants to make. Frozen so an approval stays bound to what was shown."""

    agent_id: str
    rail: RailKind
    amount: Decimal
    currency: str
    payee_id: str
    payee_name: str
    description: str
    merchant_category: str = ""
    evidence: tuple[Evidence, ...] = ()
    agent_reasoning: str = ""
    extra: dict[str, Any] = field(default_factory=dict)  # rail-specific: x402 url, merchant url, items
    id: str = field(default_factory=lambda: f"prop_{uuid.uuid4().hex[:12]}")
    request_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "amount", Decimal(str(self.amount)))
        object.__setattr__(self, "currency", self.currency.upper())
        object.__setattr__(self, "evidence", tuple(self.evidence))
        if not self.request_id:
            object.__setattr__(self, "request_id", f"req_{self.id}")

    def summary(self) -> dict[str, Any]:
        """What goes into the audit log: everything except rail-specific extras."""
        data = asdict(self)
        data.pop("extra")
        data["amount"] = str(self.amount)
        return data

    def evidence_hash(self) -> str:
        """sha256 of the canonical evidence list, so a receipt can prove what the decision was based on."""
        canonical = json.dumps([[e.source, e.trusted, e.text] for e in self.evidence], separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RiskReport:
    flags: tuple[str, ...]
    severity: int  # 0 none, 1 note, 2 needs a human, 3 likely manipulation
    explanation: str
    source: str  # "claude", "offline" or "skipped"

    @classmethod
    def skipped(cls, why: str) -> RiskReport:
        return cls(flags=(), severity=0, explanation=why, source="skipped")


@dataclass
class Decision:
    proposal_id: str
    action: Action
    reasons: list[str]
    violations: list[str]
    risk: RiskReport
    trust_before: float
    trust_after: float
    approval_hash: str | None = None
    execution: dict[str, Any] | None = None
    containment: dict[str, Any] | None = None
