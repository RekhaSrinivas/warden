from dataclasses import replace
from decimal import Decimal

from warden import demo_world as w
from warden.mandate import HOUR_SECONDS, WEEK_SECONDS, SpendState, check

NOW = 1_000_000.0


def violations(proposal, state=None):
    return check(w.demo_mandate(), proposal, state or SpendState(), NOW)


def test_clean_invoice_passes():
    assert violations(w.invoice("Acme Office Supply", "420", "INV-1")) == []


def test_cap_is_inclusive_like_airwallex():
    assert violations(w.invoice("Acme Office Supply", "500", "INV-1")) == []
    assert violations(w.invoice("Acme Office Supply", "500.01", "INV-1")) == ["OVER_TXN_CAP"]


def test_new_payee_and_over_cap():
    assert violations(w.fake_bank_change()) == ["OVER_TXN_CAP", "PAYEE_NOT_ALLOWLISTED"]


def test_currency_and_merchant_category():
    eur = replace(w.invoice("Globex Paper Co", "100", "INV-2"), currency="EUR")
    assert violations(eur) == ["CURRENCY_NOT_ALLOWED"]
    assert violations(w.gift_card()) == ["MERCHANT_CATEGORY_NOT_ALLOWED"]


def test_weekly_cap_is_rolling():
    state = SpendState()
    state.record(w.AGENT, Decimal("1800"), NOW - 3600)
    state.record(w.AGENT, Decimal("900"), NOW - WEEK_SECONDS - 1)  # outside the window
    assert violations(w.invoice("Acme Office Supply", "200", "INV-3"), state) == []
    assert violations(w.invoice("Acme Office Supply", "201", "INV-3"), state) == ["OVER_WEEKLY_CAP"]


def test_duplicate_reserve_floor_and_velocity():
    state = SpendState(seen_request_ids={"req_dup"}, balance=Decimal("1100"))
    p = w.invoice("Acme Office Supply", "200", "INV-4", request_id="req_dup")
    assert violations(p, state) == ["DUPLICATE_REQUEST", "RESERVE_FLOOR"]
    assert check(w.demo_mandate(), p, state, NOW, ignore_duplicate=True) == ["RESERVE_FLOOR"]

    busy = SpendState()
    for i in range(10):
        busy.record(w.AGENT, Decimal("1"), NOW - HOUR_SECONDS + 60 + i)
    assert "VELOCITY" in violations(w.data_api_call(), busy)
