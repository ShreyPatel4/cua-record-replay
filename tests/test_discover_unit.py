"""Discovery pieces that need no browser: stuck detection, retry-once, SDK mapping, parameters.

Also output parsing and the evidence writer, which discovery is the first caller of.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from cua.artifact.schema import Fingerprint, LabelTextRung, Target
from cua.discover.loop import StuckDetector, ask_with_one_retry, late_event_report
from cua.discover.model import (
    AnthropicModel,
    ModelError,
    ModelTurn,
    TransientModelError,
    turn_from_message,
)
from cua.discover.record import (
    DiscoveryRecord,
    ObservedStep,
    SecretSpec,
    beside_a_label,
    dialog_message_pattern,
    propose_parameters,
)
from cua.discover.run import DiscoverRequest, DiscoveryError, run_discovery
from cua.evidence.writer import EvidenceWriter
from cua.locate.recorder import Recording
from cua.policy.redact import Redactor
from cua.replay.extract import ExtractionError, parse_value
from cua.replay.result import DateValue, MoneyValue
from cua.surface.base import A11ySnapshot, Box, SnapshotNode, SurfaceEvent

from support import ROOT

REQUEST = httpx2.Request("POST", "https://api.invalid/v1/messages")


def test_the_same_call_three_times_running_is_stuck() -> None:
    stuck = StuckDetector()
    click = (("ref", "Find"), ("tool", "click"))
    assert stuck.note_call(click) is None
    assert stuck.note_call(click) is None
    assert stuck.note_call(click) == "repeated_action"


def test_a_different_call_in_between_resets_the_repeat_count() -> None:
    stuck = StuckDetector()
    click, read = (("tool", "click"),), (("tool", "read_text"),)
    for key in (click, click, read, click, click):
        assert stuck.note_call(key) is None


def test_two_actions_in_a_row_that_change_nothing_are_stuck() -> None:
    stuck = StuckDetector()
    assert stuck.note_act("a", "b") is None
    assert stuck.note_act("b", "b") is None
    assert stuck.note_act("b", "c") is None, "a change resets the count"
    assert stuck.note_act("c", "c") is None
    assert stuck.note_act("c", "c") == "no_progress"


def _turn() -> ModelTurn:
    return ModelTurn(text="", tool_uses=[], stop_reason="tool_use")


def test_a_transient_model_error_is_retried_exactly_once() -> None:
    calls: list[str] = []
    retries: list[str] = []

    def flaky() -> ModelTurn:
        calls.append("call")
        if len(calls) == 1:
            raise TransientModelError("overloaded")
        return _turn()

    assert ask_with_one_retry(flaky, retries.append) == _turn()
    assert len(calls) == 2
    assert retries == ["overloaded"]


def test_a_second_transient_error_fails_the_call() -> None:
    calls: list[str] = []

    def down() -> ModelTurn:
        calls.append("call")
        raise TransientModelError("still down")

    with pytest.raises(TransientModelError):
        ask_with_one_retry(down, lambda _: None)
    assert len(calls) == 2


def test_a_permanent_model_error_is_not_retried() -> None:
    calls: list[str] = []

    def refused() -> ModelTurn:
        calls.append("call")
        raise ModelError("HTTP 400")

    with pytest.raises(ModelError):
        ask_with_one_retry(refused, lambda _: None)
    assert calls == ["call"]


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (anthropic.APIConnectionError(request=REQUEST), TransientModelError),
        (anthropic.APITimeoutError(request=REQUEST), TransientModelError),
        (
            anthropic.APIStatusError(
                "overloaded", response=httpx2.Response(529, request=REQUEST), body=None
            ),
            TransientModelError,
        ),
        (
            anthropic.APIStatusError(
                "server", response=httpx2.Response(500, request=REQUEST), body=None
            ),
            TransientModelError,
        ),
        (
            anthropic.APIStatusError(
                "bad request", response=httpx2.Response(400, request=REQUEST), body=None
            ),
            ModelError,
        ),
        (
            anthropic.APIStatusError(
                "unauthorized", response=httpx2.Response(401, request=REQUEST), body=None
            ),
            ModelError,
        ),
    ],
)
def test_sdk_errors_map_to_retryable_or_not(
    monkeypatch: pytest.MonkeyPatch, raised: Exception, expected: type[Exception]
) -> None:
    model = AnthropicModel("test-key-not-real", "claude-sonnet-4-6")

    def create(**_: Any) -> Any:
        raise raised

    monkeypatch.setattr(model._client.messages, "create", create)
    with pytest.raises(expected):
        model.complete(system="s", tools=[], messages=[])


def test_a_response_becomes_text_tool_uses_and_replayable_content() -> None:
    message = SimpleNamespace(
        content=[
            SimpleNamespace(type="text", text=" Sign in first. "),
            SimpleNamespace(type="tool_use", id="toolu_1", name="click", input={"ref": "f2e21"}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(input_tokens=1200, output_tokens=40),
    )
    turn = turn_from_message(message)
    assert turn.text == "Sign in first."
    assert [(u.id, u.name, u.input) for u in turn.tool_uses] == [
        ("toolu_1", "click", {"ref": "f2e21"})
    ]
    assert turn.content[1] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "click",
        "input": {"ref": "f2e21"},
    }
    assert (turn.input_tokens, turn.output_tokens) == (1200, 40)


@pytest.mark.parametrize(
    ("text", "parse", "value"),
    [
        ("$4,210.55", "money_usd", MoneyValue(amount=Decimal("4210.55"), currency="USD")),
        (" $ 12 ", "money_usd", MoneyValue(amount=Decimal("12"), currency="USD")),
        ("-$3.10", "money_usd", MoneyValue(amount=Decimal("-3.10"), currency="USD")),
        ("($3.10)", "money_usd", MoneyValue(amount=Decimal("-3.10"), currency="USD")),
        ("1,204", "integer", 1204),
        ("2012-09-05", "date_iso", DateValue.model_validate({"value": "2012-09-05"})),
        ("  Gray   Wrenfield ", "text", "Gray Wrenfield"),
    ],
)
def test_visible_text_parses_into_typed_outputs(text: str, parse: Any, value: object) -> None:
    assert parse_value(text, parse) == value


@pytest.mark.parametrize(
    ("text", "parse"),
    [
        ("Share Savings", "money_usd"),
        ("4210.55", "money_usd"),
        ("($3.10", "money_usd"),
        ("12a", "integer"),
        ("09/05/2012", "date_iso"),
        ("   ", "text"),
    ],
)
def test_unparseable_text_fails_without_echoing_it(text: str, parse: Any) -> None:
    with pytest.raises(ExtractionError) as caught:
        parse_value(text, parse)
    assert text.strip() == "" or text not in str(caught.value)


EMPTY = A11ySnapshot(nodes=[], frame_urls={})


def _field(label: str) -> Recording:
    target = Target(
        ladder=[
            LabelTextRung(
                strategy="label_text",
                label=label,
                relation="same_row",
                control="textbox",
                confidence=0.9,
            )
        ],
        recorded_rung=0,
        frame_path=["main"],
        fingerprint=Fingerprint(role="textbox", kind="input"),
        notes="test field",
    )
    return Recording(target=target, weak=False, dropped=())


def _typed(value: str, label: str, max_length: int | None = None) -> ObservedStep:
    return ObservedStep(
        "type_text",
        "reversible",
        EMPTY,
        EMPTY,
        target=_field(label),
        value=value,
        max_length=max_length,
    )


def test_only_literals_the_goal_names_are_proposed_as_inputs() -> None:
    record = DiscoveryRecord(
        goal="Log in, look up member 10007 and read their current savings balance",
        entry_url="http://127.0.0.1:5050/",
        summary="done",
        steps=[
            _typed("{{secrets.operator_id}}", "operator id"),
            _typed("10007", "member number", max_length=5),
            _typed("branch-7", "branch code"),
            _typed("10007", "member number", max_length=5),
        ],
        outputs=[],
        checkpoint_target=_field("member number"),
    )
    proposals = propose_parameters(record)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert (proposal.literal, proposal.name, proposal.pattern) == (
        "10007",
        "member_number",
        "^[0-9]{5}$",
    )
    assert proposal.step_indexes == (1, 3)


def test_a_literal_inside_a_longer_word_is_not_a_parameter() -> None:
    record = DiscoveryRecord(
        goal="look up member 100071",
        entry_url="http://127.0.0.1:5050/",
        summary="done",
        steps=[_typed("10007", "member number")],
        outputs=[],
        checkpoint_target=_field("member number"),
    )
    assert propose_parameters(record) == []


def test_a_value_learned_mid_run_is_masked_from_then_on() -> None:
    redactor = Redactor([])
    assert redactor.text("balance $4,210.55") == "balance $4,210.55"
    redactor.add_secret("$4,210.55")
    redactor.add_secret("4210.55")
    assert redactor.text("balance $4,210.55, raw 4210.55") == "balance [REDACTED], raw [REDACTED]"
    with pytest.raises(ValueError, match="shorter"):
        redactor.add_secret("$12")


def test_evidence_is_redacted_and_the_manifest_flags_sign_in_screenshots(tmp_path: Path) -> None:
    redactor = Redactor(["planted-password-123"], identities=["teller-0417"])
    writer = EvidenceWriter(tmp_path, "disc_test", redactor, sensitive_pages=["/login"])
    writer.event(
        "model",
        "model_turn",
        rationale="teller-0417 types planted-password-123 for member 10007",
        arguments={"text": "planted-password-123", "amount": 12345},
    )
    writer.screenshot("step_00.png", b"\x89PNG login", page_urls=["http://h/", "http://h/login"])
    writer.screenshot("step_01.png", b"\x89PNG search", page_urls=["http://h/members/search"])
    manifest = json.loads(writer.finish().read_text())

    line = json.loads((tmp_path / "disc_test" / "run.jsonl").read_text())
    assert line["rationale"] == "[REDACTED] types [REDACTED] for member *0007"
    assert line["arguments"] == {"text": "[REDACTED]", "amount": 12345}
    flags = {f["path"]: f["sensitive"] for f in manifest["files"]}
    assert flags == {"run.jsonl": False, "step_00.png": True, "step_01.png": False}
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])


def test_a_digit_pattern_is_fixed_length_only_when_the_field_enforces_it() -> None:
    record = DiscoveryRecord(
        goal="deposit 100 for member 10007",
        entry_url="http://127.0.0.1:5050/",
        summary="done",
        steps=[_typed("100", "initial deposit", max_length=10), _typed("10007", "member", 5)],
        outputs=[],
        checkpoint_target=_field("member number"),
    )
    assert [p.pattern for p in propose_parameters(record)] == ["^[0-9]+$", "^[0-9]{5}$"]


def test_a_record_number_in_a_navigated_url_is_proposed_as_an_input() -> None:
    record = DiscoveryRecord(
        goal="Read the savings balance of member 10007",
        entry_url="http://127.0.0.1:5050/",
        summary="done",
        steps=[
            ObservedStep("navigate", "safe", EMPTY, EMPTY, url="{{surface.entry_url}}"),
            ObservedStep(
                "navigate", "safe", EMPTY, EMPTY, url="http://127.0.0.1:5050/members/10007?tab=2"
            ),
        ],
        outputs=[],
        checkpoint_target=_field("member number"),
    )
    [proposal] = propose_parameters(record)
    assert (proposal.literal, proposal.name, proposal.pattern) == ("10007", "member_id", "^[0-9]+$")
    assert proposal.step_indexes == (1,)


def _node(role: str, label: str, x: float, y: float, w: float = 100) -> SnapshotNode:
    return SnapshotNode(
        ref="e1",
        role=role,
        name=label,
        frame_path=["main"],
        box=Box(x=x, y=y, w=w, h=23),
        depth=3,
    )


def test_a_value_cell_next_to_its_label_is_record_data() -> None:
    header = _node("cell", "Member profile", 7, 56, w=1266)
    label = _node("cell", "Name", 11, 85, w=112)
    value = _node("cell", "Gray Wrenfield", 125, 85, w=121)
    far = _node("cell", "Balances", 643, 86, w=236)
    snapshot = A11ySnapshot(nodes=[header, label, value, far], frame_urls={})
    assert beside_a_label(value, snapshot)
    assert not beside_a_label(header, snapshot)
    assert not beside_a_label(label, snapshot)
    assert not beside_a_label(far, snapshot), "a column away is layout, not a label"


def test_dialog_patterns_keep_the_wording_but_not_the_numbers() -> None:
    pattern = dialog_message_pattern("Close account 10007? Balance $4.00 is lost.")
    assert pattern == r"^Close\ account\ [0-9]+\?\ Balance\ \$[0-9]+\.[0-9]+\ is\ lost\.$"
    assert "10007" not in pattern


def test_late_side_effects_that_undo_an_action_are_reported() -> None:
    blocked = SurfaceEvent(
        "navigation_blocked", "path /logout matches paths_deny", "http://h/logout"
    )
    answered = SurfaceEvent("dialog_answered", "confirm: ok -> accept")
    warning, logged = late_event_report([answered, blocked])
    assert warning is not None
    assert "navigation_blocked" in warning
    assert [e["kind"] for e in logged] == ["dialog_answered", "navigation_blocked"]
    assert late_event_report([answered]) == (
        None,
        [{"kind": "dialog_answered", "detail": "confirm: ok -> accept", "url": ""}],
    )


class _NeverCalled:
    model = "never"

    def complete(self, **_: Any) -> Any:
        raise AssertionError("preflight must fail before any model call")


def _preflight_request(tmp_path: Path, capability_id: str) -> DiscoverRequest:
    return DiscoverRequest(
        goal="g",
        entry_url="http://127.0.0.1:5050/",
        capability_id=capability_id,
        name="n",
        policy_id="coreledger-readonly",
        policy_file=ROOT / "policy" / "allowlist.yaml",
        secrets=[SecretSpec("operator_password", "PW", "credential")],
        evidence_root=tmp_path / "evidence",
        artifacts_root=tmp_path / "artifacts",
    )


def test_discovery_refuses_before_paying_for_a_run_it_could_not_save(tmp_path: Path) -> None:
    (tmp_path / "artifacts").mkdir()
    taken = "coreledger.member.read_savings_balance"
    (tmp_path / "artifacts" / f"{taken}@1.0.0.capability.json").write_text("{}")
    for capability_id, fragment in ((taken, "already exists"), ("not-dotted", "must be dotted")):
        with pytest.raises(DiscoveryError, match=fragment):
            run_discovery(
                _preflight_request(tmp_path, capability_id),
                model=_NeverCalled(),
                environ={"PW": "long-enough-password"},
                confirm=lambda proposals: proposals,
            )
    assert not (tmp_path / "evidence").exists()
