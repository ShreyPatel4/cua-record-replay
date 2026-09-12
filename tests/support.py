"""Test support: the committable-file enumeration and shared fixture types.

Imported by tests directly, so nothing needs to import conftest as a module.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_WALK_SKIP = {".git", ".venv", ".mypy_cache", ".ruff_cache", ".pytest_cache", "__pycache__"}
_UNCOMMITTED = {"_scratch"}

# inject("slow", ms="300") arms a fault; times=None keeps it until reset, omit it for the default.
Injector = Callable[..., None]


def candidate_files() -> list[Path]:
    """Files git would commit, plus everything under artifacts/ and evidence/ except scratch."""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout
        files = {ROOT / p for p in out.decode().split("\0") if p}
    except (FileNotFoundError, subprocess.CalledProcessError):
        files = {p for p in ROOT.rglob("*") if not (_WALK_SKIP & set(p.relative_to(ROOT).parts))}
    for tracked_dir in ("artifacts", "evidence"):
        files |= {
            p
            for p in (ROOT / tracked_dir).rglob("*")
            if not (_UNCOMMITTED & set(p.relative_to(ROOT).parts))
        }
    return sorted(p for p in files if p.is_file())
