"""Adaptive trust: anomalies shrink what an agent may move without a human."""

from __future__ import annotations

from dataclasses import dataclass, field

MIN_TRUST = 0.1
MAX_TRUST = 1.0
REWARD = 0.05
STRIKE_WINDOW_SECONDS = 7 * 24 * 3600


@dataclass
class AgentTrust:
    score: float = MAX_TRUST
    strike_times: list[float] = field(default_factory=list)
    contained: bool = False


class TrustLedger:
    def __init__(self) -> None:
        self._agents: dict[str, AgentTrust] = {}

    def get(self, agent_id: str) -> AgentTrust:
        return self._agents.setdefault(agent_id, AgentTrust())

    def score(self, agent_id: str) -> float:
        return self.get(agent_id).score

    def strikes(self, agent_id: str, now: float) -> int:
        return sum(1 for t in self.get(agent_id).strike_times if t >= now - STRIKE_WINDOW_SECONDS)

    def strike(self, agent_id: str, now: float) -> float:
        """Record an anomaly: halve trust (never below the floor). Returns the new score."""
        agent = self.get(agent_id)
        agent.strike_times.append(now)
        agent.score = max(MIN_TRUST, round(agent.score / 2, 4))
        return agent.score

    def reward(self, agent_id: str) -> float:
        """A clean payment that went through earns a little trust back."""
        agent = self.get(agent_id)
        agent.score = min(MAX_TRUST, round(agent.score + REWARD, 4))
        return agent.score

    def contain(self, agent_id: str) -> None:
        agent = self.get(agent_id)
        agent.contained = True
        agent.score = MIN_TRUST

    def is_contained(self, agent_id: str) -> bool:
        return self.get(agent_id).contained
