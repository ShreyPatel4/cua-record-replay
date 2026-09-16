"""The operator channel: what a stuck replay run does when a human has to take the live session.

It writes the intervention, waits on the shared state file, captures what the human does, and
hands the engine back a Handback (re-verify) or an escalated stop (nobody came, or they aborted).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO

from cua.evidence.writer import EvidenceWriter
from cua.policy.redact import Redactor
from cua.replay.engine import Handback, RunStopped
from cua.replay.result import HumanIntervention
from cua.session.intervention import InterventionRequest, ReasonCode
from cua.session.state import Event, SessionState, StateStore
from cua.surface.base import HumanEvent, Surface

# How often the paused run looks at the state file: fast enough that an operator never waits on us.
POLL_MS = 250
DEFAULT_WAIT_S = 300.0
# What a human can usefully do, per reason. Free text for the operator; no code branches on it.
SUGGESTIONS: dict[ReasonCode, tuple[str, ...]] = {
    "HARD_FAILURE_ESCALATE": (
        "Read the screen and clear whatever is blocking it, then hand back.",
        "If the app is broken rather than blocked, abort and the run ends as escalated.",
    ),
    "IRREVERSIBLE_NEEDS_HUMAN": (
        "Replay will not perform this step. Do it by hand if it is right, then hand back.",
        "Resuming re-checks the screen instead of repeating the step, so nothing happens twice.",
    ),
    "RECOVERY_EXHAUSTED": (
        "The automatic recovery ran out of attempts. Put the app back on the expected screen.",
    ),
    "POST_HANDOFF_CHECKPOINT_FAILED": (
        "The screen still does not match what the step expects. Read the note from last time.",
    ),
    "MAX_STEPS": ("The run hit its step budget.",),
    "TIMEOUT": ("The run hit its time budget.",),
    "REPEATED_ACTION": ("The model repeated itself. Move the app on, or abort.",),
    "NO_PROGRESS": ("Nothing on screen changed. Move the app on, or abort.",),
    "GAVE_UP": ("The model gave up and asked for a human.",),
}


def _clean(text: str) -> str:
    return " ".join(text.split())


def _to_stderr(text: str) -> None:
    """The pause banner is for the person watching, not for the caller parsing stdout."""
    print(text, file=sys.stderr, flush=True)


class OperatorChannel:
    """One run's link to a human. Passed to ReplayEngine as its escalation hook.

    The paused run never touches the browser while the human holds it: it polls session_state.json,
    which the ops CLI writes under a lock, and control comes back only through a hand-back.
    """

    def __init__(
        self,
        *,
        run_id: str,
        capability_id: str,
        evidence: EvidenceWriter,
        surface: Surface,
        redactor: Redactor,
        resume_command: Callable[[str], str],
        wait_s: float = DEFAULT_WAIT_S,
        poll_ms: int = POLL_MS,
        sleep: Callable[[int], None] | None = None,
        now: Callable[[], datetime] | None = None,
        announce: Callable[[str], None] = _to_stderr,
    ) -> None:
        self._run_id = run_id
        self._capability_id = capability_id
        self._evidence = evidence
        self._surface = surface
        self._redactor = redactor
        self._resume_command = resume_command
        self._wait_s = wait_s
        self._poll_ms = poll_ms
        self._sleep = sleep or surface.wait
        self._now = now or (lambda: datetime.now(UTC))
        self._announce = announce
        self.store = StateStore(evidence.dir / "session_state.json")
        self._pauses = 0
        self._actions = 0
        self._sink: TextIO | None = None

    # ---- the hook ----------------------------------------------------------------------------

    def __call__(self, stop: RunStopped, reason: ReasonCode) -> RunStopped | Handback:
        request = self._raise(stop, reason)
        self._wait_for_operator(request)
        state = self.store.read()
        taken_at, operator = _taker(state)
        if state.phase != "resuming":
            outcome = "aborted" if taken_at is not None else "expired"
            return self._ended(stop, request, outcome, operator=operator, taken_at=taken_at)
        assert taken_at is not None
        assert operator is not None
        return self._handback(request, reason, taken_at, operator)

    def close(self, status: str) -> None:
        """Called once the run is over, so the session file does not claim a live session."""
        if not self.store.path.exists() or self.store.read().phase == "finished":
            return
        self.store.transition("finish", "automation", note=f"run finished as {status}")

    # ---- pausing -----------------------------------------------------------------------------

    def _raise(self, stop: RunStopped, reason: ReasonCode) -> InterventionRequest:
        """Capture the screen, write the request, and move the session to paused_for_human."""
        self._pauses += 1
        screenshot, snapshot, url = self._capture(self._pauses)
        requested_at = self._now()
        request = InterventionRequest(
            intervention_id=f"iv_{self._pauses:02d}",
            run_id=self._run_id,
            kind="replay",
            capability_id=self._capability_id,
            step_id=stop.step_id or "unknown",
            reason_code=reason,
            reason_text=self._redactor.text(_clean(stop.message)),
            current_url=self._redactor.text(url),
            screenshot_path=screenshot,
            a11y_snapshot_path=snapshot,
            suggested_actions=list(SUGGESTIONS.get(reason, ())),
            resume_command=self._resume_command(self._run_id),
            requested_at=requested_at,
            expires_at=requested_at + timedelta(seconds=self._wait_s),
        )
        data = request.model_dump(mode="json")
        self._evidence.write_json("intervention.json", data)
        with (self._evidence.dir / "interventions.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps(data, ensure_ascii=False) + "\n")
        self._pause_session(request)
        self._evidence.event(
            "replay",
            "intervention_raised",
            intervention_id=request.intervention_id,
            step_id=request.step_id,
            reason_code=reason,
        )
        self._print(request, stop)
        return request

    def _pause_session(self, request: InterventionRequest) -> None:
        """First pause creates the session file. A pause after a failed re-verification is the
        brief's resuming to paused_for_human arrow, and carries the new intervention."""
        if not self.store.path.exists():
            self.store.create(SessionState.start(self._run_id, "replay", owner_pid=os.getpid()))
        resuming = self.store.read().phase == "resuming"
        event: Event = "reverify_failed" if resuming else "stuck_detected"
        self.store.transition(
            event, "automation", intervention_id=request.intervention_id, note=request.reason_text
        )

    def _capture(self, seq: int) -> tuple[str, str, str]:
        """Screenshot and accessibility snapshot for the operator to read. Both are flagged: a
        paused screen can show anything the flow had reached by then."""
        names = (f"intervention_{seq:02d}.png", f"a11y_intervention_{seq:02d}.json")
        try:
            snapshot = self._surface.snapshot()
            png = self._surface.screenshot()
            url = self._surface.frame_url([])
        except Exception as exc:  # the screen is gone; the request still has to be written
            self._evidence.event("replay", "intervention_capture_failed", error=type(exc).__name__)
            return ("", "", "")
        self._evidence.screenshot(names[0], png, page_urls=snapshot.frame_urls.values())
        self._evidence.snapshot(names[1], snapshot, mask_amounts=True)
        for name in names:
            self._evidence.flag_sensitive(name)
        return (names[0], names[1], url)

    def _print(self, request: InterventionRequest, stop: RunStopped) -> None:
        lines = [
            "",
            f"A human is needed on run {self._run_id}.",
            f"  why:     {request.reason_code} ({stop.code})",
            f"  detail:  {request.reason_text}",
            f"  step:    {request.step_id}",
            f"  screen:  {self._evidence.dir / request.screenshot_path}",
            f"  expires: {request.expires_at.isoformat(timespec='seconds')}",
        ]
        lines += [f"  try:     {s}" for s in request.suggested_actions]
        lines += ["", f"  {request.resume_command}", ""]
        self._announce("\n".join(lines))

    # ---- waiting -----------------------------------------------------------------------------

    def _wait_for_operator(self, request: InterventionRequest) -> None:
        """Poll the state file until somebody hands back, aborts, or the request expires."""
        while True:
            state = self.store.read()
            if state.phase == "human_active":
                self._begin_capture()
            elif state.phase in ("resuming", "finished"):
                self._end_capture()
                return
            elif self._now() >= request.expires_at:
                self._end_capture()
                self.store.transition("expire", "system", note="nobody took the session")
                self._evidence.event(
                    "replay", "intervention_expired", intervention_id=request.intervention_id
                )
                return
            self._sleep(self._poll_ms)

    # ---- capture -----------------------------------------------------------------------------

    def _begin_capture(self) -> None:
        if self._sink is not None:
            return
        self._sink = (self._evidence.dir / "human_actions.jsonl").open("a", encoding="utf-8")
        self._evidence.flag_sensitive("human_actions.jsonl")
        try:
            self._surface.bring_to_front()
            self._surface.start_human_capture(self._write_action)
        except Exception as exc:  # capture is evidence, not control: never fail the handoff
            self._evidence.event("operator", "capture_failed", error=type(exc).__name__)

    def _write_action(self, event: HumanEvent) -> None:
        """Redact before writing. A human types real credentials, and this file is committed."""
        if event.value and event.credential_field:
            self._redactor.add_credential(event.value)
        redact = self._redactor.free_text
        record = {
            "at": self._now().isoformat(timespec="milliseconds"),
            "event": event.kind,
            "frame_url": redact(event.frame_url),
            "target_role": event.role,
            "target_name": redact(event.name) if event.name else None,
            "target_text": redact(event.text)[:80] if event.text else None,
            "value": redact(event.value) if event.value else None,
        }
        self._actions += 1
        if self._sink is not None:
            self._sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._sink.flush()

    def _end_capture(self) -> None:
        if self._sink is None:
            return
        try:
            self._surface.stop_human_capture()
        except Exception as exc:
            self._evidence.event("operator", "capture_stop_failed", error=type(exc).__name__)
        finally:
            self._sink.close()
            self._sink = None

    # ---- endings -----------------------------------------------------------------------------

    def _handback(
        self, request: InterventionRequest, reason: ReasonCode, taken_at: datetime, operator: str
    ) -> Handback:
        note = self._redactor.text(_clean(self.store.read().history[-1].note))
        handed_back_at = self._now()

        def record(reverified: bool) -> HumanIntervention:
            return HumanIntervention(
                type="human",
                step_id=request.step_id,
                intervention_id=request.intervention_id,
                reason_code=reason,
                outcome="handed_back",
                operator_id=operator,
                operator_note=note,
                human_action_count=self._actions,
                reverified=reverified,
                taken_at=taken_at,
                handed_back_at=handed_back_at,
            )

        def confirm(ok: bool, reverified: bool) -> HumanIntervention:
            """The engine says whether the screen verified. Only a pass returns the session to
            running; a failure leaves it resuming, and the next pause carries the new request."""
            if ok:
                self.store.transition("checkpoint_reverified", "automation")
            written = record(reverified)
            self._evidence.event(
                "operator", "handback_verified", ok=ok, **written.model_dump(mode="json")
            )
            return written

        self._evidence.event(
            "operator",
            "handback_received",
            intervention_id=request.intervention_id,
            operator_id=operator,
            note=note,
            human_actions=self._actions,
        )
        return Handback(
            intervention=record(False),
            intervention_path=str(Path(self._evidence.dir) / "intervention.json"),
            confirm=confirm,
        )

    def _ended(
        self,
        stop: RunStopped,
        request: InterventionRequest,
        outcome: str,
        *,
        operator: str | None,
        taken_at: datetime | None,
    ) -> RunStopped:
        """Nobody came, or the operator ended it: an escalated stop carrying the request."""
        history = self.store.read().history
        written = HumanIntervention(
            type="human",
            step_id=request.step_id,
            intervention_id=request.intervention_id,
            reason_code=request.reason_code,
            outcome="aborted" if outcome == "aborted" else "expired",
            operator_id=operator,
            operator_note=self._redactor.text(_clean(history[-1].note)) if history else "",
            human_action_count=self._actions,
            reverified=False,
            taken_at=taken_at,
        )
        self._evidence.event("operator", "intervention_closed", **written.model_dump(mode="json"))
        said = (
            "an operator ended it"
            if outcome == "aborted"
            else f"no operator took it within {int(self._wait_s)} s"
        )
        return RunStopped(
            stop.code,
            f"{stop.message} A human was asked for ({request.reason_code}) and {said}.",
            expected=stop.expected,
            observed=stop.observed,
            status="escalated",
            intervention_path=str(Path(self._evidence.dir) / "intervention.json"),
            step_id=request.step_id,
            recoveries=(written,),
        )


def _taker(state: SessionState) -> tuple[datetime | None, str | None]:
    """When the human took this session and who they were, from the transitions themselves: a
    fast operator can take and hand back between two polls."""
    for record in reversed(state.history):
        if record.event == "take_control":
            return (record.at, record.operator_id)
    return (None, None)
