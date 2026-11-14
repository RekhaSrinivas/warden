"""Publish a live run as evidence anyone can check: docs/live-runs/<date>/.

Copies the run's evidence, signed receipts, receipt public key and audit log. Nothing secret is
in these files (no keys, no card numbers); the run summary only loses the local folder it ran in.

    python scripts/export_live_run.py 2026-11-13 [--console http://127.0.0.1:8000]

With --console, the live console's view of the run is saved too (dashboard-state.json): the offline
story page replays it, so someone without keys still sees the real run.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
# Only the unsigned run summary is touched: it records the folder the audit log lived in.
# Signed receipts and the hash-chained audit log are copied byte for byte, or they would no longer verify.
WINDOWS_DIR = re.compile(r'[A-Za-z]:(?:\\\\[^"\\]+)+\\\\(?=[^"\\]+")')  # C:\\Users\\...\\ in JSON text
LOCAL_TRACES = re.compile(r'[A-Za-z]:\\\\Users|/home/[a-z]')


def main(date: str, console: str = "") -> int:
    out = ROOT / "docs" / "live-runs" / date
    out.mkdir(parents=True, exist_ok=True)
    summary = WINDOWS_DIR.sub("", (DATA / "evidence.json").read_text(encoding="utf-8"))
    (out / "evidence.json").write_text(summary, encoding="utf-8")
    for name in ("receipts.jsonl", "audit.jsonl", "cases.json", "receipt-public-key.pem"):
        shutil.copyfile(DATA / name, out / name)
    if console:
        with urllib.request.urlopen(console.rstrip("/") + "/api/state", timeout=60) as r:
            view = json.loads(r.read().decode("utf-8"))
        if view.get("mode") != "live":
            print("Refusing: that console is not running live")
            return 1
        text = json.dumps(view, indent=1, ensure_ascii=False)
        (out / "dashboard-state.json").write_text(re.sub(r"secrets[\/]+", "", text), encoding="utf-8")
    leftovers = [p.name for p in out.iterdir() if LOCAL_TRACES.search(p.read_text(encoding="utf-8"))]
    if leftovers:
        print("Refusing: local paths remain in", leftovers)
        return 1
    evidence = json.loads(summary)
    print(f"Wrote {out.relative_to(ROOT)} ({evidence['mode']}, ran {evidence['ran_at']})")
    return 0


if __name__ == "__main__":
    args = sys.argv[1:]
    console = args[args.index("--console") + 1] if "--console" in args else ""
    raise SystemExit(main(args[0] if args and not args[0].startswith("--") else "latest", console))
