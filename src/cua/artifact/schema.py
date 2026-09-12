"""Capability artifact schema: the typed, versioned contract discovery produces and replay executes.

Models and cross-reference validation only; nothing here touches a browser, a model, or the disk.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from cua.vocab import (
    SENSITIVE_FIELD_RE,
    CheckpointId,
    DetectorId,
    FrameName,
    KeyName,
    OutcomeCode,
    ParamName,
    RiskClass,
    StepId,
    TenantId,
    is_single_template,
    template_refs,
)

SCHEMA_VERSION = "1.0"


class Strict(BaseModel):
    """Base for every artifact model: unknown fields are errors and instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _compiles(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regex {value!r}: {exc}") from exc
    return value


Regex = Annotated[str, AfterValidator(_compiles)]
FramePath = Annotated[
    list[FrameName],
    Field(
        description="Frame names from the top document down. [] is the top document; #N is an "
        "unnamed frame by index."
    ),
]


# ---- targeting: the locator ladder ------------------------------------------------------------


class RoleNameRung(Strict):
    strategy: Literal["role_name"] = Field(description="Accessible role plus accessible name.")
    role: str = Field(min_length=1, description="ARIA role as the accessibility tree reports it.")
    name: str = Field(
        min_length=1, description="Accessible name, whitespace-normalized exact match."
    )
    confidence: float = Field(
        ge=0, le=1, description="Recorder's estimate this rung alone is unique."
    )


class LabelTextRung(Strict):
    strategy: Literal["label_text"] = Field(
        description="A form control found through its visible label."
    )
    label: str = Field(
        min_length=1, description="Visible label text, whitespace-normalized exact match."
    )
    relation: Literal["wrapped", "same_row", "below"] = Field(
        description="Where the control sits relative to the label: inside it, later in the same "
        "table row, or directly below."
    )
    control: Literal["textbox", "combobox", "checkbox"] = Field(
        description="Kind of control expected at that position."
    )
    confidence: float = Field(
        ge=0, le=1, description="Recorder's estimate this rung alone is unique."
    )


class TextExactRung(Strict):
    strategy: Literal["text_exact"] = Field(
        description="An element whose own visible text matches."
    )
    text: str = Field(min_length=1, description="Visible text; whitespace and case normalized.")
    role: str | None = Field(default=None, description="Optional role filter, e.g. cell or link.")
    confidence: float = Field(
        ge=0, le=1, description="Recorder's estimate this rung alone is unique."
    )


class AnchorRelativeRung(Strict):
    strategy: Literal["anchor_relative"] = Field(
        description="An element located by direction from a stable text anchor."
    )
    anchor_text: str = Field(min_length=1, description="Exact visible text of the anchor element.")
    direction: Literal["right", "left", "below", "above"] = Field(
        description="Direction from the anchor to search."
    )
    same_row: bool = Field(
        description="Only search the anchor's table row (or visual row on non-table UIs)."
    )
    target_kind: Literal["input", "select", "clickable", "text"] = Field(
        description="What to pick in that direction: a text input, a select, something clickable, "
        "or a non-empty text node."
    )
    nth: int = Field(
        default=1, ge=1, description="Pick the nth matching element in that direction."
    )
    confidence: float = Field(
        ge=0, le=1, description="Recorder's estimate this rung alone is unique."
    )


class BBoxRung(Strict):
    strategy: Literal["bbox"] = Field(
        description="Last resort: a box normalized to the frame viewport."
    )
    x: float = Field(ge=0, le=1, description="Left edge as a fraction of frame width.")
    y: float = Field(ge=0, le=1, description="Top edge as a fraction of frame height.")
    w: float = Field(gt=0, le=1, description="Width as a fraction of frame width.")
    h: float = Field(gt=0, le=1, description="Height as a fraction of frame height.")
    fragile: Literal[True] = Field(
        description="Always true: coordinates break on any layout change."
    )
    confidence: float = Field(
        ge=0, le=1, description="Recorder's estimate this rung alone is unique."
    )


Rung = Annotated[
    RoleNameRung | LabelTextRung | TextExactRung | AnchorRelativeRung | BBoxRung,
    Field(discriminator="strategy"),
]
RUNG_RANK: dict[str, int] = {
    "role_name": 0,
    "label_text": 1,
    "text_exact": 2,
    "anchor_relative": 3,
    "bbox": 4,
}


class Fingerprint(Strict):
    """What the element was at record time, so a reviewer and the resolver can sanity check it."""

    role: str | None = Field(default=None, description="Role seen at record time.")
    name: str | None = Field(default=None, description="Accessible name seen at record time.")
    text: str | None = Field(
        default=None, max_length=80, description="Visible text seen at record time, redacted."
    )


class Target(Strict):
    ladder: list[Rung] = Field(
        min_length=1,
        description="Rungs tried in order; a rung wins only when exactly one element matches.",
    )
    recorded_rung: int = Field(
        ge=0,
        description="Index of the rung that resolved at record time; a later rung winning on "
        "replay is a drift signal.",
    )
    frame_path: FramePath
    fingerprint: Fingerprint = Field(description="Identity of the element at record time.")
    notes: str = Field(
        min_length=1, description="Why this ladder is ordered this way and how robust it is."
    )

    @model_validator(mode="after")
    def _ladder_shape(self) -> Self:
        if self.recorded_rung >= len(self.ladder):
            raise ValueError(
                f"recorded_rung {self.recorded_rung} is outside a {len(self.ladder)}-rung ladder"
            )
        ranks = [RUNG_RANK[r.strategy] for r in self.ladder]
        if ranks != sorted(set(ranks)):
            raise ValueError(
                "ladder rungs must be unique and ordered role_name, label_text, text_exact, "
                "anchor_relative, bbox"
            )
        if all(r.strategy == "bbox" for r in self.ladder):
            raise ValueError("a target needs at least one rung that is not raw coordinates")
        return self

    @property
    def looks_sensitive(self) -> bool:
        labels = [r.label for r in self.ladder if isinstance(r, LabelTextRung)]
        names = [r.name for r in self.ladder if isinstance(r, RoleNameRung)]
        seen = [self.fingerprint.name or "", *labels, *names]
        return any(SENSITIVE_FIELD_RE.search(s) for s in seen)


# ---- conditions: checkpoints and detector triggers --------------------------------------------


class TextPresent(Strict):
    kind: Literal["text_present"] = Field(description="Visible text appears in the frame.")
    text: str = Field(min_length=1, description="Substring of the frame's visible text.")
    frame_path: FramePath = []


class ElementPresent(Strict):
    kind: Literal["element_present"] = Field(
        description="A target resolves to one visible element."
    )
    target: Target = Field(description="The element that must be present and visible.")


class UrlMatches(Strict):
    kind: Literal["url_matches"] = Field(description="The frame's current URL matches a regex.")
    pattern: Regex = Field(
        description="Regex searched against the frame URL (not the top page URL)."
    )
    frame_path: FramePath = []


class StatusCode(Strict):
    kind: Literal["status_code"] = Field(
        description="The frame's last document response had a status."
    )
    code: int = Field(
        ge=100, le=599, description="HTTP status of the last navigation in the frame."
    )
    frame_path: FramePath = []


class CheckpointTimeout(Strict):
    kind: Literal["checkpoint_timeout"] = Field(
        description="A checkpoint did not pass within its wait. Only valid in detector triggers."
    )
    checkpoint: CheckpointId = Field(description="The checkpoint that timed out.")


class AllOf(Strict):
    kind: Literal["all"] = Field(description="Every nested condition holds.")
    conditions: list[Condition] = Field(min_length=1, description="Conditions combined with AND.")


class AnyOf(Strict):
    kind: Literal["any"] = Field(description="At least one nested condition holds.")
    conditions: list[Condition] = Field(min_length=1, description="Conditions combined with OR.")


Condition = Annotated[
    TextPresent | ElementPresent | UrlMatches | StatusCode | CheckpointTimeout | AllOf | AnyOf,
    Field(discriminator="kind"),
]


def iter_conditions(condition: Condition) -> Iterator[Condition]:
    yield condition
    if isinstance(condition, AllOf | AnyOf):
        for nested in condition.conditions:
            yield from iter_conditions(nested)


class Checkpoint(Strict):
    description: str = Field(
        min_length=1, description="The state this checkpoint proves, in words."
    )
    condition: Condition = Field(description="What must hold for the checkpoint to pass.")


# ---- waits and steps --------------------------------------------------------------------------


class CheckpointWait(Strict):
    kind: Literal["checkpoint"] = Field(description="Wait until a named checkpoint passes.")
    checkpoint: CheckpointId = Field(description="Checkpoint to wait for.")
    timeout_ms: int = Field(
        default=10_000, ge=100, le=120_000, description="Give up and consult detectors after this."
    )


class SettleWait(Strict):
    kind: Literal["settle"] = Field(description="Wait for network idle plus a quiet DOM.")
    quiet_ms: int = Field(default=300, ge=50, le=5_000, description="DOM must be quiet this long.")
    timeout_ms: int = Field(
        default=10_000, ge=100, le=120_000, description="Upper bound on waiting."
    )


class UrlChangeWait(Strict):
    kind: Literal["url_change"] = Field(description="Wait until the frame URL matches a regex.")
    pattern: Regex = Field(description="Regex searched against the frame URL.")
    frame_path: FramePath = []
    timeout_ms: int = Field(
        default=10_000, ge=100, le=120_000, description="Upper bound on waiting."
    )


Wait = Annotated[CheckpointWait | SettleWait | UrlChangeWait, Field(discriminator="kind")]

StepDescription = Annotated[
    str, Field(min_length=1, description="What this step does, for a human reviewer.")
]
StepRisk = Annotated[
    RiskClass,
    Field(
        description="Declared risk. The policy gate recomputes risk at run time and uses the "
        "higher of the two."
    ),
]
StepWait = Annotated[
    Wait, Field(description="How replay knows the step took effect. No fixed sleeps.")
]
StepOnFail = Annotated[
    Literal["hard_failure", "escalate"],
    Field(description="What an unrecoverable failure at this step becomes."),
]
StepTarget = Annotated[Target, Field(description="The element this step acts on.")]


class DialogExpectation(Strict):
    """A native dialog the step is expected to open. Unexpected dialogs always fail the step."""

    dialog_type: Literal["confirm", "alert"] = Field(description="Native dialog type.")
    message_pattern: Regex = Field(description="Regex the dialog message must match.")
    response: Literal["accept", "dismiss"] = Field(description="How replay answers the dialog.")


class NavigateStep(Strict):
    id: StepId = Field(description="Stable step id, unique in the artifact.")
    action: Literal["navigate"] = Field(description="Load a URL in the top document.")
    description: StepDescription
    url: str = Field(
        min_length=1, description="URL or template; may use {{surface.*}} and non-sensitive inputs."
    )
    risk: StepRisk
    wait_for: StepWait
    on_fail: StepOnFail = "hard_failure"


class ClickStep(Strict):
    id: StepId = Field(description="Stable step id, unique in the artifact.")
    action: Literal["click"] = Field(description="Click an element.")
    description: StepDescription
    target: StepTarget
    dialog: DialogExpectation | None = Field(
        default=None, description="Native dialog this click opens, and how to answer it."
    )
    risk: StepRisk
    wait_for: StepWait
    on_fail: StepOnFail = "hard_failure"


class TypeTextStep(Strict):
    id: StepId = Field(description="Stable step id, unique in the artifact.")
    action: Literal["type_text"] = Field(description="Type into a text control.")
    description: StepDescription
    target: StepTarget
    value: str = Field(
        description="Literal text or a template. Credential fields must be a single "
        "{{secrets.*}} or sensitive {{inputs.*}} template, never a literal."
    )
    clear_first: bool = Field(default=True, description="Clear the control before typing.")
    risk: StepRisk
    wait_for: StepWait
    on_fail: StepOnFail = "hard_failure"


class SelectOptionStep(Strict):
    id: StepId = Field(description="Stable step id, unique in the artifact.")
    action: Literal["select_option"] = Field(description="Choose an option in a select control.")
    description: StepDescription
    target: StepTarget
    option_label: str = Field(
        min_length=1, description="Visible option label, literal or template."
    )
    risk: StepRisk
    wait_for: StepWait
    on_fail: StepOnFail = "hard_failure"


class PressKeyStep(Strict):
    id: StepId = Field(description="Stable step id, unique in the artifact.")
    action: Literal["press_key"] = Field(description="Press a key, optionally on a focused target.")
    description: StepDescription
    key: KeyName = Field(description="Key to press.")
    target: Target | None = Field(default=None, description="Element to focus first, if any.")
    risk: StepRisk
    wait_for: StepWait
    on_fail: StepOnFail = "hard_failure"


Step = Annotated[
    NavigateStep | ClickStep | TypeTextStep | SelectOptionStep | PressKeyStep,
    Field(discriminator="action"),
]


# ---- outcome detectors ------------------------------------------------------------------------


class TextPatternMessage(Strict):
    kind: Literal["text_pattern"] = Field(
        description="First regex match in the frame's visible text."
    )
    pattern: Regex = Field(description="Regex searched against the frame's visible text.")
    frame_path: FramePath = []


class TargetTextMessage(Strict):
    kind: Literal["target_text"] = Field(description="Visible text of a target element.")
    target: Target = Field(description="Element whose text becomes the message.")


MessageSource = Annotated[TextPatternMessage | TargetTextMessage, Field(discriminator="kind")]


class ClickRecovery(Strict):
    kind: Literal["click"] = Field(
        description="Click something, e.g. dismiss a known interstitial."
    )
    target: Target = Field(description="Element to click.")
    max_attempts: int = Field(default=2, ge=1, le=5, description="Attempts before giving up.")


class WaitRetryRecovery(Strict):
    kind: Literal["wait_retry"] = Field(description="Keep re-checking a slow checkpoint.")
    max_total_ms: int = Field(ge=100, le=120_000, description="Total extra time allowed.")
    poll_ms: int = Field(default=500, ge=50, le=10_000, description="Interval between re-checks.")


class RunStepsRecovery(Strict):
    kind: Literal["run_steps"] = Field(
        description="Re-run earlier steps' actions (e.g. sign in again), then re-verify the "
        "interrupted step's checkpoint."
    )
    step_ids: list[StepId] = Field(min_length=1, description="Steps to re-run, in order.")
    max_attempts: int = Field(default=1, ge=1, le=3, description="Attempts before giving up.")


Recovery = Annotated[
    ClickRecovery | WaitRetryRecovery | RunStepsRecovery, Field(discriminator="kind")
]
Code = Annotated[
    OutcomeCode, Field(description="Stable machine-readable code returned to the caller.")
]


class BusinessOutcome(Strict):
    type: Literal["business_outcome"] = Field(
        description="A legitimate answer the caller needs, not a failure. Exit code 0."
    )
    code: Code
    message_from: MessageSource | None = Field(
        default=None, description="Where the human-readable message comes from."
    )
    field: str | None = Field(default=None, description="Input or form field the outcome concerns.")


class Recoverable(Strict):
    type: Literal["recoverable"] = Field(
        description="A known transient condition. If recovery is exhausted it becomes a hard "
        "failure with this same code."
    )
    code: Code
    recovery: Recovery = Field(description="What replay does about it.")


class HardFailure(Strict):
    type: Literal["hard_failure"] = Field(
        description="Stop and surface a debuggable error. Exit 2."
    )
    code: Code
    escalate: bool = Field(
        description="Raise an intervention for a human instead of just stopping."
    )


Outcome = Annotated[BusinessOutcome | Recoverable | HardFailure, Field(discriminator="type")]


class OutcomeDetector(Strict):
    id: DetectorId = Field(description="Stable detector id, unique in the artifact.")
    description: str = Field(min_length=1, description="The runtime condition this recognizes.")
    when: Condition = Field(description="Trigger. Evaluated after each in-scope step, in order.")
    scope: list[StepId] | None = Field(
        default=None, description="Steps after which this detector runs; null means every step."
    )
    outcome: Outcome = Field(description="How a match is classified and handled. First match wins.")


# ---- contract: inputs, outputs, secrets, surface ----------------------------------------------


class InputParam(Strict):
    name: ParamName = Field(description="Referenced in steps as {{inputs.<name>}}.")
    type: Literal["string", "integer", "money", "boolean", "enum"] = Field(
        description="Type the value is coerced to before any step runs."
    )
    description: str = Field(min_length=1, description="What a calling agent must supply.")
    required: bool = Field(default=True, description="Whether the caller must supply a value.")
    pattern: Regex | None = Field(
        default=None, description="Full-match regex the raw value must satisfy."
    )
    enum_values: list[str] | None = Field(default=None, description="Allowed values for type enum.")
    sensitive: bool = Field(
        default=False, description="Masked in logs and evidence, and never allowed in a URL."
    )

    @model_validator(mode="after")
    def _enum_shape(self) -> Self:
        if (self.type == "enum") != (self.enum_values is not None):
            raise ValueError("enum_values is required for type enum and only for type enum")
        return self


class SecretRef(Strict):
    name: ParamName = Field(description="Referenced in steps as {{secrets.<name>}}.")
    env_var: str = Field(
        pattern=r"^[A-Z][A-Z0-9_]*$",
        description="Environment variable replay reads it from. Values never enter artifacts.",
    )
    description: str = Field(min_length=1, description="What credential this is.")


_PARSE_FOR_TYPE = {"money": "money_usd", "string": "text", "integer": "integer", "date": "date_iso"}


class Extraction(Strict):
    target: Target = Field(description="Element whose visible text holds the value.")
    parse: Literal["money_usd", "text", "integer", "date_iso"] = Field(
        description="How the visible text is parsed into the typed output."
    )


class OutputSpec(Strict):
    name: ParamName = Field(description="Key in ReplayResult.outputs.")
    type: Literal["money", "string", "integer", "date"] = Field(
        description="Type returned to the caller."
    )
    description: str = Field(min_length=1, description="What the value means to a calling agent.")
    extract: Extraction = Field(description="Where and how the value is read once success holds.")
    sensitive: bool = Field(
        default=False, description="Returned to the caller but masked in evidence."
    )

    @model_validator(mode="after")
    def _parse_matches_type(self) -> Self:
        if _PARSE_FOR_TYPE[self.type] != self.extract.parse:
            raise ValueError(f"output type {self.type} must use parse {_PARSE_FOR_TYPE[self.type]}")
        return self


class WebSurface(Strict):
    kind: Literal["web"] = Field(description="A browser-rendered application.")
    entry_url: str = Field(pattern=r"^https?://[^/\s]+/\S*$", description="Where step 1 starts.")
    app_family: ParamName = Field(
        description="Vendor product of this flow; tenants on the same family share artifacts."
    )
    app_version_hint: str = Field(min_length=1, description="Build the flow was recorded against.")


class DesktopSurface(Strict):
    kind: Literal["desktop"] = Field(
        description="A native app driven through OS accessibility APIs. Designed, not built."
    )
    executable: str = Field(min_length=1, description="Application to launch or attach to.")
    window_title_pattern: Regex = Field(description="Regex identifying the main window.")
    app_family: ParamName = Field(description="Vendor product this flow belongs to.")
    app_version_hint: str = Field(min_length=1, description="Build the flow was recorded against.")


Surface = Annotated[WebSurface | DesktopSurface, Field(discriminator="kind")]


class CapabilityMeta(Strict):
    id: str = Field(
        pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}$",
        description="Dotted id, stable across versions: <app_family>.<entity>.<verb_phrase>.",
    )
    version: str = Field(
        pattern=r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$",
        description="Semver. Major for input/output contract changes, minor for flow changes, "
        "patch for wording.",
    )
    status: Literal["draft", "approved", "deprecated"] = Field(
        description="Drafts need --allow-draft to replay; approval is a human act."
    )
    name: str = Field(min_length=1, description="Short human name.")
    description: str = Field(min_length=1, description="What the capability does, end to end.")
    created_at: datetime = Field(description="When the artifact was recorded, timezone-aware.")
    created_from_run_id: str | None = Field(
        default=None, description="Discovery run that produced it, if any."
    )
    policy_ref: str = Field(
        pattern=r"^[a-z0-9][a-z0-9-]*$", description="Policy id in policy/allowlist.yaml."
    )
    approved_by: str | None = Field(default=None, description="Who approved it.")
    approved_at: datetime | None = Field(default=None, description="When it was approved.")

    @model_validator(mode="after")
    def _approval_shape(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        approved = self.approved_by is not None and self.approved_at is not None
        if self.status == "approved" and not approved:
            raise ValueError("an approved capability needs approved_by and approved_at")
        if self.status == "draft" and (self.approved_by or self.approved_at):
            raise ValueError("a draft capability cannot carry approval fields")
        return self


class SuccessCondition(Strict):
    checkpoint: CheckpointId = Field(description="Checkpoint that proves the goal was reached.")
    requires_outputs: list[ParamName] = Field(
        description="Outputs that must extract successfully for the run to count as success."
    )


class TenantOverride(Strict):
    """A per-tenant patch on the base artifact, applied at load time. See artifact.overrides."""

    description: str = Field(min_length=1, description="Why this tenant differs from the base.")
    entry_url: str | None = Field(
        default=None, pattern=r"^https?://[^/\s]+/\S*$", description="Tenant's own host."
    )
    steps: dict[StepId, dict[str, JsonValue]] = Field(
        default_factory=dict,
        description="step_id -> partial step, deep-merged. id, action and risk stay fixed.",
    )
    checkpoints: dict[CheckpointId, dict[str, JsonValue]] = Field(
        default_factory=dict, description="checkpoint_id -> partial checkpoint, deep-merged."
    )


class Provenance(Strict):
    recorded_by: Literal["discover", "human"] = Field(description="Who produced the artifact.")
    model: str | None = Field(default=None, description="Model used by discovery, if any.")
    evidence_run: str | None = Field(default=None, description="Evidence directory of that run.")
    review_notes: str = Field(default="", description="Reviewer notes; free text.")


# ---- the capability ---------------------------------------------------------------------------

TargetBearingStep = ClickStep | TypeTextStep | SelectOptionStep
_UNOVERRIDABLE_STEP_KEYS = {"id", "action", "risk"}


class Capability(Strict):
    schema_version: Literal["1.0"] = Field(description="Artifact schema version.")
    capability: CapabilityMeta = Field(description="Identity, version, and approval state.")
    surface: Surface = Field(description="What kind of application this runs against.")
    secrets: list[SecretRef] = Field(
        default_factory=list, description="Credentials the flow needs, by reference only."
    )
    inputs: list[InputParam] = Field(description="Typed parameters the caller supplies.")
    outputs: list[OutputSpec] = Field(description="Typed values returned on success.")
    steps: list[Step] = Field(min_length=1, description="Ordered actions.")
    checkpoints: dict[CheckpointId, Checkpoint] = Field(
        description="Named states steps wait for and success is defined by."
    )
    outcome_detectors: list[OutcomeDetector] = Field(
        default_factory=list,
        description="Runtime conditions this app is known to produce, and how each is classified.",
    )
    success: SuccessCondition = Field(description="What counts as done.")
    tenant_overrides: dict[TenantId, TenantOverride] = Field(
        default_factory=dict, description="Per-tenant patches for the same app_family."
    )
    provenance: Provenance = Field(description="Where the artifact came from.")

    @model_validator(mode="after")
    def _cross_references(self) -> Self:
        errors: list[str] = []
        step_ids = [s.id for s in self.steps]
        input_by_name = {i.name: i for i in self.inputs}
        secret_names = {s.name for s in self.secrets}
        output_names = [o.name for o in self.outputs]

        for label, names in (
            ("step id", step_ids),
            ("input", [i.name for i in self.inputs]),
            ("output", output_names),
            ("secret", [s.name for s in self.secrets]),
            ("detector id", [d.id for d in self.outcome_detectors]),
        ):
            dupes = sorted({n for n in names if names.count(n) > 1})
            if dupes:
                errors.append(f"duplicate {label}: {dupes}")

        def need_checkpoint(ref: str, where: str) -> None:
            if ref not in self.checkpoints:
                errors.append(f"{where} references unknown checkpoint {ref!r}")

        def need_step(ref: str, where: str) -> None:
            if ref not in step_ids:
                errors.append(f"{where} references unknown step {ref!r}")

        for step in self.steps:
            if isinstance(step.wait_for, CheckpointWait):
                need_checkpoint(step.wait_for.checkpoint, f"step {step.id} wait_for")
            errors += self._template_errors(step, input_by_name, secret_names)

        need_checkpoint(self.success.checkpoint, "success")
        missing_outputs = sorted(set(self.success.requires_outputs) - set(output_names))
        if missing_outputs:
            errors.append(f"success requires undeclared outputs {missing_outputs}")

        for cp_id, checkpoint in self.checkpoints.items():
            if any(isinstance(c, CheckpointTimeout) for c in iter_conditions(checkpoint.condition)):
                errors.append(
                    f"checkpoint {cp_id} uses checkpoint_timeout, which only detectors may"
                )

        for detector in self.outcome_detectors:
            where = f"detector {detector.id}"
            for condition in iter_conditions(detector.when):
                if isinstance(condition, CheckpointTimeout):
                    need_checkpoint(condition.checkpoint, where)
            for step_ref in detector.scope or []:
                need_step(step_ref, f"{where} scope")
            if isinstance(detector.outcome, Recoverable) and isinstance(
                detector.outcome.recovery, RunStepsRecovery
            ):
                for step_ref in detector.outcome.recovery.step_ids:
                    need_step(step_ref, f"{where} recovery")

        for tenant, override in self.tenant_overrides.items():
            for step_ref, patch in override.steps.items():
                need_step(step_ref, f"tenant {tenant} override")
                forbidden = sorted(_UNOVERRIDABLE_STEP_KEYS & set(patch))
                if forbidden:
                    errors.append(
                        f"tenant {tenant} override of {step_ref} may not change {forbidden}"
                    )
            for cp_ref in override.checkpoints:
                need_checkpoint(cp_ref, f"tenant {tenant} override")

        if isinstance(self.surface, DesktopSurface):
            errors += self._desktop_errors()

        if errors:
            raise ValueError("; ".join(errors))
        return self

    @staticmethod
    def _template_errors(step: Step, inputs: dict[str, InputParam], secrets: set[str]) -> list[str]:
        errors: list[str] = []
        fields: list[tuple[str, str]] = []
        if isinstance(step, NavigateStep):
            fields.append(("url", step.url))
        elif isinstance(step, TypeTextStep):
            fields.append(("value", step.value))
        elif isinstance(step, SelectOptionStep):
            fields.append(("option_label", step.option_label))
        for field_name, value in fields:
            for scope, name in template_refs(value):
                known = {"inputs": name in inputs, "secrets": name in secrets}.get(
                    scope, name == "entry_url"
                )
                if not known:
                    errors.append(f"step {step.id} {field_name} references unknown {scope}.{name}")
                elif isinstance(step, NavigateStep) and (
                    scope == "secrets" or (scope == "inputs" and inputs[name].sensitive)
                ):
                    errors.append(f"step {step.id} puts {scope}.{name} in a URL; URLs are logged")
        if isinstance(step, TypeTextStep) and step.target.looks_sensitive:
            refs = template_refs(step.value)
            safe = is_single_template(step.value) and (
                refs[0][0] == "secrets"
                or (
                    refs[0][0] == "inputs"
                    and inputs.get(refs[0][1]) is not None
                    and inputs[refs[0][1]].sensitive
                )
            )
            if not safe:
                errors.append(
                    f"step {step.id} types into a credential field; value must be a single "
                    "{{secrets.*}} or sensitive {{inputs.*}} template"
                )
        return errors

    def _desktop_errors(self) -> list[str]:
        errors: list[str] = []
        for step in self.steps:
            if isinstance(step, NavigateStep):
                errors.append(f"step {step.id}: desktop surfaces have no URLs to navigate")
        conditions = [cp.condition for cp in self.checkpoints.values()] + [
            d.when for d in self.outcome_detectors
        ]
        for root in conditions:
            for condition in iter_conditions(root):
                if isinstance(condition, UrlMatches | StatusCode):
                    errors.append(f"{condition.kind} has no meaning on a desktop surface")
        return errors


AllOf.model_rebuild()
AnyOf.model_rebuild()
