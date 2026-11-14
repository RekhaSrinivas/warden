import json
import time
from decimal import Decimal

from fastapi.testclient import TestClient

from warden import demo_world as w
from warden.audit import AuditLog
from warden.authorize import RemoteAuthorizer, sign, verify_signature
from warden.engine import Warden
from warden.risk import OfflineRiskReader
from warden.web.app import create_app

CARD = "card_procurement"


def payload(amount="85", mcc=w.MCC_STATIONERY, event="evt_1", card=CARD, kind="AUTHORIZATION"):
    return {"version": 2, "card_id": card, "card_transaction_event_id": event, "card_transaction_id": f"tx_{event}",
            "transaction_type": kind, "transaction_amount": amount, "transaction_currency": "USD",
            "merchant": {"name": "STAPLES", "category_code": mcc}}


class CallbackCardRail:
    """Like Airwallex: while Warden's card payment runs, the network calls Warden's authorizer and
    approves or declines on its answer."""

    name = "airwallex-card"

    def __init__(self):
        self.authorizer = None
        self.answers = []

    def execute(self, p):
        answer = self.authorizer.decide(payload(str(p.amount), p.merchant_category, event=f"evt_{p.id}"))
        self.answers.append(answer)
        return {"rail": self.name, "status": "CLEARED" if answer["response_status"] == "AUTHORIZED" else "DECLINED"}

    def contain(self, agent_id):
        return {"rail": self.name, "status": "CONTAINED"}


def world():
    rail = CallbackCardRail()
    warden = Warden(w.procurement_mandate(), OfflineRiskReader(w.VENDOR_DOMAINS), {"card": rail}, audit=AuditLog())
    warden.hire(w.PROCUREMENT, w.procurement_mandate())
    authorizer = RemoteAuthorizer(lambda: warden, lambda: {CARD: w.PROCUREMENT}, secret=None)
    rail.authorizer = authorizer
    return warden, authorizer, rail


def test_the_network_calls_back_mid_payment_and_warden_authorizes_its_own_charge_without_deadlock():
    warden, _, rail = world()
    decision = warden.submit(w.supplies_card("85"))
    assert decision.action.value == "ALLOW" and decision.execution["status"] == "CLEARED"
    assert rail.answers[0]["response_status"] == "AUTHORIZED"
    events = [e for e in warden.audit.entries() if e["event"] == "remote_authorization"]
    assert events[0]["data"]["proposal_id"] == decision.proposal_id and events[0]["data"]["decided_in_ms"] < 2500


def test_no_intent_receipt_no_authorization():
    warden, authorizer, _ = world()
    out_of_band = authorizer.decide(payload("180", event="evt_oob"))
    assert out_of_band == {"card_transaction_event_id": "evt_oob", "response_status": "DECLINED",
                           "status_reason": out_of_band["status_reason"]}
    assert "never approved" in out_of_band["status_reason"]
    assert authorizer.decide(payload(event="evt_x", card="someone_elses_card"))["response_status"] == "DECLINED"
    assert authorizer.decide(payload(kind="REFUND", event="evt_r"))["response_status"] == "AUTHORIZED"
    assert warden.audit.verify() is None


def test_one_approval_authorizes_exactly_one_charge_of_exactly_that_amount():
    warden, authorizer, _ = world()
    p = w.supplies_card("85")
    warden.intents.expect(p)  # approved, not yet charged
    assert authorizer.decide(payload("85.01", event="evt_a"))["response_status"] == "DECLINED"  # a cent off
    assert authorizer.decide(payload("85", mcc=w.MCC_GIFT_SHOP, event="evt_b"))["response_status"] == "DECLINED"
    first = authorizer.decide(payload("85", event="evt_c"))
    assert first["response_status"] == "AUTHORIZED"
    assert authorizer.decide(payload("85", event="evt_c")) == first  # the network retrying gets the same answer
    assert authorizer.decide(payload("85", event="evt_d"))["response_status"] == "DECLINED"  # used up


def test_a_contained_agents_card_authorizes_nothing():
    warden, authorizer, _ = world()
    warden.intents.expect(w.supplies_card("85"))
    warden.contain(w.PROCUREMENT, "fired")
    assert authorizer.decide(payload("85", event="evt_e"))["response_status"] == "DECLINED"


def test_requests_must_carry_a_fresh_airwallex_signature():
    now = time.time()
    nonce = f"{int(now * 1000)}.abc123"
    good = {"x-nonce": nonce, "x-signature": sign("s3cret", nonce)}
    assert verify_signature("s3cret", good, now) is None
    assert verify_signature("other", good, now) == "bad signature"
    assert verify_signature("s3cret", good, now + 3600) == "stale nonce"
    assert verify_signature("s3cret", {}, now) == "missing x-nonce or x-signature"
    warden, _, _ = world()
    signed = RemoteAuthorizer(lambda: warden, lambda: {CARD: w.PROCUREMENT}, secret="s3cret")
    assert signed.handle({"x-nonce": nonce, "x-signature": "forged"}, b"{}")[0] == 401
    status, answer = signed.handle(good, json.dumps(payload(event="evt_s")).encode())
    assert status == 200 and answer["response_status"] == "DECLINED"


def test_the_dashboard_serves_the_endpoint_and_shows_each_answer():
    app = create_app()
    client = TestClient(app)
    response = client.post("/airwallex/remote-auth", json=payload("180", event="evt_web", card="simulated_card_procurement"))
    assert response.status_code == 200 and response.json()["response_status"] == "DECLINED"
    shown = client.get("/api/state").json()["authorizations"][0]
    assert shown["response_status"] == "DECLINED" and Decimal(shown["amount"]) == Decimal("180")


def test_a_charge_warden_authorized_may_settle_but_a_stranger_clearing_may_not():
    warden, authorizer, _ = world()
    warden.intents.expect(w.supplies_card("85"))
    auth = payload("85", event="evt_a") | {"card_transaction_lifecycle_id": "life_1"}
    assert authorizer.decide(auth)["response_status"] == "AUTHORIZED"
    clearing = payload("85", event="evt_c", kind="CLEARING") | {"card_transaction_lifecycle_id": "life_1"}
    assert authorizer.decide(clearing)["response_status"] == "AUTHORIZED"
    larger = payload("85.01", event="evt_c2", kind="CLEARING") | {"card_transaction_lifecycle_id": "life_1"}
    assert authorizer.decide(larger)["response_status"] == "DECLINED"
    stranger = payload("85", event="evt_s", kind="CLEARING") | {"card_transaction_lifecycle_id": "life_2"}
    assert authorizer.decide(stranger)["response_status"] == "DECLINED"
