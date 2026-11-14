from warden import demo_world as w
from warden import redteam
from warden.risk import OfflineRiskReader


def test_expert_attacks_never_get_paid_and_legitimate_work_always_does():
    board = redteam.score(redteam.EXPERT_ATTACKS, OfflineRiskReader(w.VENDOR_DOMAINS), workers=4)
    s = board["summary"]
    assert s["attacks_paid_without_a_human"] == 0, [r for r in board["attacks"] if r["result"] == "paid"]
    assert s["legitimate_paid"] == s["legitimate"] == 30
    assert "## " not in redteam.markdown(board).split("\n", 1)[0]  # renders
