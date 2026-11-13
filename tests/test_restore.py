"""A restarted Warden (e.g. the live dashboard after the demo) remembers what the audit log says happened."""

from warden import demo_world as w
from warden.audit import AuditLog
from warden.engine import Warden
from warden.risk import OfflineRiskReader


def test_restore_rebuilds_containment_trust_spend_and_the_duplicate_lock(tmp_path):
    path = tmp_path / "audit.jsonl"
    first, _ = w.offline_warden()
    first.audit = AuditLog(path)
    first.hire(w.PROCUREMENT, w.procurement_mandate())
    first.hire(w.RESEARCH, w.research_mandate())
    paid = w.invoice("Acme Office Supply", "420", "INV-1001")
    first.submit(paid)
    first.submit(w.fake_bank_change())  # strike one
    first.submit(w.gift_card())         # strike two: contained
    first.submit(w.data_api_call())

    second = Warden(w.demo_mandate(), OfflineRiskReader(w.VENDOR_DOMAINS), first.rails, audit=AuditLog(path))
    second.mandates = dict(first.mandates)
    second.restore()

    assert second.trust.is_contained(w.PROCUREMENT) and not second.trust.is_contained(w.RESEARCH)
    assert second.trust.score(w.PROCUREMENT) == first.trust.score(w.PROCUREMENT)
    assert second.state.spent_since(w.PROCUREMENT, 0) == 420
    assert second.submit(paid).violations[:1] == ["AGENT_CONTAINED"]  # still refused after the restart
    assert paid.request_id in second.state.seen_request_ids  # the duplicate lock survives the restart
