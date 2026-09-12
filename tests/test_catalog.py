"""Catalog: save, list, get, and approve capabilities on disk, with version rules enforced at save.

Runs against a temp directory seeded from the example, never against the real artifacts/ folder.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from cua.artifact.catalog import EXAMPLE_FILE, Catalog, CatalogError, load_capability
from cua.artifact.schema import Capability
from cua.artifact.versioning import VersionError
from cua.cli import app

from support import ROOT

EXAMPLE = ROOT / "artifacts" / "example.capability.json"


def _capability(version: str = "1.0.0", **meta: Any) -> Capability:
    data: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["capability"].update(version=version, **meta)
    return Capability.model_validate(data)


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    (tmp_path / EXAMPLE_FILE).write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return Catalog(tmp_path)


def test_save_uses_id_at_version_and_ignores_the_example(catalog: Catalog) -> None:
    assert catalog.entries() == []
    path = catalog.save(_capability())
    assert path.name == "coreledger.member.read_savings_balance@1.0.0.capability.json"
    assert [(e.id, e.version, e.status) for e in catalog.entries()] == [
        ("coreledger.member.read_savings_balance", "1.0.0", "draft")
    ]


def test_get_returns_latest_non_deprecated(catalog: Catalog) -> None:
    catalog.save(_capability("1.0.0"))
    catalog.save(_capability("1.0.1"))
    capability, _ = catalog.get("coreledger.member.read_savings_balance")
    assert capability.capability.version == "1.0.1"
    with pytest.raises(CatalogError, match="no capability"):
        catalog.get("coreledger.member.close_account")


def test_save_refuses_overwrite_and_undersized_bumps(catalog: Catalog) -> None:
    catalog.save(_capability("1.0.0"))
    with pytest.raises(CatalogError, match="already exists"):
        catalog.save(_capability("1.0.0"))
    changed: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    changed["capability"]["version"] = "1.0.1"
    changed["steps"][5]["wait_for"]["timeout_ms"] = 2500
    with pytest.raises(VersionError, match="needs a minor bump"):
        catalog.save(Capability.model_validate(changed))


def test_approve_flips_draft_and_records_who(catalog: Catalog) -> None:
    catalog.save(_capability())
    now = datetime(2026, 9, 12, 16, 30, 15, 123, tzinfo=UTC)
    path = catalog.approve("coreledger.member.read_savings_balance", "1.0.0", "shrey", now=now)
    approved = load_capability(path)
    assert approved.capability.status == "approved"
    assert approved.capability.approved_by == "shrey"
    assert approved.capability.approved_at == now.replace(microsecond=0)
    with pytest.raises(CatalogError, match="is approved, not draft"):
        catalog.approve("coreledger.member.read_savings_balance", "1.0.0", "shrey")


def test_file_name_must_match_contents(catalog: Catalog) -> None:
    catalog.save(_capability())
    wrong = catalog.root / "coreledger.member.read_savings_balance@9.9.9.capability.json"
    (catalog.root / Catalog.filename(_capability())).rename(wrong)
    with pytest.raises(CatalogError, match="expected file name"):
        catalog.entries()


def test_cli_list_show_approve(catalog: Catalog) -> None:
    catalog.save(_capability())
    runner = CliRunner()
    root = ["--root", str(catalog.root)]
    listed = runner.invoke(app, ["catalog", "list", *root])
    assert listed.exit_code == 0
    assert "coreledger.member.read_savings_balance@1.0.0  draft" in listed.output

    shown = runner.invoke(app, ["catalog", "show", "coreledger.member.read_savings_balance", *root])
    assert shown.exit_code == 0
    contract = json.loads(shown.output)
    assert contract["inputs"][0]["name"] == "member_id"
    assert contract["business_outcomes"] == ["MEMBER_NOT_FOUND", "VALIDATION_ERROR"]

    approved = runner.invoke(
        app,
        ["catalog", "approve", "coreledger.member.read_savings_balance", "--by", "shrey", *root],
    )
    assert approved.exit_code == 0, approved.output
    assert "approved coreledger.member.read_savings_balance@1.0.0" in approved.output

    missing = runner.invoke(app, ["catalog", "show", "coreledger.member.nope", *root])
    assert missing.exit_code == 1
