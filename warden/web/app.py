"""Warden's dashboard: one story for a finance team (and a judge). Hire it, guard it, prove it, recover.

Everything on the page is derived from the audit log, the signed receipt book and the case
file, so the screen can never claim something those records do not hold. The mode-specific
work (drafting with Claude, provisioning real cards, reconciling real charges) sits behind
`Services`, so the offline demo and the live sandbox render through the same code.
"""

from __future__ import annotations

import asyncio
import json
import random
import tempfile
import time
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
from warden.engine import ApprovalError, Warden
from warden.models import Proposal
from warden.rails.fake import FakeRail
from warden.web.verify import create_verify_router

HERE = Path(__file__).resolve().parent
WEEK = 7 * 24 * 3600
TAGLINE = "Warden doesn't ask you to trust your AI agents. It makes that trust enforceable, revocable, and verifiable."

# Rail kinds grouped the way a finance team thinks about them.
RAIL_GROUPS = [
    ("airwallex", "Airwallex", ("transfer", "card")),
    ("airi", "Visa VIC · Airi", ("vic_checkout",)),
    ("metal", "Metal x402", ("x402",)),
]
DECISION_EVENTS = ("decision", "approval", "denial")
CASE_PIPELINE = ["UNATTRIBUTED", "INVESTIGATING", "DISPUTE_SUBMITTED", "RECOVERED"]


class ApproveBody(BaseModel):
    proposal_id: str
    approval_hash: str


class ProposalBody(BaseModel):
    proposal_id: str


class AgentBody(BaseModel):
    agent_id: str = w.PROCUREMENT


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
            {"title": "Data payment under procurement-bot's Metal mandate", "lock": "Metal on-chain mandate",
             "expected": "mandate_revoked", "got": "mandate_revoked" if frozen else "PAID (mandate still bound)"},
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
        results = warden.contain(agent_id, reason)  # freezes its card and revokes its Metal mandate
        if agent_id in lw.registry.agents:
            lw.registry.fire(agent_id)
        return results

    def cases() -> list[dict[str, Any]]:
        return [c.to_json() for c in rv.load_cases()]  # the whole case file, including earlier agents' cards

    def locks(warden: Warden) -> dict[str, Any]:
        evidence = _read_json(live.ROOT / "data" / "evidence.json") or {}
        results = [{"title": p.get("title"), "expected": p.get("expected"), "held": p.get("held"),
                    "got": str((p.get("result") or {}).get("failure_reason") or (p.get("result") or {}).get("reason")
                               or (p.get("result") or {}).get("result")),
                    "ids": {k: v for k, v in (p.get("result") or {}).items()
                            if k in ("card_id", "card_transaction_id", "credential", "transaction") and v}}
                   for p in evidence.get("independent_locks", [])]
        return {"results": results, "source": f"live demo run {evidence.get('ran_at', '')}".strip()}

    return Services(
        mode="live", receipts_path=rc.RECEIPTS_PATH, public_key_path=rc.PUBLIC_KEY_PATH,
        sync_receipts=lambda warden: live.issue_receipts(lw) and None,
        draft=draft, commit_hire=commit_hire, fire=fire,
        reconcile=lambda warden: [c.to_json() for c in live.reconcile_and_recover(lw)],
        cases=cases, locks=locks, audit_path=lw.warden.audit.path,
        extra={"airi": "configured" if lw.airi else "waiting for allowlist"},
    )


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
        proposals: dict[str, dict[str, Any]] = {}
        order: list[str] = []
        history: dict[str, list[dict[str, Any]]] = {}
        rejected: dict[str, list[str]] = {}
        for entry in self.warden.audit.entries():
            event, data = entry["event"], entry["data"]
            if event == "proposal_received":
                pid = data["proposal"]["id"]
                proposals[pid] = data["proposal"]
                order.append(pid)
            elif event in DECISION_EVENTS:
                history.setdefault(data["proposal_id"], []).append(
                    {**data, "event": event, "seq": entry["seq"], "ts": entry["ts"],
                     "hash": entry["hash"], "prev_hash": entry["prev_hash"]})
            elif event == "approval_rejected":
                rejected.setdefault(data["proposal_id"], []).append(data.get("reason", ""))
        return [{"proposal": proposals[pid], "decision": (history.get(pid) or [None])[-1],
                 "history": history.get(pid, []), "pending": pid in self.warden.pending,
                 "rejected_approvals": rejected.get(pid, [])} for pid in reversed(order)]

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

    def state(self) -> dict[str, Any]:
        entries = self.warden.audit.entries()
        broken = self.warden.audit.verify()
        return _jsonable({
            "mode": self.mode,
            "tagline": TAGLINE,
            "agents": self.agents(),
            "rails": self.rails(),
            "feed": self.feed(),
            "inbox": self.inbox(),
            "receipts": self.receipts(),
            "cases": {"pipeline": CASE_PIPELINE, "items": self.services.cases()},
            "locks": self.services.locks(self.warden),
            "audit": {"count": len(entries), "intact": broken is None, "broken_at": broken,
                      "recent": [{k: e[k] for k in ("seq", "ts", "event", "prev_hash", "hash")}
                                 for e in entries[-40:]][::-1]},
        })


# --- scenarios the buttons fire -----------------------------------------------------------

def attack_for(agent_id: str, pool: list[gauntlet.Scenario]) -> tuple[str, Proposal]:
    """A one-proposal attack aimed at this agent: its own scenarios first, else one retargeted at it."""
    own = [s for s in pool if s.steps()[0].agent_id == agent_id]
    scenario = random.choice(own or pool)
    proposal = scenario.steps()[0]
    if proposal.agent_id != agent_id:
        proposal = replace(proposal, agent_id=agent_id)
    return scenario.name, proposal


def single_step_attacks() -> list[gauntlet.Scenario]:
    """Attacks that are one proposal long, so one click is one attempt the audience can follow."""
    return [s for s in gauntlet.SCENARIOS if s.attack and len(s.steps()) == 1]


def normal_day(agent_id: str) -> list[Proposal]:
    """Each agent's ordinary work."""
    if agent_id == w.RESEARCH:
        return [w.data_api_call(), w.data_api_call()]
    return [w.invoice("Acme Office Supply", "420", "INV-1001"), w.invoice("Globex Paper Co", "260", "INV-1002"),
            w.supplies_card()]


# --- the app ------------------------------------------------------------------------------

def offline_factory() -> tuple[Warden, Services]:
    services = offline_services()
    return offline_warden(services.audit_path), services


def create_app(warden: Warden | None = None, services: Services | None = None,
               factory: Callable[[], tuple[Warden, Services]] | None = None) -> FastAPI:
    """With no arguments: the offline demo, with a Reset button. Live: pass the live Warden and services."""
    if warden is None or services is None:
        factory = factory or offline_factory
        warden, services = factory()

    app = FastAPI(title="Warden", docs_url=None, redoc_url=None)
    app.state.board = Dashboard(warden, services)
    app.state.factory = factory
    app.state.drafts = {}
    app.state.scorecard = None
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    app.include_router(create_verify_router(services.receipts_path, services.public_key_path, services.audit_path))

    def board() -> Dashboard:
        return app.state.board

    def agent_or_404(agent_id: str) -> None:
        if agent_id not in board().warden.mandates:
            raise HTTPException(404, f"No agent called {agent_id}")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html = (HERE / "templates" / "index.html").read_text(encoding="utf-8")
        initial = json.dumps(board().state()).replace("</", "<\\/")
        return html.replace("{{MODE}}", board().mode).replace("{{TAGLINE}}", TAGLINE) \
                   .replace("{{INITIAL_STATE}}", initial)

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
    def hire_confirm(body: AgentBody) -> dict[str, Any]:
        """Provisions exactly the mandate the manager just saw; nothing is re-drafted."""
        held = app.state.drafts.pop(body.agent_id, None)
        if held is None:
            raise HTTPException(404, "Draft a mandate first")
        role, instruction, draft, validated = held
        try:
            hired = board().services.commit_hire(board().warden, body.agent_id, role, instruction, draft, validated)
        except h.HireError as e:
            raise HTTPException(422, str(e)) from None
        return _jsonable({"agent_id": body.agent_id, "provisioning": hired.get("provisioning")})

    @app.post("/api/fire")
    def fire(body: FireBody) -> dict[str, Any]:
        agent_or_404(body.agent_id)
        return _jsonable({"agent_id": body.agent_id,
                          "results": board().services.fire(board().warden, body.agent_id, body.reason)})

    # Guard ------------------------------------------------------------------------------
    @app.post("/api/attack")
    def attack(body: AgentBody) -> dict[str, Any]:
        agent_or_404(body.agent_id)
        name, proposal = attack_for(body.agent_id, single_step_attacks())
        decision = board().warden.submit(proposal)
        return _jsonable({"scenario": name, "proposal_id": proposal.id, "action": decision.action})

    @app.post("/api/normal-day")
    def run_normal_day(body: AgentBody) -> dict[str, Any]:
        agent_or_404(body.agent_id)
        return _jsonable({"actions": [board().warden.submit(p).action for p in normal_day(body.agent_id)]})

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
        return _jsonable(board().services.test_locks(board().warden))

    # Housekeeping -----------------------------------------------------------------------
    @app.post("/api/reset")
    def reset() -> dict[str, Any]:
        if app.state.factory is None:
            raise HTTPException(400, "Reset is only available for the offline demo")
        new_warden, new_services = app.state.factory()
        app.state.board = Dashboard(new_warden, new_services)
        app.state.drafts = {}
        app.router.routes[:] = [r for r in app.router.routes if not getattr(r, "path", "").startswith("/verify")]
        app.include_router(create_verify_router(new_services.receipts_path, new_services.public_key_path,
                                                new_services.audit_path))
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
        return app.state.scorecard

    @app.get("/api/stream")
    async def stream(request: Request, once: bool = False) -> StreamingResponse:
        """Server-sent events: one 'audit' event whenever the log grows (or resets)."""

        async def events():
            seen, idle = -1, 0.0
            while True:
                count = len(board().warden.audit.entries())
                if count != seen:
                    seen, idle = count, 0.0
                    yield f"event: audit\ndata: {json.dumps({'count': count})}\n\n"
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
