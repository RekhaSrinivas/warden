"""Airi rail: online checkout with a Visa Intelligent Commerce (VIC) credential.

Warden is the only component that may ask Airi for a credential, and only for
the exact amount it approved. Airi then asks the human to confirm with a
passkey. The credential is written to a 0600 file and never logged.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from warden.models import Proposal
from warden.rails.base import RailError
from warden.rails.cli import CliRunner, tail


class AiriRail:
    name = "airi"

    def __init__(self, cli: CliRunner, payment_method_id: str, credential_dir: Path, source: str = "warden") -> None:
        self.cli = cli
        self.payment_method_id = payment_method_id
        self.credential_dir = credential_dir
        self.source = source
        self.open_mandates: dict[str, list[str]] = {}

    def execute(self, proposal: Proposal) -> dict[str, Any]:
        """Create a mandate for the approved amount. The purchase waits for the human's passkey."""
        args = ["mandate", "create", "--source", self.source, "--payment-method-id", self.payment_method_id,
                "--merchant-name", proposal.payee_name, "--merchant-url", proposal.extra["merchant_url"],
                "--merchant-country", proposal.extra.get("merchant_country", "US"),
                "--total-amount", f"{proposal.amount:.2f}", "--currency", proposal.currency,
                "--description", proposal.description]
        for item in _items(proposal):
            args += ["--item", item]
        data = self._json(*args, "-o", "json")
        mandate_id = _find(data, "mandate_id", "mandateId", "id")
        if not mandate_id:
            raise RailError("airi-cli did not return a mandate id")
        self.open_mandates.setdefault(proposal.agent_id, []).append(mandate_id)
        return {"rail": self.name, "mandate_id": mandate_id, "approval_url": _find_url(data),
                "status": "AWAITING_PASSKEY"}

    def fetch_credential(self, mandate_id: str, timeout_seconds: int = 300) -> dict[str, Any]:
        """Wait for the passkey, then save the credential to a private file. Returns only the receipt."""
        self.credential_dir.mkdir(parents=True, exist_ok=True)
        path = self.credential_dir / f"airi-cred-{mandate_id}.json"
        receipt = self._json("mandate", "get-credential", "--mandate-id", mandate_id, "--file", str(path),
                             "--timeout", str(timeout_seconds), "-o", "json")
        fields = receipt if isinstance(receipt, dict) else {}
        return {"mandate_id": mandate_id, "credential_file": str(path),
                **{k: fields.get(k) for k in ("status", "tokenType", "tokenExpiration")}}

    def report_result(self, mandate_id: str, result: str, reason: str | None = None) -> None:
        args = ["mandate", "result", "--mandate-id", mandate_id, "--result", result]
        if reason:
            args += ["--reason", reason]
        self._check(*args)

    def contain(self, agent_id: str) -> dict[str, Any]:
        cancelled = []
        for mandate_id in self.open_mandates.pop(agent_id, []):
            self._check("mandate", "cancel", "--mandate-id", mandate_id)
            cancelled.append(mandate_id)
        return {"rail": self.name, "mandates_cancelled": cancelled, "purchases": "disabled"}

    def _check(self, *args: str) -> str:
        result = self.cli(*args)
        if result.returncode != 0:
            raise RailError(f"airi-cli {args[0]} {args[1]} failed: {tail(result)}")
        return result.stdout or ""

    def _json(self, *args: str) -> Any:
        out = self._check(*args)
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            raise RailError(f"airi-cli returned non-JSON output for {args[0]} {args[1]}") from None


def _items(proposal: Proposal) -> list[str]:
    """airi-cli takes items as 'name:..,unit_price:..,quantity:..', so strip the separators from names."""
    items = proposal.extra.get("items") or [
        {"name": proposal.description, "unit_price": f"{proposal.amount:.2f}", "quantity": 1}]
    return [f"name:{_clean(i['name'])},unit_price:{i['unit_price']},quantity:{i['quantity']}" for i in items]


def _clean(name: Any) -> str:
    return str(name).replace(",", " ").replace(":", " ")


def _find(data: Any, *keys: str) -> str | None:
    if isinstance(data, dict):
        for key in keys:
            if isinstance(data.get(key), str):
                return data[key]
        for value in data.values():
            found = _find(value, *keys)
            if found:
                return found
    return None


def _find_url(data: Any) -> str | None:
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, str) and value.startswith("https://"):
                return value
            found = _find_url(value)
            if found:
                return found
    return None
