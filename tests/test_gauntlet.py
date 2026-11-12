from warden.gauntlet import SCENARIOS, run, scorecard
from warden.models import Action


def test_gauntlet_stops_every_attack_and_pays_every_clean_payment():
    results = run()
    text, ok = scorecard(results)
    assert ok, text
    assert all(d.action is Action.ALLOW for s, d in results if not s.attack)
    assert len(SCENARIOS) >= 12
