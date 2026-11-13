from warden import recover as rv
from warden.recover import (Case, CaseState, FakeAirwallex, FakeUploader, Recoverer, SandboxSettlement,
                            offline_receipts, reconcile, should_dispute)


def _cases(fake=None):
    fake = fake or FakeAirwallex()
    return fake, reconcile(fake, {"buyer-agent": "card_fake"}, offline_receipts())


def test_only_charges_without_a_receipt_become_cases():
    _, cases = _cases()
    assert [c.card_transaction_id for c in cases] == ["txn_rogue"]
    case = cases[0]
    assert case.state is CaseState.UNATTRIBUTED and case.amount == "180" and not case.cleared


def test_declined_and_reversed_charges_are_ignored():
    fake = FakeAirwallex()
    fake.events.append({**fake.events[2], "id": "evt_rev", "type": "REVERSAL", "subtype": "REVERSAL"})
    fake.events.append({**fake.events[0], "id": "evt_dec", "card_transaction_id": "txn_declined",
                        "process_result": "DECLINED"})
    assert reconcile(fake, {"a": "card_fake"}, offline_receipts()) == []


def test_reconcile_keeps_existing_case_state():
    fake, cases = _cases()
    cases[0].move(CaseState.INVESTIGATING, "looking")
    again = reconcile(fake, {"buyer-agent": "card_fake"}, offline_receipts(), cases)
    assert len(again) == 1 and again[0].state is CaseState.INVESTIGATING


def test_dispute_decision_is_code():
    case = Case("t", "a", "c", "10", "USD", cleared=True, clearing_event_id="e")
    assert should_dispute(case, has_receipt=False, has_approval=False)[0]
    assert not should_dispute(case, has_receipt=True, has_approval=False)[0]
    assert not should_dispute(case, has_receipt=False, has_approval=True)[0]
    assert not should_dispute(Case("t", "a", "c", "10", "USD"), False, False)[0]


def test_full_recovery_counts_money_only_when_won():
    fake, cases = _cases(FakeAirwallex(dispute_outcome="WON"))
    uploader = FakeUploader()
    recoverer = Recoverer(fake, uploader, settlement=SandboxSettlement(fake))
    case = cases[0]
    recoverer.investigate(case, agent_contained=True)
    assert case.state is CaseState.INVESTIGATING and "contained" in case.summary
    recoverer.settle(case)
    assert case.cleared and case.clearing_event_id == "evt_life_rogue_clear"
    recoverer.dispute(case)
    assert case.state is CaseState.DISPUTE_SUBMITTED  # filed, not recovered
    assert uploader.uploads[0][1].startswith(b"%PDF") and b"no Intent Receipt" in uploader.uploads[0][1]
    created = fake.disputes[case.dispute_id]
    assert created["card_transaction_event_id"] == "evt_life_rogue_clear"
    assert created["reason"] == "UNAUTHORIZED_TRANSACTION" and created["evidence_files"] == ["file_1"]
    recoverer.refresh(case)
    assert case.state is CaseState.DISPUTE_SUBMITTED
    fake.decide()
    recoverer.refresh(case)
    assert case.state is CaseState.RECOVERED
    assert [h["state"] for h in case.history] == ["UNATTRIBUTED", "INVESTIGATING", "DISPUTE_SUBMITTED", "RECOVERED"]


def test_lost_dispute_is_lost():
    fake, cases = _cases(FakeAirwallex(dispute_outcome="LOST"))
    recoverer = Recoverer(fake, FakeUploader(), settlement=SandboxSettlement(fake))
    case = cases[0]
    for step in (lambda: recoverer.investigate(case, False), lambda: recoverer.settle(case),
                 lambda: recoverer.dispute(case), fake.decide, lambda: recoverer.refresh(case)):
        step()
    assert case.state is CaseState.LOST


def test_upload_not_permitted_keeps_case_investigating():
    fake, cases = _cases()
    recoverer = Recoverer(fake, FakeUploader(permitted=False), settlement=SandboxSettlement(fake))
    case = cases[0]
    recoverer.investigate(case, False)
    recoverer.settle(case)
    recoverer.dispute(case)
    assert case.state is CaseState.INVESTIGATING and case.reason == rv.UPLOAD_NOT_PERMITTED
    assert fake.disputes == {}


def test_uncleared_charge_without_sandbox_settlement_waits():
    fake, cases = _cases()
    recoverer = Recoverer(fake, FakeUploader())
    case = cases[0]
    recoverer.investigate(case, False)
    recoverer.settle(case)
    recoverer.dispute(case)
    assert case.state is CaseState.INVESTIGATING and "Not cleared" in case.reason


def test_existing_draft_dispute_is_updated_not_duplicated():
    fake, cases = _cases()
    fake.disputes["dsp_draft"] = {"id": "dsp_draft", "status": "DRAFT"}
    case = cases[0]
    case.dispute_id = "dsp_draft"
    recoverer = Recoverer(fake, FakeUploader(), settlement=SandboxSettlement(fake))
    recoverer.investigate(case, False)
    recoverer.settle(case)
    recoverer.dispute(case)
    assert list(fake.disputes) == ["dsp_draft"] and fake.disputes["dsp_draft"]["evidence_files"] == ["file_1"]
    assert case.state is CaseState.DISPUTE_SUBMITTED


def test_claude_summary_is_used_and_failures_fall_back():
    class Summ:
        def __init__(self, text):
            self.text = text

        def summarize(self, facts):
            return self.text

    fake, cases = _cases()
    Recoverer(fake, FakeUploader(), Summ("Claude says so.")).investigate(cases[0], False)
    assert cases[0].summary == "Claude says so."
    fake, cases = _cases()
    Recoverer(fake, FakeUploader(), Summ(None)).investigate(cases[0], False)
    assert "no Intent Receipt" in cases[0].summary


def test_cases_round_trip(tmp_path):
    _, cases = _cases()
    rv.save_cases(cases, tmp_path / "cases.json")
    loaded = rv.load_cases(tmp_path / "cases.json")
    assert loaded[0].to_json() == cases[0].to_json()


def test_offline_run_recovers():
    cases = rv.offline_run()
    assert len(cases) == 1 and cases[0].state is CaseState.RECOVERED
