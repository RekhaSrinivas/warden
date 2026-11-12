import json
from dataclasses import replace
from decimal import Decimal

from warden import demo_world as w
from warden.approvals import approval_hash, matches
from warden.audit import AuditLog
from warden.models import Evidence
from warden.trust import MIN_TRUST, STRIKE_WINDOW_SECONDS, TrustLedger


def test_trust_halves_on_strike_floors_and_recovers():
    ledger = TrustLedger()
    assert ledger.strike("a", now=0) == 0.5
    for _ in range(10):
        ledger.strike("a", now=0)
    assert ledger.score("a") == MIN_TRUST
    ledger.reward("a")
    assert ledger.score("a") == 0.15
    assert ledger.strikes("a", now=0) == 11
    assert ledger.strikes("a", now=STRIKE_WINDOW_SECONDS + 1) == 0


def test_audit_chain_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)
    for i in range(3):
        log.append("event", n=i, amount=Decimal("1.50"))
    assert log.verify() is None

    reloaded = AuditLog(path)  # a restart continues the same chain
    reloaded.append("event", n=3)
    assert reloaded.verify() is None

    lines = path.read_text(encoding="utf-8").splitlines()
    entry = json.loads(lines[1])
    entry["data"]["n"] = 99
    lines[1] = json.dumps(entry)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    assert AuditLog(path).verify() == 1


def test_audit_detects_deleted_entry():
    log = AuditLog()
    for i in range(3):
        log.append("event", n=i)
    del log._entries[1]
    assert log.verify() == 1


def test_approval_is_bound_to_what_the_human_saw():
    p = w.lookalike_invoice()
    h = approval_hash(p)
    assert matches(p, h)
    assert not matches(replace(p, amount=Decimal("301")), h)
    assert not matches(replace(p, payee_id="ben_new_9921"), h)
    assert not matches(replace(p, evidence=p.evidence + (Evidence("email:x@y.z", False, "one more thing"),)), h)
