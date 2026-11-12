import json
from types import SimpleNamespace

import anthropic
import httpx

from warden import demo_world as w
from warden.risk import ClaudeRiskReader, OfflineRiskReader, classify_sender

DOMAINS = set(w.VENDOR_DOMAINS.values())


def test_sender_classification():
    assert classify_sender("acme-supplies.com", DOMAINS) == "known"
    assert classify_sender("acme-supp1ies.com", DOMAINS) == "LOOKALIKE_DOMAIN"
    assert classify_sender("acme-supplies-billing.net", DOMAINS) == "LOOKALIKE_DOMAIN"
    assert classify_sender("gl0bex.com", DOMAINS) == "LOOKALIKE_DOMAIN"
    assert classify_sender("randomshop.io", DOMAINS) == "UNKNOWN_SENDER"


def test_offline_reader_severities():
    reader = OfflineRiskReader(w.VENDOR_DOMAINS)
    assert reader.read(w.invoice("Acme Office Supply", "420", "INV-1")).severity == 0
    lookalike = reader.read(w.lookalike_invoice())
    assert lookalike.severity == 2 and "LOOKALIKE_DOMAIN" in lookalike.flags
    bank = reader.read(w.fake_bank_change())
    assert bank.severity == 3 and "BANK_DETAILS_CHANGED" in bank.flags
    assert reader.read(w.gift_card()).severity == 3


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def claude_reader(messages):
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return ClaudeRiskReader(w.VENDOR_DOMAINS, client=client)


def text_response(payload, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason, content=[
        SimpleNamespace(type="thinking", thinking=""),
        SimpleNamespace(type="text", text=json.dumps(payload)),
    ])


def test_claude_reader_parses_structured_flags_and_marks_evidence_untrusted():
    messages = FakeMessages(text_response({"flags": ["LOOKALIKE_DOMAIN", "NOT_A_FLAG"], "severity": 2,
                                           "explanation": "Sender imitates acme-supplies.com."}))
    report = claude_reader(messages).read(w.lookalike_invoice())
    assert report.flags == ("LOOKALIKE_DOMAIN",) and report.severity == 2 and report.source == "claude"

    call = messages.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and "server-side-fallback-2026-07-01" in call["betas"]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "untrusted data" in call["system"]
    assert 'channel="untrusted channel"' in call["messages"][0]["content"]


def test_claude_failures_force_a_human():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    for messages in (
        FakeMessages(text_response({}, stop_reason="refusal")),
        FakeMessages(SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text="not json")])),
        FakeMessages(error=anthropic.APIConnectionError(request=request)),
    ):
        report = claude_reader(messages).read(w.lookalike_invoice())
        assert report.flags == ("RISK_READER_UNAVAILABLE",) and report.severity == 2
