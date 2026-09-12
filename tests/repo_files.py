"""Enumerate the files that are committed or could be committed, plus artifacts and evidence.

Shared by the leak scan and the repo hygiene rules so both look at exactly the same set.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_WALK_SKIP = {".git", ".venv", ".mypy_cache", ".ruff_cache", ".pytest_cache", "__pycache__"}


def candidate_files() -> list[Path]:
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
        files |= set((ROOT / tracked_dir).rglob("*"))
    return sorted(p for p in files if p.is_file())
