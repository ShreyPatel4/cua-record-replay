"""Intervention contracts: the request a stuck run raises, and one captured human action.

Both are written to evidence, so callers redact every free-text field before construction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReasonCode = Literal[
    "MAX_STEPS",
    "TIMEOUT",
    "REPEATED_ACTION",
    "NO_PROGRESS",
    "GAVE_UP",
    "HARD_FAILURE_ESCALATE",
    "IRREVERSIBLE_NEEDS_HUMAN",
    "RECOVERY_EXHAUSTED",
    "POST_HANDOFF_CHECKPOINT_FAILED",
]


class InterventionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class InterventionRequest(InterventionModel):
    intervention_id: str = Field(description="Unique id; referenced by session state and results.")
    run_id: str = Field(description="Paused run.")
    kind: Literal["discovery", "replay"] = Field(description="Which loop raised it.")
    capability_id: str | None = Field(default=None, description="Capability, for replay.")
    goal: str | None = Field(default=None, description="Natural-language goal, for discovery.")
    step_id: str = Field(description="Step (replay) or turn (discovery) the run stopped at.")
    reason_code: ReasonCode = Field(description="Why the run stopped.")
    reason_text: str = Field(description="Human-readable detail, redacted.")
    current_url: str = Field(description="URL of the active frame, redacted.")
    screenshot_path: str = Field(description="Screenshot taken at the moment of pausing.")
    a11y_snapshot_path: str = Field(description="Accessibility snapshot at the moment of pausing.")
    suggested_actions: list[str] = Field(
        default_factory=list, description="What the operator might do, in plain words."
    )
    resume_command: str = Field(description="Exact command that takes control of this run.")
    requested_at: datetime = Field(description="When the request was raised.")
    expires_at: datetime = Field(description="After this the run finishes as escalated.")

    @model_validator(mode="after")
    def _shape(self) -> Self:
        if (self.kind == "replay") != (self.capability_id is not None):
            raise ValueError(
                "replay interventions need capability_id; discovery ones must not set it"
            )
        if (self.kind == "discovery") != (self.goal is not None):
            raise ValueError("discovery interventions need goal; replay ones must not set it")
        if self.expires_at <= self.requested_at:
            raise ValueError("expires_at must be after requested_at")
        return self


class HumanAction(InterventionModel):
    at: datetime = Field(description="When the event fired in the page.")
    event: Literal["click", "input", "change", "navigate", "dialog"] = Field(
        description="DOM or navigation event observed while the human held control."
    )
    frame_url: str = Field(description="URL of the frame the event happened in, redacted.")
    target_role: str | None = Field(default=None, description="Role of the event target.")
    target_name: str | None = Field(default=None, description="Accessible name of the target.")
    target_text: str | None = Field(
        default=None, max_length=80, description="Visible text, redacted."
    )
    value: str | None = Field(
        default=None,
        description="Typed or selected value, redacted; credentials become [REDACTED].",
    )
