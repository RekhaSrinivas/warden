import json

from fastapi.testclient import TestClient

from warden import demo_world as w
from warden import lab
from warden import redteam as rt
from warden.risk import OfflineRiskReader
from warden.web import create_app


def test_each_design_layer_stops_more_and_warden_never_pays_an_attack():
    attacks = json.loads((rt.OUT / "attacks.json").read_text(encoding="utf-8"))
    attacks += [x for x in rt.EXPERT_ATTACKS if x["id"] not in {a["id"] for a in attacks}]
    legit, records = rt.legitimate()
    readers = {"B2": lab.NoRiskReader, "B3": lambda: OfflineRiskReader(w.VENDOR_DOMAINS)}
    paid = {}
    for design in ("B0", "B1", "B2", "B3"):
        r = lab.score_design(design, attacks, legit, records, lambda key: readers[key]())
        r["scripted"] = lab.score_scripted(design, lambda key: readers[key]())
        paid[design] = (r["summary"]["paid"], r["scripted"]["summary"]["paid"])
        if design in ("B2", "B3"):
            assert r["summary"]["legit_paid"] == r["summary"]["legitimate"]  # no false alarms
    assert paid["B0"][0] == len(attacks) > paid["B1"][0] > paid["B2"][0] == 0
    assert paid["B2"][1] > paid["B3"][1] == 0  # the reader catches what breaks no rule


def test_rail_only_model_uses_each_rails_own_controls():
    assert lab.rail_only(w.PROCUREMENT, {"kind": "invoice", "amount": "9000"})["outcome"] == "paid"
    assert lab.rail_only(w.PROCUREMENT, {"kind": "card", "amount": "40", "category": "gift cards"})["outcome"] == "blocked"
    assert lab.rail_only(w.PROCUREMENT, {"kind": "online_checkout", "amount": "40"})["outcome"] == "waiting_for_human"
    assert lab.rail_only(w.RESEARCH, {"kind": "data", "amount": "0.01", "pay_to": "https://evil.example/q"})["outcome"] == "blocked"
    assert lab.wilson(0, 46)[1] < 0.08


def test_lab_page_embeds_the_published_results_safely():
    page = TestClient(create_app()).get("/lab")
    assert page.status_code == 200 and "Which defence stops" in page.text
    data = page.text.split('id="lab-data" type="application/json">', 1)[1].split("</script>", 1)[0]
    assert json.loads(data.replace("<\\/", "</"))["results"]["B0"]["summary"]["paid"] == 46
