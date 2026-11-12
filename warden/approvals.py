"""Approvals bound to exactly what the human saw.

The hash covers amount, currency, payee, merchant category and every piece of
evidence. If the agent changes any of them, the approval no longer matches and
the payment cannot run on it.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from warden.models import Proposal


def approval_hash(proposal: Proposal) -> str:
    material = {
        "proposal_id": proposal.id,
        "rail": proposal.rail,
        "amount": str(proposal.amount),
        "currency": proposal.currency,
        "payee_id": proposal.payee_id,
        "payee_name": proposal.payee_name,
        "merchant_category": proposal.merchant_category,
        "evidence": [[e.source, e.trusted, e.text] for e in proposal.evidence],
    }
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def matches(proposal: Proposal, approved_hash: str) -> bool:
    return hmac.compare_digest(approval_hash(proposal), approved_hash)
