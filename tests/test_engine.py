from dataclasses import replace

import pytest

from warden import demo_world as w
from warden.engine import ApprovalError
from warden.models import Action, RiskReport


class BlindReader:
    """A risk reader that sees nothing, to show what code alone would do."""

    def read(self, proposal):
        return RiskReport(flags=(), severity=0, explanation="", source="test")


def test_demo_story():
    warden, rails = w.offline_warden()

    clean = [w.invoice("Acme Office Supply", "420", "INV-1001", "req_inv_1001"),
             w.invoice("Globex Paper Co", "310", "INV-77"),
             w.invoice("Initech Cleaning", "150", "INV-64")]
    for p in clean:
        assert warden.submit(p).action is Action.ALLOW
    assert len(rails["transfer"].executed) == 3

    subtle = warden.submit(w.lookalike_invoice())
    assert subtle.action is Action.STEP_UP
    assert subtle.violations == [] and "LOOKALIKE_DOMAIN" in subtle.risk.flags  # only the risk reader caught it
    assert subtle.approval_hash and subtle.trust_after == subtle.trust_before == 1.0

    attack = warden.submit(w.fake_bank_change())
    assert attack.action is Action.BLOCK
    assert "PAYEE_NOT_ALLOWLISTED" in attack.violations
    assert attack.trust_after == attack.trust_before / 2

    strike_two = warden.submit(w.gift_card())
    assert strike_two.action is Action.CONTAIN
    assert all(rail.contained == [w.AGENT] for rail in rails.values())
    assert subtle.proposal_id not in warden.pending  # containment drops everything still waiting

    replay = warden.submit(w.invoice("Acme Office Supply", "420", "INV-1001", "req_inv_1001"))
    assert replay.action is Action.BLOCK and "DUPLICATE_REQUEST" in replay.violations
    assert len(rails["transfer"].executed) == 3  # nothing paid twice

    assert warden.audit.verify() is None
    events = [e["event"] for e in warden.audit.entries()]
    assert events.count("decision") == 7


def test_lookalike_needs_the_risk_reader():
    warden, _ = w.offline_warden(BlindReader())
    assert warden.submit(w.lookalike_invoice()).action is Action.ALLOW


def test_approval_runs_only_the_exact_proposal():
    warden, rails = w.offline_warden()
    decision = warden.submit(w.lookalike_invoice())

    with pytest.raises(ApprovalError):
        warden.approve(decision.proposal_id, "0" * 64)
    assert decision.proposal_id in warden.pending  # a bad approval doesn't consume the proposal

    approved = warden.approve(decision.proposal_id, decision.approval_hash)
    assert approved.action is Action.ALLOW and approved.execution["status"] == "SIMULATED"
    assert len(rails["transfer"].executed) == 1
    with pytest.raises(ApprovalError):
        warden.approve(decision.proposal_id, decision.approval_hash)  # one approval, one payment


def test_two_denials_contain_the_agent():
    warden, rails = w.offline_warden()
    first = warden.submit(w.lookalike_invoice())
    second = warden.submit(replace(w.lookalike_invoice("250"), request_id="req_second"))

    assert warden.deny(first.proposal_id).action is Action.BLOCK
    assert warden.deny(second.proposal_id).action is Action.CONTAIN
    assert rails["x402"].contained == [w.AGENT]
    assert warden.submit(w.supplies_card()).action is Action.BLOCK  # contained agents can't spend


def test_rail_decline_is_recorded_as_the_second_lock():
    warden, rails = w.offline_warden(BlindReader())
    warden.mandate = replace(warden.mandate, allowed_mccs=warden.mandate.allowed_mccs | {w.MCC_GIFT_SHOP})
    decision = warden.submit(w.gift_card())  # Warden's mandate is weakened; the card's own control still says no
    assert decision.action is Action.ALLOW
    assert decision.execution == {"rail": "airwallex-card", "status": "DECLINED_BY_RAIL",
                                  "reason": "MERCHANT_CATEGORY_NOT_ALLOWED", "simulated": True}
    assert rails["card"].executed == []
