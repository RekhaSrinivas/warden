"""Hiring: the model drafts, code validates, and nothing is provisioned without confirmation."""

from decimal import Decimal

import pytest

from warden import demo_world as w
from warden import hire as h


def test_offline_draft_reads_limits_categories_payees_and_human_rules():
    draft = h.offline_draft(w.PROCUREMENT_INSTRUCTION)
    assert (draft.per_txn_cap, draft.weekly_cap) == ("500", "2500")
    assert draft.categories == ["office supplies", "stationery", "office furniture"]
    assert "VendorCheck API" in draft.payees and "Acme Office Supply" in draft.payees
    assert draft.human_required == ["new bank details", "new payee"]


def test_negated_sentences_never_become_permissions():
    assert "gift cards" not in h.offline_draft(w.RESEARCH_INSTRUCTION).categories


def test_validator_clamps_and_drops_everything_beyond_policy():
    draft = h.MandateDraft(purpose="p", per_txn_cap=900, weekly_cap=5000, currencies=["USD", "EUR"],
                           categories=["office supplies", "gift cards", "crypto"],
                           payees=["Acme", "CheapPrint Ltd"], approval_threshold=200, human_required=["new payee"])
    v = h.validate(draft, h.OrgPolicy(), "procurement-bot")
    m = v.mandate
    assert (m.per_txn_cap, m.weekly_cap) == (Decimal("500.00"), Decimal("2000.00"))
    assert m.currencies == {"USD"} and m.allowed_mccs == {w.MCC_OFFICE_SUPPLIES}
    assert m.payee_allowlist == {"ben_acme"} and m.approval_threshold == Decimal("200.00")
    assert m.auto_approve_cap == Decimal("200.00")
    joined = " ".join(v.changes)
    for phrase in ("capped at $500", "capped at $2000", "EUR", "'gift cards' is forbidden", "'crypto'",
                   "'CheapPrint Ltd' is not a verified vendor"):
        assert phrase in joined
    assert v.ok


def test_provisioning_needs_confirmation_and_a_valid_mandate():
    provisioner = h.FakeProvisioner()
    v = h.validate(h.offline_draft(w.RESEARCH_INSTRUCTION), h.OrgPolicy(), w.RESEARCH)
    with pytest.raises(h.HireError):
        h.provision(w.RESEARCH, v, provisioner, confirm=False)
    empty = h.validate(h.MandateDraft("p", 0, 0, ["USD"], [], []), h.OrgPolicy(), w.RESEARCH)
    with pytest.raises(h.HireError):
        h.provision(w.RESEARCH, empty, provisioner, confirm=True)
    assert provisioner.provisioned == []
    assert h.provision(w.RESEARCH, v, provisioner, confirm=True)["card_id"] == "simulated_card_research-bot"


class BrokenClient:
    class beta:
        class messages:
            @staticmethod
            def create(**kwargs):
                import anthropic
                import httpx
                raise anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com"))


def test_claude_failure_falls_back_to_the_deterministic_drafter():
    draft = h.draft_mandate(w.RESEARCH_INSTRUCTION, client=BrokenClient())
    assert draft.source == "offline" and draft.per_txn_cap == "50"


def test_the_console_example_travel_job_drafts_a_usable_mandate():
    # The placeholder in the console's hire box must always produce an agent that can be provisioned.
    html = open("warden/web/templates/index.html", encoding="utf-8").read()
    example = html.split('id="hire-text"', 1)[1].split('placeholder="', 1)[1].split('"', 1)[0]
    v = h.validate(h.offline_draft(example), h.OrgPolicy(), "travel-bot", example)
    assert v.ok, v.errors
    assert v.mandate.allowed_mccs == {"4511", "7011"}  # airlines, hotels
    assert (v.mandate.per_txn_cap, v.mandate.weekly_cap, v.mandate.approval_threshold) == \
        (Decimal("400.00"), Decimal("1500.00"), Decimal("300.00"))
