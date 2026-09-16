"""Replay engine: runs a capability's steps with no model in the loop and returns a ReplayResult.

It sees only the surface, locator, and gate contracts, so the flow never knows it runs in a browser.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, TypeVar

from cua.artifact.inputs import TypedInput, render
from cua.artifact.schema import (
    POLL_MS,
    BusinessOutcome,
    Capability,
    CheckpointTimeout,
    CheckpointWait,
    ClickRecovery,
    ClickStep,
    Condition,
    HardFailure,
    MessageSource,
    NavigateStep,
    OutcomeDetector,
    OutputSpec,
    PressKeyStep,
    Recoverable,
    RunStepsRecovery,
    SettleWait,
    Step,
    Target,
    TargetTextMessage,
    TypeTextStep,
    UrlMatches,
    WaitRetryRecovery,
    iter_conditions,
)
from cua.evidence.writer import EvidenceWriter
from cua.locate.resolver import Resolved, Unresolved, resolve
from cua.policy.enforce import GatedSurface
from cua.policy.models import RequiresConfirmation
from cua.policy.redact import Redactor
from cua.replay.conditions import ConditionSurface, describe, failing, holds, render_pattern
from cua.replay.extract import ExtractionError, parse_value
from cua.replay.result import (
    AutomaticRecovery,
    DriftWarning,
    FieldError,
    HumanIntervention,
    OutputValue,
    RecoveryRecord,
    ReplayResult,
    ReplayWarning,
    StepRecord,
    WeakTargetWarning,
)
from cua.session.intervention import ReasonCode
from cua.surface.base import (
    BLOCKING_EVENTS,
    Action,
    ActResult,
    Click,
    ExpectedDialog,
    Navigate,
    PressKey,
    ReadText,
    SelectOption,
    SurfaceError,
    TypeText,
)
from cua.vocab import RiskClass, max_risk

T = TypeVar("T")
StopStatus = Literal["business_outcome", "hard_failure", "escalated"]
# Outputs sit on the screen the success checkpoint just verified, so they get a short grace only.
OUTPUT_TIMEOUT_MS = 2000
RECOVERY_SETTLE_MS = 300
# What a re-run step can fail with that means "the recovery did not work", so the recovering
# detector's code is reported. Anything a detector recognizes, or a policy block, keeps its own.
_RECOVERY_STEP_FAILURES = frozenset(
    {"TARGET_NOT_FOUND", "TARGET_AMBIGUOUS", "CHECKPOINT_TIMEOUT", "ACTION_FAILED"}
)


class Clock(Protocol):
    def now(self) -> float:
        """Seconds on a monotonic clock."""
        ...

    def sleep(self, ms: int) -> None:
        """Let ms pass while the surface keeps handling its events."""
        ...


class SurfaceClock:
    """Real time, waiting through the surface so routes and dialogs keep being answered."""

    def __init__(self, surface: ConditionSurface) -> None:
        self._surface = surface

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, ms: int) -> None:
        self._surface.wait(ms)


class RunStopped(Exception):
    """Ends the run from anywhere inside a step; the engine turns it into the ReplayResult."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        expected: str,
        observed: str,
        status: StopStatus = "hard_failure",
        field_errors: tuple[FieldError, ...] = (),
        intervention_path: str | None = None,
        step_id: str = "",
        recoveries: tuple[RecoveryRecord, ...] = (),
    ) -> None:
        super().__init__(code)
        self.code = code
        self.message = message
        self.expected = expected
        self.observed = observed
        self.status = status
        self.field_errors = field_errors
        self.intervention_path = intervention_path
        # The step the run was on, filled in when the stop reaches an escalation handler, and what
        # that handler wants recorded: a pause nobody answered is still a recovery record.
        self.step_id = step_id
        self.recoveries = recoveries


@dataclass
class Handback:
    """An operator took the session and gave it back. The engine re-verifies the interrupted step
    instead of acting again, because the human may already have done the step by hand.

    confirm reports what re-verification found and returns the record for the result: ok says
    automation may carry on, reverified says the step's own checkpoint passed.
    """

    intervention: HumanIntervention
    intervention_path: str
    confirm: Callable[[bool, bool], HumanIntervention]


class _HandedBack(Exception):
    """Unwinds out of the step that paused, so run() can re-verify in one place."""

    def __init__(self, handback: Handback) -> None:
        super().__init__(handback.intervention.intervention_id)
        self.handback = handback


# An escalation handler gets the stop that asked for a human and why, and returns how the run
# ends: an escalated stop carrying its intervention_path, a hard failure when nobody can come, or
# a Handback when an operator fixed the screen and returned control.
Escalation = Callable[[RunStopped, ReasonCode], "RunStopped | Handback"]
# A run that keeps coming back to a human is not converging; stop asking after this many pauses.
MAX_HUMAN_PAUSES = 3


def no_operator(stop: RunStopped, reason: ReasonCode) -> RunStopped:
    """The handler when no operator channel is attached: a hard failure that keeps the escalating
    code and says a human is needed."""
    return RunStopped(
        stop.code,
        f"{stop.message} This needs a human ({reason}), but no operator channel is attached, so "
        "the run stops here.",
        expected=stop.expected,
        observed=stop.observed,
    )


@dataclass
class _OpenRecovery:
    detector: OutcomeDetector
    outcome: Recoverable
    started: float
    attempts: int = 0
    window_ends: float | None = None


@dataclass
class _StepInProgress:
    step: Step
    started: float
    attempts: int = 0
    resolved: Resolved | None = None
    # Whether the step's own action reached the app. A pause before it (an irreversible step under
    # escalate handling) and a pause after it both resume the same way, but the log says which.
    acted: bool = False


class ReplayEngine:
    """One run of one tenant-resolved capability over a gated surface.

    Every wait is a poll loop. Each tick drains surface events (a refused request or an unexpected
    dialog stops the run), evaluates the step's in-scope detectors in artifact order, then the
    wait's own condition. checkpoint_timeout detectors are evaluated once per deadline. A click or
    run_steps recovery restarts the interrupted wait with its full timeout, and owns the failure if
    that wait still never verifies. A wait_retry recovery keeps polling for its budget with its own
    trigger set aside. Attempts are counted per step and detector across all of a step's waits.
    """

    def __init__(
        self,
        capability: Capability,
        surface: ConditionSurface,
        gated: GatedSurface,
        *,
        inputs: Mapping[str, TypedInput],
        masked_inputs: Mapping[str, str],
        secrets: Mapping[str, str],
        redactor: Redactor,
        evidence: EvidenceWriter,
        run_id: str,
        started_at: datetime,
        tenant: str | None = None,
        clock: Clock | None = None,
        escalation: Escalation = no_operator,
    ) -> None:
        self._cap = capability
        self._surface = surface
        self._gated = gated
        self._inputs = dict(inputs)
        self._masked_inputs = dict(masked_inputs)
        self._secrets = dict(secrets)
        self._redactor = redactor
        self._evidence = evidence
        self._run_id = run_id
        self._started_at = started_at
        self._tenant = tenant
        self._clock = clock or SurfaceClock(surface)
        self._escalation = escalation
        entry = getattr(capability.surface, "entry_url", "")
        self._surface_values = {"entry_url": entry} if entry else {}
        self._steps_by_id = {s.id: s for s in capability.steps}
        self._index = 1
        self._reached: str | None = None
        self._current: _StepInProgress | None = None
        self._pauses = 0
        self._reverifying = False
        self._intervention_path: str | None = None
        self._records: list[StepRecord] = []
        self._recoveries: list[RecoveryRecord] = []
        self._attempts: dict[tuple[str, str], int] = {}
        self._warnings: list[ReplayWarning] = []
        self._warned: set[tuple[str, str, int]] = set()
        self._shows_sensitive = False
        self._sensitive_outputs = any(o.sensitive for o in capability.outputs)
        success_checkpoint = capability.success.checkpoint
        self._success_step = next(
            (
                s.id
                for s in capability.steps
                if isinstance(s.wait_for, CheckpointWait)
                and s.wait_for.checkpoint == success_checkpoint
            ),
            capability.steps[-1].id,
        )

    # ---- the run --------------------------------------------------------------------------------

    def run(self) -> ReplayResult:
        begin = self._clock.now()
        meta = self._cap.capability
        self._log(
            "run_started",
            capability=meta.id,
            version=meta.version,
            status=meta.status,
            tenant=self._tenant,
            inputs=self._masked_inputs,
            steps=[s.id for s in self._cap.steps],
        )
        try:
            for index, step in enumerate(self._cap.steps, start=1):
                self._step_with_handbacks(step, index)
            outputs = self._finish_with_handbacks()
        except RunStopped as stop:
            self._record_unfinished_step()
            self._capture(failed=True)
            result = self._result(begin, stop=stop)
        else:
            result = self._result(begin, outputs=outputs)
        self._log(
            "run_finished",
            status=result.status,
            outcome_code=result.outcome_code,
            step_reached=result.step_reached,
            duration_ms=result.duration_ms,
            recoveries=len(result.recoveries),
            warnings=len(result.warnings),
        )
        return result

    def _result(
        self,
        begin: float,
        *,
        stop: RunStopped | None = None,
        outputs: dict[str, OutputValue] | None = None,
    ) -> ReplayResult:
        common: dict[str, Any] = {
            "capability_id": self._cap.capability.id,
            "capability_version": self._cap.capability.version,
            "tenant": self._tenant,
            "run_id": self._run_id,
            "started_at": self._started_at,
            "duration_ms": max(0, int((self._clock.now() - begin) * 1000)),
            "inputs": self._masked_inputs,
            "step_reached": self._reached,
            "steps": self._records,
            "recoveries": self._recoveries,
            "warnings": self._warnings,
            "evidence_dir": str(self._evidence.dir),
        }
        if stop is None:
            # A finished run has only verified recoveries: an unverified one always ends the run.
            status = "recovered_then_success" if self._recoveries else "success"
            return ReplayResult(status=status, outputs=outputs or {}, **common)
        return ReplayResult(
            status=stop.status,
            outcome_code=stop.code,
            message=self._redactor.text(stop.message),
            field_errors=list(stop.field_errors),
            expected=self._redactor.text(stop.expected) or None,
            observed=self._redactor.text(stop.observed) or None,
            intervention_path=stop.intervention_path,
            **common,
        )

    # ---- steps ----------------------------------------------------------------------------------

    def _step_with_handbacks(self, step: Step, index: int) -> None:
        """Run a step, and keep re-verifying it for as long as operators hand control back.

        A hand-back never repeats the step's action: the human works on the same screen, so acting
        again could submit twice. Instead the step's in-scope detectors run again and its
        checkpoint is re-verified, exactly as the brief's resuming state says.
        """
        handed: _HandedBack | None = None
        try:
            self._run_step(step, index=index)
        except _HandedBack as paused:
            handed = paused
        while handed is not None:
            handed = self._reverify(step, handed.handback)

    def _reverify(self, step: Step, handback: Handback) -> _HandedBack | None:
        """Check the screen the operator left behind. None when the run may carry on, another
        hand-back when the check failed and an operator took the session again."""
        progress = self._current
        checkpoint = isinstance(step.wait_for, CheckpointWait)
        self._log(
            "handback_received",
            step_id=step.id,
            intervention=handback.intervention.intervention_id,
            operator=handback.intervention.operator_id,
            acted_before_pause=progress.acted if progress else True,
            reverifying=step.wait_for.kind if checkpoint else "detectors_only",
        )
        self._reverifying = True
        try:
            if checkpoint:
                self._wait(step)
            else:
                self._detectors_once(step)
        except RunStopped as stop:
            # The hand-back owns this stop; from here on a fresh escalation is allowed again.
            self._reverifying = False
            self._recoveries.append(handback.confirm(False, False))
            failed = RunStopped(
                "POST_HANDOFF_CHECKPOINT_FAILED",
                f"step {step.id} still does not verify after the hand-back: {stop.message}",
                expected=stop.expected,
                observed=stop.observed,
            )
            try:
                raise self._escalate(failed, "POST_HANDOFF_CHECKPOINT_FAILED")
            except _HandedBack as again:
                return again
        finally:
            self._reverifying = False
        self._recoveries.append(handback.confirm(True, checkpoint))
        if progress is not None:
            self._records.append(self._step_record(progress, passed=True))
            self._current = None
            self._capture(failed=False)
        return None

    def _finish_with_handbacks(self) -> dict[str, OutputValue]:
        """The success check and the outputs, with the same hand-back loop the steps get: the
        success step can escalate too, and a human may fix the screen it reads from."""
        last = self._steps_by_id[self._success_step]
        while True:
            try:
                return self._finish()
            except _HandedBack as paused:
                handed: _HandedBack | None = paused
                while handed is not None:
                    handed = self._reverify(last, handed.handback)

    def _detectors_once(self, step: Step) -> None:
        """One pass of a step's in-scope detectors, for a step whose wait cannot be run again (a
        settle that already happened, a URL that already changed)."""
        opened: dict[str, _OpenRecovery] = {}
        try:
            self._raise_on_events(step)
            detector = self._matching(step)
            if detector is not None:
                self._handle(step, detector, opened)
        except RunStopped:
            self._close(step, opened, succeeded=False)
            raise
        self._close(step, opened, succeeded=True)

    def _run_step(self, step: Step, *, index: int | None = None, skip_wait: bool = False) -> None:
        """A step from the flow (index given) or one re-run by a recovery (index None)."""
        rerun = index is None
        begin = self._clock.now()
        progress = _StepInProgress(step, begin)
        if index is not None:
            self._index, self._reached, self._current = index, step.id, progress
        if step.id == self._success_step and self._sensitive_outputs:
            self._shows_sensitive = True
        self._log(
            "step_started",
            step_id=step.id,
            action=step.action,
            description=step.description,
            rerun=rerun,
        )
        target = getattr(step, "target", None)
        timeout_ms = step.wait_for.timeout_ms
        while True:
            progress.attempts += 1
            if target is not None:
                progress.resolved = self._resolve(step, target, "step", timeout_ms)
            action = self._action(step, progress.resolved)
            risk = self._effective_risk(step, action)
            changed = progress.resolved is not None and progress.resolved.identity_changed
            if changed and risk == "irreversible":
                raise self._step_failure(
                    step,
                    "TARGET_CHANGED",
                    f"step {step.id} is irreversible and its target no longer looks like the "
                    "recorded element, so replay will not act on it",
                    expected=f"the recorded element {target.fingerprint.name or ''}".strip()
                    if target
                    else "the recorded element",
                    observed="a renamed element found by "
                    + getattr(progress.resolved, "strategy", "a fallback rung"),
                )
            act = self._perform(step.id, action, step.risk)
            if act.ok:
                progress.acted = True
                break
            # At most once for anything irreversible or dialog-answering: a failed act may still
            # have reached the server.
            retryable = risk != "irreversible" and not (
                isinstance(step, ClickStep) and step.dialog is not None
            )
            if not retryable or self._clock.now() - begin >= timeout_ms / 1000:
                raise self._step_failure(
                    step,
                    "ACTION_FAILED",
                    f"{step.action} on step {step.id} failed: {act.message}",
                    expected=step.description,
                    observed=act.message or "the surface refused the action",
                )
            self._log("act_retry", step_id=step.id, attempt=progress.attempts + 1)
            self._clock.sleep(POLL_MS)
        if not skip_wait:
            self._wait(step)
        if rerun:
            return
        self._records.append(self._step_record(progress, passed=True))
        self._current = None
        self._capture(failed=False)

    def _step_record(self, progress: _StepInProgress, *, passed: bool) -> StepRecord:
        resolved = progress.resolved
        return StepRecord(
            step_id=progress.step.id,
            action=progress.step.action,
            resolved_rung=resolved.rung_index if resolved else None,
            resolved_strategy=resolved.strategy if resolved else None,
            passed=passed,
            attempts=max(1, progress.attempts),
            duration_ms=max(0, int((self._clock.now() - progress.started) * 1000)),
        )

    def _record_unfinished_step(self) -> None:
        if self._current is not None:
            self._records.append(self._step_record(self._current, passed=False))
            self._current = None

    def _effective_risk(self, step: Step, action: Action) -> RiskClass:
        """The higher of the declared risk and the gate's, which can see page-dependent rules."""
        try:
            return max_risk(step.risk, self._gated.judge(action, declared_risk=step.risk).risk)
        except SurfaceError:
            return step.risk

    def _render(self, value: str) -> str:
        return render(
            value, inputs=self._inputs, secrets=self._secrets, surface=self._surface_values
        )

    def _action(self, step: Step, resolved: Resolved | None) -> Action:
        if isinstance(step, NavigateStep):
            return Navigate(self._render(step.url))
        if isinstance(step, PressKeyStep):
            return PressKey(step.key, resolved.element if resolved else None)
        assert resolved is not None
        if isinstance(step, ClickStep):
            dialog = step.dialog
            expected = (
                ExpectedDialog(
                    dialog.dialog_type,
                    render_pattern(dialog.message_pattern, self._inputs),
                    dialog.response,
                )
                if dialog
                else None
            )
            return Click(resolved.element, dialog=expected)
        if isinstance(step, TypeTextStep):
            return TypeText(resolved.element, self._render(step.value), step.clear_first)
        return SelectOption(resolved.element, self._render(step.option_label))

    def _perform(self, step_id: str, action: Action, risk: RiskClass) -> ActResult:
        result = self._gated.perform(action, declared_risk=risk)
        decision = self._gated.last_decision
        if decision is not None:
            self._evidence.event(
                "policy",
                "decision",
                step_id=step_id,
                action=action.kind,
                decision=decision.model_dump(mode="json"),
            )
        self._log(
            "act",
            step_id=step_id,
            action=action.kind,
            ok=result.ok,
            code=result.code,
            message=result.message,
            events=[{"kind": e.kind, "detail": e.detail} for e in result.events],
            duration_ms=result.duration_ms,
        )
        if result.ok:
            return result
        expected = f"{action.kind} allowed by policy {self._cap.capability.policy_ref}"
        if result.code == "POLICY_BLOCKED":
            raise RunStopped(
                "POLICY_BLOCKED", result.message, expected=expected, observed=result.message
            )
        if result.code == "CONFIRMATION_REQUIRED":
            stop = RunStopped(
                "CONFIRMATION_REQUIRED",
                f"{result.message}. Replay does not confirm irreversible actions on its own.",
                expected=expected,
                observed=result.message,
            )
            if isinstance(decision, RequiresConfirmation) and decision.handling == "escalate":
                raise self._escalate(stop, "IRREVERSIBLE_NEEDS_HUMAN")
            stop.message += " Pass --confirm-irreversible to allow it."
            raise stop
        if any(e.kind == "unexpected_dialog" for e in result.events):
            raise RunStopped(
                "UNEXPECTED_DIALOG",
                result.message,
                expected=f"no dialog, or the one step {step_id} declares",
                observed=result.message,
            )
        return result

    def _step_failure(
        self, step: Step, code: str, message: str, *, expected: str, observed: str
    ) -> RunStopped:
        stop = RunStopped(code, message, expected=expected, observed=observed)
        return self._escalate(stop, "HARD_FAILURE_ESCALATE") if step.on_fail == "escalate" else stop

    def _escalate(self, stop: RunStopped, reason: ReasonCode) -> RunStopped:
        """Ask for a human. Returns the stop that ends the run, or raises _HandedBack when an
        operator took the session and gave it back for the engine to re-verify."""
        if self._reverifying:
            # A stop raised while re-verifying a hand-back belongs to that hand-back: the caller
            # reports it as the post-handoff failure rather than as a fresh escalation.
            return stop
        self._log("escalation_requested", code=stop.code, reason=reason, pauses=self._pauses)
        if self._pauses >= MAX_HUMAN_PAUSES:
            self._log("escalation_capped", code=stop.code, reason=reason, pauses=self._pauses)
            return RunStopped(
                stop.code,
                f"{stop.message} The run paused for a human {self._pauses} times without "
                "finishing, so it stops rather than asking again.",
                expected=stop.expected,
                observed=stop.observed,
                status="escalated" if self._intervention_path else "hard_failure",
                intervention_path=self._intervention_path,
            )
        self._pauses += 1
        stop.step_id = stop.step_id or self._reached or ""
        outcome = self._escalation(stop, reason)
        if isinstance(outcome, Handback):
            self._intervention_path = outcome.intervention_path
            raise _HandedBack(outcome)
        self._recoveries.extend(outcome.recoveries)
        self._intervention_path = outcome.intervention_path or self._intervention_path
        return outcome

    # ---- targets --------------------------------------------------------------------------------

    def _resolve(
        self,
        step: Step,
        target: Target,
        target_ref: str,
        timeout_ms: int,
        *,
        failure_code: str | None = None,
    ) -> Resolved:
        last: list[Unresolved] = []

        def attempt() -> Resolved | None:
            try:
                found = resolve(target, self._surface)
            except SurfaceError:
                return None
            if isinstance(found, Resolved):
                return found
            last[:] = [found]
            return None

        def timed_out() -> RunStopped:
            code = failure_code or (last[0].code if last else "TARGET_NOT_FOUND")
            detail = last[0].detail if last else "the frame was not available"
            return self._step_failure(
                step,
                code,
                f"the {target_ref} target of step {step.id} did not resolve to exactly one "
                f"element within {timeout_ms} ms",
                expected=f"exactly one {target.fingerprint.kind or 'element'} in "
                f"{'/'.join(target.frame_path) or 'top'}: {target.notes}",
                observed=f"{detail}; {self._where(target.frame_path)}",
            )

        found = self._poll(step, attempt, timeout_ms=timeout_ms, on_timeout=timed_out)
        self._note(step.id, target_ref, target, found)
        self._log(
            "target_resolved",
            step_id=step.id,
            target=target_ref,
            rung=found.rung_index,
            strategy=found.strategy,
            recorded_rung=target.recorded_rung,
            drift=found.drift,
            identity_changed=found.identity_changed,
            matches=[{"strategy": a.strategy, "matches": a.matches} for a in found.attempts],
        )
        return found

    def _note(self, step_id: str, target_ref: str, target: Target, found: Resolved) -> None:
        key = (step_id, target_ref, found.rung_index)
        if key in self._warned:
            return
        if found.drift or found.identity_changed:
            self._warned.add(key)
            self._warnings.append(
                DriftWarning(
                    type="drift",
                    step_id=step_id,
                    target_ref=target_ref,
                    recorded_rung=target.recorded_rung,
                    resolved_rung=found.rung_index,
                    resolved_strategy=found.strategy,
                    identity_changed=found.identity_changed,
                )
            )
            self._log(
                "drift_signal",
                step_id=step_id,
                target=target_ref,
                recorded_rung=target.recorded_rung,
                resolved_rung=found.rung_index,
                strategy=found.strategy,
                identity_changed=found.identity_changed,
            )
        if found.fragile:
            self._warned.add(key)
            self._warnings.append(
                WeakTargetWarning(
                    type="weak_target",
                    step_id=step_id,
                    target_ref=target_ref,
                    message="resolved only by coordinates, which break on any layout change",
                )
            )

    # ---- waits ----------------------------------------------------------------------------------

    def _wait(self, step: Step) -> None:
        wait = step.wait_for
        begin = self._clock.now()
        if isinstance(wait, CheckpointWait):
            self._wait_checkpoint(step, wait.checkpoint, wait.timeout_ms)
        elif isinstance(wait, SettleWait):

            def settled() -> bool | None:
                return (
                    True if self._surface.settle(wait.quiet_ms, wait.quiet_ms + POLL_MS) else None
                )

            self._poll(
                step,
                settled,
                timeout_ms=wait.timeout_ms,
                pace=False,
                on_timeout=lambda: self._step_failure(
                    step,
                    "CHECKPOINT_TIMEOUT",
                    f"the page did not settle within {wait.timeout_ms} ms after step {step.id}",
                    expected=f"no requests in flight and a DOM quiet for {wait.quiet_ms} ms",
                    observed=f"still busy; {self._where([])}",
                ),
            )
        else:
            condition = UrlMatches(
                kind="url_matches", pattern=wait.pattern, frame_path=wait.frame_path
            )
            self._poll(
                step,
                lambda: True if holds(condition, self._surface, self._inputs) else None,
                timeout_ms=wait.timeout_ms,
                on_timeout=lambda: self._step_failure(
                    step,
                    "CHECKPOINT_TIMEOUT",
                    f"the URL did not change as step {step.id} expects within {wait.timeout_ms} ms",
                    expected=describe(condition),
                    observed=self._where(wait.frame_path),
                ),
            )
        self._log(
            "wait_passed",
            step_id=step.id,
            wait=wait.kind,
            checkpoint=getattr(wait, "checkpoint", None),
            duration_ms=max(0, int((self._clock.now() - begin) * 1000)),
        )

    def _wait_checkpoint(self, step: Step, checkpoint_id: str, timeout_ms: int) -> None:
        checkpoint = self._cap.checkpoints[checkpoint_id]

        def passes() -> bool | None:
            seen: list[tuple[Target, Resolved]] = []
            ok = holds(
                checkpoint.condition,
                self._surface,
                self._inputs,
                on_resolved=lambda target, found: seen.append((target, found)),
            )
            if not ok:
                return None
            for target, found in seen:
                self._note(step.id, f"checkpoint:{checkpoint_id}", target, found)
            return True

        self._poll(
            step,
            passes,
            timeout_ms=timeout_ms,
            checkpoint=checkpoint_id,
            on_timeout=lambda: self._step_failure(
                step,
                "CHECKPOINT_TIMEOUT",
                f"checkpoint {checkpoint_id} did not hold within {timeout_ms} ms after step "
                f"{step.id}",
                expected=f"{checkpoint_id}: {checkpoint.description}",
                observed=self._observed(checkpoint.condition),
            ),
        )

    def _poll(
        self,
        step: Step,
        check: Callable[[], T | None],
        *,
        timeout_ms: int,
        on_timeout: Callable[[], RunStopped],
        checkpoint: str | None = None,
        pace: bool = True,
    ) -> T:
        opened: dict[str, _OpenRecovery] = {}
        deadline = self._clock.now() + timeout_ms / 1000
        try:
            while True:
                self._raise_on_events(step)
                windows = [s for s in opened.values() if s.window_ends is not None]
                detector = self._matching(step, skip={s.detector.id for s in windows})
                if detector is not None:
                    if self._handle(step, detector, opened) is None:
                        deadline = self._clock.now() + timeout_ms / 1000
                    continue
                value = check()
                if value is not None:
                    self._close(step, opened, succeeded=True)
                    return value
                now = self._clock.now()
                live = [s for s in windows if s.window_ends is not None and now < s.window_ends]
                if windows and not live:
                    raise self._unverified(step, opened)
                if live:
                    pause = min(
                        min(_poll_s(s.outcome) for s in live),
                        min(s.window_ends or now for s in live) - now,
                    )
                elif now >= deadline:
                    fired = (
                        self._matching(step, timed_out=frozenset({checkpoint}), timeouts=True)
                        if checkpoint is not None
                        else None
                    )
                    if fired is None:
                        if opened:
                            raise self._unverified(step, opened)
                        raise on_timeout()
                    if self._handle(step, fired, opened) is None:
                        deadline = self._clock.now() + timeout_ms / 1000
                    continue
                else:
                    pause = min(POLL_MS / 1000, deadline - now)
                if pace:
                    self._clock.sleep(max(1, int(pause * 1000)))
        except RunStopped:
            self._close(step, opened, succeeded=False)
            raise

    def _raise_on_events(self, step: Step) -> None:
        events = self._surface.drain_events()
        if not events:
            return
        self._log(
            "surface_events",
            step_id=step.id,
            events=[{"kind": e.kind, "detail": e.detail, "url": e.url} for e in events],
        )
        blocked = [e for e in events if e.kind in BLOCKING_EVENTS]
        if blocked:
            raise RunStopped(
                "POLICY_BLOCKED",
                f"{blocked[0].kind}: {blocked[0].detail}",
                expected=f"every request inside policy {self._cap.capability.policy_ref}",
                observed=f"{blocked[0].kind} {blocked[0].url}".strip(),
            )
        dialogs = [e for e in events if e.kind == "unexpected_dialog"]
        if dialogs:
            raise RunStopped(
                "UNEXPECTED_DIALOG",
                f"an unexpected dialog was dismissed: {dialogs[0].detail}",
                expected=f"no dialog during step {step.id}",
                observed=dialogs[0].detail,
            )

    # ---- detectors and recoveries ---------------------------------------------------------------

    def _matching(
        self,
        step: Step,
        *,
        timed_out: frozenset[str] = frozenset(),
        timeouts: bool = False,
        skip: set[str] | None = None,
    ) -> OutcomeDetector | None:
        """First in-scope detector that holds. Timeout triggers only when timeouts is set."""
        for detector in self._cap.outcome_detectors:
            if detector.scope is not None and step.id not in detector.scope:
                continue
            if skip and detector.id in skip:
                continue
            on_timeout = any(
                isinstance(c, CheckpointTimeout) for c in iter_conditions(detector.when)
            )
            if on_timeout != timeouts:
                continue
            if holds(detector.when, self._surface, self._inputs, timed_out=timed_out):
                self._log(
                    "detector_matched",
                    step_id=step.id,
                    detector=detector.id,
                    outcome=detector.outcome.type,
                    code=detector.outcome.code,
                )
                return detector
        return None

    def _handle(
        self, step: Step, detector: OutcomeDetector, opened: dict[str, _OpenRecovery]
    ) -> _OpenRecovery | None:
        """Act on a matched detector. Returns the recovery when it opened a wait_retry window,
        None when it acted (click or run_steps) and the interrupted wait should restart."""
        outcome = detector.outcome
        if isinstance(outcome, BusinessOutcome):
            message = self._message(outcome.message_from) or detector.description
            errors = (
                (FieldError(field=outcome.field, message=self._redactor.text(message)),)
                if outcome.field
                else ()
            )
            raise RunStopped(
                outcome.code,
                message,
                expected="",
                observed=message,
                status="business_outcome",
                field_errors=errors,
            )
        if isinstance(outcome, HardFailure):
            stop = RunStopped(
                outcome.code,
                detector.description,
                expected=self._expected_of(step),
                observed=f"{describe(detector.when)}; {self._where(_frame_of(detector.when))}",
            )
            raise self._escalate(stop, "HARD_FAILURE_ESCALATE") if outcome.escalate else stop
        assert isinstance(outcome, Recoverable)
        recovery = outcome.recovery
        key = (step.id, detector.id)
        used = self._attempts.get(key, 0)
        state = opened.setdefault(
            detector.id, _OpenRecovery(detector, outcome, started=self._clock.now())
        )
        # One wait_retry budget per step; clicks and re-runs up to max_attempts per step.
        limit = 1 if isinstance(recovery, WaitRetryRecovery) else recovery.max_attempts
        if used >= limit:
            state.attempts = max(state.attempts, used)
            raise self._exhausted(step, state)
        self._attempts[key] = used + 1
        state.attempts += 1
        self._log(
            "recovery_started",
            step_id=step.id,
            detector=detector.id,
            kind=recovery.kind,
            attempt=used + 1,
            limit=limit,
        )
        if isinstance(recovery, WaitRetryRecovery):
            state.window_ends = self._clock.now() + recovery.max_total_ms / 1000
            return state
        if isinstance(recovery, ClickRecovery):
            self._recovery_click(step, detector, recovery)
        else:
            self._recovery_steps(step, state, recovery)
        # Let the app answer the recovery before the wait looks again, so a sign-in that is still
        # redirecting is not read as the session expiring a second time.
        self._surface.settle(RECOVERY_SETTLE_MS, step.wait_for.timeout_ms)
        return None

    def _recovery_click(
        self, step: Step, detector: OutcomeDetector, recovery: ClickRecovery
    ) -> None:
        try:
            found = resolve(recovery.target, self._surface)
        except SurfaceError:
            found = None
        if not isinstance(found, Resolved):
            self._log("recovery_target_missing", step_id=step.id, detector=detector.id)
            return
        self._note(step.id, f"recovery:{detector.id}", recovery.target, found)
        result = self._perform(step.id, Click(found.element), "safe")
        if not result.ok:
            self._log("recovery_click_failed", step_id=step.id, message=result.message)

    def _recovery_steps(self, step: Step, state: _OpenRecovery, recovery: RunStepsRecovery) -> None:
        """Re-run each step through the gate and its own wait, except the last, whose wait is the
        interrupted one restarting: the app decides where a re-run lands. A re-run step that
        cannot be done means the recovery failed, so the recovering detector's code is reported."""
        last = recovery.step_ids[-1]
        for step_id in recovery.step_ids:
            try:
                self._run_step(self._steps_by_id[step_id], skip_wait=step_id == last)
            except RunStopped as nested:
                if nested.status != "hard_failure" or nested.code not in _RECOVERY_STEP_FAILURES:
                    raise
                raise self._exhausted(
                    step, state, detail=f"Re-running {step_id} failed: {nested.message}"
                ) from nested

    def _unverified(self, step: Step, opened: dict[str, _OpenRecovery]) -> RunStopped:
        """The wait ended without verifying after recoveries: the first recovery that acted (a
        click or a re-run) owns the failure, else the last wait_retry window to close."""
        acted = [s for s in opened.values() if s.window_ends is None]
        if acted:
            return self._exhausted(step, acted[0])
        windows = [s for s in opened.values() if s.window_ends is not None]
        return self._exhausted(step, max(windows, key=lambda s: s.window_ends or 0.0))

    def _exhausted(self, step: Step, state: _OpenRecovery, detail: str = "") -> RunStopped:
        outcome = state.outcome
        self._log(
            "recovery_exhausted",
            step_id=step.id,
            detector=state.detector.id,
            attempts=state.attempts,
            on_exhausted=outcome.on_exhausted,
        )
        stop = RunStopped(
            outcome.code,
            f"{state.detector.description} The {outcome.recovery.kind} recovery did not bring the "
            f"flow back after {max(1, state.attempts)} attempt(s)."
            + (f" {detail}" if detail else ""),
            expected=self._expected_of(step),
            observed=self._observed_of(step),
        )
        if outcome.on_exhausted == "escalate":
            return self._escalate(stop, "RECOVERY_EXHAUSTED")
        return stop

    def _close(self, step: Step, opened: dict[str, _OpenRecovery], *, succeeded: bool) -> None:
        now = self._clock.now()
        for state in opened.values():
            record = AutomaticRecovery(
                type="automatic",
                step_id=step.id,
                detector_id=state.detector.id,
                code=state.outcome.code,
                recovery_kind=state.outcome.recovery.kind,
                attempts=max(1, state.attempts),
                succeeded=succeeded,
                duration_ms=max(0, int((now - state.started) * 1000)),
            )
            self._recoveries.append(record)
            self._log("recovery_finished", **record.model_dump(mode="json"))
        opened.clear()

    def _message(self, source: MessageSource | None) -> str | None:
        if source is None:
            return None
        try:
            if isinstance(source, TargetTextMessage):
                found = resolve(source.target, self._surface)
                if not isinstance(found, Resolved):
                    return None
                return self._surface.describe(found.element).own_text or None
            match = re.search(source.pattern, self._surface.frame_text(source.frame_path))
        except SurfaceError:
            return None
        return match.group(0) if match else None

    # ---- success and outputs --------------------------------------------------------------------

    def _finish(self) -> dict[str, OutputValue]:
        last = self._steps_by_id[self._success_step]
        checkpoint_id = self._cap.success.checkpoint
        self._shows_sensitive = self._shows_sensitive or self._sensitive_outputs
        self._wait_checkpoint(last, checkpoint_id, last.wait_for.timeout_ms)
        self._log("success_verified", checkpoint=checkpoint_id)
        outputs: dict[str, OutputValue] = {}
        for spec in self._cap.outputs:
            before = len(self._recoveries)
            try:
                outputs[spec.name] = self._extract(last, spec)
            except RunStopped as stop:
                failed_recovery = any(
                    isinstance(r, AutomaticRecovery) and not r.succeeded
                    for r in self._recoveries[before:]
                )
                optional_miss = (
                    spec.name not in self._cap.success.requires_outputs
                    and stop.status == "hard_failure"
                    and stop.code == "OUTPUT_EXTRACTION_FAILED"
                    and not failed_recovery
                )
                if not optional_miss:
                    raise
                self._log("output_skipped", output=spec.name, reason=stop.code)
        # Anything the page did while outputs were read still counts, e.g. a refused navigation.
        self._raise_on_events(last)
        return outputs

    def _extract(self, step: Step, spec: OutputSpec) -> OutputValue:
        target = spec.extract.target
        found = self._resolve(
            step,
            target,
            f"output:{spec.name}",
            OUTPUT_TIMEOUT_MS,
            failure_code="OUTPUT_EXTRACTION_FAILED",
        )
        read = self._perform(step.id, ReadText(found.element), "safe")
        expected = f"output {spec.name} readable as {spec.type}"
        if not read.ok or read.text is None:
            raise RunStopped(
                "OUTPUT_EXTRACTION_FAILED",
                f"could not read output {spec.name}: {read.message}",
                expected=expected,
                observed=read.message or "no text",
            )
        if spec.sensitive:
            self._redactor.add_value(read.text)
        try:
            value = parse_value(read.text, spec.extract.parse)
        except ExtractionError as exc:
            raise RunStopped(
                "OUTPUT_EXTRACTION_FAILED",
                f"output {spec.name}: {exc}",
                expected=expected,
                observed=f"text that does not parse as {spec.extract.parse}",
            ) from exc
        self._log("output_extracted", output=spec.name, type=spec.type, sensitive=spec.sensitive)
        return value

    # ---- evidence -------------------------------------------------------------------------------

    def _log(self, event: str, **fields: Any) -> None:
        self._evidence.event("replay", event, **fields)

    def _capture(self, *, failed: bool) -> None:
        """Screenshot per step, plus the accessibility snapshot on failure. Never fails the run.

        A failure keeps the step's own screenshot and adds step_NN_failure.png beside it. From
        the success step on, a screen may show a sensitive output the run has not read, so its
        files are flagged and dollar amounts in the snapshot are masked.
        """
        name = f"step_{self._index:02d}.png"
        if failed and (self._evidence.dir / name).exists():
            name = f"step_{self._index:02d}_failure.png"
        try:
            snapshot = self._surface.snapshot()
            png = self._surface.screenshot()
        except Exception as exc:
            self._log("capture_failed", step=self._index, error=type(exc).__name__)
            return
        self._evidence.screenshot(name, png, page_urls=snapshot.frame_urls.values())
        if self._shows_sensitive:
            self._evidence.flag_sensitive(name)
        if failed:
            a11y = self._evidence.snapshot(
                f"a11y_{self._index:02d}.json", snapshot, mask_amounts=self._shows_sensitive
            )
            if self._shows_sensitive:
                self._evidence.flag_sensitive(a11y)

    def _where(self, frame_path: list[str]) -> str:
        try:
            url = self._surface.frame_url(frame_path)
        except SurfaceError:
            return f"frame {'/'.join(frame_path) or 'top'} unavailable"
        return f"{'/'.join(frame_path) or 'top'} at {url}"

    def _observed(self, condition: Condition) -> str:
        try:
            lines = failing(condition, self._surface, self._inputs)
        except SurfaceError:
            lines = []
        where = self._where(_frame_of(condition))
        return "; ".join([*lines, where]) if lines else f"the condition holds now; {where}"

    def _expected_of(self, step: Step) -> str:
        wait = step.wait_for
        if isinstance(wait, CheckpointWait):
            return f"{wait.checkpoint}: {self._cap.checkpoints[wait.checkpoint].description}"
        return f"step {step.id} to take effect ({wait.kind})"

    def _observed_of(self, step: Step) -> str:
        wait = step.wait_for
        if isinstance(wait, CheckpointWait):
            return self._observed(self._cap.checkpoints[wait.checkpoint].condition)
        return self._where([])


def _poll_s(outcome: Recoverable) -> float:
    recovery = outcome.recovery
    return (recovery.poll_ms if isinstance(recovery, WaitRetryRecovery) else POLL_MS) / 1000


def _frame_of(condition: Condition) -> list[str]:
    """The first frame a condition looks at, for pointing a human at the right place."""
    for leaf in iter_conditions(condition):
        frame_path = getattr(leaf, "frame_path", None)
        if frame_path is None and hasattr(leaf, "target"):
            frame_path = leaf.target.frame_path
        if frame_path is not None:
            return list(frame_path)
    return []
