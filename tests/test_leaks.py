"""Secret leak scan over committable files, artifacts, and evidence, including zip trace members.

Secrets match raw, URL-encoded, JSON-escaped, or base64, since traces store POST bodies encoded.
"""

from __future__ import annotations

import base64
import json
import os
import re
import zipfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from urllib.parse import quote, quote_plus

import pytest
from dotenv import dotenv_values

from support import ROOT, candidate_files

SECRET_KEYS = ("CORELEDGER_OPERATOR_PASSWORD", "ANTHROPIC_API_KEY")
KEY_SHAPES = (re.compile(rb"sk-ant-[A-Za-z0-9_\-]{16,}"),)
MIN_SECRET_LEN = 8
# Pre-commit sets this so a machine with no secrets configured cannot pass the scan vacuously.
REQUIRE_ENV = "CUA_REQUIRE_SECRET_SCAN"


def configured_secrets() -> dict[str, str]:
    from_file = dotenv_values(ROOT / ".env") if (ROOT / ".env").exists() else {}
    found: dict[str, str] = {}
    for key in SECRET_KEYS:
        for source, value in (("env", os.environ.get(key)), (".env", from_file.get(key))):
            if value:
                found[f"{key} ({source})"] = value
    return found


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


def test_configured_secrets_are_long_enough_to_scan_exactly() -> None:
    short = [name for name, value in configured_secrets().items() if len(value) < MIN_SECRET_LEN]
    assert not short, (
        f"use secrets of at least {MIN_SECRET_LEN} chars so the scan is exact: {short}"
    )


def test_no_secret_in_committable_files_artifacts_or_evidence() -> None:
    files = candidate_files()
    assert files, "scan found no files; the enumeration is broken"
    secrets = configured_secrets()
    if not secrets:
        if os.environ.get(REQUIRE_ENV) == "1":
            pytest.fail(f"no secrets configured; set them in .env or unset {REQUIRE_ENV}")
        pytest.skip("no secrets configured, value scan has nothing to look for")
    assert not find_leaks(files, secrets)


def test_no_api_key_shapes_anywhere() -> None:
    assert not find_leaks(candidate_files(), {})


def test_scanner_catches_planted_secrets_in_every_encoding(tmp_path: Path) -> None:
    secret = "Tr@ce pass/word+1&x=y"
    plain = tmp_path / "run.jsonl"
    plain.write_text(json.dumps({"typed": secret}))
    trace = tmp_path / "trace.zip"
    with zipfile.ZipFile(trace, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("resources/post.dat", "operator=x&password=" + quote_plus(secret))
    encoded = tmp_path / "b64.txt"
    encoded.write_bytes(base64.b64encode(secret.encode()))
    shaped = tmp_path / "notes.md"
    shaped.write_bytes(b"key " + b"sk-ant-" + b"a" * 24)

    leaks = find_leaks([plain, trace, encoded, shaped], {"PW": secret})

    assert any("run.jsonl" in leak for leak in leaks)
    assert any("trace.zip!resources/post.dat" in leak for leak in leaks)
    assert any("b64.txt" in leak for leak in leaks)
    assert any("API key shape" in leak and "notes.md" in leak for leak in leaks)


@pytest.mark.parametrize("clean", [b"", b"password=[REDACTED]"])
def test_scanner_is_quiet_on_clean_content(tmp_path: Path, clean: bytes) -> None:
    path = tmp_path / "clean.txt"
    path.write_bytes(clean)
    assert find_leaks([path], {"PW": "planted-operator-password"}) == []
