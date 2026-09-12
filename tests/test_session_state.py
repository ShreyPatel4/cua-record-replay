"""Session control: the state machine, who may fire what, the automation lock, and the shared file.

The store is used through two handles on one path, the way the run and the ops CLI share it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from cua.session.intervention import InterventionRequest
from cua.session.state import (
    IllegalTransition,
    NotInControl,
    SessionState,
    StaleState,
    StateStore,
    advance,
    require_automation,
)


def _paused() -> SessionState:
    return advance(
        SessionState.start("replay_1", "replay"),
        "stuck_detected",
        "automation",
        intervention_id="int_1",
    )


def test_full_handoff_cycle() -> None:
    state = _paused()
    assert (state.phase, state.controller, state.intervention_id) == (
        "paused_for_human",
        "nobody",
        "int_1",
    )
    state = advance(state, "take_control", "operator")
    assert (state.phase, state.controller) == ("human_active", "human")
    state = advance(state, "hand_back", "operator", note="cleared app_error, reopened member")
    assert (state.phase, state.controller, state.intervention_id) == (
        "resuming",
        "automation",
        "int_1",
    )
    state = advance(state, "checkpoint_reverified", "automation")
    assert (state.phase, state.controller, state.intervention_id) == ("running", "automation", None)
    state = advance(state, "finish", "automation")
    assert state.phase == "finished"
    assert state.version == 5
    assert [h.event for h in state.history] == [
        "stuck_detected",
        "take_control",
        "hand_back",
        "checkpoint_reverified",
        "finish",
    ]


def test_failed_reverification_pauses_again() -> None:
    state = advance(advance(_paused(), "take_control", "operator"), "hand_back", "operator")
    state = advance(state, "reverify_failed", "automation", intervention_id="int_2")
    assert (state.phase, state.controller, state.intervention_id) == (
        "paused_for_human",
        "nobody",
        "int_2",
    )


@pytest.mark.parametrize(
    ("event", "actor", "match"),
    [
        ("take_control", "operator", "not allowed from 'running'"),
        ("stuck_detected", "operator", "may not fire"),
        ("hand_back", "operator", "not allowed from 'running'"),
    ],
)
def test_illegal_transitions_from_running(event: str, actor: str, match: str) -> None:
    with pytest.raises(IllegalTransition, match=match):
        advance(SessionState.start("r", "replay"), event, actor, intervention_id="int")  # type: ignore[arg-type]


def test_operator_cannot_skip_reverification() -> None:
    state = advance(advance(_paused(), "take_control", "operator"), "hand_back", "operator")
    with pytest.raises(IllegalTransition, match="may not fire"):
        advance(state, "checkpoint_reverified", "operator")


def test_stuck_must_open_an_intervention() -> None:
    with pytest.raises(IllegalTransition, match="must open an intervention"):
        advance(SessionState.start("r", "replay"), "stuck_detected", "automation")


def test_controller_must_match_phase() -> None:
    fields = SessionState.start("r", "replay").model_dump()
    fields["controller"] = "human"
    with pytest.raises(ValidationError, match="requires controller automation"):
        SessionState.model_validate(fields)


@pytest.mark.parametrize("phase_events", [["stuck_detected"], ["stuck_detected", "take_control"]])
def test_automation_is_locked_out_unless_it_holds_control(phase_events: list[str]) -> None:
    state = SessionState.start("r", "replay")
    for event in phase_events:
        actor = "automation" if event == "stuck_detected" else "operator"
        state = advance(state, event, actor, intervention_id="int")  # type: ignore[arg-type]
    with pytest.raises(NotInControl):
        require_automation(state)


def test_store_shares_state_across_handles_with_compare_and_swap(tmp_path: Path) -> None:
    path = tmp_path / "run" / "session_state.json"
    run_side, ops_side = StateStore(path), StateStore(path)
    run_side.create(SessionState.start("r", "replay"))
    with pytest.raises(FileExistsError):
        run_side.create(SessionState.start("r", "replay"))

    paused = run_side.transition(
        "stuck_detected", "automation", expected_version=0, intervention_id="int"
    )
    assert ops_side.read() == paused
    ops_side.transition("take_control", "operator", expected_version=1)
    with pytest.raises(StaleState, match="version 2, caller expected 1"):
        run_side.transition("hand_back", "operator", expected_version=1)
    assert sorted(p.name for p in path.parent.iterdir()) == [
        "session_state.json",
        "session_state.json.lock",
    ]


def _intervention(**overrides: object) -> dict[str, object]:
    now = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    fields: dict[str, object] = {
        "intervention_id": "int_1",
        "run_id": "replay_1",
        "kind": "replay",
        "capability_id": "coreledger.member.read_savings_balance",
        "step_id": "s06",
        "reason_code": "HARD_FAILURE_ESCALATE",
        "reason_text": "APP_ERROR on member detail",
        "current_url": "http://127.0.0.1:5050/members/*0007",
        "screenshot_path": "evidence/replay_1/step_06.png",
        "a11y_snapshot_path": "evidence/replay_1/a11y_06.json",
        "resume_command": "uv run cua ops take-control replay_1",
        "requested_at": now,
        "expires_at": now + timedelta(minutes=30),
    }
    fields.update(overrides)
    return fields


def test_intervention_request_shape() -> None:
    InterventionRequest.model_validate(_intervention())
    with pytest.raises(ValidationError, match="need capability_id"):
        InterventionRequest.model_validate(_intervention(capability_id=None))
    with pytest.raises(ValidationError, match="need goal"):
        InterventionRequest.model_validate(_intervention(kind="discovery", capability_id=None))
    with pytest.raises(ValidationError, match="expires_at must be after"):
        InterventionRequest.model_validate(
            _intervention(expires_at=datetime(2026, 9, 12, 14, 0, tzinfo=UTC))
        )
