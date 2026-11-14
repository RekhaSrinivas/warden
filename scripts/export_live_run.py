"""Publish a live run as evidence anyone can check: docs/live-runs/<date>/.

Copies the run's evidence, signed receipts, receipt public key and audit log. Nothing secret is
in these files (no keys, no card numbers); the run summary only loses the local folder it ran in.

    python scripts/export_live_run.py 2026-11-13
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
# Only the unsigned run summary is touched: it records the folder the audit log lived in.
# Signed receipts and the hash-chained audit log are copied byte for byte, or they would no longer verify.
WINDOWS_DIR = re.compile(r'[A-Za-z]:(?:\\\\[^"\\]+)+\\\\(?=[^"\\]+")')  # C:\\Users\\...\\ in JSON text
LOCAL_TRACES = re.compile(r'[A-Za-z]:\\\\Users|/home/[a-z]')


def main(date: str) -> int:
    out = ROOT / "docs" / "live-runs" / date
    out.mkdir(parents=True, exist_ok=True)
    summary = WINDOWS_DIR.sub("", (DATA / "evidence.json").read_text(encoding="utf-8"))
    (out / "evidence.json").write_text(summary, encoding="utf-8")
    for name in ("receipts.jsonl", "audit.jsonl", "cases.json", "receipt-public-key.pem"):
        shutil.copyfile(DATA / name, out / name)
    leftovers = [p.name for p in out.iterdir() if LOCAL_TRACES.search(p.read_text(encoding="utf-8"))]
    if leftovers:
        print("Refusing: local paths remain in", leftovers)
        return 1
    evidence = json.loads(summary)
    print(f"Wrote {out.relative_to(ROOT)} ({evidence['mode']}, ran {evidence['ran_at']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else "latest"))
