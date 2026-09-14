"""Replay without a browser: the poll loop on a fake clock, pre-run refusals, and trace scrubbing.

A scripted surface decides what the frame shows at each instant, so timing assertions are exact.
"""

from __future__ import annotations

import base64
import json
import re
import zipfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import pytest

from cua.artifact.catalog import load_capability
from cua.artifact.schema import Capability, Rung
from cua.evidence.trace import scrub_trace
from cua.evidence.writer import EvidenceWriter
from cua.locate.backend import ElementFacts
from cua.policy.enforce import GatedSurface
from cua.policy.gate import PolicyGate
from cua.policy.models import load_policy
from cua.policy.redact import REDACTED, Redactor
from cua.replay.engine import ReplayEngine, RunStopped, no_operator
from cua.replay.result import AutomaticRecovery, ReplayResult
from cua.replay.run import ReplayRequest, ReplaySurface, run_replay
from cua.session.intervention import ReasonCode
from cua.surface.base import A11ySnapshot, Action, ActResult, Element, SurfaceEvent

from support import ROOT, find_leaks

POLICY_FILE = ROOT / "policy" / "allowlist.yaml"
BALANCE_ARTIFACT = (
    ROOT / "artifacts" / "coreledger.member.read_savings_balance@1.1.0.capability.json"
)
URL = "http://127.0.0.1:5050/members/10007"
NOW = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)
ENVIRON = {
    "CORELEDGER_OPERATOR_USER": "teller-0417",
    "CORELEDGER_OPERATOR_PASSWORD": "unit-test-password-123",
}


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def now(self) -> float:
        return self.t

    def sleep(self, ms: int) -> None:
        self.t += ms / 1000


class ScriptedSurface:
    """The main frame reads "Member profile" from ready_at seconds on, "Loading" before."""

    def __init__(self, clock: FakeClock, ready_at: float | None) -> None:
        self.clock = clock
        self.ready_at = ready_at
        self.text_reads = 0
        self.acts: list[str] = []

    def frame_text(self, frame_path: Sequence[str]) -> str:
        self.text_reads += 1
        ready = self.ready_at is not None and self.clock.t >= self.ready_at
        return "Member profile" if ready else "Loading"

    def frame_url(self, frame_path: Sequence[str]) -> str:
        return URL

    def frame_status(self, frame_path: Sequence[str]) -> int | None:
        return 200

    def act(self, action: Action, *, dialog_guard: Any = None) -> ActResult:
        self.acts.append(action.kind)
        return ActResult(ok=True, action=action.kind)

    def install_request_guard(self, guard: Any) -> None:
        pass

    def drain_events(self) -> list[SurfaceEvent]:
        return []

    def settle(self, quiet_ms: int, timeout_ms: int) -> bool:
        return True

    def wait(self, ms: int) -> None:
        self.clock.sleep(ms)

    def start_trace(self) -> None:
        pass

    def stop_trace(self, path: Path | None) -> bool:
        return False

    def snapshot(self) -> A11ySnapshot:
        return A11ySnapshot(nodes=[], frame_urls={"": URL})

    def screenshot(self) -> bytes:
        return b"\x89PNG"

    def candidates(self, rung: Rung, frame_path: Sequence[str]) -> list[Element]:
        return []

    def same_element(self, a: Element, b: Element) -> bool:
        return False

    def describe(self, element: Element) -> ElementFacts:
        raise AssertionError("nothing to describe")


def _capability(detectors: list[dict[str, Any]], timeout_ms: int = 3000) -> Capability:
    return Capability.model_validate(
        {
            "schema_version": "1.0",
            "capability": {
                "id": "coreledger.member.poll_probe",
                "version": "1.0.0",
                "status": "approved",
                "name": "Poll probe",
                "description": "Open a profile and wait for it.",
                "created_at": "2026-09-14T09:00:00Z",
                "policy_ref": "coreledger-readonly",
                "approved_by": "tests",
                "approved_at": "2026-09-14T09:00:00Z",
            },
            "surface": {
                "kind": "web",
                "entry_url": "http://127.0.0.1:5050/",
                "app_family": "coreledger",
                "app_version_hint": "4.2.1",
            },
            "inputs": [],
            "outputs": [],
            "steps": [
                {
                    "id": "s01",
                    "action": "navigate",
                    "description": "Open the profile.",
                    "url": URL,
                    "risk": "safe",
                    "wait_for": {
                        "kind": "checkpoint",
                        "checkpoint": "cp_profile",
                        "timeout_ms": timeout_ms,
                    },
                }
            ],
            "checkpoints": {
                "cp_profile": {
                    "description": "The profile is showing.",
                    "condition": {"kind": "text_present", "text": "Member profile"},
                }
            },
            "outcome_detectors": detectors,
            "success": {"checkpoint": "cp_profile", "requires_outputs": []},
            "provenance": {"recorded_by": "human"},
        }
    )


SLOW = {
    "id": "od_slow",
    "description": "The profile is slow.",
    "when": {"kind": "checkpoint_timeout", "checkpoint": "cp_profile"},
    "scope": ["s01"],
    "outcome": {
        "type": "recoverable",
        "code": "SLOW_LOAD",
        "recovery": {"kind": "wait_retry", "max_total_ms": 5000, "poll_ms": 500},
        "on_exhausted": "hard_failure",
    },
}
DOWN = {
    "id": "od_down",
    "description": "The app says it is down.",
    "when": {"kind": "text_present", "text": "Loading"},
    "outcome": {"type": "hard_failure", "code": "APP_DOWN", "escalate": True},
}


def _run(
    tmp_path: Path, capability: Capability, ready_at: float | None, **engine: Any
) -> tuple[ReplayResult, ScriptedSurface, list[dict[str, Any]]]:
    clock = FakeClock()
    surface = ScriptedSurface(clock, ready_at)
    gate = PolicyGate(load_policy(POLICY_FILE, "coreledger-readonly"))
    evidence = EvidenceWriter(tmp_path, "replay_probe", Redactor())
    result = ReplayEngine(
        capability,
        surface,
        GatedSurface(surface, surface, gate),
        inputs={},
        masked_inputs={},
        secrets={},
        redactor=Redactor(),
        evidence=evidence,
        run_id="replay_probe",
        started_at=NOW,
        clock=clock,
        **engine,
    ).run()
    lines = (evidence.dir / "run.jsonl").read_text().splitlines()
    return result, surface, [json.loads(line) for line in lines]


def _matched(events: list[dict[str, Any]]) -> list[str]:
    return [e["detector"] for e in events if e["event"] == "detector_matched"]


def test_a_checkpoint_that_never_holds_times_out_at_its_deadline_polling_each_tick(
    tmp_path: Path,
) -> None:
    result, surface, _ = _run(tmp_path, _capability([]), ready_at=None)
    assert (result.status, result.outcome_code) == ("hard_failure", "CHECKPOINT_TIMEOUT")
    assert result.duration_ms == 3000, "no fixed sleeps: the deadline is the step's own timeout"
    assert 12 <= surface.text_reads <= 14, "about one look every 250 ms"
    assert result.step_reached == "s01"
    assert result.expected
    assert "cp_profile" in result.expected
    assert result.observed
    assert "missing text 'Member profile'" in result.observed


def test_a_slow_checkpoint_fires_its_timeout_detector_once_then_recovers(tmp_path: Path) -> None:
    """The timeout trigger is evaluated at the deadline only, so the wait_retry window it opens
    cannot re-trigger itself on every tick."""
    result, _, events = _run(tmp_path, _capability([SLOW]), ready_at=5.0)
    assert result.status == "recovered_then_success"
    assert result.duration_ms == 5000
    assert _matched(events) == ["od_slow"]
    assert result.recoveries == [
        AutomaticRecovery(
            type="automatic",
            step_id="s01",
            detector_id="od_slow",
            code="SLOW_LOAD",
            recovery_kind="wait_retry",
            attempts=1,
            succeeded=True,
            duration_ms=2000,
        )
    ]


def test_an_exhausted_wait_retry_keeps_the_detector_code(tmp_path: Path) -> None:
    result, _, events = _run(tmp_path, _capability([SLOW]), ready_at=None)
    assert (result.status, result.outcome_code) == ("hard_failure", "SLOW_LOAD")
    assert result.duration_ms == 8000, "3 s wait plus the 5 s retry budget"
    assert _matched(events) == ["od_slow"]
    assert [(r.detector_id, r.succeeded) for r in result.recoveries] == [("od_slow", False)]


def test_escalations_pass_through_one_hook_and_stop_as_hard_failures_without_an_operator(
    tmp_path: Path,
) -> None:
    calls: list[ReasonCode] = []

    def hook(stop: RunStopped, reason: ReasonCode) -> RunStopped:
        calls.append(reason)
        return no_operator(stop, reason)

    result, _, _ = _run(tmp_path, _capability([DOWN]), ready_at=None, escalation=hook)
    assert calls == ["HARD_FAILURE_ESCALATE"]
    assert (result.status, result.outcome_code, result.exit_code) == ("hard_failure", "APP_DOWN", 2)
    assert result.message
    assert "needs a human" in result.message


def _refusal_request(tmp_path: Path, **changes: Any) -> ReplayRequest:
    fields: dict[str, Any] = {
        "capability": load_capability(BALANCE_ARTIFACT),
        "inputs": {"member_number": "10007"},
        "policy_file": POLICY_FILE,
        "evidence_root": tmp_path,
        "allow_draft": True,
    }
    fields.update(changes)
    return ReplayRequest(**fields)


def _elsewhere_policy(tmp_path: Path) -> Path:
    path = tmp_path / "elsewhere.yaml"
    text = POLICY_FILE.read_text().replace("http://127.0.0.1:5050", "http://127.0.0.1:6060")
    path.write_text(text)
    return path


def _never_opened() -> ReplaySurface:
    raise AssertionError("a pre-run refusal must not open a browser")


@pytest.mark.parametrize(
    ("changes", "environ", "code"),
    [
        ({"allow_draft": False}, ENVIRON, "DRAFT_NOT_APPROVED"),
        ({"tenant": "acme-cu"}, ENVIRON, "TENANT_UNKNOWN"),
        ({"policy_file": "elsewhere"}, ENVIRON, "POLICY_MISMATCH"),
        ({"inputs": {"member_number": "12ab"}}, ENVIRON, "INPUT_INVALID"),
        ({}, {}, "SECRET_MISSING"),
    ],
)
def test_every_pre_run_code_refuses_before_step_one_and_leaves_evidence(
    tmp_path: Path, changes: dict[str, Any], environ: dict[str, str], code: str
) -> None:
    if changes.get("policy_file") == "elsewhere":
        changes = {"policy_file": _elsewhere_policy(tmp_path)}
    request = _refusal_request(tmp_path / "evidence", **changes)
    result = run_replay(request, environ=environ, open_surface=_never_opened)
    assert (result.status, result.outcome_code, result.exit_code) == ("hard_failure", code, 2)
    assert result.step_reached is None
    assert result.expected
    assert result.observed
    evidence = Path(result.evidence_dir)
    assert json.loads((evidence / "result.json").read_text())["outcome_code"] == code
    assert (evidence / "manifest.json").exists()


def test_an_invalid_input_is_reported_without_echoing_other_secrets(tmp_path: Path) -> None:
    request = _refusal_request(tmp_path, inputs={"member_number": "123456789"})
    result = run_replay(request, environ=ENVIRON, open_surface=_never_opened)
    assert result.outcome_code == "INPUT_INVALID"
    assert "123456789" not in (result.message or "")
    assert result.inputs == {"member_number": "*****6789"}


def test_trace_archives_lose_every_encoding_of_a_secret_and_their_cookies(tmp_path: Path) -> None:
    password, operator = "Tr@ce pass/word+1&x=y", "teller-0417"
    body = f"operator={operator}&password={quote_plus(password)}"
    network = {
        "type": "resource-snapshot",
        "snapshot": {
            "request": {
                "headers": [{"name": "Cookie", "value": "CLSESSID=abc123"}],
                "postData": {"text": body},
            },
            "response": {
                "headers": [{"name": "Set-Cookie", "value": "CLSESSID=abc123; HttpOnly"}],
                "cookies": [{"name": "CLSESSID", "value": "abc123"}],
            },
        },
    }
    raw = tmp_path / "raw.zip"
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr("trace.network", json.dumps(network) + "\n")
        archive.writestr("trace.trace", json.dumps({"value": password}) + "\n")
        archive.writestr("resources/body.dat", body)
        archive.writestr("resources/b64.dat", base64.b64encode(password.encode()))
    out = tmp_path / "trace.zip"
    scrub_trace(raw, out, Redactor([password], identities=[operator]))

    assert not find_leaks([out], {"password": password, "operator": operator})
    with zipfile.ZipFile(out) as archive:
        logged = archive.read("trace.network").decode()
    assert "abc123" not in logged
    assert logged.count(REDACTED) >= 3
    assert re.search(r'"name":\s*"CLSESSID"', logged), "cookie names stay, values go"
