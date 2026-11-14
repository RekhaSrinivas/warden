"""Real-time card authorization: no Intent Receipt, no authorization.

With Airwallex Issuing remote authorization switched on, the card network asks Warden about
every charge on an agent's card before it is approved, and waits at most 2.5 seconds. Warden
answers from memory, without a model or a lock:

- AUTHORIZED only if Warden itself approved this exact charge moments ago: same agent's card,
  same amount and currency, same merchant category. Each approval authorizes one charge.
- DECLINED for everything else: copied card details used out of band, a contained agent, an
  amount that differs by a cent, a card Warden does not manage.

Requests are verified with Airwallex's signature (x-nonce, x-signature: HMAC-SHA256 of the nonce
with the shared secret) and a timestamp window. Every answer is written to the audit log.

    python -m warden.authorize status                     show the sandbox's remote-auth settings
    python -m warden.authorize enable --url https://...   point Airwallex at Warden (needs Airwallex to
                                                          enable remote auth on the account first)
    python -m warden.authorize selftest [--url ...]       send signed test requests to a running Warden
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
import time
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, Callable, Mapping

from warden.models import Proposal

if TYPE_CHECKING:
    from warden.engine import Warden

INTENT_TTL = 600  # an approved card payment must be charged within 10 minutes
NONCE_TOLERANCE = 300  # seconds
CREDIT_TYPES = {"REFUND", "REVERSAL", "CREDIT", "CREDIT_VOUCHER"}  # money coming back to the card


@dataclass
class Intent:
    proposal_id: str
    agent_id: str
    amount: Decimal
    currency: str
    merchant_category: str
    payee_name: str
    expires: float
    used_by: str = ""  # the card transaction event it authorized


@dataclass
class IntentBook:
    """Card payments Warden approved and is about to make. The authorizer reads it without the engine lock."""

    clock: Callable[[], float] = time.time
    _intents: list[Intent] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def expect(self, p: Proposal) -> None:
        with self._lock:
            now = self.clock()
            self._intents = [i for i in self._intents if i.expires > now]
            self._intents.append(Intent(p.id, p.agent_id, p.amount, p.currency, p.merchant_category, p.payee_name,
                                        now + INTENT_TTL))

    def withdraw(self, proposal_id: str) -> None:
        with self._lock:
            self._intents = [i for i in self._intents if i.proposal_id != proposal_id or i.used_by]

    def claim(self, agent_id: str, amount: Decimal, currency: str, mcc: str, event_id: str) -> Intent | None:
        """The unused, unexpired intent this charge matches, marked used. None if there is none."""
        with self._lock:
            now = self.clock()
            for intent in self._intents:
                if (not intent.used_by and intent.expires > now and intent.agent_id == agent_id
                        and intent.amount == amount and intent.currency == currency
                        and (not mcc or not intent.merchant_category or intent.merchant_category == mcc)):
                    intent.used_by = event_id
                    return intent
            return None


# --- signatures -------------------------------------------------------------------------------

def sign(secret: str, nonce: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).digest()).decode()


def verify_signature(secret: str, headers: Mapping[str, str], now: float, tolerance: float = NONCE_TOLERANCE) -> str | None:
    """None if the request is genuine and fresh, else why not."""
    lower = {k.lower(): v for k, v in headers.items()}
    nonce, signature = lower.get("x-nonce", ""), lower.get("x-signature", "")
    if not nonce or not signature:
        return "missing x-nonce or x-signature"
    if not hmac.compare_digest(sign(secret, nonce), signature):
        return "bad signature"
    try:
        sent = int(nonce.split(".", 1)[0]) / 1000
    except ValueError:
        return "nonce has no timestamp"
    if abs(now - sent) > tolerance:
        return "stale nonce"
    return None


# --- the decision ----------------------------------------------------------------------------

class RemoteAuthorizer:
    def __init__(self, get_warden: Callable[[], Warden], card_agents: Callable[[], Mapping[str, str]],
                 secret: str | None, clock: Callable[[], float] = time.time) -> None:
        """`card_agents` maps card id -> agent id. `secret` is the shared secret Airwallex returned when
        remote auth was configured; None only for local tests, where signatures are not checked."""
        self.get_warden = get_warden
        self.card_agents = card_agents
        self.secret = secret
        self.clock = clock
        self._answers: dict[str, dict[str, Any]] = {}  # event id -> answer, so a retry gets the same one

    def handle(self, headers: Mapping[str, str], body: bytes) -> tuple[int, dict[str, Any]]:
        started = time.perf_counter()
        if self.secret is not None:
            problem = verify_signature(self.secret, headers, self.clock())
            if problem:
                return 401, {"error": problem}
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return 400, {"error": "body is not JSON"}
        answer = self.decide(payload, started)
        return 200, answer

    def decide(self, payload: Mapping[str, Any], started: float | None = None) -> dict[str, Any]:
        started = started if started is not None else time.perf_counter()
        version = int(payload.get("version") or 2)
        id_field = "card_transaction_event_id" if version >= 2 else "transaction_id"
        event_id = str(payload.get(id_field) or "")
        if event_id and event_id in self._answers:
            return self._answers[event_id]

        warden = self.get_warden()
        card_id = str(payload.get("card_id") or "")
        agent_id = self.card_agents().get(card_id)
        kind = str(payload.get("transaction_type") or "").upper()
        currency = str(payload.get("transaction_currency") or "").upper()
        merchant = payload.get("merchant") or {}
        mcc = str(merchant.get("category_code") or "")
        try:
            amount = abs(Decimal(str(payload.get("transaction_amount")))).quantize(Decimal("0.01"))
        except (InvalidOperation, TypeError):
            amount = None

        intent = None
        if agent_id is None:
            status, reason = "DECLINED", "Card is not managed by Warden."
        elif kind in CREDIT_TYPES:
            status, reason = "AUTHORIZED", "Refund or reversal: money back to the company."
        elif warden.trust.is_contained(agent_id):
            status, reason = "DECLINED", f"{agent_id} is contained; its card authorizes nothing."
        elif amount is None:
            status, reason = "DECLINED", "No readable amount."
        else:
            intent = warden.intents.claim(agent_id, amount, currency, mcc, event_id)
            if intent is None:
                status, reason = "DECLINED", ("No Intent Receipt: Warden never approved this charge "
                                              f"({amount} {currency}, merchant category {mcc or '?'}).")
            else:
                status, reason = "AUTHORIZED", f"Matches Warden-approved payment {intent.proposal_id}."

        ms = round((time.perf_counter() - started) * 1000, 1)
        warden.audit.append("remote_authorization", agent_id=agent_id, card_id=card_id, event_id=event_id,
                            card_transaction_id=payload.get("card_transaction_id"),
                            amount=None if amount is None else str(amount), currency=currency,
                            merchant_name=merchant.get("name"), merchant_category=mcc, transaction_type=kind,
                            response_status=status, reason=reason,
                            proposal_id=intent.proposal_id if intent else None, decided_in_ms=ms)
        answer = {id_field: event_id, "response_status": status, "status_reason": reason[:200]}
        if event_id:
            self._answers[event_id] = answer
        return answer


# --- CLI ---------------------------------------------------------------------------------------

def _client():
    from warden.config import load_settings
    from warden.rails.airwallex import AirwallexClient

    s = load_settings()
    return AirwallexClient(s.airwallex_base_url, s.airwallex_client_id, s.airwallex_api_key, s.airwallex_api_version)


def _save_secret(secret: str) -> None:
    from warden.config import ROOT

    env = ROOT / ".env"
    lines = [l for l in env.read_text(encoding="utf-8-sig").splitlines() if not l.startswith("AIRWALLEX_REMOTE_AUTH_SECRET=")]
    lines.append(f"AIRWALLEX_REMOTE_AUTH_SECRET={secret}")
    env.write_text("\n".join(lines) + "\n", encoding="utf-8")


def selftest(url: str, card_id: str, secret: str | None) -> None:
    """Signed requests against a running Warden: an unknown charge must be declined fast."""
    import httpx

    payload = {"version": 2, "card_id": card_id, "card_transaction_event_id": f"selftest_{secrets.token_hex(6)}",
               "card_transaction_id": "selftest", "transaction_type": "AUTHORIZATION", "transaction_amount": 180,
               "transaction_currency": "USD", "merchant": {"name": "OUT OF BAND TEST", "category_code": "5943"}}
    nonce = f"{int(time.time() * 1000)}.{secrets.token_urlsafe(12)}"
    headers = {"x-nonce": nonce, "x-signature": sign(secret, nonce) if secret else "unsigned"}
    t = time.perf_counter()
    r = httpx.post(url, json=payload, headers=headers, timeout=5)
    print(f"{r.status_code} in {(time.perf_counter() - t) * 1000:.0f} ms: {r.text}")


def main(argv: list[str] | None = None) -> int:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status", help="show the sandbox's remote authorization settings")
    enable = sub.add_parser("enable", help="point Airwallex's remote authorization at Warden")
    enable.add_argument("--url", required=True, help="public HTTPS URL ending in /airwallex/remote-auth")
    test = sub.add_parser("selftest", help="send a signed out-of-band charge to a running Warden")
    test.add_argument("--url", default="http://127.0.0.1:8000/airwallex/remote-auth")
    test.add_argument("--card", default="selftest-card")
    args = parser.parse_args(argv)

    if args.command == "status":
        settings = _client().get("/api/v1/issuing/config").get("remote_auth_settings")
        print(json.dumps(settings, indent=2) if settings else
              "Remote authorization is not set up on this account (no remote_auth_settings). "
              "Airwallex has to enable it first.")
    elif args.command == "enable":
        if not args.url.startswith("https://"):
            raise SystemExit("Airwallex needs an HTTPS URL")
        data = _client().post("/api/v1/issuing/config/update", {"remote_auth": {
            "enabled": True, "url": args.url, "default_action": "DECLINED", "new_shared_secret": True}})
        secret = json.dumps(data).split('"shared_secret": "', 1)[-1].split('"', 1)[0] if "shared_secret" in json.dumps(data) else ""
        if secret:
            _save_secret(secret)
            print("Remote authorization enabled; shared secret saved to .env (AIRWALLEX_REMOTE_AUTH_SECRET).")
        else:
            print("Airwallex answered without a shared secret:", json.dumps(data)[:500])
    else:
        from warden.config import load_settings

        load_settings()
        selftest(args.url, args.card, os.environ.get("AIRWALLEX_REMOTE_AUTH_SECRET") or None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
