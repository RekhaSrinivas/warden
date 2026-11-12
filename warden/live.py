"""Warden wired to the real rails: Airwallex sandbox, Claude, Metal testnet and (once allowlisted) Airi.

Provisioning runs once and is cached in data/live_state.json, so reruns reuse the
same beneficiaries, card and mandate. `reset=True` issues a fresh card and binds a
fresh Metal mandate, because containment freezes and revokes the old ones on purpose.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from warden import demo_world as w
from warden.audit import AuditLog
from warden.config import ROOT, Settings, load_settings
from warden.engine import Warden
from warden.mandate import Mandate
from warden.rails.airi import AiriRail
from warden.rails.airwallex import AirwallexClient, AirwallexError, AirwallexRail, SimulationAdapter
from warden.rails.base import RailError
from warden.rails.cli import CliRunner, tail
from warden.rails.metal import MetalRail
from warden.risk import ClaudeRiskReader

STATE_PATH = ROOT / "data" / "live_state.json"
SECRETS = ROOT / "secrets"
METAL_OWNER_KEY = SECRETS / "metal-owner-key.json"
# The pinned copy: metal-agent's own install pairs x402 packages that break `pay`.
METAL_ENTRY = ROOT / "tools" / "metal" / "node_modules" / "@metalntwx" / "metal-agent" / "dist" / "index.js"
# airi-cli ships only for macOS and Linux, so it runs inside WSL.
AIRI_COMMAND = ("wsl", "-d", "Ubuntu", "-e", "/home/chres/.local/bin/airi-cli")

# Fake but well-formed US bank details for the three approved vendors.
VENDOR_ACCOUNTS = {"ben_acme": "50001121", "ben_globex": "50001122", "ben_initech": "50001123"}
METAL_CAP, METAL_BUDGET = "0.05", "1"


@dataclass
class LiveWarden:
    warden: Warden
    airwallex: AirwallexRail
    metal: MetalRail
    airi: AiriRail | None
    state: dict[str, Any]


def build(reset: bool = False, settings: Settings | None = None) -> LiveWarden:
    settings = settings or load_settings()
    if not (settings.has_airwallex and settings.has_claude and settings.metal_rpc_url):
        raise SystemExit("Live mode needs AIRWALLEX_*, ANTHROPIC_API_KEY and METAL_RPC_URL in .env")
    state = _load_state()
    mandate = w.demo_mandate()

    client = AirwallexClient(settings.airwallex_base_url, settings.airwallex_client_id, settings.airwallex_api_key,
                             settings.airwallex_api_version)
    simulation = SimulationAdapter(client)
    _provision_airwallex(client, mandate, state, fresh_card=reset)
    airwallex = AirwallexRail(client, simulation, {w.AGENT: state["card_id"]}, payees=state["beneficiaries"],
                              settle_transfers=True)

    metal_cli = metal_runner(settings)
    _provision_metal(metal_cli, state, fresh_mandate=reset)
    metal = MetalRail(metal_cli, {w.AGENT: ROOT / state["metal"]["credential"]}, METAL_OWNER_KEY)
    _save_state(state)

    rails: dict[str, Any] = {"transfer": airwallex, "card": airwallex, "x402": metal}
    airi = _airi_rail()
    if airi is not None:
        rails["vic_checkout"] = airi

    warden = Warden(mandate, ClaudeRiskReader(w.VENDOR_DOMAINS), rails, audit=AuditLog(settings.audit_path),
                    balance=lambda: airwallex.available_balance("USD"))
    return LiveWarden(warden, airwallex, metal, airi, state)


def metal_runner(settings: Settings) -> CliRunner:
    return CliRunner(["node", str(METAL_ENTRY)], env={"METAL_RPC_URL": settings.metal_rpc_url}, timeout=180)


# --- Airwallex ----------------------------------------------------------------------

def card_controls(mandate: Mandate) -> dict[str, Any]:
    """The mandate's card rules, enforced a second time by Airwallex itself."""
    return {
        "allowed_transaction_count": "MULTIPLE",
        "allowed_currencies": sorted(mandate.currencies),
        "allowed_merchant_categories": sorted(mandate.allowed_mccs),
        "transaction_limits": {"currency": "USD", "limits": [
            {"amount": float(mandate.per_txn_cap), "interval": "PER_TRANSACTION"},
            {"amount": float(mandate.weekly_cap), "interval": "WEEKLY"},
        ]},
    }


def beneficiary_body(vendor: str, account_number: str) -> dict[str, Any]:
    return {
        "beneficiary": {
            "entity_type": "COMPANY",
            "address": {"city": "Seattle", "country_code": "US", "postcode": "98104", "state": "Washington",
                        "street_address": "412 5th Avenue"},
            "bank_details": {
                "account_currency": "USD", "account_name": vendor, "account_number": account_number,
                "account_routing_type1": "aba", "account_routing_value1": "021000021",
                "bank_account_category": "Checking",  # required for US ACH
                "bank_country_code": "US", "local_clearing_system": "ACH",
            },
        },
        "transfer_methods": ["LOCAL"],
    }


def _provision_airwallex(client: AirwallexClient, mandate: Mandate, state: dict[str, Any], fresh_card: bool) -> None:
    beneficiaries = state.setdefault("beneficiaries", {})
    for vendor, (logical_id, _) in w.VENDORS.items():
        if logical_id not in beneficiaries:
            data = client.post("/api/v1/beneficiaries/create", beneficiary_body(vendor, VENDOR_ACCOUNTS[logical_id]))
            beneficiaries[logical_id] = data.get("id") or data["beneficiary_id"]

    if "cardholder_id" not in state:
        data = client.post("/api/v1/issuing/cardholders/create",
                           {"type": "DELEGATE", "email": f"warden.agent+{int(time.time())}@example.com"})
        state["cardholder_id"] = data["cardholder_id"]

    if fresh_card or "card_id" not in state:
        if "card_id" in state:
            state.setdefault("retired_cards", []).append(state["card_id"])
        data = client.post("/api/v1/issuing/cards/create", {
            "request_id": str(uuid.uuid4()), "cardholder_id": state["cardholder_id"], "created_by": "Warden",
            "form_factor": "VIRTUAL", "is_personalized": False, "program": {"purpose": "COMMERCIAL"},
            "authorization_controls": card_controls(mandate),
        })
        state["card_id"] = data.get("card_id") or data["id"]
        _wait_active(client, state["card_id"])


def _wait_active(client: AirwallexClient, card_id: str) -> None:
    for _ in range(15):
        if client.get(f"/api/v1/issuing/cards/{card_id}").get("card_status") == "ACTIVE":
            return
        time.sleep(2)
    raise RailError(f"Card {card_id} did not become ACTIVE")


# --- Metal --------------------------------------------------------------------------

_ACCOUNT = re.compile(r"^account\s+(0x[0-9a-fA-F]{40})", re.M)
_MANDATE = re.compile(r"^mandate\s+(0x[0-9a-fA-F]+)", re.M)
_TX = re.compile(r"^transaction\s+(0x[0-9a-fA-F]+)", re.M)
_EXPLORER = re.compile(r"^explorer\s+(\S+)", re.M)


def _provision_metal(cli: CliRunner, state: dict[str, Any], fresh_mandate: bool) -> None:
    metal = state.setdefault("metal", {})
    if "account" not in metal:
        out = _run(cli, "account", "create", "--key", str(METAL_OWNER_KEY), "--network", "testnet")
        metal["account"] = _ACCOUNT.search(out).group(1)
    if fresh_mandate or "credential" not in metal:
        if "credential" in metal:
            metal.setdefault("retired_mandates", []).append(metal["mandate_id"])
        credential = SECRETS / f"mandate-{int(time.time())}.json"
        out = _run(cli, "mandate", "credential", "create", "--principal", metal["account"], "--cap", METAL_CAP,
                   "--budget", METAL_BUDGET, "--out", str(credential), "--key", str(METAL_OWNER_KEY),
                   "--network", "testnet", "--payee", f"{w.DATA_API_PAYEE},VendorCheck API",
                   "--description", "Warden buyer-agent: VendorCheck data API only")
        mandate_id = _MANDATE.search(out).group(1)
        bound = _run(cli, "mandate", "bind", "--credential", str(credential), "--key", str(METAL_OWNER_KEY),
                     "--network", "testnet")
        metal.update(credential=str(credential.relative_to(ROOT)), mandate_id=mandate_id,
                     bind_tx=_TX.search(bound).group(1), bind_explorer=_EXPLORER.search(bound).group(1))


def _run(cli: CliRunner, *args: str) -> str:
    result = cli(*args)
    if result.returncode != 0:
        raise RailError(f"metal-agent {' '.join(args[:2])} failed: {tail(result)}")
    return result.stdout or ""


# --- Airi ---------------------------------------------------------------------------

def _airi_rail() -> AiriRail | None:
    """Airi joins only once the account is allowlisted and a payment method id is configured."""
    method = os.environ.get("AIRI_PAYMENT_METHOD_ID", "").strip()
    if not method:
        return None
    return AiriRail(CliRunner(AIRI_COMMAND), method, SECRETS / "airi")


# --- state --------------------------------------------------------------------------

def _load_state() -> dict[str, Any]:
    return json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def direct_card_charge(lw: LiveWarden, amount: str, mcc: str) -> dict[str, Any]:
    """Charge the agent's card without asking Warden: proves Airwallex's own controls still hold."""
    try:
        data = lw.airwallex.simulation.card_authorization(lw.state["card_id"], Decimal(amount), "USD", mcc)
    except AirwallexError as e:
        return {"status": "ERROR", "error": e.summary()}
    return {"card_id": lw.state["card_id"], "card_transaction_id": data.get("card_transaction_id"),
            "result": data.get("process_result") or data.get("status"), "failure_reason": data.get("failure_reason")}
