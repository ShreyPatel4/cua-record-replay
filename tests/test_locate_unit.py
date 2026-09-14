"""Ladder logic without a browser: the one-match rule, kind checks, drift, and recording rules.

A fake backend decides what each rung matches and what each element is, so every branch is exact.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from cua.artifact.schema import (
    AnchorRelativeRung,
    BBoxRung,
    Fingerprint,
    Rung,
    Target,
    TextExactRung,
)
from cua.locate.backend import AnchorFact, ElementFacts, LabelFact, NormBox
from cua.locate.recorder import RecordingError, record
from cua.locate.resolver import Resolved, Unresolved, resolve
from cua.surface.base import Element

FIND = Element(handle="find", frame_path=("main",))
OTHER = Element(handle="other", frame_path=("main",))
TEXT = TextExactRung(strategy="text_exact", text="Find", confidence=0.85)
ANCHOR = AnchorRelativeRung(
    strategy="anchor_relative",
    anchor_text="Member number",
    direction="right",
    same_row=True,
    target_kind="clickable",
    confidence=0.8,
)
BOX = BBoxRung(strategy="bbox", x=0.2, y=0.05, w=0.05, h=0.03, fragile=True, confidence=0.4)


def _facts(**overrides: object) -> ElementFacts:
    fields: dict[str, object] = {
        "tag": "span",
        "role": "cell",
        "name": "Find",
        "own_text": "Find",
        "input_type": None,
        "kind": "clickable",
        "frame_path": ["main"],
        "labels": [],
        "anchors": [
            AnchorFact(anchor_text="Member number", direction="right", same_row=True, nth=1)
        ],
        "frame_box": NormBox(x=0.2, y=0.05, w=0.05, h=0.03),
    }
    fields.update(overrides)
    return ElementFacts.model_validate(fields)


class FakeBackend:
    def __init__(
        self,
        matches: dict[str, list[Element]],
        facts: dict[object, ElementFacts] | ElementFacts | None = None,
    ) -> None:
        self.matches = matches
        self.facts = facts if facts is not None else _facts()

    def candidates(self, rung: Rung, frame_path: Sequence[str]) -> list[Element]:
        return self.matches.get(rung.strategy, [])

    def same_element(self, a: Element, b: Element) -> bool:
        return a is b

    def describe(self, element: Element) -> ElementFacts:
        if isinstance(self.facts, dict):
            return self.facts[element.handle]
        return self.facts


def _target(recorded: int = 0, **fingerprint: object) -> Target:
    fields: dict[str, object] = {
        "role": "cell",
        "name": "Find",
        "text": "Find",
        "kind": "clickable",
    }
    fields.update(fingerprint)
    return Target(
        ladder=[TEXT, ANCHOR, BOX],
        recorded_rung=recorded,
        frame_path=["main"],
        fingerprint=Fingerprint.model_validate(fields),
        notes="n",
    )


def test_the_first_rung_with_exactly_one_match_wins() -> None:
    result = resolve(_target(), FakeBackend({"text_exact": [FIND], "anchor_relative": [OTHER]}))
    assert isinstance(result, Resolved)
    assert (result.element, result.strategy, result.drift, result.fragile) == (
        FIND,
        "text_exact",
        False,
        False,
    )
    assert not result.identity_changed


def test_an_ambiguous_rung_is_skipped_and_the_fallback_is_drift() -> None:
    backend = FakeBackend({"text_exact": [FIND, OTHER], "anchor_relative": [FIND]})
    result = resolve(_target(), backend)
    assert isinstance(result, Resolved)
    assert (result.rung_index, result.drift) == (1, True)
    assert [a.matches for a in result.attempts] == [2, 1]


def test_winning_above_the_recorded_rung_is_not_drift() -> None:
    result = resolve(_target(recorded=1), FakeBackend({"text_exact": [FIND]}))
    assert isinstance(result, Resolved)
    assert not result.drift


def test_coordinates_winning_is_fragile() -> None:
    result = resolve(_target(), FakeBackend({"bbox": [FIND]}))
    assert isinstance(result, Resolved)
    assert result.fragile
    assert result.drift


def test_a_match_of_another_kind_is_not_a_match() -> None:
    """The reviewer's case: after drift, coordinates land on an empty cell where Find was."""
    empty_cell = _facts(tag="td", kind="text", name=None, own_text="x")
    result = resolve(_target(), FakeBackend({"bbox": [OTHER]}, {"other": empty_cell}))
    assert isinstance(result, Unresolved)
    assert result.code == "TARGET_NOT_FOUND"


def test_the_kind_check_can_turn_two_candidates_into_one() -> None:
    facts = {"find": _facts(), "other": _facts(tag="td", kind="text")}
    result = resolve(_target(), FakeBackend({"text_exact": [OTHER, FIND]}, facts))
    assert isinstance(result, Resolved)
    assert result.element is FIND


def test_a_renamed_winner_resolves_but_says_so() -> None:
    search = _facts(name="Search", own_text="Search")
    result = resolve(_target(), FakeBackend({"anchor_relative": [FIND]}, search))
    assert isinstance(result, Resolved)
    assert (result.drift, result.identity_changed) == (True, True)


@pytest.mark.parametrize(
    ("matches", "code"),
    [({}, "TARGET_NOT_FOUND"), ({"text_exact": [FIND, OTHER]}, "TARGET_AMBIGUOUS")],
)
def test_no_winner_says_why(matches: dict[str, list[Element]], code: str) -> None:
    result = resolve(_target(), FakeBackend(matches))
    assert isinstance(result, Unresolved)
    assert result.code == code
    assert result.detail.startswith(code)


def test_recorder_keeps_only_rungs_that_resolve_to_this_element() -> None:
    backend = FakeBackend({"text_exact": [FIND], "anchor_relative": [FIND], "bbox": [OTHER]})
    recording = record(FIND, backend)
    assert [r.strategy for r in recording.target.ladder] == ["text_exact", "anchor_relative"]
    assert recording.dropped == ("bbox (1 matches, other element)",)
    assert not recording.weak
    fingerprint = recording.target.fingerprint
    assert (fingerprint.text, fingerprint.kind, fingerprint.role) == ("Find", "clickable", "cell")


def test_a_target_known_only_by_position_is_weak() -> None:
    backend = FakeBackend(
        {"anchor_relative": [FIND], "bbox": [FIND]}, _facts(name=None, own_text="")
    )
    recording = record(FIND, backend)
    assert [r.strategy for r in recording.target.ladder] == ["anchor_relative", "bbox"]
    assert recording.weak
    assert "Weak target" in recording.target.notes
    assert "anchor_relative: the " in recording.target.notes


def test_coordinates_alone_cannot_be_recorded() -> None:
    backend = FakeBackend({"bbox": [FIND]}, _facts(name=None, own_text="", anchors=[]))
    with pytest.raises(RecordingError, match="no stable rung"):
        record(FIND, backend)


def test_a_volatile_value_keeps_no_text_name_or_data_anchor() -> None:
    facts = _facts(
        tag="td",
        role="cell",
        name="$4,210.55",
        own_text="$4,210.55",
        kind="text",
        anchors=[
            AnchorFact(anchor_text="$1,002.10", direction="below", same_row=False, nth=1),
            AnchorFact(anchor_text="Share Savings", direction="right", same_row=True, nth=1),
        ],
    )
    backend = FakeBackend({"text_exact": [FIND], "anchor_relative": [FIND]}, facts)
    recording = record(FIND, backend, volatile_text=True)
    [anchor] = recording.target.ladder
    assert isinstance(anchor, AnchorRelativeRung)
    assert anchor.anchor_text == "Share Savings", (
        "an anchor on another balance never resolves again"
    )
    assert (recording.target.fingerprint.name, recording.target.fingerprint.text) == (None, None)


def test_a_sensitive_text_element_records_no_text_anywhere() -> None:
    facts = _facts(tag="td", name="10007", own_text="10007", kind="text")
    backend = FakeBackend({"text_exact": [FIND], "anchor_relative": [FIND]}, facts)
    recording = record(FIND, backend, sensitive=True)
    assert [r.strategy for r in recording.target.ladder] == ["anchor_relative"]
    assert "10007" not in recording.target.model_dump_json()


def test_data_looking_text_is_never_keyed_on_even_when_not_flagged() -> None:
    facts = _facts(tag="td", name="Member 10007", own_text="Member 10007", kind="text")
    recording = record(FIND, FakeBackend({"text_exact": [FIND], "anchor_relative": [FIND]}, facts))
    assert [r.strategy for r in recording.target.ladder] == ["anchor_relative"]
    assert "10007" not in recording.target.model_dump_json()


def test_a_password_field_keeps_its_label_but_never_text() -> None:
    facts = _facts(
        tag="input",
        role="textbox",
        name="Password",
        own_text="",
        input_type="password",
        kind="input",
        labels=[LabelFact(label="Password", relation="wrapped")],
        anchors=[],
    )
    backend = FakeBackend({"role_name": [FIND], "label_text": [FIND]}, facts)
    recording = record(FIND, backend, sensitive=True)
    assert [r.strategy for r in recording.target.ladder] == ["role_name", "label_text"]
    assert recording.target.fingerprint.name == "Password"
    assert recording.target.looks_sensitive


def test_the_browser_reported_name_is_tried_first_and_names_are_redacted() -> None:
    facts = _facts(
        tag="input", role="searchbox", name="Find member", own_text="", kind="input", anchors=[]
    )
    backend = FakeBackend({"role_name": [FIND]}, facts)
    recording = record(
        FIND, backend, role_hint="searchbox", name_hint="Find a member", redact=lambda s: s.upper()
    )
    first = recording.target.ladder[0]
    assert (first.strategy, getattr(first, "name", None)) == ("role_name", "Find a member")
    assert recording.target.fingerprint.name == "FIND MEMBER"
