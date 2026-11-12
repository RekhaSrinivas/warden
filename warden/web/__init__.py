"""Warden's web dashboard. Run with `python -m warden.web` (add `--live` for real rails)."""

from warden.web.app import create_app

__all__ = ["create_app"]
