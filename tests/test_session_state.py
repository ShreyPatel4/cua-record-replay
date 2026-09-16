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
    file_control,
    require_automation,
)

OP = "op-shrey"


def _paused() -> SessionState:
    return advance(
        SessionState.start("replay_1", "replay", owner_pid=4242),
        "stuck_detected",
        "automation",
        intervention_id="int_1",
    )


def _held() -> SessionState:
    return advance(_paused(), "take_control", "operator", operator_id=OP)


def test_full_handoff_cycle() -> None:
    state = _paused()
    assert (state.phase, state.controller, state.intervention_id, state.owner_pid) == (
        "paused_for_human",
        "nobody",
        "int_1",
        4242,
    )
    state = advance(state, "take_control", "operator", operator_id=OP)
    assert (state.phase, state.controller, state.operator_id) == ("human_active", "human", OP)
    state = advance(state, "hand_back", "operator", operator_id=OP, note="cleared app_error")
    assert (state.phase, state.controller, state.intervention_id) == (
        "resuming",
        "automation",
        "int_1",
    )
    state = advance(state, "checkpoint_reverified", "automation")
    assert (state.phase, state.intervention_id, state.operator_id) == ("running", None, None)
    state = advance(state, "finish", "automation")
    assert state.phase == "finished"
    assert state.version == 5
    assert [(h.event, h.operator_id) for h in state.history] == [
        ("stuck_detected", None),
        ("take_control", OP),
        ("hand_back", OP),
        ("checkpoint_reverified", None),
        ("finish", None),
    ]


def test_failed_reverification_pauses_again() -> None:
    state = advance(_held(), "hand_back", "operator", operator_id=OP)
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
        advance(
            SessionState.start("r", "replay"),
            event,  # type: ignore[arg-type]
            actor,  # type: ignore[arg-type]
            intervention_id="int",
            operator_id=OP,
        )


def test_operator_cannot_skip_reverification() -> None:
    state = advance(_held(), "hand_back", "operator", operator_id=OP)
    with pytest.raises(IllegalTransition, match="may not fire"):
        advance(state, "checkpoint_reverified", "operator", operator_id=OP)


def test_operator_events_must_name_the_operator() -> None:
    with pytest.raises(IllegalTransition, match="need an operator_id"):
        advance(_paused(), "take_control", "operator")


def test_only_the_operator_holding_control_can_hand_back_or_abort() -> None:
    for event in ("hand_back", "abort"):
        with pytest.raises(NotInControl, match="does not hold run replay_1"):
            advance(_held(), event, "operator", operator_id="op-someone-else")  # type: ignore[arg-type]


def test_abort_and_expiry_keep_the_intervention_for_the_result() -> None:
    aborted = advance(_held(), "abort", "operator", operator_id=OP)
    expired = advance(_held(), "expire", "system")
    for state in (aborted, expired):
        assert (state.phase, state.intervention_id) == ("finished", "int_1")


def test_an_operator_can_stop_a_run_that_is_not_paused() -> None:
    state = advance(SessionState.start("r", "replay"), "abort", "operator", operator_id=OP)
    assert state.phase == "finished"


def test_stuck_must_open_an_intervention() -> None:
    with pytest.raises(IllegalTransition, match="must open an intervention"):
        advance(SessionState.start("r", "replay"), "stuck_detected", "automation")


def test_controller_must_match_phase() -> None:
    fields = SessionState.start("r", "replay").model_dump()
    fields["controller"] = "human"
    with pytest.raises(ValidationError, match="requires controller automation"):
        SessionState.model_validate(fields)
    held = _held().model_dump()
    held["operator_id"] = None
    with pytest.raises(ValidationError, match="human_active requires the operator_id"):
        SessionState.model_validate(held)


@pytest.mark.parametrize("held", [False, True])
def test_automation_is_locked_out_unless_it_holds_control(held: bool) -> None:
    with pytest.raises(NotInControl):
        require_automation(_held() if held else _paused())


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
    ops_side.transition("take_control", "operator", expected_version=1, operator_id=OP)
    with pytest.raises(StaleState, match="version 2, caller expected 1"):
        run_side.transition("hand_back", "operator", expected_version=1, operator_id=OP)
    assert ops_side.read().operator_id == OP
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


def test_file_control_locks_automation_out_while_a_human_holds_the_session(tmp_path: Path) -> None:
    """The control hook every surface calls before it acts. The run and the ops CLI are separate
    processes, so the file is the only thing they both see."""
    path = tmp_path / "run" / "session_state.json"
    control = file_control(path)
    # No session file yet: nobody has ever paused this run, so automation owns it.
    control()

    store = StateStore(path)
    store.create(SessionState.start("r", "replay"))
    control()

    store.transition("stuck_detected", "automation", intervention_id="iv_01")
    with pytest.raises(NotInControl, match="paused_for_human"):
        control()

    store.transition("take_control", "operator", operator_id="teller-9")
    with pytest.raises(NotInControl, match="controller is human"):
        control()

    store.transition("hand_back", "operator", operator_id="teller-9")
    # Resuming: automation may act again, and re-verifies the step before carrying on.
    control()
