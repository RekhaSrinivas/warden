"""python -m warden.web [--live] [--port 8000]

Offline (default): simulated rails and the heuristic risk reader, no keys needed.
--live: the agents the live demo hired, on the real sandbox, Claude and Metal testnet.
"""

from __future__ import annotations

import argparse
import sys

import uvicorn

from warden.web.app import create_app, live_services


def _live_app():
    from warden import live  # needs the .env keys, so only imported for --live

    lw = live.build_for_dashboard()
    return create_app(lw.warden, live_services(lw))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Warden dashboard")
    parser.add_argument("--live", action="store_true", help="use real rails and Claude (needs .env keys)")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    app = _live_app() if args.live else create_app()
    print(f"Warden dashboard ({'live' if args.live else 'offline demo'}): http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
