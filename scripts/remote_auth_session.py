"""Switch Airwallex remote authorization on for as long as this window stays open.

Opens a Cloudflare quick tunnel to the local Warden, points Airwallex Issuing's remote authorization at it,
checks a signed request gets through, then waits. Press Ctrl+C to switch remote authorization off again
and close the tunnel, so card charges never fail while Warden is not running. (If the window was closed
instead, run `python -m warden.authorize disable`.)

    python scripts/remote_auth_session.py [--port 8000]

Needs tools/cloudflared/cloudflared.exe (https://github.com/cloudflare/cloudflared/releases) and Warden
running live on the port. The public link this opens is look-only: every button answers 403.
"""
from __future__ import annotations

import re
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLOUDFLARED = ROOT / "tools" / "cloudflared" / ("cloudflared.exe" if sys.platform == "win32" else "cloudflared")


def authorize(*args: str) -> str:
    out = subprocess.run([sys.executable, "-m", "warden.authorize", *args], cwd=ROOT, capture_output=True, text=True)
    return (out.stdout + out.stderr).strip()


def main(port: int) -> int:
    local = f"http://127.0.0.1:{port}"
    try:
        urllib.request.urlopen(local + "/api/state", timeout=30)
    except Exception:
        print(f"Start Warden first: python -m warden.web --live --port {port}")
        return 1
    if not CLOUDFLARED.exists():
        print(f"Missing {CLOUDFLARED}; download cloudflared from github.com/cloudflare/cloudflared/releases")
        return 1
    tunnel = subprocess.Popen([str(CLOUDFLARED), "tunnel", "--no-autoupdate", "--url", local],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace")
    found: list[str] = []

    def read() -> None:
        for line in tunnel.stdout:  # keep draining, or cloudflared blocks on a full pipe
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not found:
                found.append(m.group(0))

    threading.Thread(target=read, daemon=True).start()
    try:
        for _ in range(60):
            if found:
                break
            time.sleep(1)
        if not found:
            print("The tunnel did not start; check your internet connection.")
            return 1
        url = found[0]
        for _ in range(30):  # the new hostname takes a few seconds to resolve
            try:
                urllib.request.urlopen(url + "/api/state", timeout=10)
                break
            except Exception:
                time.sleep(2)
        print(authorize("enable", "--url", url + "/airwallex/remote-auth"))
        check = authorize("selftest", "--url", url + "/airwallex/remote-auth")
        if '"response_status":"DECLINED"' not in check.replace(" ", ""):
            print("Signed test failed:", check)
            return 1
        print("Signed test request reached Warden through the tunnel:", check.split(":", 1)[0])
        time.sleep(8)  # Airwallex applies the new URL after a few seconds
        print("\nREADY. Airwallex now asks Warden before every card charge.")
        print("Keep this window open while you record. Press Ctrl+C here when done.\n")
        while tunnel.poll() is None:
            time.sleep(1)
        print("The tunnel stopped.")
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        print(authorize("disable"))
        tunnel.terminate()


if __name__ == "__main__":
    args = sys.argv[1:]
    raise SystemExit(main(int(args[args.index("--port") + 1]) if "--port" in args else 8000))
