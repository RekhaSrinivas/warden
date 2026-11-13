"""Airi rail: online checkout with a Visa Intelligent Commerce (VIC) credential.

Warden is the only component that may ask Airi for a credential, and only for the
exact merchant, items and amount it approved. Airi then asks the human to confirm
with a passkey: no passkey, no credential. The credential (a tokenized DPAN, expiry
and CVV for one checkout) is written by airi-cli to a 0600 file, handed to the
merchant's checkout, then deleted. It is never printed, logged or put in a receipt.
"""

from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Any, Callable, Protocol

from warden.models import Proposal
from warden.rails.base import RailDeclined, RailError
from warden.rails.cli import CliRunner, tail


class CredentialFiles(Protocol):
    """Where airi-cli writes credentials. Paths are as airi-cli sees them (inside WSL on Windows)."""

    def read(self, path: str) -> str: ...
    def delete(self, path: str) -> None: ...


class LocalFiles:
    def read(self, path: str) -> str:
        return Path(path).read_text(encoding="utf-8")

    def delete(self, path: str) -> None:
        Path(path).unlink(missing_ok=True)


class WslFiles:
    """Reads a file airi-cli wrote inside WSL, through the \\\\wsl.localhost share."""

    def __init__(self, distro: str = "Ubuntu") -> None:
        self.root = Path(rf"\\wsl.localhost\{distro}")

    def _local(self, path: str) -> Path:
        return self.root.joinpath(*path.strip("/").split("/"))

    def read(self, path: str) -> str:
        return self._local(path).read_text(encoding="utf-8")

    def delete(self, path: str) -> None:
        self._local(path).unlink(missing_ok=True)


class Merchant(Protocol):
    def charge(self, credential: dict[str, Any], proposal: Proposal) -> dict[str, Any]: ...


class DemoCheckout:
    """A stand-in merchant checkout (no real shop takes the sandbox token). It accepts a VIC
    credential the way a card form would: a well-formed, unexpired Visa token for the exact amount."""

    def charge(self, credential: dict[str, Any], proposal: Proposal) -> dict[str, Any]:
        number = str(credential.get("number", "")).replace(" ", "")
        problems = []
        if not (number.isdigit() and 13 <= len(number) <= 19 and _luhn(number)):
            problems.append("card number is not a valid token")
        if not number.startswith("4"):
            problems.append("not a Visa token")
        if not _unexpired(credential.get("expiryYear"), credential.get("expiryMonth")):
            problems.append("credential expired")
        if not str(credential.get("cvv", "")).isdigit():
            problems.append("missing CVV")
        if problems:
            return {"approved": False, "reason": "; ".join(problems)}
        return {"approved": True, "order_id": f"OC-{int(time.time())}",
                "charged": f"{proposal.amount:.2f} {proposal.currency}"}


class AiriRail:
    name = "airi"

    def __init__(self, cli: CliRunner, payment_method_id: str, credential_dir: str, source: str = "Warden",
                 files: CredentialFiles | None = None, merchant: Merchant | None = None,
                 on_passkey: Callable[[dict[str, Any]], None] | None = None, passkey_timeout: int = 300) -> None:
        self.cli = cli
        self.payment_method_id = payment_method_id
        self.credential_dir = str(credential_dir).replace("\\", "/").rstrip("/")
        self.source = source
        self.files = files or LocalFiles()
        self.merchant = merchant or DemoCheckout()
        self.on_passkey = on_passkey or (lambda request: None)
        self.passkey_timeout = passkey_timeout
        self.open_mandates: dict[str, list[str]] = {}

    def execute(self, proposal: Proposal) -> dict[str, Any]:
        """The whole checkout: mandate for the approved order, the human's passkey, the credential,
        the merchant's answer, and the outcome reported back to Airi."""
        created = self.create_mandate(proposal)
        mandate_id = created["mandate_id"]
        self.on_passkey({**created, "agent_id": proposal.agent_id, "merchant": proposal.payee_name,
                         "amount": f"{proposal.amount:.2f}", "currency": proposal.currency,
                         "description": proposal.description})
        try:
            receipt = self.fetch_credential(mandate_id, self.passkey_timeout)
        except RailError:
            self.cancel(mandate_id)  # no passkey in time: nothing can be charged
            raise RailDeclined("NO_PASSKEY", {"mandate_id": mandate_id, "airi_status": "CANCELLED"}) from None
        path = receipt["credential_file"]
        try:
            credential = json.loads(self.files.read(path))
            outcome = self.merchant.charge(credential, proposal)
        finally:
            self.files.delete(path)  # single use: gone as soon as the checkout has it
        self._forget(proposal.agent_id, mandate_id)
        result = "SUCCEEDED" if outcome.get("approved") else "DECLINED"
        self.report_result(mandate_id, result, None if outcome.get("approved") else outcome.get("reason"))
        if result == "DECLINED":
            raise RailDeclined("CHECKOUT_DECLINED", {"mandate_id": mandate_id, "detail": str(outcome.get("reason"))})
        return {"rail": self.name, "mandate_id": mandate_id, "status": result, "order_id": outcome.get("order_id"),
                "protocol": credential.get("protocolType"), "token_type": credential.get("tokenType"),
                "passkey_confirmed": True}

    def create_mandate(self, proposal: Proposal) -> dict[str, Any]:
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
        return {"rail": self.name, "mandate_id": mandate_id,
                "approval_url": _find(data, "passkey_url", "passkeyUrl") or _find_url(data),
                "status": "AWAITING_PASSKEY"}

    def fetch_credential(self, mandate_id: str, timeout_seconds: int = 300) -> dict[str, Any]:
        """Blocks until the human confirms with a passkey. Returns only the receipt, never the credential."""
        path = f"{self.credential_dir}/airi-cred-{mandate_id}.json"
        receipt = self._json("mandate", "get-credential", "--mandate-id", mandate_id, "--file", path,
                             "--timeout", str(timeout_seconds), "--force", "-o", "json")
        fields = receipt if isinstance(receipt, dict) else {}
        return {"mandate_id": mandate_id, "credential_file": path,
                **{k: fields.get(k) for k in ("status", "tokenType", "tokenExpiration")}}

    def report_result(self, mandate_id: str, result: str, reason: str | None = None) -> None:
        args = ["mandate", "result", "--mandate-id", mandate_id, "--result", result]
        if reason:
            args += ["--reason", reason]
        self._check(*args)

    def cancel(self, mandate_id: str) -> None:
        try:
            self._check("mandate", "cancel", "--mandate-id", mandate_id)
        finally:
            for ids in self.open_mandates.values():
                if mandate_id in ids:
                    ids.remove(mandate_id)

    def contain(self, agent_id: str) -> dict[str, Any]:
        cancelled = []
        for mandate_id in list(self.open_mandates.pop(agent_id, [])):
            self._check("mandate", "cancel", "--mandate-id", mandate_id)
            cancelled.append(mandate_id)
        return {"rail": self.name, "mandates_cancelled": cancelled, "purchases": "disabled"}

    def _forget(self, agent_id: str, mandate_id: str) -> None:
        if mandate_id in self.open_mandates.get(agent_id, []):
            self.open_mandates[agent_id].remove(mandate_id)

    def _check(self, *args: str) -> str:
        result = self.cli(*args)
        if result.returncode != 0:
            raise RailError(f"airi-cli {args[0]} {args[1]} failed: {tail(result)}")
        return result.stdout or ""

    def _json(self, *args: str) -> Any:
        out = self._check(*args)
        if not out.strip():
            return {}
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


def _luhn(number: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2:
            d = d * 2 - 9 if d * 2 > 9 else d * 2
        total += d
    return total % 10 == 0


def _unexpired(year: Any, month: Any) -> bool:
    try:
        y, m = int(year), int(month)
    except (TypeError, ValueError):
        return False
    y = y + 2000 if y < 100 else y
    today = date.today()
    return (y, m) >= (today.year, today.month)


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
