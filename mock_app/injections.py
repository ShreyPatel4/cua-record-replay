"""Deterministic failure injection: named faults armed via /__control or a one-shot ?inject= param.

Every fault, count, site, and param is validated at arm time, so a typo fails loudly, not silently.
"""

from __future__ import annotations

import enum
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, get_args

InjectionName = Literal[
    "not_found",
    "validation_error",
    "interstitial",
    "slow",
    "session_expired",
    "permission_denied",
    "app_error",
    "layout_drift",
]
INJECTION_NAMES: tuple[str, ...] = get_args(InjectionName)

# Inside the kickoff's 4 to 8 s band. It only outlasts a checkpoint whose per-step timeout is
# shorter than this, which is how the member-detail step exercises wait_and_retry (see CLAUDE.md).
DEFAULT_SLOW_MS = 5000
MAX_SLOW_MS = 60_000

DESCRIPTIONS: dict[str, str] = {
    "not_found": "Member search returns 'No member found' for any number.",
    "validation_error": "Sub-account create rejects the initial deposit regardless of value.",
    "interstitial": "Member detail hides behind a 'System notice' until OK. params: sticky=1.",
    "slow": f"Member detail responds after ms milliseconds (default {DEFAULT_SLOW_MS}).",
    "session_expired": "Kills the session and redirects to sign-in. params: on=detail (default) "
    "or create, which expires between the last field and the create POST. Fires once.",
    "permission_denied": "Returns an 'Access denied' 403. params: on=create (default) or detail.",
    "app_error": "Returns an 'Internal Server Error' 500. params: on=detail|search|create.",
    "layout_drift": "Search page relabels 'Find' to 'Search' and moves it one cell right.",
}

# Faults that can fire at more than one route take an `on` param naming the site.
SITES: dict[str, tuple[str, ...]] = {
    "permission_denied": ("create", "detail"),
    "app_error": ("detail", "search", "create"),
    "session_expired": ("detail", "create"),
}

# session_expired defaults to one shot so "recover once" is the default behaviour; arm it with
# times=None to exercise "recurs, so hard failure".
DEFAULT_TIMES: dict[str, int | None] = {"session_expired": 1}


class Default(enum.Enum):
    """Typed sentinel for 'use the per-injection default count'."""

    TIMES = "default"


DEFAULT = Default.TIMES


class InjectionError(ValueError):
    """Bad injection name, count, site, or param. The control surfaces map this to HTTP 400."""


@dataclass
class ArmedInjection:
    name: str
    remaining: int | None
    params: dict[str, str] = field(default_factory=dict)

    @property
    def site(self) -> str | None:
        sites = SITES.get(self.name)
        return None if sites is None else self.params.get("on", sites[0])


def validate_name(name: str) -> str:
    if name not in INJECTION_NAMES:
        raise InjectionError(f"unknown injection {name!r}; known: {', '.join(INJECTION_NAMES)}")
    return name


def validate_params(name: str, params: Mapping[str, str]) -> dict[str, str]:
    validate_name(name)
    allowed = {"slow": {"ms"}, "interstitial": {"sticky"}}.get(name, set())
    if name in SITES:
        allowed = {"on"}
    unknown = set(params) - allowed
    if unknown:
        raise InjectionError(f"{name} does not take params {sorted(unknown)}")
    clean = {k: str(v) for k, v in params.items()}
    if "on" in clean and clean["on"] not in SITES[name]:
        raise InjectionError(f"{name} on= must be one of {SITES[name]}, got {clean['on']!r}")
    if "ms" in clean and not (clean["ms"].isdigit() and 0 <= int(clean["ms"]) <= MAX_SLOW_MS):
        raise InjectionError(f"slow ms= must be an integer 0..{MAX_SLOW_MS}, got {clean['ms']!r}")
    if "sticky" in clean and clean["sticky"] not in ("0", "1"):
        raise InjectionError(f"interstitial sticky= must be 0 or 1, got {clean['sticky']!r}")
    return clean


def validate_times(name: str, times: object) -> int | None:
    if times is DEFAULT:
        return DEFAULT_TIMES.get(name)
    if times is None:
        return None
    if isinstance(times, bool) or not isinstance(times, int) or times < 1:
        raise InjectionError(f"times must be a positive integer or null, got {times!r}")
    return times


@dataclass
class InjectionState:
    _armed: dict[str, ArmedInjection] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def arm(
        self,
        name: str,
        times: int | Default | None = DEFAULT,
        params: Mapping[str, str] | None = None,
    ) -> ArmedInjection:
        clean = validate_params(name, params or {})
        armed = ArmedInjection(name, validate_times(name, times), clean)
        with self._lock:
            self._armed[name] = armed
        return armed

    def clear(self, name: str) -> None:
        validate_name(name)
        with self._lock:
            self._armed.pop(name, None)

    def reset(self) -> None:
        with self._lock:
            self._armed.clear()

    def snapshot(self) -> dict[str, ArmedInjection]:
        with self._lock:
            return {
                k: ArmedInjection(v.name, v.remaining, dict(v.params))
                for k, v in self._armed.items()
            }

    def fire(
        self,
        name: str,
        *,
        at: str | None = None,
        one_shot: Mapping[str, ArmedInjection] | None = None,
    ) -> ArmedInjection | None:
        """Return the fault if it applies at this site, consuming one use of a counted fault.

        A sited fault only fires (and is only consumed) where its site equals `at`.
        `one_shot` holds faults requested by ?inject= on this request; they never touch state.
        """
        validate_name(name)
        if one_shot is not None and name in one_shot:
            requested = one_shot[name]
            return requested if at is None or requested.site == at else None
        with self._lock:
            armed = self._armed.get(name)
            if armed is None or (at is not None and armed.site != at):
                return None
            if armed.remaining is not None:
                armed.remaining -= 1
                if armed.remaining <= 0:
                    del self._armed[name]
            return ArmedInjection(armed.name, armed.remaining, dict(armed.params))
