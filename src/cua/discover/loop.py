"""Discovery loop: observe, ask the model, check policy, act, settle, log, until done or stuck.

Every action goes through GatedSurface; every target becomes a verified ladder before it is used.
"""

from __future__ import annotations

import base64
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field, replace
from functools import partial
from importlib.resources import files
from typing import Any, Literal, Protocol

from cua.artifact.schema import DialogExpectation
from cua.discover.model import ModelClient, ModelError, ModelTurn, TransientModelError
from cua.discover.record import (
    DeclaredOutput,
    DiscoveryRecord,
    ObservedStep,
    SecretSpec,
    StepAction,
    beside_a_label,
    dialog_message_pattern,
)
from cua.discover.tools import (
    ClickCall,
    DeclareOutputCall,
    DoneCall,
    GiveUpCall,
    NavigateCall,
    PressKeyCall,
    ReadTextCall,
    RequestConfirmationCall,
    ScrollCall,
    SelectOptionCall,
    ToolCall,
    ToolError,
    TypeTextCall,
    parse_tool_call,
    tool_definitions,
)
from cua.evidence.writer import EvidenceWriter
from cua.locate.backend import LocatorBackend
from cua.locate.recorder import Recording, looks_like_data, record
from cua.locate.resolver import Resolved, resolve
from cua.policy.enforce import GatedSurface
from cua.policy.models import RequiresConfirmation
from cua.policy.redact import MIN_SECRET_LEN, Redactor
from cua.replay.extract import ExtractionError, parse_value
from cua.replay.result import MoneyValue
from cua.surface.base import (
    BLOCKING_EVENTS,
    A11ySnapshot,
    Action,
    ActResult,
    Click,
    Element,
    ExpectedDialog,
    Navigate,
    PressKey,
    ReadText,
    Scroll,
    SelectOption,
    SnapshotNode,
    Surface,
    SurfaceError,
    SurfaceEvent,
    TypeText,
)
from cua.vocab import SENSITIVE_FIELD_RE, RiskClass, is_single_template, template_refs

SYSTEM_PROMPT = files("cua.discover").joinpath("prompt.md").read_text(encoding="utf-8")
TOOLS = tool_definitions()
StopReason = Literal[
    "done",
    "gave_up",
    "max_steps",
    "timeout",
    "repeated_action",
    "no_progress",
    "model_error",
    "entry_failed",
]
_PARSE: dict[str, Literal["money_usd", "text", "integer", "date_iso"]] = {
    "money": "money_usd",
    "string": "text",
    "integer": "integer",
    "date": "date_iso",
}
REPEAT_LIMIT = 3
CUT_OFF = (
    "Your reply hit the token limit before a complete tool call. Keep the rationale to one "
    "sentence and call one tool."
)
NO_PROGRESS_LIMIT = 2


class DiscoverySurface(Surface, LocatorBackend, Protocol):
    """The loop records ladders as it acts, so it needs the locator backend too."""


@dataclass(frozen=True)
class DiscoveryConfig:
    goal: str
    entry_url: str
    max_steps: int = 25
    timeout_s: float = 180.0
    allow_irreversible: bool = False
    settle_quiet_ms: int = 300
    settle_timeout_ms: int = 10_000


@dataclass(frozen=True)
class Secret:
    spec: SecretSpec
    value: str


@dataclass
class DiscoveryOutcome:
    stop_reason: StopReason
    message: str
    turns: int
    duration_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    record: DiscoveryRecord | None = None


class StuckDetector:
    """Two stuck signals from the kickoff: the same call three times running, and acting twice
    running without the snapshot digest changing. Reads and declarations do not count as acting."""

    def __init__(self, repeat_limit: int = REPEAT_LIMIT, no_change_limit: int = NO_PROGRESS_LIMIT):
        self._repeat_limit = repeat_limit
        self._no_change_limit = no_change_limit
        self._calls: list[tuple[object, ...]] = []
        self._unchanged = 0

    def note_call(self, key: tuple[object, ...]) -> StopReason | None:
        self._calls.append(key)
        recent = self._calls[-self._repeat_limit :]
        if len(recent) == self._repeat_limit and all(k == key for k in recent):
            return "repeated_action"
        return None

    def note_act(self, before_digest: str, after_digest: str) -> StopReason | None:
        self._unchanged = self._unchanged + 1 if before_digest == after_digest else 0
        return "no_progress" if self._unchanged >= self._no_change_limit else None


def ask_with_one_retry(ask: Callable[[], ModelTurn], on_retry: Callable[[str], None]) -> ModelTurn:
    """A transient API error gets exactly one more try; a second one fails the run."""
    try:
        return ask()
    except TransientModelError as exc:
        on_retry(str(exc))
        return ask()


def late_event_report(events: Sequence[SurfaceEvent]) -> tuple[str | None, list[dict[str, str]]]:
    """Side effects that arrived after an act returned: what to log, and what to tell the model
    when one means the action did not do what it seemed to (a refused navigation, a dialog)."""
    logged = [{"kind": e.kind, "detail": e.detail, "url": e.url} for e in events]
    serious = [e for e in events if e.kind in BLOCKING_EVENTS or e.kind == "unexpected_dialog"]
    if not serious:
        return None, logged
    first = serious[0]
    return (
        f"After the action the page reported {first.kind}: {first.detail}. It did not take "
        "effect as intended.",
        logged,
    )


@dataclass
class _Screen:
    turn: int
    snapshot: A11ySnapshot
    png: bytes
    screenshot: str


@dataclass
class _Exchange:
    content: list[dict[str, Any]]
    tool_use_id: str | None
    text: str
    is_error: bool


@dataclass
class _Outcome:
    text: str
    is_error: bool = False
    acted: bool = False
    stop: StopReason | None = None
    step: ObservedStep | None = None
    log: dict[str, Any] = field(default_factory=dict)
    logged: str | None = None  # what evidence records instead of text, when text is page data


class DiscoveryLoop:
    def __init__(
        self,
        surface: DiscoverySurface,
        gated: GatedSurface,
        model: ModelClient,
        config: DiscoveryConfig,
        *,
        secrets: Mapping[str, Secret],
        redactor: Redactor,
        evidence: EvidenceWriter,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._surface = surface
        self._gated = gated
        self._model = model
        self._config = config
        self._secrets = dict(secrets)
        self._redactor = redactor
        self._evidence = evidence
        self._clock = clock
        self._steps: list[ObservedStep] = []
        self._outputs: list[DeclaredOutput] = []
        self._dialogs_seen: dict[tuple[object, ...], tuple[str, str]] = {}
        self._confirmation_armed = False
        self._record: DiscoveryRecord | None = None
        self._screen_name = ""
        self._last_screen: _Screen | None = None
        self._shows_sensitive_output = False

    # ---- the loop -------------------------------------------------------------------------------

    def run(self) -> DiscoveryOutcome:
        """Run to a stop. An unexpected exception still leaves its last screen in the evidence."""
        try:
            return self._run()
        except Exception as exc:
            self._evidence.event("discovery", "run_crashed", error=f"{type(exc).__name__}: {exc}")
            if self._last_screen is not None:
                with suppress(Exception):
                    self._final_evidence(self._last_screen)
            raise

    def _run(self) -> DiscoveryOutcome:
        started = self._clock()
        config = self._config
        tokens_in = tokens_out = 0
        turn = 0

        def finish(reason: StopReason, message: str) -> DiscoveryOutcome:
            elapsed = int((self._clock() - started) * 1000)
            self._evidence.event(
                "discovery",
                "run_finished",
                stop_reason=reason,
                message=self._redactor.free_text(message),
                turns=turn,
                duration_ms=elapsed,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
            )
            return DiscoveryOutcome(
                stop_reason=reason,
                message=message,
                turns=turn,
                duration_ms=elapsed,
                input_tokens=tokens_in,
                output_tokens=tokens_out,
                record=self._record if reason == "done" else None,
            )

        self._evidence.event(
            "discovery",
            "run_started",
            goal=config.goal,
            entry_url=config.entry_url,
            model=self._model.model,
            max_steps=config.max_steps,
            timeout_s=config.timeout_s,
            allow_irreversible=config.allow_irreversible,
            secrets=sorted(self._secrets),
        )
        blank = self._surface.snapshot()
        entry = self._gated.perform(Navigate(config.entry_url), declared_risk="safe")
        self._log_act("entry", entry)
        if not entry.ok:
            self._final_evidence(None)
            return finish("entry_failed", f"could not open the entry URL: {entry.message}")
        self._settle()
        screen = self._observe(0)
        self._steps.append(
            ObservedStep(
                action="navigate",
                risk="safe",
                before=blank,
                after=screen.snapshot,
                url="{{surface.entry_url}}",
            )
        )
        history: list[_Exchange] = []
        stuck = StuckDetector()
        while True:
            if turn >= config.max_steps:
                self._final_evidence(screen)
                return finish("max_steps", f"no result after {config.max_steps} turns")
            if self._clock() - started >= config.timeout_s:
                self._final_evidence(screen)
                return finish("timeout", f"no result after {config.timeout_s:.0f} s")
            turn += 1
            messages = self._messages(history, screen)
            asked = self._clock()
            try:
                reply = ask_with_one_retry(
                    partial(
                        self._model.complete, system=SYSTEM_PROMPT, tools=TOOLS, messages=messages
                    ),
                    partial(self._log_retry, turn),
                )
            except (TransientModelError, ModelError) as exc:
                self._final_evidence(screen)
                return finish("model_error", f"model call failed: {exc}")
            tokens_in += reply.input_tokens
            tokens_out += reply.output_tokens
            content = reply.content or [{"type": "text", "text": "(no reply)"}]
            model_log: dict[str, Any] = {
                "turn": turn,
                "stop_reason": reply.stop_reason,
                "input_tokens": reply.input_tokens,
                "output_tokens": reply.output_tokens,
                "duration_ms": int((self._clock() - asked) * 1000),
            }
            cut_off = reply.stop_reason == "max_tokens"
            if not reply.tool_uses:
                model_log["rationale"] = self._redactor.free_text(reply.text)
                self._evidence.event("model", "model_turn", **model_log)
                nudge = CUT_OFF if cut_off else "Call exactly one tool."
                history.append(_Exchange(content, None, nudge, True))
                continue
            use = reply.tool_uses[0]
            call = ToolError(use.name, CUT_OFF) if cut_off else parse_tool_call(use.name, use.input)
            armed = self._confirmation_armed
            acted_at = self._clock()
            repeated = stuck.note_call(self._repeat_key(call, use.name, screen.snapshot))
            if repeated is not None:
                outcome = _Outcome("Stopped: the same action three times in a row.", stop=repeated)
            elif isinstance(call, ToolError):
                outcome = _Outcome(f"Invalid {call.tool} call: {call.message}", is_error=True)
            else:
                outcome = self._execute(call, screen.snapshot)
            if armed and not isinstance(call, RequestConfirmationCall):
                # A confirmation covers exactly the next call, whatever that call was.
                self._confirmation_armed = False
            if outcome.acted:
                self._settle()
                warning, late = late_event_report(self._surface.drain_events())
                if late:
                    outcome.log["late_events"] = late
                if warning is not None:
                    outcome.is_error = True
                    outcome.text += f" {warning}"
                    if outcome.step is not None:
                        outcome.text += " The step was not recorded."
                        outcome.step = None
                after = self._observe(turn)
                if outcome.step is not None:
                    self._steps.append(replace(outcome.step, after=after.snapshot))
                    outcome.log["step_id"] = f"s{len(self._steps):02d}"
                    if outcome.step.target is not None:
                        outcome.log["rung"] = outcome.step.target.target.ladder[0].strategy
                neutral = isinstance(call, ScrollCall) or (
                    isinstance(call, PressKeyCall) and call.key == "Tab"
                )
                if outcome.stop is None and not neutral:
                    outcome.stop = stuck.note_act(screen.snapshot.digest, after.snapshot.digest)
                    if outcome.stop is not None:
                        outcome.text += " Stopped: two actions in a row changed nothing on screen."
                screen = after
            # Logged after the tool ran, so a value declared this turn is already masked here.
            self._evidence.event(
                "model",
                "model_turn",
                **model_log,
                rationale=self._redactor.free_text(reply.text),
                tool=use.name,
                arguments=self._loggable(use.input),
            )
            text = self._redactor.text(outcome.text)
            logged = outcome.text if outcome.logged is None else outcome.logged
            self._evidence.event(
                "discovery",
                "tool_result",
                turn=turn,
                tool=use.name,
                ok=not outcome.is_error,
                message=self._redactor.free_text(logged),
                duration_ms=int((self._clock() - acted_at) * 1000),
                **outcome.log,
            )
            history.append(_Exchange(content, use.id, text, outcome.is_error))
            if outcome.stop is not None:
                if outcome.stop != "done":
                    self._final_evidence(screen)
                return finish(outcome.stop, text)

    def _log_retry(self, turn: int, detail: str) -> None:
        self._evidence.event("discovery", "model_retry", turn=turn, error=detail)

    def _loggable(self, arguments: object) -> object:
        """Tool arguments for the log: free text such as a summary has its amounts masked."""
        if not isinstance(arguments, dict):
            return arguments
        return {
            key: self._redactor.free_text(value) if isinstance(value, str) else value
            for key, value in arguments.items()
        }

    # ---- model context --------------------------------------------------------------------------

    def _intro(self) -> str:
        config = self._config
        secrets = "\n".join(
            f"  {{{{secrets.{name}}}}} ({secret.spec.kind})"
            for name, secret in sorted(self._secrets.items())
        )
        irreversible = (
            "allowed only after request_confirmation"
            if config.allow_irreversible
            else "not allowed in this run; request_confirmation will be denied"
        )
        return (
            f"Goal: {config.goal}\n"
            f"Target application: {config.entry_url}\n"
            f"Credential templates:\n{secrets or '  (none)'}\n"
            f"Irreversible actions: {irreversible}\n"
            f"Turn budget: {config.max_steps}"
        )

    def _screen_blocks(self, screen: _Screen) -> list[dict[str, Any]]:
        redacted = screen.snapshot.redacted(self._redactor.text)
        frames = "\n".join(f"  {key or 'top'}: {url}" for key, url in redacted.frame_urls.items())
        text = f"Screen {screen.turn}. Frames:\n{frames}\nSnapshot:\n{redacted.render()}"
        image = base64.b64encode(screen.png).decode("ascii")
        return [
            {"type": "text", "text": text},
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": image},
            },
        ]

    def _messages(self, history: list[_Exchange], screen: _Screen) -> list[dict[str, Any]]:
        """Only the latest screen is sent in full; older ones are elided to keep turns small."""
        omitted = {"type": "text", "text": "[older screen omitted]"}
        first: list[dict[str, Any]] = [{"type": "text", "text": self._intro()}]
        first += self._screen_blocks(screen) if not history else [omitted]
        messages: list[dict[str, Any]] = [{"role": "user", "content": first}]
        for index, exchange in enumerate(history):
            messages.append({"role": "assistant", "content": exchange.content})
            latest = index == len(history) - 1
            body = [{"type": "text", "text": exchange.text}]
            body += self._screen_blocks(screen) if latest else [omitted]
            if exchange.tool_use_id is None:
                messages.append({"role": "user", "content": body})
            elif exchange.is_error:
                # An error tool_result may hold text only. The screen follows it as its own blocks
                # in the same turn, so the model still sees what the failed call left behind.
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": exchange.tool_use_id,
                                "content": [{"type": "text", "text": exchange.text}],
                                "is_error": True,
                            },
                            *(self._screen_blocks(screen) if latest else [omitted]),
                        ],
                    }
                )
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": exchange.tool_use_id,
                                "content": body,
                            }
                        ],
                    }
                )
        return messages

    # ---- perceiving -----------------------------------------------------------------------------

    def _settle(self) -> None:
        self._surface.settle(self._config.settle_quiet_ms, self._config.settle_timeout_ms)

    def _observe(self, turn: int) -> _Screen:
        try:
            snapshot = self._surface.snapshot()
        except SurfaceError:
            self._settle()
            snapshot = self._surface.snapshot()
        png = self._surface.screenshot()
        name = self._evidence.screenshot(
            f"step_{turn:02d}.png", png, page_urls=snapshot.frame_urls.values()
        )
        if self._shows_sensitive_output:
            self._evidence.flag_sensitive(name)
        self._screen_name = name
        self._evidence.event(
            "discovery",
            "observation",
            turn=turn,
            screenshot=name,
            frames=snapshot.frame_urls,
            digest=snapshot.digest[:12],
            nodes=len(snapshot.nodes),
        )
        self._last_screen = _Screen(turn=turn, snapshot=snapshot, png=png, screenshot=name)
        return self._last_screen

    def _final_evidence(self, screen: _Screen | None) -> None:
        """A stopped run keeps the accessibility snapshot it stopped on, redacted."""
        if screen is not None:
            self._evidence.snapshot(f"a11y_{screen.turn:02d}.json", screen.snapshot)

    @staticmethod
    def _signature(node: SnapshotNode) -> tuple[object, ...]:
        return (tuple(node.frame_path), node.role, node.name or node.text)

    def _repeat_key(
        self, call: ToolCall | ToolError, tool: str, snapshot: A11ySnapshot
    ) -> tuple[object, ...]:
        if isinstance(call, ToolError):
            return ("invalid", tool, call.message)
        data = call.model_dump()
        for key in ("ref", "checkpoint_ref"):
            ref = data.get(key)
            if isinstance(ref, str):
                with suppress(KeyError):
                    data[key] = self._signature(snapshot.by_ref(ref))
        return tuple(sorted((k, repr(v)) for k, v in data.items()))

    # ---- acting ---------------------------------------------------------------------------------

    def _element(self, ref: str, snapshot: A11ySnapshot) -> tuple[Element, SnapshotNode] | str:
        try:
            node = snapshot.by_ref(ref)
        except KeyError:
            return f"ref {ref} is not in the latest snapshot; use a ref from the current screen"
        try:
            return self._surface.element_for_ref(ref), node
        except SurfaceError as exc:
            return f"ref {ref} could not be used ({exc}); pick another element"

    def _recording(
        self, element: Element, node: SnapshotNode, *, sensitive: bool, volatile: bool
    ) -> Recording | str:
        try:
            return record(
                element,
                self._surface,
                sensitive=sensitive,
                volatile_text=volatile,
                role_hint=None if node.role in ("clickable", "text") else node.role,
                name_hint=node.name or None,
                redact=self._redactor.text,
            )
        except (ValueError, SurfaceError) as exc:
            return (
                f"no stable way to find this element again ({exc}). Choose an element with a "
                "visible label or text, or a control next to one."
            )

    @staticmethod
    def _ladder_log(recording: Recording) -> dict[str, Any]:
        target = recording.target
        return {
            "target": {
                "frame": target.frame_path,
                "role": target.fingerprint.role,
                "name": target.fingerprint.name,
                "kind": target.fingerprint.kind,
            },
            "ladder": [rung.strategy for rung in target.ladder],
            "weak": recording.weak,
            "dropped": list(recording.dropped),
        }

    def _log_act(self, what: str, result: ActResult) -> None:
        decision = self._gated.last_decision
        self._evidence.event(
            "policy",
            "decision",
            action=what,
            decision=decision.kind if decision is not None else "not_judged",
            risk=decision.risk if decision is not None else None,
            reason=getattr(decision, "reason", None),
            ok=result.ok,
            code=result.code,
            events=[{"kind": e.kind, "detail": e.detail, "url": e.url} for e in result.events],
        )

    def _perform(
        self, action: Action, *, declared_risk: RiskClass = "safe"
    ) -> tuple[ActResult, RiskClass]:
        confirmed = self._config.allow_irreversible and self._confirmation_armed
        result = self._gated.perform(action, declared_risk=declared_risk, confirmed=confirmed)
        self._log_act(action.kind, result)
        decision = self._gated.last_decision
        risk: RiskClass = decision.risk if decision is not None else declared_risk
        return result, risk

    def _refusal(self, result: ActResult) -> str:
        if result.code == "CONFIRMATION_REQUIRED":
            decision = self._gated.last_decision
            if isinstance(decision, RequiresConfirmation) and decision.handling == "escalate":
                return (
                    "Refused: this capability's policy sends irreversible actions to a human. Do "
                    "not look for another way; call give_up if the goal needs it."
                )
            if not self._config.allow_irreversible:
                return (
                    "Denied: irreversible actions are not allowed in this discovery run. Call "
                    "give_up if the goal needs one."
                )
            return "Refused: call request_confirmation before this irreversible action."
        if result.code == "POLICY_BLOCKED":
            return f"Blocked by policy: {result.message}"
        return f"Failed: {result.message}"

    def _execute(self, call: ToolCall, snapshot: A11ySnapshot) -> _Outcome:
        if isinstance(call, ClickCall):
            return self._click(call, snapshot)
        if isinstance(call, TypeTextCall):
            return self._type(call, snapshot)
        if isinstance(call, SelectOptionCall):
            return self._select(call, snapshot)
        if isinstance(call, PressKeyCall):
            result, risk = self._perform(PressKey(call.key))
            if not result.ok:
                return _Outcome(self._refusal(result), is_error=True, acted=True)
            step = ObservedStep("press_key", risk, snapshot, snapshot, key=call.key)
            return _Outcome(f"Pressed {call.key}.", acted=True, step=step)
        if isinstance(call, ScrollCall):
            result, _ = self._perform(Scroll(call.direction, call.amount))
            text = "Scrolled." if result.ok else self._refusal(result)
            return _Outcome(text, is_error=not result.ok, acted=True)
        if isinstance(call, NavigateCall):
            result, risk = self._perform(Navigate(call.url))
            if not result.ok:
                return _Outcome(self._refusal(result), is_error=True, acted=True)
            step = ObservedStep("navigate", risk, snapshot, snapshot, url=call.url)
            return _Outcome("Navigated.", acted=True, step=step)
        if isinstance(call, ReadTextCall):
            return self._read(call, snapshot)
        if isinstance(call, DeclareOutputCall):
            return self._declare(call, snapshot)
        if isinstance(call, RequestConfirmationCall):
            if not self._config.allow_irreversible:
                self._evidence.event("policy", "confirmation_denied", reason=call.reason)
                return _Outcome(
                    "Denied: this discovery run does not allow irreversible actions. Do not "
                    "attempt one; call give_up if the goal needs it."
                )
            self._confirmation_armed = True
            self._evidence.event("policy", "confirmation_granted", reason=call.reason)
            return _Outcome("Confirmed: the next irreversible action may proceed.")
        if isinstance(call, DoneCall):
            return self._done(call, snapshot)
        assert isinstance(call, GiveUpCall)
        return _Outcome(f"Gave up: {call.reason}", stop="gave_up")

    def _click(self, call: ClickCall, snapshot: A11ySnapshot) -> _Outcome:
        found = self._element(call.ref, snapshot)
        if isinstance(found, str):
            return _Outcome(found, is_error=True)
        element, node = found
        label = node.name or node.text
        recording = self._recording(element, node, sensitive=False, volatile=looks_like_data(label))
        if isinstance(recording, str):
            return _Outcome(recording, is_error=True)
        expected: ExpectedDialog | None = None
        declared: RiskClass = "safe"
        if call.dialog is not None:
            seen = self._dialogs_seen.get(self._signature(node))
            if seen is None:
                return _Outcome(
                    "No dialog has been reported for this element yet. Click it without dialog "
                    "first to see its message.",
                    is_error=True,
                )
            dialog_type, message = seen
            expected = ExpectedDialog(
                dialog_type="confirm" if dialog_type == "confirm" else "alert",
                message_pattern=dialog_message_pattern(message),
                response=call.dialog,
            )
            if expected.dialog_type == "confirm" and call.dialog == "accept":
                declared = "irreversible"
        result, risk = self._perform(Click(element, dialog=expected), declared_risk=declared)
        log = self._ladder_log(recording)
        if not result.ok:
            dialogs = [e for e in result.events if e.kind == "unexpected_dialog"]
            if dialogs:
                dialog_type, _, message = dialogs[0].detail.partition(": ")
                self._dialogs_seen[self._signature(node)] = (dialog_type, message)
                return _Outcome(
                    f"The click opened a native {dialog_type} dialog, which was dismissed: "
                    f"'{message}'. To answer it, click again with dialog accept or dismiss.",
                    is_error=True,
                    acted=True,
                    log=log,
                )
            return _Outcome(self._refusal(result), is_error=True, acted=True, log=log)
        dialog = (
            DialogExpectation(
                dialog_type=expected.dialog_type,
                message_pattern=expected.message_pattern,
                response=expected.response,
            )
            if expected is not None
            else None
        )
        step = ObservedStep("click", risk, snapshot, snapshot, target=recording, dialog=dialog)
        answered = f" and {expected.response}ed the dialog" if expected else ""
        return _Outcome(f"Clicked{answered}.", acted=True, step=step, log=log)

    def _type(self, call: TypeTextCall, snapshot: A11ySnapshot) -> _Outcome:
        found = self._element(call.ref, snapshot)
        if isinstance(found, str):
            return _Outcome(found, is_error=True)
        element, node = found
        refs = template_refs(call.text)
        secret: Secret | None = None
        if refs:
            if any(scope != "secrets" for scope, _ in refs) or not is_single_template(call.text):
                return _Outcome(
                    "Only a single {{secrets.name}} template may be typed, as the whole text. Type "
                    "other values literally.",
                    is_error=True,
                )
            secret = self._secrets.get(refs[0][1])
            if secret is None:
                return _Outcome(
                    f"Unknown secret {refs[0][1]}; available: {sorted(self._secrets)}",
                    is_error=True,
                )
        credential_field = bool(SENSITIVE_FIELD_RE.search(node.name or ""))
        recording = self._recording(
            element, node, sensitive=secret is not None or credential_field, volatile=False
        )
        if isinstance(recording, str):
            return _Outcome(recording, is_error=True)
        looks_sensitive = recording.target.looks_sensitive
        if looks_sensitive and secret is None:
            return _Outcome(
                "This looks like a credential field. Type the matching {{secrets.name}} template.",
                is_error=True,
            )
        if secret is not None and secret.spec.kind == "credential" and not looks_sensitive:
            return _Outcome(
                f"{{{{secrets.{secret.spec.name}}}}} is a credential and this field does not look "
                "like its field. Choose the right field.",
                is_error=True,
            )
        try:
            max_length = self._surface.describe(element).max_length
        except SurfaceError:
            max_length = None
        text = secret.value if secret is not None else call.text
        result, risk = self._perform(
            TypeText(element, text, clear_first=call.clear_first), declared_risk="reversible"
        )
        log = self._ladder_log(recording) | {"typed": call.text}
        if not result.ok:
            return _Outcome(self._refusal(result), is_error=True, acted=True, log=log)
        step = ObservedStep(
            "type_text",
            risk,
            snapshot,
            snapshot,
            target=recording,
            value=call.text,
            clear_first=call.clear_first,
            max_length=max_length,
        )
        return _Outcome(f"Typed {call.text}.", acted=True, step=step, log=log)

    def _select(self, call: SelectOptionCall, snapshot: A11ySnapshot) -> _Outcome:
        found = self._element(call.ref, snapshot)
        if isinstance(found, str):
            return _Outcome(found, is_error=True)
        element, node = found
        recording = self._recording(element, node, sensitive=False, volatile=False)
        if isinstance(recording, str):
            return _Outcome(recording, is_error=True)
        result, risk = self._perform(SelectOption(element, call.option), declared_risk="reversible")
        log = self._ladder_log(recording) | {"option": call.option}
        if not result.ok:
            return _Outcome(self._refusal(result), is_error=True, acted=True, log=log)
        action: StepAction = "select_option"
        step = ObservedStep(action, risk, snapshot, snapshot, target=recording, value=call.option)
        return _Outcome(f"Selected {call.option}.", acted=True, step=step, log=log)

    def _read_element_text(self, element: Element) -> tuple[str | None, str]:
        result, _ = self._perform(ReadText(element))
        if not result.ok:
            return None, self._refusal(result)
        return result.text or "", ""

    def _read(self, call: ReadTextCall, snapshot: A11ySnapshot) -> _Outcome:
        found = self._element(call.ref, snapshot)
        if isinstance(found, str):
            return _Outcome(found, is_error=True)
        text, problem = self._read_element_text(found[0])
        if text is None:
            return _Outcome(problem, is_error=True)
        return _Outcome(f"Text: {text}", logged=f"Read {len(text)} characters; not logged.")

    def _declare(self, call: DeclareOutputCall, snapshot: A11ySnapshot) -> _Outcome:
        if any(o.name == call.name for o in self._outputs):
            return _Outcome(f"Output {call.name} is already declared.", is_error=True)
        found = self._element(call.ref, snapshot)
        if isinstance(found, str):
            return _Outcome(found, is_error=True)
        element, node = found
        text, problem = self._read_element_text(element)
        if text is None:
            return _Outcome(problem, is_error=True)
        try:
            value = parse_value(text, _PARSE[call.type])
        except ExtractionError as exc:
            return _Outcome(
                f"That element's text does not parse as {call.type}: {exc}. Declare the element "
                "that shows only the value.",
                is_error=True,
            )
        recording = self._recording(element, node, sensitive=True, volatile=True)
        if isinstance(recording, str):
            return _Outcome(recording, is_error=True)
        self._outputs.append(DeclaredOutput(call.name, call.type, recording))
        # Outputs are recorded sensitive, so the value is masked in everything written from here on
        # (logs, the model's own later screens) and screenshots showing it are flagged.
        forms = {" ".join(text.split())}
        if isinstance(value, MoneyValue):
            forms |= {f"{abs(value.amount):,.2f}", f"{abs(value.amount):.2f}"}
        for form in forms:
            if len(form) >= MIN_SECRET_LEN:
                self._redactor.add_secret(form)
        self._evidence.flag_sensitive(self._screen_name)
        self._shows_sensitive_output = True
        return _Outcome(
            f"Declared output {call.name} ({call.type}); its text parses as {call.type}.",
            log=self._ladder_log(recording) | {"output": call.name},
        )

    def _done(self, call: DoneCall, snapshot: A11ySnapshot) -> _Outcome:
        found = self._element(call.checkpoint_ref, snapshot)
        if isinstance(found, str):
            return _Outcome(found, is_error=True)
        element, node = found
        volatile = looks_like_data(node.name or node.text) or beside_a_label(node, snapshot)
        recording = self._recording(element, node, sensitive=volatile, volatile=volatile)
        if isinstance(recording, str):
            return _Outcome(recording, is_error=True)
        for output in self._outputs:
            if not isinstance(resolve(output.target.target, self._surface), Resolved):
                return _Outcome(
                    f"Output {output.name} is not on this screen. Call done where the declared "
                    "values are visible.",
                    is_error=True,
                )
        self._record = DiscoveryRecord(
            goal=self._config.goal,
            entry_url=self._config.entry_url,
            summary=call.summary,
            steps=list(self._steps),
            outputs=list(self._outputs),
            checkpoint_target=recording,
            secrets=[s.spec for s in self._secrets.values()],
        )
        return _Outcome(
            f"Done: {call.summary}",
            stop="done",
            log=self._ladder_log(recording) | {"checkpoint": True},
        )
