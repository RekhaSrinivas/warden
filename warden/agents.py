"""The company's AI workers: who was hired, under which mandate, holding which card and Metal mandate.

Persisted to data/agents.json. Only ids and file paths are stored, never keys or card numbers.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

from warden.config import ROOT
from warden.mandate import Mandate, mandate_hash

AGENTS_PATH = ROOT / "data" / "agents.json"


@dataclass
class AgentRecord:
    agent_id: str
    role: str
    instruction: str
    mandate: dict[str, Any]
    mandate_hash: str
    provisioning: dict[str, Any] = field(default_factory=dict)  # card_id, metal credential path, tx hashes...
    status: str = "active"  # active | fired
    hired_at: float = field(default_factory=time.time)
    fired_at: float | None = None

    @classmethod
    def hired(cls, agent_id: str, role: str, mandate: Mandate, provisioning: dict[str, Any]) -> AgentRecord:
        return cls(agent_id, role, mandate.instruction, mandate.to_dict(), mandate_hash(mandate), dict(provisioning))

    def to_mandate(self) -> Mandate:
        m = self.mandate
        return Mandate(
            per_txn_cap=Decimal(m["per_txn_cap"]), weekly_cap=Decimal(m["weekly_cap"]),
            currencies=frozenset(m["currencies"]), allowed_mccs=frozenset(m["allowed_mccs"]),
            payee_allowlist=frozenset(m["payee_allowlist"]), reserve_floor=Decimal(m["reserve_floor"]),
            velocity_per_hour=m["velocity_per_hour"], mandate_id=m["mandate_id"], agent_id=m["agent_id"],
            purpose=m["purpose"], instruction=m["instruction"],
            approval_threshold=None if m["approval_threshold"] is None else Decimal(m["approval_threshold"]),
            human_required=tuple(m["human_required"]),
        )


class AgentRegistry:
    def __init__(self, path: Path | None = AGENTS_PATH) -> None:
        self.path = path
        self.agents: dict[str, AgentRecord] = {}
        if path is not None and path.exists():
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.agents = {a["agent_id"]: AgentRecord(**a) for a in raw.get("agents", [])}

    def add(self, record: AgentRecord) -> None:
        self.agents[record.agent_id] = record
        self.save()

    def fire(self, agent_id: str) -> None:
        record = self.agents[agent_id]
        record.status, record.fired_at = "fired", time.time()
        self.save()

    def active(self) -> list[AgentRecord]:
        return [a for a in self.agents.values() if a.status == "active"]

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"agents": [asdict(a) for a in self.agents.values()]}, indent=2),
                             encoding="utf-8")
