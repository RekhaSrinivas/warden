"""The Warden engine: the only path from an agent's proposal to money moving.

submit():  dedupe lock -> mandate check -> risk read -> decide -> act -> audit
approve(): runs a STEP_UP proposal only if the human's approval hash still matches
deny():    refuses it and records a strike against the agent
contain(): the kill switch, also triggered automatically on a second strike
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict
from decimal import Decimal
from typing import Any, Callable, Mapping

from warden import approvals
from warden.audit import AuditLog
from warden.decide import AGENT_CONTAINED, DUPLICATE, decide
from warden.mandate import Mandate, SpendState, check
from warden.models import Action, Decision, Proposal, RiskReport
from warden.rails.base import Rail, RailDeclined, RailError
from warden.risk import RiskReader
from warden.trust import TrustLedger


class ApprovalError(Exception):
    """An approval that must not run: unknown proposal, changed proposal, or contained agent."""


class Warden:
    def __init__(self, mandate: Mandate, risk_reader: RiskReader, rails: Mapping[str, Rail],
                 audit: AuditLog | None = None, trust: TrustLedger | None = None,
                 balance: Callable[[], Decimal | None] | None = None,
                 clock: Callable[[], float] = time.time) -> None:
        """`rails` maps a proposal's rail kind ("transfer", "card", "vic_checkout", "x402") to the rail that runs it."""
        self.mandate = mandate
        self.risk_reader = risk_reader
        self.rails = dict(rails)
        self.audit = audit or AuditLog()
        self.trust = trust or TrustLedger()
        self.balance = balance
        self.clock = clock
        self.state = SpendState()
        self.pending: dict[str, Proposal] = {}
        self.decisions: dict[str, Decision] = {}
        self._lock = threading.Lock()  # one decision at a time, so two proposals can't both fit the same budget

    # --- agent side -------------------------------------------------------------

    def submit(self, proposal: Proposal) -> Decision:
        with self._lock:
            return self._submit(proposal)

    def _submit(self, p: Proposal) -> Decision:
        now = self.clock()
        self.audit.append("proposal_received", proposal=p.summary())
        if self.balance is not None:
            self.state.balance = self.balance()

        violations = check(self.mandate, p, self.state, now)
        if self.trust.is_contained(p.agent_id):
            violations.insert(0, AGENT_CONTAINED)
        self.state.seen_request_ids.add(p.request_id)  # from here on, a replay of this request is refused

        if AGENT_CONTAINED in violations or DUPLICATE in violations:
            risk = RiskReport.skipped("Refused before risk review.")
        else:
            risk = self.risk_reader.read(p)
        self.audit.append("risk_read", proposal_id=p.id, **asdict(risk))

        trust_before = self.trust.score(p.agent_id)
        verdict = decide(violations, risk, trust_before, self.trust.strikes(p.agent_id, now), p.amount,
                         self.mandate.per_txn_cap)
        decision = Decision(proposal_id=p.id, action=verdict.action, reasons=verdict.reasons, violations=violations,
                            risk=risk, trust_before=trust_before, trust_after=trust_before)
        if verdict.strike:
            decision.trust_after = self.trust.strike(p.agent_id, now)

        if verdict.action is Action.CONTAIN:
            decision.containment = self._contain(p.agent_id)
            decision.trust_after = self.trust.score(p.agent_id)
        elif verdict.action is Action.STEP_UP:
            self.pending[p.id] = p
            decision.approval_hash = approvals.approval_hash(p)
        elif verdict.action is Action.ALLOW:
            decision.execution = self._execute(p, now)
            decision.trust_after = self.trust.score(p.agent_id)

        return self._record(decision)

    # --- human side -------------------------------------------------------------

    def approve(self, proposal_id: str, approval_hash: str, approver: str = "human") -> Decision:
        with self._lock:
            p = self.pending.get(proposal_id)
            if p is None:
                raise ApprovalError(f"No pending proposal {proposal_id}")
            if not approvals.matches(p, approval_hash):
                self.audit.append("approval_rejected", proposal_id=proposal_id, reason="APPROVAL_MISMATCH")
                raise ApprovalError("This approval does not match the proposal the human saw.")
            del self.pending[proposal_id]
            if self.trust.is_contained(p.agent_id):
                self.audit.append("approval_rejected", proposal_id=proposal_id, reason=AGENT_CONTAINED)
                raise ApprovalError("The agent was contained after this proposal was made.")

            now = self.clock()
            trust = self.trust.score(p.agent_id)
            # Budgets may have moved while the proposal waited, so the mandate is checked again.
            violations = check(self.mandate, p, self.state, now, ignore_duplicate=True)
            original = self.decisions[proposal_id]
            decision = Decision(proposal_id=p.id, action=Action.BLOCK if violations else Action.ALLOW,
                                reasons=[f"Approved by {approver}."], violations=violations, risk=original.risk,
                                trust_before=trust, trust_after=trust, approval_hash=approval_hash)
            if violations:
                decision.reasons.append(f"Mandate violated at execution time: {', '.join(violations)}.")
            else:
                decision.execution = self._execute(p, now)
                decision.trust_after = self.trust.score(p.agent_id)
            return self._record(decision, event="approval")

    def deny(self, proposal_id: str, approver: str = "human") -> Decision:
        with self._lock:
            p = self.pending.pop(proposal_id, None)
            if p is None:
                raise ApprovalError(f"No pending proposal {proposal_id}")
            now = self.clock()
            trust_before = self.trust.score(p.agent_id)
            prior_strikes = self.trust.strikes(p.agent_id, now)
            trust_after = self.trust.strike(p.agent_id, now)
            decision = Decision(proposal_id=p.id, action=Action.BLOCK, reasons=[f"Denied by {approver}."],
                                violations=[], risk=self.decisions[proposal_id].risk,
                                trust_before=trust_before, trust_after=trust_after)
            if prior_strikes >= 1:
                decision.action = Action.CONTAIN
                decision.reasons.append("Second strike: agent contained on every rail.")
                decision.containment = self._contain(p.agent_id)
                decision.trust_after = self.trust.score(p.agent_id)
            return self._record(decision, event="denial")

    def contain(self, agent_id: str, reason: str) -> dict[str, Any]:
        """Manual kill switch."""
        with self._lock:
            results = self._contain(agent_id)
            self.audit.append("manual_containment", agent_id=agent_id, reason=reason, results=results)
            return results

    # --- internals ----------------------------------------------------------------

    def _execute(self, p: Proposal, now: float) -> dict[str, Any]:
        rail = self.rails.get(p.rail)
        if rail is None:
            return {"status": "NO_RAIL", "error": f"No rail configured for {p.rail}"}
        try:
            result = rail.execute(p)
        except RailDeclined as e:
            # Warden allowed it but the rail's own lock refused: that lock is the backstop.
            return {"rail": rail.name, "status": "DECLINED_BY_RAIL", "reason": e.reason, **e.detail}
        except RailError as e:
            return {"rail": rail.name, "status": "RAIL_ERROR", "error": str(e)}
        self.state.record(p.agent_id, p.amount, now)
        self.trust.reward(p.agent_id)
        return result

    def _contain(self, agent_id: str) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for rail in {id(r): r for r in self.rails.values()}.values():
            try:
                results[rail.name] = rail.contain(agent_id)
            except RailError as e:
                results[rail.name] = {"status": "CONTAIN_FAILED", "error": str(e)}
        self.trust.contain(agent_id)
        for pid in [pid for pid, p in self.pending.items() if p.agent_id == agent_id]:
            del self.pending[pid]  # nothing the agent proposed earlier may run now
        return results

    def _record(self, decision: Decision, event: str = "decision") -> Decision:
        self.audit.append(event, **asdict(decision))
        self.decisions[decision.proposal_id] = decision
        return decision
