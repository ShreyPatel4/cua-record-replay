"""Resolver: walk a target's ladder on the live surface and return exactly one element or a reason.

One pass, no guessing: a rung wins only with exactly one match of the recorded kind.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cua.artifact.schema import BBoxRung, Fingerprint, Target
from cua.locate.backend import ElementFacts, LocatorBackend
from cua.surface.base import Element, SurfaceError

# More candidates than this is ambiguous whatever their kinds; describing them all is wasted work.
MAX_DESCRIBED = 3


@dataclass(frozen=True)
class RungAttempt:
    index: int
    strategy: str
    matches: int


@dataclass(frozen=True)
class Resolved:
    element: Element
    rung_index: int
    strategy: str
    attempts: tuple[RungAttempt, ...]
    drift: bool
    fragile: bool
    identity_changed: bool


@dataclass(frozen=True)
class Unresolved:
    code: Literal["TARGET_NOT_FOUND", "TARGET_AMBIGUOUS"]
    attempts: tuple[RungAttempt, ...]

    @property
    def detail(self) -> str:
        tried = ", ".join(f"{a.strategy}={a.matches}" for a in self.attempts)
        return f"{self.code}: matches per rung {tried}"


def same_kind(facts: ElementFacts, fingerprint: Fingerprint) -> bool:
    """Kind, role, and input type agree with what was recorded; names are allowed to change."""
    return (
        (fingerprint.kind is None or facts.kind == fingerprint.kind)
        and (fingerprint.role is None or facts.role == fingerprint.role)
        and (fingerprint.input_type is None or facts.input_type == fingerprint.input_type)
    )


def _renamed(facts: ElementFacts, fingerprint: Fingerprint) -> bool:
    if fingerprint.name is not None and facts.name != fingerprint.name:
        return True
    return fingerprint.text is not None and facts.own_text[:80] != fingerprint.text


def resolve(target: Target, backend: LocatorBackend) -> Resolved | Unresolved:
    """Try rungs top to bottom; the first with exactly one visible match of the recorded kind wins.

    Candidates whose kind, role, or input type disagree with the fingerprint are not matches, so a
    fallback rung cannot land on a different sort of element (an empty cell where a button was). A
    rung with two or more matches is skipped, never guessed from. If no rung wins and any rung was
    ambiguous the result is TARGET_AMBIGUOUS, otherwise TARGET_NOT_FOUND. Winning below the
    recorded rung sets drift, winning on bbox sets fragile, and a winner whose name or text differs
    from the fingerprint sets identity_changed, which replay treats as a reason for extra caution.
    """
    fingerprint = target.fingerprint
    attempts: list[RungAttempt] = []
    for index, rung in enumerate(target.ladder):
        if isinstance(rung, BBoxRung) and fingerprint.role is None and fingerprint.name is None:
            attempts.append(RungAttempt(index=index, strategy=rung.strategy, matches=0))
            continue
        raw = backend.candidates(rung, target.frame_path)
        matching: list[tuple[Element, ElementFacts]] = []
        if len(raw) <= MAX_DESCRIBED:
            for element in raw:
                try:
                    facts = backend.describe(element)
                except SurfaceError:
                    continue
                if same_kind(facts, fingerprint):
                    matching.append((element, facts))
        count = len(matching) if len(raw) <= MAX_DESCRIBED else len(raw)
        attempts.append(RungAttempt(index=index, strategy=rung.strategy, matches=count))
        if count == 1:
            element, facts = matching[0]
            return Resolved(
                element=element,
                rung_index=index,
                strategy=rung.strategy,
                attempts=tuple(attempts),
                drift=index > target.recorded_rung,
                fragile=isinstance(rung, BBoxRung),
                identity_changed=_renamed(facts, fingerprint),
            )
    ambiguous = any(a.matches > 1 for a in attempts)
    return Unresolved(
        code="TARGET_AMBIGUOUS" if ambiguous else "TARGET_NOT_FOUND", attempts=tuple(attempts)
    )
