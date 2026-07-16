"""Resolve admin bearer token from CLI or environment."""

from __future__ import annotations

import os


def resolve_token(cli_token: str | None, env_name: str) -> str | None:
    """Prefer non-empty CLI token; else read env_name; return None if unset."""
    if cli_token:
        return cli_token
    value = os.environ.get(env_name, "").strip()
    return value or None
