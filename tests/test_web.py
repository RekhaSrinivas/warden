from decimal import Decimal
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
    story = client.get("/")
    assert story.status_code == 200 and 'href="/console"' in story.text and "{{" not in story.text
    assert client.get("/static/story.js").status_code == 200
    res = client.get("/console")
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
    seen = draft["mandate"]["mandate_id"]
    # The manager edits the job and drafts again: the draft they first saw can no longer be confirmed.
    newer = client.post("/api/hire/draft", json={"agent_id": "Supplies Bot", "role": "supplies",
                                                 "instruction": instruction.replace("$400", "$300")}).json()
    stale = client.post("/api/hire/confirm", json={"agent_id": "supplies-bot", "mandate_id": seen})
    assert stale.status_code == 409 and "supplies-bot" not in warden.mandates
    ok = client.post("/api/hire/confirm", json={"agent_id": "supplies-bot", "mandate_id": newer["mandate"]["mandate_id"]})
    assert ok.status_code == 200
    assert warden.mandates["supplies-bot"].weekly_cap == 2000 and warden.mandates["supplies-bot"].per_txn_cap == 300
    again = client.post("/api/hire/confirm", json={"agent_id": "supplies-bot", "mandate_id": newer["mandate"]["mandate_id"]})
    assert again.status_code == 404  # one draft, one hire
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


def test_money_protected_counts_each_payment_once_by_its_final_outcome():
    from fastapi.testclient import TestClient as Client

    app = create_app()
    client = Client(app)
    warden = app.state.board.warden
    client.post("/api/normal-day", json={"agent_id": w.PROCUREMENT})  # 420 + 260 + 85 paid
    first = w.invoice("Acme Office Supply", "420", "INV-1001")
    warden.submit(first)  # a fresh invoice object: paid
    warden.submit(first)  # the same request again: a double payment prevented
    warden.submit(w.fake_bank_change("1200"))  # stopped
    client.post("/airwallex/remote-auth", json={"version": 2, "card_id": "simulated_card_procurement", "card_transaction_event_id": "e1",
                                                "transaction_amount": 180, "transaction_currency": "USD"})
    for stranger in ({"card_id": "not-ours"}, {"card_id": "simulated_card_procurement", "card_transaction_event_id": "selftest_1"}):
        client.post("/airwallex/remote-auth", json={"version": 2, "card_transaction_event_id": "e2", "transaction_amount": 999,
                                                    "transaction_currency": "USD"} | stranger)  # not Warden's money
    m = client.get("/api/state").json()["protection"]
    assert m["paid"] == {"amount": "1185", "count": 4}
    assert m["double_payments"] == {"amount": "420", "count": 1}
    assert m["stopped"] == {"amount": "1200", "count": 1}
    assert Decimal(m["declined_live"]["amount"]) == 180 and m["declined_live"]["count"] == 1
    assert Decimal(m["protected"]) == 1800


def test_normal_day_never_pays_twice_and_a_hired_agent_gets_its_own(tmp_path):
    client, warden = make_client(tmp_path)
    first = client.post("/api/normal-day", json={"agent_id": w.PROCUREMENT}).json()["actions"]
    again = client.post("/api/normal-day", json={"agent_id": w.PROCUREMENT}).json()["actions"]
    assert first == ["ALLOW", "ALLOW", "ALLOW"] and again == ["BLOCK", "BLOCK", "BLOCK"]  # retries absorbed
    assert not warden.trust.strikes(w.PROCUREMENT, warden.clock())  # a retry is not the agent's fault
    instruction = "Book flights and hotels for the sales team, max $400 per purchase and $1,500 a week, USD only. Anything over $300 needs me."
    d = client.post("/api/hire/draft", json={"agent_id": "travel-bot", "role": "Sales", "instruction": instruction}).json()
    assert client.post("/api/hire/confirm", json={"agent_id": "travel-bot", "mandate_id": d["mandate"]["mandate_id"]}).status_code == 200
    assert client.post("/api/normal-day", json={"agent_id": "travel-bot"}).json()["actions"] == ["ALLOW", "ALLOW"]


def test_the_verify_page_is_never_behind_the_console_or_a_reset(tmp_path):
    client, _ = make_client(tmp_path)
    client.post("/api/normal-day", json={"agent_id": w.PROCUREMENT})
    page = client.get("/verify").text  # opened before the console refreshed
    receipt_id = page.split("/verify/rcpt_", 1)[1].split('"', 1)[0]
    assert client.get(f"/verify/rcpt_{receipt_id}").status_code == 200


def test_the_attack_button_tells_the_story_in_order(tmp_path):
    client, warden = make_client(tmp_path)
    actions = [client.post("/api/attack", json={"agent_id": w.PROCUREMENT}).json() for _ in range(3)]
    assert [a["action"] for a in actions] == ["STEP_UP", "BLOCK", "CONTAIN"]  # inbox, strike one, fired
    assert actions[0]["scenario"].startswith("Lookalike") and actions[1]["scenario"].startswith("Fake 'bank")
    assert warden.trust.is_contained(w.PROCUREMENT)


def test_offline_story_replays_the_published_live_run_until_something_happens(tmp_path):
    client, _ = make_client(tmp_path)
    fresh = client.get("/").text
    assert '"snapshot": "2026-11-13"' in fresh and '"mode": "live"' in fresh  # the real run, labelled
    client.post("/api/normal-day", json={"agent_id": w.PROCUREMENT})
    assert '"snapshot"' not in client.get("/").text  # once the visitor runs something, it is their book


def test_locks_are_tested_only_after_the_agent_is_fired_and_then_all_hold(tmp_path):
    client, _ = make_client(tmp_path)
    early = client.post("/api/locks")
    assert early.status_code == 409 and "Fire procurement-bot first" in early.json()["detail"]
    client.post("/api/fire", json={"agent_id": w.PROCUREMENT})
    results = client.post("/api/locks").json()["results"]
    assert len(results) == 4 and all(r["held"] for r in results)


def test_a_public_link_is_look_only_except_airwallex_remote_auth(tmp_path):
    client, _ = make_client(tmp_path)
    public = {"host": "warden-demo.trycloudflare.com"}
    assert client.get("/", headers=public).status_code == 200
    assert client.get("/api/state", headers=public).status_code == 200
    for path, body in (("/api/normal-day", {"agent_id": "procurement-bot"}), ("/api/reset", None),
                       ("/api/fire", {"agent_id": "procurement-bot"}), ("/mcp", {})):
        assert client.post(path, json=body, headers=public).status_code == 403, path
    answer = client.post("/airwallex/remote-auth", headers=public, json={
        "version": 2, "card_id": "unknown", "card_transaction_event_id": "evt_1", "transaction_amount": 5,
        "transaction_currency": "USD", "merchant": {"category_code": "5943"}})
    assert answer.status_code == 200 and answer.json()["response_status"] == "DECLINED"
    assert client.post("/api/normal-day", json={"agent_id": "procurement-bot"},
                       headers={"host": "127.0.0.1:8000"}).status_code == 200
