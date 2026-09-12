"""ReplayResult contract: status, outcome code, outputs, and exit code cannot contradict each other.

Also the stability report's determinism signal, which must not read an intended difference as flaky.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from cua.artifact.catalog import load_capability
from cua.policy.redact import REDACTED
from cua.replay.result import (
    DateValue,
    HumanIntervention,
    IterationSummary,
    MoneyValue,
    ReplayResult,
    StabilityReport,
    outputs_digest,
)

from support import ROOT

NOW = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
EXAMPLE = ROOT / "artifacts" / "example.capability.json"
BALANCE = MoneyValue(amount=Decimal("4210.55"), currency="USD")
FAILURE = {"outputs": {}, "expected": "cp_member_loaded", "observed": "Internal Server Error"}


def _recovery(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "type": "automatic",
        "step_id": "s06",
        "detector_id": "od_system_notice",
        "code": "INTERSTITIAL",
        "recovery_kind": "click",
        "attempts": 1,
        "succeeded": True,
        "duration_ms": 120,
    }
    fields.update(overrides)
    return fields


def _result(**overrides: Any) -> ReplayResult:
    fields: dict[str, Any] = {
        "status": "success",
        "capability_id": "coreledger.member.read_savings_balance",
        "capability_version": "1.0.0",
        "run_id": "replay_20260912T150000Z_ab12",
        "started_at": NOW,
        "duration_ms": 2100,
        "inputs": {"member_id": "*0007"},
        "outputs": {"savings_balance": BALANCE},
        "step_reached": "s06",
        "evidence_dir": "evidence/replay_20260912T150000Z_ab12",
    }
    fields.update(overrides)
    return ReplayResult(**fields)


def test_member_not_found_is_an_answer_so_it_exits_zero() -> None:
    result = _result(
        status="business_outcome",
        outputs={},
        outcome_code="MEMBER_NOT_FOUND",
        message="No member found for member number *9999.",
        field_errors=[
            {"field": "member_id", "message": "No member found for member number *9999."}
        ],
    )
    assert result.exit_code == 0
    assert result.field_errors[0].field == "member_id"


def test_exit_codes() -> None:
    assert _result().exit_code == 0
    assert _result(status="hard_failure", outcome_code="APP_ERROR", **FAILURE).exit_code == 2
    escalated = _result(
        status="escalated",
        outcome_code="APP_ERROR",
        intervention_path="evidence/x/intervention.json",
        **FAILURE,
    )
    assert escalated.exit_code == 3


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"outcome_code": "MEMBER_NOT_FOUND"}, "cannot carry outcome_code"),
        ({"status": "business_outcome"}, "requires an outcome_code"),
        (
            {"status": "business_outcome", "outcome_code": "MEMBER_NOT_FOUND"},
            "cannot return outputs",
        ),
        ({"status": "recovered_then_success"}, "at least one successful recovery"),
        (
            {"status": "recovered_then_success", "recoveries": [_recovery(succeeded=False)]},
            "at least one successful recovery",
        ),
        (
            {"status": "hard_failure", "outcome_code": "APP_ERROR", "outputs": {}},
            "what was expected and what was observed",
        ),
        (
            {
                "status": "escalated",
                "outcome_code": "APP_ERROR",
                "outputs": {},
                "expected": "e",
                "observed": "o",
            },
            "intervention_path is required",
        ),
        (
            {
                "status": "hard_failure",
                "outcome_code": "APP_ERROR",
                "field_errors": [{"field": "member_id", "message": "m"}],
                **FAILURE,
            },
            "field_errors belong to business_outcome",
        ),
        ({"step_reached": None}, "only for a pre-run failure code"),
        (
            {"status": "hard_failure", "outcome_code": "INPUT_INVALID", **FAILURE},
            "pre-run hard_failure with no step_reached",
        ),
        ({"started_at": datetime(2026, 9, 12, 15, 0)}, "timezone-aware"),
    ],
)
def test_contradictory_results_cannot_be_built(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _result(**overrides)


def test_a_run_that_fails_before_step_one_has_no_step() -> None:
    result = _result(
        status="hard_failure",
        outcome_code="INPUT_INVALID",
        step_reached=None,
        expected="member_id matching ^[0-9]{5}$",
        observed="member_id='12ab'",
        outputs={},
    )
    assert result.exit_code == 2


def test_success_with_recoveries_must_say_so() -> None:
    with pytest.raises(ValidationError, match="recovered_then_success"):
        _result(recoveries=[_recovery()])
    assert _result(status="recovered_then_success", recoveries=[_recovery()]).exit_code == 0


def test_exhausted_recovery_rides_along_on_the_failure() -> None:
    result = _result(
        status="hard_failure",
        outcome_code="SLOW_LOAD",
        recoveries=[
            _recovery(detector_id="od_slow_member_load", code="SLOW_LOAD", succeeded=False)
        ],
        **FAILURE,
    )
    assert result.contract_problems(load_capability(EXAMPLE)) == []


def _human(**overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "type": "human",
        "step_id": "s06",
        "intervention_id": "int_1",
        "reason_code": "HARD_FAILURE_ESCALATE",
        "outcome": "handed_back",
        "operator_id": "op-shrey",
        "operator_note": "cleared the fault and reopened the member",
        "human_action_count": 4,
        "reverified": True,
        "taken_at": NOW,
        "handed_back_at": NOW,
    }
    fields.update(overrides)
    return fields


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"handed_back_at": None}, "required for handed_back"),
        ({"outcome": "aborted"}, "only for handed_back"),
        (
            {"outcome": "expired", "handed_back_at": None},
            "only a handed-back run can be reverified",
        ),
        ({"operator_id": None}, "needs taken_at and operator_id"),
    ],
)
def test_human_intervention_shapes(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        HumanIntervention.model_validate(_human(**overrides))


def test_an_abort_before_anyone_took_control_is_recordable() -> None:
    record = HumanIntervention.model_validate(
        _human(
            outcome="aborted",
            operator_id=None,
            taken_at=None,
            handed_back_at=None,
            reverified=False,
            human_action_count=0,
        )
    )
    assert record.taken_at is None


def test_json_round_trip_keeps_money_and_dates_exact_and_unions_typed() -> None:
    original = _result(
        status="recovered_then_success",
        outputs={"savings_balance": BALANCE, "joined": DateValue(value=date(2012, 9, 5))},
        recoveries=[_human()],
        warnings=[
            {
                "type": "drift",
                "step_id": "s06",
                "target_ref": "step",
                "recorded_rung": 0,
                "resolved_rung": 1,
                "resolved_strategy": "anchor_relative",
            }
        ],
    )
    text = original.model_dump_json()
    assert '"amount":"4210.55"' in text
    assert ReplayResult.model_validate_json(text) == original


def test_results_are_checked_against_the_capability_that_produced_them() -> None:
    capability = load_capability(EXAMPLE)
    assert _result().contract_problems(capability) == []
    assert _result(outputs={}).contract_problems(capability) == [
        "success is missing required outputs ['savings_balance']"
    ]
    wrong_type = _result(outputs={"savings_balance": "$4,210.55"})
    assert wrong_type.contract_problems(capability) == ["output savings_balance should be money"]
    undeclared = _result(status="hard_failure", outcome_code="BRANCH_CLOSED", **FAILURE)
    assert "neither an engine code nor declared" in undeclared.contract_problems(capability)[0]
    misfiled = _result(status="business_outcome", outputs={}, outcome_code="SESSION_EXPIRED")
    assert "does not fit SESSION_EXPIRED" in misfiled.contract_problems(capability)[0]


def test_evidence_copy_masks_sensitive_outputs() -> None:
    evidence = _result().for_evidence(load_capability(EXAMPLE))
    assert evidence["outputs"] == {"savings_balance": REDACTED}
    assert evidence["inputs"] == {"member_id": "*0007"}


def test_outputs_digest_is_keyed() -> None:
    outputs: dict[str, Any] = {"savings_balance": BALANCE}
    assert outputs_digest(outputs, b"k1") == outputs_digest(outputs, b"k1")
    assert outputs_digest(outputs, b"k1") != outputs_digest(outputs, b"k2")


def _iteration(
    run: int,
    rungs: dict[str, str],
    conditions: dict[str, str] | None = None,
    status: str = "success",
) -> IterationSummary:
    return IterationSummary(
        run_id=f"r{run}",
        status=status,  # type: ignore[arg-type]
        outcome_code="MEMBER_NOT_FOUND" if status == "business_outcome" else None,
        rungs=rungs,
        outputs_digest="abc",
        duration_ms=1000 + run * 10,
        conditions=conditions or {},
    )


def _report(iterations: list[IterationSummary]) -> StabilityReport:
    return StabilityReport(capability_id="c.a.b", capability_version="1.0.0", iterations=iterations)


def test_identical_iterations_are_deterministic() -> None:
    report = _report([_iteration(i, {"s06": "text_exact"}) for i in range(5)])
    assert report.determinism == "deterministic"
    assert report.passes == 5
    assert report.rung_distribution == {"s06": {"text_exact": 5}}
    assert report.duration_spread_ms == (1000, 1020, 1040)


def test_rung_changes_under_the_same_conditions_are_not_deterministic() -> None:
    report = _report(
        [_iteration(0, {"s06": "text_exact"}), _iteration(1, {"s06": "anchor_relative"})]
    )
    assert report.determinism == "nondeterministic"


def test_differences_explained_by_conditions_are_not_flakiness() -> None:
    drift = {"faults": "layout_drift"}
    clean = {"faults": "none"}
    report = _report(
        [
            _iteration(0, {"s06": "anchor_relative"}, drift),
            _iteration(1, {"s06": "anchor_relative"}, drift),
            _iteration(2, {"s06": "text_exact"}, clean),
            _iteration(3, {"s06": "text_exact"}, clean),
        ]
    )
    assert report.determinism == "deterministic"


def test_a_condition_seen_once_cannot_vouch_for_determinism() -> None:
    report = _report(
        [
            _iteration(0, {"s06": "anchor_relative"}, {"faults": "layout_drift"}),
            _iteration(1, {"s06": "text_exact"}, {"faults": "none"}),
            _iteration(2, {"s06": "text_exact"}, {"faults": "none"}),
        ]
    )
    assert report.determinism == "insufficient_repeats"


def test_a_business_outcome_is_not_a_pass_when_success_was_expected() -> None:
    iterations = [_iteration(0, {}), _iteration(1, {}, status="business_outcome")]
    assert _report(iterations).passes == 1
    expected_not_found = StabilityReport(
        capability_id="c.a.b",
        capability_version="1.0.0",
        expected_status="business_outcome",
        iterations=iterations,
    )
    assert expected_not_found.passes == 1
