"""Per-agent mandates and containment, and the audit fields signed receipts are built from."""

from warden import demo_world as w
from warden.agents import AgentRecord, AgentRegistry
from warden.mandate import mandate_hash
from warden.models import Action


def test_containing_one_agent_leaves_the_other_working():
    warden, rails = w.offline_warden()
    assert warden.submit(w.fake_bank_change()).action is Action.BLOCK
    assert warden.submit(w.gift_card()).action is Action.CONTAIN
    assert all(rail.contained == [w.PROCUREMENT] for rail in rails.values())
    after = warden.submit(w.data_api_call())
    assert after.action is Action.ALLOW and after.execution["status"] == "SIMULATED"
    assert warden.submit(w.supplies_card()).action is Action.BLOCK  # procurement stays contained


def test_each_agent_is_held_to_its_own_mandate():
    warden, _ = w.offline_warden()
    big_data_call = w.data_api_call(amount="60")  # within procurement's $500, above research's $50
    assert "OVER_TXN_CAP" in warden.submit(big_data_call).violations
    assert warden.submit(w.data_api_call(amount="60", agent_id=w.PROCUREMENT)).action is Action.ALLOW


def test_decision_entries_carry_every_receipt_field():
    warden, _ = w.offline_warden()
    proposal = w.invoice("Acme Office Supply", "420", "INV-1001")
    warden.submit(proposal)
    hired = [e for e in warden.audit.entries() if e["event"] == "agent_hired"]
    assert [e["data"]["agent_id"] for e in hired] == [w.PROCUREMENT, w.RESEARCH]
    decision = [e for e in warden.audit.entries() if e["event"] == "decision"][-1]["data"]
    for key in ("agent_id", "mandate_id", "mandate_hash", "proposal_id", "rail", "amount", "currency", "payee_id",
                "payee_name", "request_id", "evidence_hash", "action", "reasons", "approval_hash", "approver",
                "execution"):
        assert key in decision
    assert decision["mandate_hash"] == mandate_hash(w.procurement_mandate()) == hired[0]["data"]["mandate_hash"]
    assert decision["evidence_hash"] == proposal.evidence_hash() and decision["amount"] == "420"


def test_approver_is_recorded_on_approval():
    warden, _ = w.offline_warden()
    held = warden.submit(w.lookalike_invoice())
    warden.approve(held.proposal_id, held.approval_hash, approver="rekha")
    approval = [e for e in warden.audit.entries() if e["event"] == "approval"][-1]["data"]
    assert approval["approver"] == "rekha" and approval["approval_hash"] == held.approval_hash


def test_registry_round_trips_the_mandate(tmp_path):
    registry = AgentRegistry(tmp_path / "agents.json")
    registry.add(AgentRecord.hired(w.RESEARCH, "research", w.research_mandate(), {"card_id": "card_1"}))
    loaded = AgentRegistry(tmp_path / "agents.json").agents[w.RESEARCH]
    assert loaded.to_mandate() == w.research_mandate()
    assert loaded.mandate_hash == mandate_hash(w.research_mandate())
