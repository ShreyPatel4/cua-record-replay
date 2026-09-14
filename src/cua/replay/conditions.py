"""Condition evaluation: whether a checkpoint or detector trigger holds on the surface right now.

One look, no waiting. The replay engine polls it; discovery uses it to check a derived checkpoint.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Protocol

from cua.artifact.inputs import TypedInput, render
from cua.artifact.schema import (
    AllOf,
    AnyOf,
    CheckpointTimeout,
    Condition,
    ElementPresent,
    StatusCode,
    TextPresent,
    UrlMatches,
)
from cua.locate.backend import LocatorBackend
from cua.locate.resolver import Resolved, resolve
from cua.surface.base import Surface, SurfaceError
from cua.vocab import TEMPLATE_RE


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
) -> bool:
    """A frame that is missing or mid-navigation makes its condition false, never an error."""
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
            return isinstance(resolve(condition.target, surface), Resolved)
        if isinstance(condition, CheckpointTimeout):
            return condition.checkpoint in timed_out
        if isinstance(condition, AllOf):
            return all(holds(c, surface, inputs, timed_out=timed_out) for c in condition.conditions)
        if isinstance(condition, AnyOf):
            return any(holds(c, surface, inputs, timed_out=timed_out) for c in condition.conditions)
    except SurfaceError:
        return False
    raise TypeError(f"unknown condition {condition!r}")
