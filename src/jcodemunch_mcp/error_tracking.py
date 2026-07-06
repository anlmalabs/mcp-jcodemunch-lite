"""Soft error-tracking integration (Sentry / Bugsnag / Rollbar).

Activated via env vars:
- SENTRY_DSN — enables Sentry (https://sentry.io)
- BUGSNAG_API_KEY — enables Bugsnag (https://bugsnag.com)
- ROLLBAR_ACCESS_TOKEN — enables Rollbar (https://rollbar.com)

If none of these is set, the module is a no-op. This lets a Docker/self-hosted
deployment stay dependency-free while production SaaS installs gain rich
observability. Guarded imports so the server boots even if the SDK isn't
installed.
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_initialized = False


def _get_version() -> str:
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("jcodemunch-mcp")
        except PackageNotFoundError:
            return "dev"
    except ImportError:
        return "dev"


def init_error_tracking() -> None:
    """Activate error tracking if an SDK is configured.

    Safe to call at server startup with no env set (no-op).
    """
    global _initialized
    if _initialized:
        return
    _initialized = True
    dsns = {
        "sentry": os.environ.get("SENTRY_DSN"),
        "bugsnag": os.environ.get("BUGSNAG_API_KEY"),
        "rollbar": os.environ.get("ROLLBAR_ACCESS_TOKEN"),
    }
    active = {k: v for k, v in dsns.items() if v}
    if not active:
        return  # no SDK configured — quiet no-op
    for name, key in active.items():
        try:
            getattr(__import__(f"_jcm_error_{name}"), "init")(key, _get_version())
        except ImportError:
            log.warning(
                "%s SDK key set (%s=...) but SDK not installed. "
                "Run `pip install sentry-sdk` (or analog) to capture crashes.",
                name.upper(),
                _ENV_VAR_NAMES[name],
            )


# Map SDK name -> env var for clearer error messages.
_ENV_VAR_NAMES = {
    "sentry": "SENTRY_DSN",
    "bugsnag": "BUGSNAG_API_KEY",
    "rollbar": "ROLLBAR_ACCESS_TOKEN",
}
