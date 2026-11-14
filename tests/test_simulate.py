from warden import simulate


def test_random_worlds_never_break_a_money_rule():
    report = simulate.run(worlds=30, steps=80, seed=7)
    assert report.violations == []
    assert report.hostile > 0 and report.hostile_auto_paid == 0
    assert report.executed > 0  # honest work still gets paid


def test_the_rule_checker_catches_a_sabotaged_warden():
    report = simulate.run(worlds=10, steps=80, seed=7, sabotage=True)
    assert report.violations  # with the mandate check off, the rules break and the checker sees it
    assert simulate.run(worlds=3, steps=40, seed=7).violations == []  # and the real check is restored
