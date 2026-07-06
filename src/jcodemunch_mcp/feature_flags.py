"""Lightweight feature-flag system for safe agent-authored rollouts.

Flags default to OFF. Opt in per environment with
``JCM_FF_<FLAG_NAME_UPPER>=1``; unset or explicit false-like values leave the
flag disabled. Register new flags as attributes on ``FF``.
"""

from __future__ import annotations

import os


class _Flag:
    def __init__(self, name: str, description: str, *, default: bool = False):
        self.name = name
        self.description = description
        self.default = default
        self.env_key = f"JCM_FF_{name.upper()}"

    def __repr__(self) -> str:
        return f"<Flag {self.name} env={self.env_key}>"


class FF:
    """Feature flag registry. New flags are added here and read via is_enabled."""


KNOWN_FLAGS: dict[str, _Flag] = {
    name: val
    for name, val in vars(FF).items()
    if isinstance(val, _Flag) and not name.startswith("_")
}


def is_enabled(flag: _Flag) -> bool:
    """Return whether ``flag`` is enabled in the current environment."""
    raw = os.environ.get(flag.env_key)
    if raw is None:
        return flag.default
    return raw.lower() in ("1", "true", "on", "yes", "enabled")


def flag_status(flag: _Flag) -> dict[str, object]:
    """Return a structured status record for health checks or logging."""
    return {
        "name": flag.name,
        "env_key": flag.env_key,
        "enabled": is_enabled(flag),
        "description": flag.description,
        "default": flag.default,
    }


def all_flag_statuses() -> list[dict[str, object]]:
    return [flag_status(flag) for flag in KNOWN_FLAGS.values()]
