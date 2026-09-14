"""ReplayResult: the structured answer a replay returns to its caller, plus the stability report.

Validators tie status, outcome code, and exit code together so an impossible result cannot exist.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import statistics
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from cua.artifact.schema import Capability
from cua.policy.redact import REDACTED
from cua.vocab import ENGINE_CODES, PRE_RUN_CODES, OutcomeCode, ParamName, StepId

ReplayStatus = Literal[
    "success", "business_outcome", "recovered_then_success", "hard_failure", "escalated"
]
SUCCESS_STATUSES = frozenset({"success", "recovered_then_success"})

EXIT_CODES: dict[str, int] = {
    "success": 0,
    "recovered_then_success": 0,
    "business_outcome": 0,
    "hard_failure": 2,
    "escalated": 3,
}


class ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MoneyValue(ResultModel):
    amount: Decimal = Field(description="Exact decimal amount, serialized as a string.")
    currency: Literal["USD"] = Field(description="ISO 4217 currency code.")


class DateValue(ResultModel):
    value: date = Field(description="Calendar date, serialized as YYYY-MM-DD.")


# Money and dates are wrapped so a JSON round trip never turns them into plain strings.
OutputValue = MoneyValue | DateValue | int | str
_OUTPUT_PYTHON_TYPE: dict[str, type] = {
    "money": MoneyValue,
    "date": DateValue,
    "integer": int,
    "string": str,
}


class AutomaticRecovery(ResultModel):
    type: Literal["automatic"] = Field(description="Handled by a detector's recovery, no human.")
    step_id: StepId = Field(description="Step during which the condition appeared.")
    detector_id: str = Field(description="Detector that matched.")
    code: OutcomeCode = Field(description="Condition code, e.g. INTERSTITIAL or SLOW_LOAD.")
    recovery_kind: Literal["click", "wait_retry", "run_steps"] = Field(description="What was done.")
    attempts: int = Field(ge=1, description="Attempts used.")
    succeeded: bool = Field(
        description="Whether the interrupted wait verified afterwards. False means exhausted."
    )
    duration_ms: int = Field(ge=0, description="Time spent recovering.")


class HumanIntervention(ResultModel):
    type: Literal["human"] = Field(description="A run paused for a human.")
    step_id: StepId = Field(description="Step the run was paused at.")
    intervention_id: str = Field(description="Id of the intervention request.")
    reason_code: str = Field(description="Why the run paused.")
    outcome: Literal["handed_back", "aborted", "expired"] = Field(
        description="How the pause ended: control returned, the operator aborted, or nobody came."
    )
    operator_id: str | None = Field(
        default=None, description="Operator who took control, if anyone did."
    )
    operator_note: str = Field(default="", description="What the operator said they did, redacted.")
    human_action_count: int = Field(
        ge=0, description="Captured human actions, see human_actions.jsonl."
    )
    reverified: bool = Field(description="Whether the step checkpoint passed after hand-back.")
    taken_at: datetime | None = Field(default=None, description="When the human took control.")
    handed_back_at: datetime | None = Field(
        default=None, description="When control returned to automation."
    )

    @model_validator(mode="after")
    def _outcome_shape(self) -> Self:
        handed_back = self.outcome == "handed_back"
        if handed_back != (self.handed_back_at is not None):
            raise ValueError("handed_back_at is required for handed_back and only for handed_back")
        if handed_back and (self.taken_at is None or self.operator_id is None):
            raise ValueError("a hand-back needs taken_at and operator_id")
        if (self.taken_at is None) != (self.operator_id is None):
            raise ValueError("taken_at and operator_id go together")
        if self.reverified and not handed_back:
            raise ValueError("only a handed-back run can be reverified")
        if self.taken_at and self.handed_back_at and self.handed_back_at < self.taken_at:
            raise ValueError("handed_back_at is before taken_at")
        return self


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
    identity_changed: bool = Field(
        default=False,
        description="The winner's name or text differs from the fingerprint, e.g. Find became "
        "Search. Replay acts on it for safe steps and refuses irreversible ones.",
    )


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


class FieldError(ResultModel):
    field: ParamName = Field(description="Declared input the app rejected or could not find.")
    message: str = Field(description="The app's own message, redacted.")


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
        default=None,
        description="Why a non-success run ended: an engine code or one the artifact declares. "
        "Null on success statuses.",
    )
    message: str | None = Field(default=None, description="Human-readable explanation, redacted.")
    field_errors: list[FieldError] = Field(
        default_factory=list,
        description="Per-input problems a calling agent can fix; only on business_outcome.",
    )
    step_reached: StepId | None = Field(
        default=None,
        description="Last step started. Null only when the run failed before step 1 with a "
        "pre-run code such as INPUT_INVALID.",
    )
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
        succeeded = self.status in SUCCESS_STATUSES
        if succeeded and self.outcome_code is not None:
            raise ValueError(f"{self.status} cannot carry outcome_code {self.outcome_code}")
        if not succeeded and self.outcome_code is None:
            raise ValueError(f"{self.status} requires an outcome_code")
        if not succeeded and self.outputs:
            raise ValueError(f"{self.status} cannot return outputs")
        if self.field_errors and self.status != "business_outcome":
            raise ValueError("field_errors belong to business_outcome results only")
        pre_run = self.outcome_code in PRE_RUN_CODES
        if pre_run and (self.step_reached is not None or self.status != "hard_failure"):
            raise ValueError(f"{self.outcome_code} is a pre-run hard_failure with no step_reached")
        if self.step_reached is None and not pre_run:
            raise ValueError("step_reached may be null only for a pre-run failure code")
        if self.status == "success" and self.recoveries:
            raise ValueError("a run with recoveries is recovered_then_success, not success")
        if self.status == "recovered_then_success" and not any(
            r.succeeded if isinstance(r, AutomaticRecovery) else r.reverified
            for r in self.recoveries
        ):
            raise ValueError("recovered_then_success requires at least one successful recovery")
        if self.status in ("hard_failure", "escalated") and not (self.expected and self.observed):
            raise ValueError(f"{self.status} must say what was expected and what was observed")
        if (self.status == "escalated") != (self.intervention_path is not None):
            raise ValueError("intervention_path is required for escalated and only for escalated")
        if self.started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        return self

    def contract_problems(self, capability: Capability) -> list[str]:
        """Where this result disagrees with the capability it claims to come from."""
        problems: list[str] = []
        meta = capability.capability
        if (self.capability_id, self.capability_version) != (meta.id, meta.version):
            problems.append(f"result is for {self.capability_id}@{self.capability_version}")
        declared = capability.outcome_codes
        code = self.outcome_code
        if code is not None and code not in ENGINE_CODES:
            kind = declared.get(code)
            if kind is None:
                problems.append(f"outcome_code {code} is neither an engine code nor declared")
            elif (self.status == "business_outcome") != (kind == "business_outcome"):
                problems.append(f"status {self.status} does not fit {code}, a {kind}")
        if self.status == "business_outcome" and code in ENGINE_CODES:
            problems.append(f"engine code {code} is never a business outcome")
        specs = {o.name: o for o in capability.outputs}
        for name, value in self.outputs.items():
            spec = specs.get(name)
            if spec is None:
                problems.append(f"output {name} is not declared")
            elif type(value) is not _OUTPUT_PYTHON_TYPE[spec.type]:
                problems.append(f"output {name} should be {spec.type}")
        if self.status in SUCCESS_STATUSES:
            missing = sorted(set(capability.success.requires_outputs) - set(self.outputs))
            if missing:
                problems.append(f"success is missing required outputs {missing}")
        input_names = {i.name for i in capability.inputs}
        problems += [
            f"field error names undeclared input {e.field}"
            for e in self.field_errors
            if e.field not in input_names
        ]
        detector_ids = {d.id for d in capability.outcome_detectors}
        problems += [
            f"recovery names unknown detector {r.detector_id}"
            for r in self.recoveries
            if isinstance(r, AutomaticRecovery) and r.detector_id not in detector_ids
        ]
        return problems

    def for_evidence(self, capability: Capability) -> dict[str, Any]:
        """Evidence JSON: sensitive inputs and outputs masked, everything else kept."""
        data = self.model_dump(mode="json")
        for spec in capability.outputs:
            if spec.sensitive and spec.name in data["outputs"]:
                data["outputs"][spec.name] = REDACTED
        for param in capability.inputs:
            if param.sensitive and param.name in data["inputs"]:
                data["inputs"][param.name] = REDACTED
        return data


def outputs_digest(outputs: dict[str, OutputValue], key: bytes) -> str:
    """Keyed hash of the outputs. The key lives only in memory for one report, so a digest of a
    four-digit balance cannot be reversed by hashing every candidate."""
    canonical = json.dumps(
        {
            k: v.model_dump(mode="json") if isinstance(v, BaseModel) else v
            for k, v in outputs.items()
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hmac.new(key, canonical.encode(), hashlib.sha256).hexdigest()


class IterationSummary(ResultModel):
    run_id: str = Field(description="Run id of this iteration.")
    status: ReplayStatus = Field(description="Status of this iteration.")
    outcome_code: str | None = Field(default=None, description="Outcome code, if any.")
    rungs: dict[StepId, str] = Field(description="step_id -> resolved strategy.")
    outputs_digest: str = Field(
        description="outputs_digest() under the report's in-memory key, so equality is visible "
        "without values."
    )
    duration_ms: int = Field(ge=0, description="Wall time.")
    conditions: dict[str, str] = Field(
        default_factory=dict,
        description="Environment facts the harness set for this iteration, e.g. the fault set it "
        "re-armed before the run, so an intended difference is never read as flakiness.",
    )


Determinism = Literal["deterministic", "nondeterministic", "insufficient_repeats"]


class StabilityReport(ResultModel):
    capability_id: str = Field(description="Capability replayed.")
    capability_version: str = Field(description="Version replayed.")
    expected_status: Literal["success", "business_outcome"] = Field(
        default="success",
        description="What a passing iteration looks like for these inputs. success also accepts "
        "recovered_then_success.",
    )
    iterations: list[IterationSummary] = Field(min_length=1, description="One entry per run.")

    @property
    def passes(self) -> int:
        wanted = SUCCESS_STATUSES if self.expected_status == "success" else {"business_outcome"}
        return sum(1 for i in self.iterations if i.status in wanted)

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
    def determinism(self) -> Determinism:
        """Same status, code, rungs, and outputs across iterations run under equal conditions.

        A conditions group with one iteration proves nothing, so it cannot vouch for determinism.
        """
        groups: dict[tuple[tuple[str, str], ...], list[tuple[object, ...]]] = {}
        for i in self.iterations:
            key = tuple(sorted(i.conditions.items()))
            shape = (i.status, i.outcome_code, tuple(sorted(i.rungs.items())), i.outputs_digest)
            groups.setdefault(key, []).append(shape)
        if any(len(set(shapes)) > 1 for shapes in groups.values()):
            return "nondeterministic"
        if any(len(shapes) < 2 for shapes in groups.values()):
            return "insufficient_repeats"
        return "deterministic"
