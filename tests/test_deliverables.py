"""The brief's deliverables, checked the way a reviewer reads them: paths, headings, and claims.

A wrong heading or a command that no longer exists is a submission defect, so it fails the suite.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cua.artifact.catalog import Catalog

from support import ROOT

README = ROOT / "README.md"
REPORT = ROOT / "REPORT.md"
EVIDENCE = ROOT / "evidence"
EM_DASH = "\u2014"
# Section 6.2 of the brief, verbatim and in order. It says: use these exact headings.
REPORT_HEADINGS = [
    "Architecture",
    "Artifact schema",
    "Determinism & error handling",
    "Heterogeneity & multi-tenant",
    "Escalation & handoff",
    "Safety",
    "Cuts",
]


def test_the_report_uses_the_briefs_seven_headings_in_order() -> None:
    found = re.findall(r"^## (.+)$", REPORT.read_text(encoding="utf-8"), flags=re.M)
    assert found == REPORT_HEADINGS


def test_the_readme_covers_setup_a_demo_path_and_running_without_a_model() -> None:
    text = README.read_text(encoding="utf-8")
    for needed in (
        "uv sync",
        "playwright install chromium",
        ".env",
        "cua discover",
        "cua replay",
        "cua ops take-control",
    ):
        assert needed in text, needed
    assert "## The path with no LLM" in text, "the brief asks how to run without live services"


@pytest.mark.parametrize(
    "document", [README, REPORT, ROOT / "DECISIONS.md", EVIDENCE / "README.md"]
)
def test_the_write_ups_have_no_em_dashes(document: Path) -> None:
    assert EM_DASH not in document.read_text(encoding="utf-8")


def _run_dirs() -> list[Path]:
    return [p for p in EVIDENCE.iterdir() if p.is_dir() and p.name != "_scratch"]


def test_every_committed_run_is_described_and_every_description_exists() -> None:
    """A reviewer reads evidence/README.md as the index. It has to match the directory."""
    listed = set(
        re.findall(r"(?:replay|disc|stability)_[0-9A-Za-z_]+", (EVIDENCE / "README.md").read_text())
    )
    on_disk = {p.name for p in _run_dirs()}
    assert on_disk - listed == set(), "a committed run nobody explained"
    assert {name for name in listed if name not in on_disk} == set(), "a described run that is gone"


def test_the_evidence_holds_both_a_discovery_run_and_a_replay_that_hit_an_exceptional_state() -> (
    None
):
    """Brief section 6.3: logs from both a discovery run and a replay run, and ideally one replay
    that hits an error or an exceptional state."""
    kinds = {p.name.split("_")[0] for p in _run_dirs()}
    assert {"disc", "replay", "stability"} <= kinds

    results = [
        json.loads((p / "result.json").read_text())
        for p in _run_dirs()
        if (p / "result.json").exists()
    ]
    statuses = {r.get("status") for r in results}
    assert {
        "success",
        "business_outcome",
        "recovered_then_success",
        "hard_failure",
        "escalated",
    } <= statuses
    codes = {r.get("outcome_code") for r in results}
    assert {
        "MEMBER_NOT_FOUND",
        "VALIDATION_ERROR",
        "PERMISSION_DENIED",
        "CONFIRMATION_REQUIRED",
    } <= codes


def test_a_failed_run_carries_the_richer_signal_the_brief_asks_for() -> None:
    """Section 3.5: at least one richer signal on failure. Ours is a screenshot, an accessibility
    snapshot, and a scrubbed trace."""
    for run in _run_dirs():
        result_file = run / "result.json"
        if not result_file.exists():
            continue
        result = json.loads(result_file.read_text())
        if result.get("status") != "hard_failure" or result.get("step_reached") is None:
            continue
        files = {p.name for p in run.iterdir()}
        assert any(f.startswith("step_") and f.endswith(".png") for f in files), run.name
        assert any(f.startswith("a11y_") for f in files), run.name
        assert "trace.zip" in files, run.name


def test_every_capability_the_readme_demonstrates_is_approved_and_replayable() -> None:
    catalog = Catalog(ROOT / "artifacts")
    demonstrated = set(re.findall(r"cua replay (coreledger\.[a-z_.]+)", README.read_text()))
    assert demonstrated, "the README demonstrates no capability"
    for capability_id in demonstrated:
        if capability_id.startswith("coreledger.demo."):
            continue  # recorded by the reader when they run the discovery demo
        latest, _path = catalog.get(capability_id)
        assert latest.capability.status == "approved", capability_id
        assert latest.capability.approved_by, capability_id
        assert latest.steps, capability_id
        assert latest.outputs, capability_id
        assert latest.outcome_detectors, capability_id
