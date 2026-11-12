"""The interface every payment rail implements."""

from __future__ import annotations

from typing import Any, Protocol

from warden.models import Proposal


class RailDeclined(Exception):
    """The rail itself refused the payment: the second lock held.

    `reason` is the rail's own code, e.g. MERCHANT_CATEGORY_NOT_ALLOWED from an
    Airwallex card or mandate_cap_exceeded from a Metal mandate.
    """

    def __init__(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        self.reason = reason
        self.detail = detail or {}
        super().__init__(reason)


class RailError(Exception):
    """The rail could not be reached or failed in a way that says nothing about the payment."""


class Rail(Protocol):
    name: str

    def execute(self, proposal: Proposal) -> dict[str, Any]:
        """Move the money. Returns the rail's real ids so the audit log can point at them."""
        ...

    def contain(self, agent_id: str) -> dict[str, Any]:
        """Cut the agent off on this rail (freeze its card, revoke its mandate, cancel its mandates)."""
        ...
