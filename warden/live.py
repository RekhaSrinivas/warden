"""Warden wired to the real rails: Airwallex sandbox, Claude, Metal testnet and (once allowlisted) Airi.

Each hired agent gets its own DELEGATE cardholder, its own virtual card whose controls
mirror its mandate, and its own Metal mandate bound on chain. Shared setup (vendor
beneficiaries, the Metal owner account) is cached in data/live_state.json; agents are
in data/agents.json. Containing one agent freezes and revokes only its own credentials.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from warden import demo_world as w
from warden import hire as h
from warden.agents import AgentRecord, AgentRegistry
from warden.audit import AuditLog
from warden.config import ROOT, Settings, load_settings
from warden.engine import Warden
from warden.mandate import Mandate
from warden.rails.airi import AiriRail
from warden.rails.airwallex import AirwallexClient, AirwallexError, AirwallexRail, SimulationAdapter, card_ids
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
# On-chain limits per agent, in *USDZ: tighter than the mandate, since x402 calls cost cents.
METAL_LIMITS = {w.PROCUREMENT: ("0.05", "0.5"), w.RESEARCH: ("0.05", "1")}
DEFAULT_METAL_LIMITS = ("0.05", "0.5")

ROLES = {w.PROCUREMENT: ("procurement", w.PROCUREMENT_INSTRUCTION), w.RESEARCH: ("research", w.RESEARCH_INSTRUCTION)}


@dataclass
class LiveWarden:
    warden: Warden
    airwallex: AirwallexRail
    metal: MetalRail
    airi: AiriRail | None
    state: dict[str, Any]
    registry: AgentRegistry
    provisioner: LiveProvisioner
    hires: list[dict[str, Any]] = field(default_factory=list)  # what each hire drafted, changed and provisioned


def build(fresh: bool = False, settings: Settings | None = None) -> LiveWarden:
    """`fresh=True` archives the old audit log and hires both agents again with new cards and mandates
    (the last run froze and revoked the old ones on purpose). Otherwise the hired agents are reused."""
    settings = settings or load_settings()
    if not (settings.has_airwallex and settings.has_claude and settings.metal_rpc_url):
        raise SystemExit("Live mode needs AIRWALLEX_*, ANTHROPIC_API_KEY and METAL_RPC_URL in .env")
    state = _load_state()
    client = AirwallexClient(settings.airwallex_base_url, settings.airwallex_client_id, settings.airwallex_api_key,
                             settings.airwallex_api_version)
    simulation = SimulationAdapter(client)
    metal_cli = metal_runner(settings)
    _provision_shared(client, metal_cli, state)
    _save_state(state)

    airwallex = AirwallexRail(client, simulation, {}, payees=state["beneficiaries"], settle_transfers=True)
    metal = MetalRail(metal_cli, {}, METAL_OWNER_KEY)
    rails: dict[str, Any] = {"transfer": airwallex, "card": airwallex, "x402": metal}
    airi = _airi_rail()
    if airi is not None:
        rails["vic_checkout"] = airi

    if fresh and settings.audit_path.exists():
        settings.audit_path.rename(settings.audit_path.with_name(f"audit-{int(time.time())}.jsonl"))
    warden = Warden(w.demo_mandate(), ClaudeRiskReader(w.VENDOR_DOMAINS), rails, audit=AuditLog(settings.audit_path),
                    balance=lambda: airwallex.available_balance("USD"))
    registry = AgentRegistry()
    lw = LiveWarden(warden, airwallex, metal, airi, state, registry, LiveProvisioner(client, metal_cli, state))
    if fresh:
        for record in registry.active():
            registry.fire(record.agent_id)
    else:
        for record in registry.active():
            attach(lw, record)
        warden.restore()  # trust, containment and spend from the existing audit log
    return lw


def build_for_dashboard() -> LiveWarden:
    """Reuse the hired agents (as the live demo left them), hiring the demo pair if there are none yet."""
    lw = build()
    for agent_id, (role, instruction) in ROLES.items():
        if agent_id not in lw.registry.agents or lw.registry.agents[agent_id].status != "active":
            hire_agent(lw, agent_id, role, instruction, confirm=True)
    return lw


def build_live_warden() -> Warden:
    return build_for_dashboard().warden


def hire_agent(lw: LiveWarden, agent_id: str, role: str, instruction: str, confirm: bool,
               org: h.OrgPolicy = h.OrgPolicy()) -> dict[str, Any]:
    """Claude drafts, code validates, and code provisions only once confirmed."""
    draft = h.draft_mandate(instruction)
    validated = h.validate(draft, org, agent_id, instruction)
    return commit_hire(lw, agent_id, role, instruction, draft, validated, confirm)


def commit_hire(lw: LiveWarden, agent_id: str, role: str, instruction: str, draft: h.MandateDraft,
                validated: h.Validated, confirm: bool) -> dict[str, Any]:
    """Provision exactly the mandate the manager was shown (the dashboard drafts first, then confirms)."""
    provisioning = h.provision(agent_id, validated, lw.provisioner, confirm=confirm)
    record = AgentRecord.hired(agent_id, role, validated.mandate, provisioning)
    lw.registry.add(record)
    lw.warden.hire(agent_id, validated.mandate, role=role, provisioning=provisioning)
    attach(lw, record)
    hired = {"agent_id": agent_id, "role": role, "instruction": instruction, "draft": draft.to_dict(),
             "changes": validated.changes, "mandate": record.mandate, "mandate_hash": record.mandate_hash,
             "provisioning": provisioning}
    lw.hires.append(hired)
    return hired


def attach(lw: LiveWarden, record: AgentRecord) -> None:
    """Point the rails at this agent's own card and Metal mandate."""
    lw.warden.mandates[record.agent_id] = record.to_mandate()
    if record.provisioning.get("card_id"):
        lw.airwallex.agent_cards[record.agent_id] = record.provisioning["card_id"]
    if record.provisioning.get("metal_credential"):
        lw.metal.mandates[record.agent_id] = ROOT / record.provisioning["metal_credential"]


def metal_runner(settings: Settings) -> CliRunner:
    return CliRunner(["node", str(METAL_ENTRY)], env={"METAL_RPC_URL": settings.metal_rpc_url}, timeout=180)


# --- provisioning -------------------------------------------------------------------

class LiveProvisioner:
    """Issues one agent's card (controls = its mandate) and binds its Metal mandate."""

    def __init__(self, client: AirwallexClient, metal_cli: CliRunner, state: dict[str, Any]) -> None:
        self.client = client
        self.metal_cli = metal_cli
        self.state = state

    def provision(self, agent_id: str, mandate: Mandate) -> dict[str, Any]:
        ids: dict[str, Any] = {}
        if mandate.allowed_mccs:
            data = self.client.post("/api/v1/issuing/cardholders/create",
                                    {"type": "DELEGATE", "email": f"{agent_id}+{int(time.time())}@example.com"})
            ids["cardholder_id"] = data["cardholder_id"]
            data = self.client.post("/api/v1/issuing/cards/create", {
                "request_id": str(uuid.uuid4()), "cardholder_id": ids["cardholder_id"], "created_by": "Warden",
                "form_factor": "VIRTUAL", "is_personalized": False, "program": {"purpose": "COMMERCIAL"},
                "authorization_controls": card_controls(mandate),
            })
            ids["card_id"] = data.get("card_id") or data["id"]
            _wait_active(self.client, ids["card_id"])
        if w.DATA_API_PAYEE in mandate.payee_allowlist:
            ids.update(self._metal_mandate(agent_id))
        return ids

    def _metal_mandate(self, agent_id: str) -> dict[str, Any]:
        cap, budget = METAL_LIMITS.get(agent_id, DEFAULT_METAL_LIMITS)
        credential = SECRETS / f"mandate-{agent_id}-{int(time.time())}.json"
        out = _run(self.metal_cli, "mandate", "credential", "create", "--principal", self.state["metal"]["account"],
                   "--cap", cap, "--budget", budget, "--out", str(credential), "--key", str(METAL_OWNER_KEY),
                   "--network", "testnet", "--payee", f"{w.DATA_API_PAYEE},VendorCheck API",
                   "--description", f"Warden {agent_id}: VendorCheck data API only")
        mandate_id = _MANDATE.search(out).group(1)
        bound = _run(self.metal_cli, "mandate", "bind", "--credential", str(credential), "--key", str(METAL_OWNER_KEY),
                     "--network", "testnet")
        return {"metal_credential": str(credential.relative_to(ROOT)).replace("\\", "/"),
                "metal_mandate_id": mandate_id, "metal_cap_usdz": cap, "metal_budget_usdz": budget,
                "metal_bind_tx": _TX.search(bound).group(1), "metal_bind_explorer": _EXPLORER.search(bound).group(1)}


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


def _provision_shared(client: AirwallexClient, metal_cli: CliRunner, state: dict[str, Any]) -> None:
    beneficiaries = state.setdefault("beneficiaries", {})
    for vendor, (logical_id, _) in w.VENDORS.items():
        if logical_id not in beneficiaries:
            data = client.post("/api/v1/beneficiaries/create", beneficiary_body(vendor, VENDOR_ACCOUNTS[logical_id]))
            beneficiaries[logical_id] = data.get("id") or data["beneficiary_id"]
    metal = state.setdefault("metal", {})
    if "account" not in metal:
        out = _run(metal_cli, "account", "create", "--key", str(METAL_OWNER_KEY), "--network", "testnet")
        metal["account"] = _ACCOUNT.search(out).group(1)


def _wait_active(client: AirwallexClient, card_id: str) -> None:
    for _ in range(15):
        if client.get(f"/api/v1/issuing/cards/{card_id}").get("card_status") == "ACTIVE":
            return
        time.sleep(2)
    raise RailError(f"Card {card_id} did not become ACTIVE")


_ACCOUNT = re.compile(r"^account\s+(0x[0-9a-fA-F]{40})", re.M)
_MANDATE = re.compile(r"^mandate\s+(0x[0-9a-fA-F]+)", re.M)
_TX = re.compile(r"^transaction\s+(0x[0-9a-fA-F]+)", re.M)
_EXPLORER = re.compile(r"^explorer\s+(\S+)", re.M)


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


# --- prove and recover ------------------------------------------------------------------

def issue_receipts(lw: LiveWarden) -> dict[str, Any]:
    """Sign a receipt for every final decision not yet receipted, then check the whole chain."""
    from warden import receipts as rc

    new = rc.sync(lw.warden.audit.path)
    all_receipts = rc.load()
    broken = rc.verify_chain(all_receipts, rc.load_public_key())
    return {"issued": len(new), "total": len(all_receipts), "chain_intact": broken is None, "broken_at": broken}


def reconcile_and_recover(lw: LiveWarden, settings: Settings | None = None) -> list[Any]:
    """Match every debit on the agents' cards to a receipt; dispute the ones no human authorised."""
    import anthropic

    from warden import receipts as rc
    from warden import recover as rv

    settings = settings or load_settings()
    client = lw.airwallex.client
    cards = dict(lw.airwallex.agent_cards)
    contained = {a for a in cards if lw.warden.trust.is_contained(a)}
    summarizer = rv.ClaudeCaseSummarizer(anthropic.Anthropic(api_key=settings.anthropic_api_key))
    cases = rv.run(client, rv.EvidenceUploader(client), cards, rc.load(), rv.load_cases(), contained,
                   summarizer, rv.SandboxSettlement(client))
    rv.save_cases(cases)
    return [c for c in cases if c.agent_id in cards]


# --- Warden bypassed: payments made straight to a rail ----------------------------------

def direct_card_charge(lw: LiveWarden, agent_id: str, amount: str, mcc: str) -> dict[str, Any]:
    """Charge an agent's card without asking Warden. Proves Airwallex's own controls, and is also how a
    hacked agent with copied card details would spend out of band. The charge is left authorized:
    clearing and any dispute are the recovery step's job."""
    card_id = lw.airwallex.agent_cards[agent_id]
    try:
        data = lw.airwallex.simulation.card_authorization(card_id, Decimal(amount), "USD", mcc)
    except AirwallexError as e:
        return {"status": "ERROR", "error": e.summary(), "card_id": card_id}
    ids = card_ids(card_id, data)
    return {**ids, "agent_id": agent_id, "amount": amount, "currency": "USD", "merchant_category_code": mcc,
            "result": data.get("process_result") or data.get("status")}


def direct_metal_payment(lw: LiveWarden, agent_id: str) -> dict[str, Any]:
    """Pay the data API under an agent's Metal mandate without asking Warden."""
    credential: Path = lw.metal.mandates[agent_id]
    result = lw.metal.cli("pay", w.DATA_API_URL, "--mandate", str(credential), "--network", lw.metal.network)
    output = (result.stdout or "") + (result.stderr or "")
    refused = re.search(r"payment refused:\s*([a-z_]+)", output)
    tx = re.search(r"transaction\s+(0x[0-9a-fA-F]+)", output)
    return {"agent_id": agent_id, "credential": str(credential.relative_to(ROOT)).replace("\\", "/"),
            "reason": refused.group(1) if refused else None, "transaction": tx.group(1) if tx else None,
            "result": "REFUSED" if refused else ("PAID" if result.returncode == 0 else "ERROR"),
            "output": tail(result)}
