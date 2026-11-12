"""Dashboard API, offline: simulated rails and the heuristic risk reader."""

from fastapi.testclient import TestClient

from warden import demo_world as w
from warden.web import create_app


def make_client():
    warden, rails = w.offline_warden()
    return TestClient(create_app(warden)), warden, rails


def test_page_renders_with_initial_state():
    client, _, _ = make_client()
    res = client.get("/")
    assert res.status_code == 200
    assert "One guard for every way an AI agent spends money" in res.text
    assert '"mode": "offline"' in res.text
    assert client.get("/static/app.js").status_code == 200


def test_normal_day_is_allowed_and_attack_is_stopped():
    client, _, _ = make_client()
    actions = client.post("/api/normal-day").json()["actions"]
    assert set(actions) == {"ALLOW"}
    attack = client.post("/api/attack").json()
    assert attack["action"] != "ALLOW"
    feed = client.get("/api/state").json()["feed"]
    assert feed[0]["proposal"]["id"] == attack["proposal_id"]  # newest first


def test_step_up_flow_rejects_tampering_then_approves():
    client, warden, rails = make_client()
    pid = warden.submit(w.lookalike_invoice()).proposal_id
    inbox = client.get("/api/state").json()["inbox"]
    assert [i["proposal"]["id"] for i in inbox] == [pid]

    tampered = client.post("/api/tamper", json={"proposal_id": pid})
    assert tampered.status_code == 409 and tampered.json()["refused"]
    assert pid in warden.pending  # a changed proposal never runs and the original still waits

    res = client.post("/api/approve", json={"proposal_id": pid, "approval_hash": inbox[0]["approval_hash"]})
    assert res.status_code == 200 and res.json()["action"] == "ALLOW"
    assert len(rails["transfer"].executed) == 1

    receipt = client.get(f"/api/receipt/{pid}")
    assert "attachment" in receipt.headers["content-disposition"]
    body = receipt.json()
    assert body["approver"] == "finance@acme (dashboard)"
    assert body["chain_intact"] and body["audit_entry"]["hash"]


def test_kill_switch_contains_every_rail():
    client, _, rails = make_client()
    client.post("/api/contain", json={"reason": "test"})
    state = client.get("/api/state").json()
    assert state["agent"]["contained"]
    assert {r["status"] for r in state["rails"]} == {"contained"}
    assert all(r.contained for r in rails.values())


def test_verify_detects_tampering():
    client, warden, _ = make_client()
    client.post("/api/normal-day")
    assert client.get("/api/verify").json()["intact"]
    warden.audit._entries[2]["data"]["note"] = "edited"
    assert client.get("/api/verify").json() == {"intact": False, "broken_at": 2,
                                                "entries": len(warden.audit.entries())}


def test_scorecard_and_stream():
    client, _, _ = make_client()
    score = client.get("/api/scorecard").json()
    assert score["ok"] and "attacks stopped" in score["summary"]
    with client.stream("GET", "/api/stream?once=true") as res:
        assert res.headers["content-type"].startswith("text/event-stream")
        assert "event: audit" in "".join(res.iter_text())


def test_reset_only_offline():
    client = TestClient(create_app())  # default: offline factory, so reset works
    client.post("/api/normal-day")
    assert client.post("/api/reset").json() == {"ok": True}
    assert client.get("/api/state").json()["feed"] == []
    warden, _ = w.offline_warden()
    assert TestClient(create_app(warden, mode="live")).post("/api/reset").status_code == 400
