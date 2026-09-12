"""Resolver: walk a target's ladder on the live surface and return exactly one element or a reason.

One pass, no guessing: a rung wins only with exactly one match. Retrying over time is the caller's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from cua.artifact.schema import BBoxRung, Target
from cua.locate.backend import LocatorBackend
from cua.surface.base import Element


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


@dataclass(frozen=True)
class Unresolved:
    code: Literal["TARGET_NOT_FOUND", "TARGET_AMBIGUOUS"]
    attempts: tuple[RungAttempt, ...]

    @property
    def detail(self) -> str:
        tried = ", ".join(f"{a.strategy}={a.matches}" for a in self.attempts)
        return f"{self.code}: matches per rung {tried}"


def resolve(target: Target, backend: LocatorBackend) -> Resolved | Unresolved:
    """Try rungs top to bottom; the first with exactly one visible match wins.

    A rung with two or more matches is skipped, never guessed from. If no rung wins and any rung was
    ambiguous the result is TARGET_AMBIGUOUS, otherwise TARGET_NOT_FOUND. Winning below the
    recorded rung sets drift; winning on the bbox rung sets fragile. Coordinates always hit
    something, so a bbox hit only counts when its role agrees with the recorded fingerprint.
    """
    attempts: list[RungAttempt] = []
    for index, rung in enumerate(target.ladder):
        found = backend.candidates(rung, target.frame_path)
        if isinstance(rung, BBoxRung) and target.fingerprint.role is not None:
            found = [e for e in found if backend.describe(e).role == target.fingerprint.role]
        attempts.append(RungAttempt(index=index, strategy=rung.strategy, matches=len(found)))
        if len(found) == 1:
            return Resolved(
                element=found[0],
                rung_index=index,
                strategy=rung.strategy,
                attempts=tuple(attempts),
                drift=index > target.recorded_rung,
                fragile=rung.strategy == "bbox",
            )
    ambiguous = any(a.matches > 1 for a in attempts)
    return Unresolved(
        code="TARGET_AMBIGUOUS" if ambiguous else "TARGET_NOT_FOUND", attempts=tuple(attempts)
    )
