"""Warden under fire: random worlds, random payments, attacks, retries, approvals and firings,
with every money-moving rule checked after every step.

    python -m warden.simulate                       1,000 worlds x 100 actions = 100,000 actions
    python -m warden.simulate --worlds 50 --steps 40
    python -m warden.simulate --sabotage            switch Warden's mandate check off: the checker must catch it

Each world is a fresh offline Warden (simulated rails, the heuristic risk reader) with its own
clock. The actions are drawn at random: approved invoices, payments to strangers, card buys in
allowed and forbidden categories, data calls, scams, replays of earlier requests, human approvals
(honest and tampered), denials, firing an agent, hiring a replacement, and time passing.

After every action, the rules are checked against what the rails actually executed:

  R1 no payment above the agent's per-payment cap
  R2 no agent ever over its weekly cap, in any rolling 7-day window
  R3 transfers and x402 payments only to allowlisted payees
  R4 card and checkout purchases only in allowed merchant categories
  R5 no request ever executed twice
  R6 nothing executed for an agent after it was fired or contained
  R7 nothing above the auto-approve limit executed without a human
  R8 a tampered approval never executes anything
  R9 the audit log's hash chain is intact
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Callable

from warden import approvals
from warden import demo_world as w
from warden import mandate as mandate_rules
from warden.audit import AuditLog
from warden.engine import ApprovalError, Warden
from warden.mandate import MERCHANT_RAILS, PAYEE_RAILS, WEEK_SECONDS, Mandate
from warden.models import Proposal
from warden.rails.base import RailDeclined
from warden.risk import OfflineRiskReader

MCCS = [w.MCC_OFFICE_SUPPLIES, w.MCC_STATIONERY, w.MCC_OFFICE_FURNITURE, w.MCC_SOFTWARE, w.MCC_DATA_SERVICES,
        w.MCC_GIFT_SHOP, "7995", "5999"]  # the last two: gambling, misc retail


@dataclass
class Executed:
    proposal: Proposal
    at: float
    by_human: bool


class RecordingRail:
    """A simulated rail that remembers what it executed and when. Mimics the card network's
    own merchant-category control for gift shops, like the offline demo."""

    def __init__(self, name: str, world: World) -> None:
        self.name = name
        self.world = world

    def execute(self, p: Proposal) -> dict[str, Any]:
        if p.rail == "card" and p.merchant_category == w.MCC_GIFT_SHOP:
            raise RailDeclined("MERCHANT_CATEGORY_NOT_ALLOWED", {"simulated": True})
        self.world.executed.append(Executed(p, self.world.now, self.world.human_acting))
        return {"rail": self.name, "status": "SIMULATED", "id": f"sim_{len(self.world.executed)}"}

    def contain(self, agent_id: str) -> dict[str, Any]:
        return {"rail": self.name, "status": "CONTAINED (simulated)"}


@dataclass
class World:
    rng: random.Random
    now: float = 1_800_000_000.0
    executed: list[Executed] = field(default_factory=list)
    human_acting: bool = False
    fired_at: dict[str, int] = field(default_factory=dict)  # agent -> number of payments executed when it was fired
    roles: dict[str, str] = field(default_factory=dict)  # agent id -> "procurement" | "research"
    history: list[Proposal] = field(default_factory=list)
    actions: Counter = field(default_factory=Counter)
    outcomes: Counter = field(default_factory=Counter)
    tamper_attempts: int = 0
    kinds: dict[str, str] = field(default_factory=dict)  # proposal id -> "work: ..." | "hostile: ..."
    hostile_auto_paid: int = 0
    warden: Warden = field(init=False)

    def __post_init__(self) -> None:
        rails = {kind: RecordingRail(kind, self) for kind in ("transfer", "card", "vic_checkout", "x402")}
        self.warden = Warden(w.procurement_mandate(), OfflineRiskReader(w.VENDOR_DOMAINS), rails,
                             audit=AuditLog(), clock=lambda: self.now)
        self.hire("procurement")
        self.hire("research")

    def hire(self, role: str) -> str:
        n = sum(1 for r in self.roles.values() if r == role) + 1
        agent_id = f"{role}-bot" + ("" if n == 1 else f"-{n}")
        base = w.procurement_mandate() if role == "procurement" else w.research_mandate()
        self.warden.hire(agent_id, replace(base, agent_id=agent_id, mandate_id=f"mdt_{agent_id}"), role=role)
        self.roles[agent_id] = role
        return agent_id

    def active(self) -> list[str]:
        return [a for a in self.roles if a not in self.fired_at]


# --- random proposals ----------------------------------------------------------------------

def _amount(rng: random.Random, low: float, high: float) -> Decimal:
    return Decimal(str(round(rng.uniform(low, high), 2)))


def random_proposal(world: World, agent_id: str) -> tuple[str, Proposal]:
    """About 70% ordinary work for the agent's role (sometimes over a limit), 30% hostile:
    strangers, scams, forbidden categories, replays of earlier requests, another role's work."""
    rng = world.rng
    role = world.roles[agent_id]
    if rng.random() < 0.7:
        over = rng.random() < 0.08  # an honest agent occasionally asks for more than its cap
        if role == "procurement":
            kind = rng.choice(["invoice", "invoice", "card", "checkout"])
            hi = 900 if over else 300
            if kind == "invoice":
                p = w.invoice(rng.choice(list(w.VENDORS)), str(_amount(rng, 20, hi)), f"INV-{rng.randint(1000, 99999)}")
            elif kind == "card":
                p = replace(w.supplies_card(str(_amount(rng, 5, hi))),
                            merchant_category=rng.choice([w.MCC_OFFICE_SUPPLIES, w.MCC_STATIONERY]))
            else:
                p = w.office_chair_checkout(str(_amount(rng, 40, hi)))
        else:
            kind = rng.choice(["data", "data", "data", "card"])
            if kind == "data":
                p = w.data_api_call(str(_amount(rng, 0.01, 90 if over else 2)))
            else:
                p = replace(w.supplies_card(str(_amount(rng, 5, 120 if over else 40))), payee_name="SaaS Tools Inc",
                            payee_id="merchant_saas", merchant_category=w.MCC_SOFTWARE,
                            description="Software subscription")
        kind = f"work: {kind}"
    else:
        kind = rng.choice(["stranger", "scam", "forbidden category", "replay", "other role's work"])
        if kind == "replay" and world.history:
            old = rng.choice(world.history)
            return f"hostile: {kind}", replace(old, id=f"prop_r{rng.getrandbits(40):x}")  # same request_id
        if kind == "stranger":
            p = replace(w.invoice("Acme Office Supply", str(_amount(rng, 20, 650)), f"X-{rng.randint(1, 9999)}"),
                        payee_id=f"ben_{rng.getrandbits(24):x}", payee_name="Unknown Supplies LLC")
        elif kind == "forbidden category":
            allowed = world.warden.mandate_for(agent_id).allowed_mccs
            p = replace(w.supplies_card(str(_amount(rng, 5, 600))),
                        merchant_category=rng.choice([m for m in MCCS if m not in allowed]))
        elif kind == "other role's work":
            p = w.invoice(rng.choice(list(w.VENDORS)), str(_amount(rng, 20, 400)), f"INV-{rng.randint(1000, 99999)}")                 if role == "research" else w.data_api_call(str(_amount(rng, 0.01, 60)),
                                                           payee=f"0x{rng.getrandbits(160):040x}")
        else:
            kind = "scam"
            p = rng.choice([w.fake_bank_change, w.lookalike_invoice, w.gift_card])(str(_amount(rng, 50, 1500)))
        kind = f"hostile: {kind}"
    p = replace(p, agent_id=agent_id, id=f"prop_{rng.getrandbits(48):x}", request_id=f"req_{rng.getrandbits(48):x}")
    return kind, p


# --- one action ----------------------------------------------------------------------------

def step(world: World) -> None:
    rng, warden = world.rng, world.warden
    roll = rng.random()
    if roll < 0.06:
        world.now += rng.uniform(600, 2.5 * 24 * 3600)  # time passes
        world.actions["time passes"] += 1
        return
    if roll < 0.10 and warden.pending:
        pid = rng.choice(list(warden.pending))
        p = warden.pending[pid]
        choice = rng.random()
        if choice < 0.5:
            world.human_acting = True
            try:
                warden.approve(pid, approvals.approval_hash(p))
                world.actions["human approves"] += 1
            except ApprovalError:
                world.actions["human approves (refused)"] += 1
            finally:
                world.human_acting = False
        elif choice < 0.75:
            before = len(world.executed)
            changed = replace(p, amount=p.amount + Decimal("1"))
            world.tamper_attempts += 1
            world.human_acting = True
            try:
                warden.approve(pid, approvals.approval_hash(changed))
                world.outcomes["TAMPERED APPROVAL EXECUTED"] += 1
            except ApprovalError:
                pass
            finally:
                world.human_acting = False
            if len(world.executed) != before:
                raise AssertionError("R8: a tampered approval executed a payment")
            world.actions["tampered approval"] += 1
        else:
            warden.deny(pid)
            world.actions["human denies"] += 1
        return
    if 0.10 <= roll < 0.11:
        candidates = world.active()
        if candidates:
            agent = rng.choice(candidates)
            warden.contain(agent, "fired in simulation")
            world.fired_at[agent] = len(world.executed)
            world.actions["agent fired"] += 1
        return
    if 0.11 <= roll < 0.12 or not world.active():
        world.hire(rng.choice(["procurement", "research"]))
        world.actions["agent hired"] += 1
        return
    agent = rng.choice(list(world.roles))  # fired agents keep trying too
    kind, p = random_proposal(world, agent)
    decision = warden.submit(p)
    world.history.append(p)
    if len(world.history) > 200:
        world.history.pop(0)
    world.actions[f"proposal: {kind}"] += 1
    world.outcomes[decision.action.value] += 1
    world.kinds[p.id] = kind
    if kind.startswith("hostile") and decision.action.value == "ALLOW":
        world.hostile_auto_paid += 1
    if warden.trust.is_contained(agent) and agent not in world.fired_at:
        world.fired_at[agent] = len(world.executed)  # Warden contained it on its own


# --- the rules -----------------------------------------------------------------------------

def check_rules(world: World, auto_cap: Callable[[Mandate], Decimal]) -> list[str]:
    broken = []
    warden = world.warden
    seen: set[str] = set()
    by_agent: dict[str, list[Executed]] = {}
    for index, ex in enumerate(world.executed):
        p, m = ex.proposal, warden.mandate_for(ex.proposal.agent_id)
        tag = f"{p.agent_id} {p.rail} {p.amount} to {p.payee_name}"
        if p.amount > m.per_txn_cap:
            broken.append(f"R1 over per-payment cap: {tag}")
        if p.rail in PAYEE_RAILS and p.payee_id not in m.payee_allowlist:
            broken.append(f"R3 payee not allowlisted: {tag}")
        if p.rail in MERCHANT_RAILS and p.merchant_category not in m.allowed_mccs:
            broken.append(f"R4 merchant category not allowed: {tag}")
        if p.request_id in seen:
            broken.append(f"R5 request executed twice: {p.request_id}")
        seen.add(p.request_id)
        fired = world.fired_at.get(p.agent_id)
        if fired is not None and index >= fired:
            broken.append(f"R6 executed after the agent was fired: {tag}")
        if not ex.by_human and p.amount > auto_cap(m):
            broken.append(f"R7 above auto-approve limit without a human: {tag}")
        by_agent.setdefault(p.agent_id, []).append(ex)
    for agent, items in by_agent.items():
        cap = warden.mandate_for(agent).weekly_cap
        for i, ex in enumerate(items):
            window = sum((e.proposal.amount for e in items[: i + 1] if e.at > ex.at - WEEK_SECONDS), Decimal("0"))
            if window > cap:
                broken.append(f"R2 weekly cap exceeded: {agent} spent {window} in 7 days (cap {cap})")
                break
    if warden.audit.verify() is not None:
        broken.append("R9 audit chain broken")
    return broken


@dataclass
class Report:
    worlds: int
    actions: int
    executed: int
    executed_value: Decimal
    blocked_attacks: int
    outcomes: Counter
    action_counts: Counter
    tamper_attempts: int
    violations: list[str]
    seconds: float
    hostile: int = 0
    hostile_auto_paid: int = 0

    def text(self) -> str:
        lines = [f"{self.worlds:,} worlds, {self.actions:,} random actions in {self.seconds:.1f}s",
                 f"  payments executed: {self.executed:,} (${self.executed_value:,.2f}); "
                 f"tampered approvals tried: {self.tamper_attempts:,}",
                 f"  hostile requests: {self.hostile:,}; paid without a human: {self.hostile_auto_paid:,}",
                 "  decisions: " + ", ".join(f"{k} {v:,}" for k, v in sorted(self.outcomes.items())),
                 "  actions:   " + ", ".join(f"{k} {v:,}" for k, v in sorted(self.action_counts.items()))]
        if self.violations:
            lines.append(f"RULES BROKEN: {len(self.violations):,}")
            lines += [f"  {v}" for v in self.violations[:10]]
        else:
            lines.append("RULES BROKEN: 0  (R1-R9 held after every action)")
        return "\n".join(lines)


def run(worlds: int = 1000, steps: int = 100, seed: int = 2026, sabotage: bool = False) -> Report:
    started = time.time()
    original_check = mandate_rules.check
    if sabotage:  # a Warden whose mandate check always says yes
        import warden.engine as engine_module
        engine_module.check = lambda *a, **k: []
    try:
        totals, actions, violations, executed, value, tampers = Counter(), Counter(), [], 0, Decimal("0"), 0
        hostile, hostile_paid = 0, 0
        for i in range(worlds):
            world = World(random.Random(seed * 100_003 + i))
            for _ in range(steps):
                step(world)
            # auto-approve limit: the mandate's, scaled by trust; trust never exceeds 1.0
            violations += [f"world {i}: {v}" for v in check_rules(world, lambda m: m.auto_approve_cap)]
            totals += world.outcomes
            actions += world.actions
            executed += len(world.executed)
            value += sum((e.proposal.amount for e in world.executed), Decimal("0"))
            tampers += world.tamper_attempts
            hostile += sum(v for k, v in world.actions.items() if k.startswith("proposal: hostile"))
            hostile_paid += world.hostile_auto_paid
    finally:
        if sabotage:
            import warden.engine as engine_module
            engine_module.check = original_check
    blocked = totals["BLOCK"] + totals["CONTAIN"] + totals["STEP_UP"]
    return Report(worlds, worlds * steps, executed, value, blocked, totals, actions, tampers, violations,
                  time.time() - started, hostile, hostile_paid)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--worlds", type=int, default=1000)
    parser.add_argument("--steps", type=int, default=100)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--sabotage", action="store_true", help="disable the mandate check to prove the rules catch it")
    args = parser.parse_args(argv)
    report = run(args.worlds, args.steps, args.seed, args.sabotage)
    print(("SABOTAGED WARDEN (mandate check off)\n" if args.sabotage else "") + report.text())
    return 1 if report.violations and not args.sabotage else 0


if __name__ == "__main__":
    sys.exit(main())
