"""Tests for ``scripts/detect_dead_flags.py``.

The detector consumes the live ``jcodemunch_mcp.feature_flags.KNOWN_FLAGS``
registry. We run it **in-process** so ``monkeypatch.setitem`` on the same
dict the detector sees actually reflects. The CI step runs only the script,
but that path is covered by ``test_real_registry_passes_against_current_src``.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src" / "jcodemunch_mcp"

# Make the detector importable under pytest. ``scripts/`` lives next to the repo
# root; pytest's ``rootdir`` (=. / pyproject) doesn't auto-add it, so add it
# explicitly here. The detector also prepends ``src/`` to sys.path at import
# time, so once it's imported the feature_flags module is reachable.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from detect_dead_flags import main as detector_main  # noqa: E402


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _write_module(root: Path, filename: str, code: str) -> Path:
    pkg = root / "src" / "jcodemunch_mcp"
    pkg.mkdir(parents=True, exist_ok=True)
    f = pkg / filename
    f.write_text(textwrap.dedent(code), encoding="utf-8")
    return f


# ──────────────────────────────────────────────────────────────────────────────
# Tests
# ──────────────────────────────────────────────────────────────────────────────


def test_empty_registry_passes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """With no flags registered, the detector exits 0."""
    # Make the live KNOWN_FLAGS dict empty so the detector shrugs.
    from jcodemunch_mcp.feature_flags import KNOWN_FLAGS
    with monkeypatch.context() as m:
        original = dict(KNOWN_FLAGS)
        KNOWN_FLAGS.clear()
        try:
            rc = detector_main([f"--src-dir={tmp_path / 'src' / 'jcodemunch_mcp'}"])
            assert rc == 0, rc
        finally:
            KNOWN_FLAGS.update(original)


def test_live_flag_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A flag referenced at least once in src/ is reported as OK and exits 0."""
    _write_module(
        tmp_path,
        "fake_usage.py",
        """
        from jcodemunch_mcp.feature_flags import FF, is_enabled

        def use_flag():
            if is_enabled(FF.NEW_RANKING):
                return "new"
            return "legacy"
        """,
    )
    (tmp_path / "src" / "jcodemunch_mcp" / "__init__.py").write_text("", encoding="utf-8")

    from jcodemunch_mcp.feature_flags import KNOWN_FLAGS, _Flag
    with monkeypatch.context() as m:
        m.setitem(KNOWN_FLAGS, "NEW_RANKING", _Flag("NEW_RANKING", "test live flag"))
        rc = detector_main([f"--src-dir={tmp_path / 'src' / 'jcodemunch_mcp'}"])
        assert rc == 0, rc


def test_dead_flag_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A registered flag with zero src/ refs fails the detector."""
    empty_pkg = tmp_path / "src" / "jcodemunch_mcp"
    empty_pkg.mkdir(parents=True)
    (empty_pkg / "__init__.py").write_text("", encoding="utf-8")

    from jcodemunch_mcp.feature_flags import KNOWN_FLAGS, _Flag
    with monkeypatch.context() as m:
        m.setitem(KNOWN_FLAGS, "GHOST_FLAG", _Flag("GHOST_FLAG", "never referenced"))
        rc = detector_main([f"--src-dir={tmp_path / 'src' / 'jcodemunch_mcp'}"])
        assert rc != 0, rc


def test_detector_does_not_flag_itself(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """feature_flags.py' own references must not keep a stale flag alive."""
    _write_module(
        tmp_path,
        "feature_flags.py",
        """
        class _Flag:
            def __init__(self, name): self.name = name
        class FF:
            ONLY_FILE_LOCAL = _Flag("ONLY_FILE_LOCAL")
        KNOWN_FF_NAMES = ["ONLY_FILE_LOCAL"]
        """,
    )
    _write_module(
        tmp_path,
        "module.py",
        "# unrelated: this module doesn't touch the flagged code path\n",
    )

    from jcodemunch_mcp.feature_flags import KNOWN_FLAGS, _Flag
    with monkeypatch.context() as m:
        m.setitem(KNOWN_FLAGS, "ONLY_FILE_LOCAL", _Flag("ONLY_FILE_LOCAL", "only in ff.py"))
        rc = detector_main([f"--src-dir={tmp_path / 'src' / 'jcodemunch_mcp'}"])
        assert rc != 0, rc


def test_real_registry_passes_against_current_src():
    """Sanity: the live KNOWN_FLAGS against real src/ exits 0."""
    rc = detector_main([])
    from jcodemunch_mcp.feature_flags import KNOWN_FLAGS
    if not KNOWN_FLAGS:
        assert rc == 0


def test_script_invocation_end_to_end(tmp_path: Path):
    """The script can also be invoked via ``sys.executable -m``.

    Uses the live registry (currently empty); the script must exit 0 with the
    vacuously-passes banner.
    """
    empty_pkg = tmp_path / "src" / "jcodemunch_mcp"
    empty_pkg.mkdir(parents=True)
    (empty_pkg / "__init__.py").write_text("", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "scripts" / "detect_dead_flags.py"),
            f"--src-dir={tmp_path / 'src' / 'jcodemunch_mcp'}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    from jcodemunch_mcp.feature_flags import KNOWN_FLAGS
    if not KNOWN_FLAGS:
        assert result.returncode == 0, result.stdout + result.stderr
        assert "vacuously passes" in result.stdout
