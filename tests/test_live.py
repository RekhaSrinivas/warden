"""Live wiring without the network: card controls, payee mapping, settlement, and the offline story."""

from decimal import Decimal
from typing import Any

from warden import demo, demo_world as w, live
from warden.models import Action
from warden.rails.airwallex import AirwallexRail


def test_card_controls_mirror_the_mandate():
    controls = live.card_controls(w.demo_mandate())
    assert controls["allowed_currencies"] == ["USD"]
    assert w.MCC_GIFT_SHOP not in controls["allowed_merchant_categories"]
    limits = {l["interval"]: l["amount"] for l in controls["transaction_limits"]["limits"]}
    assert limits == {"PER_TRANSACTION": 500.0, "WEEKLY": 2000.0}


def test_us_ach_beneficiary_has_the_required_account_category():
    details = live.beneficiary_body("Acme Office Supply", "50001121")["beneficiary"]["bank_details"]
    assert details["bank_account_category"] == "Checking"
    assert details["local_clearing_system"] == "ACH"


class RecordingClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        self.posts.append((path, body or {}))
        if path.endswith("/transfers/create"):
            return {"id": "tr_1", "status": "SCHEDULED"}
        return {"status": (body or {}).get("next_status")}


class RecordingSimulation:
    def __init__(self, client: RecordingClient) -> None:
        self.client = client

    def transfer_transition(self, transfer_id: str, next_status: str, failure_type: str | None = None):
        return self.client.post(f"/api/v1/simulation/transfers/{transfer_id}/transition", {"next_status": next_status})


def test_transfer_uses_the_real_beneficiary_and_settles_to_paid():
    client = RecordingClient()
    rail = AirwallexRail(client, RecordingSimulation(client), {}, payees={"ben_acme": "real-ben-uuid"},
                         settle_transfers=True)
    result = rail.execute(w.invoice("Acme Office Supply", "420", "INV-1001"))
    create = client.posts[0][1]
    assert create["beneficiary_id"] == "real-ben-uuid"
    assert create["transfer_amount"] == "420"
    assert [body["next_status"] for _, body in client.posts[1:]] == ["SENT", "PAID"]
    assert result["status"] == "PAID"


def test_offline_story_contains_the_agent_and_every_second_lock_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "EVIDENCE_PATH", tmp_path / "evidence.json")
    monkeypatch.setattr(demo, "say", lambda line: None)
    story, extra = demo.offline_story()
    demo.run_story(story)
    actions = [s["action"] for s in story.steps]
    assert actions == ["ALLOW"] * 4 + ["BLOCK", "STEP_UP", "BLOCK", "CONTAIN"]
    assert all(p["held"] for p in story.proofs)
    assert story.warden.trust.is_contained(w.AGENT)
    story.save(extra)
    assert (tmp_path / "evidence.json").exists()
    assert Action(actions[-1]) is Action.CONTAIN and Decimal(story.steps[3]["amount"]) == Decimal("0.01")
