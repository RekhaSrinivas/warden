"""Shared runner for rails that drive a vendor CLI (metal-agent, airi-cli)."""

from __future__ import annotations

import os
import shutil
import subprocess
from typing import Callable, Sequence

Runner = Callable[..., subprocess.CompletedProcess]


class CliRunner:
    def __init__(self, command: Sequence[str], env: dict[str, str] | None = None, timeout: int = 120,
                 run: Runner = subprocess.run) -> None:
        """`command` may carry a prefix, e.g. ("wsl", "-d", "Ubuntu", "airi-cli") when a CLI only runs on Linux."""
        self.command = list(command)
        self.env = env or {}
        self.timeout = timeout
        self.run = run

    def __call__(self, *args: str) -> subprocess.CompletedProcess:
        # npm installs Windows shims as .cmd files, which subprocess only finds through shutil.which.
        exe = shutil.which(self.command[0]) or self.command[0]
        return self.run([exe, *self.command[1:], *args], capture_output=True, text=True, timeout=self.timeout,
                        env={**os.environ, **self.env})


def tail(result: subprocess.CompletedProcess, lines: int = 3) -> str:
    text = (result.stdout or "") + (result.stderr or "")
    return " | ".join(line.strip() for line in text.strip().splitlines()[-lines:])
