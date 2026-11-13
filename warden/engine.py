"""The Warden engine: the only path from an agent's proposal to money moving.

hire():    records an agent's mandate (what a human authorised) before it may spend
submit():  dedupe lock -> mandate check -> risk read -> decide -> act -> audit
approve(): runs a STEP_UP proposal only if the human's approval hash still matches
deny():    refuses it and records a strike against the agent
contain(): the kill switch for one agent, also triggered automatically on a second strike

Each agent has its own mandate, trust score and strikes. Containing one agent
freezes only its card and revokes only its Metal mandate; the others keep working.

Audit entry schema (stable: signed receipts are built from the log alone)
-------------------------------------------------------------------------
Every entry: {seq, ts, event, data, prev_hash, hash}. The `data` per event:

agent_hired        agent_id, role, mandate (Mandate.to_dict()), mandate_id, mandate_hash,
                   provisioning (card_id, cardholder_id, metal mandate_id / bind tx, ...)
proposal_received  proposal (Proposal.summary())
risk_read          proposal_id, flags, severity, explanation, source
decision | approval | denial
                   every Decision field (proposal_id, action, reasons, violations, risk, trust_before,
                   trust_after, approval_hash, execution, containment) plus:
                   agent_id, mandate_id, mandate_hash, rail, amount (str), currency, payee_id,
                   payee_name, merchant_category, request_id, evidence_hash, approver (None unless
                   a human acted).
                   execution holds the rail's real ids: Airwallex transfer_id/status/request_id;
                   card card_id, card_transaction_id, lifecycle_id, auth_event_id, clearing_event_id,
                   status; Metal transaction + explorer + credential; Airi mandate_id + approval_url.
approval_rejected  proposal_id, reason
manual_containment agent_id, reason, results (per rail, real ids)
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
from warden.mandate import Mandate, SpendState, check, mandate_hash
from warden.models import Action, Decision, Proposal, RiskReport
from warden.rails.base import Rail, RailDeclined, RailError
from warden.risk import RiskReader
from warden.trust import MIN_TRUST, TrustLedger


_NOT_EXECUTED = ("NO_RAIL", "DECLINED_BY_RAIL", "RAIL_ERROR")


class ApprovalError(Exception):
    """An approval that must not run: unknown proposal, changed proposal, or contained agent."""


class Warden:
    def __init__(self, mandate: Mandate, risk_reader: RiskReader, rails: Mapping[str, Rail],
                 audit: AuditLog | None = None, trust: TrustLedger | None = None,
                 balance: Callable[[], Decimal | None] | None = None,
                 clock: Callable[[], float] = time.time, mandates: Mapping[str, Mandate] | None = None) -> None:
        """`rails` maps a proposal's rail kind ("transfer", "card", "vic_checkout", "x402") to the rail that runs it.
        `mandate` applies to any agent without its own entry in `mandates`."""
        self.mandate = mandate
        self.mandates: dict[str, Mandate] = dict(mandates or {})
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
        # Called with (proposal, decision) once a payment is allowed and before the rail runs it, so a narrator
        # can show the decision before a rail that waits on a human (Airi's passkey) asks for anything.
        self.on_allow: Callable[[Proposal, Decision], None] | None = None

    def mandate_for(self, agent_id: str) -> Mandate:
        return self.mandates.get(agent_id, self.mandate)

    # --- hiring -------------------------------------------------------------------

    def hire(self, agent_id: str, mandate: Mandate, role: str = "",
             provisioning: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """Give an agent its mandate. Logged with its hash so every later receipt can point back to it."""
        with self._lock:
            self.mandates[agent_id] = mandate
            return self.audit.append("agent_hired", agent_id=agent_id, role=role, mandate=mandate.to_dict(),
                                     mandate_id=mandate.mandate_id, mandate_hash=mandate_hash(mandate),
                                     provisioning=dict(provisioning or {}))

    # --- agent side -------------------------------------------------------------

    def submit(self, proposal: Proposal) -> Decision:
        with self._lock:
            return self._submit(proposal)

    def _submit(self, p: Proposal) -> Decision:
        now = self.clock()
        mandate = self.mandate_for(p.agent_id)
        self.audit.append("proposal_received", proposal=p.summary())
        if self.balance is not None:
            self.state.balance = self.balance()

        violations = check(mandate, p, self.state, now)
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
                         mandate.auto_approve_cap)
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
            decision.execution = self._execute(p, now, decision)
            decision.trust_after = self.trust.score(p.agent_id)

        return self._record(decision, p)

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
            violations = check(self.mandate_for(p.agent_id), p, self.state, now, ignore_duplicate=True)
            original = self.decisions[proposal_id]
            decision = Decision(proposal_id=p.id, action=Action.BLOCK if violations else Action.ALLOW,
                                reasons=[f"Approved by {approver}."], violations=violations, risk=original.risk,
                                trust_before=trust, trust_after=trust, approval_hash=approval_hash)
            if violations:
                decision.reasons.append(f"Mandate violated at execution time: {', '.join(violations)}.")
            else:
                decision.execution = self._execute(p, now, decision)
                decision.trust_after = self.trust.score(p.agent_id)
            return self._record(decision, p, event="approval", approver=approver)

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
            return self._record(decision, p, event="denial", approver=approver)

    def contain(self, agent_id: str, reason: str) -> dict[str, Any]:
        """Manual kill switch for one agent."""
        with self._lock:
            results = self._contain(agent_id)
            self.audit.append("manual_containment", agent_id=agent_id, reason=reason, results=results)
            return results

    def restore(self) -> None:
        """Rebuild trust, strikes, containment, spend and the duplicate lock from the audit log, so a
        restarted Warden (say, the dashboard after the live demo) remembers what already happened.
        Pending approvals are not restored: nothing proposed before a restart may run without a fresh look."""
        standing: dict[str, dict[str, Any]] = {}
        for entry in self.audit.entries():
            event, data, ts = entry["event"], entry["data"], entry["ts"]
            if event == "proposal_received":
                self.state.seen_request_ids.add(data["proposal"].get("request_id", ""))
            elif event == "manual_containment":
                standing.setdefault(data["agent_id"], {"score": 1.0, "strikes": []})["contained"] = True
            elif event in ("decision", "approval", "denial") and data.get("agent_id"):
                agent = standing.setdefault(data["agent_id"], {"score": 1.0, "strikes": []})
                agent["score"] = data.get("trust_after", agent["score"])
                if data.get("action") in ("BLOCK", "CONTAIN") and (data.get("trust_after") or 1) < (data.get("trust_before") or 1):
                    agent["strikes"].append(ts)
                if data.get("action") == "CONTAIN":
                    agent["contained"] = True
                execution = data.get("execution") or {}
                if data.get("action") == "ALLOW" and execution and execution.get("status") not in _NOT_EXECUTED:
                    self.state.record(data["agent_id"], Decimal(data["amount"]), ts)
        for agent_id, s in standing.items():
            contained = s.get("contained", False)
            self.trust.restore(agent_id, MIN_TRUST if contained else s["score"], s["strikes"], contained)

    # --- internals ----------------------------------------------------------------

    def _execute(self, p: Proposal, now: float, decision: Decision | None = None) -> dict[str, Any]:
        rail = self.rails.get(p.rail)
        if rail is None:
            return {"status": "NO_RAIL", "error": f"No rail configured for {p.rail}"}
        if self.on_allow is not None and decision is not None:
            self.on_allow(p, decision)
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

    def _record(self, decision: Decision, p: Proposal, event: str = "decision", approver: str | None = None) -> Decision:
        mandate = self.mandate_for(p.agent_id)
        self.audit.append(event, **asdict(decision), agent_id=p.agent_id, mandate_id=mandate.mandate_id,
                          mandate_hash=mandate_hash(mandate), rail=p.rail, amount=str(p.amount), currency=p.currency,
                          payee_id=p.payee_id, payee_name=p.payee_name, merchant_category=p.merchant_category,
                          request_id=p.request_id, evidence_hash=p.evidence_hash(), approver=approver)
        self.decisions[decision.proposal_id] = decision
        return decision
