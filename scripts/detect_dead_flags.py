#!/usr/bin/env python3
"""Detect registered feature flags that are dead (unreferenced in ``src/``).

Runs in CI (``uv run python scripts/detect_dead_flags.py``) and exits non-zero
whenever any flag in ``jcodemunch_mcp.feature_flags.KNOWN_FLAGS`` is not
mentioned anywhere under ``src/jcodemunch_mcp/`` outside of
``feature_flags.py`` itself.

Empty registry → exits 0 with ``[ok] no active feature flags — dead flag
detection vacuously passes`` (no extra CI special-casing).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_THIS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_DIR.parent
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from jcodemunch_mcp.feature_flags import KNOWN_FLAGS  # noqa: E402

SRC_DIR: Path = _REPO_ROOT / "src" / "jcodemunch_mcp"
EXCLUDED: frozenset[str] = frozenset({"feature_flags.py"})


def _collect(flags, *, src_dir: Path = SRC_DIR) -> dict[str, int]:
    """Count each flag's ``name`` substring across ``src_dir``.

    Skips ``feature_flags.py`` so the registry file itself never counts.
    """
    counts = {f.name: 0 for f in flags}
    for py in src_dir.rglob("*.py"):
        if py.name in EXCLUDED:
            continue
        if any(p.startswith(".") for p in py.relative_to(src_dir).parts):
            continue
        try:
            text = py.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for name in counts:
            counts[name] += text.count(name)
    return counts


def _report(refs: dict[str, int]) -> tuple[int, list[str]]:
    lines = [f"{n}: {'OK' if c > 0 else 'DEAD'} ({c} references in src/)" for n, c in refs.items()]
    if not refs:
        lines.append("[ok] no active feature flags — dead flag detection vacuously passes")
    dead = sum(1 for c in refs.values() if c == 0)
    return (dead and 1 or 0), lines


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Detect registered feature flags unreferenced in src/")
    p.add_argument("--no-exit-on-dead", action="store_true", help="Report but exit 0.")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    p.add_argument("--src-dir", type=Path, default=None, help="Override src/ directory (tests).")
    args = p.parse_args(argv)

    flags = list(KNOWN_FLAGS.values())
    src_dir = args.src_dir or SRC_DIR
    refs = _collect(flags, src_dir=src_dir)

    if args.json:
        import json
        json.dump({"flags": refs, "dead": [n for n, c in refs.items() if c == 0]}, sys.stdout)
        sys.stdout.write("\n")
        sys.stdout.flush()
        return 0 if all(c > 0 for c in refs.values()) else 1

    exit_code, lines = _report(refs)
    print("\n".join(lines))
    dead = sum(1 for c in refs.values() if c == 0)
    print(f"[dead-flags] {dead} dead flags", file=sys.stderr)
    return 0 if args.no_exit_on_dead else exit_code


if __name__ == "__main__":
    raise SystemExit(main())
