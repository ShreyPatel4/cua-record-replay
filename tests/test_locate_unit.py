"""Ladder logic without a browser: the one-match rule, drift, weak targets, and verification.

A fake backend decides what each rung matches, so every branch is reachable and exact.
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
from cua.locate.backend import AnchorFact, ElementFacts, NormBox
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


class FakeBackend:
    def __init__(
        self, matches: dict[str, list[Element]], facts: ElementFacts | None = None
    ) -> None:
        self.matches = matches
        self.facts = facts or _facts()

    def candidates(self, rung: Rung, frame_path: Sequence[str]) -> list[Element]:
        return self.matches.get(rung.strategy, [])

    def same_element(self, a: Element, b: Element) -> bool:
        return a is b

    def describe(self, element: Element) -> ElementFacts:
        return self.facts


def _target(recorded: int = 0) -> Target:
    return Target(
        ladder=[TEXT, ANCHOR, BOX],
        recorded_rung=recorded,
        frame_path=["main"],
        fingerprint=Fingerprint(role="cell", name="Find"),
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


def test_an_ambiguous_rung_is_skipped_and_the_fallback_is_drift() -> None:
    result = resolve(
        _target(), FakeBackend({"text_exact": [FIND, OTHER], "anchor_relative": [FIND]})
    )
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


def test_a_coordinate_hit_with_the_wrong_role_does_not_count() -> None:
    textbox = _facts(role="textbox", kind="input", own_text="", name=None)
    result = resolve(_target(), FakeBackend({"bbox": [OTHER]}, textbox))
    assert isinstance(result, Unresolved)
    assert result.code == "TARGET_NOT_FOUND"


@pytest.mark.parametrize(
    ("matches", "code"),
    [({}, "TARGET_NOT_FOUND"), ({"text_exact": [FIND, OTHER]}, "TARGET_AMBIGUOUS")],
)
def test_no_winner_says_why(matches: dict[str, list[Element]], code: str) -> None:
    result = resolve(_target(), FakeBackend(matches))
    assert isinstance(result, Unresolved)
    assert result.code == code
    assert result.detail.startswith(code)


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


def test_recorder_keeps_only_rungs_that_resolve_to_this_element() -> None:
    backend = FakeBackend(
        {"text_exact": [FIND], "anchor_relative": [FIND], "bbox": [OTHER]}, _facts()
    )
    recording = record(FIND, backend)
    assert [r.strategy for r in recording.target.ladder] == ["text_exact", "anchor_relative"]
    assert recording.dropped == ("bbox (1 matches, other element)",)
    assert not recording.weak
    assert recording.target.fingerprint.text == "Find"
    assert "role_name" not in recording.target.notes, "cell is not a meaningful role to key on"


def test_a_target_known_only_by_position_is_weak() -> None:
    backend = FakeBackend(
        {"anchor_relative": [FIND], "bbox": [FIND]}, _facts(name=None, own_text="")
    )
    recording = record(FIND, backend)
    assert [r.strategy for r in recording.target.ladder] == ["anchor_relative", "bbox"]
    assert recording.weak
    assert "Weak target" in recording.target.notes


def test_coordinates_alone_cannot_be_recorded() -> None:
    backend = FakeBackend({"bbox": [FIND]}, _facts(name=None, own_text="", anchors=[]))
    with pytest.raises(RecordingError, match="no stable rung"):
        record(FIND, backend)


def test_volatile_and_sensitive_text_never_reaches_rungs_or_fingerprint() -> None:
    facts = _facts(role="cell", name="$4,210.55", own_text="$4,210.55", kind="text")
    backend = FakeBackend({"text_exact": [FIND], "anchor_relative": [FIND]}, facts)
    recording = record(FIND, backend, volatile_text=True)
    assert [r.strategy for r in recording.target.ladder] == ["anchor_relative"]
    assert recording.target.fingerprint.text is None

    pin = _facts(name="Reset PIN", own_text="Reset PIN")
    recorded = record(FIND, FakeBackend({"text_exact": [FIND]}, pin))
    assert recorded.target.fingerprint.text is None, "a credential-looking target keeps no text"
