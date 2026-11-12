"""A rail that moves no money. Used by tests and the offline gauntlet; every id says 'simulated'."""

from __future__ import annotations

from typing import Any

from warden.models import Proposal
from warden.rails.base import RailDeclined


class FakeRail:
    def __init__(self, name: str = "fake", decline: dict[str, str] | None = None) -> None:
        """`decline` maps a merchant category or payee id to a decline reason, to mimic the rail's own lock."""
        self.name = name
        self.decline = decline or {}
        self.executed: list[Proposal] = []
        self.contained: list[str] = []

    def execute(self, proposal: Proposal) -> dict[str, Any]:
        reason = self.decline.get(proposal.merchant_category) or self.decline.get(proposal.payee_id)
        if reason:
            raise RailDeclined(reason, {"simulated": True})
        self.executed.append(proposal)
        return {"rail": self.name, "id": f"simulated_{len(self.executed)}", "status": "SIMULATED"}

    def contain(self, agent_id: str) -> dict[str, Any]:
        self.contained.append(agent_id)
        return {"rail": self.name, "agent_id": agent_id, "status": "CONTAINED (simulated)"}
