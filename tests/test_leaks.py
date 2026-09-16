"""Secret leak scan over committable files, artifacts, and evidence, including zip trace members.

Secrets match raw, URL-encoded, JSON-escaped, or base64, since traces store POST bodies encoded.
"""

from __future__ import annotations

import base64
import json
import os
import zipfile
from pathlib import Path
from urllib.parse import quote_plus

import pytest
from dotenv import dotenv_values

from support import ROOT, candidate_files, find_leaks

SECRET_KEYS = ("CORELEDGER_OPERATOR_PASSWORD", "ANTHROPIC_API_KEY")
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


def test_the_operator_id_never_appears_in_an_artifact_or_an_evidence_file() -> None:
    """The operator id is an identity, not a credential: it is published in .env.example and used
    in tests on purpose. What it must never do is survive redaction into a committed run, in any
    encoding. A base64 form of it inside a trace POST body is exactly how it escaped once."""
    operator = (
        dotenv_values(ROOT / ".env").get("CORELEDGER_OPERATOR_USER")
        if (ROOT / ".env").exists()
        else None
    )
    operator = os.environ.get("CORELEDGER_OPERATOR_USER") or operator
    if not operator:
        if os.environ.get(REQUIRE_ENV) == "1":
            pytest.fail("no operator id configured; set CORELEDGER_OPERATOR_USER in .env")
        pytest.skip("no operator id configured")
    recorded = [
        path
        for tracked in ("artifacts", "evidence")
        for path in (ROOT / tracked).rglob("*")
        if path.is_file() and "_scratch" not in path.parts
    ]
    assert recorded, "scan found no committed artifacts or evidence"
    assert not find_leaks(recorded, {"operator id": operator})
