"""Test support: the committable-file enumeration and shared fixture types.

Imported by tests directly, so nothing needs to import conftest as a module.
"""

from __future__ import annotations

import base64
import json
import re
import subprocess
import zipfile
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from urllib.parse import quote, quote_plus

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


KEY_SHAPES = (re.compile(rb"sk-ant-[A-Za-z0-9_\-]{16,}"),)


def encodings(secret: str) -> set[bytes]:
    """Every form a secret plausibly takes on disk: raw, form-encoded, JSON-escaped, base64."""
    forms = {secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret)[1:-1]}
    raw = {f.encode() for f in forms}
    raw.add(base64.b64encode(secret.encode()).rstrip(b"="))
    return raw


def _blobs(path: Path) -> Iterator[tuple[str, bytes]]:
    label = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    yield label, path.read_bytes()
    if path.suffix == ".zip" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                yield f"{label}!{member}", archive.read(member)


def find_leaks(files: Iterable[Path], secrets: dict[str, str]) -> list[str]:
    needles = {name: encodings(value) for name, value in secrets.items()}
    leaks: list[str] = []
    for path in files:
        for label, data in _blobs(path):
            leaks += [
                f"{name} in {label}"
                for name, forms in needles.items()
                if any(f in data for f in forms)
            ]
            leaks += [f"API key shape in {label}" for shape in KEY_SHAPES if shape.search(data)]
    return leaks
