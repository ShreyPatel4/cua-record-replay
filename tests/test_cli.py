"""CLI surface: every subcommand from the plan exists, and unbuilt ones fail loudly, not silently.

Stubs exit 1 so nothing downstream can mistake an unimplemented command for a success.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from cua.cli import app
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


@pytest.mark.parametrize(
    "argv",
    [
        ["ops", "take-control", "run_x"],
    ],
)
def test_unbuilt_commands_exit_nonzero(argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == 1


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
