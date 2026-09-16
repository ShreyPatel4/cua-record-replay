"""The operator channel: what a stuck replay run does when a human has to take the live session.

It writes the intervention, waits on the state file, captures the human, and hands back or ends.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable, Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import urlsplit

from cua.evidence.writer import EvidenceWriter
from cua.policy.redact import REDACTED, Redactor
from cua.replay.engine import Handback, RunStopped
from cua.replay.result import HumanIntervention
from cua.session.intervention import InterventionRequest, ReasonCode
from cua.session.state import (
    Actor,
    Event,
    IllegalTransition,
    NotInControl,
    SessionState,
    StaleState,
    StateStore,
)
from cua.surface.base import HumanEvent, Surface
from cua.vocab import SENSITIVE_FIELD_RE

# How often the paused run looks at the state file: fast enough that an operator never waits on us.
POLL_MS = 250
DEFAULT_WAIT_S = 300.0
# What a human can usefully do, per reason. Free text for the operator; no code branches on it.
SUGGESTIONS: dict[ReasonCode, tuple[str, ...]] = {
    "HARD_FAILURE_ESCALATE": (
        "Read the screen and clear whatever is blocking it, then hand back.",
        "Replay will not repeat the step it paused on. If that step never happened, do it by "
        "hand before you hand back; replay only re-checks the screen.",
        "If the app is broken rather than blocked, abort and the run ends as escalated.",
    ),
    "IRREVERSIBLE_NEEDS_HUMAN": (
        "Replay will not perform this step. Do it by hand if it is right, then hand back.",
        "Resuming re-checks the screen instead of repeating the step, so nothing happens twice.",
    ),
    "RECOVERY_EXHAUSTED": (
        "The automatic recovery ran out of attempts. Put the app back on the expected screen.",
        "Replay re-checks that screen on hand-back; it does not repeat the step, so finish it "
        "by hand if it never completed.",
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
        self._holding = False
        self._capturing = False
        self._sink: TextIO | None = None

    # ---- the hook ----------------------------------------------------------------------------

    def __call__(self, stop: RunStopped, reason: ReasonCode) -> RunStopped | Handback:
        opened_at = self._now()
        request = self._raise(stop, reason)
        self._wait_for_operator(request)
        state = self.store.read()
        taken_at, operator = _taker(state, opened_at)
        if state.phase != "resuming":
            return self._ended(stop, request, state, operator=operator, taken_at=taken_at)
        assert taken_at is not None
        assert operator is not None
        return self._handback(request, reason, taken_at, operator)

    def close(self, status: str) -> None:
        """Called once the run is over, so the session file does not claim a live session."""
        self._end_capture()
        if not self.store.path.exists() or self.store.read().phase == "finished":
            return
        self._quietly("finish", "automation", note=f"run finished as {status}")

    def _quietly(self, event: Event, actor: Actor, **fields: Any) -> None:
        """A transition that must not fail the run. An operator can abort at any moment, and an
        aborted session is already over: recording that fact again is not worth a crash."""
        try:
            self.store.transition(event, actor, **fields)
        except (IllegalTransition, NotInControl, StaleState, OSError) as exc:
            self._evidence.event(
                "operator", "transition_skipped", skipped=event, reason=type(exc).__name__
            )

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
        self._evidence.append_json("interventions.jsonl", data)
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
            # The history carries operator notes, which are free text an operator wrote in a
            # hurry. Redaction masks what it knows; the flag covers what it cannot know.
            self._evidence.flag_sensitive("session_state.json")
            self._evidence.flag_sensitive("session_state.json.lock")
        current = self.store.read()
        event: Event = "reverify_failed" if current.phase == "resuming" else "stuck_detected"
        self._quietly(
            event,
            "automation",
            expected_version=current.version,
            intervention_id=request.intervention_id,
            note=request.reason_text,
        )

    def _capture(self, seq: int) -> tuple[str, str, str]:
        """Screenshot and accessibility snapshot for the operator to read. Both are flagged: a
        paused screen can show anything the flow had reached by then."""
        names = (f"intervention_{seq:02d}.png", f"a11y_intervention_{seq:02d}.json")
        try:
            snapshot = self._surface.snapshot()
            png = self._surface.screenshot()
            url = _working_url(snapshot.frame_urls) or self._surface.frame_url([])
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
        """Poll the state file until somebody hands back, aborts, or the wait runs out.

        Taking control restarts the clock: an operator who takes the session at the last second
        gets the whole window to work in, and one who then walks away still ends the run instead
        of holding the browser forever.
        """
        deadline = request.expires_at
        # The window is live and clickable from the moment the run pauses, before anyone takes
        # the session formally. Capture it from then, or what is typed in between is neither
        # recorded nor learned by the redactor.
        self._begin_capture()
        while True:
            state = self.store.read()
            if state.phase in ("resuming", "finished"):
                self._end_capture()
                return
            if state.phase == "human_active" and not self._holding:
                self._holding = True
                deadline = state.updated_at + timedelta(seconds=self._wait_s)
                self._to_front()
            if self._now() >= deadline:
                self._end_capture()
                waited = (
                    "nobody took the session"
                    if state.phase == "paused_for_human"
                    else (f"{state.operator_id} took the session and did not hand it back")
                )
                self._quietly("expire", "system", expected_version=state.version, note=waited)
                self._evidence.event(
                    "replay",
                    "intervention_expired",
                    intervention_id=request.intervention_id,
                    detail=waited,
                )
                return
            self._sleep(self._poll_ms)

    # ---- capture -----------------------------------------------------------------------------

    def _begin_capture(self) -> None:
        if self._capturing:
            return
        self._capturing = True
        try:
            self._surface.start_human_capture(self._write_action)
        except Exception as exc:  # capture is evidence, not control: never fail the handoff
            self._capturing = False
            self._evidence.event("operator", "capture_failed", error=type(exc).__name__)

    def _open_sink(self) -> TextIO:
        """The action log exists only once a human has done something, so a pause nobody answered
        does not leave an empty file in evidence."""
        if self._sink is None:
            self._sink = (self._evidence.dir / "human_actions.jsonl").open("a", encoding="utf-8")
            self._evidence.flag_sensitive("human_actions.jsonl")
        return self._sink

    def _to_front(self) -> None:
        """The operator has taken the session; put the window where they can see it."""
        try:
            self._surface.bring_to_front()
        except Exception as exc:
            self._evidence.event("operator", "bring_to_front_failed", error=type(exc).__name__)

    def _write_action(self, event: HumanEvent) -> None:
        """Redact before writing. A human types real credentials, and this file is committed."""
        if event.value and event.credential_field:
            with suppress(ValueError):  # too short to redact safely; it is masked below anyway
                self._redactor.add_credential(event.value)
        redact = self._redactor.free_text
        if not self._capturing:
            return
        credential = event.credential_field or (
            event.name is not None and bool(SENSITIVE_FIELD_RE.search(event.name))
        )
        record = {
            "at": self._now().isoformat(timespec="milliseconds"),
            "event": event.kind,
            "frame_url": redact(event.frame_url),
            "target_role": event.role,
            "target_name": redact(event.name) if event.name else None,
            "target_text": redact(event.text)[:80] if event.text else None,
            "value": REDACTED
            if (credential and event.value)
            else redact(event.value or "") or None,
        }
        self._actions += 1
        sink = self._open_sink()
        sink.write(json.dumps(record, ensure_ascii=False) + "\n")
        sink.flush()

    def _end_capture(self) -> None:
        if not self._capturing:
            return
        self._capturing = False
        try:
            self._surface.stop_human_capture()
        except Exception as exc:
            self._evidence.event("operator", "capture_stop_failed", error=type(exc).__name__)
        finally:
            if self._sink is not None:
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
                self._quietly("checkpoint_reverified", "automation")
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
        state: SessionState,
        *,
        operator: str | None,
        taken_at: datetime | None,
    ) -> RunStopped:
        """Nobody came, or the operator ended it: an escalated stop carrying the request.

        Which of the two it was comes from the transition that ended the pause, not from whether
        anyone ever held this run: an earlier pause may have had an operator who is not to blame
        for this one.
        """
        last = state.history[-1] if state.history else None
        aborted = last is not None and last.event == "abort"
        by = last.operator_id if aborted and last else None
        written = HumanIntervention(
            type="human",
            step_id=request.step_id,
            intervention_id=request.intervention_id,
            reason_code=request.reason_code,
            outcome="aborted" if aborted else "expired",
            operator_id=by or (operator if not aborted else None),
            operator_note=self._redactor.text(_clean(last.note)) if aborted and last else "",
            human_action_count=self._actions,
            reverified=False,
            taken_at=taken_at,
        )
        self._evidence.event("operator", "intervention_closed", **written.model_dump(mode="json"))
        said = (
            f"{by or 'an operator'} ended it"
            if aborted
            else f"no operator finished it within {int(self._wait_s)} s"
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


def _working_url(frame_urls: Mapping[str, str]) -> str:
    """The frame the flow got furthest in, not the frameset shell or the banner beside it.

    A legacy app splits one screen across frames, and the one an operator needs to see is the one
    that navigated deepest, so the URL with the most path segments wins.
    """

    def depth(url: str) -> tuple[int, int]:
        return (len([part for part in urlsplit(url).path.split("/") if part]), len(url))

    return max(frame_urls.values(), key=depth) if frame_urls else ""


def _taker(state: SessionState, since: datetime) -> tuple[datetime | None, str | None]:
    """When a human took this session and who they were, from the transitions themselves: a fast
    operator can take and hand back between two polls. Only this pause counts, so an operator who
    answered an earlier one is not recorded against a pause they never saw."""
    for record in reversed(state.history):
        if record.at < since:
            break
        if record.event == "take_control":
            return (record.at, record.operator_id)
    return (None, None)
