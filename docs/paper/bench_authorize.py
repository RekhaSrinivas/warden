"""Time Warden's real-time card authorization decision, offline (no keys, no network, no port).

    python docs/paper/bench_authorize.py            10,000 declines + 10,000 approvals
    python docs/paper/bench_authorize.py --n 2000

Each approval first registers the exact card payment Warden approved (IntentBook.expect), then the
card network's request claims it. Each decline is a charge Warden never approved. The timer wraps
RemoteAuthorizer.decide(), which includes writing the answer to the (in-memory) audit log. HTTP,
TLS and signature checking are not included.
"""

from __future__ import annotations

import argparse
import platform
import statistics
import sys
import time
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from warden import demo_world as w  # noqa: E402
from warden.audit import AuditLog  # noqa: E402
from warden.authorize import RemoteAuthorizer  # noqa: E402
from warden.engine import Warden  # noqa: E402
from warden.risk import OfflineRiskReader  # noqa: E402

CARD = "card_bench"


def payload(event: str, amount: str, mcc: str) -> dict:
    return {"version": 2, "card_id": CARD, "card_transaction_event_id": event, "card_transaction_id": f"tx_{event}",
            "transaction_type": "AUTHORIZATION", "transaction_amount": amount, "transaction_currency": "USD",
            "merchant": {"name": "BENCH", "category_code": mcc}}


def pct(values: list[float], q: float) -> float:
    s = sorted(values)
    return s[min(len(s) - 1, int(round(q * (len(s) - 1))))]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=10_000)
    args = parser.parse_args()

    warden = Warden(w.procurement_mandate(), OfflineRiskReader(w.VENDOR_DOMAINS), {}, audit=AuditLog())
    warden.hire(w.PROCUREMENT, w.procurement_mandate())
    auth = RemoteAuthorizer(lambda: warden, lambda: {CARD: w.PROCUREMENT}, secret=None)
    base = w.supplies_card("85")

    declines, approvals, wrong = [], [], 0
    for i in range(args.n):
        t = time.perf_counter()
        a = auth.decide(payload(f"d{i}", "12.00", w.MCC_STATIONERY))
        declines.append((time.perf_counter() - t) * 1000)
        wrong += a["response_status"] != "DECLINED"

        p = replace(base, id=f"prop_b{i}", agent_id=w.PROCUREMENT)
        warden.intents.expect(p)
        t = time.perf_counter()
        a = auth.decide(payload(f"a{i}", str(p.amount), p.merchant_category))
        approvals.append((time.perf_counter() - t) * 1000)
        wrong += a["response_status"] != "AUTHORIZED"
        # A used intent stays in the book until it expires (10 min); clear it so the book holds about as
        # many intents as a live run does, instead of growing to n.
        warden.intents._intents.clear()

    print(f"python {platform.python_version()} on {platform.system()} {platform.machine()}")
    for name, xs in (("declined (no Intent Receipt)", declines), ("authorized (matching intent)", approvals)):
        print(f"{name:30s} n={len(xs):,}  median {statistics.median(xs):.3f} ms  p99 {pct(xs, 0.99):.3f} ms"
              f"  max {max(xs):.3f} ms")
    print(f"wrong answers: {wrong}")
    return 1 if wrong else 0


if __name__ == "__main__":
    raise SystemExit(main())
