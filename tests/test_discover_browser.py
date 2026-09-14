"""Discovery end to end with a scripted model: the real loop, gate, recorder, and evidence writer.

No API calls. The scripted model finds refs in the same screen text a real model receives.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Browser

from cua.artifact.catalog import load_capability
from cua.artifact.schema import AllOf, CheckpointWait, ElementPresent, TextPresent, TypeTextStep
from cua.discover.model import ModelTurn, ToolUse
from cua.discover.record import ParamProposal, SecretSpec
from cua.discover.run import DiscoverRequest, DiscoveryResult, run_discovery
from mock_app.data import money
from mock_app.server import RunningMockApp
from mock_app.settings import MockSettings

from support import ROOT, find_leaks

pytestmark = pytest.mark.browser

LINE = re.compile(
    r'^\s*\[(?P<ref>\w+)\] (?P<role>\w+)(?: "(?P<label>(?:[^"\\]|\\.)*)")?[^@]*@(?P<frame>\S+)'
)
Move = Callable[[str], tuple[str, dict[str, Any]]]
SECRETS = [
    SecretSpec("operator_id", "CORELEDGER_OPERATOR_USER", "identity"),
    SecretSpec("operator_password", "CORELEDGER_OPERATOR_PASSWORD", "credential"),
]


def _latest_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    content: list[dict[str, Any]] = messages[-1]["content"]
    if content and content[0].get("type") == "tool_result":
        blocks: list[dict[str, Any]] = content[0]["content"]
        return blocks
    return content


def ref(screen: str, role: str, label: str | None = None, *, after: str | None = None) -> str:
    lines = screen.splitlines()
    start = 0
    if after is not None:
        start = next(i for i, line in enumerate(lines) if f'"{after}"' in line) + 1
    for line in lines[start:]:
        match = LINE.match(line)
        wanted = match is not None and match["role"] == role and match["frame"] == "main"
        if wanted and match is not None and (label is None or match["label"] == label):
            return match["ref"]
    raise AssertionError(f"no {role} {label!r} on screen:\n{screen}")


class ScriptedModel:
    """Plays a fixed list of moves, reading refs from the latest screen like a model would."""

    model = "scripted-test-model"

    def __init__(self, moves: list[Move]) -> None:
        self.moves = moves
        self.screens: list[str] = []
        self.results: list[str] = []

    def complete(
        self, *, system: str, tools: list[dict[str, Any]], messages: list[dict[str, Any]]
    ) -> ModelTurn:
        assert "request_confirmation" in system
        blocks = _latest_blocks(messages)
        texts = [b["text"] for b in blocks if b.get("type") == "text"]
        screen = next(t for t in texts if t.startswith("Screen "))
        self.screens.append(screen)
        if len(messages) > 1:
            self.results.append(texts[0])
        index = len(self.screens) - 1
        name, args = (
            self.moves[index](screen)
            if index < len(self.moves)
            else ("give_up", {"reason": "the script ran out"})
        )
        use = ToolUse(id=f"toolu_{index:02d}", name=name, input=args)
        return ModelTurn(
            text=f"Scripted move {index}.",
            tool_uses=[use],
            stop_reason="tool_use",
            content=[
                {"type": "text", "text": f"Scripted move {index}."},
                {"type": "tool_use", "id": use.id, "name": name, "input": args},
            ],
        )


SIGN_IN: list[Move] = [
    lambda s: (
        "type_text",
        {"ref": ref(s, "textbox", "Operator ID"), "text": "{{secrets.operator_id}}"},
    ),
    lambda s: (
        "type_text",
        {"ref": ref(s, "textbox", "Password"), "text": "{{secrets.operator_password}}"},
    ),
    lambda s: ("click", {"ref": ref(s, "button", "Sign in")}),
]
LOOK_UP: list[Move] = [
    lambda s: ("type_text", {"ref": ref(s, "textbox"), "text": "10007"}),
    lambda s: ("click", {"ref": ref(s, "cell", "Find")}),
]
READ_BALANCE: list[Move] = [
    lambda s: (
        "declare_output",
        {"name": "savings_balance", "ref": ref(s, "cell", after="Share Savings"), "type": "money"},
    ),
    lambda s: (
        "done",
        {"summary": "Read the balance.", "checkpoint_ref": ref(s, "cell", after="Share Savings")},
    ),
]
GOAL = "Log in, look up member 10007 and read their current savings balance."


def _policy_file(tmp_path: Path, base_url: str) -> Path:
    text = (ROOT / "policy" / "allowlist.yaml").read_text(encoding="utf-8")
    path = tmp_path / "allowlist.yaml"
    path.write_text(text.replace("http://127.0.0.1:5050", base_url), encoding="utf-8")
    return path


def _discover(
    tmp_path: Path,
    coreledger: RunningMockApp,
    mock_settings: MockSettings,
    browser: Browser,
    moves: list[Move],
    *,
    goal: str = GOAL,
    confirm: Callable[[list[ParamProposal]], list[ParamProposal]] = lambda proposals: proposals,
    **overrides: Any,
) -> tuple[DiscoveryResult, ScriptedModel, Path]:
    policy_file = _policy_file(tmp_path, coreledger.base_url)
    fields: dict[str, Any] = {
        "goal": goal,
        "entry_url": coreledger.base_url + "/",
        "capability_id": "coreledger.member.read_savings_balance",
        "name": "Read member savings balance",
        "policy_id": "coreledger-readonly",
        "policy_file": policy_file,
        "secrets": SECRETS,
        "evidence_root": tmp_path / "evidence",
        "artifacts_root": tmp_path / "artifacts",
        "max_steps": 20,
        "timeout_s": 90,
    }
    model = ScriptedModel(moves)
    result = run_discovery(
        DiscoverRequest(**(fields | overrides)),
        model=model,
        environ={
            "CORELEDGER_OPERATOR_USER": mock_settings.operator_user,
            "CORELEDGER_OPERATOR_PASSWORD": mock_settings.operator_password,
        },
        confirm=confirm,
        browser=browser,
    )
    return result, model, policy_file


def test_a_scripted_run_records_a_valid_parameterized_draft(
    tmp_path: Path, coreledger: RunningMockApp, mock_settings: MockSettings, browser: Browser
) -> None:
    result, _, policy_file = _discover(
        tmp_path, coreledger, mock_settings, browser, SIGN_IN + LOOK_UP + READ_BALANCE
    )
    assert result.status == "recorded", result.message
    assert result.artifact_path is not None
    capability = load_capability(Path(result.artifact_path), policy_file)

    assert [s.action for s in capability.steps] == [
        "navigate",
        "type_text",
        "type_text",
        "click",
        "type_text",
        "click",
    ]
    typed = [s for s in capability.steps if isinstance(s, TypeTextStep)]
    assert [s.value for s in typed] == [
        "{{secrets.operator_id}}",
        "{{secrets.operator_password}}",
        "{{inputs.member_number}}",
    ]
    assert {s.name: s.kind for s in capability.secrets} == {
        "operator_id": "identity",
        "operator_password": "credential",
    }
    [member_number] = capability.inputs
    assert (member_number.name, member_number.pattern) == ("member_number", "^[0-9]{5}$")

    sign_in_wait = capability.steps[3].wait_for
    assert isinstance(sign_in_wait, CheckpointWait)
    sign_in_condition = capability.checkpoints[sign_in_wait.checkpoint].condition
    assert isinstance(sign_in_condition, AllOf)
    assert TextPresent(kind="text_present", text="Member Lookup", frame_path=["main"]) in (
        sign_in_condition.conditions
    )
    assert any(isinstance(c, ElementPresent) for c in sign_in_condition.conditions)

    success = capability.checkpoints[capability.success.checkpoint].condition
    assert isinstance(success, AllOf)
    texts = {c.text for c in success.conditions if isinstance(c, TextPresent)}
    assert texts == {"Member profile", "{{inputs.member_number}}"}
    assert capability.success.requires_outputs == ["savings_balance"]
    [balance] = capability.outputs
    assert balance.sensitive
    assert balance.extract.parse == "money_usd"
    assert balance.extract.target.fingerprint.text is None
    assert capability.outcome_detectors == []
    assert capability.capability.status == "draft"
    assert capability.provenance.recorded_by == "discover"

    artifact_text = Path(result.artifact_path).read_text()
    assert "10007" not in artifact_text
    assert mock_settings.operator_password not in artifact_text
    assert mock_settings.operator_user not in artifact_text


def test_the_model_and_the_evidence_never_see_secrets_or_full_member_numbers(
    tmp_path: Path, coreledger: RunningMockApp, mock_settings: MockSettings, browser: Browser
) -> None:
    result, model, _ = _discover(
        tmp_path, coreledger, mock_settings, browser, SIGN_IN + LOOK_UP + READ_BALANCE
    )
    assert result.status == "recorded", result.message
    for screen in model.screens:
        assert mock_settings.operator_password not in screen
        assert mock_settings.operator_user not in screen
    assert any("*0007" in screen for screen in model.screens)

    evidence = Path(result.evidence_dir)
    names = {p.name for p in evidence.iterdir()}
    assert {"run.jsonl", "result.json", "manifest.json", "artifact.capability.json"} <= names
    assert {"step_00.png", "step_05.png"} <= names
    files = sorted(p for p in evidence.rglob("*") if p.is_file())
    assert find_leaks(files, {"PW": mock_settings.operator_password}) == []
    for path in files:
        if path.suffix in (".json", ".jsonl"):
            text = path.read_text()
            assert "10007" not in text, path.name
            assert mock_settings.operator_user not in text, path.name

    events = [json.loads(line) for line in (evidence / "run.jsonl").read_text().splitlines()]
    kinds = {e["event"] for e in events}
    assert {"run_started", "observation", "model_turn", "tool_result", "decision"} <= kinds
    assert {"parameters_confirmed", "artifact_saved", "run_finished"} <= kinds
    rationale = [e["rationale"] for e in events if e["event"] == "model_turn"]
    assert rationale[0] == "Scripted move 0."
    manifest = json.loads((evidence / "manifest.json").read_text())
    flagged = {f["path"] for f in manifest["files"] if f["sensitive"]}
    assert "step_00.png" in flagged, "the sign-in screen is a sensitive page"
    assert "step_05.png" in flagged, "the screen showing the sensitive output"
    assert "step_04.png" not in flagged
    log = (evidence / "run.jsonl").read_text()
    savings = coreledger.state.ledger.members["10007"].savings
    for form in (money(savings), f"{savings:,.2f}", f"{savings:.2f}"):
        assert form not in log, "the declared balance never reaches the log"
    assert "Scripted move" in log, "the model's rationale does"


def test_a_declined_parameter_stays_a_literal(
    tmp_path: Path, coreledger: RunningMockApp, mock_settings: MockSettings, browser: Browser
) -> None:
    result, _, policy_file = _discover(
        tmp_path,
        coreledger,
        mock_settings,
        browser,
        SIGN_IN + LOOK_UP + READ_BALANCE,
        confirm=lambda proposals: [],
    )
    assert result.status == "recorded", result.message
    assert result.artifact_path is not None
    assert [p.accepted for p in result.parameters] == [False]
    capability = load_capability(Path(result.artifact_path), policy_file)
    assert capability.inputs == []
    assert [s.value for s in capability.steps if isinstance(s, TypeTextStep)][-1] == "10007"


def test_two_actions_that_change_nothing_stop_the_run_as_stuck(
    tmp_path: Path, coreledger: RunningMockApp, mock_settings: MockSettings, browser: Browser
) -> None:
    header: Move = lambda s: ("click", {"ref": ref(s, "cell", "Operator sign-in")})  # noqa: E731
    result, model, _ = _discover(tmp_path, coreledger, mock_settings, browser, [header, header])
    assert (result.status, result.stop_reason) == ("stopped", "no_progress")
    assert result.artifact_path is None
    assert len(model.screens) == 2
    assert list(Path(result.evidence_dir).glob("a11y_*.json"))


def test_an_invalid_call_is_answered_and_the_run_continues(
    tmp_path: Path, coreledger: RunningMockApp, mock_settings: MockSettings, browser: Browser
) -> None:
    bad: Move = lambda s: ("click", {"ref": "zz999"})  # noqa: E731
    give_up: Move = lambda s: ("give_up", {"reason": "demo"})  # noqa: E731
    result, model, _ = _discover(tmp_path, coreledger, mock_settings, browser, [bad, give_up])
    assert (result.status, result.stop_reason) == ("stopped", "gave_up")
    assert "not in the latest snapshot" in model.results[0]


def test_request_confirmation_is_denied_and_the_irreversible_click_never_happens(
    tmp_path: Path, coreledger: RunningMockApp, mock_settings: MockSettings, browser: Browser
) -> None:
    moves: list[Move] = [
        *SIGN_IN,
        *LOOK_UP,
        lambda s: ("click", {"ref": ref(s, "link", "Open sub-account")}),
        lambda s: ("request_confirmation", {"reason": "open a sub-account"}),
        lambda s: ("click", {"ref": ref(s, "cell", "Create sub-account")}),
        lambda s: ("give_up", {"reason": "irreversible actions are not allowed"}),
    ]
    result, model, _ = _discover(
        tmp_path,
        coreledger,
        mock_settings,
        browser,
        moves,
        goal="Open a sub-account for member 10007.",
        capability_id="coreledger.member.open_subaccount",
        policy_id="coreledger-subaccount",
    )
    assert (result.status, result.stop_reason) == ("stopped", "gave_up")
    # results[k] answers move k; the final give_up is never answered.
    assert model.results[-1].startswith("Denied: irreversible actions are not allowed")
    assert model.results[-2].startswith("Denied: this discovery run does not allow")
    assert coreledger.state.ledger.subaccounts == {}


def test_a_confirmed_run_answers_the_dialog_and_records_an_irreversible_step(
    tmp_path: Path, coreledger: RunningMockApp, mock_settings: MockSettings, browser: Browser
) -> None:
    create: Move = lambda s: ("click", {"ref": ref(s, "cell", "Create sub-account")})  # noqa: E731
    moves: list[Move] = [
        *SIGN_IN,
        *LOOK_UP,
        lambda s: ("click", {"ref": ref(s, "link", "Open sub-account")}),
        lambda s: ("select_option", {"ref": ref(s, "combobox"), "option": "Money Market"}),
        lambda s: ("type_text", {"ref": ref(s, "textbox", after="Nickname"), "text": "Rainy Day"}),
        lambda s: (
            "type_text",
            {"ref": ref(s, "textbox", after="Initial deposit"), "text": "25.00"},
        ),
        lambda s: ("request_confirmation", {"reason": "open the sub-account"}),
        create,
        lambda s: (
            "click",
            {"ref": ref(s, "cell", "Create sub-account"), "dialog": "accept"},
        ),
        lambda s: (
            "done",
            {"summary": "Opened.", "checkpoint_ref": ref(s, "cell", "Sub-account opened")},
        ),
    ]
    result, model, policy_file = _discover(
        tmp_path,
        coreledger,
        mock_settings,
        browser,
        moves,
        goal="Open a Money Market sub-account named Rainy Day for member 10007 with 25.00.",
        capability_id="coreledger.member.open_subaccount",
        name="Open a sub-account",
        policy_id="coreledger-subaccount",
        allow_irreversible=True,
    )
    assert result.status == "recorded", result.message
    assert "dismissed: 'Open this sub-account? This cannot be undone.'" in model.results[10]
    assert len(coreledger.state.ledger.subaccounts) == 1
    assert result.artifact_path is not None
    capability = load_capability(Path(result.artifact_path), policy_file)
    create_step = capability.steps[-1]
    assert create_step.action == "click"
    assert create_step.risk == "irreversible"
    assert create_step.dialog is not None
    assert create_step.dialog.response == "accept"
    assert {i.name for i in capability.inputs} == {
        "member_number",
        "account_type",
        "nickname",
        "initial_deposit",
    }
    success = capability.checkpoints[capability.success.checkpoint].condition
    assert isinstance(success, AllOf)
    assert "Sub-account opened" in {
        c.text for c in success.conditions if isinstance(c, TextPresent)
    }
