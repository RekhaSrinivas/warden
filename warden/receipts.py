"""Signed Intent Receipts: proof of what a human authorised and what then happened.

A receipt is issued for every proposal that reached a final outcome (ALLOW, BLOCK
or CONTAIN, directly or after a human approved or denied it). It binds the mandate,
the exact payee and amount, a hash of the evidence, Warden's decision, the approval
and the real rail ids, and is signed with Warden's Ed25519 key.

Receipts are built from the audit log alone, so they can never claim something the
log does not record, and they form their own append-only chain. They are safe to
publish: payee ids, descriptions and evidence are hashed or masked, never copied.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from warden.config import ROOT

VERSION = "warden-intent-receipt/1"
GENESIS = "0" * 64
SIGNING_KEY_PATH = ROOT / "secrets" / "receipt-signing-key.pem"
PUBLIC_KEY_PATH = ROOT / "data" / "receipt-public-key.pem"
RECEIPTS_PATH = ROOT / "data" / "receipts.jsonl"

FINAL_EVENTS = ("decision", "approval", "denial")
FINAL_ACTIONS = ("ALLOW", "BLOCK", "CONTAIN")

# Rail result fields that are ids or statuses, never secrets. Everything else is dropped.
_EXECUTION_FIELDS = ("rail", "status", "reason", "request_id", "transfer_id", "card_transaction_id",
                     "lifecycle_id", "card_transaction_event_id", "transaction", "explorer", "airi_mandate_id",
                     "mandate_id", "simulated_authorization", "order_id", "protocol", "token_type",
                     "passkey_confirmed")
_DIGITS = re.compile(r"\d{4,}")


def canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")


def sha256(data: Any) -> str:
    raw = data if isinstance(data, bytes) else canonical(data) if not isinstance(data, str) else data.encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


# --- keys ---------------------------------------------------------------------------


def load_or_create_signing_key(path: Path = SIGNING_KEY_PATH, public_path: Path = PUBLIC_KEY_PATH) -> Ed25519PrivateKey:
    """The private key lives in secrets/ (git-ignored); the public key is published in data/."""
    if path.exists():
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{path} is not an Ed25519 key")
    else:
        key = Ed25519PrivateKey.generate()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    public_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.write_bytes(public_key_pem(key.public_key()).encode("ascii"))
    return key


def public_key_pem(public_key: Ed25519PublicKey) -> str:
    return public_key.public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def load_public_key(path: Path = PUBLIC_KEY_PATH) -> Ed25519PublicKey:
    key = serialization.load_pem_public_key(path.read_bytes())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError(f"{path} is not an Ed25519 public key")
    return key


def key_fingerprint(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return hashlib.sha256(raw).hexdigest()[:16]


# --- receipts -------------------------------------------------------------------------


@dataclass(frozen=True)
class Receipt:
    body: dict[str, Any]
    receipt_hash: str
    signature: str

    @property
    def id(self) -> str:
        return self.body["receipt_id"]

    def to_json(self) -> dict[str, Any]:
        return {**self.body, "receipt_hash": self.receipt_hash, "signature": self.signature}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Receipt:
        body = {k: v for k, v in data.items() if k not in ("receipt_hash", "signature")}
        return cls(body=body, receipt_hash=data.get("receipt_hash", ""), signature=data.get("signature", ""))


def sign(body: dict[str, Any], key: Ed25519PrivateKey) -> Receipt:
    receipt_hash = sha256(canonical(body))
    signature = base64.b64encode(key.sign(canonical(body))).decode("ascii")
    return Receipt(body=body, receipt_hash=receipt_hash, signature=signature)


@dataclass(frozen=True)
class Verification:
    signature_ok: bool
    hash_ok: bool
    chain_ok: bool | None  # None when no previous receipt was supplied to compare against

    @property
    def ok(self) -> bool:
        return self.signature_ok and self.hash_ok and self.chain_ok is not False


def verify(receipt: Receipt, public_key: Ed25519PublicKey, prev_receipt_hash: str | None = None) -> Verification:
    body = canonical(receipt.body)
    try:
        public_key.verify(base64.b64decode(receipt.signature), body)
        signature_ok = True
    except (InvalidSignature, ValueError):
        signature_ok = False
    chain_ok = None if prev_receipt_hash is None else receipt.body.get("prev_receipt_hash") == prev_receipt_hash
    return Verification(signature_ok=signature_ok, hash_ok=sha256(body) == receipt.receipt_hash, chain_ok=chain_ok)


def verify_chain(receipts: list[Receipt], public_key: Ed25519PublicKey) -> int | None:
    """Index of the first receipt that fails, or None if every signature, hash and link holds."""
    prev = GENESIS
    for i, receipt in enumerate(receipts):
        if not verify(receipt, public_key, prev).ok:
            return i
        prev = receipt.receipt_hash
    return None


# --- building receipts from the audit log ---------------------------------------------


def issue_from_audit(entries: Iterable[dict[str, Any]], key: Ed25519PrivateKey, prev_receipt_hash: str = GENESIS,
                     already_issued: Iterable[str] = (), now: float | None = None) -> list[Receipt]:
    """One receipt per final outcome in the log (a STEP_UP waits for its approval or denial),
    skipping audit entries already receipted. A replayed proposal gets its own receipt for
    the refusal, so the original payment's receipt is never overwritten.

    Tolerates both the current audit shape and the newer one that carries agent_id,
    mandate_id/hash, evidence_hash, approver and rail ids: anything missing is null.
    """
    done = set(already_issued)
    proposals: dict[str, dict[str, Any]] = {}
    receipts: list[Receipt] = []
    prev = prev_receipt_hash
    pubkey_fp = key_fingerprint(key.public_key())
    for entry in entries:
        data = entry.get("data") or {}
        if entry.get("event") == "proposal_received" and isinstance(data.get("proposal"), dict):
            proposals[data["proposal"].get("id", "")] = data
            continue
        if entry.get("event") not in FINAL_EVENTS or data.get("action") not in FINAL_ACTIONS:
            continue
        if entry.get("hash") in done:
            continue
        receipt = sign(_body(proposals.get(data.get("proposal_id", ""), {}), entry, prev, pubkey_fp, now), key)
        receipts.append(receipt)
        prev = receipt.receipt_hash
    return receipts


def _body(proposal_entry: dict[str, Any], entry: dict[str, Any], prev: str, pubkey_fp: str,
          now: float | None) -> dict[str, Any]:
    p = proposal_entry.get("proposal") or {}
    d = entry.get("data") or {}
    risk = d.get("risk") or {}
    execution = d.get("execution") or {}
    payee_id = str(p.get("payee_id") or "")
    return {
        "version": VERSION,
        "receipt_id": "rcpt_" + sha256(f"{d.get('proposal_id')}:{entry.get('hash')}")[:16],
        "issued_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now if now is not None else time.time())),
        "signer": pubkey_fp,
        "agent_id": d.get("agent_id") or p.get("agent_id"),
        "mandate_id": d.get("mandate_id") or proposal_entry.get("mandate_id"),
        "mandate_hash": d.get("mandate_hash") or proposal_entry.get("mandate_hash"),
        "proposal_id": d.get("proposal_id"),
        "request_id": p.get("request_id"),
        "rail": p.get("rail"),
        "amount": str(p["amount"]) if p.get("amount") is not None else None,
        "currency": p.get("currency"),
        "payee": {"name": p.get("payee_name"), "id": mask(payee_id), "id_hash": sha256(payee_id) if payee_id else None},
        "merchant_category": p.get("merchant_category") or None,
        "purpose_hash": sha256(p["description"]) if p.get("description") else None,
        "evidence_hash": d.get("evidence_hash") or proposal_entry.get("evidence_hash") or _evidence_hash(p),
        "evidence_sources": [{"kind": str(e.get("source", "")).split(":", 1)[0], "trusted": bool(e.get("trusted"))}
                             for e in p.get("evidence") or []],
        "decision": {
            "action": d.get("action"),
            "reasons": [redact(r) for r in d.get("reasons") or []],
            "violations": list(d.get("violations") or []),
            "risk": {"flags": list(risk.get("flags") or []), "severity": risk.get("severity"),
                     "source": risk.get("source"), "explanation": redact(risk.get("explanation") or "")[:400]},
            "trust_before": d.get("trust_before"),
            "trust_after": d.get("trust_after"),
        },
        "approval": {"approval_hash": d.get("approval_hash"), "approver": d.get("approver") or _approver(d)},
        "execution": {k: execution[k] for k in _EXECUTION_FIELDS if execution.get(k) is not None} or None,
        "metal_credential_ref": _credential_ref(execution, d),
        "containment": _containment(d.get("containment")),
        "settlement_status": _settlement(d.get("action"), execution),
        "audit": {"event": entry.get("event"), "seq": entry.get("seq"), "entry_hash": entry.get("hash")},
        "prev_receipt_hash": prev,
    }


def _evidence_hash(p: dict[str, Any]) -> str | None:
    evidence = p.get("evidence")
    return sha256([{"source": e.get("source"), "trusted": e.get("trusted"), "text": e.get("text")} for e in evidence]) \
        if evidence else None


def _approver(d: dict[str, Any]) -> str | None:
    for reason in d.get("reasons") or []:
        match = re.match(r"(?:Approved|Denied) by (.+?)\.?$", reason)
        if match:
            return match.group(1)
    return None


def _credential_ref(execution: dict[str, Any], d: dict[str, Any]) -> str | None:
    """Only the file name, so the verify page can find the mandate credential; never its contents."""
    ref = execution.get("credential") or execution.get("mandate_credential") or d.get("mandate_credential")
    return Path(str(ref).replace("\\", "/")).name if ref else None


def _containment(containment: dict[str, Any] | None) -> dict[str, Any] | None:
    if not containment:
        return None
    out: dict[str, Any] = {}
    for rail, result in containment.items():
        if isinstance(result, dict):
            out[rail] = {k: result[k] for k in ("card_status", "status", "transaction", "explorer", "mandate_revoked")
                         if result.get(k) is not None}
    return out


def _settlement(action: str | None, execution: dict[str, Any]) -> str:
    if action != "ALLOW":
        return "NOT_EXECUTED"
    return str(execution.get("status") or ("SETTLED_ONCHAIN" if execution.get("transaction") else "EXECUTED"))


def mask(value: str) -> str | None:
    """Show enough to recognise an id, never enough to reuse it."""
    if not value:
        return None
    if len(value) <= 8:
        return value[:2] + "…"
    return f"{value[:6]}…{value[-4:]}"


def redact(text: str) -> str:
    """Mask long digit runs (account or card numbers) in free text."""
    return _DIGITS.sub(lambda m: "•" * (len(m.group()) - 4) + m.group()[-4:], text)


# --- store ----------------------------------------------------------------------------


def load(path: Path = RECEIPTS_PATH) -> list[Receipt]:
    if not path.exists():
        return []
    return [Receipt.from_json(json.loads(line)) for line in path.read_text(encoding="utf-8").splitlines() if line]


def sync(audit_path: Path, receipts_path: Path = RECEIPTS_PATH, key: Ed25519PrivateKey | None = None) -> list[Receipt]:
    """Issue receipts for every final audit outcome not yet receipted and append them. Returns the new ones."""
    entries = [json.loads(line) for line in audit_path.read_text(encoding="utf-8").splitlines() if line] \
        if audit_path.exists() else []
    return sync_entries(entries, receipts_path, key or load_or_create_signing_key())


def sync_entries(entries: list[dict[str, Any]], receipts_path: Path, key: Ed25519PrivateKey) -> list[Receipt]:
    """Like sync(), for an audit log held in memory (the offline dashboard)."""
    existing = load(receipts_path)
    issued_for = {r.body.get("audit", {}).get("entry_hash") for r in existing}
    prev = existing[-1].receipt_hash if existing else GENESIS
    new = issue_from_audit(entries, key, prev, issued_for)
    if new:
        receipts_path.parent.mkdir(parents=True, exist_ok=True)
        with receipts_path.open("a", encoding="utf-8") as f:
            for receipt in new:
                f.write(canonical(receipt.to_json()).decode("utf-8") + "\n")
    return new


def main() -> int:
    from warden.config import load_settings

    settings = load_settings()
    new = sync(settings.audit_path)
    receipts = load()
    broken = verify_chain(receipts, load_public_key())
    print(f"Issued {len(new)} new receipt(s); {len(receipts)} total in {RECEIPTS_PATH}")
    for r in receipts[-10:]:
        b = r.body
        print(f"  {b['receipt_id']}  {b['decision']['action']:<8} {b.get('amount') or '':>8} {b.get('currency') or '':<4}"
              f" {b['payee']['name'] or '':<22} {b['settlement_status']}")
    print("Chain:", "intact" if broken is None else f"broken at receipt {broken}")
    return 0 if broken is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
