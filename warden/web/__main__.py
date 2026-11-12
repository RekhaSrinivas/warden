"""python -m warden.web [--live] [--port 8000]

Offline (default): simulated rails and the heuristic risk reader, no keys needed.
--live: real rails and Claude, built by warden.live when that module is present.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from warden.web.app import create_app

LIVE_BUILDERS = ("build_live_warden", "build_warden", "build")


def _live_app():
    try:
        import warden.live as live
    except ImportError as e:
        print(f"Live mode is not available yet ({e}). Run without --live for the offline demo.")
        return None
    builder = next((getattr(live, name) for name in LIVE_BUILDERS if hasattr(live, name)), None)
    if builder is None:
        print(f"warden.live has none of {', '.join(LIVE_BUILDERS)}; cannot build a live Warden.")
        return None
    return create_app(builder(), mode="live")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Warden dashboard")
    parser.add_argument("--live", action="store_true", help="use real rails and Claude (needs .env keys)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    app = _live_app() if args.live else create_app()
    if app is None:
        return 2
    print(f"Warden dashboard ({'live' if args.live else 'offline demo'}): http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
