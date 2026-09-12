"""Secret leak scan over committable files, artifacts, and evidence, including inside zip traces.

CI uses a throwaway password, so the value scan bites hardest in the pre-commit hook, which runs
against the developer's real .env. The key-shape scan is meaningful everywhere.
"""

from __future__ import annotations

import os
import re
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path

import pytest
from dotenv import dotenv_values

from repo_files import ROOT, candidate_files

SECRET_KEYS = ("CORELEDGER_OPERATOR_PASSWORD", "ANTHROPIC_API_KEY")
KEY_SHAPES = (re.compile(rb"sk-ant-[A-Za-z0-9_\-]{16,}"),)
MIN_SECRET_LEN = 8


def configured_secrets() -> dict[str, bytes]:
    from_file = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
    found: dict[str, bytes] = {}
    for key in SECRET_KEYS:
        for source, value in (("env", os.environ.get(key)), (".env", from_file.get(key))):
            if value:
                found[f"{key} ({source})"] = value.encode()
    return found


def _blobs(path: Path) -> Iterator[tuple[str, bytes]]:
    label = str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)
    data = path.read_bytes()
    yield label, data
    if path.suffix == ".zip" and zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for member in archive.namelist():
                yield f"{label}!{member}", archive.read(member)


def find_leaks(files: Iterable[Path], secrets: dict[str, bytes]) -> list[str]:
    leaks: list[str] = []
    for path in files:
        for label, data in _blobs(path):
            leaks += [f"{name} in {label}" for name, value in secrets.items() if value in data]
            leaks += [f"API key shape in {label}" for shape in KEY_SHAPES if shape.search(data)]
    return leaks


def test_configured_secrets_are_long_enough_to_scan_exactly() -> None:
    short = [name for name, value in configured_secrets().items() if len(value) < MIN_SECRET_LEN]
    assert not short, (
        f"use secrets of at least {MIN_SECRET_LEN} chars so the scan is exact: {short}"
    )


def test_no_secret_in_committable_files_artifacts_or_evidence() -> None:
    files = candidate_files()
    assert files, "scan found no files; the enumeration is broken"
    assert not find_leaks(files, configured_secrets())


def test_scanner_catches_planted_secrets_in_plain_files_and_zip_members(tmp_path: Path) -> None:
    secret = b"planted-operator-password"
    plain = tmp_path / "run.jsonl"
    plain.write_bytes(b'{"typed": "' + secret + b'"}')
    trace = tmp_path / "trace.zip"
    with zipfile.ZipFile(trace, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("network.log", b"password=" + secret)
    shaped = tmp_path / "notes.md"
    shaped.write_bytes(b"key " + b"sk-ant-" + b"a" * 24)

    leaks = find_leaks([plain, trace, shaped], {"PW": secret})

    assert any("run.jsonl" in leak for leak in leaks)
    assert any("trace.zip!network.log" in leak for leak in leaks)
    assert any("API key shape" in leak and "notes.md" in leak for leak in leaks)


@pytest.mark.parametrize("clean", [b"", b"password=[REDACTED]"])
def test_scanner_is_quiet_on_clean_content(tmp_path: Path, clean: bytes) -> None:
    path = tmp_path / "clean.txt"
    path.write_bytes(clean)
    assert find_leaks([path], {"PW": b"planted-operator-password"}) == []
