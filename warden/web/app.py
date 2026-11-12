"""Warden's dashboard: what a finance team (and a judge) sees while agents spend.

Everything on the page is derived from the audit log plus the engine's live state,
so the screen can never claim something the log does not record.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from warden import approvals, demo_world as w, gauntlet
from warden.engine import ApprovalError, Warden
from warden.rails.fake import FakeRail

HERE = Path(__file__).resolve().parent

# Rail kinds grouped the way a finance team thinks about them.
RAIL_GROUPS = [
    ("airwallex", "Airwallex", ("transfer", "card")),
    ("airi", "Visa VIC · Airi", ("vic_checkout",)),
    ("metal", "Metal x402", ("x402",)),
]

DECISION_EVENTS = ("decision", "approval", "denial")


class ApproveBody(BaseModel):
    proposal_id: str
    approval_hash: str


class ProposalBody(BaseModel):
    proposal_id: str


class ContainBody(BaseModel):
    reason: str = "Kill switch pressed on the dashboard"


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str))


def evidence_hash(evidence: list[dict[str, Any]]) -> str:
    canonical = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Dashboard:
    """Reads the audit log into the shapes the page renders."""

    def __init__(self, warden: Warden, mode: str, agent_id: str = w.AGENT) -> None:
        self.warden = warden
        self.mode = mode
        self.agent_id = agent_id

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

        cards = []
        for pid in reversed(order):
            steps = history.get(pid, [])
            cards.append({
                "proposal": proposals[pid],
                "decision": steps[-1] if steps else None,
                "history": steps,
                "pending": pid in self.warden.pending,
                "rejected_approvals": rejected.get(pid, []),
            })
        return cards

    def inbox(self) -> list[dict[str, Any]]:
        items = []
        for pid, p in self.warden.pending.items():
            decision = self.warden.decisions.get(pid)
            items.append({
                "proposal": p.summary(),
                "approval_hash": approvals.approval_hash(p),
                "risk": _jsonable(decision.risk) if decision else None,
                "reasons": decision.reasons if decision else [],
            })
        return items

    def rails(self) -> list[dict[str, Any]]:
        contained = self.warden.trust.is_contained(self.agent_id)
        chips = []
        for key, label, kinds in RAIL_GROUPS:
            rails = [self.warden.rails[k] for k in kinds if k in self.warden.rails]
            if not rails:
                status = "offline"
            elif contained:
                status = "contained"
            elif all(isinstance(r, FakeRail) for r in rails):
                status = "simulated"
            else:
                status = "live"
            chips.append({"key": key, "label": label, "status": status})
        return chips

    def state(self) -> dict[str, Any]:
        entries = self.warden.audit.entries()
        broken = self.warden.audit.verify()
        return _jsonable({
            "mode": self.mode,
            "agent": {
                "id": self.agent_id,
                "trust": self.warden.trust.score(self.agent_id),
                "contained": self.warden.trust.is_contained(self.agent_id),
            },
            "rails": self.rails(),
            "feed": self.feed(),
            "inbox": self.inbox(),
            "audit": {
                "count": len(entries),
                "intact": broken is None,
                "broken_at": broken,
                "recent": [{k: e[k] for k in ("seq", "ts", "event", "prev_hash", "hash")} for e in entries[-40:]][::-1],
            },
        })

    def receipt(self, proposal_id: str) -> dict[str, Any]:
        """A self-contained record of one payment decision that anyone can check against the chain."""
        card = next((c for c in self.feed() if c["proposal"]["id"] == proposal_id), None)
        if card is None or card["decision"] is None:
            raise KeyError(proposal_id)
        d, p = card["decision"], card["proposal"]
        approver = next((r[len("Approved by "):].rstrip(".") for r in d["reasons"] if r.startswith("Approved by ")), None)
        return _jsonable({
            "receipt": "warden.trust_receipt.v1",
            "proposal": p,
            "decision": d["action"],
            "rules": {"violations": d["violations"], "reasons": d["reasons"]},
            "risk": d["risk"],
            "approver": approver,
            "approval_hash": d.get("approval_hash"),
            "evidence_hash": evidence_hash(p.get("evidence", [])),
            "execution": d.get("execution"),
            "containment": d.get("containment"),
            "trust": {"before": d["trust_before"], "after": d["trust_after"]},
            "audit_entry": {"seq": d["seq"], "prev_hash": d["prev_hash"], "hash": d["hash"]},
            "chain_intact": self.warden.audit.verify() is None,
        })


def _single_step_attacks() -> list[gauntlet.Scenario]:
    """Attacks that are one proposal long, so one click is one attempt the audience can follow."""
    return [s for s in gauntlet.SCENARIOS if s.attack and len(s.steps()) == 1]


def normal_day() -> list:
    """The agent's ordinary work: approved invoices, supplies, a checkout and a data call."""
    return [
        w.invoice("Acme Office Supply", "420", "INV-1001"),
        w.invoice("Globex Paper Co", "260", "INV-1002"),
        w.invoice("Initech Cleaning", "180", "INV-1003"),
        w.supplies_card(),
        w.office_chair_checkout(),
        w.data_api_call(),
    ]


def offline_factory() -> Warden:
    warden, _ = w.offline_warden()
    return warden


def create_app(warden: Warden | None = None, mode: str = "offline",
               factory: Callable[[], Warden] | None = None) -> FastAPI:
    """`mode` is shown on the page ("offline" means simulated rails). `factory` enables Reset."""
    if warden is None:
        factory = factory or offline_factory
        warden = factory()

    app = FastAPI(title="Warden", docs_url=None, redoc_url=None)
    app.state.board = Dashboard(warden, mode)
    app.state.factory = factory
    app.state.attacks = []
    app.state.scorecard = None
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def board() -> Dashboard:
        return app.state.board

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        html = (HERE / "templates" / "index.html").read_text(encoding="utf-8")
        initial = json.dumps(board().state()).replace("</", "<\\/")
        return html.replace("{{MODE}}", mode).replace("{{INITIAL_STATE}}", initial)

    @app.get("/api/state")
    def state() -> dict[str, Any]:
        return board().state()

    @app.post("/api/attack")
    def attack() -> dict[str, Any]:
        if not app.state.attacks:  # cycle through every attack before repeating one
            app.state.attacks = _single_step_attacks()
            random.shuffle(app.state.attacks)
        scenario = app.state.attacks.pop()
        proposal = scenario.steps()[0]
        decision = board().warden.submit(proposal)
        return _jsonable({"scenario": scenario.name, "proposal_id": proposal.id, "action": decision.action})

    @app.post("/api/normal-day")
    def run_normal_day() -> dict[str, Any]:
        actions = [board().warden.submit(p).action for p in normal_day()]
        return _jsonable({"actions": actions})

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

    @app.post("/api/contain")
    def contain(body: ContainBody | None = None) -> dict[str, Any]:
        reason = body.reason if body else ContainBody().reason
        return _jsonable({"results": board().warden.contain(board().agent_id, reason)})

    @app.post("/api/reset")
    def reset() -> dict[str, Any]:
        if app.state.factory is None:
            raise HTTPException(400, "Reset is only available for the offline demo")
        app.state.board = Dashboard(app.state.factory(), mode)
        return {"ok": True}

    @app.get("/api/verify")
    def verify() -> dict[str, Any]:
        broken = board().warden.audit.verify()
        return {"intact": broken is None, "broken_at": broken, "entries": len(board().warden.audit.entries())}

    @app.get("/api/receipt/{proposal_id}")
    def receipt(proposal_id: str) -> JSONResponse:
        try:
            data = board().receipt(proposal_id)
        except KeyError:
            raise HTTPException(404, "No decision recorded for that proposal") from None
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="receipt-{proposal_id}.json"'})

    @app.get("/api/scorecard")
    def scorecard() -> dict[str, Any]:
        if app.state.scorecard is None:  # offline gauntlet: fresh simulated Wardens, never the live one
            results = gauntlet.run()
            text, ok = gauntlet.scorecard(results)
            app.state.scorecard = {
                "summary": text.strip().splitlines()[-1],
                "ok": ok,
                "rows": [{"name": s.name, "attack": s.attack, "action": str(d.action),
                          "codes": d.violations + [f for f in d.risk.flags if f not in d.violations]}
                         for s, d in results],
            }
        return app.state.scorecard

    @app.get("/api/stream")
    async def stream(request: Request, once: bool = False) -> StreamingResponse:
        """Server-sent events: one 'audit' event whenever the log grows (or resets)."""

        async def events():
            seen = -1
            idle = 0.0
            while True:
                count = len(board().warden.audit.entries())
                if count != seen:
                    seen = count
                    idle = 0.0
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
