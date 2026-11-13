"""Live wiring without the network: card controls, payee mapping, settlement, and the offline story."""

from typing import Any

from warden import demo, demo_world as w, live
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


def test_offline_story_contains_only_the_attacked_agent_and_every_lock_holds(tmp_path, monkeypatch):
    monkeypatch.setattr(demo, "OFFLINE_EVIDENCE_PATH", tmp_path / "evidence.json")
    monkeypatch.setattr(demo, "say", lambda line: None)
    story, extra = demo.offline_story()
    demo.run_story(story)
    actions = [s["action"] for s in story.steps]
    assert actions == ["ALLOW"] * 6 + ["BLOCK", "STEP_UP", "BLOCK", "CONTAIN", "ALLOW"]
    assert [h["agent_id"] for h in story.hires] == [w.PROCUREMENT, w.RESEARCH]
    assert any("capped at $2000" in c for c in story.hires[0]["changes"])
    assert story.out_of_band["result"] == "APPROVED"  # Warden never saw it: that is the point
    assert all(p["held"] for p in story.proofs) and len(story.proofs) == 3
    assert story.warden.trust.is_contained(w.PROCUREMENT)
    assert not story.warden.trust.is_contained(w.RESEARCH)
    assert story.steps[-1]["agent_id"] == w.RESEARCH  # research-bot kept working after the containment
    receipts = story.recovery["receipts"]
    assert receipts["chain_intact"] and receipts["issued"] >= 10
    [case] = story.recovery["cases"]  # the charge made outside Warden, and only that one
    assert case["amount"] == "180" and case["state"] == "DISPUTE_SUBMITTED"  # never RECOVERED on submission
    story.save(extra)
    assert (tmp_path / "evidence.json").exists()
