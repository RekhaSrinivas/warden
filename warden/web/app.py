"""Warden's dashboard: one story for a finance team (and a judge). Hire it, guard it, prove it, recover.

Everything on the page is derived from the audit log, the signed receipt book and the case
file, so the screen can never claim something those records do not hold. The mode-specific
work (drafting with Claude, provisioning real cards, reconciling real charges) sits behind
`Services`, so the offline demo and the live sandbox render through the same code.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from warden import approvals, demo_world as w, gauntlet
from warden import hire as h
from warden import receipts as rc
from warden.audit import AuditLog
from warden.config import ROOT
from warden.authorize import RemoteAuthorizer
from warden.engine import ApprovalError, Warden
from warden.gateway import Gateway, TokenStore
from warden.mcp_server import TOKENS_PATH, build_server
from warden.mandate import Mandate
from warden.models import Evidence, Proposal
from warden.rails.fake import FakeRail
from warden.web.verify import create_verify_router

PUBLIC_WRITES = {"/airwallex/remote-auth"}


def is_card_decision(entry: dict[str, Any]) -> bool:
    """A real-time answer about one of Warden's own agents' cards (not a self-test, not a stranger's card)."""
    data = entry["data"]
    return (entry["event"] == "remote_authorization" and bool(data.get("agent_id"))
            and not str(data.get("event_id") or "").startswith("selftest_"))


def is_local_host(host: str) -> bool:
    """True for a request addressed to this machine (the Host header, port ignored)."""
    name = host.strip().lower()
    name = name[1:name.index("]")] if name.startswith("[") and "]" in name else name.rsplit(":", 1)[0] if name.count(":") == 1 else name
    return name in ("127.0.0.1", "localhost", "::1", "testserver", "")

HERE = Path(__file__).resolve().parent
WEEK = 7 * 24 * 3600
ASSET_VERSION = str(int(time.time()))  # cache-buster for /static links, new on every start
TAGLINE = "Warden doesn't ask you to trust your AI agents. It makes that trust enforceable, revocable, and verifiable."

# Rail kinds grouped the way a finance team thinks about them.
RAIL_GROUPS = [
    ("airwallex", "Airwallex", ("transfer", "card")),
    ("airi", "Visa VIC · Airi", ("vic_checkout",)),
    ("metal", "Alta x402", ("x402",)),
]
DECISION_EVENTS = ("decision", "approval", "denial")
MCP_PATH = "/mcp"
CASE_PIPELINE = ["UNATTRIBUTED", "INVESTIGATING", "DISPUTE_SUBMITTED", "RECOVERED"]


class ApproveBody(BaseModel):
    proposal_id: str
    approval_hash: str


class ProposalBody(BaseModel):
    proposal_id: str


class AgentBody(BaseModel):
    agent_id: str = w.PROCUREMENT


class ConfirmBody(BaseModel):
    agent_id: str
    mandate_id: str  # the draft the manager saw; a newer or older draft is refused


class FireBody(BaseModel):
    agent_id: str
    reason: str = "Fired from the dashboard"


class HireBody(BaseModel):
    agent_id: str
    role: str = ""
    instruction: str


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def _short(value: Any, keep: int = 6) -> str | None:
    """Enough of an id to recognise it on screen, never the whole thing."""
    s = str(value or "")
    return None if not s else (s if len(s) <= keep * 2 + 1 else f"{s[:keep]}…{s[-4:]}")


# --- mode-specific services -------------------------------------------------------------

@dataclass
class Services:
    """What differs between the offline demo and the live sandbox."""

    mode: str
    receipts_path: Path
    public_key_path: Path
    sync_receipts: Callable[[Warden], None]
    draft: Callable[[str, str], tuple[h.MandateDraft, h.Validated]]  # (agent_id, instruction)
    commit_hire: Callable[[Warden, str, str, str, h.MandateDraft, h.Validated], dict[str, Any]]
    fire: Callable[[Warden, str, str], dict[str, Any]]
    reconcile: Callable[[Warden], list[dict[str, Any]]]
    cases: Callable[[], list[dict[str, Any]]]
    locks: Callable[[Warden], dict[str, Any]]
    test_locks: Callable[[Warden], dict[str, Any]] | None = None
    passkey: Callable[[Warden], dict[str, Any] | None] = lambda warden: None  # an Airi checkout waiting on the human
    # Charge a card straight at the network, skipping Warden (copied card details). Live only; None offline.
    charge_card_directly: Callable[[str, Decimal, str], dict[str, Any]] | None = None
    audit_path: Path | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def offline_warden(audit_path: Path | None = None) -> Warden:
    """Both agents hired on simulated rails. File-backed audit so the verify page can check it."""
    warden, _ = w.offline_warden(hire=False)
    warden.audit = AuditLog(audit_path)
    warden.hire(w.PROCUREMENT, w.procurement_mandate(), role="procurement",
                provisioning={"card_id": "simulated_card_procurement", "simulated": True})
    warden.hire(w.RESEARCH, w.research_mandate(), role="research",
                provisioning={"card_id": "simulated_card_research", "metal_mandate_id": "simulated_mandate_research",
                              "simulated": True})
    return warden


def offline_services(workdir: Path | None = None) -> Services:
    from warden import recover as rv

    workdir = workdir or Path(tempfile.mkdtemp(prefix="warden-offline-"))
    receipts_path, public_key_path = workdir / "receipts.jsonl", workdir / "receipt-public-key.pem"
    key = rc.load_or_create_signing_key(workdir / "receipt-signing-key.pem", public_key_path)
    cases: list[dict[str, Any]] = []
    provisioner = h.FakeProvisioner()

    def draft(agent_id: str, instruction: str) -> tuple[h.MandateDraft, h.Validated]:
        d = h.offline_draft(instruction)
        return d, h.validate(d, h.OrgPolicy(), agent_id, instruction)

    def commit_hire(warden: Warden, agent_id: str, role: str, instruction: str, d: h.MandateDraft,
                    validated: h.Validated) -> dict[str, Any]:
        provisioning = h.provision(agent_id, validated, provisioner, confirm=True)
        warden.hire(agent_id, validated.mandate, role=role, provisioning=provisioning)
        return {"agent_id": agent_id, "provisioning": provisioning}

    def reconcile(warden: Warden) -> list[dict[str, Any]]:
        fake = rv.FakeAirwallex()  # one $85 charge with a receipt, one $180 charge made outside Warden
        contained = {a for a in warden.mandates if warden.trust.is_contained(a)}
        found = rv.run(fake, rv.FakeUploader(), {w.PROCUREMENT: "card_fake"}, rv.offline_receipts(), [],
                       contained, settlement=rv.SandboxSettlement(fake))
        cases[:] = [c.to_json() for c in found]
        return cases

    def test_locks(warden: Warden) -> dict[str, Any]:
        """Simulated rails mimic each lock; the live page shows the real answers from the demo run."""
        frozen = warden.trust.is_contained(w.PROCUREMENT)
        results = [
            {"title": "$25 on procurement-bot's card", "lock": "Airwallex card status",
             "expected": "CARD_INACTIVE", "got": "CARD_INACTIVE" if frozen else "APPROVED (card still active)"},
            {"title": "$40 gift-shop charge on research-bot's card", "lock": "Airwallex merchant categories",
             "expected": "MERCHANT_CATEGORY_NOT_ALLOWED", "got": "MERCHANT_CATEGORY_NOT_ALLOWED"},
            {"title": "Data payment under procurement-bot's Alta mandate", "lock": "Alta on-chain mandate",
             "expected": "mandate_revoked", "got": "mandate_revoked" if frozen else "PAID (mandate still bound)"},
            {"title": "$60 Visa credential requested straight from Airi; nobody confirms", "lock": "Airi passkey",
             "expected": "NO_PASSKEY", "got": "NO_PASSKEY: Airi issued no credential; mandate cancelled"},
        ]
        for r in results:
            r["held"] = r["expected"] in r["got"]
        locks_state.update(results=results, source="simulated rails", ran_at=time.time())
        return locks_state

    locks_state: dict[str, Any] = {"results": [], "source": "simulated rails"}

    return Services(
        mode="offline", receipts_path=receipts_path, public_key_path=public_key_path,
        sync_receipts=lambda warden: rc.sync_entries(warden.audit.entries(), receipts_path, key) and None,
        draft=draft, commit_hire=commit_hire,
        fire=lambda warden, agent_id, reason: warden.contain(agent_id, reason),
        reconcile=reconcile, cases=lambda: cases, locks=lambda warden: locks_state, test_locks=test_locks,
        audit_path=workdir / "audit.jsonl",
    )


def live_services(lw: Any) -> Services:
    """Real rails: Claude drafts, Airwallex and Metal provision, cases come from real card charges."""
    from warden import live
    from warden import recover as rv

    def draft(agent_id: str, instruction: str) -> tuple[h.MandateDraft, h.Validated]:
        d = h.draft_mandate(instruction)
        return d, h.validate(d, h.OrgPolicy(), agent_id, instruction)

    def commit_hire(warden: Warden, agent_id: str, role: str, instruction: str, d: h.MandateDraft,
                    validated: h.Validated) -> dict[str, Any]:
        return live.commit_hire(lw, agent_id, role, instruction, d, validated, confirm=True)

    def fire(warden: Warden, agent_id: str, reason: str) -> dict[str, Any]:
        results = warden.contain(agent_id, reason)  # freezes its card and revokes its Alta mandate
        if agent_id in lw.registry.agents:
            lw.registry.fire(agent_id)
        return results

    def cases() -> list[dict[str, Any]]:
        current = set(lw.airwallex.agent_cards.values())  # this story's cards; older cases stay on file
        return [c.to_json() for c in rv.load_cases() if c.card_id in current]

    def locks(warden: Warden) -> dict[str, Any]:
        evidence = _read_json(live.ROOT / "data" / "evidence.json") or {}
        results = [{"title": p.get("title"), "expected": p.get("expected"), "held": p.get("held"),
                    "lock": LOCK_NAMES.get(str(p.get("expected")), ""),
                    "got": str((p.get("result") or {}).get("failure_reason") or (p.get("result") or {}).get("reason")
                               or (p.get("result") or {}).get("result")),
                    "ids": {k: v for k, v in (p.get("result") or {}).items()
                            if k in ("card_id", "card_transaction_id", "credential", "transaction", "mandate_id") and v}}
                   for p in evidence.get("independent_locks", [])]
        return {"results": results, "source": f"live demo run {evidence.get('ran_at', '')}".strip()}

    def charge_card_directly(card_id: str, amount: Decimal, mcc: str) -> dict[str, Any]:
        from warden.rails.airwallex import AirwallexError

        try:
            data = lw.airwallex.simulation.card_authorization(card_id, amount, "USD", mcc)
        except AirwallexError as e:
            return {"result": "ERROR", "failure_reason": e.summary()}
        return {"result": data.get("process_result") or data.get("status"), "failure_reason": data.get("failure_reason"),
                "card_transaction_event_id": data.get("card_transaction_event_id")}

    return Services(
        mode="live", receipts_path=rc.RECEIPTS_PATH, public_key_path=rc.PUBLIC_KEY_PATH,
        sync_receipts=lambda warden: live.issue_receipts(lw) and None,
        draft=draft, commit_hire=commit_hire, fire=fire,
        reconcile=lambda warden: [c.to_json() for c in live.reconcile_and_recover(lw)],
        cases=cases, locks=locks, audit_path=lw.warden.audit.path,
        passkey=lambda warden: live.open_passkey_request(warden.audit.entries()),
        extra={"airi": "configured" if lw.airi else "waiting for allowlist"},
        charge_card_directly=charge_card_directly,
    )


LOCK_NAMES = {"CARD_INACTIVE": "Airwallex card status", "MERCHANT_CATEGORY_NOT_ALLOWED": "Airwallex merchant categories",
              "revoked": "Alta on-chain mandate", "NO_PASSKEY": "Airi passkey (the human confirms every Visa checkout)"}


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


# --- the page's data ----------------------------------------------------------------------

class Dashboard:
    """Reads the audit log, receipt book and cases into the shapes the page renders."""

    def __init__(self, warden: Warden, services: Services) -> None:
        self.warden = warden
        self.services = services

    @property
    def mode(self) -> str:
        return self.services.mode

    def card_agents(self) -> dict[str, str]:
        """Card id -> agent id, for each agent's current card (its latest hire)."""
        latest: dict[str, str] = {}
        for entry in self.warden.audit.entries():
            if entry["event"] == "agent_hired":
                card = (entry["data"].get("provisioning") or {}).get("card_id")
                if card:
                    latest[entry["data"]["agent_id"]] = card
        return {card: agent for agent, card in latest.items()}

    def agents(self) -> list[dict[str, Any]]:
        hired: dict[str, dict[str, Any]] = {}
        for entry in self.warden.audit.entries():
            if entry["event"] == "agent_hired":
                hired[entry["data"]["agent_id"]] = {**entry["data"], "hired_ts": entry["ts"]}
        now = time.time()
        out = []
        for agent_id, mandate in self.warden.mandates.items():
            record = hired.get(agent_id, {})
            prov = record.get("provisioning") or {}
            spent = self.warden.state.spent_since(agent_id, now - WEEK)
            out.append({
                "id": agent_id,
                "role": record.get("role") or "",
                "purpose": mandate.purpose,
                "instruction": mandate.instruction,
                "mandate_id": mandate.mandate_id,
                "mandate_hash": _short(record.get("mandate_hash"), 8),
                "per_txn_cap": str(mandate.per_txn_cap),
                "weekly_cap": str(mandate.weekly_cap),
                "spent_week": str(spent),
                "budget_pct": float(min(Decimal(100), spent / mandate.weekly_cap * 100)) if mandate.weekly_cap else 0.0,
                "currencies": sorted(mandate.currencies),
                "mccs": sorted(mandate.allowed_mccs),
                "payees": len(mandate.payee_allowlist),
                "human_required": list(mandate.human_required),
                "trust": self.warden.trust.score(agent_id),
                "strikes": self.warden.trust.strikes(agent_id, now),
                "contained": self.warden.trust.is_contained(agent_id),
                "card": {"id": _short(prov.get("card_id")), "simulated": bool(prov.get("simulated"))}
                if prov.get("card_id") else None,
                "metal": {"mandate_id": _short(prov.get("metal_mandate_id"), 8),
                          "explorer": prov.get("metal_bind_explorer"),
                          "cap": prov.get("metal_cap_usdz"), "budget": prov.get("metal_budget_usdz")}
                if prov.get("metal_mandate_id") else None,
            })
        return out

    def feed(self) -> list[dict[str, Any]]:
        """One card per submission. A retry of the same proposal is its own card (blocked as a duplicate),
        so the original keeps showing what really happened to it."""
        items: list[dict[str, Any]] = []
        latest: dict[str, int] = {}  # proposal id -> index of its most recent submission
        for entry in self.warden.audit.entries():
            event, data = entry["event"], entry["data"]
            if event == "proposal_received":
                pid = data["proposal"]["id"]
                latest[pid] = len(items)
                items.append({"proposal": data["proposal"], "history": [], "rejected_approvals": []})
            elif event in DECISION_EVENTS and data.get("proposal_id") in latest:
                items[latest[data["proposal_id"]]]["history"].append(
                    {**data, "event": event, "seq": entry["seq"], "ts": entry["ts"],
                     "hash": entry["hash"], "prev_hash": entry["prev_hash"]})
            elif event == "approval_rejected" and data.get("proposal_id") in latest:
                items[latest[data["proposal_id"]]]["rejected_approvals"].append(data.get("reason", ""))
        for i, item in enumerate(items):
            pid = item["proposal"]["id"]
            item["decision"] = (item["history"] or [None])[-1]
            item["pending"] = pid in self.warden.pending and latest.get(pid) == i
        return items[::-1]

    def inbox(self) -> list[dict[str, Any]]:
        items = []
        for pid, p in self.warden.pending.items():
            decision = self.warden.decisions.get(pid)
            items.append({"proposal": p.summary(), "approval_hash": approvals.approval_hash(p),
                          "risk": _jsonable(decision.risk) if decision else None,
                          "reasons": decision.reasons if decision else []})
        return items

    def rails(self) -> list[dict[str, Any]]:
        chips = []
        for key, label, kinds in RAIL_GROUPS:
            rails = [self.warden.rails[k] for k in kinds if k in self.warden.rails]
            if not rails:
                status = self.services.extra.get(key, "offline")
            elif all(isinstance(r, FakeRail) for r in rails):
                status = "simulated"
            else:
                status = "live"
            chips.append({"key": key, "label": label, "status": status})
        return chips

    def receipts(self) -> dict[str, Any]:
        self.services.sync_receipts(self.warden)
        book = rc.load(self.services.receipts_path)
        try:
            public_key = rc.load_public_key(self.services.public_key_path)
            broken = rc.verify_chain(book, public_key)
            fingerprint = rc.key_fingerprint(public_key)
        except (OSError, ValueError):
            broken, fingerprint = 0 if book else None, None
        rows = []
        for i, r in enumerate(book):
            b = r.body
            execution = b.get("execution") or {}
            rows.append({
                "id": r.id, "agent_id": b.get("agent_id"), "action": (b.get("decision") or {}).get("action"),
                "amount": b.get("amount"), "currency": b.get("currency"), "rail": b.get("rail"),
                "payee": (b.get("payee") or {}).get("name"), "settlement": b.get("settlement_status"),
                "issued_at": b.get("issued_at"), "signed": broken is None or i < broken,
                "approver": (b.get("approval") or {}).get("approver"),
                "metal_tx": execution.get("transaction") if b.get("rail") == "x402" else None,
            })
        return {"count": len(book), "chain_intact": broken is None, "broken_at": broken,
                "fingerprint": fingerprint, "recent": rows[-40:][::-1]}

    def protection(self, feed: list[dict[str, Any]], cases: list[dict[str, Any]],
                   entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Money Warden protected, counted once per payment from its final decision."""
        total = {k: Decimal("0") for k in ("paid", "stopped", "double_payments", "held", "declined_live",
                                          "under_dispute", "recovered", "lost")}
        count = {k: 0 for k in total}

        def add(key: str, amount: Any) -> None:
            total[key] += Decimal(str(amount or "0"))
            count[key] += 1

        for item in feed:
            p, d = item["proposal"], item["decision"]
            if item["pending"]:
                add("held", p["amount"])
            elif d is None:
                continue
            elif d["action"] == "ALLOW":
                if (d.get("execution") or {}).get("status") not in ("NO_RAIL", "DECLINED_BY_RAIL", "RAIL_ERROR"):
                    add("paid", p["amount"])
            elif "DUPLICATE_REQUEST" in (d.get("violations") or []):
                add("double_payments", p["amount"])
            else:
                add("stopped", p["amount"])
        for e in entries:
            if is_card_decision(e) and e["data"].get("response_status") == "DECLINED":
                add("declined_live", e["data"].get("amount"))
        for c in cases:
            state = str(c.get("state"))
            key = "recovered" if state == "RECOVERED" else "lost" if state == "LOST" else                 "under_dispute" if state in ("UNATTRIBUTED", "INVESTIGATING", "DISPUTE_SUBMITTED") else None
            if key:
                add(key, c.get("amount"))
        protected = total["stopped"] + total["double_payments"] + total["declined_live"] + total["recovered"]
        return {"currency": "USD", "protected": str(protected),
                **{k: {"amount": str(v), "count": count[k]} for k, v in total.items()}}

    def state(self) -> dict[str, Any]:
        entries = self.warden.audit.entries()
        broken = self.warden.audit.verify()
        feed = self.feed()
        cases = self.services.cases()
        return _jsonable({
            "mode": self.mode,
            "tagline": TAGLINE,
            "agents": self.agents(),
            "rails": self.rails(),
            "feed": feed,
            "protection": self.protection(feed, cases, entries),
            "inbox": self.inbox(),
            "receipts": self.receipts(),
            "cases": {"pipeline": CASE_PIPELINE, "items": cases},
            "locks": self.services.locks(self.warden),
            "passkey": self.services.passkey(self.warden),
            "authorizations": [{k: e["data"].get(k) for k in ("agent_id", "amount", "currency", "merchant_name",
                                                               "merchant_category", "response_status", "reason",
                                                               "decided_in_ms", "proposal_id")} | {"ts": e["ts"]}
                               for e in entries if is_card_decision(e)][-20:][::-1],
            "flags": [{"agent_id": e["data"].get("agent_id"), "summary": e["data"].get("summary"),
                       "source": e["data"].get("source"), "ts": e["ts"]}
                      for e in entries if e["event"] == "agent_flag"][-20:][::-1],
            "audit": {"count": len(entries), "intact": broken is None, "broken_at": broken,
                      "recent": [{k: e[k] for k in ("seq", "ts", "event", "prev_hash", "hash")}
                                 for e in entries[-40:]][::-1]},
        })


# --- scenarios the buttons fire -----------------------------------------------------------

# The order a presenter wants: held for a human first (the inbox), then a block (strike one), then
# the one that fires the agent. Unlisted attacks follow in their gauntlet order.
ATTACK_ORDER = ("Lookalike domain invoice", "Fake 'bank details changed' email", "CEO impersonation")


def attack_for(agent_id: str, pool: list[gauntlet.Scenario], turn: int | None = None) -> tuple[str, Proposal]:
    """A one-proposal attack aimed at this agent: its own scenarios first, else one retargeted at it.
    With `turn`, the attacks come in a fixed order (repeatable for a demo); without it, at random."""
    own = [s for s in pool if s.steps()[0].agent_id == agent_id] or pool
    rank = {name: i for i, name in enumerate(ATTACK_ORDER)}
    own = sorted(own, key=lambda s: next((rank[k] for k in rank if s.name.startswith(k)), len(rank)))
    scenario = random.choice(own) if turn is None else own[turn % len(own)]
    proposal = scenario.steps()[0]
    if proposal.agent_id != agent_id:
        proposal = replace(proposal, agent_id=agent_id)
    return scenario.name, proposal


def single_step_attacks() -> list[gauntlet.Scenario]:
    """Attacks that are one proposal long, so one click is one attempt the audience can follow."""
    return [s for s in gauntlet.SCENARIOS if s.attack and len(s.steps()) == 1]


# A hired agent's ordinary card spend, by the merchant categories its mandate allows.
TYPICAL_PURCHASE: dict[str, tuple[str, str, Decimal]] = {
    "4511": ("United Airlines", "Flight to the customer meeting", Decimal("0.6")),
    "7011": ("Marriott", "Hotel, two nights", Decimal("0.45")),
    "4722": ("Amtrak via travel agent", "Train tickets", Decimal("0.3")),
    "4121": ("Yellow Cab", "Taxi to the airport", Decimal("0.1")),
    "5812": ("Corner Bistro", "Team lunch", Decimal("0.2")),
    w.MCC_OFFICE_SUPPLIES: ("Office Depot", "Notebooks and pens", Decimal("0.15")),
    w.MCC_STATIONERY: ("Staples", "Printer paper", Decimal("0.15")),
    w.MCC_OFFICE_FURNITURE: ("OfficeCo", "Desk lamp", Decimal("0.3")),
    w.MCC_SOFTWARE: ("JetBrains", "IDE licence", Decimal("0.3")),
    w.MCC_DATA_SERVICES: ("DataHub", "Monthly data feed", Decimal("0.2")),
}


BIG_PURCHASE: dict[str, str] = {
    "7011": "Hotel, five nights for the sales kickoff",
}  # preferred first; any other category falls back to a larger order of its usual purchase


def normal_day(agent_id: str, mandate: Mandate | None = None) -> list[Proposal]:
    """Each agent's ordinary work. Approved bills carry a stable request id, so pressing the button
    twice is a retry Warden absorbs ("never pays twice"), not a second payment."""
    if agent_id == w.RESEARCH:
        return [w.data_api_call(), w.data_api_call()]  # pay-per-call: every call is a new purchase
    if agent_id == w.PROCUREMENT:
        return [w.invoice("Acme Office Supply", "420", "INV-1001", request_id="req_day_INV-1001"),
                w.invoice("Globex Paper Co", "260", "INV-1002", request_id="req_day_INV-1002"),
                replace(w.supplies_card(), request_id="req_day_PR-311")]
    if mandate is None:
        return []
    day = []
    for mcc in sorted(mandate.allowed_mccs):
        if mcc not in TYPICAL_PURCHASE:
            continue
        merchant, what, share = TYPICAL_PURCHASE[mcc]
        ceiling = mandate.per_txn_cap if mandate.approval_threshold is None else min(mandate.per_txn_cap, mandate.approval_threshold)
        amount = max(Decimal("1"), (ceiling * share).quantize(Decimal("1")))
        day.append(Proposal(
            agent_id=agent_id, rail="card", amount=amount, currency=sorted(mandate.currencies)[0],
            payee_id=f"merchant_{merchant.lower().split()[0]}", payee_name=merchant, merchant_category=mcc,
            description=what, request_id=f"req_day_{agent_id}_{mcc}",
            evidence=(Evidence(f"purchase_request:{agent_id}-{mcc}", True, f"Team lead approved: {what.lower()}."),),
            agent_reasoning=f"{what}, approved by the team lead and inside my mandate."))
    day = day[:3]
    # One bigger purchase above the "needs me" line, so an ordinary day also shows a human approving.
    limit = mandate.approval_threshold
    if day and limit is not None and limit < mandate.per_txn_cap:
        base = next((p for mcc in BIG_PURCHASE for p in day if p.merchant_category == mcc), day[-1])
        what = BIG_PURCHASE.get(base.merchant_category, f"Larger order: {base.description.lower()}")
        day.append(replace(
            base, amount=(limit + (mandate.per_txn_cap - limit) / 2).quantize(Decimal("1")), description=what,
            request_id=base.request_id + "_big",
            evidence=(Evidence(f"purchase_request:{agent_id}-{base.merchant_category}-big", True,
                               f"Team lead asked for it: {what.lower()}."),),
            agent_reasoning=f"{what}. It is over my approval line, so a human has to approve it."))
    return day


def published_live_view() -> dict[str, Any] | None:
    """The newest published live run's console view, marked with where it came from."""
    runs = sorted((Path(__file__).resolve().parents[2] / "docs" / "live-runs").glob("*/dashboard-state.json"))
    view = _read_json(runs[-1]) if runs else None
    if view:
        view["snapshot"] = runs[-1].parent.name
    return view


# --- the app ------------------------------------------------------------------------------

def offline_factory() -> tuple[Warden, Services]:
    services = offline_services()
    return offline_warden(services.audit_path), services


class TokenBody(BaseModel):
    agent_id: str


def create_app(warden: Warden | None = None, services: Services | None = None,
               factory: Callable[[], tuple[Warden, Services]] | None = None,
               tokens: TokenStore | None = None) -> FastAPI:
    """With no arguments: the offline demo, with a Reset button. Live: pass the live Warden and services.
    Either way, agents connect over MCP at /mcp and their requests run through this same Warden."""
    if warden is None or services is None:
        factory = factory or offline_factory
        warden, services = factory()

    tokens = tokens or TokenStore(TOKENS_PATH)
    gateway = Gateway(lambda: app.state.board.warden)  # follows a Reset to the new Warden
    mcp = build_server(gateway, tokens)
    mcp_app = mcp.streamable_http_app(streamable_http_path=MCP_PATH, stateless_http=True, json_response=True)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="Warden", docs_url=None, redoc_url=None, lifespan=lifespan)

    @app.middleware("http")
    async def public_is_read_only(request: Request, call_next):
        """Through a public tunnel the console is look-only: nobody else can move money, hire or fire.
        The one write a stranger may make is Airwallex's signed remote-authorization call."""
        if (not is_local_host(request.headers.get("host", "")) and request.method not in ("GET", "HEAD")
                and request.url.path not in PUBLIC_WRITES):
            return JSONResponse(status_code=403, content={
                "detail": "This public link is read-only. Run Warden locally to try the buttons (see the README)."})
        return await call_next(request)

    app.router.routes.extend(mcp_app.routes)
    app.state.tokens = tokens
    app.state.gateway = gateway
    # Live: only requests signed with the secret Airwallex issued are answered. Offline: unsigned, for demos and tests.
    secret = os.environ.get("AIRWALLEX_REMOTE_AUTH_SECRET", "").strip() or None
    app.state.authorizer = RemoteAuthorizer(lambda: app.state.board.warden, lambda: app.state.board.card_agents(),
                                            secret=secret if services.mode == "live" else None)
    app.state.board = Dashboard(warden, services)
    app.state.factory = factory
    app.state.drafts = {}
    app.state.attack_turns: dict[str, int] = {}  # each agent's next attack, in ATTACK_ORDER
    app.state.scorecard = None
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    def current_book() -> tuple[Path, Path, Path]:  # the verify page follows whatever demo is running now
        b = board()
        b.services.sync_receipts(b.warden)
        return b.services.receipts_path, b.services.public_key_path, b.services.audit_path

    app.include_router(create_verify_router(current=current_book))

    def board() -> Dashboard:
        return app.state.board

    def agent_or_404(agent_id: str) -> None:
        if agent_id not in board().warden.mandates:
            raise HTTPException(404, f"No agent called {agent_id}")

    def page(template: str, view: dict[str, Any] | None = None) -> str:
        html = (HERE / "templates" / template).read_text(encoding="utf-8")
        initial = json.dumps(view or board().state()).replace("</", "<\\/")
        return html.replace("{{MODE}}", board().mode).replace("{{TAGLINE}}", TAGLINE) \
                   .replace("{{V}}", ASSET_VERSION).replace("{{INITIAL_STATE}}", initial)

    @app.get("/", response_class=HTMLResponse)
    def story() -> str:
        """The scroll story: the live run, told scene by scene. Offline with nothing run yet, it replays
        the published live run (docs/live-runs), labelled as such, rather than an empty book."""
        view = board().state()
        if view["mode"] != "live" and not view["feed"]:
            view = published_live_view() or view
        return page("story.html", view)

    @app.get("/lab", response_class=HTMLResponse)
    def lab() -> str:
        """Which defence stops which attack: docs/lab/results.json, made by `python -m warden.lab`."""
        results = ROOT / "docs" / "lab" / "results.json"
        data = results.read_text(encoding="utf-8") if results.exists() else "null"
        html = (HERE / "templates" / "lab.html").read_text(encoding="utf-8")
        return html.replace("{{V}}", ASSET_VERSION).replace("{{LAB}}", data.replace("</", "<" + "\\" + "/"))

    @app.get("/console", response_class=HTMLResponse)
    def index() -> str:
        """The live console: hire, guard, prove, recover, locks."""
        return page("index.html")

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return board().state()

    # Hire -------------------------------------------------------------------------------
    @app.post("/api/hire/draft")
    def hire_draft(body: HireBody) -> dict[str, Any]:
        agent_id = body.agent_id.strip().lower().replace(" ", "-")
        if not agent_id or not body.instruction.strip():
            raise HTTPException(400, "Give the agent a name and an instruction")
        if agent_id in board().warden.mandates and not board().warden.trust.is_contained(agent_id):
            raise HTTPException(409, f"{agent_id} is already working; fire it first or pick another name")
        draft, validated = board().services.draft(agent_id, body.instruction)
        app.state.drafts[agent_id] = (body.role or agent_id, body.instruction, draft, validated)
        return _jsonable({"agent_id": agent_id, "draft": draft.to_dict(), "changes": validated.changes,
                          "errors": validated.errors, "mandate": validated.mandate.to_dict()})

    @app.post("/api/hire/confirm")
    def hire_confirm(body: ConfirmBody) -> dict[str, Any]:
        """Provisions exactly the mandate the manager just saw; nothing is re-drafted."""
        held = app.state.drafts.get(body.agent_id)
        if held is None:
            raise HTTPException(404, "Draft a mandate first")
        if held[3].mandate.mandate_id != body.mandate_id:
            raise HTTPException(409, "The job changed since this draft; draft the mandate again")
        role, instruction, draft, validated = app.state.drafts.pop(body.agent_id)
        try:
            hired = board().services.commit_hire(board().warden, body.agent_id, role, instruction, draft, validated)
        except h.HireError as e:
            raise HTTPException(422, str(e)) from None
        return _jsonable({"agent_id": body.agent_id, "provisioning": hired.get("provisioning")})

    @app.post("/airwallex/remote-auth")
    async def remote_auth(request: Request) -> JSONResponse:
        """Airwallex Issuing asks: approve this card charge? Answered from memory, well inside 2.5 seconds."""
        if board().mode == "live" and app.state.authorizer.secret is None:
            return JSONResponse(status_code=503, content={"error": "remote auth secret not configured"})
        status, answer = app.state.authorizer.handle(dict(request.headers), await request.body())
        return JSONResponse(status_code=status, content=answer)

    @app.post("/api/mcp/token")
    def mcp_token(body: TokenBody, request: Request) -> dict[str, Any]:
        """Issue a token so a real agent (Claude Code, an Agent SDK app) can connect as this hired agent."""
        agent_or_404(body.agent_id)
        token = app.state.tokens.issue(body.agent_id)
        url = f"{str(request.base_url).rstrip('/')}{MCP_PATH}"
        return {"agent_id": body.agent_id, "token": token, "url": url,
                "claude_code": f'claude mcp add --transport http warden {url} --header "Authorization: Bearer {token}"'}

    @app.post("/api/fire")
    def fire(body: FireBody) -> dict[str, Any]:
        agent_or_404(body.agent_id)
        return _jsonable({"agent_id": body.agent_id,
                          "results": board().services.fire(board().warden, body.agent_id, body.reason)})

    # Guard ------------------------------------------------------------------------------
    @app.post("/api/attack")
    def attack(body: AgentBody) -> dict[str, Any]:
        agent_or_404(body.agent_id)
        turn = app.state.attack_turns.get(body.agent_id, 0)
        app.state.attack_turns[body.agent_id] = turn + 1
        name, proposal = attack_for(body.agent_id, single_step_attacks(), turn)
        decision = board().warden.submit(proposal)
        return _jsonable({"scenario": name, "proposal_id": proposal.id, "action": decision.action})

    @app.post("/api/card-outside-warden")
    def card_outside_warden(body: AgentBody) -> dict[str, Any]:
        """Someone copied the agent's card details and spends at a merchant the card itself allows, within its
        limits: only Warden can stop it, and only if Airwallex asks Warden (remote authorization)."""
        agent_or_404(body.agent_id)
        card_id = next((c for c, a in board().card_agents().items() if a == body.agent_id), None)
        mandate = board().warden.mandates.get(body.agent_id)
        if card_id is None or mandate is None or not mandate.allowed_mccs:
            raise HTTPException(400, f"{body.agent_id} has no card")
        mcc = next((m for m in TYPICAL_PURCHASE if m in mandate.allowed_mccs), sorted(mandate.allowed_mccs)[0])
        amount = max(Decimal("1"), (mandate.per_txn_cap * Decimal("0.4")).quantize(Decimal("1")))
        started = time.time()
        if board().services.charge_card_directly is None:  # offline: play Airwallex asking Warden
            answer = app.state.authorizer.decide({
                "version": 2, "card_id": card_id, "card_transaction_event_id": f"offline_{int(started * 1000)}",
                "transaction_type": "AUTHORIZATION", "transaction_amount": str(amount), "transaction_currency": "USD",
                "merchant": {"name": "STOLEN CARD TEST", "category_code": mcc}})
            declined = answer["response_status"] == "DECLINED"
            network = {"result": "DECLINED" if declined else "APPROVED",
                       "failure_reason": "REMOTE_AUTH_DECLINED" if declined else None}
        else:
            network = board().services.charge_card_directly(card_id, amount, mcc)
        asked = next((e["data"] for e in reversed(board().warden.audit.entries())
                      if e["event"] == "remote_authorization" and e["data"].get("card_id") == card_id
                      and e["ts"] >= started - 1), None)
        return _jsonable({"agent_id": body.agent_id, "amount": amount, "merchant_category": mcc,
                          "airwallex": network.get("result"), "failure_reason": network.get("failure_reason"),
                          "warden": {k: asked.get(k) for k in ("response_status", "reason", "decided_in_ms")} if asked else None})

    @app.post("/api/normal-day")
    def run_normal_day(body: AgentBody) -> dict[str, Any]:
        agent_or_404(body.agent_id)
        day = normal_day(body.agent_id, board().warden.mandates.get(body.agent_id))
        if not day:
            raise HTTPException(400, f"{body.agent_id} has no everyday purchases Warden can simulate")
        return _jsonable({"actions": [board().warden.submit(p).action for p in day]})

    @app.post("/api/approve")
    def approve(body: ApproveBody) -> dict[str, Any]:
        try:
            decision = board().warden.approve(body.proposal_id, body.approval_hash, approver="finance@acme (dashboard)")
        except ApprovalError as e:
            raise HTTPException(409, str(e)) from None
        return _jsonable({"action": decision.action, "execution": decision.execution})

    @app.post("/api/deny")
    def deny(body: ProposalBody) -> dict[str, Any]:
        try:
            decision = board().warden.deny(body.proposal_id, approver="finance@acme (dashboard)")
        except ApprovalError as e:
            raise HTTPException(409, str(e)) from None
        return _jsonable({"action": decision.action})

    @app.post("/api/tamper")
    def tamper(body: ProposalBody) -> JSONResponse:
        """Approve a copy with the amount bumped by $1: the hash no longer matches, so nothing runs."""
        p = board().warden.pending.get(body.proposal_id)
        if p is None:
            raise HTTPException(404, "No pending proposal with that id")
        changed = replace(p, amount=p.amount + Decimal("1"))
        try:
            board().warden.approve(p.id, approvals.approval_hash(changed), approver="tamper test")
        except ApprovalError as e:
            return JSONResponse(status_code=409, content={"refused": True, "reason": str(e),
                                                          "tried_amount": str(changed.amount)})
        raise HTTPException(500, "A changed proposal was approved; this must never happen")

    # Recover and locks ------------------------------------------------------------------
    @app.post("/api/reconcile")
    def reconcile() -> dict[str, Any]:
        return _jsonable({"cases": board().services.reconcile(board().warden)})

    @app.post("/api/locks")
    def test_locks() -> dict[str, Any]:
        if board().services.test_locks is None:
            raise HTTPException(400, "Live locks are tested by the demo run (python -m warden.demo --live)")
        if not board().warden.trust.is_contained(w.PROCUREMENT):
            # Two of the locks are what firing put in place (frozen card, revoked mandate): nothing to test yet.
            raise HTTPException(409, "Fire procurement-bot first (its Fire agent button, or keep sending it scam emails until it is contained): the locks test "
                                     "what firing it put in place, with Warden switched off.")
        return _jsonable(board().services.test_locks(board().warden))

    # Housekeeping -----------------------------------------------------------------------
    @app.post("/api/reset")
    def reset() -> dict[str, Any]:
        if app.state.factory is None:
            raise HTTPException(400, "Reset is only available for the offline demo")
        new_warden, new_services = app.state.factory()
        app.state.board = Dashboard(new_warden, new_services)
        app.state.drafts = {}
        app.state.attack_turns = {}
        return {"ok": True}

    @app.get("/api/verify")
    def verify() -> dict[str, Any]:
        broken = board().warden.audit.verify()
        return {"intact": broken is None, "broken_at": broken, "entries": len(board().warden.audit.entries())}

    @app.get("/api/scorecard")
    def scorecard() -> dict[str, Any]:
        if app.state.scorecard is None:  # offline gauntlet: fresh simulated Wardens, never the live one
            results = gauntlet.run()
            text, ok = gauntlet.scorecard(results)
            app.state.scorecard = {
                "summary": text.strip().splitlines()[-1], "ok": ok,
                "rows": [{"name": s.name, "attack": s.attack, "action": str(d.action),
                          "codes": d.violations + [f for f in d.risk.flags if f not in d.violations]}
                         for s, d in results],
            }
            red = _read_json(Path(__file__).resolve().parents[2] / "docs" / "redteam" / "scoreboard.json")
            app.state.scorecard["redteam"] = (red or {}).get("summary")
        return app.state.scorecard

    @app.get("/api/stream")
    async def stream(request: Request, once: bool = False) -> StreamingResponse:
        """Server-sent events: one 'audit' event whenever the log grows (or resets)."""

        async def events():
            seen, idle = None, 0.0
            while True:
                # The log, plus any Airi passkey request that opened or closed since (it logs nothing itself).
                passkey = board().services.passkey(board().warden)
                count = (len(board().warden.audit.entries()), (passkey or {}).get("mandate_id"))
                if count != seen:
                    seen, idle = count, 0.0
                    yield f"event: audit\ndata: {json.dumps({'count': count[0]})}\n\n"
                    if once:
                        return
                elif idle >= 15:
                    idle = 0.0
                    yield ": keep-alive\n\n"
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.4)
                idle += 0.4

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
