"""Deterministic failure injection: named faults armed via /__control or a one-shot ?inject= param.

Each armed fault has an optional remaining count (None means until cleared) and string params.
"""

from __future__ import annotations

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

# Default slow delay sits inside the 4 to 8 s band so it outlasts a naive 3 s wait but fits a
# 15 s recovery budget. Tests override it with params={"ms": "..."} to stay fast.
DEFAULT_SLOW_MS = 5000

DESCRIPTIONS: dict[str, str] = {
    "not_found": "Member search returns 'No member found' for any number.",
    "validation_error": "Sub-account create rejects the initial deposit regardless of value.",
    "interstitial": "Member detail opens with a 'System notice' modal. params: sticky=1 keeps it.",
    "slow": f"Member detail responds after ms milliseconds (default {DEFAULT_SLOW_MS}).",
    "session_expired": "Member detail kills the session and redirects to sign-in. Fires once.",
    "permission_denied": "Returns an 'Access denied' 403. params: on=create (default) or detail.",
    "app_error": "Returns an 'Internal Server Error' 500. params: on=detail|search|create.",
    "layout_drift": "Search page relabels 'Find' to 'Search' and moves it to another cell.",
}

# session_expired defaults to one shot so the "recover once" path is the default behaviour;
# arm it with times=None to exercise "recurs, so hard failure".
DEFAULT_TIMES: dict[str, int | None] = {"session_expired": 1}


class UnknownInjectionError(ValueError):
    pass


@dataclass
class ArmedInjection:
    name: str
    remaining: int | None
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class InjectionState:
    _armed: dict[str, ArmedInjection] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @staticmethod
    def validate(name: str) -> str:
        if name not in INJECTION_NAMES:
            raise UnknownInjectionError(f"unknown injection {name!r}; known: {INJECTION_NAMES}")
        return name

    def arm(
        self, name: str, times: int | None = -1, params: dict[str, str] | None = None
    ) -> ArmedInjection:
        """Arm a fault. times=-1 means use the per-injection default."""
        self.validate(name)
        remaining = DEFAULT_TIMES.get(name) if times == -1 else times
        if remaining is not None and remaining < 1:
            raise ValueError("times must be a positive integer or None")
        armed = ArmedInjection(name=name, remaining=remaining, params=dict(params or {}))
        with self._lock:
            self._armed[name] = armed
        return armed

    def clear(self, name: str) -> None:
        self.validate(name)
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
        default_at: str | None = None,
        one_shot: Mapping[str, dict[str, str]] | None = None,
    ) -> ArmedInjection | None:
        """Return the fault if it applies at this site, consuming one use of a counted fault.

        Faults with an `on` param only fire where on == at, and are not consumed elsewhere.
        `one_shot` carries faults requested by ?inject= on this request; they never touch state.
        """
        self.validate(name)

        def applies(params: Mapping[str, str]) -> bool:
            return at is None or params.get("on", default_at) == at

        if one_shot is not None and name in one_shot:
            params = one_shot[name]
            return ArmedInjection(name, 0, dict(params)) if applies(params) else None
        with self._lock:
            armed = self._armed.get(name)
            if armed is None or not applies(armed.params):
                return None
            if armed.remaining is not None:
                armed.remaining -= 1
                if armed.remaining <= 0:
                    del self._armed[name]
            return ArmedInjection(armed.name, armed.remaining, dict(armed.params))
