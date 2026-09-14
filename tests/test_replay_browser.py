"""Replay against live CoreLedger: one test per status and per detector, evidence, and the CLI.

Runs the reviewed 1.1.0 artifact with the entry URL and policy moved to the test server's port.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar

import pytest
from playwright.sync_api import Browser

from cua.artifact.catalog import dump_json
from cua.artifact.schema import Capability
from cua.policy.redact import REDACTED
from cua.replay.result import AutomaticRecovery, DriftWarning, MoneyValue, ReplayResult
from cua.replay.run import ReplayRequest, run_replay, run_stability
from cua.surface.playwright import PlaywrightSurface
from mock_app.server import RunningMockApp
from mock_app.settings import MockSettings

from support import ROOT, Injector, find_leaks

pytestmark = pytest.mark.browser
ARTIFACT = ROOT / "artifacts" / "coreledger.member.read_savings_balance@1.1.0.capability.json"
POLICY_FILE = ROOT / "policy" / "allowlist.yaml"
RECORDED_ORIGIN = "http://127.0.0.1:5050"
BALANCE_10007 = MoneyValue(amount=Decimal("4210.55"), currency="USD")
SIGN_OUT_STEP = {
    "id": "s041",
    "action": "click",
    "description": "Sign out, which the policy denies.",
    "target": {
        "ladder": [
            {"strategy": "role_name", "role": "link", "name": "Sign out", "confidence": 0.9}
        ],
        "recorded_rung": 0,
        "frame_path": ["banner"],
        "fingerprint": {"role": "link", "name": "Sign out", "kind": "clickable"},
        "notes": "Test step: a click whose navigation the gate refuses.",
    },
    "risk": "safe",
    "wait_for": {"kind": "settle", "quiet_ms": 300, "timeout_ms": 5000},
}


class CookieKeepingSurface(PlaywrightSurface):
    """Remembers the session cookies each run ends with, so leak scans can look for them."""

    seen: ClassVar[set[str]] = set()

    def close(self) -> None:
        with suppress(Exception):
            CookieKeepingSurface.seen.update(self.session_tokens())
        super().close()


@dataclass
class Replayer:
    capability: Capability
    policy_file: Path
    environ: dict[str, str]
    evidence_root: Path
    open_surface: Callable[[], PlaywrightSurface]

    def run(
        self, member: str, capability: Capability | None = None, **request: Any
    ) -> ReplayResult:
        chosen = capability or self.capability
        fields: dict[str, Any] = {
            "capability": chosen,
            "inputs": {"member_number": member},
            "policy_file": self.policy_file,
            "evidence_root": self.evidence_root,
            "allow_draft": True,
        }
        fields.update(request)
        result = run_replay(
            ReplayRequest(**fields), environ=self.environ, open_surface=self.open_surface
        )
        assert result.contract_problems(chosen) == []
        return result

    def tweak(self, mutate: Callable[[dict[str, Any]], None]) -> Capability:
        data = self.capability.model_dump(mode="json")
        mutate(data)
        return Capability.model_validate(data)


@pytest.fixture
def replayer(
    coreledger: RunningMockApp, mock_settings: MockSettings, browser: Browser, tmp_path: Path
) -> Replayer:
    base = coreledger.base_url
    policy_file = tmp_path / "allowlist.yaml"
    policy_file.write_text(POLICY_FILE.read_text().replace(RECORDED_ORIGIN, base))
    capability = Capability.model_validate_json(
        ARTIFACT.read_text().replace(f"{RECORDED_ORIGIN}/", f"{base}/")
    )
    return Replayer(
        capability=capability,
        policy_file=policy_file,
        environ={
            "CORELEDGER_OPERATOR_USER": mock_settings.operator_user,
            "CORELEDGER_OPERATOR_PASSWORD": mock_settings.operator_password,
        },
        evidence_root=tmp_path / "evidence",
        open_surface=lambda: CookieKeepingSurface.launch(browser, control=lambda: None),
    )


def _files(result: ReplayResult) -> list[Path]:
    return sorted(p for p in Path(result.evidence_dir).rglob("*") if p.is_file())


def _manifest(result: ReplayResult) -> dict[str, bool]:
    data = json.loads((Path(result.evidence_dir) / "manifest.json").read_text())
    return {f["path"]: f["sensitive"] for f in data["files"]}


def _assert_no_secrets(result: ReplayResult, settings: MockSettings) -> None:
    """Credentials, the operator id, and session cookies, in every file and every trace member.

    Cookies deleted mid-run are not in the jar at the end, so trace text is also checked for any
    cookie header or CLSESSID pair whose value survived."""
    secrets = {"password": settings.operator_password, "operator": settings.operator_user}
    secrets |= {f"cookie{i}": v for i, v in enumerate(sorted(CookieKeepingSurface.seen))}
    assert not find_leaks(_files(result), secrets)
    trace = Path(result.evidence_dir) / "trace.zip"
    if trace.exists():
        with zipfile.ZipFile(trace) as archive:
            for name in archive.namelist():
                data = archive.read(name)
                assert not re.search(rb"CLSESSID=(?!\[REDACTED\])", data), name
                assert not re.search(rb"(?i)\bcookie[ \t]*+:[ \t]*+(?!\[REDACTED\])", data), name


def _events(result: ReplayResult) -> list[dict[str, Any]]:
    lines = (Path(result.evidence_dir) / "run.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _only(result: ReplayResult) -> AutomaticRecovery:
    assert len(result.recoveries) == 1
    recovery = result.recoveries[0]
    assert isinstance(recovery, AutomaticRecovery)
    return recovery


# ---- one test per status --------------------------------------------------------------------


def test_success_returns_the_balance_and_keeps_it_out_of_evidence(
    replayer: Replayer, mock_settings: MockSettings
) -> None:
    result = replayer.run("10007")
    assert (result.status, result.exit_code) == ("success", 0)
    assert result.outputs == {"savings_balance": BALANCE_10007}
    assert [s.step_id for s in result.steps] == ["s01", "s02", "s03", "s04", "s05", "s06"]
    assert result.inputs == {"member_number": "*0007"}
    names = {p.name for p in _files(result)}
    assert {"run.jsonl", "result.json", "manifest.json", "step_06.png"} <= names
    assert "trace.zip" not in names, "traces are kept for failures only"
    for path in _files(result):
        data = path.read_bytes()
        assert b"4210.55" not in data, path.name
        assert b"4,210.55" not in data, path.name
    stored = json.loads((Path(result.evidence_dir) / "result.json").read_text())
    assert stored["outputs"] == {"savings_balance": REDACTED}
    assert _manifest(result)["step_06.png"] is True, "the balance is on screen"
    _assert_no_secrets(result, mock_settings)


def test_member_not_found_is_an_answer_so_it_exits_zero(replayer: Replayer) -> None:
    """A calling agent asked about a member who does not exist: that is information, not a crash."""
    result = replayer.run("99999")
    assert (result.status, result.outcome_code, result.exit_code) == (
        "business_outcome",
        "MEMBER_NOT_FOUND",
        0,
    )
    assert result.step_reached == "s06"
    assert [(e.field, e.message) for e in result.field_errors] == [
        ("member_number", "No member found for member number *9999.")
    ]
    assert not result.outputs


def test_interstitial_is_dismissed_then_the_run_succeeds(
    replayer: Replayer, inject: Injector
) -> None:
    inject("interstitial")
    result = replayer.run("10007")
    assert (result.status, result.exit_code) == ("recovered_then_success", 0)
    recovery = _only(result)
    assert (
        recovery.detector_id,
        recovery.recovery_kind,
        recovery.attempts,
        recovery.succeeded,
    ) == (
        "od_system_notice",
        "click",
        1,
        True,
    )
    assert result.outputs == {"savings_balance": BALANCE_10007}


def test_permission_denied_is_a_hard_failure_with_trace_snapshot_and_screenshot(
    replayer: Replayer, mock_settings: MockSettings
) -> None:
    result = replayer.run("10013")
    assert (result.status, result.outcome_code, result.exit_code) == (
        "hard_failure",
        "PERMISSION_DENIED",
        2,
    )
    assert result.observed
    assert "Access denied" in result.observed
    evidence = Path(result.evidence_dir)
    assert {"trace.zip", "a11y_06.json", "step_06.png"} <= {p.name for p in _files(result)}
    assert _manifest(result)["trace.zip"] is True
    with zipfile.ZipFile(evidence / "trace.zip") as archive:
        names = archive.namelist()
        network = b"".join(archive.read(n) for n in names if n.endswith(".network"))
    assert any(n.endswith(".trace") for n in names)
    assert b"/login" in network, "the trace covers sign-in, so the scrub is what keeps it clean"
    assert b"CLSESSID=" not in network
    _assert_no_secrets(result, mock_settings)


def test_escalated_status_needs_the_operator_channel(replayer: Replayer, inject: Injector) -> None:
    """Escalating detectors reach one hook; with no operator channel attached the run stops as a
    hard failure that keeps the escalating code and says a human is needed."""
    inject("app_error", on="detail")
    result = replayer.run("10007")
    assert (result.status, result.outcome_code) == ("hard_failure", "APP_ERROR")
    assert result.message
    assert "needs a human (HARD_FAILURE_ESCALATE)" in result.message
    assert result.intervention_path is None


# ---- one test per detector --------------------------------------------------------------------


def test_slow_profile_trips_the_step_timeout_and_recovers_by_waiting(
    replayer: Replayer, inject: Injector
) -> None:
    inject("slow", ms="4500")
    result = replayer.run("10007")
    assert result.status == "recovered_then_success"
    recovery = _only(result)
    assert (recovery.detector_id, recovery.recovery_kind, recovery.succeeded) == (
        "od_slow_member_load",
        "wait_retry",
        True,
    )


def test_an_exhausted_recovery_keeps_its_original_code(
    replayer: Replayer, inject: Injector
) -> None:
    def short_budget(data: dict[str, Any]) -> None:
        slow = next(d for d in data["outcome_detectors"] if d["id"] == "od_slow_member_load")
        slow["outcome"]["recovery"]["max_total_ms"] = 1000

    inject("slow", ms="6000")
    result = replayer.run("10007", replayer.tweak(short_budget))
    assert (result.status, result.outcome_code) == ("hard_failure", "SLOW_LOAD")
    assert not _only(result).succeeded
    assert "trace.zip" in {p.name for p in _files(result)}


def test_an_expired_session_signs_in_again_and_finishes(
    replayer: Replayer, inject: Injector
) -> None:
    inject("session_expired")
    result = replayer.run("10007")
    assert result.status == "recovered_then_success"
    recovery = _only(result)
    assert (recovery.detector_id, recovery.recovery_kind, recovery.succeeded) == (
        "od_session_expired",
        "run_steps",
        True,
    )
    assert result.outputs == {"savings_balance": BALANCE_10007}


def test_a_session_that_keeps_expiring_is_a_hard_failure_with_its_own_code(
    replayer: Replayer, inject: Injector, mock_settings: MockSettings
) -> None:
    inject("session_expired", times=None)
    result = replayer.run("10007")
    assert (result.status, result.outcome_code) == ("hard_failure", "SESSION_EXPIRED")
    assert not _only(result).succeeded
    _assert_no_secrets(result, mock_settings)


def test_a_notice_that_will_not_clear_exhausts_its_clicks_and_keeps_its_code(
    replayer: Replayer, inject: Injector
) -> None:
    inject("interstitial", sticky="1")
    result = replayer.run("10007")
    assert (result.status, result.outcome_code) == ("hard_failure", "INTERSTITIAL")
    recovery = _only(result)
    assert (recovery.attempts, recovery.succeeded) == (2, False)
    assert result.message
    assert "RECOVERY_EXHAUSTED" in result.message


def test_app_error_is_recognised_from_the_500_page(replayer: Replayer, inject: Injector) -> None:
    inject("app_error", on="detail")
    result = replayer.run("10007")
    assert result.outcome_code == "APP_ERROR"
    assert result.observed
    assert "status 500" in result.observed


def test_a_rejected_member_number_is_a_validation_error_on_that_field(
    replayer: Replayer,
) -> None:
    def no_pattern(data: dict[str, Any]) -> None:
        data["inputs"][0]["pattern"] = None

    result = replayer.run("1234", replayer.tweak(no_pattern))
    assert (result.status, result.outcome_code, result.exit_code) == (
        "business_outcome",
        "VALIDATION_ERROR",
        0,
    )
    assert [e.field for e in result.field_errors] == ["member_number"]


def test_access_denied_detector_also_covers_an_injected_denial(
    replayer: Replayer, inject: Injector
) -> None:
    inject("permission_denied", on="detail")
    assert replayer.run("10007").outcome_code == "PERMISSION_DENIED"


# ---- targeting, policy, confirmation --------------------------------------------------------


def test_layout_drift_succeeds_on_a_lower_rung_and_says_so(
    replayer: Replayer, inject: Injector
) -> None:
    inject("layout_drift")
    result = replayer.run("10007")
    assert (result.status, result.exit_code) == ("success", 0)
    assert result.warnings == [
        DriftWarning(
            type="drift",
            step_id="s06",
            target_ref="step",
            recorded_rung=0,
            resolved_rung=1,
            resolved_strategy="anchor_relative",
            identity_changed=True,
        )
    ]
    assert next(s for s in result.steps if s.step_id == "s06").resolved_strategy == (
        "anchor_relative"
    )


def test_a_policy_block_during_replay_is_a_hard_failure_never_a_skip(
    replayer: Replayer,
) -> None:
    def sign_out_after_sign_in(data: dict[str, Any]) -> None:
        data["steps"].insert(4, SIGN_OUT_STEP)

    result = replayer.run("10007", replayer.tweak(sign_out_after_sign_in))
    assert (result.status, result.outcome_code) == ("hard_failure", "POLICY_BLOCKED")
    assert result.step_reached == "s041"
    assert [(s.step_id, s.passed) for s in result.steps] == [
        ("s01", True),
        ("s02", True),
        ("s03", True),
        ("s04", True),
        ("s041", False),
    ]


def _irreversible_search(policy_ref: str) -> Callable[[dict[str, Any]], None]:
    def mutate(data: dict[str, Any]) -> None:
        data["capability"]["policy_ref"] = policy_ref
        data["steps"][5]["risk"] = "irreversible"

    return mutate


def test_an_irreversible_step_under_escalate_handling_never_acts(replayer: Replayer) -> None:
    capability = replayer.tweak(_irreversible_search("coreledger-readonly"))
    result = replayer.run("10007", capability, confirm_irreversible=True)
    assert (result.status, result.outcome_code) == ("hard_failure", "CONFIRMATION_REQUIRED")
    assert result.message
    assert "IRREVERSIBLE_NEEDS_HUMAN" in result.message
    assert result.step_reached == "s06"
    s06_acts = [e for e in _events(result) if e["event"] == "act" and e["step_id"] == "s06"]
    assert [(e["ok"], e["code"]) for e in s06_acts] == [(False, "CONFIRMATION_REQUIRED")]
    assert not any(e["event"] == "wait_passed" and e["step_id"] == "s06" for e in _events(result))


def test_confirm_handling_needs_the_flag_and_then_proceeds(replayer: Replayer) -> None:
    capability = replayer.tweak(_irreversible_search("coreledger-subaccount"))
    refused = replayer.run("10007", capability)
    assert refused.outcome_code == "CONFIRMATION_REQUIRED"
    assert refused.message
    assert "--confirm-irreversible" in refused.message
    assert replayer.run("10007", capability, confirm_irreversible=True).status == "success"


# ---- stability and the command line ---------------------------------------------------------


def test_repeated_runs_report_passes_rungs_and_determinism(replayer: Replayer) -> None:
    request = ReplayRequest(
        capability=replayer.capability,
        inputs={"member_number": "10007"},
        policy_file=replayer.policy_file,
        evidence_root=replayer.evidence_root,
        allow_draft=True,
    )
    report, path = run_stability(
        request, repeat=3, environ=replayer.environ, open_surface=replayer.open_surface
    )
    assert (report.passes, report.determinism) == (3, "deterministic")
    assert report.rung_distribution["s06"] == {"text_exact": 3}
    stored = json.loads(path.read_text())
    assert stored["passes"] == 3
    assert len({i["outputs_digest"] for i in stored["iterations"]}) == 1
    assert "4210.55" not in path.read_text()


def test_the_cli_exits_zero_for_not_found_and_two_for_a_hard_failure(
    replayer: Replayer, tmp_path: Path
) -> None:
    workdir = tmp_path / "cli"
    (workdir / "policy").mkdir(parents=True)
    shutil.copy(replayer.policy_file, workdir / "policy" / "allowlist.yaml")
    artifact = workdir / "read_balance.capability.json"
    artifact.write_text(dump_json(replayer.capability))
    env = {k: v for k, v in os.environ.items() if not k.startswith("CORELEDGER_")}
    env.update(replayer.environ)

    def cli(member: str) -> tuple[int, dict[str, Any]]:
        argv = [sys.executable, "-m", "cua.cli", "replay", str(artifact), "--allow-draft"]
        argv += ["-i", f"member_number={member}", "--evidence-root", str(workdir / "evidence")]
        done = subprocess.run(  # noqa: S603
            argv, cwd=workdir, env=env, capture_output=True, text=True, check=False
        )
        return done.returncode, json.loads(done.stdout)

    code, body = cli("99999")
    assert (code, body["status"], body["outcome_code"]) == (
        0,
        "business_outcome",
        "MEMBER_NOT_FOUND",
    )
    code, body = cli("10013")
    assert (code, body["status"], body["outcome_code"]) == (2, "hard_failure", "PERMISSION_DENIED")


def test_an_output_that_does_not_parse_fails_without_leaking_the_balance(
    replayer: Replayer, mock_settings: MockSettings
) -> None:
    def read_the_name_cell(data: dict[str, Any]) -> None:
        data["outputs"][0]["extract"]["target"]["ladder"] = [
            {
                "strategy": "anchor_relative",
                "anchor_text": "Name",
                "direction": "right",
                "same_row": True,
                "target_kind": "text",
                "nth": 1,
                "confidence": 0.8,
            }
        ]

    result = replayer.run("10007", replayer.tweak(read_the_name_cell))
    assert (result.status, result.outcome_code) == ("hard_failure", "OUTPUT_EXTRACTION_FAILED")
    snapshot = (Path(result.evidence_dir) / "a11y_06.json").read_text()
    assert "4,210.55" not in snapshot, "amounts are masked once a sensitive output may be on screen"
    assert "4210.55" not in snapshot
    assert _manifest(result)["a11y_06.json"] is True
    assert {"step_06.png", "step_06_failure.png", "trace.zip"} <= {p.name for p in _files(result)}
    _assert_no_secrets(result, mock_settings)
