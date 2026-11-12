"""Metal rail: per-call x402 payments in *USDZ under an on-chain mandate.

The mandate (per-payment cap, lifetime budget, payee list) lives on chain, so
it still refuses a payment if Warden is bypassed. Containment revokes it.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping

from warden.models import Proposal
from warden.rails.base import RailDeclined, RailError
from warden.rails.cli import CliRunner, tail

_TRANSACTION = re.compile(r"transaction\s+(0x[0-9a-fA-F]+)")
_EXPLORER = re.compile(r"explorer\s+(https?://\S+)")
_REFUSED = re.compile(r"payment refused:\s*([a-z_]+)")


class MetalRail:
    name = "metal"

    def __init__(self, cli: CliRunner, mandates: Mapping[str, Path], owner_key: Path, network: str = "testnet") -> None:
        self.cli = cli
        self.mandates = dict(mandates)
        self.owner_key = owner_key
        self.network = network

    def execute(self, proposal: Proposal) -> dict[str, Any]:
        """Pay an x402 seller. The seller quotes the price; the mandate cap bounds it on chain."""
        mandate = self._mandate(proposal.agent_id)
        result = self.cli("pay", proposal.extra["url"], "--mandate", str(mandate), "--network", self.network)
        output = (result.stdout or "") + (result.stderr or "")
        refused = _REFUSED.search(output)
        if refused:
            raise RailDeclined(refused.group(1), {"output": tail(result)})
        if result.returncode != 0:
            raise RailError(f"metal-agent pay failed: {tail(result)}")
        return {"rail": self.name, "transaction": _first(_TRANSACTION, output), "explorer": _first(_EXPLORER, output)}

    def contain(self, agent_id: str) -> dict[str, Any]:
        mandate = self.mandates.get(agent_id)
        if mandate is None:
            return {"rail": self.name, "mandate": "none bound"}
        result = self.cli("mandate", "revoke", "--credential", str(mandate), "--key", str(self.owner_key),
                          "--network", self.network)
        if result.returncode != 0:
            raise RailError(f"metal-agent mandate revoke failed: {tail(result)}")
        output = (result.stdout or "") + (result.stderr or "")
        return {"rail": self.name, "mandate_revoked": str(mandate.name), "transaction": _first(_TRANSACTION, output),
                "explorer": _first(_EXPLORER, output)}

    def status(self, agent_id: str) -> str:
        result = self.cli("mandate", "status", "--credential", str(self._mandate(agent_id)), "--network", self.network)
        return (result.stdout or "").strip()

    def _mandate(self, agent_id: str) -> Path:
        if agent_id not in self.mandates:
            raise RailError(f"No Metal mandate bound for agent {agent_id}")
        return self.mandates[agent_id]


def _first(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1) if match else None
