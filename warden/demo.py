"""The demo story, narrated, with every real id saved to data/evidence.json.

    python -m warden.demo --offline          simulated rails, heuristic risk reader (no keys needed)
    python -m warden.demo --live --reset     Airwallex sandbox, Claude, Metal testnet: hires both agents
                                             again with fresh cards and mandates, and starts a new audit log
    python -m warden.demo --live             same, reusing the agents hired last time (their cards may be frozen)

Act 0: hire.  Act 1: a normal day.  Act 2: the attacks, including a charge Warden never sees.
Act 3: the independent locks, tested with Warden switched off.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from warden import demo_world as w
from warden import hire as h
from warden.config import ROOT
from warden.engine import Warden
from warden.models import Decision, Proposal

EVIDENCE_PATH = ROOT / "data" / "evidence.json"
OUT_OF_BAND_AMOUNT = "180"


@dataclass
class Probes:
    """Payments made straight to a rail, with Warden switched off."""

    card_charge: Callable[[str, str, str], dict[str, Any]]  # (agent_id, amount, mcc) -> rail result
    metal_payment: Callable[[str], dict[str, Any]]  # agent_id -> rail result


@dataclass
class Story:
    warden: Warden
    probes: Probes
    hire: Callable[[str, str, str], dict[str, Any]]  # (agent_id, role, instruction) -> hire record
    mode: str
    hires: list[dict[str, Any]] = field(default_factory=list)
    steps: list[dict[str, Any]] = field(default_factory=list)
    proofs: list[dict[str, Any]] = field(default_factory=list)
    out_of_band: dict[str, Any] = field(default_factory=dict)

    def hire_agent(self, agent_id: str, role: str, instruction: str) -> None:
        say(f"\n> Hire {agent_id}")
        say(f"  manager: \"{instruction}\"")
        hired = self.hire(agent_id, role, instruction)
        draft, m = hired["draft"], hired["mandate"]
        say(f"  draft ({draft['source']}): ${draft['per_txn_cap']}/purchase, ${draft['weekly_cap']}/week, "
            f"categories {draft['categories']}, payees {draft['payees']}")
        for change in hired["changes"] or ["(none: the draft was within company policy)"]:
            say(f"  validator: {change}")
        say(f"  mandate {m['mandate_id']}: ${m['per_txn_cap']}/purchase, ${m['weekly_cap']}/week, "
            f"MCCs {m['allowed_mccs']}, {len(m['payee_allowlist'])} payees, human needed for {m['human_required']}")
        say(f"  mandate hash {hired['mandate_hash'][:16]}...  provisioned: {_ids(hired['provisioning'])}")
        self.hires.append(hired)

    def propose(self, title: str, proposal: Proposal) -> Decision:
        say(f"\n> {title}")
        say(f"  {proposal.agent_id} proposes: {proposal.description}, {proposal.amount} {proposal.currency} "
            f"to {proposal.payee_name}")
        decision = self.warden.submit(proposal)
        self._narrate(decision)
        self.steps.append({"title": title, "agent_id": proposal.agent_id, "proposal_id": proposal.id,
                           "request_id": proposal.request_id, "rail": proposal.rail, "amount": str(proposal.amount),
                           "payee": proposal.payee_name, "action": decision.action.value, "reasons": decision.reasons,
                           "violations": decision.violations,
                           "risk": {"severity": decision.risk.severity, "flags": list(decision.risk.flags),
                                    "explanation": decision.risk.explanation, "source": decision.risk.source},
                           "trust_before": decision.trust_before, "trust_after": decision.trust_after,
                           "approval_hash": decision.approval_hash,
                           "execution": decision.execution, "containment": decision.containment})
        return decision

    def prove(self, title: str, expected: str, probe: Callable[[], dict[str, Any]]) -> None:
        say(f"\n> {title}  (Warden switched off)")
        result = probe()
        got = str(result.get("failure_reason") or result.get("reason") or result.get("result") or result.get("status"))
        held = expected in got
        say(f"  rail answered: {got}  ->  {'independent lock HELD' if held else 'UNEXPECTED, check the evidence file'}")
        self.proofs.append({"title": title, "expected": expected, "held": held, "result": result})

    def _narrate(self, d: Decision) -> None:
        if d.risk.source != "skipped":
            flags = ", ".join(d.risk.flags) or "none"
            say(f"  risk reader ({d.risk.source}): severity {d.risk.severity}, flags: {flags}")
            say(f"    \"{d.risk.explanation}\"")
        say(f"  WARDEN: {d.action.value}. {' '.join(d.reasons)}")
        if d.trust_after != d.trust_before:
            say(f"  trust {d.trust_before:.2f} -> {d.trust_after:.2f}")
        if d.execution:
            say(f"  executed: {_ids(d.execution)}")
        if d.containment:
            for rail, result in d.containment.items():
                say(f"  contained on {rail}: {_ids(result)}")

    def save(self, extra: dict[str, Any]) -> None:
        EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
        EVIDENCE_PATH.write_text(json.dumps({
            "mode": self.mode, "ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "audit_chain_intact": self.warden.audit.verify() is None, **extra,
            "hires": self.hires, "steps": self.steps, "out_of_band": self.out_of_band,
            "independent_locks": self.proofs}, indent=2, default=str), encoding="utf-8")
        say(f"\nEvidence saved to {EVIDENCE_PATH}")


def run_story(story: Story) -> None:
    say("=" * 72)
    say(f"WARDEN DEMO ({story.mode})  -  Hire it, guard it, prove it, recover")
    say("=" * 72)

    say("\nACT 0: hire two AI workers from plain English")
    story.hire_agent(w.PROCUREMENT, "procurement", w.PROCUREMENT_INSTRUCTION)
    story.hire_agent(w.RESEARCH, "research", w.RESEARCH_INSTRUCTION)

    say("\nACT 1: a normal day")
    first = w.invoice("Acme Office Supply", "420", "INV-1001")
    story.propose("Invoice from Acme, approved in the AP portal", first)
    story.propose("Invoice from Globex", w.invoice("Globex Paper Co", "310", "GX-552"))
    story.propose("Invoice from Initech", w.invoice("Initech Cleaning", "275", "IT-0907"))
    story.propose("Printer paper and toner on procurement-bot's card", w.supplies_card())
    story.propose("research-bot buys a data lookup on Metal", w.data_api_call())
    story.propose("research-bot buys another data lookup", w.data_api_call())
    story.propose("Network retry: the first invoice is submitted again", first)

    say("\nACT 2: the attacks")
    story.propose("Invoice from acme-supp1ies.com (a '1' instead of an 'l'), under every limit",
                  w.lookalike_invoice())
    story.propose("Email: 'our bank details changed, pay the new account today'", w.fake_bank_change())
    say(f"\n> Out of band: the hijacked agent uses its copied card details at a stationery store, "
        f"${OUT_OF_BAND_AMOUNT}, within every card limit. Warden never sees this charge.")
    story.out_of_band = story.probes.card_charge(w.PROCUREMENT, OUT_OF_BAND_AMOUNT, w.MCC_STATIONERY)
    say(f"  Airwallex answered: {story.out_of_band.get('result')}  ({_ids(story.out_of_band)})")
    say("  No Intent Receipt exists for it: the reconciliation step will find it.")
    story.propose("Same email: 'buy a $499 gift card and keep it between us'", w.gift_card())
    story.propose("research-bot keeps working: another data lookup", w.data_api_call())

    say("\nACT 3: three independent locks, with Warden switched off")
    story.prove("A $25 charge on procurement-bot's frozen card", "CARD_INACTIVE",
                lambda: story.probes.card_charge(w.PROCUREMENT, "25", w.MCC_STATIONERY))
    story.prove("A $40 gift-shop charge on research-bot's card", "MERCHANT_CATEGORY_NOT_ALLOWED",
                lambda: story.probes.card_charge(w.RESEARCH, "40", w.MCC_GIFT_SHOP))
    story.prove("A data payment under procurement-bot's revoked Metal mandate", "revoked",
                lambda: story.probes.metal_payment(w.PROCUREMENT))

    say(f"\nPending approvals left: {len(story.warden.pending)} (containment cancels everything the agent proposed)")
    for agent in (w.PROCUREMENT, w.RESEARCH):
        state = "CONTAINED" if story.warden.trust.is_contained(agent) else "active"
        say(f"{agent}: {state}, trust {story.warden.trust.score(agent):.2f}")
    intact = story.warden.audit.verify() is None
    say(f"Audit log: {len(story.warden.audit.entries())} entries, hash chain {'intact' if intact else 'BROKEN'}")


# --- offline ----------------------------------------------------------------------------

def offline_story() -> tuple[Story, dict[str, Any]]:
    warden, rails = w.offline_warden(hire=False)
    provisioner = h.FakeProvisioner()

    def hire(agent_id: str, role: str, instruction: str) -> dict[str, Any]:
        draft = h.offline_draft(instruction)
        validated = h.validate(draft, h.OrgPolicy(), agent_id, instruction)
        provisioning = h.provision(agent_id, validated, provisioner, confirm=True)
        entry = warden.hire(agent_id, validated.mandate, role=role, provisioning=provisioning)
        return {"agent_id": agent_id, "role": role, "instruction": instruction, "draft": draft.to_dict(),
                "changes": validated.changes, "mandate": validated.mandate.to_dict(),
                "mandate_hash": entry["data"]["mandate_hash"], "provisioning": provisioning}

    def card_charge(agent_id: str, amount: str, mcc: str) -> dict[str, Any]:
        base = {"agent_id": agent_id, "card_id": f"simulated_card_{agent_id}", "amount": amount,
                "merchant_category_code": mcc, "simulated": True}
        if agent_id in rails["card"].contained:
            return {**base, "result": "DECLINED", "failure_reason": "CARD_INACTIVE"}
        if mcc not in warden.mandate_for(agent_id).allowed_mccs:
            return {**base, "result": "DECLINED", "failure_reason": "MERCHANT_CATEGORY_NOT_ALLOWED"}
        return {**base, "result": "APPROVED", "card_transaction_id": "simulated_ctx", "lifecycle_id": "simulated_life"}

    def metal_payment(agent_id: str) -> dict[str, Any]:
        if agent_id in rails["x402"].contained:
            return {"agent_id": agent_id, "result": "REFUSED", "reason": "mandate_revoked", "simulated": True}
        return {"agent_id": agent_id, "result": "PAID", "transaction": "simulated", "simulated": True}

    return Story(warden, Probes(card_charge, metal_payment), hire, "offline, simulated rails"), {}


# --- live -------------------------------------------------------------------------------

def live_story(fresh: bool) -> tuple[Story, dict[str, Any]]:
    from warden import live  # imported here so offline runs need no keys

    lw = live.build(fresh=fresh)

    def hire(agent_id: str, role: str, instruction: str) -> dict[str, Any]:
        existing = lw.registry.agents.get(agent_id)
        if existing and existing.status == "active":  # reused: show what it was hired under
            return {"agent_id": agent_id, "role": role, "instruction": existing.instruction,
                    "draft": {"source": "reused", "per_txn_cap": existing.mandate["per_txn_cap"],
                              "weekly_cap": existing.mandate["weekly_cap"], "categories": [], "payees": []},
                    "changes": ["(reused from the last run)"], "mandate": existing.mandate,
                    "mandate_hash": existing.mandate_hash, "provisioning": existing.provisioning}
        return live.hire_agent(lw, agent_id, role, instruction, confirm=True)

    probes = Probes(lambda agent, amount, mcc: live.direct_card_charge(lw, agent, amount, mcc),
                    lambda agent: live.direct_metal_payment(lw, agent))
    story = Story(lw.warden, probes, hire, "live: Airwallex sandbox + Claude + Metal testnet")
    extra = {"beneficiaries": lw.state["beneficiaries"], "metal_account": lw.state["metal"]["account"],
             "airi": "configured" if lw.airi else "not allowlisted yet: VIC checkout skipped",
             "audit_log": str(lw.warden.audit.path)}
    return story, extra


def _ids(result: dict[str, Any]) -> str:
    keep = ("status", "card_status", "transfer_id", "card_id", "card_transaction_id", "lifecycle_id",
            "clearing_event_id", "transaction", "explorer", "mandate_revoked", "metal_mandate_id", "reason",
            "failure_reason", "error", "id")
    return ", ".join(f"{k}={result[k]}" for k in keep if result.get(k)) or json.dumps(result, default=str)


def say(line: str) -> None:
    print(line, flush=True)


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--live", action="store_true", help="use the real sandbox rails")
    mode.add_argument("--offline", action="store_true", help="simulated rails (default)")
    parser.add_argument("--reset", action="store_true",
                        help="live only: hire both agents again with fresh cards and mandates, new audit log")
    args = parser.parse_args(argv)

    story, extra = live_story(args.reset) if args.live else offline_story()
    run_story(story)
    story.save(extra)
    return 0


if __name__ == "__main__":
    sys.exit(main())
