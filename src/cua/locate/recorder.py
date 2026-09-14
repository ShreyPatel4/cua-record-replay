"""Recorder: turn one live element into a verified locator ladder, ranked and explained.

Every rung is resolved on the page before it is kept, so a ladder never ships a failing rung.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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
FORM_KINDS = frozenset({"input", "select", "checkbox"})
# Text that is data rather than interface: amounts, member numbers, dates, times. A rung keyed on
# it cannot resolve for the next record, and it may be sensitive, so the recorder never uses it.
DATA_LIKE_RE = re.compile(r"[$€£]|[0-9]{3,}|[0-9]+[.,:/-][0-9]+")
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


def looks_like_data(text: str) -> bool:
    return bool(DATA_LIKE_RE.search(text))


def _reason(rung: Rung) -> str:
    """Why a kept rung is trustworthy, in the words a reviewer checks it against."""
    if isinstance(rung, RoleNameRung):
        return (
            f"role_name: the accessibility tree calls it {rung.role} '{rung.name}', the handle "
            "least tied to layout"
        )
    if isinstance(rung, LabelTextRung):
        return (
            f"label_text: reached through its visible label '{rung.label}' "
            f"({rung.relation.replace('_', ' ')}), which survives the control moving"
        )
    if isinstance(rung, TextExactRung):
        return f"text_exact: its own text '{rung.text}' is interface wording, not data"
    if isinstance(rung, AnchorRelativeRung):
        row = " in the same row" if rung.same_row else ""
        slot = f", number {rung.nth} that way" if rung.nth > 1 else ""
        where = {"right": "right of", "left": "left of", "below": "below", "above": "above"}
        return (
            f"anchor_relative: the {rung.target_kind} {where[rung.direction]} the fixed text "
            f"'{rung.anchor_text}'{row}{slot}, which survives the element itself being renamed"
        )
    return "bbox: coordinates as a last resort, broken by any layout change"


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


def _candidate_rungs(
    facts: ElementFacts, *, private: bool, role_hint: str | None, name_hint: str | None
) -> list[list[Rung]]:
    """Alternatives per rank, in canonical rank order. The first verified alternative is kept.

    private: the element's own text is data or sensitive, so nothing may key on it. For form
    controls the name comes from a label, not the value, so it stays usable.
    """
    content_named = facts.kind not in FORM_KINDS
    ranks: list[list[Rung]] = []

    role_names: list[Rung] = []
    for role, name in ((role_hint, name_hint), (facts.role, facts.name)):
        if not role or not name or role in NON_SEMANTIC_ROLES or looks_like_data(name):
            continue
        if private and content_named:
            continue
        rung = RoleNameRung(
            strategy="role_name", role=role, name=normalize_prose(name), confidence=0.95
        )
        if rung not in role_names:
            role_names.append(rung)
    if role_names:
        ranks.append(role_names)

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
                if not looks_like_data(label.label)
            ]
        )
    text = facts.own_text
    usable_text = bool(text) and len(text) <= MAX_TEXT_RUNG and not looks_like_data(text)
    if usable_text and control is None and not private:
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
                if not looks_like_data(anchor.anchor_text)
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
    role_hint: str | None = None,
    name_hint: str | None = None,
    redact: Callable[[str], str] | None = None,
) -> Recording:
    """Build the ladder for an element the caller already identified (by ref or by resolution).

    sensitive: the element's text, or the value typed into it, must never be recorded.
    volatile_text: the element's text is data that changes per record (a balance).
    role_hint, name_hint: the role and accessible name the browser's own accessibility tree
    reported for the node (a snapshot node), tried before the page script's approximation.
    redact: applied to the fingerprint's name and text before they enter the artifact.
    """
    facts = backend.describe(element)
    private = sensitive or volatile_text
    kept: list[Rung] = []
    dropped: list[str] = []
    ranks = _candidate_rungs(facts, private=private, role_hint=role_hint, name_hint=name_hint)
    for alternatives in ranks:
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
    if volatile_text:
        private_note = " Its own text is data that changes per record, so nothing keys on it."
    elif sensitive:
        private_note = " It takes or shows sensitive data, so its text is never recorded."
    else:
        private_note = ""
    notes = normalize_prose(
        "Every rung here resolved to exactly this element on the live page. "
        + " ".join(f"{_reason(r)}." for r in kept)
        + private_note
        + (f" Dropped: {'; '.join(dropped)}." if dropped else "")
        + (
            " Weak target: only position identifies it, so replay reports any fallback as drift."
            if weak
            else ""
        )
    )

    def clean(value: str) -> str:
        return redact(value) if redact is not None else value

    content_named = facts.kind not in FORM_KINDS
    name = facts.name
    if name is not None and content_named and (private or looks_like_data(name)):
        name = None
    fingerprint = Fingerprint(
        role=facts.role,
        name=clean(normalize_prose(name)) if name else None,
        input_type=facts.input_type,
        kind=facts.kind if facts.kind != "other" else None,
    )
    target = Target(
        ladder=kept,
        recorded_rung=0,
        frame_path=facts.frame_path,
        fingerprint=fingerprint,
        notes=notes,
    )
    text_ok = bool(facts.own_text) and not looks_like_data(facts.own_text)
    if text_ok and not (private or target.looks_sensitive):
        text = clean(normalize_prose(facts.own_text))[:80]
        target = Target.model_validate(
            target.model_dump() | {"fingerprint": fingerprint.model_dump() | {"text": text}}
        )
    return Recording(target=target, weak=weak, dropped=tuple(dropped))
