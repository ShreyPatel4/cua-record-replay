"""Run recorder: turns the actions a discovery run performed into a draft capability artifact.

Waits and checkpoints come from what changed on screen; parameters are proposed for confirmation.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from cua.artifact.schema import (
    AllOf,
    AnchorRelativeRung,
    Capability,
    CapabilityMeta,
    Checkpoint,
    CheckpointWait,
    ClickStep,
    Condition,
    DialogExpectation,
    ElementPresent,
    Extraction,
    InputParam,
    LabelTextRung,
    NavigateStep,
    OutputSpec,
    PressKeyStep,
    Provenance,
    SecretRef,
    SelectOptionStep,
    SettleWait,
    Step,
    SuccessCondition,
    Target,
    TextExactRung,
    TextPresent,
    TypeTextStep,
    Wait,
    WebSurface,
)
from cua.locate.recorder import Recording, looks_like_data, normalize_prose
from cua.policy.redact import Redactor
from cua.surface.base import A11ySnapshot, SnapshotNode
from cua.surface.snapshot import VALUE_ROLES
from cua.vocab import SENSITIVE_FIELD_RE, TEMPLATE_RE, KeyName, RiskClass, template_refs

StepAction = Literal["navigate", "click", "type_text", "select_option", "press_key"]
OutputType = Literal["money", "string", "integer", "date"]
_PARSE: dict[str, Literal["money_usd", "text", "integer", "date_iso"]] = {
    "money": "money_usd",
    "string": "text",
    "integer": "integer",
    "date": "date_iso",
}
# Checkpoint text should read like a heading: short, not data, not a control's own label.
CHECKPOINT_TEXT_LEN = (3, 60)
_CONTROL_ROLES = VALUE_ROLES | {"button", "link", "clickable", "checkbox", "radio", "option"}
ENTRY_URL = "{{surface.entry_url}}"
REVIEW_NOTES = (
    "Recorded by discovery on the happy path. Each checkpoint is the text that appeared after its "
    "step plus the element the next step acts on. Outcome detectors are empty on purpose: one "
    "successful run cannot observe a not-found result, an interstitial, a slow load, an expired "
    "session, or an app error, so a reviewer adds them, and tunes timeouts, before approval."
)


class BuildError(ValueError):
    """The run cannot become a valid artifact; the message says what is missing."""


@dataclass(frozen=True)
class SecretSpec:
    name: str
    env_var: str
    kind: Literal["credential", "identity"]


@dataclass(frozen=True)
class ObservedStep:
    """One action that succeeded on the page, with the screens before and after it settled."""

    action: StepAction
    risk: RiskClass
    before: A11ySnapshot
    after: A11ySnapshot
    target: Recording | None = None
    value: str | None = None
    clear_first: bool = True
    url: str | None = None
    key: KeyName | None = None
    dialog: DialogExpectation | None = None
    max_length: int | None = None


@dataclass(frozen=True)
class DeclaredOutput:
    name: str
    type: OutputType
    target: Recording


@dataclass(frozen=True)
class DiscoveryRecord:
    goal: str
    entry_url: str
    summary: str
    steps: list[ObservedStep]
    outputs: list[DeclaredOutput]
    checkpoint_target: Recording
    secrets: list[SecretSpec] = field(default_factory=list)


@dataclass(frozen=True)
class ParamProposal:
    """A literal the model typed that the goal also names: probably a per-call input."""

    literal: str
    name: str
    pattern: str | None
    label: str
    step_indexes: tuple[int, ...]


@dataclass(frozen=True)
class ArtifactMeta:
    capability_id: str
    name: str
    policy_ref: str
    app_version_hint: str
    model: str
    run_id: str
    evidence_run: str
    created_at: datetime


# ---- labels and names ------------------------------------------------------------------------


def target_label(target: Target) -> str | None:
    """The words a human would use for the element: its name, label, text, or anchor."""
    if target.fingerprint.name:
        return normalize_prose(target.fingerprint.name)
    for rung in target.ladder:
        if isinstance(rung, LabelTextRung | TextExactRung):
            return normalize_prose(rung.label if isinstance(rung, LabelTextRung) else rung.text)
    for rung in target.ladder:
        if isinstance(rung, AnchorRelativeRung):
            return normalize_prose(rung.anchor_text)
    return None


def describe_target(target: Target) -> str:
    if target.fingerprint.name:
        return f"'{normalize_prose(target.fingerprint.name)}'"
    for rung in target.ladder:
        if isinstance(rung, LabelTextRung):
            return f"the {rung.control} labelled '{normalize_prose(rung.label)}'"
        if isinstance(rung, TextExactRung):
            return f"'{normalize_prose(rung.text)}'"
    for rung in target.ladder:
        if isinstance(rung, AnchorRelativeRung):
            return (
                f"the {rung.target_kind} {rung.direction} of '{normalize_prose(rung.anchor_text)}'"
            )
    return "the recorded element"


def _snake(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:40].strip("_")
    return slug if re.fullmatch(r"[a-z][a-z0-9_]*", slug) else ""


def _word_boundary(literal: str) -> re.Pattern[str]:
    return re.compile(rf"(?<![A-Za-z0-9]){re.escape(literal)}(?![A-Za-z0-9])", re.IGNORECASE)


def digits_pattern(literal: str, max_length: int | None) -> str | None:
    """A fixed length only when the field itself enforces it; one observed value proves nothing."""
    if not literal.isdigit():
        return None
    return f"^[0-9]{{{len(literal)}}}$" if max_length == len(literal) else "^[0-9]+$"


def propose_parameters(record: DiscoveryRecord) -> list[ParamProposal]:
    """Literals the goal names that the run typed, chose, or put in a URL. The rest stay literal.

    In URLs only segments and query values containing a digit count: words in a path are routes.
    """
    found: dict[str, ParamProposal] = {}
    used: set[str] = set()

    def propose(literal: str, label: str, base: str, pattern: str | None, index: int) -> None:
        if literal in found:
            old = found[literal]
            found[literal] = replace(old, step_indexes=(*old.step_indexes, index))
            return
        name, n = base, 2
        while name in used:
            name, n = f"{base}_{n}", n + 1
        used.add(name)
        found[literal] = ParamProposal(literal, name, pattern, label, (index,))

    def named(literal: str) -> bool:
        return bool(literal) and _word_boundary(literal).search(record.goal) is not None

    for index, step in enumerate(record.steps):
        if step.action in ("type_text", "select_option") and step.value:
            literal = step.value.strip()
            if template_refs(literal) or not named(literal):
                continue
            label = (target_label(step.target.target) if step.target else None) or "value"
            pattern = digits_pattern(literal, step.max_length)
            propose(literal, label, _snake(label) or "value", pattern, index)
        elif step.action == "navigate" and step.url and step.url != ENTRY_URL:
            parts = urlsplit(step.url)
            segments = [s for s in parts.path.split("/") if s]
            for position, segment in enumerate(segments):
                if not any(ch.isdigit() for ch in segment) or not named(segment):
                    continue
                previous = segments[position - 1] if position else ""
                noun = _snake(previous.removesuffix("s"))
                label = f"the URL segment after '{previous}'" if previous else "the URL path"
                propose(
                    segment,
                    label,
                    f"{noun}_id" if noun else "value",
                    digits_pattern(segment, None),
                    index,
                )
            for key, value in parse_qsl(parts.query):
                if any(ch.isdigit() for ch in value) and named(value):
                    label = f"the URL parameter '{key}'"
                    propose(
                        value, label, _snake(key) or "value", digits_pattern(value, None), index
                    )
    return list(found.values())


def dialog_message_pattern(message: str) -> str:
    """The exact message, anchored, with digit runs generalized so no record number is stored."""
    parts = re.split(r"([0-9]+)", message)
    return "^" + "".join("[0-9]+" if p.isdigit() else re.escape(p) for p in parts) + "$"


# ---- checkpoints -----------------------------------------------------------------------------


def _frame_labels(snapshot: A11ySnapshot, frame: list[str]) -> list[tuple[SnapshotNode, str]]:
    return [
        (node, " ".join((node.name or node.text).split()))
        for node in snapshot.nodes
        if node.frame_path == frame and (node.name or node.text)
    ]


# A value cell starts at most this far right of its label cell.
LABEL_GAP_PX = 40


def beside_a_label(node: SnapshotNode, snapshot: A11ySnapshot) -> bool:
    """Text just right of other static text in the same row: the value half of a label and value
    pair, such as a member's name next to "Name". Record-specific even when it is not digits."""
    box = node.box
    if box is None:
        return False
    for other in snapshot.nodes:
        near = other.box
        if other is node or near is None or other.frame_path != node.frame_path:
            continue
        if other.role in _CONTROL_ROLES or other.clickable or not (other.name or other.text):
            continue
        overlap = min(box.y + box.h, near.y + near.h) - max(box.y, near.y)
        gap = box.x - (near.x + near.w)
        if overlap >= 0.5 * min(box.h, near.h) and -1 <= gap <= LABEL_GAP_PX:
            return True
    return False


def _new_heading(
    before: A11ySnapshot,
    after: A11ySnapshot,
    frame: list[str],
    literals: Sequence[str],
    redactor: Redactor,
) -> str | None:
    """The first text that appeared in the frame and reads like interface, not data."""
    seen = {label for _, label in _frame_labels(before, frame)}
    low, high = CHECKPOINT_TEXT_LEN
    for node, label in _frame_labels(after, frame):
        if label in seen or node.role in _CONTROL_ROLES or not low <= len(label) <= high:
            continue
        if beside_a_label(node, after):
            continue
        if looks_like_data(label) or SENSITIVE_FIELD_RE.search(label) or "{{" in label:
            continue
        if redactor.text(label) != label or any(lit in label for lit in literals):
            continue
        return label
    return None


_AMOUNT_LITERAL = re.compile(r"[$,]|\d\.\d")


def _visible_in_frame(literal: str, snapshot: A11ySnapshot, frame: list[str]) -> bool:
    pattern = _word_boundary(literal)
    return any(pattern.search(label) for _, label in _frame_labels(snapshot, frame))


def _reformatted_on_screen(literal: str) -> bool:
    """Whether the app is likely to render this value in a shape other than the one typed.

    An amount goes through the app's own formatter: 1000.00 typed comes back as $1,000.00, and a
    checkpoint asserting the typed text would then fail on a run that did exactly what was asked.
    The value still proves nothing useful as a checkpoint, so it is left out of one.
    """
    return bool(_AMOUNT_LITERAL.search(literal))


def _relative_to_entry(url: str, entry_url: str) -> str:
    """A same-origin URL under the entry URL becomes a template, so tenants keep their own host."""
    if url.startswith(entry_url) and (
        entry_url.endswith("/") or url[len(entry_url) :][:1] in "/?#"
    ):
        return ENTRY_URL + url[len(entry_url) :]
    return url


def _changed_frame(before: A11ySnapshot, after: A11ySnapshot) -> list[str]:
    for key, url in after.frame_urls.items():
        if key and before.frame_urls.get(key) != url:
            return key.split("/")
    return []


def _checkpoint_id(text: str | None, step_id: str, taken: Mapping[str, object]) -> str:
    base = f"cp_{_snake(text)}" if text and _snake(text) else f"cp_after_{step_id}"
    name, n = base, 2
    while name in taken:
        name, n = f"{base}_{n}", n + 1
    return name


def _unique_targets(targets: Sequence[Target]) -> list[Target]:
    unique: list[Target] = []
    for target in targets:
        key = (target.ladder, target.frame_path)
        if all((t.ladder, t.frame_path) != key for t in unique):
            unique.append(target)
    return unique


# ---- the artifact ----------------------------------------------------------------------------


def build_capability(
    record: DiscoveryRecord,
    params: Sequence[ParamProposal],
    meta: ArtifactMeta,
    redactor: Redactor,
) -> Capability:
    """params are the proposals a human accepted, with their final names."""
    if not record.steps:
        raise BuildError("the run recorded no steps")
    literals = [p.literal for p in params]
    by_length = sorted(params, key=lambda p: len(p.literal), reverse=True)

    def templated(text: str) -> str:
        for param in by_length:
            text = _word_boundary(param.literal).sub(f"{{{{inputs.{param.name}}}}}", text)
        return text

    def prose(text: str) -> str:
        return normalize_prose(redactor.text(templated(text)))

    checkpoints: dict[str, Checkpoint] = {}
    waits: list[Wait] = []
    last = len(record.steps) - 1
    for index, step in enumerate(record.steps):
        step_id = f"s{index + 1:02d}"
        if step.action in ("type_text", "select_option"):
            waits.append(SettleWait(kind="settle"))
            continue
        following = record.steps[index + 1] if index < last else None
        if index == last:
            focus = (
                record.outputs[0].target if record.outputs else record.checkpoint_target
            ).target
            frame = list(focus.frame_path)
            targets = [o.target.target for o in record.outputs] + [record.checkpoint_target.target]
        elif following is not None and following.target is not None:
            frame = list(following.target.target.frame_path)
            targets = [following.target.target]
        else:
            frame = _changed_frame(step.before, step.after)
            targets = []
        changed = step.before.digest != step.after.digest
        conditions: list[Condition] = []
        heading = _new_heading(step.before, step.after, frame, literals, redactor)
        if heading and changed:
            conditions.append(TextPresent(kind="text_present", text=heading, frame_path=frame))
        if index == last:
            conditions += [
                TextPresent(kind="text_present", text=f"{{{{inputs.{p.name}}}}}", frame_path=frame)
                for p in params
                if _visible_in_frame(p.literal, step.after, frame)
                and not _reformatted_on_screen(p.literal)
            ]
        if changed or index == last:
            conditions += [
                ElementPresent(kind="element_present", target=t) for t in _unique_targets(targets)
            ]
        if not conditions:
            waits.append(SettleWait(kind="settle"))
            continue
        cp_id = _checkpoint_id(heading, step_id, checkpoints)
        shown = f"'{heading}' is showing" if heading else "the screen changed"
        if index == last:
            what = f"The goal is reached: {shown} and the values to read are on screen."
        elif targets:
            what = f"After {step_id}: {shown} and the next control is present."
        else:
            what = f"After {step_id}: {shown}."
        checkpoints[cp_id] = Checkpoint(
            description=what,
            condition=conditions[0]
            if len(conditions) == 1
            else AllOf(kind="all", conditions=conditions),
        )
        waits.append(CheckpointWait(kind="checkpoint", checkpoint=cp_id))

    final_wait = waits[-1]
    if isinstance(final_wait, CheckpointWait):
        success_checkpoint = final_wait.checkpoint
    else:
        success_checkpoint = _checkpoint_id("goal reached", f"s{last + 1:02d}", checkpoints)
        goal_targets = [o.target.target for o in record.outputs] + [record.checkpoint_target.target]
        present: list[Condition] = [
            ElementPresent(kind="element_present", target=t) for t in _unique_targets(goal_targets)
        ]
        checkpoints[success_checkpoint] = Checkpoint(
            description="The values to read and the element proving the goal are on screen.",
            condition=present[0] if len(present) == 1 else AllOf(kind="all", conditions=present),
        )

    steps: list[Step] = []
    used_secrets: set[str] = set()
    for index, (step, wait) in enumerate(zip(record.steps, waits, strict=True)):
        step_id = f"s{index + 1:02d}"
        target = step.target.target if step.target else None
        risk = step.risk
        if step.action == "navigate":
            raw = step.url or ENTRY_URL
            if raw == ENTRY_URL:
                url, description = ENTRY_URL, "Open the application entry page."
            else:
                url = _relative_to_entry(templated(raw), record.entry_url)
                path_only = re.sub(r"^[a-z][a-z0-9+.-]*://[^/]*", "", TEMPLATE_RE.sub("", url))
                if re.search(r"[0-9]{5,}", path_only):
                    raise BuildError(
                        f"step {step_id} navigates to a URL that still holds a record number; "
                        "accept that value as an input, or reach the page through the screens"
                    )
                description = f"Open {normalize_prose(redactor.text(url))}."
            steps.append(
                NavigateStep(
                    id=step_id,
                    action="navigate",
                    description=description,
                    url=url,
                    risk=risk,
                    wait_for=wait,
                )
            )
            continue
        if target is None:
            if step.action != "press_key" or step.key is None:
                raise BuildError(f"step {step_id} ({step.action}) has no recorded target")
            steps.append(
                PressKeyStep(
                    id=step_id,
                    action="press_key",
                    description=f"Press {step.key}.",
                    key=step.key,
                    risk=risk,
                    wait_for=wait,
                )
            )
            continue
        where = normalize_prose(describe_target(target))
        if step.action == "click":
            dialog = step.dialog
            if (
                dialog is not None
                and dialog.dialog_type == "confirm"
                and dialog.response == "accept"
            ):
                risk = "irreversible"
            said = f" and {dialog.response} the {dialog.dialog_type} dialog" if dialog else ""
            steps.append(
                ClickStep(
                    id=step_id,
                    action="click",
                    description=f"Click {where}{said}.",
                    target=target,
                    dialog=dialog,
                    risk=risk,
                    wait_for=wait,
                )
            )
        elif step.action == "type_text":
            raw = step.value or ""
            used_secrets |= {name for scope, name in template_refs(raw) if scope == "secrets"}
            value = raw if template_refs(raw) else templated(raw)
            shown_value = value if template_refs(value) else f"'{redactor.text(value)}'"
            steps.append(
                TypeTextStep(
                    id=step_id,
                    action="type_text",
                    description=normalize_prose(f"Enter {shown_value} into {where}."),
                    target=target,
                    value=value,
                    clear_first=step.clear_first,
                    risk=risk,
                    wait_for=wait,
                )
            )
        elif step.action == "select_option":
            label = templated(step.value or "")
            steps.append(
                SelectOptionStep(
                    id=step_id,
                    action="select_option",
                    description=normalize_prose(f"Choose '{redactor.text(label)}' in {where}."),
                    target=target,
                    option_label=label,
                    risk=risk,
                    wait_for=wait,
                )
            )
        else:
            steps.append(
                PressKeyStep(
                    id=step_id,
                    action="press_key",
                    description=f"Press {step.key} on {where}.",
                    key=step.key or "Enter",
                    target=target,
                    risk=risk,
                    wait_for=wait,
                )
            )

    specs = {s.name: s for s in record.secrets}
    missing = sorted(used_secrets - set(specs))
    if missing:
        raise BuildError(f"steps use undeclared secrets {missing}")
    inputs = [
        InputParam(
            name=p.name,
            type="string",
            description=normalize_prose(
                f"Typed into '{p.label}'."
                + (f" Discovery saw a {len(p.literal)}-digit value." if p.pattern else "")
            ),
            required=True,
            pattern=p.pattern,
            sensitive=False,
        )
        for p in params
    ]
    outputs = [
        OutputSpec(
            name=o.name,
            type=o.type,
            description=normalize_prose(
                f"Value shown by {describe_target(o.target.target)} when the flow ends."
            ),
            extract=Extraction(target=o.target.target, parse=_PARSE[o.type]),
            sensitive=True,
        )
        for o in record.outputs
    ]
    goal = record.goal
    for param in by_length:
        goal = _word_boundary(param.literal).sub(f"<{param.name}>", goal)
    try:
        return Capability(
            schema_version="1.0",
            capability=CapabilityMeta(
                id=meta.capability_id,
                version="1.0.0",
                status="draft",
                name=normalize_prose(meta.name),
                description=normalize_prose(redactor.text(goal)),
                created_at=meta.created_at,
                created_from_run_id=meta.run_id,
                policy_ref=meta.policy_ref,
            ),
            surface=WebSurface(
                kind="web",
                entry_url=record.entry_url,
                app_family=meta.capability_id.split(".")[0],
                app_version_hint=meta.app_version_hint,
            ),
            secrets=[
                SecretRef(
                    name=name,
                    kind=specs[name].kind,
                    env_var=specs[name].env_var,
                    description=f"Supplied at run time from {specs[name].env_var}; never stored.",
                )
                for name in sorted(used_secrets)
            ],
            inputs=inputs,
            outputs=outputs,
            steps=steps,
            checkpoints=checkpoints,
            outcome_detectors=[],
            success=SuccessCondition(
                checkpoint=success_checkpoint, requires_outputs=[o.name for o in record.outputs]
            ),
            tenant_overrides={},
            provenance=Provenance(
                recorded_by="discover",
                model=meta.model,
                evidence_run=meta.evidence_run,
                review_notes=REVIEW_NOTES,
            ),
        )
    except ValueError as exc:
        raise BuildError(f"the recorded run is not a valid capability: {exc}") from exc
