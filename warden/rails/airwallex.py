"""Airwallex rail: bank transfers to allowlisted beneficiaries and the agent's virtual card.

Endpoints and field names match scripts/smoke_test.py, which checks them against
the sandbox. Every sandbox-only call goes through `SimulationAdapter`, so going
live means replacing that one class and nothing in the decision logic.
"""

from __future__ import annotations

import json
import time
from decimal import Decimal
from typing import Any, Mapping

import httpx

from warden.models import Proposal
from warden.rails.base import RailDeclined, RailError


class AirwallexError(Exception):
    def __init__(self, method: str, path: str, status: int, body: Any) -> None:
        self.method, self.path, self.status, self.body = method, path, status, body
        super().__init__(self.summary())

    @property
    def code(self) -> str:
        return str(self.body.get("code")) if isinstance(self.body, dict) and self.body.get("code") else f"HTTP_{self.status}"

    def summary(self) -> str:
        message = self.body.get("message") if isinstance(self.body, dict) else str(self.body)[:200]
        return f"{self.method} {self.path} -> HTTP {self.status} {self.code}: {message}"


class AirwallexClient:
    """Bearer-token client. Logs in lazily and refreshes before the 30-minute token expires."""

    def __init__(self, base_url: str, client_id: str, api_key: str, api_version: str | None = None,
                 http: httpx.Client | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._api_key = api_key
        self.api_version = api_version
        self.http = http or httpx.Client(timeout=30)
        self._token: str | None = None
        self._token_expires = 0.0

    def login(self) -> None:
        data = self._send("POST", "/api/v1/authentication/login", auth=False,
                          headers={"x-client-id": self._client_id, "x-api-key": self._api_key})
        self._token = data["token"]
        self._token_expires = time.time() + 25 * 60

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return self._send("GET", path, params=params)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        return self._send("POST", path, body=body if body is not None else {})

    def _send(self, method: str, path: str, body: dict[str, Any] | None = None, params: dict[str, Any] | None = None,
              headers: dict[str, str] | None = None, auth: bool = True) -> Any:
        hdrs = {"Accept": "application/json", "User-Agent": "warden/0.1"}
        if self.api_version:
            hdrs["x-api-version"] = self.api_version
        if auth:
            if not self._token or time.time() > self._token_expires:
                self.login()
            hdrs["Authorization"] = f"Bearer {self._token}"
        hdrs.update(headers or {})
        try:
            resp = self.http.request(method, self.base_url + path, json=body, params=params, headers=hdrs)
        except httpx.HTTPError as e:
            raise RailError(f"Airwallex unreachable: {type(e).__name__}") from None
        try:
            data = resp.json() if resp.content else {}
        except json.JSONDecodeError:
            data = resp.text
        if resp.status_code >= 400:
            raise AirwallexError(method, path, resp.status_code, data)
        return data


class SimulationAdapter:
    """Sandbox-only calls: funding, card authorizations, transfer state changes."""

    def __init__(self, client: AirwallexClient) -> None:
        self.client = client

    def deposit(self, global_account_id: str, amount: Decimal) -> dict[str, Any]:
        return self.client.post("/api/v1/simulation/deposit/create", {
            "amount": float(amount), "global_account_id": global_account_id, "payer_name": "Warden demo",
            "payer_country": "US", "reference": "warden demo funding", "status": "SETTLED",
        })

    def card_authorization(self, card_id: str, amount: Decimal, currency: str, mcc: str) -> dict[str, Any]:
        return self.client.post("/api/v1/simulation/issuing/create", {
            "card_id": card_id, "transaction_amount": float(amount),
            "transaction_currency": currency, "merchant_category_code": mcc,
        })

    def transfer_transition(self, transfer_id: str, next_status: str, failure_type: str | None = None) -> dict[str, Any]:
        body = {"next_status": next_status}
        if failure_type:
            body["failure_type"] = failure_type
        return self.client.post(f"/api/v1/simulation/transfers/{transfer_id}/transition", body)


class AirwallexRail:
    name = "airwallex"

    def __init__(self, client: AirwallexClient, simulation: SimulationAdapter, agent_cards: Mapping[str, str]) -> None:
        self.client = client
        self.simulation = simulation
        self.agent_cards = dict(agent_cards)

    def execute(self, proposal: Proposal) -> dict[str, Any]:
        if proposal.rail == "transfer":
            return self._transfer(proposal)
        if proposal.rail == "card":
            return self._card_purchase(proposal)
        raise RailError(f"Airwallex rail cannot execute {proposal.rail!r} proposals")

    def _transfer(self, p: Proposal) -> dict[str, Any]:
        try:
            data = self.client.post("/api/v1/transfers/create", {
                "request_id": p.request_id,  # Airwallex refuses a reused request_id for 7 days
                "beneficiary_id": p.payee_id,
                "transfer_amount": str(p.amount),
                "transfer_currency": p.currency,
                "source_currency": p.currency,
                "transfer_method": "LOCAL",
                "reason": p.extra.get("reason", "goods_purchased"),
                "reference": p.extra.get("reference", f"Warden {p.id[-8:]}"),
            })
        except AirwallexError as e:
            raise _decline_or_error(e) from None
        return {"rail": self.name, "transfer_id": data["id"], "status": data.get("status"), "request_id": p.request_id}

    def _card_purchase(self, p: Proposal) -> dict[str, Any]:
        card_id = self.agent_cards.get(p.agent_id)
        if not card_id:
            raise RailError(f"No card issued to agent {p.agent_id}")
        try:
            data = self.simulation.card_authorization(card_id, p.amount, p.currency, p.merchant_category)
        except AirwallexError as e:
            raise _decline_or_error(e) from None
        result = data.get("process_result") or data.get("status")
        if result not in ("APPROVED", "PENDING"):
            raise RailDeclined(str(data.get("failure_reason") or result), {"card_id": card_id})
        return {"rail": self.name, "card_id": card_id, "card_transaction_id": data.get("card_transaction_id"),
                "status": result, "simulated_authorization": True}

    def contain(self, agent_id: str) -> dict[str, Any]:
        card_id = self.agent_cards.get(agent_id)
        if not card_id:
            return {"rail": self.name, "card": "none issued"}
        try:
            self.client.post(f"/api/v1/issuing/cards/{card_id}/update", {"card_status": "INACTIVE"})
            status = self.client.get(f"/api/v1/issuing/cards/{card_id}").get("card_status")
        except AirwallexError as e:
            raise RailError(e.summary()) from None
        return {"rail": self.name, "card_id": card_id, "card_status": status}

    def available_balance(self, currency: str) -> Decimal | None:
        data = self.client.get("/api/v1/balances/current")
        items = data if isinstance(data, list) else data.get("items", [])
        for b in items:
            if b.get("currency") == currency:
                return Decimal(str(b.get("available_amount", 0)))
        return None


def _decline_or_error(e: AirwallexError) -> Exception:
    """A 4xx is Airwallex saying no to this payment; anything else is an outage."""
    if 400 <= e.status < 500:
        return RailDeclined(e.code, {"error": e.summary()})
    return RailError(e.summary())
