"""CLI surface: every subcommand from the plan exists, and unbuilt ones fail loudly, not silently.

Stubs exit 1 so nothing downstream can mistake an unimplemented command for a success.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from cua.cli import _operator_id, app
from cua.session.state import SessionState, StateStore
from mock_app.server import RunningMockApp
from mock_app.settings import MockSettings

runner = CliRunner()


def test_top_level_help_lists_every_subcommand() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for name in ("discover", "replay", "ops", "catalog", "mock"):
        assert name in result.output


def test_ops_help_lists_the_control_transfer_commands() -> None:
    result = runner.invoke(app, ["ops", "--help"])
    assert result.exit_code == 0
    for name in ("list", "show", "take-control", "hand-back", "abort"):
        assert name in result.output


def test_ops_commands_on_a_run_that_has_no_session_say_so(tmp_path: Path) -> None:
    for argv in (
        ["take-control", "run_x"],
        ["hand-back", "run_x"],
        ["abort", "run_x"],
        ["show", "run_x"],
    ):
        result = runner.invoke(app, ["ops", *argv, "--evidence-root", str(tmp_path)])
        assert result.exit_code == 1
        assert "no session for run 'run_x'" in result.output


def _paused(root: Path, run_id: str = "replay_demo", pid: int | None = None) -> StateStore:
    """A run paused for a human, the way the operator channel leaves one."""
    store = StateStore(root / run_id / "session_state.json")
    store.create(SessionState.start(run_id, "replay", owner_pid=pid or os.getpid()))
    store.transition("stuck_detected", "automation", intervention_id="iv_01", note="app error")
    (root / run_id / "intervention.json").write_text(
        json.dumps(
            {
                "intervention_id": "iv_01",
                "run_id": run_id,
                "reason_code": "HARD_FAILURE_ESCALATE",
                "step_id": "s06",
                "screenshot_path": "intervention_01.png",
            }
        )
    )
    return store


def test_the_ops_commands_walk_a_session_from_paused_to_resuming(tmp_path: Path) -> None:
    store = _paused(tmp_path)
    root = ["--evidence-root", str(tmp_path)]

    listed = runner.invoke(app, ["ops", "list", *root])
    assert listed.exit_code == 0
    assert "replay_demo" in listed.output
    assert "paused_for_human" in listed.output

    shown = runner.invoke(app, ["ops", "show", "replay_demo", *root, "--no-open"])
    assert shown.exit_code == 0
    assert "HARD_FAILURE_ESCALATE" in shown.output

    taken = runner.invoke(app, ["ops", "take-control", "replay_demo", *root, "--operator", "op-1"])
    assert taken.exit_code == 0
    assert store.read().controller == "human"
    assert "hand-back" in taken.output, "the operator is told how to give it back"

    again = runner.invoke(app, ["ops", "take-control", "replay_demo", *root, "--operator", "op-2"])
    assert again.exit_code == 1
    assert "human_active" in again.output

    wrong = runner.invoke(app, ["ops", "hand-back", "replay_demo", *root, "--operator", "op-2"])
    assert wrong.exit_code == 1
    assert "does not hold" in wrong.output

    back = runner.invoke(
        app, ["ops", "hand-back", "replay_demo", *root, "--operator", "op-1", "--note", "fixed it"]
    )
    assert back.exit_code == 0
    state = store.read()
    assert (state.phase, state.controller) == ("resuming", "automation")
    assert state.history[-1].note == "fixed it"


def test_an_operator_note_is_redacted_before_it_reaches_the_session_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Notes are free text in a committed evidence file, and an operator writes what they did."""
    store = _paused(tmp_path)
    root = ["--evidence-root", str(tmp_path)]
    monkeypatch.setenv("CORELEDGER_OPERATOR_PASSWORD", "break-glass-9182")
    runner.invoke(app, ["ops", "take-control", "replay_demo", *root, "--operator", "op-1"])
    done = runner.invoke(
        app,
        [
            "ops",
            "hand-back",
            "replay_demo",
            *root,
            "--operator",
            "op-1",
            "--note",
            "signed in with break-glass-9182 and reloaded",
        ],
    )
    assert done.exit_code == 0
    written = store.path.read_text()
    assert "break-glass-9182" not in written
    assert "[REDACTED]" in written


def test_a_session_whose_run_is_gone_cannot_be_taken(tmp_path: Path) -> None:
    """Nothing would answer the hand-back, so ops says the run is dead instead of pretending."""
    _paused(tmp_path, pid=2**22 - 1)
    root = ["--evidence-root", str(tmp_path)]
    listed = runner.invoke(app, ["ops", "list", *root])
    assert "dead" in listed.output
    taken = runner.invoke(app, ["ops", "take-control", "replay_demo", *root])
    assert taken.exit_code == 1
    assert "gone" in taken.output


def test_the_operator_id_falls_back_to_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CUA_OPERATOR", raising=False)
    monkeypatch.setenv("USER", "shrey")
    assert _operator_id(None) == "shrey"
    monkeypatch.setenv("CUA_OPERATOR", "teller-0417")
    assert _operator_id(None) == "teller-0417"
    assert _operator_id("  ") == "operator"


DISCOVER = ["discover", "--goal", "g", "--target", "http://127.0.0.1:5050/"]


def test_discover_refuses_to_run_unattended_without_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    """It ends by asking a human to confirm parameters, which needs a terminal or --yes."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    result = runner.invoke(app, DISCOVER)
    assert result.exit_code == 1
    assert "--yes" in result.output


def test_discover_without_an_api_key_says_replay_does_not_need_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An empty value blocks load_dotenv from filling the key in from a developer's .env.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    result = runner.invoke(app, [*DISCOVER, "--yes"])
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY is not set" in result.output


def test_mock_smoke_passes_against_a_live_server(
    coreledger: RunningMockApp, mock_settings: MockSettings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CORELEDGER_OPERATOR_USER", mock_settings.operator_user)
    monkeypatch.setenv("CORELEDGER_OPERATOR_PASSWORD", mock_settings.operator_password)
    result = runner.invoke(app, ["mock", "smoke", "--base-url", coreledger.base_url])
    assert result.exit_code == 0, result.output
    assert "checks passed" in result.output
