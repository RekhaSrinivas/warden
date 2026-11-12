"""Settings loaded from .env. Every key may be missing so offline runs still work."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_AIRWALLEX_BASE_URL = "https://api.sandbox.airwallex.com"


def load_env(path: Path) -> None:
    """Non-empty .env values win over the shell: a stale machine-wide ANTHROPIC_API_KEY
    once shadowed the project key and every Claude call failed with 401."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip().strip('"').strip("'")
        if value:
            os.environ[key.strip()] = value


@dataclass(frozen=True)
class Settings:
    airwallex_client_id: str
    airwallex_api_key: str
    airwallex_base_url: str
    airwallex_api_version: str | None
    anthropic_api_key: str
    metal_rpc_url: str
    audit_path: Path

    @property
    def has_airwallex(self) -> bool:
        return bool(self.airwallex_client_id and self.airwallex_api_key)

    @property
    def has_claude(self) -> bool:
        return bool(self.anthropic_api_key)


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def load_settings(env_file: Path | None = ROOT / ".env") -> Settings:
    if env_file is not None:
        load_env(env_file)
    return Settings(
        airwallex_client_id=_env("AIRWALLEX_CLIENT_ID"),
        airwallex_api_key=_env("AIRWALLEX_API_KEY"),
        airwallex_base_url=_env("AIRWALLEX_BASE_URL") or DEFAULT_AIRWALLEX_BASE_URL,
        airwallex_api_version=_env("AIRWALLEX_API_VERSION") or None,
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        metal_rpc_url=_env("METAL_RPC_URL"),
        audit_path=Path(_env("WARDEN_AUDIT_PATH") or ROOT / "data" / "audit.jsonl"),
    )
