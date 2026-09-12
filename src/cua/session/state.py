"""Session control: who holds the live browser, the legal transitions, and the shared state file.

The paused run and the ops CLI are separate processes, so every write is a locked compare-and-swap.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Controller = Literal["automation", "human", "nobody"]
Phase = Literal["running", "paused_for_human", "human_active", "resuming", "finished"]
Event = Literal[
    "stuck_detected",
    "take_control",
    "hand_back",
    "checkpoint_reverified",
    "reverify_failed",
    "finish",
    "abort",
    "expire",
]
Actor = Literal["automation", "operator", "system"]

CONTROLLER_FOR: dict[Phase, Controller] = {
    "running": "automation",
    "paused_for_human": "nobody",
    "human_active": "human",
    "resuming": "automation",
    "finished": "nobody",
}

TRANSITIONS: dict[tuple[Phase, Event], Phase] = {
    ("running", "stuck_detected"): "paused_for_human",
    ("running", "finish"): "finished",
    ("running", "abort"): "finished",
    ("paused_for_human", "take_control"): "human_active",
    ("paused_for_human", "abort"): "finished",
    ("paused_for_human", "expire"): "finished",
    ("human_active", "hand_back"): "resuming",
    ("human_active", "abort"): "finished",
    # The operator's CLI died or they walked away: expire instead of hanging the run forever.
    ("human_active", "expire"): "finished",
    ("resuming", "checkpoint_reverified"): "running",
    ("resuming", "reverify_failed"): "paused_for_human",
    ("resuming", "abort"): "finished",
}

# Which actor may fire which event. The operator can never resume automation without a hand-back.
ALLOWED_ACTORS: dict[Event, set[Actor]] = {
    "stuck_detected": {"automation"},
    "finish": {"automation"},
    "take_control": {"operator"},
    "hand_back": {"operator"},
    "abort": {"operator"},
    "checkpoint_reverified": {"automation"},
    "reverify_failed": {"automation"},
    "expire": {"system", "automation"},
}


class IllegalTransition(RuntimeError):
    pass


class NotInControl(RuntimeError):
    """Automation acted without control, or an operator acted on a session another one holds."""


class StaleState(RuntimeError):
    """Another process changed the state file since the caller last read it."""


class SessionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TransitionRecord(SessionModel):
    from_phase: Phase = Field(description="Phase before the event.")
    to_phase: Phase = Field(description="Phase after the event.")
    event: Event = Field(description="What happened.")
    actor: Actor = Field(description="Who fired the event.")
    operator_id: str | None = Field(default=None, description="Operator behind the event, if any.")
    at: datetime = Field(description="When, timezone-aware.")
    note: str = Field(default="", description="Free text, redacted by the caller.")


class SessionState(SessionModel):
    run_id: str = Field(description="Run this session belongs to.")
    kind: Literal["discovery", "replay"] = Field(description="Which loop owns the session.")
    phase: Phase = Field(description="Where the session is in the control state machine.")
    controller: Controller = Field(description="Who may act on the live browser right now.")
    version: int = Field(
        ge=0, description="Incremented on every transition; used for compare-and-swap."
    )
    updated_at: datetime = Field(description="Last transition time.")
    owner_pid: int | None = Field(
        default=None,
        description="Process that owns the browser, so ops can tell a paused run from a dead one.",
    )
    operator_id: str | None = Field(
        default=None,
        description="Operator holding or resuming from control. Only they may hand back or abort "
        "while human_active.",
    )
    intervention_id: str | None = Field(
        default=None,
        description="Open intervention while paused, and the last one when a pause ends the run.",
    )
    history: list[TransitionRecord] = Field(default_factory=list, description="All transitions.")

    @model_validator(mode="after")
    def _controller_matches_phase(self) -> Self:
        expected = CONTROLLER_FOR[self.phase]
        if self.controller != expected:
            raise ValueError(
                f"phase {self.phase} requires controller {expected}, got {self.controller}"
            )
        if self.phase == "human_active" and self.operator_id is None:
            raise ValueError("human_active requires the operator_id holding control")
        return self

    @classmethod
    def start(
        cls, run_id: str, kind: Literal["discovery", "replay"], owner_pid: int | None = None
    ) -> SessionState:
        return cls(
            run_id=run_id,
            kind=kind,
            phase="running",
            controller="automation",
            version=0,
            updated_at=datetime.now(UTC),
            owner_pid=owner_pid,
        )


def advance(
    state: SessionState,
    event: Event,
    actor: Actor,
    *,
    note: str = "",
    intervention_id: str | None = None,
    operator_id: str | None = None,
    now: datetime | None = None,
) -> SessionState:
    """Pure transition function. Raises IllegalTransition for anything not in the table."""
    target = TRANSITIONS.get((state.phase, event))
    if target is None:
        raise IllegalTransition(f"{event!r} is not allowed from {state.phase!r}")
    if actor not in ALLOWED_ACTORS[event]:
        raise IllegalTransition(f"{actor!r} may not fire {event!r}")
    if actor == "operator" and operator_id is None:
        raise IllegalTransition(f"operator events need an operator_id, got none for {event!r}")
    if event == "stuck_detected" and intervention_id is None:
        raise IllegalTransition("stuck_detected must open an intervention")
    if state.phase == "human_active" and actor == "operator" and operator_id != state.operator_id:
        raise NotInControl(
            f"{operator_id!r} does not hold run {state.run_id}; {state.operator_id!r} does"
        )

    at = now or datetime.now(UTC)
    record = TransitionRecord(
        from_phase=state.phase,
        to_phase=target,
        event=event,
        actor=actor,
        operator_id=operator_id,
        at=at,
        note=note,
    )
    if target == "running":
        open_intervention, holder = None, None
    else:
        open_intervention = intervention_id or state.intervention_id
        holder = operator_id if event == "take_control" else state.operator_id
    return state.model_copy(
        update={
            "phase": target,
            "controller": CONTROLLER_FOR[target],
            "version": state.version + 1,
            "updated_at": at,
            "intervention_id": open_intervention,
            "operator_id": holder,
            "history": [*state.history, record],
        }
    )


def require_automation(state: SessionState) -> None:
    if state.controller != "automation":
        raise NotInControl(
            f"run {state.run_id} is {state.phase}; controller is {state.controller}, not automation"
        )


class StateStore:
    """session_state.json plus a sibling lock file. Writes are atomic renames under an flock."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock_path = path.with_name(path.name + ".lock")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def read(self) -> SessionState:
        return SessionState.model_validate_json(self.path.read_text(encoding="utf-8"))

    def _write(self, state: SessionState) -> None:
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(state.model_dump_json(indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def create(self, state: SessionState) -> SessionState:
        with self._locked():
            if self.path.exists():
                raise FileExistsError(f"{self.path} already exists")
            self._write(state)
        return state

    def transition(
        self,
        event: Event,
        actor: Actor,
        *,
        expected_version: int | None = None,
        note: str = "",
        intervention_id: str | None = None,
        operator_id: str | None = None,
    ) -> SessionState:
        with self._locked():
            current = self.read()
            if expected_version is not None and current.version != expected_version:
                raise StaleState(
                    f"state is at version {current.version}, caller expected {expected_version}"
                )
            updated = advance(
                current,
                event,
                actor,
                note=note,
                intervention_id=intervention_id,
                operator_id=operator_id,
            )
            self._write(updated)
            return updated
