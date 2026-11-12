import json
import subprocess
from pathlib import Path

import httpx
import pytest

from warden import demo_world as w
from warden.rails.airi import AiriRail
from warden.rails.airwallex import AirwallexClient, AirwallexRail, SimulationAdapter
from warden.rails.base import RailDeclined
from warden.rails.cli import CliRunner
from warden.rails.metal import MetalRail


class FakeCli:
    """Stands in for subprocess.run and records every command line."""

    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.commands = []

    def __call__(self, cmd, **kwargs):
        self.commands.append(cmd)
        returncode, stdout = self.outputs.pop(0)
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")


def test_metal_pay_parses_transaction_and_refusal(tmp_path):
    fake = FakeCli([
        (0, "paid 0.02 *USDZ\ntransaction 0xabc123\nexplorer https://testnet.metalntwx.com/tx/0xabc123\n"),
        (1, "error: payment refused: mandate_cap_exceeded (the amount is above the cap); nothing was paid\n"),
        (0, "revoked\ntransaction 0xdef456\n"),
    ])
    rail = MetalRail(CliRunner(["metal-agent"], run=fake), {w.AGENT: tmp_path / "mandate.json"}, tmp_path / "owner.json")

    assert rail.execute(w.data_api_call()) == {"rail": "metal", "transaction": "0xabc123",
                                                "explorer": "https://testnet.metalntwx.com/tx/0xabc123"}
    with pytest.raises(RailDeclined) as refused:
        rail.execute(w.data_api_call())
    assert refused.value.reason == "mandate_cap_exceeded"
    assert rail.contain(w.AGENT)["transaction"] == "0xdef456"
    assert fake.commands[0][1:] == ["pay", w.DATA_API_URL, "--mandate", str(tmp_path / "mandate.json"),
                                    "--network", "testnet"]


def test_airi_mandate_create_then_contain_cancels_it(tmp_path):
    fake = FakeCli([
        (0, json.dumps({"mandate_id": "mdt_123", "approval": {"url": "https://app.airi.com/passkey/checkout?m=1"}})),
        (0, ""),
    ])
    rail = AiriRail(CliRunner(["airi-cli"], run=fake), "pm_test", Path(tmp_path))

    result = rail.execute(w.office_chair_checkout())
    assert result == {"rail": "airi", "mandate_id": "mdt_123",
                      "approval_url": "https://app.airi.com/passkey/checkout?m=1", "status": "AWAITING_PASSKEY"}
    args = fake.commands[0]
    assert args[args.index("--total-amount") + 1] == "120.00"
    assert args[args.index("--item") + 1] == "name:Ergonomic chair,unit_price:120.00,quantity:1"

    assert rail.contain(w.AGENT)["mandates_cancelled"] == ["mdt_123"]
    assert fake.commands[1][1:] == ["mandate", "cancel", "--mandate-id", "mdt_123"]


def airwallex_rail(handler):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = AirwallexClient("https://api.sandbox.airwallex.com", "cid", "key", http=http)
    return AirwallexRail(client, SimulationAdapter(client), {w.AGENT: "card_1"})


def test_airwallex_transfer_card_decline_and_freeze():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        seen.append((path, body))
        if path.endswith("/authentication/login"):
            assert request.headers["x-api-key"] == "key"
            return httpx.Response(200, json={"token": "t"})
        assert request.headers["Authorization"] == "Bearer t"
        if path.endswith("/transfers/create"):
            return httpx.Response(201, json={"id": "tr_1", "status": "SCHEDULED"})
        if path.endswith("/simulation/issuing/create"):
            return httpx.Response(200, json={"process_result": "DECLINED",
                                             "failure_reason": "MERCHANT_CATEGORY_NOT_ALLOWED"})
        if path.endswith("/cards/card_1/update"):
            return httpx.Response(200, json={"card_status": "INACTIVE"})
        if path.endswith("/cards/card_1"):
            return httpx.Response(200, json={"card_status": "INACTIVE"})
        return httpx.Response(404, json={"code": "not_found"})

    rail = airwallex_rail(handler)
    p = w.invoice("Acme Office Supply", "420", "INV-1001", "req_inv_1001")
    assert rail.execute(p) == {"rail": "airwallex", "transfer_id": "tr_1", "status": "SCHEDULED",
                               "request_id": "req_inv_1001"}
    transfer_body = seen[1][1]
    assert transfer_body["request_id"] == "req_inv_1001" and transfer_body["beneficiary_id"] == "ben_acme"

    with pytest.raises(RailDeclined) as declined:
        rail.execute(w.gift_card())
    assert declined.value.reason == "MERCHANT_CATEGORY_NOT_ALLOWED"
    assert rail.contain(w.AGENT) == {"rail": "airwallex", "card_id": "card_1", "card_status": "INACTIVE"}


def test_airwallex_duplicate_request_is_a_decline():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/authentication/login"):
            return httpx.Response(200, json={"token": "t"})
        return httpx.Response(400, json={"code": "duplicate_request", "message": "request_id already used"})

    with pytest.raises(RailDeclined) as declined:
        airwallex_rail(handler).execute(w.invoice("Acme Office Supply", "420", "INV-1001"))
    assert declined.value.reason == "duplicate_request"
