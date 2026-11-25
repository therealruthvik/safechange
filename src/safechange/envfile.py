"""Minimal .env loader (no extra dependency).

Reads KEY=VALUE lines from ./.env. Variables already set in the environment win, so a
shell `export` always overrides the file. Blank lines and `#` comments are ignored;
an optional leading `export ` and surrounding single/double quotes are stripped.
"""
from __future__ import annotations

import os
from pathlib import Path


def parse(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        key = key.strip()
        if not sep or not key.replace("_", "").isalnum():
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        out[key] = value
    return out


def load(path: str | Path = ".env") -> list[str]:
    """Load `path` into os.environ without overriding existing vars. Returns keys set."""
    p = Path(path)
    if not p.is_file():
        return []
    loaded = []
    for key, value in parse(p.read_text()).items():
        if key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded
