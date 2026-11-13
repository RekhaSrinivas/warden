import json
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from warden import receipts as rc
from warden.audit import AuditLog
from warden.web.verify import OnchainVerifier, create_verify_router, parse_verify_output


def _proposal(pid: str, **extra) -> dict:
    return {"id": pid, "agent_id": "buyer-agent", "rail": "transfer", "amount": "420", "currency": "USD",
            "payee_id": "ben_acme_1234567890", "payee_name": "Acme Office Supply",
            "description": "Pay invoice INV-1001, account 12345678", "merchant_category": "",
            "evidence": [{"source": "invoice:INV-1001", "trusted": True, "text": "Bank acct 9876543210"}],
            "request_id": f"req_{pid}", **extra}


def _log() -> AuditLog:
    log = AuditLog()
    log.append("proposal_received", proposal=_proposal("p1"))
    log.append("risk_read", proposal_id="p1", flags=[], severity=0, explanation="Routine.", source="claude")
    log.append("decision", proposal_id="p1", action="ALLOW", reasons=["Within mandate."], violations=[],
               risk={"flags": [], "severity": 0, "explanation": "Routine.", "source": "claude"},
               trust_before=1.0, trust_after=1.0, approval_hash=None,
               execution={"rail": "airwallex", "transfer_id": "tr_1", "status": "PAID", "secret": "nope"},
               containment=None)
    # A replay of the same proposal is refused; it must not replace the PAID receipt.
    log.append("proposal_received", proposal=_proposal("p1"))
    log.append("decision", proposal_id="p1", action="BLOCK", reasons=["Duplicate."], violations=["DUPLICATE_REQUEST"],
               risk={}, trust_before=1.0, trust_after=1.0)
    log.append("proposal_received", proposal=_proposal("p2", amount="300"))
    log.append("decision", proposal_id="p2", action="STEP_UP", reasons=["Held."], violations=[], risk={},
               trust_before=1.0, trust_after=1.0, approval_hash="ah_1")
    log.append("approval", proposal_id="p2", action="ALLOW", reasons=["Approved by finance-lead."], violations=[],
               risk={}, trust_before=1.0, trust_after=1.0, approval_hash="ah_1",
               execution={"rail": "metal", "transaction": "0x" + "ab" * 32, "explorer": "https://x/tx/1",
                          "credential": "secrets\\mandate-42.json"},
               agent_id="buyer-agent", mandate_id="m_1", mandate_hash="mh_1", evidence_hash="eh_1")
    return log


@pytest.fixture
def key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


def test_one_receipt_per_final_outcome_and_replays_do_not_overwrite(key):
    receipts = rc.issue_from_audit(_log().entries(), key)
    assert [(r.body["proposal_id"], r.body["decision"]["action"]) for r in receipts] == \
        [("p1", "ALLOW"), ("p1", "BLOCK"), ("p2", "ALLOW")]
    assert receipts[0].body["settlement_status"] == "PAID"
    assert receipts[1].body["settlement_status"] == "NOT_EXECUTED"


def test_receipts_verify_and_chain(key):
    receipts = rc.issue_from_audit(_log().entries(), key)
    assert rc.verify_chain(receipts, key.public_key()) is None
    assert rc.verify(receipts[1], key.public_key(), receipts[0].receipt_hash).ok


def test_tampering_breaks_signature_hash_and_chain(key):
    receipts = rc.issue_from_audit(_log().entries(), key)
    tampered = rc.Receipt({**receipts[0].body, "amount": "9999"}, receipts[0].receipt_hash, receipts[0].signature)
    result = rc.verify(tampered, key.public_key())
    assert not result.signature_ok and not result.hash_ok
    assert rc.verify_chain([receipts[0], receipts[2]], key.public_key()) == 1  # receipt removed from the middle
    assert not rc.verify(receipts[0], Ed25519PrivateKey.generate().public_key()).signature_ok


def test_receipts_never_carry_raw_ids_text_or_unknown_fields(key):
    text = json.dumps([r.to_json() for r in rc.issue_from_audit(_log().entries(), key)])
    for secret in ("ben_acme_1234567890", "12345678", "9876543210", "Pay invoice", "nope", "mandate-42.json\\"):
        assert secret not in text
    first = rc.issue_from_audit(_log().entries(), key)[0].body
    assert first["payee"]["id"] == "ben_ac…7890" and first["payee"]["id_hash"]
    assert first["evidence_hash"] and first["evidence_sources"] == [{"kind": "invoice", "trusted": True}]


def test_newer_audit_fields_are_bound(key):
    approved = rc.issue_from_audit(_log().entries(), key)[2].body
    assert approved["mandate_id"] == "m_1" and approved["mandate_hash"] == "mh_1"
    assert approved["evidence_hash"] == "eh_1"
    assert approved["approval"] == {"approval_hash": "ah_1", "approver": "finance-lead"}
    assert approved["metal_credential_ref"] == "mandate-42.json"
    assert approved["settlement_status"] == "SETTLED_ONCHAIN"


def test_malformed_entries_are_skipped(key):
    entries = [{"event": "decision"}, {"event": "decision", "data": {"proposal_id": "x", "action": "ALLOW"}}, {}]
    receipts = rc.issue_from_audit(entries, key)
    assert len(receipts) == 1 and receipts[0].body["amount"] is None


def test_sync_is_append_only_and_idempotent(tmp_path, key):
    audit = tmp_path / "audit.jsonl"
    log = AuditLog(audit)
    for entry in _log().entries():
        log.append(entry["event"], **entry["data"])
    out = tmp_path / "receipts.jsonl"
    assert len(rc.sync(audit, out, key)) == 3
    assert rc.sync(audit, out, key) == []
    log.append("decision", proposal_id="p9", action="BLOCK", reasons=[], violations=[], risk={},
               trust_before=1.0, trust_after=1.0)
    new = rc.sync(audit, out, key)
    loaded = rc.load(out)
    assert len(new) == 1 and len(loaded) == 4
    assert new[0].body["prev_receipt_hash"] == loaded[2].receipt_hash
    assert rc.verify_chain(loaded, key.public_key()) is None


def test_redact_masks_long_digit_runs():
    assert rc.redact("acct 12345678 ref 42") == "acct ••••5678 ref 42"


VERIFY_YES = """mandate 0xcb
step 1 pass: The mandate is bound on the principal's account
step 2 pass: The credential in hand hashes to the bound hash(C)
step 3 pass: The credential's signatures
step 4 pass: Each payment and refusal is the mandate key's per-payment mandate
verified yes
"""


def test_parse_verify_output():
    result = parse_verify_output(VERIFY_YES)
    assert result.verified and [s["result"] for s in result.steps] == ["pass"] * 4
    assert not parse_verify_output(VERIFY_YES.replace("step 2 pass", "step 2 fail").replace("yes", "no")).verified
    assert not parse_verify_output("error: boom").verified


def _app(tmp_path, key, runner):
    audit = tmp_path / "audit.jsonl"
    log = AuditLog(audit)
    for entry in _log().entries():
        log.append(entry["event"], **entry["data"])
    pub = tmp_path / "pub.pem"
    pub.write_text(rc.public_key_pem(key.public_key()))
    out = tmp_path / "receipts.jsonl"
    rc.sync(audit, out, key)
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "mandate-42.json").write_text("{}")
    onchain = OnchainVerifier(runner=runner, secrets_dir=secrets, cache_path=tmp_path / "cache.json")
    app = FastAPI()
    app.include_router(create_verify_router(out, pub, audit, onchain))
    return TestClient(app), rc.load(out)


def test_verify_page_routes(tmp_path, key):
    calls = []

    def runner(*args):
        calls.append(args)
        return SimpleNamespace(stdout=VERIFY_YES, stderr="", returncode=0)

    client, receipts = _app(tmp_path, key, runner)
    assert client.get("/verify").status_code == 200
    page = client.get(f"/verify/{receipts[0].id}")
    assert page.status_code == 200 and "Authentic receipt" in page.text and page.text.count("check ok") == 4
    assert client.get(f"/verify/{receipts[0].id}/json").json()["receipt_hash"] == receipts[0].receipt_hash
    assert client.get(f"/verify/{receipts[0].id}/onchain").status_code == 404  # not a Metal payment

    metal = receipts[2]
    assert client.get(f"/verify/{metal.id}/onchain").json()["verified"] is True
    assert client.get(f"/verify/{metal.id}/onchain").json()["verified"] is True
    assert len(calls) == 1 and "mandate-42.json" in str(calls[0])  # the receipt's own credential, then cached

    assert client.get("/verify/rcpt_missing").status_code == 404
    ok = client.post("/verify/paste", json=receipts[0].to_json()).json()["checks"]
    bad = client.post("/verify/paste", json={**receipts[0].to_json(), "amount": "1"}).json()["checks"]
    assert all(c["ok"] for c in ok) and not any(c["ok"] for c in bad)
    assert client.post("/verify/paste", content=b"[1]", headers={"Content-Type": "application/json"}).status_code == 400
