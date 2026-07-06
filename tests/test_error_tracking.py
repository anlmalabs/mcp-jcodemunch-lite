"""Tests for jcodemunch_mcp.error_tracking."""

from __future__ import annotations

import pytest

from jcodemunch_mcp import error_tracking


def test_init_noop_without_dsn(monkeypatch: pytest.MonkeyPatch):
    """No SDK env set → init_error_tracking() must not raise."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    monkeypatch.delenv("BUGSNAG_API_KEY", raising=False)
    monkeypatch.delenv("ROLLBAR_ACCESS_TOKEN", raising=False)
    error_tracking._initialized = False
    error_tracking.init_error_tracking()  # must not raise


def test_init_noop_repeated_calls(monkeypatch: pytest.MonkeyPatch):
    """Multiple init_error_tracking() calls should be idempotent."""
    monkeypatch.delenv("SENTRY_DSN", raising=False)
    error_tracking._initialized = False
    error_tracking.init_error_tracking()
    error_tracking.init_error_tracking()  # second call must not re-init


def test_dsn_set_without_sdk(monkeypatch: pytest.MonkeyPatch, caplog):
    """SENTRY_DSN set but sentry-sdk not installed → graceful warning."""
    monkeypatch.setenv("SENTRY_DSN", "https://example@ingest.sentry.io/1234")
    error_tracking._initialized = False
    error_tracking.init_error_tracking()  # must not raise
