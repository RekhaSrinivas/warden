"""Warden sandbox smoke test.

Runs every Airwallex sandbox call Warden depends on and prints PASS / WARN / FAIL
for each one, then a summary you can paste into Slack if something is blocked.

Usage:
    1. Copy .env.example to .env and paste your sandbox Client ID and API key.
    2. python scripts/smoke_test.py

Standard library only, so there is nothing to install.
Never prints your API key or access token.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT_PATH = ROOT / "smoke_report.json"
DEFAULT_BASE_URL = "https://api.sandbox.airwallex.com"

# Visa merchant category codes used by the card checks.
MCC_OFFICE_SUPPLIES = "5111"  # stationery, office and school supplies
MCC_STATIONERY = "5943"  # stationery stores
MCC_GIFT_SHOP = "5947"  # gift, card, novelty and souvenir shops (not allowed)

PER_TRANSACTION_LIMIT = 500
WEEKLY_LIMIT = 2000

# Some accounts reject an explicit card type, so try the minimal program first.
CARD_PROGRAMS = [
    {"purpose": "COMMERCIAL"},
    {"purpose": "COMMERCIAL", "sub_type": "GOOD_FUNDS_CREDIT", "type": "CREDIT"},
]

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class ApiError(Exception):
    def __init__(self, method: str, path: str, status: int, body):
        self.method, self.path, self.status, self.body = method, path, status, body
        super().__init__(self.summary())

    def summary(self) -> str:
        body = self.body
        if isinstance(body, dict):
            code, message = body.get("code"), body.get("message")
            fields = [e.get("source") for e in (body.get("details") or {}).get("errors", []) if e.get("source")]
            if fields:  # schema errors name the offending fields here, not in message
                message = f"invalid field(s): {', '.join(fields)}"
            if code or message:
                return f"HTTP {self.status} {code or ''}: {message or ''}".strip()
        text = body if isinstance(body, str) else json.dumps(body)
        if "<title>" in text:  # HTML error page from the edge, not an API error
            text = text.split("<title>", 1)[1].split("</title>", 1)[0] + " (HTML error page)"
        return f"HTTP {self.status}: {text[:300]}"


class CheckFailed(Exception):
    """The call did not do what Warden needs."""


class Mismatch(Exception):
    """The call worked, but the result differs from what the guide predicts."""


def _parse(raw: str):
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return raw


class Airwallex:
    def __init__(self, base_url: str, client_id: str, api_key: str, api_version: str | None):
        self.base_url = base_url.rstrip("/")
        self.client_id = client_id
        self.api_key = api_key
        self.api_version = api_version
        self.token: str | None = None
        self.token_expires = 0.0

    def login(self) -> dict:
        data = self._send(
            "POST",
            "/api/v1/authentication/login",
            headers={"x-client-id": self.client_id, "x-api-key": self.api_key},
            auth=False,
        )
        self.token = data["token"]
        # Tokens last 30 minutes; refresh a little early.
        self.token_expires = time.time() + 25 * 60
        return data

    def get(self, path: str, params: dict | None = None):
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        return self._send("GET", path)

    def post(self, path: str, body: dict | None = None):
        return self._send("POST", path, body=body if body is not None else {})

    def _send(self, method, path, body=None, headers=None, auth=True):
        hdrs = {"Accept": "application/json", "User-Agent": "warden-smoke-test/0.1"}
        if self.api_version:
            hdrs["x-api-version"] = self.api_version
        if auth:
            if not self.token or time.time() > self.token_expires:
                self.login()
            hdrs["Authorization"] = f"Bearer {self.token}"
        data = None
        if body is not None:
            hdrs["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")
        elif method == "POST":
            data = b""
        hdrs.update(headers or {})
        req = urllib.request.Request(self.base_url + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return _parse(resp.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise ApiError(method, path.split("?")[0], e.code, _parse(e.read().decode("utf-8", "replace"))) from None


class Report:
    def __init__(self):
        self.rows: list[dict] = []

    def check(self, step: str, name: str, fn) -> bool:
        endpoint = None
        try:
            status, detail = PASS, fn()
        except ApiError as e:
            status, detail, endpoint = FAIL, e.summary(), f"{e.method} {e.path}"
        except CheckFailed as e:
            status, detail = FAIL, str(e)
        except Mismatch as e:
            status, detail = WARN, str(e)
        except Exception as e:  # network errors, unexpected response shapes
            status, detail = FAIL, f"{type(e).__name__}: {e}"
        self._add(step, name, status, detail, endpoint)
        return status != FAIL

    def skip(self, step: str, name: str, reason: str) -> None:
        self._add(step, name, SKIP, f"skipped: {reason}", None)

    def _add(self, step, name, status, detail, endpoint):
        self.rows.append({"step": step, "name": name, "status": status, "detail": detail, "endpoint": endpoint})
        print(f"[{status}] {step:<3} {name}")
        if detail:
            print(f"           {detail}")

    def finish(self, ids: dict) -> int:
        counts = {s: sum(r["status"] == s for r in self.rows) for s in (PASS, WARN, FAIL, SKIP)}
        print("\n" + "=" * 72)
        print("  ".join(f"{s}: {n}" for s, n in counts.items()))

        failures = [r for r in self.rows if r["status"] in (FAIL, WARN)]
        if failures:
            works = [r["name"] for r in self.rows if r["status"] == PASS]
            print("\nSlack-ready summary (add your account ID from the Airwallex dashboard, never your keys):\n")
            print("Warden (Kits 2 + 3) sandbox check.")
            print("What works: " + ("; ".join(works) if works else "nothing yet") + ".")
            print("What fails:")
            for r in failures:
                where = f" [{r['endpoint']}]" if r["endpoint"] else ""
                print(f"- {r['name']}{where}: {r['detail']}")
            print("Account ID: <paste here>. Happy to DM more details. Thanks!")
        else:
            print("\nEverything Warden needs works on this sandbox account.")

        report = {"ran_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "ids": ids, "results": self.rows}
        REPORT_PATH.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"\nFull report saved to {REPORT_PATH}")
        return 1 if counts[FAIL] else 0


# --- checks -----------------------------------------------------------------


def usd_available(aw: Airwallex):
    data = aw.get("/api/v1/balances/current")
    items = data if isinstance(data, list) else data.get("items", [])
    for b in items:
        if b.get("currency") == "USD":
            return b.get("available_amount")
    return 0


def read_balances(aw: Airwallex) -> str:
    data = aw.get("/api/v1/balances/current")
    items = data if isinstance(data, list) else data.get("items", [])
    if not items:
        return "no balances yet (normal for a new account)"
    return ", ".join(f"{b.get('currency')} {b.get('available_amount')}" for b in items)


def usd_global_account(aw: Airwallex, ids: dict) -> str:
    data = aw.get("/api/v1/global_accounts")
    for acct in data.get("items", []):
        features = acct.get("required_features") or acct.get("supported_features") or []
        is_usd = acct.get("currency") == "USD" or any(f.get("currency") == "USD" for f in features)
        if is_usd and acct.get("status", "ACTIVE") == "ACTIVE":
            ids["global_account_id"] = acct["id"]
            return f"found {acct['id']}"
    created = aw.post(
        "/api/v1/global_accounts/create",
        {
            "request_id": str(uuid.uuid4()),
            "country_code": "US",
            "nick_name": "Warden USD",
            "required_features": [{"currency": "USD", "transfer_method": "LOCAL"}],
        },
    )
    ids["global_account_id"] = created["id"]
    return f"opened {created['id']} (status {created.get('status')})"


def simulate_deposit(aw: Airwallex, ids: dict) -> str:
    aw.post(
        "/api/v1/simulation/deposit/create",
        {
            "amount": 5000,
            "global_account_id": ids["global_account_id"],
            "payer_name": "Warden Smoke Test",
            "payer_country": "US",
            "payer_bankname": "Test bank",
            "reference": "warden smoke",
            "status": "SETTLED",
        },
    )
    return f"USD available now: {usd_available(aw)}"


def create_cardholder(aw: Airwallex, ids: dict) -> str:
    email = f"warden.agent+{int(time.time())}@example.com"
    data = aw.post("/api/v1/issuing/cardholders/create", {"type": "DELEGATE", "email": email})
    ids["cardholder_id"] = data["cardholder_id"]
    if data.get("status") != "READY":
        raise Mismatch(f"created {data['cardholder_id']} but status is {data.get('status')}, expected READY")
    return f"{data['cardholder_id']} READY"


def create_card(aw: Airwallex, ids: dict) -> str:
    errors = []
    for program in CARD_PROGRAMS:
        body = {
            "request_id": str(uuid.uuid4()),
            "cardholder_id": ids["cardholder_id"],
            "created_by": "Warden smoke test",
            "form_factor": "VIRTUAL",
            "is_personalized": False,
            "program": program,
            "authorization_controls": {
                "allowed_transaction_count": "MULTIPLE",
                "allowed_currencies": ["USD"],
                "allowed_merchant_categories": [MCC_OFFICE_SUPPLIES, MCC_STATIONERY],
                "transaction_limits": {
                    "currency": "USD",
                    "limits": [
                        {"amount": PER_TRANSACTION_LIMIT, "interval": "PER_TRANSACTION"},
                        {"amount": WEEKLY_LIMIT, "interval": "WEEKLY"},
                    ],
                },
            },
        }
        try:
            data = aw.post("/api/v1/issuing/cards/create", body)
        except ApiError as e:
            errors.append(f"program {json.dumps(program)} -> {e.summary()}")
            continue
        ids["card_id"] = data.get("card_id") or data.get("id")
        return f"{ids['card_id']} with program {json.dumps(program)}"
    raise CheckFailed(" | ".join(errors))


def wait_card_active(aw: Airwallex, ids: dict) -> str:
    status = None
    for _ in range(10):
        status = aw.get(f"/api/v1/issuing/cards/{ids['card_id']}").get("card_status")
        if status == "ACTIVE":
            return "ACTIVE"
        time.sleep(2)
    raise CheckFailed(f"card still {status} after 20 seconds")


def read_limits(aw: Airwallex, ids: dict) -> str:
    return json.dumps(aw.get(f"/api/v1/issuing/cards/{ids['card_id']}/limits"))[:300]


def charge(aw: Airwallex, ids: dict, amount, currency: str, mcc: str, expect: str | None = None) -> str:
    body = {
        "card_id": ids["card_id"],
        "transaction_amount": amount,
        "transaction_currency": currency,
        "merchant_category_code": mcc,
    }
    try:
        data = aw.post("/api/v1/simulation/issuing/create", body)
    except ApiError as e:
        if expect and expect in json.dumps(e.body):
            return f"declined with HTTP {e.status} ({expect})"
        raise
    result = data.get("process_result") or data.get("status")
    reason = data.get("failure_reason")
    got = f"{result} ({reason})" if reason else f"{result}"
    approved = result in ("APPROVED", "PENDING")
    if expect is None:
        if approved:
            return got
        raise CheckFailed(f"expected approval, got {got}")
    if reason == expect:
        return got
    if approved:
        raise CheckFailed(f"expected decline {expect}, but it was approved")
    raise Mismatch(f"declined, but reason was {reason}, expected {expect}")


def list_card_transactions(aw: Airwallex, ids: dict) -> str:
    data = aw.get("/api/v1/issuing/transactions", {"card_id": ids["card_id"]})
    items = data.get("items", [])
    return f"{len(items)} transactions on the card"


def freeze_card(aw: Airwallex, ids: dict) -> str:
    aw.post(f"/api/v1/issuing/cards/{ids['card_id']}/update", {"card_status": "INACTIVE"})
    status = aw.get(f"/api/v1/issuing/cards/{ids['card_id']}").get("card_status")
    if status != "INACTIVE":
        raise CheckFailed(f"card status is {status} after freezing")
    return "card is INACTIVE"


def create_beneficiary(aw: Airwallex, ids: dict) -> str:
    data = aw.post(
        "/api/v1/beneficiaries/create",
        {
            "beneficiary": {
                "entity_type": "COMPANY",
                "address": {
                    "city": "Seattle",
                    "country_code": "US",
                    "postcode": "98104",
                    "state": "Washington",
                    "street_address": "412 5th Avenue",
                },
                "bank_details": {
                    "account_currency": "USD",
                    "account_name": "Acme Office Supply",
                    "bank_account_category": "Checking",  # required for US ACH
                    "account_number": "50001121",
                    "account_routing_type1": "aba",
                    "account_routing_value1": "021000021",
                    "bank_country_code": "US",
                    "local_clearing_system": "ACH",
                },
            },
            "transfer_methods": ["LOCAL"],
        },
    )
    ids["beneficiary_id"] = data.get("id") or data.get("beneficiary_id")
    return f"{ids['beneficiary_id']}"


def transfer_body(ids: dict, request_id: str) -> dict:
    return {
        "request_id": request_id,
        "beneficiary_id": ids["beneficiary_id"],
        "transfer_amount": 50,
        "transfer_currency": "USD",
        "source_currency": "USD",
        "transfer_method": "LOCAL",
        "reason": "goods_purchased",
        "reference": "Warden smoke test",
    }


def create_transfer(aw: Airwallex, ids: dict, key: str) -> str:
    # Long request_ids fail schema validation; a bare UUID is safe.
    request_id = str(uuid.uuid4())
    data = aw.post("/api/v1/transfers/create", transfer_body(ids, request_id))
    ids[f"{key}_id"] = data["id"]
    ids[f"{key}_request_id"] = request_id
    return f"{data['id']} status {data.get('status')}"


def transition(aw: Airwallex, transfer_id: str, next_status: str, failure_type: str | None = None) -> str:
    body = {"next_status": next_status}
    if failure_type:
        body["failure_type"] = failure_type
    return aw.post(f"/api/v1/simulation/transfers/{transfer_id}/transition", body).get("status")


def pay_transfer(aw: Airwallex, ids: dict) -> str:
    transition(aw, ids["transfer_1_id"], "SENT")
    status = transition(aw, ids["transfer_1_id"], "PAID")
    if status != "PAID":
        raise Mismatch(f"status is {status} after PAID transition")
    return "SENT -> PAID"


def fail_transfer(aw: Airwallex, ids: dict) -> str:
    transition(aw, ids["transfer_2_id"], "SENT")
    transition(aw, ids["transfer_2_id"], "FAILED", "INVALID_ACCOUNT_NAME_OR_NUMBER")
    data = aw.get(f"/api/v1/transfers/{ids['transfer_2_id']}")
    status, reason = data.get("status"), data.get("failure_reason") or data.get("failure_type")
    if status != "CANCELLED":
        raise Mismatch(f"ended as {status} ({reason}); the guide says failed transfers end CANCELLED")
    return f"SENT -> FAILED, ended CANCELLED ({reason})"


def duplicate_refused(aw: Airwallex, ids: dict) -> str:
    try:
        data = aw.post("/api/v1/transfers/create", transfer_body(ids, ids["transfer_1_request_id"]))
    except ApiError as e:
        if 400 <= e.status < 500:
            return f"refused: {e.summary()}"
        raise
    if data.get("id") == ids["transfer_1_id"]:
        return "returned the original transfer, no second payment"
    raise CheckFailed(f"a SECOND transfer was created ({data.get('id')})")


# --- main -------------------------------------------------------------------


def main() -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_env(ROOT / ".env")
    client_id = os.environ.get("AIRWALLEX_CLIENT_ID", "").strip()
    api_key = os.environ.get("AIRWALLEX_API_KEY", "").strip()
    if not client_id or not api_key:
        print("Missing AIRWALLEX_CLIENT_ID or AIRWALLEX_API_KEY.")
        print("Copy .env.example to .env and paste your sandbox keys there.")
        return 2

    aw = Airwallex(
        base_url=os.environ.get("AIRWALLEX_BASE_URL") or DEFAULT_BASE_URL,
        client_id=client_id,
        api_key=api_key,
        api_version=os.environ.get("AIRWALLEX_API_VERSION") or None,
    )
    report = Report()
    ids: dict = {}
    print(f"Warden sandbox smoke test against {aw.base_url}")
    print(f"API version: {aw.api_version or 'account default'}\n")

    # 1. Log in. Nothing else can run without a token.
    if not report.check("1", "Log in and get an access token", lambda: f"token valid until {aw.login().get('expires_at')}"):
        print("           Check that .env has your SANDBOX keys (sandbox.airwallex.com > Account > Developer >")
        print("           API keys), with no extra spaces or quotes, and that the key is enabled.")
        return report.finish(ids)

    # 2. Money in the wallet.
    report.check("2a", "Read current balances", lambda: read_balances(aw))
    report.check("2b", "Find or open a USD global account", lambda: usd_global_account(aw, ids))
    if "global_account_id" in ids:
        report.check("2c", "Simulate a $5,000 USD deposit", lambda: simulate_deposit(aw, ids))
    else:
        report.skip("2c", "Simulate a $5,000 USD deposit", "no USD global account")

    # 3. The agent's card, with Warden's mandate as card controls.
    report.check("3a", "Create a DELEGATE cardholder", lambda: create_cardholder(aw, ids))
    card_steps = [
        ("3b", "Create a virtual card ($500/txn, $2,000/week, USD, supplies MCCs)", lambda: create_card(aw, ids)),
        ("3c", "Card becomes ACTIVE", lambda: wait_card_active(aw, ids)),
        ("3d", "Read card limits", lambda: read_limits(aw, ids)),
        # 4. Simulated charges. Each decline reason should match the policy.
        ("4a", "Charge that should pass: $25 at a stationery store",
         lambda: charge(aw, ids, 25, "USD", MCC_STATIONERY)),
        ("4b", "Charge that should fail: $499 at a gift shop",
         lambda: charge(aw, ids, 499, "USD", MCC_GIFT_SHOP, expect="MERCHANT_CATEGORY_NOT_ALLOWED")),
        ("4c", "Charge that should fail: $600, over the per-transaction limit",
         lambda: charge(aw, ids, 600, "USD", MCC_STATIONERY, expect="LIMIT_EXCEEDED")),
        ("4d", "Charge that should fail: EUR instead of USD",
         lambda: charge(aw, ids, 25, "EUR", MCC_STATIONERY, expect="CURRENCY_NOT_ALLOWED")),
        ("4e", "List the card's transactions", lambda: list_card_transactions(aw, ids)),
        # 5. Containment: freeze the card, then prove the rail refuses it.
        ("5a", "Freeze the card", lambda: freeze_card(aw, ids)),
        ("5b", "Charge on the frozen card should fail",
         lambda: charge(aw, ids, 25, "USD", MCC_STATIONERY, expect="CARD_INACTIVE")),
    ]
    for step, name, fn in card_steps:
        needs = "card_id" if step != "3b" else "cardholder_id"
        if needs in ids:
            report.check(step, name, fn)
        else:
            report.skip(step, name, f"no {needs.replace('_', ' ')}")

    # 6. Transfers to a payee, including a failed one.
    report.check("6a", "Create a US ACH beneficiary", lambda: create_beneficiary(aw, ids))
    transfer_steps = [
        ("6b", "Create a $50 transfer", lambda: create_transfer(aw, ids, "transfer_1"), "beneficiary_id"),
        ("6c", "Move it SENT -> PAID", lambda: pay_transfer(aw, ids), "transfer_1_id"),
        ("6d", "Create a second $50 transfer", lambda: create_transfer(aw, ids, "transfer_2"), "beneficiary_id"),
        ("6e", "Move it SENT -> FAILED", lambda: fail_transfer(aw, ids), "transfer_2_id"),
        # 7. Duplicate protection.
        ("7", "Re-send the first transfer's request_id: must not pay twice",
         lambda: duplicate_refused(aw, ids), "transfer_1_id"),
    ]
    for step, name, fn, needs in transfer_steps:
        if needs in ids:
            report.check(step, name, fn)
        else:
            report.skip(step, name, f"no {needs.replace('_', ' ')}")

    return report.finish(ids)


if __name__ == "__main__":
    sys.exit(main())
