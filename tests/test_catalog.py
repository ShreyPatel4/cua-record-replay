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
POLICY_FILE = ROOT / "policy" / "allowlist.yaml"
CAPABILITY_ID = "coreledger.member.read_savings_balance"


def _data(version: str = "1.0.0", **meta: Any) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["capability"].update(version=version, **meta)
    return data


def _capability(version: str = "1.0.0", **meta: Any) -> Capability:
    return Capability.model_validate(_data(version, **meta))


@pytest.fixture
def catalog(tmp_path: Path) -> Catalog:
    (tmp_path / EXAMPLE_FILE).write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
    return Catalog(tmp_path, POLICY_FILE)


def test_save_uses_id_at_version_and_ignores_the_example(catalog: Catalog) -> None:
    assert catalog.entries() == []
    path = catalog.save(_capability())
    assert path.name == f"{CAPABILITY_ID}@1.0.0.capability.json"
    assert [(e.id, e.version, e.status) for e in catalog.entries()] == [
        (CAPABILITY_ID, "1.0.0", "draft")
    ]


def test_get_prefers_the_latest_approved_version_over_a_newer_draft(catalog: Catalog) -> None:
    catalog.save(_capability("1.0.0"))
    with pytest.raises(CatalogError, match="no approved version"):
        catalog.get(CAPABILITY_ID)
    catalog.approve(CAPABILITY_ID, "1.0.0", "shrey")
    catalog.save(_capability("1.0.1"))
    assert catalog.get(CAPABILITY_ID)[0].capability.version == "1.0.0"
    assert catalog.get(CAPABILITY_ID, include_drafts=True)[0].capability.version == "1.0.1"
    with pytest.raises(CatalogError, match="no capability"):
        catalog.get("coreledger.member.close_account")


def test_save_refuses_overwrite_and_undersized_bumps(catalog: Catalog) -> None:
    catalog.save(_capability("1.0.0"))
    with pytest.raises(CatalogError, match="already exists"):
        catalog.save(_capability("1.0.0"))
    changed = _data("1.0.1")
    changed["steps"][5]["wait_for"]["timeout_ms"] = 2500
    with pytest.raises(VersionError, match="needs a minor bump"):
        catalog.save(Capability.model_validate(changed))


def test_approved_artifacts_are_never_overwritten_or_saved_directly(catalog: Catalog) -> None:
    catalog.save(_capability())
    catalog.approve(CAPABILITY_ID, "1.0.0", "shrey")
    with pytest.raises(CatalogError, match="never overwritten"):
        catalog.save(_capability(), overwrite=True)
    approved = _capability(
        "2.0.0", status="approved", approved_by="x", approved_at="2026-09-12T15:00:00Z"
    )
    with pytest.raises(CatalogError, match="only drafts are saved"):
        catalog.save(approved)


def test_approve_flips_draft_and_records_who(catalog: Catalog) -> None:
    catalog.save(_capability())
    now = datetime(2026, 9, 12, 16, 30, 15, 123, tzinfo=UTC)
    path = catalog.approve(CAPABILITY_ID, "1.0.0", "shrey", now=now)
    approved = load_capability(path)
    assert approved.capability.status == "approved"
    assert approved.capability.approved_by == "shrey"
    assert approved.capability.approved_at == now.replace(microsecond=0)
    with pytest.raises(CatalogError, match="is approved, not draft"):
        catalog.approve(CAPABILITY_ID, "1.0.0", "shrey")


def test_file_name_must_match_contents(catalog: Catalog) -> None:
    catalog.save(_capability())
    wrong = catalog.root / f"{CAPABILITY_ID}@9.9.9.capability.json"
    (catalog.root / Catalog.filename(_capability())).rename(wrong)
    with pytest.raises(CatalogError, match="expected file name"):
        catalog.entries()


def test_a_broken_file_is_named_in_the_error(catalog: Catalog) -> None:
    catalog.save(_capability())
    (catalog.root / "coreledger.member.broken@1.0.0.capability.json").write_text("{}")
    with pytest.raises(CatalogError, match=r"1 bad catalog file.*coreledger\.member\.broken"):
        catalog.entries()


def test_an_off_policy_tenant_host_fails_at_load(catalog: Catalog) -> None:
    data = _data()
    data["tenant_overrides"] = {
        "harbor-fcu": {"description": "Own host.", "entry_url": "http://harbor.example.test:5050/"}
    }
    catalog.save(Capability.model_validate(data))
    with pytest.raises(CatalogError, match="tenant harbor-fcu: entry_url is off policy"):
        catalog.entries()


def test_cli_list_show_approve(catalog: Catalog, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    catalog.save(_capability())
    runner = CliRunner()
    root = ["--root", str(catalog.root)]
    listed = runner.invoke(app, ["catalog", "list", *root])
    assert listed.exit_code == 0, listed.output
    assert f"{CAPABILITY_ID}@1.0.0  draft" in listed.output

    shown = runner.invoke(app, ["catalog", "show", CAPABILITY_ID, *root])
    assert shown.exit_code == 0
    contract = json.loads(shown.output)
    assert contract["inputs"][0]["name"] == "member_id"
    assert contract["business_outcomes"] == ["MEMBER_NOT_FOUND", "VALIDATION_ERROR"]

    approved = runner.invoke(app, ["catalog", "approve", CAPABILITY_ID, "--by", "shrey", *root])
    assert approved.exit_code == 0, approved.output
    assert f"approved {CAPABILITY_ID}@1.0.0" in approved.output

    missing = runner.invoke(app, ["catalog", "show", "coreledger.member.nope", *root])
    assert missing.exit_code == 1
