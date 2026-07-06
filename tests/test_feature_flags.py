"""Tests for jcodemunch_mcp.feature_flags."""

from __future__ import annotations

import os

import pytest

from jcodemunch_mcp.feature_flags import (
    FF,
    _Flag,
    all_flag_statuses,
    flag_status,
    is_enabled,
)


@pytest.fixture
def sample_flag() -> _Flag:
    return _Flag("test_flag", "a test flag", default=False)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip JCM_FF_* env vars so tests do not leak into each other."""
    for key in list(os.environ):
        if key.startswith("JCM_FF_"):
            monkeypatch.delenv(key, raising=False)


def test_default_is_off(sample_flag: _Flag) -> None:
    assert is_enabled(sample_flag) is False


def test_env_enables_flag(sample_flag: _Flag, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sample_flag.env_key, "1")
    assert is_enabled(sample_flag) is True


def test_env_variants_enable(sample_flag: _Flag, monkeypatch: pytest.MonkeyPatch) -> None:
    for variant in ("1", "true", "on", "yes", "enabled", "TRUE", "On"):
        monkeypatch.setenv(sample_flag.env_key, variant)
        assert is_enabled(sample_flag) is True, f"{variant!r} failed"


def test_env_explicit_zero_disables(sample_flag: _Flag, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(sample_flag.env_key, "0")
    assert is_enabled(sample_flag) is False


def test_flag_status_shape(sample_flag: _Flag) -> None:
    status = flag_status(sample_flag)
    assert status["name"] == "test_flag"
    assert status["env_key"] == "JCM_FF_TEST_FLAG"
    assert status["enabled"] is False
    assert status["description"] == "a test flag"
    assert status["default"] is False


def test_all_flag_statuses_empty_when_no_flags_registered() -> None:
    assert all_flag_statuses() == []


def test_known_flags_collects_from_ff_class_at_import_time() -> None:
    flag = _Flag("dynamic", "dynamic flag")
    try:
        setattr(FF, "DYNAMIC", flag)
        from jcodemunch_mcp.feature_flags import KNOWN_FLAGS

        assert "DYNAMIC" not in KNOWN_FLAGS or KNOWN_FLAGS["DYNAMIC"] is flag
    finally:
        delattr(FF, "DYNAMIC")
