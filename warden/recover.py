r"""Reconcile and recover: find card charges no human authorised, then dispute them.

reconcile(): every debit on an agent's card must match an Intent Receipt. One that
doesn't (say, a copied card used outside Warden) becomes a Case. A missing receipt
is evidence of an unexplained charge, not proof of fraud, so each Case moves
through explicit states:

    UNATTRIBUTED -> INVESTIGATING -> DISPUTE_SUBMITTED -> RECOVERED | LOST
                                  \-> NOT_ELIGIBLE

Claude writes the case summary; code alone decides to dispute, and only when there
is no receipt, no human approval, and the charge has cleared. A submitted dispute is
never counted as money back: RECOVERED needs Airwallex to report the dispute WON.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

import anthropic

from warden import receipts as rc
from warden.config import ROOT

CASES_PATH = ROOT / "data" / "cases.json"
UPLOAD_NOT_PERMITTED = "evidence upload not permitted: add Files: Write to the API key"
DISPUTE_REASON = "UNAUTHORIZED_TRANSACTION"


class CaseState(StrEnum):
    UNATTRIBUTED = "UNATTRIBUTED"
    INVESTIGATING = "INVESTIGATING"
    NOT_ELIGIBLE = "NOT_ELIGIBLE"
    DISPUTE_SUBMITTED = "DISPUTE_SUBMITTED"
    RECOVERED = "RECOVERED"
    LOST = "LOST"


# Airwallex dispute status -> case state. SUBMITTED/IN_PROGRESS stay "submitted": nothing is back yet.
_DISPUTE_STATES = {"SUBMITTED": CaseState.DISPUTE_SUBMITTED, "IN_PROGRESS": CaseState.DISPUTE_SUBMITTED,
                   "WON": CaseState.RECOVERED, "LOST": CaseState.LOST, "REJECTED": CaseState.LOST,
                   "CANCELED": CaseState.LOST, "EXPIRED": CaseState.LOST}


@dataclass
class Case:
    card_transaction_id: str
    agent_id: str
    card_id: str
    amount: str
    currency: str
    merchant_name: str = ""
    merchant_category: str = ""
    transacted_at: str = ""
    lifecycle_id: str | None = None
    clearing_event_id: str | None = None
    cleared: bool = False
    state: CaseState = CaseState.UNATTRIBUTED
    summary: str = ""
    reason: str = ""
    dispute_id: str | None = None
    dispute_status: str | None = None
    evidence_file_id: str | None = None
    history: list[dict[str, Any]] = field(default_factory=list)

    @property
    def case_id(self) -> str:
        return "case_" + hashlib.sha256(self.card_transaction_id.encode()).hexdigest()[:12]

    def move(self, state: CaseState, note: str) -> None:
        self.state, self.reason = state, note
        self.history.append({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "state": str(state), "note": note})

    def to_json(self) -> dict[str, Any]:
        return {"case_id": self.case_id, **asdict(self), "state": str(self.state)}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Case:
        fields = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        fields["state"] = CaseState(fields.get("state", CaseState.UNATTRIBUTED))
        return cls(**fields)


# --- reconcile ------------------------------------------------------------------------


class AirwallexApi(Protocol):
    def get(self, path: str, params: dict[str, Any] | None = None) -> Any: ...
    def post(self, path: str, body: dict[str, Any] | None = None) -> Any: ...


def authorised_ids(receipts: Iterable[rc.Receipt]) -> set[str]:
    """Card transaction and lifecycle ids that some executed receipt vouches for."""
    ids: set[str] = set()
    for r in receipts:
        if (r.body.get("decision") or {}).get("action") != "ALLOW":
            continue
        execution = r.body.get("execution") or {}
        ids.update(str(execution[k]) for k in ("card_transaction_id", "lifecycle_id") if execution.get(k))
    return ids


def card_debits(client: AirwallexApi, card_id: str) -> dict[str, list[dict[str, Any]]]:
    """Approved events per card transaction, skipping declines and anything reversed or refunded."""
    events = client.get("/api/v1/issuing/card_transaction_events", {"card_id": card_id, "page_size": 100}).get("items", [])
    grouped: dict[str, list[dict[str, Any]]] = {}
    for event in events:
        if event.get("process_result") == "APPROVED" and event.get("card_transaction_id"):
            grouped.setdefault(event["card_transaction_id"], []).append(event)
    undone = {"REVERSAL", "PARTIAL_REVERSAL", "REFUND", "CLEARING_REVERSAL"}
    return {txn: evs for txn, evs in grouped.items()
            if not any(_kind(e) in undone for e in evs) and any(_kind(e) in ("AUTHORIZATION", "CLEARING") for e in evs)}


def _kind(event: dict[str, Any]) -> str:
    return str(event.get("subtype") or event.get("type") or "")


def reconcile(client: AirwallexApi, card_ids: Mapping[str, str], receipts: Iterable[rc.Receipt],
              existing: Iterable[Case] = ()) -> list[Case]:
    """Cases for every agent-card debit without a receipt. Existing cases keep their state and history."""
    known = {c.card_transaction_id: c for c in existing}
    vouched = authorised_ids(receipts)
    for agent_id, card_id in card_ids.items():
        for txn, events in card_debits(client, card_id).items():
            lifecycle = next((e.get("lifecycle_id") for e in events if e.get("lifecycle_id")), None)
            if txn in vouched or (lifecycle and lifecycle in vouched):
                continue
            clearing = next((e for e in events if _kind(e) == "CLEARING"), None)
            first = events[-1]
            case = known.get(txn)
            if case is None:
                merchant = first.get("merchant") or {}
                case = Case(card_transaction_id=txn, agent_id=agent_id, card_id=card_id,
                            amount=str(first.get("billing_amount") or first.get("transaction_amount") or ""),
                            currency=str(first.get("billing_currency") or first.get("transaction_currency") or ""),
                            merchant_name=str(merchant.get("name") or ""), merchant_category=str(merchant.get("category_code") or ""),
                            transacted_at=str(first.get("created_at") or ""))
                case.move(CaseState.UNATTRIBUTED, "Card debit with no Intent Receipt.")
                known[txn] = case
            case.lifecycle_id = lifecycle or case.lifecycle_id
            case.cleared = clearing is not None
            case.clearing_event_id = clearing.get("id") if clearing else case.clearing_event_id
    return list(known.values())


# --- investigate ----------------------------------------------------------------------


class CaseSummarizer(Protocol):
    def summarize(self, facts: dict[str, Any]) -> str | None: ...


SUMMARY_PROMPT = """You write the case summary for an unexplained charge on an AI agent's company card.
Use only the facts given. State plainly what is known and what is not. A missing Intent Receipt means
no human authorisation was recorded; it is evidence, not proof, of fraud. Three sentences at most."""

_SUMMARY_SCHEMA = {"type": "object", "properties": {"summary": {"type": "string"}},
                   "required": ["summary"], "additionalProperties": False}


class ClaudeCaseSummarizer:
    def __init__(self, client: anthropic.Anthropic | None = None, model: str = "claude-opus-5-5") -> None:
        self.client = client or anthropic.Anthropic()
        self.model = model

    def summarize(self, facts: dict[str, Any]) -> str | None:
        """None on any failure: the caller falls back to a plain summary, and the dispute decision is code anyway."""
        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=4000,
                system=SUMMARY_PROMPT,
                messages=[{"role": "user", "content": json.dumps(facts, indent=1)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": _SUMMARY_SCHEMA}},
                # On a safety decline, the API retries on Anthropic's recommended fallback model.
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except (anthropic.APIStatusError, anthropic.APIConnectionError):
            return None
        if response.stop_reason in ("refusal", "max_tokens"):
            return None
        texts = [block.text for block in response.content if block.type == "text"]
        try:
            return str(json.loads(texts[-1])["summary"])
        except (IndexError, KeyError, TypeError, ValueError):
            return None


def facts(case: Case, agent_contained: bool) -> dict[str, Any]:
    return {"amount": f"{case.amount} {case.currency}", "merchant": case.merchant_name,
            "merchant_category": case.merchant_category, "when": case.transacted_at,
            "intent_receipt": "none", "human_approval": "none", "cleared": case.cleared,
            "agent": case.agent_id, "agent_status": "contained" if agent_contained else "active"}


def plain_summary(f: dict[str, Any]) -> str:
    return (f"A {f['amount']} charge at {f['merchant'] or 'an unknown merchant'} on {f['agent']}'s card has no Intent "
            f"Receipt and no recorded human approval; the agent is {f['agent_status']}.")


def should_dispute(case: Case, has_receipt: bool, has_approval: bool) -> tuple[bool, str]:
    """The only place that decides to dispute. Code, not the model."""
    if has_receipt or has_approval:
        return False, "A human authorised this charge."
    if not case.cleared or not case.clearing_event_id:
        return False, "Not cleared yet: card disputes need a cleared debit."
    return True, "No receipt, no approval, and the debit has cleared."


# --- evidence and dispute -------------------------------------------------------------


class UploadNotPermitted(Exception):
    pass


class EvidenceUploader:
    """Uploads to Airwallex's file service (a separate files.* host) and returns a file_id."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.url = client.base_url.replace("://api.", "://files.", 1) + "/api/v1/files/upload"

    def upload(self, name: str, pdf: bytes) -> str:
        if not getattr(self.client, "_token", None):
            self.client.login()
        # AirwallexClient keeps its bearer token private; the file host is the one call it doesn't wrap.
        resp = self.client.http.post(self.url, files={"file": (name, pdf, "application/pdf")},
                                     headers={"Authorization": f"Bearer {self.client._token}", "User-Agent": "warden/0.1"})
        if resp.status_code in (401, 403):
            raise UploadNotPermitted(UPLOAD_NOT_PERMITTED)
        resp.raise_for_status()
        return resp.json()["file_id"]


class SandboxSettlement:
    """Sandbox only: clear an authorised charge so it becomes disputable, as a real merchant would."""

    def __init__(self, client: AirwallexApi) -> None:
        self.client = client

    def capture(self, lifecycle_id: str) -> dict[str, Any]:
        return self.client.post(f"/api/v1/simulation/issuing/card_transaction_lifecycles/{lifecycle_id}/capture", {})


def evidence_pdf(case: Case, summary: str) -> bytes:
    lines = [
        "Warden - Unauthorised agent card charge",
        "",
        f"Case: {case.case_id}",
        f"Card transaction: {case.card_transaction_id}",
        f"Amount: {case.amount} {case.currency}",
        f"Merchant: {case.merchant_name} (MCC {case.merchant_category})",
        f"Date: {case.transacted_at}",
        f"Agent: {case.agent_id}",
        "",
        "Finding: no Intent Receipt exists for this charge.",
        "Every payment Warden authorises carries a signed Intent Receipt binding the",
        "human-approved mandate, payee, amount and evidence. This charge has none and",
        "no human approval was recorded, so it was made outside the company's controls.",
        "",
        "Investigation summary:",
        *_wrap(summary, 88),
    ]
    return _pdf(lines)


def _wrap(text: str, width: int) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if len(line) + len(word) + 1 > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    return out + ([line] if line else [])


def _pdf(lines: list[str]) -> bytes:
    """A one-page PDF from plain text lines, standard library only."""
    def esc(s: str) -> str:
        return s.encode("latin-1", "replace").decode("latin-1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    text = "BT /F1 11 Tf 15 TL 50 780 Td " + " ".join(f"({esc(line)}) Tj T*" for line in lines) + " ET"
    stream = text.encode("latin-1")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
               b"/Resources << /Font << /F1 5 0 R >> >> >>",
               b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pdf, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(pdf))
        pdf += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(pdf)
    pdf += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return pdf + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)


class Recoverer:
    def __init__(self, client: AirwallexApi, uploader: Any, summarizer: CaseSummarizer | None = None,
                 settlement: SandboxSettlement | None = None) -> None:
        self.client = client
        self.uploader = uploader
        self.summarizer = summarizer
        self.settlement = settlement

    def investigate(self, case: Case, agent_contained: bool) -> None:
        if case.state is not CaseState.UNATTRIBUTED:
            return
        f = facts(case, agent_contained)
        case.summary = (self.summarizer.summarize(f) if self.summarizer else None) or plain_summary(f)
        case.move(CaseState.INVESTIGATING, "Checking for a receipt, an approval and dispute eligibility.")

    def settle(self, case: Case) -> None:
        """Sandbox: capture the authorisation so the debit clears, then pick up its CLEARING event."""
        if case.cleared or not case.lifecycle_id or self.settlement is None:
            return
        self.settlement.capture(case.lifecycle_id)
        events = card_debits(self.client, case.card_id).get(case.card_transaction_id, [])
        clearing = next((e for e in events if _kind(e) == "CLEARING"), None)
        if clearing:
            case.cleared, case.clearing_event_id = True, clearing["id"]

    def dispute(self, case: Case, has_receipt: bool = False, has_approval: bool = False) -> None:
        if case.state is not CaseState.INVESTIGATING:
            return
        go, why = should_dispute(case, has_receipt, has_approval)
        if not go:
            case.reason = why
            return
        try:
            case.evidence_file_id = self.uploader.upload(f"{case.case_id}.pdf", evidence_pdf(case, case.summary))
        except UploadNotPermitted as e:
            case.reason = str(e)
            return
        note = f"No Intent Receipt or human approval exists for this agent card charge. Warden case {case.case_id}."
        try:
            if case.dispute_id:  # e.g. a DRAFT created earlier without evidence
                self.client.post(f"/api/v1/issuing/transaction_disputes/{case.dispute_id}/update",
                                 {"evidence_files": [case.evidence_file_id], "note": note})
            else:
                created = self.client.post("/api/v1/issuing/transaction_disputes/create", {
                    "card_transaction_event_id": case.clearing_event_id, "reason": DISPUTE_REASON, "note": note,
                    "reference": case.case_id, "evidence_files": [case.evidence_file_id]})
                case.dispute_id = created.get("id")
            submitted = self.client.post(f"/api/v1/issuing/transaction_disputes/{case.dispute_id}/submit", {})
        except Exception as e:  # noqa: BLE001 - the rail's error type is the client's; record it, never guess
            summary = getattr(e, "summary", None)
            message = summary() if callable(summary) else str(e)
            if "ineligible" in message.lower():
                case.move(CaseState.NOT_ELIGIBLE, message)
            else:
                case.reason = f"Dispute not filed: {message}"
            return
        case.dispute_status = submitted.get("status")
        case.move(_DISPUTE_STATES.get(str(case.dispute_status), CaseState.DISPUTE_SUBMITTED),
                  f"Dispute {case.dispute_id} submitted with evidence; Airwallex status {case.dispute_status}.")

    def refresh(self, case: Case) -> None:
        """Follow Airwallex's verdict. Only WON counts as recovered."""
        if not case.dispute_id or case.state not in (CaseState.DISPUTE_SUBMITTED,):
            return
        status = str(self.client.get(f"/api/v1/issuing/transaction_disputes/{case.dispute_id}").get("status"))
        if status != case.dispute_status:
            case.dispute_status = status
            new = _DISPUTE_STATES.get(status)
            if new is not None and new is not case.state:
                case.move(new, f"Airwallex reports the dispute {status}.")


# --- offline fakes (tests and --offline) ----------------------------------------------


class FakeAirwallex:
    """Two charges on one card: one Warden authorised (it has a receipt), one made outside it."""

    def __init__(self, dispute_outcome: str = "SUBMITTED") -> None:
        self.events = [
            _event("evt_ok_auth", "txn_ok", "life_ok", "AUTHORIZATION", 85, "Staples"),
            _event("evt_ok_clear", "txn_ok", "life_ok", "CLEARING", 85, "Staples"),
            _event("evt_rogue_auth", "txn_rogue", "life_rogue", "AUTHORIZATION", 180, "Office Depot Online"),
        ]
        self.disputes: dict[str, dict[str, Any]] = {}
        self.dispute_outcome = dispute_outcome
        self.calls: list[tuple[str, str]] = []

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        self.calls.append(("GET", path))
        if path == "/api/v1/issuing/card_transaction_events":
            return {"items": [e for e in self.events if e["card_id"] == (params or {}).get("card_id")]}
        if path.startswith("/api/v1/issuing/transaction_disputes/"):
            return self.disputes[path.rsplit("/", 1)[-1]]
        raise KeyError(path)

    def post(self, path: str, body: dict[str, Any] | None = None) -> Any:
        self.calls.append(("POST", path))
        body = body or {}
        if path.endswith("/capture"):
            life = path.split("/")[-2]
            src = next(e for e in self.events if e["lifecycle_id"] == life)
            self.events.append(_event(f"evt_{life}_clear", src["card_transaction_id"], life, "CLEARING",
                                      src["billing_amount"], src["merchant"]["name"]))
            return {"process_result": "APPROVED"}
        if path == "/api/v1/issuing/transaction_disputes/create":
            if not body.get("evidence_files"):
                raise ValueError("At least 1 evidence should be attached")
            dispute_id = f"dsp_{len(self.disputes) + 1}"
            self.disputes[dispute_id] = {"id": dispute_id, "status": "DRAFT", **body}
            return self.disputes[dispute_id]
        if path.endswith("/submit"):
            dispute = self.disputes[path.split("/")[-2]]
            dispute["status"] = "SUBMITTED"
            return dict(dispute)
        if path.endswith("/update"):
            self.disputes[path.split("/")[-2]].update(body)
            return self.disputes[path.split("/")[-2]]
        raise KeyError(path)

    def decide(self) -> None:
        """Simulate the card network's verdict arriving later."""
        for dispute in self.disputes.values():
            dispute["status"] = self.dispute_outcome


def _event(event_id: str, txn: str, life: str, kind: str, amount: float, merchant: str) -> dict[str, Any]:
    return {"id": event_id, "card_id": "card_fake", "card_transaction_id": txn, "lifecycle_id": life, "type": kind,
            "subtype": kind, "process_result": "APPROVED", "billing_amount": amount, "billing_currency": "USD",
            "merchant": {"name": merchant, "category_code": "5943"}, "created_at": "2026-11-13T00:00:00Z"}


class FakeUploader:
    def __init__(self, permitted: bool = True) -> None:
        self.permitted = permitted
        self.uploads: list[tuple[str, bytes]] = []

    def upload(self, name: str, pdf: bytes) -> str:
        if not self.permitted:
            raise UploadNotPermitted(UPLOAD_NOT_PERMITTED)
        self.uploads.append((name, pdf))
        return f"file_{len(self.uploads)}"


def offline_receipts() -> list[rc.Receipt]:
    """A signed receipt for the authorised Staples charge, with a throwaway key."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    body = {"receipt_id": "rcpt_offline", "decision": {"action": "ALLOW"},
            "execution": {"rail": "airwallex", "card_transaction_id": "txn_ok"}}
    return [rc.sign(body, Ed25519PrivateKey.generate())]


def offline_run() -> list[Case]:
    fake = FakeAirwallex(dispute_outcome="WON")
    cases = run(fake, FakeUploader(), {"buyer-agent": "card_fake"}, offline_receipts(), [], {"buyer-agent"},
                settlement=SandboxSettlement(fake))
    fake.decide()
    recoverer = Recoverer(fake, FakeUploader())
    for case in cases:
        recoverer.refresh(case)
    return cases


# --- store and CLI --------------------------------------------------------------------


def load_cases(path: Path = CASES_PATH) -> list[Case]:
    try:
        return [Case.from_json(c) for c in json.loads(path.read_text(encoding="utf-8"))]
    except (OSError, json.JSONDecodeError):
        return []


def save_cases(cases: list[Case], path: Path = CASES_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([c.to_json() for c in cases], indent=1, default=str), encoding="utf-8")


def agent_cards(data_dir: Path = ROOT / "data") -> dict[str, str]:
    """Agent -> card id, from the hire flow's agents.json, else the live demo's state."""
    agents = _read(data_dir / "agents.json")
    if isinstance(agents, dict) and isinstance(agents.get("agents"), list):
        agents = {a.get("agent_id") or a.get("id"): a for a in agents["agents"]}
    if isinstance(agents, dict) and agents:
        out = {}
        for agent_id, value in agents.items():
            card = value if isinstance(value, str) else (value or {}).get("card_id") if isinstance(value, dict) else None
            if agent_id and card:
                out[agent_id] = card
        if out:
            return out
    state = _read(data_dir / "live_state.json") or {}
    if state.get("card_id"):
        from warden.demo_world import AGENT
        return {AGENT: state["card_id"]}
    return {}


def contained_agents(audit_path: Path) -> set[str]:
    contained: set[str] = set()
    proposals: dict[str, str] = {}
    for line in audit_path.read_text(encoding="utf-8").splitlines() if audit_path.exists() else []:
        entry = json.loads(line)
        data = entry.get("data") or {}
        if entry.get("event") == "proposal_received":
            proposals[(data.get("proposal") or {}).get("id", "")] = (data.get("proposal") or {}).get("agent_id", "")
        elif entry.get("event") == "manual_containment":
            contained.add(data.get("agent_id", ""))
        elif data.get("action") == "CONTAIN":
            contained.add(data.get("agent_id") or proposals.get(data.get("proposal_id", ""), ""))
    return contained


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def run(client: AirwallexApi, uploader: Any, cards: Mapping[str, str], receipts: list[rc.Receipt],
        cases: list[Case], contained: set[str], summarizer: CaseSummarizer | None = None,
        settlement: SandboxSettlement | None = None, file_disputes: bool = True) -> list[Case]:
    cases = reconcile(client, cards, receipts, cases)
    recoverer = Recoverer(client, uploader, summarizer, settlement)
    for case in cases:
        recoverer.investigate(case, case.agent_id in contained)
        if file_disputes:
            recoverer.settle(case)
            recoverer.dispute(case)
        recoverer.refresh(case)
    return cases


def print_cases(cases: list[Case]) -> None:
    if not cases:
        print("Every card debit matches an Intent Receipt. Nothing to recover.")
    for c in cases:
        print(f"{c.case_id}  {c.state:<18} {c.amount:>8} {c.currency}  {c.merchant_name or '?':<22} agent={c.agent_id}")
        if c.summary:
            print(f"    summary: {c.summary}")
        if c.reason:
            print(f"    status:  {c.reason}")
        if c.dispute_id:
            print(f"    dispute: {c.dispute_id} ({c.dispute_status})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Reconcile agent card charges against Intent Receipts and recover.")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Airwallex sandbox + Claude")
    mode.add_argument("--offline", action="store_true", help="fakes only, nothing persisted")
    parser.add_argument("--card", action="append", default=[], metavar="AGENT=CARD_ID", help="also reconcile this card")
    parser.add_argument("--adopt-dispute", action="append", default=[], metavar="CARD_TXN_ID=DISPUTE_ID",
                        help="attach an existing DRAFT dispute to a case instead of creating one")
    parser.add_argument("--no-dispute", action="store_true", help="reconcile and investigate only")
    args = parser.parse_args(argv)

    if args.offline:
        print_cases(offline_run())
        return 0

    from warden.config import load_settings
    from warden.rails.airwallex import AirwallexClient

    settings = load_settings()
    client = AirwallexClient(settings.airwallex_base_url, settings.airwallex_client_id, settings.airwallex_api_key,
                             settings.airwallex_api_version)
    rc.sync(settings.audit_path)
    cards = agent_cards()
    cards.update(dict(pair.split("=", 1) for pair in args.card))
    cases = load_cases()
    adopt = dict(pair.split("=", 1) for pair in args.adopt_dispute)
    cases = reconcile(client, cards, rc.load(), cases)
    for case in cases:
        if case.card_transaction_id in adopt and not case.dispute_id:
            case.dispute_id = adopt[case.card_transaction_id]
    cases = run(client, EvidenceUploader(client), cards, rc.load(), cases, contained_agents(settings.audit_path),
                ClaudeCaseSummarizer(anthropic.Anthropic(api_key=settings.anthropic_api_key)) if settings.has_claude else None,
                SandboxSettlement(client), file_disputes=not args.no_dispute)
    save_cases(cases)
    print_cases(cases)
    print(f"\nCases saved to {CASES_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
