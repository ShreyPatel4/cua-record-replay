"""Replay without a browser: the poll loop on a fake clock, pre-run refusals, and trace scrubbing.

A scripted surface decides what the frame shows at each instant, so timing assertions are exact.
"""

from __future__ import annotations

import base64
import json
import re
import zipfile
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote_plus

import pytest

from cua.artifact.catalog import load_capability
from cua.artifact.schema import Capability, Rung
from cua.evidence.trace import scrub_trace
from cua.evidence.writer import EvidenceWriter
from cua.locate.backend import ElementFacts
from cua.policy.enforce import GatedSurface
from cua.policy.fit import capability_policy_errors
from cua.policy.gate import PolicyGate
from cua.policy.models import load_policy
from cua.policy.redact import REDACTED, Redactor
from cua.replay.engine import ReplayEngine, RunStopped, no_operator
from cua.replay.result import AutomaticRecovery, HumanIntervention, ReplayResult
from cua.replay.run import Control, ReplayRequest, ReplaySurface, run_replay
from cua.session.intervention import ReasonCode
from cua.session.operator import OperatorChannel
from cua.session.state import SessionState, StateStore, file_control
from cua.surface.base import A11ySnapshot, Action, ActResult, Element, HumanEvent, SurfaceEvent

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
        self.fronted = False
        self.human_sink: Callable[[HumanEvent], None] | None = None

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

    def bring_to_front(self) -> None:
        self.fronted = True

    def start_human_capture(self, sink: Callable[[HumanEvent], None]) -> None:
        self.human_sink = sink

    def stop_human_capture(self) -> None:
        self.human_sink = None

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
    draft = load_capability(BALANCE_ARTIFACT).model_dump(mode="json")
    draft["capability"].update(status="draft", approved_by=None, approved_at=None)
    fields: dict[str, Any] = {
        "capability": Capability.model_validate(draft),
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


def _never_opened(control: Control) -> ReplaySurface:
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


def test_an_invalid_input_is_reported_with_the_member_number_masked(tmp_path: Path) -> None:
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


# ---- targets, acts, events, and recoveries on a scripted surface ------------------------------

BUTTON = Element(handle="button", frame_path=("main",))
CP = {"kind": "checkpoint", "checkpoint": "cp_profile", "timeout_ms": 1000}
SETTLE = {"kind": "settle", "quiet_ms": 300, "timeout_ms": 1000}


def _facts(name: str, kind: str = "clickable") -> ElementFacts:
    return ElementFacts.model_validate(
        {
            "tag": "span",
            "role": "cell",
            "name": name,
            "own_text": name,
            "input_type": None,
            "kind": kind,
            "frame_path": ["main"],
            "labels": [],
            "anchors": [],
            "frame_box": None,
        }
    )


def _target(name: str = "Find", kind: str = "clickable") -> dict[str, Any]:
    return {
        "ladder": [{"strategy": "text_exact", "text": name, "confidence": 0.9}],
        "recorded_rung": 0,
        "frame_path": ["main"],
        "fingerprint": {"role": "cell", "name": name, "text": name, "kind": kind},
        "notes": "Scripted target.",
    }


def _navigate(step_id: str, wait: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {
        "id": step_id,
        "action": "navigate",
        "description": "Open the page.",
        "url": URL,
        "risk": "safe",
        "wait_for": wait,
        **extra,
    }


def _click(step_id: str, wait: dict[str, Any], name: str = "Find", **extra: Any) -> dict[str, Any]:
    return {
        "id": step_id,
        "action": "click",
        "description": f"Click {name}.",
        "target": _target(name),
        "risk": "safe",
        "wait_for": wait,
        **extra,
    }


def _flow(
    steps: list[dict[str, Any]],
    detectors: Sequence[dict[str, Any]] = (),
    *,
    policy_ref: str = "coreledger-readonly",
    outputs: Sequence[dict[str, Any]] = (),
    requires: Sequence[str] = (),
) -> Capability:
    data = _capability([]).model_dump(mode="json")
    data["steps"] = steps
    data["outcome_detectors"] = list(detectors)
    data["capability"]["policy_ref"] = policy_ref
    data["outputs"] = list(outputs)
    data["success"]["requires_outputs"] = list(requires)
    return Capability.model_validate(data)


class TargetSurface(ScriptedSurface):
    """One scripted clickable, scripted act results, queued events, and scripted text and URL."""

    def __init__(
        self,
        clock: FakeClock,
        *,
        text: Any = lambda s: "Member profile",
        url: Any = lambda s: URL,
        matches: int = 1,
        name: str = "Find",
        kind: str = "clickable",
        act_results: Sequence[ActResult] = (),
    ) -> None:
        super().__init__(clock, ready_at=0.0)
        self.text_fn, self.url_fn = text, url
        self.matches, self.name, self.kind = matches, name, kind
        self.act_results = list(act_results)
        self.queued: list[SurfaceEvent] = []
        self.on_candidates: list[SurfaceEvent] = []
        self.on_read: list[SurfaceEvent] = []
        self.read_text = "Gray"

    def frame_text(self, frame_path: Sequence[str]) -> str:
        self.text_reads += 1
        return str(self.text_fn(self))

    def frame_url(self, frame_path: Sequence[str]) -> str:
        return str(self.url_fn(self))

    def candidates(self, rung: Rung, frame_path: Sequence[str]) -> list[Element]:
        self.queued += self.on_candidates
        self.on_candidates = []
        return [BUTTON] * self.matches

    def describe(self, element: Element) -> ElementFacts:
        return _facts(self.name, self.kind)

    def act(self, action: Action, *, dialog_guard: Any = None) -> ActResult:
        self.acts.append(action.kind)
        if action.kind == "read_text":
            self.queued += self.on_read
            return ActResult(ok=True, action="read_text", text=self.read_text)
        if self.act_results:
            return self.act_results.pop(0)
        return ActResult(ok=True, action=action.kind)

    def drain_events(self) -> list[SurfaceEvent]:
        events, self.queued = self.queued, []
        return events

    def session_tokens(self) -> list[str]:
        return []

    def close(self) -> None:
        pass


def _run_on(
    tmp_path: Path,
    capability: Capability,
    surface: TargetSurface,
    *,
    policy_id: str = "coreledger-readonly",
    confirm: bool = False,
    **engine: Any,
) -> tuple[ReplayResult, list[dict[str, Any]]]:
    gate = PolicyGate(load_policy(POLICY_FILE, policy_id))
    evidence = EvidenceWriter(tmp_path, "replay_scripted", Redactor())
    result = ReplayEngine(
        capability,
        surface,
        GatedSurface(surface, surface, gate, confirm_irreversible=confirm),
        inputs={},
        masked_inputs={},
        secrets={},
        redactor=Redactor(),
        evidence=evidence,
        run_id="replay_scripted",
        started_at=NOW,
        clock=surface.clock,
        **engine,
    ).run()
    assert result.contract_problems(capability) == [] or result.outcome_code in {"APP_DOWN"}
    lines = (evidence.dir / "run.jsonl").read_text().splitlines()
    return result, [json.loads(line) for line in lines]


def _failed(message: str, **extra: Any) -> ActResult:
    return ActResult(ok=False, action="click", code="ACTION_FAILED", message=message, **extra)


def test_a_failed_act_is_retried_within_the_step_timeout(tmp_path: Path) -> None:
    surface = TargetSurface(FakeClock(), act_results=[_failed("element detached")])
    result, _ = _run_on(tmp_path, _flow([_click("s01", CP)]), surface)
    assert result.status == "success"
    assert surface.acts == ["click", "click"]
    assert result.steps[0].attempts == 2


def test_an_act_the_policy_rates_irreversible_is_never_retried(tmp_path: Path) -> None:
    """Declared safe, but the gate reads 'Submit' as irreversible: a timed-out click may already
    have reached the server, so it is attempted at most once."""
    surface = TargetSurface(FakeClock(), name="Submit", act_results=[_failed("timed out")])
    capability = _flow([_click("s01", CP, name="Submit")], policy_ref="coreledger-subaccount")
    result, _ = _run_on(
        tmp_path, capability, surface, policy_id="coreledger-subaccount", confirm=True
    )
    assert (result.status, result.outcome_code) == ("hard_failure", "ACTION_FAILED")
    assert surface.acts == ["click"]
    assert [(s.step_id, s.passed) for s in result.steps] == [("s01", False)]


def test_policy_fit_refuses_a_step_declared_below_the_gates_rating() -> None:
    capability = _flow([_click("s01", CP, name="Submit")], policy_ref="coreledger-subaccount")
    errors = capability_policy_errors(capability, load_policy(POLICY_FILE, "coreledger-subaccount"))
    assert any("declared safe" in e and "rates it irreversible" in e for e in errors)


@pytest.mark.parametrize(("matches", "code"), [(0, "TARGET_NOT_FOUND"), (2, "TARGET_AMBIGUOUS")])
def test_a_target_that_never_resolves_to_one_element_fails_with_why(
    tmp_path: Path, matches: int, code: str
) -> None:
    surface = TargetSurface(FakeClock(), matches=matches)
    result, _ = _run_on(tmp_path, _flow([_click("s01", CP)]), surface)
    assert (result.status, result.outcome_code) == ("hard_failure", code)
    assert result.duration_ms == 1000
    assert surface.acts == []
    assert result.observed
    assert f"text_exact={matches}" in result.observed


def test_a_renamed_target_on_an_irreversible_step_is_never_acted_on(tmp_path: Path) -> None:
    surface = TargetSurface(FakeClock(), name="Submit now")
    step = _click("s01", CP, name="Submit")
    step["risk"] = "irreversible"
    capability = _flow([step], policy_ref="coreledger-subaccount")
    result, _ = _run_on(
        tmp_path, capability, surface, policy_id="coreledger-subaccount", confirm=True
    )
    assert (result.status, result.outcome_code) == ("hard_failure", "TARGET_CHANGED")
    assert surface.acts == []


def test_a_renamed_target_on_a_safe_step_acts_and_warns(tmp_path: Path) -> None:
    surface = TargetSurface(FakeClock(), name="Search")
    result, _ = _run_on(tmp_path, _flow([_click("s01", CP)]), surface)
    assert result.status == "success"
    assert [(w.type, getattr(w, "identity_changed", None)) for w in result.warnings] == [
        ("drift", True)
    ]


def test_an_unexpected_dialog_during_an_act_stops_the_run(tmp_path: Path) -> None:
    dialog = SurfaceEvent("unexpected_dialog", "confirm: Are you sure?")
    surface = TargetSurface(
        FakeClock(), act_results=[_failed("unexpected dialog", events=[dialog])]
    )
    result, _ = _run_on(tmp_path, _flow([_click("s01", CP)]), surface)
    assert result.outcome_code == "UNEXPECTED_DIALOG"
    assert surface.acts == ["click"]


@pytest.mark.parametrize(
    ("event", "code"),
    [
        (SurfaceEvent("navigation_blocked", "paths_deny /logout", URL), "POLICY_BLOCKED"),
        (SurfaceEvent("unexpected_dialog", "alert: Posting delayed"), "UNEXPECTED_DIALOG"),
    ],
)
def test_a_late_event_stops_the_run_on_the_next_tick(
    tmp_path: Path, event: SurfaceEvent, code: str
) -> None:
    surface = TargetSurface(FakeClock(), text=lambda s: "Loading")
    surface.queued = [event]
    result, _ = _run_on(tmp_path, _flow([_navigate("s01", CP)]), surface)
    assert (result.status, result.outcome_code, result.step_reached) == (
        "hard_failure",
        code,
        "s01",
    )


def test_on_fail_escalate_sends_an_engine_failure_to_the_hook(tmp_path: Path) -> None:
    calls: list[ReasonCode] = []

    def hook(stop: RunStopped, reason: ReasonCode) -> RunStopped:
        calls.append(reason)
        return no_operator(stop, reason)

    surface = TargetSurface(FakeClock(), text=lambda s: "Loading")
    capability = _flow([_navigate("s01", CP, on_fail="escalate")])
    result, _ = _run_on(tmp_path, capability, surface, escalation=hook)
    assert calls == ["HARD_FAILURE_ESCALATE"]
    assert result.outcome_code == "CHECKPOINT_TIMEOUT"


def test_a_url_change_wait_passes_when_the_frame_url_matches(tmp_path: Path) -> None:
    clock = FakeClock()
    surface = TargetSurface(
        clock, url=lambda s: URL if s.clock.t >= 1.0 else "http://127.0.0.1:5050/members/search"
    )
    wait = {
        "kind": "url_change",
        "pattern": "/members/10007$",
        "frame_path": [],
        "timeout_ms": 3000,
    }
    result, _ = _run_on(tmp_path, _flow([_navigate("s01", wait)]), surface)
    assert result.status == "success"
    assert result.duration_ms == 1000


def test_an_operator_channel_can_end_the_run_as_escalated(tmp_path: Path) -> None:
    intervention = tmp_path / "intervention.json"
    intervention.write_text("{}")

    def operator_channel(stop: RunStopped, reason: ReasonCode) -> RunStopped:
        return RunStopped(
            stop.code,
            f"{stop.message} Waiting for an operator.",
            expected=stop.expected,
            observed=stop.observed,
            status="escalated",
            intervention_path=str(intervention),
        )

    result, _, _ = _run(tmp_path, _capability([DOWN]), ready_at=None, escalation=operator_channel)
    assert (result.status, result.exit_code) == ("escalated", 3)
    assert result.intervention_path == str(intervention)


def test_two_wait_retry_detectors_matching_together_share_the_wait(tmp_path: Path) -> None:
    def loading(detector_id: str, code: str) -> dict[str, Any]:
        return {
            "id": detector_id,
            "description": "Still loading.",
            "when": {"kind": "text_present", "text": "Loading"},
            "scope": ["s01"],
            "outcome": {
                "type": "recoverable",
                "code": code,
                "recovery": {"kind": "wait_retry", "max_total_ms": 5000, "poll_ms": 250},
            },
        }

    capability = _flow(
        [_navigate("s01", CP)], [loading("od_a", "LOAD_A"), loading("od_b", "LOAD_B")]
    )
    surface = TargetSurface(
        FakeClock(), text=lambda s: "Member profile" if s.clock.t >= 4 else "Loading"
    )
    result, _ = _run_on(tmp_path, capability, surface)
    assert result.status == "recovered_then_success"
    assert sorted(r.detector_id for r in result.recoveries if isinstance(r, AutomaticRecovery)) == [
        "od_a",
        "od_b",
    ]


EXPIRED = {
    "id": "od_expired",
    "description": "The session expired.",
    "when": {"kind": "text_present", "text": "Expired"},
    "scope": ["s02"],
    "outcome": {
        "type": "recoverable",
        "code": "SESSION_EXPIRED",
        "recovery": {"kind": "run_steps", "step_ids": ["s01"], "max_attempts": 1},
    },
}


def test_a_re_run_that_never_verifies_reports_the_recovering_detector(tmp_path: Path) -> None:
    slow = {**SLOW, "id": "od_slow_s02", "scope": ["s02"]}
    slow["outcome"] = {**SLOW["outcome"], "recovery": {"kind": "wait_retry", "max_total_ms": 2000}}
    capability = _flow([_navigate("s01", SETTLE), _navigate("s02", CP)], [EXPIRED, slow])

    def text(s: TargetSurface) -> str:
        return "Expired" if s.acts.count("navigate") == 2 else "Loading"

    result, _ = _run_on(tmp_path, capability, TargetSurface(FakeClock(), text=text))
    assert (result.status, result.outcome_code) == ("hard_failure", "SESSION_EXPIRED")
    assert all(not r.succeeded for r in result.recoveries if isinstance(r, AutomaticRecovery))


def test_recovery_attempts_count_across_all_waits_of_a_step(tmp_path: Path) -> None:
    capability = _flow([_navigate("s01", SETTLE), _click("s02", CP)], [EXPIRED])

    def text(s: TargetSurface) -> str:
        if s.acts.count("click"):
            return "Expired"
        return "Expired" if s.acts.count("navigate") == 1 else "Loading"

    result, events = _run_on(tmp_path, capability, TargetSurface(FakeClock(), text=text))
    assert (result.status, result.outcome_code) == ("hard_failure", "SESSION_EXPIRED")
    assert [e["detector"] for e in events if e["event"] == "recovery_started"] == ["od_expired"]


OPTIONAL = {
    "name": "nickname",
    "type": "string",
    "description": "Optional value.",
    "extract": {"target": _target("Nickname", kind="clickable"), "parse": "text"},
    "sensitive": False,
}


def test_an_optional_output_that_is_not_there_is_skipped(tmp_path: Path) -> None:
    surface = TargetSurface(FakeClock(), matches=0)
    result, _ = _run_on(tmp_path, _flow([_navigate("s01", CP)], outputs=[OPTIONAL]), surface)
    assert (result.status, result.outputs) == ("success", {})


def test_a_policy_block_while_reading_an_optional_output_is_never_swallowed(
    tmp_path: Path,
) -> None:
    surface = TargetSurface(FakeClock(), matches=0)
    surface.on_candidates = [SurfaceEvent("navigation_blocked", "paths_deny /logout", URL)]
    result, _ = _run_on(tmp_path, _flow([_navigate("s01", CP)], outputs=[OPTIONAL]), surface)
    assert (result.status, result.outcome_code) == ("hard_failure", "POLICY_BLOCKED")


def test_an_event_that_lands_while_the_last_output_is_read_still_counts(tmp_path: Path) -> None:
    surface = TargetSurface(FakeClock(), name="Nickname")
    surface.on_read = [SurfaceEvent("navigation_blocked", "paths_deny /logout", URL)]
    capability = _flow([_navigate("s01", CP)], outputs=[OPTIONAL], requires=["nickname"])
    result, _ = _run_on(tmp_path, capability, surface)
    assert (result.status, result.outcome_code) == ("hard_failure", "POLICY_BLOCKED")
    assert result.outputs == {}


# ---- the runner: refusals, crashes, and traces ------------------------------------------------


def _tweaked_balance(mutate: Any) -> Capability:
    data = load_capability(BALANCE_ARTIFACT).model_dump(mode="json")
    mutate(data)
    return Capability.model_validate(data)


def test_a_deprecated_capability_has_its_own_refusal(tmp_path: Path) -> None:
    capability = _tweaked_balance(lambda d: d["capability"].update(status="deprecated"))
    result = run_replay(
        _refusal_request(tmp_path, capability=capability),
        environ=ENVIRON,
        open_surface=_never_opened,
    )
    assert result.outcome_code == "CAPABILITY_DEPRECATED"


def test_a_secret_too_short_to_redact_is_refused_not_a_crash(tmp_path: Path) -> None:
    environ = {**ENVIRON, "CORELEDGER_OPERATOR_PASSWORD": "abc"}
    result = run_replay(_refusal_request(tmp_path), environ=environ, open_surface=_never_opened)
    assert result.outcome_code == "SECRET_MISSING"
    assert result.observed
    assert "shorter than 4" in result.observed


def test_an_optional_input_the_flow_uses_must_be_supplied(tmp_path: Path) -> None:
    capability = _tweaked_balance(lambda d: d["inputs"][0].update(required=False))
    result = run_replay(
        _refusal_request(tmp_path, capability=capability, inputs={}),
        environ=ENVIRON,
        open_surface=_never_opened,
    )
    assert result.outcome_code == "INPUT_INVALID"
    assert result.message
    assert "member_number" in result.message


def test_undeclared_inputs_are_masked_in_the_result(tmp_path: Path) -> None:
    request = _refusal_request(tmp_path, inputs={"member_number": "10007", "pin": "hunter2-xyz"})
    result = run_replay(request, environ=ENVIRON, open_surface=_never_opened)
    assert result.outcome_code == "INPUT_INVALID"
    assert result.inputs == {"member_number": "*0007", "pin": REDACTED}
    assert "hunter2-xyz" not in (Path(result.evidence_dir) / "result.json").read_text()


class CrashingSurface(TargetSurface):
    def start_trace(self) -> None:
        raise RuntimeError("trace backend gone")


class BadTraceSurface(TargetSurface):
    def stop_trace(self, path: Path | None) -> bool:
        if path is not None:
            path.write_bytes(b"not a zip")
            return True
        return False


def _poll_request(tmp_path: Path) -> ReplayRequest:
    return ReplayRequest(
        capability=_capability([]), inputs={}, policy_file=POLICY_FILE, evidence_root=tmp_path
    )


def test_a_crash_still_leaves_a_result_file(tmp_path: Path) -> None:
    surface = CrashingSurface(FakeClock())
    with pytest.raises(RuntimeError, match="trace backend gone"):
        run_replay(_poll_request(tmp_path), environ={}, open_surface=lambda _control: surface)
    (run_dir,) = tmp_path.iterdir()
    assert json.loads((run_dir / "result.json").read_text())["status"] == "crashed"


def test_a_trace_that_cannot_be_scrubbed_is_dropped_and_the_failure_stands(tmp_path: Path) -> None:
    surface = BadTraceSurface(FakeClock(), text=lambda s: "Loading")
    result = run_replay(
        _poll_request(tmp_path),
        environ={},
        open_surface=lambda _control: surface,
        clock=lambda s: surface.clock,
    )
    assert (result.status, result.outcome_code, result.exit_code) == (
        "hard_failure",
        "CHECKPOINT_TIMEOUT",
        2,
    )
    names = {p.name for p in Path(result.evidence_dir).iterdir()}
    assert "trace.zip" not in names
    assert "result.json" in names
    assert "trace_dropped" in (Path(result.evidence_dir) / "run.jsonl").read_text()


def test_cookies_written_as_log_text_are_learned_and_scrubbed_everywhere(tmp_path: Path) -> None:
    raw = tmp_path / "raw.zip"
    with zipfile.ZipFile(raw, "w") as archive:
        archive.writestr(
            "trace.trace",
            json.dumps({"type": "log", "message": "  set-cookie: CLSESSID=zzz999abcdef; HttpOnly"})
            + "\n"
            + json.dumps({"type": "log", "message": "  cookie: CLSESSID=yyy888abcdef"})
            + "\n",
        )
        archive.writestr("resources/page.html", "<a href='?s=zzz999abcdef&t=yyy888abcdef'>x</a>")
        archive.writestr("resources/live.dat", "token=live777abcdef")
    out = tmp_path / "trace.zip"
    scrub_trace(raw, out, Redactor(), session_tokens=["live777abcdef"])
    leaks = find_leaks([out], {"a": "zzz999abcdef", "b": "yyy888abcdef", "live": "live777abcdef"})
    assert not leaks
    with zipfile.ZipFile(out) as archive:
        assert "set-cookie: [REDACTED]" in archive.read("trace.trace").decode()


def test_the_approved_profile_wait_keeps_the_slow_recovery_reachable() -> None:
    """The slow evidence run depends on these three numbers: s06 must give up before the injected
    delay, and the retry budget must outlast it. Retuning either side silently breaks the demo."""
    from cua.artifact.schema import Recoverable, WaitRetryRecovery
    from mock_app.injections import DEFAULT_SLOW_MS

    capability = load_capability(BALANCE_ARTIFACT)
    wait = next(s for s in capability.steps if s.id == "s06").wait_for.timeout_ms
    slow = next(d for d in capability.outcome_detectors if d.id == "od_slow_member_load").outcome
    assert isinstance(slow, Recoverable)
    assert isinstance(slow.recovery, WaitRetryRecovery)
    assert wait < DEFAULT_SLOW_MS < wait + slow.recovery.max_total_ms


class FakeOperator:
    """An operator, driven from inside the paused run's own poll so no test needs a thread.

    The channel sleeps between looks at the state file; this is that sleep, and it takes the
    session, optionally fixes the app the way a human would, and hands back or aborts.
    """

    def __init__(
        self,
        store: StateStore,
        clock: FakeClock,
        *,
        fix: Callable[[], None] | None = None,
        aborts: bool = False,
        answers: bool = True,
        who: str = "teller-9",
        note: str = "cleared the notice by hand",
    ) -> None:
        self.store = store
        self.clock = clock
        self.fix = fix
        self.aborts = aborts
        self.answers = answers
        self.who = who
        self.note = note
        self.takes = 0
        self.polls = 0

    def __call__(self, ms: int) -> None:
        self.clock.sleep(ms)
        if not self.answers or not self.store.path.exists():
            return
        state = self.store.read()
        at = NOW + timedelta(seconds=self.clock.t)
        if state.phase == "paused_for_human":
            self.takes += 1
            self.store.transition("take_control", "operator", operator_id=self.who, now=at)
        elif state.phase == "human_active":
            if self.aborts:
                self.store.transition(
                    "abort", "operator", operator_id=self.who, note="not fixable", now=at
                )
                return
            if self.fix is not None:
                self.fix()
            self.store.transition(
                "hand_back", "operator", operator_id=self.who, note=self.note, now=at
            )


def _with_operator(
    tmp_path: Path,
    capability: Capability,
    ready_at: float | None,
    *,
    operator: Callable[[StateStore, FakeClock, ScriptedSurface], FakeOperator],
    wait_s: float = 30.0,
) -> tuple[ReplayResult, ScriptedSurface, list[dict[str, Any]], StateStore]:
    clock = FakeClock()
    surface = ScriptedSurface(clock, ready_at)
    evidence = EvidenceWriter(tmp_path, "replay_probe", Redactor())
    store = StateStore(evidence.dir / "session_state.json")
    driver = operator(store, clock, surface)
    channel = OperatorChannel(
        run_id="replay_probe",
        capability_id=capability.capability.id,
        evidence=evidence,
        surface=cast(Any, surface),
        redactor=Redactor(),
        resume_command=lambda run: f"uv run cua ops take-control {run}",
        wait_s=wait_s,
        poll_ms=250,
        sleep=driver,
        now=lambda: NOW + timedelta(seconds=clock.t),
        announce=lambda _text: None,
    )
    gate = PolicyGate(load_policy(POLICY_FILE, "coreledger-readonly"))
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
        escalation=channel,
    ).run()
    channel.close(result.status)
    lines = (evidence.dir / "run.jsonl").read_text().splitlines()
    return result, surface, [json.loads(line) for line in lines], store


def test_a_hand_back_re_verifies_the_step_instead_of_acting_again(tmp_path: Path) -> None:
    """The human fixes the screen and gives the session back. Replay checks the checkpoint rather
    than repeating the step, because the human may already have done it by hand."""

    def script(store: StateStore, clock: FakeClock, surface: ScriptedSurface) -> FakeOperator:
        def fix() -> None:
            surface.ready_at = clock.t

        return FakeOperator(store, clock, fix=fix)

    result, surface, events, store = _with_operator(
        tmp_path, _capability([DOWN]), ready_at=None, operator=script
    )
    assert result.status == "recovered_then_success"
    assert surface.acts == ["navigate"], "the step ran once; the hand-back did not repeat it"
    assert [r.model_dump()["type"] for r in result.recoveries] == ["human"]
    human = result.recoveries[0]
    assert isinstance(human, HumanIntervention)
    assert (human.outcome, human.reverified, human.operator_id) == ("handed_back", True, "teller-9")
    assert human.operator_note == "cleared the notice by hand"
    assert [e["event"] for e in events if e["event"] == "handback_received"]
    assert store.read().phase == "finished"
    assert (Path(result.evidence_dir) / "intervention.json").is_file()


def test_an_operator_who_aborts_ends_the_run_as_escalated_with_the_request(tmp_path: Path) -> None:
    result, _, _, store = _with_operator(
        tmp_path,
        _capability([DOWN]),
        ready_at=None,
        operator=lambda store, clock, _s: FakeOperator(store, clock, aborts=True),
    )
    assert (result.status, result.outcome_code, result.exit_code) == ("escalated", "APP_DOWN", 3)
    assert result.intervention_path
    assert result.intervention_path.endswith("intervention.json")
    human = result.recoveries[0]
    assert isinstance(human, HumanIntervention)
    assert (human.outcome, human.operator_id) == ("aborted", "teller-9")
    assert store.read().phase == "finished"


def test_a_pause_nobody_answers_expires_and_the_run_escalates(tmp_path: Path) -> None:
    result, _, events, store = _with_operator(
        tmp_path,
        _capability([DOWN]),
        ready_at=None,
        operator=lambda store, clock, _s: FakeOperator(store, clock, answers=False),
        wait_s=5.0,
    )
    assert (result.status, result.exit_code) == ("escalated", 3)
    assert [e["event"] for e in events if e["event"] == "intervention_expired"]
    human = result.recoveries[0]
    assert isinstance(human, HumanIntervention)
    assert (human.outcome, human.taken_at) == ("expired", None)
    assert store.read().phase == "finished"


def test_a_hand_back_that_does_not_fix_the_screen_pauses_again_then_gives_up(
    tmp_path: Path,
) -> None:
    """Re-verification failing is the brief's resuming to paused_for_human arrow. It carries a new
    request, and a run that keeps coming back stops asking instead of ping-ponging forever."""
    result, _, events, _ = _with_operator(
        tmp_path,
        _capability([DOWN]),
        ready_at=None,
        operator=lambda store, clock, _s: FakeOperator(store, clock, fix=None),
    )
    assert (result.status, result.exit_code) == ("escalated", 3)
    assert result.message
    assert "paused for a human" in result.message
    raised = [e for e in events if e["event"] == "intervention_raised"]
    assert [e["intervention_id"] for e in raised] == ["iv_01", "iv_02", "iv_03"]
    assert [e["reason_code"] for e in raised[1:]] == ["POST_HANDOFF_CHECKPOINT_FAILED"] * 2
    assert [e["event"] for e in events if e["event"] == "escalation_capped"]
    outcomes = [r.model_dump()["outcome"] for r in result.recoveries]
    assert outcomes == ["handed_back", "handed_back", "handed_back"]
    lines = (Path(result.evidence_dir) / "interventions.jsonl").read_text().splitlines()
    assert len(lines) == 3, "every request is kept, not just the last"


def test_what_a_human_types_is_redacted_before_it_reaches_the_evidence(tmp_path: Path) -> None:
    """The capture sink sees raw keystroke values. A password typed by hand must not survive."""
    evidence = EvidenceWriter(tmp_path, "replay_capture", Redactor())
    channel = OperatorChannel(
        run_id="replay_capture",
        capability_id="coreledger.member.poll_probe",
        evidence=evidence,
        surface=cast(Any, ScriptedSurface(FakeClock(), None)),
        redactor=Redactor(),
        resume_command=lambda run: run,
    )
    channel._begin_capture()
    channel._write_action(
        HumanEvent(
            kind="input",
            frame_url=URL,
            role="password",
            name="Password",
            value="hunter2-operator-secret",
            credential_field=True,
        )
    )
    channel._write_action(
        HumanEvent(kind="click", frame_url=URL, role="cell", name="Find", text="Find")
    )
    channel._end_capture()
    written = (evidence.dir / "human_actions.jsonl").read_text()
    assert "hunter2-operator-secret" not in written
    assert "[REDACTED]" in written
    assert '"event": "click"' in written
    assert not find_leaks(evidence.dir.rglob("*"), {"typed": "hunter2-operator-secret"})


def test_a_hand_back_on_a_step_with_no_checkpoint_still_finishes_the_run(tmp_path: Path) -> None:
    """A settle step has no checkpoint to re-verify, so the hand-back is recorded as not
    re-verified. The run still carried on, which is what recovered_then_success means."""
    data = _capability([DOWN]).model_dump(mode="json")
    data["steps"][0]["wait_for"] = {"kind": "settle", "quiet_ms": 300, "timeout_ms": 3000}
    data["success"]["checkpoint"] = "cp_profile"
    capability = Capability.model_validate(data)

    def script(store: StateStore, clock: FakeClock, surface: ScriptedSurface) -> FakeOperator:
        def fix() -> None:
            surface.ready_at = clock.t

        return FakeOperator(store, clock, fix=fix)

    result, _, _, _ = _with_operator(tmp_path, capability, ready_at=None, operator=script)
    assert (result.status, result.exit_code) == ("recovered_then_success", 0)
    human = result.recoveries[0]
    assert isinstance(human, HumanIntervention)
    assert (human.outcome, human.reverified) == ("handed_back", False)


def test_an_operator_who_takes_control_and_walks_away_expires_instead_of_hanging(
    tmp_path: Path,
) -> None:
    """Taking the session restarts the clock, so an operator gets the full window. Walking away
    with it still ends the run: a paused run must never hold the browser forever."""

    class TakesAndLeaves(FakeOperator):
        def __call__(self, ms: int) -> None:
            self.polls += 1
            assert self.polls < 500, "the run must not poll forever"
            self.clock.sleep(ms)
            if self.store.path.exists() and self.store.read().phase == "paused_for_human":
                self.takes += 1
                self.store.transition(
                    "take_control",
                    "operator",
                    operator_id=self.who,
                    now=NOW + timedelta(seconds=self.clock.t),
                )

    def script(store: StateStore, clock: FakeClock, _s: ScriptedSurface) -> FakeOperator:
        return TakesAndLeaves(store, clock)

    result, _, events, store = _with_operator(
        tmp_path, _capability([DOWN]), ready_at=None, operator=script, wait_s=5.0
    )
    assert (result.status, result.exit_code) == ("escalated", 3)
    expired = [e for e in events if e["event"] == "intervention_expired"]
    assert expired
    assert "did not hand it back" in expired[0]["detail"]
    human = result.recoveries[0]
    assert isinstance(human, HumanIntervention)
    assert (human.outcome, human.operator_id) == ("expired", "teller-9")
    assert store.read().phase == "finished"


def test_an_abort_after_an_earlier_hand_back_is_not_blamed_on_the_first_operator(
    tmp_path: Path,
) -> None:
    """Each pause is closed by its own transitions. The operator who answered pause one is not
    recorded as the one who ended pause two."""

    class OnceThenSilent(FakeOperator):
        def __call__(self, ms: int) -> None:
            self.clock.sleep(ms)
            if not self.store.path.exists():
                return
            state = self.store.read()
            at = NOW + timedelta(seconds=self.clock.t)
            if state.phase == "paused_for_human" and self.takes == 0:
                self.takes += 1
                self.store.transition("take_control", "operator", operator_id=self.who, now=at)
            elif state.phase == "human_active":
                self.store.transition(
                    "hand_back", "operator", operator_id=self.who, note="nothing to do", now=at
                )

    result, _, _, _ = _with_operator(
        tmp_path,
        _capability([DOWN]),
        ready_at=None,
        operator=lambda store, clock, _s: OnceThenSilent(store, clock),
        wait_s=5.0,
    )
    assert result.status == "escalated"
    first, second = result.recoveries[0], result.recoveries[1]
    assert isinstance(first, HumanIntervention)
    assert isinstance(second, HumanIntervention)
    assert (first.outcome, first.operator_id) == ("handed_back", "teller-9")
    assert (second.outcome, second.operator_id, second.operator_note) == ("expired", None, "")


def test_an_abort_under_a_running_step_ends_the_run_instead_of_crashing(tmp_path: Path) -> None:
    """Losing control mid-step is an escalation, not a traceback: the operator took the browser
    and the run has nothing left to do."""

    class TakesThenAborts(FakeOperator):
        def __call__(self, ms: int) -> None:
            self.clock.sleep(ms)
            if not self.store.path.exists():
                return
            state = self.store.read()
            at = NOW + timedelta(seconds=self.clock.t)
            if state.phase == "paused_for_human":
                self.store.transition("take_control", "operator", operator_id=self.who, now=at)
            elif state.phase == "human_active":
                # Hand back, then take the session away again while the run is re-verifying.
                self.store.transition("hand_back", "operator", operator_id=self.who, now=at)
                self.store.transition(
                    "abort", "operator", operator_id=self.who, note="changed my mind", now=at
                )

    clock = FakeClock()
    surface = ScriptedSurface(clock, None)
    evidence = EvidenceWriter(tmp_path, "replay_probe", Redactor())
    store = StateStore(evidence.dir / "session_state.json")
    control = file_control(store.path)
    driver = TakesThenAborts(store, clock)
    channel = OperatorChannel(
        run_id="replay_probe",
        capability_id="coreledger.member.poll_probe",
        evidence=evidence,
        surface=cast(Any, surface),
        redactor=Redactor(),
        resume_command=lambda run: run,
        wait_s=30.0,
        sleep=driver,
        now=lambda: NOW + timedelta(seconds=clock.t),
        announce=lambda _text: None,
    )

    class Guarded(ScriptedSurface):
        """Reads and acts go through the control hook, the way a real surface does."""

        def frame_text(self, frame_path: Sequence[str]) -> str:
            control()
            return super().frame_text(frame_path)

    guarded = Guarded(clock, None)
    gate = PolicyGate(load_policy(POLICY_FILE, "coreledger-readonly"))
    result = ReplayEngine(
        _capability([DOWN]),
        guarded,
        GatedSurface(guarded, guarded, gate),
        inputs={},
        masked_inputs={},
        secrets={},
        redactor=Redactor(),
        evidence=evidence,
        run_id="replay_probe",
        started_at=NOW,
        clock=clock,
        escalation=channel,
    ).run()
    assert (result.status, result.exit_code) == ("escalated", 3)
    assert result.intervention_path
    assert store.read().phase == "finished"


def test_a_run_whose_session_is_taken_with_no_pause_stops_as_operator_aborted(
    tmp_path: Path,
) -> None:
    """Somebody aborts a session that was never paused. The next read raises NotInControl, and
    that is an ending the caller can read, not a traceback."""
    clock = FakeClock()
    evidence = EvidenceWriter(tmp_path, "replay_probe", Redactor())
    store = StateStore(evidence.dir / "session_state.json")
    store.create(SessionState.start("replay_probe", "replay"))
    store.transition("stuck_detected", "automation", intervention_id="iv_01")
    store.transition("take_control", "operator", operator_id="teller-9")
    control = file_control(store.path)

    class Guarded(ScriptedSurface):
        def frame_text(self, frame_path: Sequence[str]) -> str:
            control()
            return super().frame_text(frame_path)

    guarded = Guarded(clock, 0.0)
    gate = PolicyGate(load_policy(POLICY_FILE, "coreledger-readonly"))
    result = ReplayEngine(
        _capability([]),
        guarded,
        GatedSurface(guarded, guarded, gate),
        inputs={},
        masked_inputs={},
        secrets={},
        redactor=Redactor(),
        evidence=evidence,
        run_id="replay_probe",
        started_at=NOW,
        clock=clock,
    ).run()
    assert (result.status, result.outcome_code, result.exit_code) == (
        "hard_failure",
        "OPERATOR_ABORTED",
        2,
    )
    assert result.observed
    assert "not automation" in result.observed
