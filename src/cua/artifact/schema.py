"""Capability artifact schema: the typed, versioned contract discovery produces and replay executes.

Models and cross-reference validation only; nothing here touches a browser, a model, or the disk.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
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
    ENGINE_CODES,
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
POLL_MS = 250


class Strict(BaseModel):
    """Base for every artifact model: unknown fields are errors and attribute assignment is refused.

    Nested lists and dicts are not deep-frozen. Treat instances as read-only and build changed
    copies through model_dump and model_validate, which re-runs every validator.
    """

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
Confidence = Annotated[
    float, Field(ge=0, le=1, description="Recorder's estimate this rung alone is unique.")
]


# ---- targeting: the locator ladder ------------------------------------------------------------


class RoleNameRung(Strict):
    strategy: Literal["role_name"] = Field(description="Accessible role plus accessible name.")
    role: str = Field(min_length=1, description="ARIA role as the accessibility tree reports it.")
    name: str = Field(
        min_length=1, description="Accessible name, whitespace-collapsed, exact match."
    )
    confidence: Confidence


class LabelTextRung(Strict):
    strategy: Literal["label_text"] = Field(
        description="A form control found through its visible label."
    )
    label: str = Field(
        min_length=1,
        description="The label element's own innerText, whitespace-collapsed, exact match.",
    )
    relation: Literal["wrapped", "same_row", "below"] = Field(
        description="Where the control sits relative to the label: inside it, in a later cell of "
        "the label's nearest table row, or the nearest control starting under the label's bottom "
        "edge and overlapping it horizontally."
    )
    control: Literal["textbox", "combobox", "checkbox"] = Field(
        description="Kind of control expected at that position."
    )
    confidence: Confidence


class TextExactRung(Strict):
    strategy: Literal["text_exact"] = Field(
        description="An element whose own visible text matches."
    )
    text: str = Field(
        min_length=1,
        description="The element's own innerText, whitespace-collapsed and case-folded, compared "
        "for equality. Hidden elements never match.",
    )
    role: str | None = Field(default=None, description="Optional role filter, e.g. cell or link.")
    confidence: Confidence


class AnchorRelativeRung(Strict):
    strategy: Literal["anchor_relative"] = Field(
        description="An element located by direction from a stable text anchor."
    )
    anchor_text: str = Field(
        min_length=1,
        description="Exact visible text of the anchor, matched like text_exact. The anchor itself "
        "must be unique or the rung does not resolve.",
    )
    direction: Literal["right", "left", "below", "above"] = Field(
        description="right: candidates starting at or beyond the anchor's right edge that overlap "
        "it vertically; left mirrors it; below: starting at or under its bottom edge and "
        "overlapping horizontally; above mirrors it. Candidates are ordered by distance."
    )
    same_row: bool = Field(
        description="Only consider candidates inside the anchor's nearest enclosing table row. "
        "On non-table UIs, the anchor's vertical-overlap band."
    )
    target_kind: Literal["input", "select", "clickable", "text"] = Field(
        description="input: text-like input or textarea. select: select or combobox. clickable: "
        "a, button, submit or button input, role button or link, or any element with an onclick "
        "handler. text: an element with non-empty own innerText."
    )
    nth: int = Field(
        default=1, ge=1, description="1-based position in the distance ordering after filtering."
    )
    confidence: Confidence


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
    confidence: Confidence


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
        default=None,
        max_length=80,
        description="Visible text seen at record time, redacted. Never recorded for credential "
        "targets, targets typed with a secret or sensitive input, or sensitive outputs.",
    )
    input_type: str | None = Field(
        default=None,
        pattern=r"^[a-z]+$",
        description="HTML input type seen at record time, e.g. password. A password type marks the "
        "target as a credential field however it is labelled.",
    )


class Target(Strict):
    ladder: list[Rung] = Field(
        min_length=1,
        description="Rungs tried top to bottom on every replay; a rung wins only when exactly one "
        "visible element matches.",
    )
    recorded_rung: int = Field(
        ge=0,
        description="Index of the rung that resolved at record time. A later rung winning on "
        "replay raises a drift warning; an earlier one is fine.",
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
        if self.looks_sensitive and self.fingerprint.text is not None:
            raise ValueError("a credential target must not record its text in the fingerprint")
        return self

    @property
    def looks_sensitive(self) -> bool:
        """Any name, label, or anchor that reads like a credential, or a password input type."""
        if self.fingerprint.input_type == "password":
            return True
        seen = [self.fingerprint.name or ""]
        for rung in self.ladder:
            if isinstance(rung, RoleNameRung):
                seen.append(rung.name)
            elif isinstance(rung, LabelTextRung):
                seen.append(rung.label)
            elif isinstance(rung, TextExactRung):
                seen.append(rung.text)
            elif isinstance(rung, AnchorRelativeRung) and rung.target_kind == "input":
                seen.append(rung.anchor_text)
        return any(SENSITIVE_FIELD_RE.search(s) for s in seen)


# ---- conditions: checkpoints and detector triggers --------------------------------------------


class TextPresent(Strict):
    kind: Literal["text_present"] = Field(description="Visible text appears in the frame.")
    text: str = Field(
        min_length=1,
        description="Substring of the frame body's innerText, whitespace-collapsed, "
        "case-sensitive. Hidden elements do not count. May use non-sensitive {{inputs.*}} "
        "templates.",
    )
    frame_path: FramePath = []


class ElementPresent(Strict):
    kind: Literal["element_present"] = Field(
        description="A target resolves to exactly one visible element."
    )
    target: Target = Field(description="The element that must be present and visible.")


class UrlMatches(Strict):
    kind: Literal["url_matches"] = Field(description="The frame's current URL matches a regex.")
    pattern: Regex = Field(
        description="Regex searched in the frame URL (not the top page URL). Non-sensitive "
        "{{inputs.*}} templates render regex-escaped."
    )
    frame_path: FramePath = []


class StatusCode(Strict):
    kind: Literal["status_code"] = Field(
        description="The frame's last document response had a status."
    )
    code: int = Field(
        ge=100,
        le=599,
        description="HTTP status of the most recent document response committed in the frame. "
        "Sub-resources and fetches do not count.",
    )
    frame_path: FramePath = []


class CheckpointTimeout(Strict):
    kind: Literal["checkpoint_timeout"] = Field(
        description="A checkpoint wait reached its deadline. Only valid in detector triggers, and "
        "only evaluated at that deadline."
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

_POLL_NOTE = (
    f"Polled about every {POLL_MS} ms. Each tick first evaluates the step's in-scope outcome "
    "detectors in artifact order, and the first match ends the wait."
)


class CheckpointWait(Strict):
    kind: Literal["checkpoint"] = Field(
        description=f"Wait until a named checkpoint passes. {_POLL_NOTE}"
    )
    checkpoint: CheckpointId = Field(description="Checkpoint to wait for.")
    timeout_ms: int = Field(
        default=10_000,
        ge=100,
        le=120_000,
        description="Deadline. At the deadline, checkpoint_timeout detectors for this checkpoint "
        "are evaluated; if none matches the step fails with CHECKPOINT_TIMEOUT.",
    )


class SettleWait(Strict):
    kind: Literal["settle"] = Field(
        description=f"Wait for no in-flight document requests plus a quiet DOM. {_POLL_NOTE}"
    )
    quiet_ms: int = Field(default=300, ge=50, le=5_000, description="DOM must be quiet this long.")
    timeout_ms: int = Field(
        default=10_000, ge=100, le=120_000, description="Upper bound on waiting."
    )


class UrlChangeWait(Strict):
    kind: Literal["url_change"] = Field(
        description=f"Wait until the frame URL matches a regex. {_POLL_NOTE}"
    )
    pattern: Regex = Field(description="Regex searched in the frame URL.")
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
StepTarget = Annotated[
    Target,
    Field(
        description="The element this step acts on. Resolution retries on each poll tick until "
        "the step's wait timeout, then fails with TARGET_NOT_FOUND or TARGET_AMBIGUOUS."
    ),
]


class DialogExpectation(Strict):
    """A native dialog the step is expected to open. Unexpected dialogs always fail the step."""

    dialog_type: Literal["confirm", "alert"] = Field(description="Native dialog type.")
    message_pattern: Regex = Field(description="Regex the dialog message must match.")
    response: Literal["accept", "dismiss"] = Field(
        description="How replay answers the dialog, after the policy gate approves that answer."
    )


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

    @model_validator(mode="after")
    def _accepted_confirm_is_irreversible(self) -> Self:
        dialog = self.dialog
        accepts_confirm = (
            dialog is not None and dialog.dialog_type == "confirm" and dialog.response == "accept"
        )
        if accepts_confirm and self.risk != "irreversible":
            raise ValueError(
                f"step {self.id} accepts a confirm dialog, so it must declare risk irreversible"
            )
        return self


class TypeTextStep(Strict):
    id: StepId = Field(description="Stable step id, unique in the artifact.")
    action: Literal["type_text"] = Field(description="Type into a text control.")
    description: StepDescription
    target: StepTarget
    value: str = Field(
        description="Literal text or a template. Secrets and sensitive inputs must be the whole "
        "value as one template; credential fields accept nothing else."
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
        min_length=1,
        description="Visible option label, literal or non-sensitive {{inputs.*}} template.",
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
        description="First regex match in the frame's visible text, redacted before it is stored."
    )
    pattern: Regex = Field(description="Regex searched in the frame body's innerText.")
    frame_path: FramePath = []


class TargetTextMessage(Strict):
    kind: Literal["target_text"] = Field(
        description="Visible text of a target element, redacted before it is stored."
    )
    target: Target = Field(description="Element whose text becomes the message.")


MessageSource = Annotated[TextPatternMessage | TargetTextMessage, Field(discriminator="kind")]


class ClickRecovery(Strict):
    kind: Literal["click"] = Field(
        description="Click something, e.g. dismiss a known interstitial, then restart the "
        "interrupted wait with its full timeout. Each detector match uses one attempt."
    )
    target: Target = Field(description="Element to click. Passes the policy gate like any click.")
    max_attempts: int = Field(default=2, ge=1, le=5, description="Attempts before giving up.")


class WaitRetryRecovery(Strict):
    kind: Literal["wait_retry"] = Field(
        description="Keep polling the interrupted wait, detectors included, for a bounded time."
    )
    max_total_ms: int = Field(ge=100, le=120_000, description="Total extra time allowed.")
    poll_ms: int = Field(default=500, ge=50, le=10_000, description="Interval between re-checks.")


class RunStepsRecovery(Strict):
    kind: Literal["run_steps"] = Field(
        description="Re-run earlier steps in order (e.g. sign in again), each through the policy "
        "gate and its own wait, except the last: its wait is replaced by re-verifying the "
        "interrupted step's checkpoint with that wait's timeout, because the app decides where a "
        "re-run lands (CoreLedger's sign-in redirects back to the page that expired)."
    )
    step_ids: list[StepId] = Field(
        min_length=1,
        description="Steps to re-run, in flow order, all before the detector's scope. Never "
        "irreversible steps or steps that answer a dialog.",
    )
    max_attempts: int = Field(default=1, ge=1, le=3, description="Attempts before giving up.")


Recovery = Annotated[
    ClickRecovery | WaitRetryRecovery | RunStepsRecovery, Field(discriminator="kind")
]
Code = Annotated[
    OutcomeCode,
    Field(description="Stable machine-readable code returned to the caller. Not an engine code."),
]


class BusinessOutcome(Strict):
    type: Literal["business_outcome"] = Field(
        description="A legitimate answer the caller needs, not a failure. Exit code 0."
    )
    code: Code
    message_from: MessageSource | None = Field(
        default=None, description="Where the human-readable message comes from."
    )
    field: ParamName | None = Field(
        default=None,
        description="Declared input the outcome concerns. Replay returns it with the message in "
        "ReplayResult.field_errors.",
    )

    @model_validator(mode="after")
    def _field_needs_message(self) -> Self:
        if self.field is not None and self.message_from is None:
            raise ValueError(f"{self.code}: an outcome about field {self.field} needs message_from")
        return self


class Recoverable(Strict):
    type: Literal["recoverable"] = Field(
        description="A known transient condition replay handles without a human."
    )
    code: Code
    recovery: Recovery = Field(description="What replay does about it.")
    on_exhausted: Literal["hard_failure", "escalate"] = Field(
        default="hard_failure",
        description="What the run becomes when attempts run out or the recovered state never "
        "verifies. The outcome code stays this detector's code.",
    )


class HardFailure(Strict):
    type: Literal["hard_failure"] = Field(
        description="Stop and surface a debuggable error. Exit 2, or 3 when escalated."
    )
    code: Code
    escalate: bool = Field(
        description="Raise an intervention for a human instead of just stopping."
    )


Outcome = Annotated[BusinessOutcome | Recoverable | HardFailure, Field(discriminator="type")]


class OutcomeDetector(Strict):
    id: DetectorId = Field(description="Stable detector id, unique in the artifact.")
    description: str = Field(min_length=1, description="The runtime condition this recognizes.")
    when: Condition = Field(
        description="Trigger, polled on every tick of an in-scope step's wait before the wait's "
        "own condition. checkpoint_timeout triggers are evaluated only at that wait's deadline."
    )
    scope: list[StepId] | None = Field(
        default=None,
        description="Steps whose waits poll this detector; null means every step. Required for "
        "run_steps recoveries and checkpoint_timeout triggers.",
    )
    outcome: Outcome = Field(
        description="How a match is classified and handled. Detectors are tried in artifact "
        "order and the first match wins."
    )


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
        default=False,
        description="Masked in logs and evidence; never allowed in a URL, a condition, or an "
        "option label.",
    )

    @model_validator(mode="after")
    def _enum_shape(self) -> Self:
        if (self.type == "enum") != (self.enum_values is not None):
            raise ValueError("enum_values is required for type enum and only for type enum")
        return self


class SecretRef(Strict):
    name: ParamName = Field(description="Referenced in steps as {{secrets.<name>}}.")
    kind: Literal["credential", "identity"] = Field(
        description="credential (password, PIN, token): typed only into a credential field and "
        "redacted wherever it appears. identity (an operator id): typed into any field and "
        "redacted as a whole token."
    )
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
        default=False,
        description="Returned to the caller but masked in evidence, logs, and the artifact itself.",
    )

    @model_validator(mode="after")
    def _output_shape(self) -> Self:
        if _PARSE_FOR_TYPE[self.type] != self.extract.parse:
            raise ValueError(f"output type {self.type} must use parse {_PARSE_FOR_TYPE[self.type]}")
        fingerprint = self.extract.target.fingerprint
        if self.sensitive and (fingerprint.text is not None or fingerprint.name is not None):
            raise ValueError(
                f"sensitive output {self.name} must not record the value in its fingerprint"
            )
        return self


_URL_PATTERN = r"^https?://[^/\s@\\]+/\S*$"


class WebSurface(Strict):
    kind: Literal["web"] = Field(description="A browser-rendered application.")
    entry_url: str = Field(
        pattern=_URL_PATTERN,
        description="Where step 1 starts. Must pass the policy named by policy_ref at load.",
    )
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
        description="Semver. Major for caller or deployment contract changes, minor for flow "
        "changes, patch for wording. See artifact.versioning.",
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


# Dotted paths a tenant patch may never set: identity, safety class, what gets typed or chosen,
# where the browser goes, how dialogs are answered, and what kind of element a target is.
LOCKED_STEP_PATHS = frozenset(
    {
        "id",
        "action",
        "risk",
        "on_fail",
        "value",
        "option_label",
        "url",
        "key",
        "dialog",
        "target.fingerprint.role",
        "target.fingerprint.input_type",
    }
)


def patch_paths(patch: Mapping[str, JsonValue], prefix: str = "") -> Iterator[str]:
    """Every dotted key path a patch sets, including the objects on the way down."""
    for key, value in patch.items():
        path = f"{prefix}{key}"
        yield path
        if isinstance(value, dict):
            yield from patch_paths(value, f"{path}.")


class TenantOverride(Strict):
    """A per-tenant patch on the base artifact, applied at load time. See artifact.overrides."""

    description: str = Field(min_length=1, description="Why this tenant differs from the base.")
    entry_url: str | None = Field(
        default=None,
        pattern=_URL_PATTERN,
        description="Tenant's own host. Must pass the capability's policy at load.",
    )
    steps: dict[StepId, dict[str, JsonValue]] = Field(
        default_factory=dict,
        description="step_id -> partial step, deep-merged. Targets and waits may change; "
        "identity, risk, typed values, URLs, keys, and dialog handling may not.",
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

    @property
    def outcome_codes(self) -> dict[str, str]:
        """code -> outcome type, for every code a detector can return."""
        return {d.outcome.code: d.outcome.type for d in self.outcome_detectors}

    @model_validator(mode="after")
    def _cross_references(self) -> Self:
        errors: list[str] = []
        step_ids = [s.id for s in self.steps]
        position = {s.id: i for i, s in enumerate(self.steps)}
        steps_by_id = {s.id: s for s in self.steps}
        input_by_name = {i.name: i for i in self.inputs}
        secret_by_name = {s.name: s for s in self.secrets}
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
            if ref not in position:
                errors.append(f"{where} references unknown step {ref!r}")

        for step in self.steps:
            if isinstance(step.wait_for, CheckpointWait):
                need_checkpoint(step.wait_for.checkpoint, f"step {step.id} wait_for")
            errors += self._step_template_errors(step, input_by_name, secret_by_name)

        need_checkpoint(self.success.checkpoint, "success")
        missing_outputs = sorted(set(self.success.requires_outputs) - set(output_names))
        if missing_outputs:
            errors.append(f"success requires undeclared outputs {missing_outputs}")

        for cp_id, checkpoint in self.checkpoints.items():
            for condition in iter_conditions(checkpoint.condition):
                if isinstance(condition, CheckpointTimeout):
                    errors.append(
                        f"checkpoint {cp_id} uses checkpoint_timeout, which only detectors may"
                    )
                errors += _condition_template_errors(
                    condition, f"checkpoint {cp_id}", input_by_name
                )

        codes: dict[str, str] = {}
        for detector in self.outcome_detectors:
            where = f"detector {detector.id}"
            outcome = detector.outcome
            for step_ref in detector.scope or []:
                need_step(step_ref, f"{where} scope")
            for condition in iter_conditions(detector.when):
                errors += _condition_template_errors(condition, where, input_by_name)
                if isinstance(condition, CheckpointTimeout):
                    need_checkpoint(condition.checkpoint, where)
                    errors += self._timeout_scope_errors(detector, condition, steps_by_id)
            if outcome.code in ENGINE_CODES:
                errors.append(f"{where} reuses engine code {outcome.code}")
            if codes.setdefault(outcome.code, outcome.type) != outcome.type:
                errors.append(
                    f"code {outcome.code} is both {codes[outcome.code]} and {outcome.type}"
                )
            field = outcome.field if isinstance(outcome, BusinessOutcome) else None
            if field is not None and field not in input_by_name:
                errors.append(f"{where} concerns undeclared input {field!r}")
            if isinstance(outcome, Recoverable) and isinstance(outcome.recovery, RunStepsRecovery):
                for step_ref in outcome.recovery.step_ids:
                    need_step(step_ref, f"{where} recovery")
                referenced = [*outcome.recovery.step_ids, *(detector.scope or [])]
                if all(s in position for s in referenced):
                    errors += self._run_steps_errors(detector, outcome.recovery, position)

        for tenant, override in self.tenant_overrides.items():
            for step_ref, patch in override.steps.items():
                need_step(step_ref, f"tenant {tenant} override")
                locked = sorted(
                    {
                        path
                        for path in patch_paths(patch)
                        for lock in LOCKED_STEP_PATHS
                        if path == lock or path.startswith(lock + ".")
                    }
                )
                if locked:
                    errors.append(f"tenant {tenant} override of {step_ref} may not change {locked}")
            for cp_ref in override.checkpoints:
                need_checkpoint(cp_ref, f"tenant {tenant} override")

        if isinstance(self.surface, DesktopSurface):
            errors += self._desktop_errors()

        if errors:
            raise ValueError("; ".join(errors))
        return self

    @staticmethod
    def _step_template_errors(
        step: Step, inputs: dict[str, InputParam], secrets: dict[str, SecretRef]
    ) -> list[str]:
        errors: list[str] = []
        fields: list[tuple[str, str]] = []
        if isinstance(step, NavigateStep):
            fields.append(("url", step.url))
        elif isinstance(step, TypeTextStep):
            fields.append(("value", step.value))
        elif isinstance(step, SelectOptionStep):
            fields.append(("option_label", step.option_label))

        guarded: list[tuple[str, str]] = []
        for field_name, value in fields:
            for scope, name in template_refs(value):
                known = {"inputs": name in inputs, "secrets": name in secrets}.get(
                    scope, name == "entry_url"
                )
                if not known:
                    errors.append(f"step {step.id} {field_name} references unknown {scope}.{name}")
                    continue
                if scope == "secrets" or (scope == "inputs" and inputs[name].sensitive):
                    guarded.append((scope, name))
                    if isinstance(step, NavigateStep):
                        errors.append(
                            f"step {step.id} puts {scope}.{name} in a URL; URLs are logged"
                        )
                    elif isinstance(step, SelectOptionStep):
                        errors.append(
                            f"step {step.id} puts {scope}.{name} in an option label; only "
                            "type_text may carry secrets or sensitive inputs"
                        )

        if not isinstance(step, TypeTextStep):
            return errors
        if guarded and not is_single_template(step.value):
            errors.append(
                f"step {step.id} mixes a secret or sensitive input with other text; it must be "
                "the whole value"
            )
        if guarded and step.target.fingerprint.text is not None:
            errors.append(
                f"step {step.id} types a secret or sensitive input, so its fingerprint must not "
                "record text"
            )
        for scope, name in guarded:
            credential = scope == "secrets" and secrets[name].kind == "credential"
            if credential and not step.target.looks_sensitive:
                errors.append(
                    f"step {step.id} types credential secrets.{name} into a target that does "
                    "not look like a credential field"
                )
        if step.target.looks_sensitive and not (is_single_template(step.value) and guarded):
            errors.append(
                f"step {step.id} types into a credential field; value must be a single "
                "{{secrets.*}} or sensitive {{inputs.*}} template"
            )
        return errors

    def _timeout_scope_errors(
        self, detector: OutcomeDetector, trigger: CheckpointTimeout, steps: dict[str, Step]
    ) -> list[str]:
        where = f"detector {detector.id}"
        if detector.scope is None:
            return [f"{where} has a checkpoint_timeout trigger, so it needs an explicit scope"]
        errors = []
        for step_ref in detector.scope:
            wait = steps[step_ref].wait_for if step_ref in steps else None
            if not (isinstance(wait, CheckpointWait) and wait.checkpoint == trigger.checkpoint):
                errors.append(
                    f"{where} waits for {trigger.checkpoint} to time out, but step {step_ref} "
                    "does not wait on that checkpoint"
                )
        return errors

    def _run_steps_errors(
        self, detector: OutcomeDetector, recovery: RunStepsRecovery, position: dict[str, int]
    ) -> list[str]:
        where = f"detector {detector.id}"
        if detector.scope is None:
            return [f"{where} recovers with run_steps, so it needs an explicit scope"]
        errors = []
        first_scoped = min(position[s] for s in detector.scope)
        indexes = [position[s] for s in recovery.step_ids]
        if indexes != sorted(set(indexes)):
            errors.append(f"{where} recovery steps must be unique and in flow order")
        if any(i >= first_scoped for i in indexes):
            errors.append(f"{where} recovery steps must all come before the detector's scope")
        for step_ref in recovery.step_ids:
            step = self.steps[position[step_ref]]
            if step.risk == "irreversible" or (isinstance(step, ClickStep) and step.dialog):
                errors.append(
                    f"{where} may not re-run step {step_ref}: it is irreversible or answers a "
                    "dialog"
                )
        for step_ref in detector.scope:
            if not isinstance(self.steps[position[step_ref]].wait_for, CheckpointWait):
                errors.append(
                    f"{where} scope step {step_ref} needs a checkpoint wait to re-verify after "
                    "run_steps"
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


def _condition_template_errors(
    condition: Condition, where: str, inputs: dict[str, InputParam]
) -> list[str]:
    if isinstance(condition, TextPresent):
        value = condition.text
    elif isinstance(condition, UrlMatches):
        value = condition.pattern
    else:
        return []
    errors = []
    for scope, name in template_refs(value):
        if scope != "inputs" or name not in inputs:
            errors.append(
                f"{where} condition may only template declared inputs, not {scope}.{name}"
            )
        elif inputs[name].sensitive:
            errors.append(
                f"{where} condition templates sensitive input {name}; conditions are logged"
            )
    return errors


AllOf.model_rebuild()
AnyOf.model_rebuild()
