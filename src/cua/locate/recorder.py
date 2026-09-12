"""Recorder: turn one live element into a verified locator ladder, ranked and explained.

Every candidate rung is resolved against the page before it is kept, so a ladder never ships a rung
that already fails. Nothing here knows which surface is underneath.
"""

from __future__ import annotations

from dataclasses import dataclass

from cua.artifact.schema import (
    AnchorRelativeRung,
    BBoxRung,
    Fingerprint,
    LabelTextRung,
    RoleNameRung,
    Rung,
    Target,
    TextExactRung,
)
from cua.locate.backend import ElementFacts, LocatorBackend
from cua.surface.base import Element

# Roles whose accessible name is just their text content: a text rung says the same thing honestly.
NON_SEMANTIC_ROLES = frozenset({"cell", "generic", "row", "text", "paragraph", "columnheader"})
MAX_TEXT_RUNG = 80
_CONTROL_FOR_KIND = {"input": "textbox", "select": "combobox", "checkbox": "checkbox"}
_ANCHOR_KIND = {"input": "input", "select": "select", "clickable": "clickable", "text": "text"}


class RecordingError(ValueError):
    """No rung except raw coordinates identifies the element."""


@dataclass(frozen=True)
class Recording:
    target: Target
    weak: bool
    dropped: tuple[str, ...]


def normalize_prose(text: str) -> str:
    """Dash and whitespace normalization shared with rung matching on the page."""
    return " ".join(text.replace("\u2014", "-").replace("\u2013", "-").split())


def _confidence(rung: Rung) -> float:
    if isinstance(rung, RoleNameRung):
        return 0.95
    if isinstance(rung, LabelTextRung):
        return 0.9
    if isinstance(rung, TextExactRung):
        return 0.6 if len(rung.text) <= 3 else 0.85
    if isinstance(rung, AnchorRelativeRung):
        return 0.8 if rung.nth == 1 else 0.6
    return 0.4


def _candidate_rungs(facts: ElementFacts, volatile_text: bool) -> list[list[Rung]]:
    """Alternatives per rank, in canonical rank order. The first verified alternative is kept."""
    ranks: list[list[Rung]] = []
    if facts.role and facts.name and facts.role not in NON_SEMANTIC_ROLES and not volatile_text:
        ranks.append(
            [RoleNameRung(strategy="role_name", role=facts.role, name=facts.name, confidence=0.95)]
        )
    control = _CONTROL_FOR_KIND.get(facts.kind)
    if control is not None:
        ranks.append(
            [
                LabelTextRung(
                    strategy="label_text",
                    label=label.label,
                    relation=label.relation,
                    control=control,
                    confidence=0.9,
                )
                for label in facts.labels
            ]
        )
    text = facts.own_text
    if text and len(text) <= MAX_TEXT_RUNG and control is None and not volatile_text:
        ranks.append([TextExactRung(strategy="text_exact", text=text, confidence=0.85)])
    anchor_kind = _ANCHOR_KIND.get(facts.kind)
    if anchor_kind is not None:
        ranks.append(
            [
                AnchorRelativeRung(
                    strategy="anchor_relative",
                    anchor_text=anchor.anchor_text,
                    direction=anchor.direction,
                    same_row=anchor.same_row,
                    target_kind=anchor_kind,
                    nth=anchor.nth,
                    confidence=0.8,
                )
                for anchor in facts.anchors
            ]
        )
    if facts.frame_box is not None and facts.kind != "select":
        box = facts.frame_box
        ranks.append(
            [
                BBoxRung(
                    strategy="bbox",
                    x=round(box.x, 4),
                    y=round(box.y, 4),
                    w=round(box.w, 4),
                    h=round(box.h, 4),
                    fragile=True,
                    confidence=0.4,
                )
            ]
        )
    return ranks


def record(
    element: Element,
    backend: LocatorBackend,
    *,
    sensitive: bool = False,
    volatile_text: bool = False,
) -> Recording:
    """Build the ladder for an element the caller already identified (by ref or by resolution).

    sensitive: the value typed into it is a secret or sensitive input, so no text is recorded.
    volatile_text: the element's text is data that changes per record (a balance), so no rung may
    key on it.
    """
    facts = backend.describe(element)
    kept: list[Rung] = []
    dropped: list[str] = []
    for alternatives in _candidate_rungs(facts, volatile_text):
        for rung in alternatives:
            found = backend.candidates(rung, facts.frame_path)
            if len(found) == 1 and backend.same_element(found[0], element):
                kept.append(rung.model_copy(update={"confidence": _confidence(rung)}))
                break
            other = ", other element" if len(found) == 1 else ""
            dropped.append(f"{rung.strategy} ({len(found)} matches{other})")
    if not any(r.strategy != "bbox" for r in kept):
        raise RecordingError(
            f"no stable rung identifies this {facts.kind} element; "
            f"tried {', '.join(dropped) or 'nothing'}"
        )
    weak = not any(r.strategy in ("role_name", "label_text", "text_exact") for r in kept)
    notes = (
        f"Recorded from the live page: each rung resolved to exactly this element. "
        f"Kept {', '.join(r.strategy for r in kept)}."
        + (f" Dropped {'; '.join(dropped)}." if dropped else "")
        + (
            " Weak target: no name, label, or stable text, so it relies on position."
            if weak
            else ""
        )
    )
    fingerprint = Fingerprint(role=facts.role, name=facts.name, input_type=facts.input_type)
    target = Target(
        ladder=kept,
        recorded_rung=0,
        frame_path=facts.frame_path,
        fingerprint=fingerprint,
        notes=notes,
    )
    if facts.own_text and not (sensitive or volatile_text or target.looks_sensitive):
        text = normalize_prose(facts.own_text)[:80]
        target = Target.model_validate(
            target.model_dump() | {"fingerprint": fingerprint.model_dump() | {"text": text}}
        )
    return Recording(target=target, weak=weak, dropped=tuple(dropped))
