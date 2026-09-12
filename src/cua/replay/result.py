"""ReplayResult: the structured answer a replay returns to its caller, plus the stability report.

Validators tie status, outcome code, and exit code together so an impossible result cannot exist.
"""

from __future__ import annotations

import statistics
from collections import Counter
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.vocab import OutcomeCode, ParamName, StepId

ReplayStatus = Literal[
    "success", "business_outcome", "recovered_then_success", "hard_failure", "escalated"
]

EXIT_CODES: dict[str, int] = {
    "success": 0,
    "recovered_then_success": 0,
    "business_outcome": 0,
    "hard_failure": 2,
    "escalated": 3,
}

# Codes the engine itself can produce. Artifacts add app-specific ones (MEMBER_NOT_FOUND, ...).
ENGINE_CODES = frozenset(
    {
        "TARGET_NOT_FOUND",
        "TARGET_AMBIGUOUS",
        "CHECKPOINT_TIMEOUT",
        "POLICY_BLOCKED",
        "UNEXPECTED_DIALOG",
        "OUTPUT_EXTRACTION_FAILED",
        "CONFIRMATION_REQUIRED",
        "OPERATOR_ABORTED",
        "INTERVENTION_EXPIRED",
    }
)


class ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MoneyValue(ResultModel):
    amount: Decimal = Field(description="Exact decimal amount, serialized as a string.")
    currency: Literal["USD"] = Field(description="ISO 4217 currency code.")


OutputValue = MoneyValue | int | str


class AutomaticRecovery(ResultModel):
    type: Literal["automatic"] = Field(description="Handled by a detector's recovery, no human.")
    step_id: StepId = Field(description="Step during which the condition appeared.")
    detector_id: str = Field(description="Detector that matched.")
    code: OutcomeCode = Field(description="Condition code, e.g. INTERSTITIAL or SLOW_LOAD.")
    recovery_kind: Literal["click", "wait_retry", "run_steps"] = Field(description="What was done.")
    attempts: int = Field(ge=1, description="Attempts used.")
    duration_ms: int = Field(ge=0, description="Time spent recovering.")


class HumanIntervention(ResultModel):
    type: Literal["human"] = Field(description="A human took the live session and handed it back.")
    step_id: StepId = Field(description="Step the run was paused at.")
    intervention_id: str = Field(description="Id of the intervention request.")
    reason_code: str = Field(description="Why the run paused.")
    operator_note: str = Field(description="What the operator said they did, redacted.")
    human_action_count: int = Field(
        ge=0, description="Captured human actions, see human_actions.jsonl."
    )
    reverified: bool = Field(description="Whether the step checkpoint passed after hand-back.")
    taken_at: datetime = Field(description="When the human took control.")
    handed_back_at: datetime = Field(description="When control returned to automation.")


RecoveryRecord = Annotated[AutomaticRecovery | HumanIntervention, Field(discriminator="type")]


class DriftWarning(ResultModel):
    type: Literal["drift"] = Field(description="A later rung than recorded resolved the target.")
    step_id: StepId = Field(description="Step whose target drifted.")
    target_ref: str = Field(
        description="Which target: 'step', 'output:<name>', or 'checkpoint:<id>'."
    )
    recorded_rung: int = Field(ge=0, description="Ladder index that resolved at record time.")
    resolved_rung: int = Field(ge=0, description="Ladder index that resolved now.")
    resolved_strategy: str = Field(description="Strategy of the rung that resolved now.")


class WeakTargetWarning(ResultModel):
    type: Literal["weak_target"] = Field(
        description="A target resolved only through a fragile rung."
    )
    step_id: StepId = Field(description="Step whose target is weak.")
    target_ref: str = Field(description="Which target.")
    message: str = Field(description="Why it is weak.")


ReplayWarning = Annotated[DriftWarning | WeakTargetWarning, Field(discriminator="type")]


class StepRecord(ResultModel):
    step_id: StepId = Field(description="Step executed.")
    action: str = Field(description="Action type.")
    resolved_rung: int | None = Field(default=None, description="Ladder index used, if targeted.")
    resolved_strategy: str | None = Field(default=None, description="Strategy used, if targeted.")
    passed: bool = Field(description="Whether the step's wait condition held.")
    attempts: int = Field(ge=1, description="Times the step was attempted.")
    duration_ms: int = Field(ge=0, description="Wall time for the step including waits.")


class ReplayResult(ResultModel):
    status: ReplayStatus = Field(description="Top-level classification of the run.")
    capability_id: str = Field(description="Capability replayed.")
    capability_version: str = Field(description="Version replayed.")
    tenant: str | None = Field(default=None, description="Tenant override applied, if any.")
    run_id: str = Field(description="Unique run id; also the evidence directory name.")
    started_at: datetime = Field(description="Run start, timezone-aware.")
    duration_ms: int = Field(ge=0, description="Total wall time.")
    inputs: dict[ParamName, str] = Field(description="Inputs as supplied, sensitive values masked.")
    outputs: dict[ParamName, OutputValue] = Field(
        default_factory=dict, description="Declared outputs; present only on success statuses."
    )
    outcome_code: OutcomeCode | None = Field(
        default=None, description="Why a non-success run ended. Null on success statuses."
    )
    message: str | None = Field(default=None, description="Human-readable explanation, redacted.")
    step_reached: StepId = Field(description="Last step started.")
    expected: str | None = Field(default=None, description="What the step or checkpoint expected.")
    observed: str | None = Field(default=None, description="What was actually on screen, redacted.")
    steps: list[StepRecord] = Field(default_factory=list, description="Per-step execution trace.")
    recoveries: list[RecoveryRecord] = Field(
        default_factory=list, description="Automatic recoveries and human interventions, in order."
    )
    warnings: list[ReplayWarning] = Field(
        default_factory=list, description="Drift and weak-target signals."
    )
    evidence_dir: str = Field(description="Where screenshots, logs, and traces for this run live.")
    intervention_path: str | None = Field(
        default=None, description="Intervention request file; required when escalated."
    )

    @property
    def exit_code(self) -> int:
        return EXIT_CODES[self.status]

    @model_validator(mode="after")
    def _status_invariants(self) -> Self:
        succeeded = self.status in ("success", "recovered_then_success")
        if succeeded and self.outcome_code is not None:
            raise ValueError(f"{self.status} cannot carry outcome_code {self.outcome_code}")
        if not succeeded and self.outcome_code is None:
            raise ValueError(f"{self.status} requires an outcome_code")
        if not succeeded and self.outputs:
            raise ValueError(f"{self.status} cannot return outputs")
        if self.status == "success" and self.recoveries:
            raise ValueError("a run with recoveries is recovered_then_success, not success")
        if self.status == "recovered_then_success" and not self.recoveries:
            raise ValueError("recovered_then_success requires at least one recovery record")
        if self.status in ("hard_failure", "escalated") and not (self.expected and self.observed):
            raise ValueError(f"{self.status} must say what was expected and what was observed")
        if (self.status == "escalated") != (self.intervention_path is not None):
            raise ValueError("intervention_path is required for escalated and only for escalated")
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        return self


class IterationSummary(ResultModel):
    run_id: str = Field(description="Run id of this iteration.")
    status: ReplayStatus = Field(description="Status of this iteration.")
    outcome_code: str | None = Field(default=None, description="Outcome code, if any.")
    rungs: dict[StepId, str] = Field(description="step_id -> resolved strategy.")
    outputs_digest: str = Field(
        description="Hash of the outputs, so equality is visible without values."
    )
    duration_ms: int = Field(ge=0, description="Wall time.")
    conditions: dict[str, str] = Field(
        default_factory=dict,
        description="Environment facts the harness supplied for this iteration, e.g. armed faults, "
        "so an intended difference is never read as flakiness.",
    )


class StabilityReport(ResultModel):
    capability_id: str = Field(description="Capability replayed.")
    capability_version: str = Field(description="Version replayed.")
    iterations: list[IterationSummary] = Field(min_length=1, description="One entry per run.")

    @property
    def passes(self) -> int:
        return sum(1 for i in self.iterations if EXIT_CODES[i.status] == 0)

    @property
    def rung_distribution(self) -> dict[str, dict[str, int]]:
        dist: dict[str, Counter[str]] = {}
        for iteration in self.iterations:
            for step_id, strategy in iteration.rungs.items():
                dist.setdefault(step_id, Counter())[strategy] += 1
        return {step: dict(counter) for step, counter in sorted(dist.items())}

    @property
    def duration_spread_ms(self) -> tuple[int, int, int]:
        durations = [i.duration_ms for i in self.iterations]
        return min(durations), int(statistics.median(durations)), max(durations)

    @property
    def deterministic(self) -> bool:
        """Same status, code, rungs, and outputs across iterations run under equal conditions."""
        groups: dict[tuple[tuple[str, str], ...], set[tuple[object, ...]]] = {}
        for i in self.iterations:
            key = tuple(sorted(i.conditions.items()))
            shape = (i.status, i.outcome_code, tuple(sorted(i.rungs.items())), i.outputs_digest)
            groups.setdefault(key, set()).add(shape)
        return all(len(shapes) == 1 for shapes in groups.values())
