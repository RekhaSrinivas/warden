from decimal import Decimal

import pytest

from warden.decide import auto_approve_limit, decide
from warden.models import Action, RiskReport

CAP = Decimal("500")


def risk(severity=0, *flags):
    return RiskReport(flags=flags, severity=severity, explanation="", source="test")


@pytest.mark.parametrize("violations, severity, trust, strikes, amount, action, strike", [
    (["AGENT_CONTAINED", "OVER_TXN_CAP"], 0, 0.1, 2, "10", Action.BLOCK, False),
    (["DUPLICATE_REQUEST"], 0, 1.0, 0, "10", Action.BLOCK, False),
    (["PAYEE_NOT_ALLOWLISTED"], 3, 1.0, 1, "10", Action.CONTAIN, True),
    ([], 3, 1.0, 1, "10", Action.CONTAIN, True),
    (["OVER_TXN_CAP"], 0, 1.0, 0, "900", Action.BLOCK, True),
    ([], 3, 1.0, 0, "10", Action.BLOCK, True),
    ([], 2, 1.0, 1, "10", Action.STEP_UP, False),
    ([], 0, 0.5, 0, "300", Action.STEP_UP, False),
    ([], 1, 1.0, 0, "300", Action.ALLOW, False),
    ([], 0, 0.5, 0, "250", Action.ALLOW, False),
])
def test_decision_table(violations, severity, trust, strikes, amount, action, strike):
    verdict = decide(violations, risk(severity, "X"), trust, strikes, Decimal(amount), CAP)
    assert (verdict.action, verdict.strike) == (action, strike)


def test_auto_approve_limit_scales_with_trust():
    assert auto_approve_limit(CAP, 1.0) == Decimal("500.00")
    assert auto_approve_limit(CAP, 0.25) == Decimal("125.00")
