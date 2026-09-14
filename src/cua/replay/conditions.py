"""Condition evaluation: whether a checkpoint or detector trigger holds on the surface right now.

One look, no waiting. The replay engine polls it; discovery uses it to check a derived checkpoint.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from typing import Protocol

from cua.artifact.inputs import TypedInput, render
from cua.artifact.schema import (
    AllOf,
    AnyOf,
    CheckpointTimeout,
    Condition,
    ElementPresent,
    StatusCode,
    Target,
    TextPresent,
    UrlMatches,
)
from cua.locate.backend import LocatorBackend
from cua.locate.resolver import Resolved, resolve
from cua.surface.base import Surface, SurfaceError
from cua.vocab import TEMPLATE_RE

OnResolved = Callable[[Target, Resolved], None]


class ConditionSurface(Surface, LocatorBackend, Protocol):
    """A surface that can also resolve ladders, which element_present needs."""


def _render_text(text: str, inputs: Mapping[str, TypedInput]) -> str:
    return render(text, inputs=inputs, secrets={}, surface={})


def render_pattern(pattern: str, inputs: Mapping[str, TypedInput]) -> str:
    """Templates in a URL regex render regex-escaped, so a value is matched literally."""
    return TEMPLATE_RE.sub(
        lambda m: re.escape(_render_text(m.group(0), inputs)),
        pattern,
    )


def holds(
    condition: Condition,
    surface: ConditionSurface,
    inputs: Mapping[str, TypedInput],
    *,
    timed_out: frozenset[str] = frozenset(),
    on_resolved: OnResolved | None = None,
) -> bool:
    """A frame that is missing or mid-navigation makes its condition false, never an error.

    on_resolved sees every element_present target that resolved, so a caller can report drift on
    checkpoint targets once the whole condition passes.
    """
    try:
        if isinstance(condition, TextPresent):
            wanted = " ".join(_render_text(condition.text, inputs).split())
            return wanted in surface.frame_text(condition.frame_path)
        if isinstance(condition, UrlMatches):
            pattern = render_pattern(condition.pattern, inputs)
            return re.search(pattern, surface.frame_url(condition.frame_path)) is not None
        if isinstance(condition, StatusCode):
            return surface.frame_status(condition.frame_path) == condition.code
        if isinstance(condition, ElementPresent):
            found = resolve(condition.target, surface)
            if isinstance(found, Resolved) and on_resolved is not None:
                on_resolved(condition.target, found)
            return isinstance(found, Resolved)
        if isinstance(condition, CheckpointTimeout):
            return condition.checkpoint in timed_out
        if isinstance(condition, AllOf):
            return all(
                holds(c, surface, inputs, timed_out=timed_out, on_resolved=on_resolved)
                for c in condition.conditions
            )
        if isinstance(condition, AnyOf):
            return any(
                holds(c, surface, inputs, timed_out=timed_out, on_resolved=on_resolved)
                for c in condition.conditions
            )
    except SurfaceError:
        return False
    raise TypeError(f"unknown condition {condition!r}")


def describe(condition: Condition) -> str:
    """The condition in words, templates left as templates."""
    if isinstance(condition, TextPresent):
        return f"text {condition.text!r} in {_frame(condition.frame_path)}"
    if isinstance(condition, UrlMatches):
        return f"URL of {_frame(condition.frame_path)} matching {condition.pattern!r}"
    if isinstance(condition, StatusCode):
        return f"status {condition.code} in {_frame(condition.frame_path)}"
    if isinstance(condition, ElementPresent):
        target = condition.target
        first = target.ladder[0]
        return f"element {first.strategy} ({target.fingerprint.kind or 'any'}) in " + _frame(
            target.frame_path
        )
    if isinstance(condition, CheckpointTimeout):
        return f"{condition.checkpoint} timing out"
    joiner = " and " if isinstance(condition, AllOf) else " or "
    return "(" + joiner.join(describe(c) for c in condition.conditions) + ")"


def failing(
    condition: Condition, surface: ConditionSurface, inputs: Mapping[str, TypedInput]
) -> list[str]:
    """The leaf conditions that do not hold right now, in words. Empty when the whole holds."""
    if isinstance(condition, AllOf):
        return [line for c in condition.conditions for line in failing(c, surface, inputs)]
    if isinstance(condition, AnyOf):
        if any(holds(c, surface, inputs) for c in condition.conditions):
            return []
        return [f"none of {describe(condition)}"]
    return [] if holds(condition, surface, inputs) else [f"missing {describe(condition)}"]


def _frame(frame_path: list[str]) -> str:
    return "frame " + "/".join(frame_path) if frame_path else "the top document"
