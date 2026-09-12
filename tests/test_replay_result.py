"""ReplayResult contract: status, outcome code, outputs, and exit code cannot contradict each other.

Also the stability report's determinism signal, which must not read an intended difference as flaky.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from cua.replay.result import IterationSummary, MoneyValue, ReplayResult, StabilityReport

NOW = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)


def _result(**overrides: Any) -> ReplayResult:
    fields: dict[str, Any] = {
        "status": "success",
        "capability_id": "coreledger.member.read_savings_balance",
        "capability_version": "1.0.0",
        "run_id": "replay_20260912T150000Z_ab12",
        "started_at": NOW,
        "duration_ms": 2100,
        "inputs": {"member_id": "*0007"},
        "outputs": {"savings_balance": MoneyValue(amount=Decimal("4210.55"), currency="USD")},
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
    )
    assert result.exit_code == 0


def test_exit_codes() -> None:
    assert _result().exit_code == 0
    failure = {"outputs": {}, "expected": "cp_member_loaded", "observed": "Internal Server Error"}
    assert _result(status="hard_failure", outcome_code="APP_ERROR", **failure).exit_code == 2
    escalated = _result(
        status="escalated",
        outcome_code="APP_ERROR",
        intervention_path="evidence/x/intervention.json",
        **failure,
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
        (
            {"status": "recovered_then_success"},
            "requires at least one recovery record",
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
        ({"started_at": datetime(2026, 9, 12, 15, 0)}, "timezone-aware"),
    ],
)
def test_contradictory_results_cannot_be_built(overrides: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        _result(**overrides)


def test_success_with_recoveries_must_say_so() -> None:
    recovery = {
        "type": "automatic",
        "step_id": "s06",
        "detector_id": "od_system_notice",
        "code": "INTERSTITIAL",
        "recovery_kind": "click",
        "attempts": 1,
        "duration_ms": 120,
    }
    with pytest.raises(ValidationError, match="recovered_then_success"):
        _result(recoveries=[recovery])
    assert _result(status="recovered_then_success", recoveries=[recovery]).exit_code == 0


def test_json_round_trip_keeps_money_exact_and_unions_typed() -> None:
    original = _result(
        status="recovered_then_success",
        recoveries=[
            {
                "type": "human",
                "step_id": "s06",
                "intervention_id": "int_1",
                "reason_code": "HARD_FAILURE_ESCALATE",
                "operator_note": "cleared the fault and reopened the member",
                "human_action_count": 4,
                "reverified": True,
                "taken_at": NOW,
                "handed_back_at": NOW,
            }
        ],
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


def _iteration(
    run: int, rungs: dict[str, str], conditions: dict[str, str] | None = None
) -> IterationSummary:
    return IterationSummary(
        run_id=f"r{run}",
        status="success",
        rungs=rungs,
        outputs_digest="abc",
        duration_ms=1000 + run * 10,
        conditions=conditions or {},
    )


def test_identical_iterations_are_deterministic() -> None:
    report = StabilityReport(
        capability_id="c.a.b",
        capability_version="1.0.0",
        iterations=[_iteration(i, {"s06": "text_exact"}) for i in range(5)],
    )
    assert report.deterministic
    assert report.passes == 5
    assert report.rung_distribution == {"s06": {"text_exact": 5}}
    assert report.duration_spread_ms == (1000, 1020, 1040)


def test_rung_changes_under_the_same_conditions_are_not_deterministic() -> None:
    iterations = [_iteration(0, {"s06": "text_exact"}), _iteration(1, {"s06": "anchor_relative"})]
    report = StabilityReport(
        capability_id="c.a.b", capability_version="1.0.0", iterations=iterations
    )
    assert not report.deterministic


def test_differences_explained_by_conditions_are_not_flakiness() -> None:
    iterations = [
        _iteration(0, {"s06": "anchor_relative"}, {"faults": "layout_drift"}),
        _iteration(1, {"s06": "text_exact"}, {"faults": "none"}),
        _iteration(2, {"s06": "text_exact"}, {"faults": "none"}),
    ]
    report = StabilityReport(
        capability_id="c.a.b", capability_version="1.0.0", iterations=iterations
    )
    assert report.deterministic
