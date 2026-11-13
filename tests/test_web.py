"""Dashboard API, offline: simulated rails, the heuristic risk reader, receipts signed with a throwaway key."""

from fastapi.testclient import TestClient

from warden import demo_world as w
from warden.web import create_app
from warden.web.app import Services, attack_for, offline_services, offline_warden, single_step_attacks


def make_client(tmp_path):
    services = offline_services(tmp_path)
    warden = offline_warden(services.audit_path)
    return TestClient(create_app(warden, services)), warden


def test_page_renders_the_story_with_initial_state(tmp_path):
    client, _ = make_client(tmp_path)
    res = client.get("/")
    assert res.status_code == 200
    assert "It makes that trust enforceable, revocable, and verifiable." in res.text
    for act in ("Hire it", "Guard it", "Prove it", "Recover", "Independent locks"):
        assert act in res.text
    assert '"mode": "offline"' in res.text
    assert client.get("/static/app.js").status_code == 200
    state = client.get("/api/state").json()
    assert [a["id"] for a in state["agents"]] == [w.PROCUREMENT, w.RESEARCH]
    assert state["cases"]["pipeline"] == ["UNATTRIBUTED", "INVESTIGATING", "DISPUTE_SUBMITTED", "RECOVERED"]


def test_normal_day_is_allowed_and_attack_is_stopped(tmp_path):
    client, _ = make_client(tmp_path)
    actions = client.post("/api/normal-day", json={"agent_id": w.PROCUREMENT}).json()["actions"]
    assert set(actions) == {"ALLOW"}
    attack = client.post("/api/attack", json={"agent_id": w.PROCUREMENT}).json()
    assert attack["action"] != "ALLOW"
    state = client.get("/api/state").json()
    assert state["feed"][0]["proposal"]["id"] == attack["proposal_id"]  # newest first
    procurement = next(a for a in state["agents"] if a["id"] == w.PROCUREMENT)
    assert procurement["spent_week"] != "0" and procurement["budget_pct"] > 0


def test_attacks_can_be_aimed_at_either_agent():
    for agent in (w.PROCUREMENT, w.RESEARCH):
        _, proposal = attack_for(agent, single_step_attacks())
        assert proposal.agent_id == agent


def test_step_up_flow_rejects_tampering_then_approves_and_is_receipted(tmp_path):
    client, warden = make_client(tmp_path)
    pid = warden.submit(w.lookalike_invoice()).proposal_id
    inbox = client.get("/api/state").json()["inbox"]
    assert [i["proposal"]["id"] for i in inbox] == [pid]

    tampered = client.post("/api/tamper", json={"proposal_id": pid})
    assert tampered.status_code == 409 and tampered.json()["refused"]
    assert pid in warden.pending  # a changed proposal never runs and the original still waits

    res = client.post("/api/approve", json={"proposal_id": pid, "approval_hash": inbox[0]["approval_hash"]})
    assert res.status_code == 200 and res.json()["action"] == "ALLOW"
    assert len(warden.rails["transfer"].executed) == 1

    receipts = client.get("/api/state").json()["receipts"]
    assert receipts["chain_intact"] and receipts["count"] == 1
    row = receipts["recent"][0]
    assert row["approver"] == "finance@acme (dashboard)" and row["signed"]
    page = client.get(f"/verify/{row['id']}")
    assert page.status_code == 200


def test_firing_one_agent_leaves_the_other_working(tmp_path):
    client, warden = make_client(tmp_path)
    res = client.post("/api/fire", json={"agent_id": w.PROCUREMENT})
    assert res.status_code == 200
    agents = {a["id"]: a for a in client.get("/api/state").json()["agents"]}
    assert agents[w.PROCUREMENT]["contained"] and not agents[w.RESEARCH]["contained"]
    assert client.post("/api/normal-day", json={"agent_id": w.RESEARCH}).json()["actions"] == ["ALLOW", "ALLOW"]
    locks = client.post("/api/locks").json()["results"]
    assert len(locks) == 4 and all(lock["held"] for lock in locks)
    assert client.post("/api/fire", json={"agent_id": "nobody"}).status_code == 404


def test_hire_shows_the_validator_changes_then_provisions_exactly_that(tmp_path):
    client, warden = make_client(tmp_path)
    instruction = "Buy office supplies and stationery, max $400 per purchase and $9,000 a week, USD only."
    draft = client.post("/api/hire/draft", json={"agent_id": "Supplies Bot", "role": "supplies",
                                                 "instruction": instruction}).json()
    assert draft["agent_id"] == "supplies-bot"
    assert float(draft["mandate"]["weekly_cap"]) == 2000 and draft["changes"]  # clamped to company policy, and said so
    assert client.post("/api/hire/confirm", json={"agent_id": "supplies-bot"}).status_code == 200
    assert warden.mandates["supplies-bot"].weekly_cap == 2000
    assert client.post("/api/hire/confirm", json={"agent_id": "supplies-bot"}).status_code == 404  # one draft, one hire
    taken = client.post("/api/hire/draft", json={"agent_id": w.RESEARCH, "instruction": instruction})
    assert taken.status_code == 409


def test_reconciliation_opens_a_case_for_the_charge_without_a_receipt(tmp_path):
    client, _ = make_client(tmp_path)
    cases = client.post("/api/reconcile").json()["cases"]
    assert [(c["amount"], c["state"]) for c in cases] == [("180", "DISPUTE_SUBMITTED")]
    assert client.get("/api/state").json()["cases"]["items"] == cases


def test_verify_detects_tampering(tmp_path):
    client, warden = make_client(tmp_path)
    client.post("/api/normal-day", json={"agent_id": w.PROCUREMENT})
    assert client.get("/api/verify").json()["intact"]
    warden.audit._entries[2]["data"]["note"] = "edited"
    assert client.get("/api/verify").json() == {"intact": False, "broken_at": 2,
                                                "entries": len(warden.audit.entries())}


def test_scorecard_and_stream(tmp_path):
    client, _ = make_client(tmp_path)
    score = client.get("/api/scorecard").json()
    assert score["ok"] and "attacks stopped" in score["summary"]
    with client.stream("GET", "/api/stream?once=true") as res:
        assert res.headers["content-type"].startswith("text/event-stream")
        assert "event: audit" in "".join(res.iter_text())


def test_reset_only_offline(tmp_path):
    client = TestClient(create_app())  # default: offline factory, so reset works
    client.post("/api/normal-day", json={"agent_id": w.PROCUREMENT})
    assert client.post("/api/reset").json() == {"ok": True}
    assert client.get("/api/state").json()["feed"] == []
    services = offline_services(tmp_path)
    live_like = Services(**{**services.__dict__, "mode": "live"})
    client = TestClient(create_app(offline_warden(services.audit_path), live_like))
    assert client.post("/api/reset").status_code == 400
