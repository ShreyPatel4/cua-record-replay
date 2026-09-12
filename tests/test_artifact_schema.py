"""Artifact schema: the example round-trips byte for byte, and each rule rejects bad data.

Every negative test mutates the hand-written example, so each one shows exactly what a rule forbids.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from cua.artifact import schema
from cua.artifact.catalog import dump_json, load_capability
from cua.artifact.schema import Capability, CheckpointWait, WaitRetryRecovery
from cua.policy import models as policy_models
from cua.replay import result
from cua.session import intervention, state
from mock_app.injections import DEFAULT_SLOW_MS

from support import ROOT

EXAMPLE = ROOT / "artifacts" / "example.capability.json"


@pytest.fixture
def data() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return loaded


def _step(data: dict[str, Any], step_id: str) -> dict[str, Any]:
    return next(s for s in data["steps"] if s["id"] == step_id)


def _rejects(data: dict[str, Any], match: str) -> None:
    with pytest.raises(ValidationError, match=match):
        Capability.model_validate(data)


def test_example_round_trips_byte_for_byte() -> None:
    assert dump_json(load_capability(EXAMPLE)) == EXAMPLE.read_text(encoding="utf-8")


def test_every_contract_field_has_a_description() -> None:
    missing = []
    for module in (schema, result, policy_models, state, intervention):
        for obj in vars(module).values():
            if (
                isinstance(obj, type)
                and issubclass(obj, BaseModel)
                and obj.__module__ == module.__name__
            ):
                missing += [
                    f"{obj.__name__}.{name}"
                    for name, f in obj.model_fields.items()
                    if not f.description
                ]
    assert not missing


def test_slow_path_timing_makes_the_recovery_reachable() -> None:
    """The step wait is shorter than the injected delay, and the delay fits the retry budget."""
    capability = load_capability(EXAMPLE)
    wait = next(s for s in capability.steps if s.id == "s06").wait_for
    assert isinstance(wait, CheckpointWait)
    slow = next(d for d in capability.outcome_detectors if d.id == "od_slow_member_load")
    assert slow.outcome.type == "recoverable"
    recovery = slow.outcome.recovery
    assert isinstance(recovery, WaitRetryRecovery)
    assert wait.timeout_ms < DEFAULT_SLOW_MS < wait.timeout_ms + recovery.max_total_ms


def test_unknown_top_level_field_is_rejected(data: dict[str, Any]) -> None:
    data["notes"] = "sneaky"
    _rejects(data, "Extra inputs are not permitted")


def test_unknown_nested_field_is_rejected(data: dict[str, Any]) -> None:
    _step(data, "s01")["retries"] = 3
    _rejects(data, "Extra inputs are not permitted")


def test_selector_rungs_do_not_exist(data: dict[str, Any]) -> None:
    _step(data, "s05")["target"]["ladder"][0] = {
        "strategy": "css",
        "selector": "input[name=q]",
        "confidence": 1,
    }
    _rejects(data, "css")


def test_ladder_must_be_in_canonical_order(data: dict[str, Any]) -> None:
    ladder = _step(data, "s06")["target"]["ladder"]
    ladder.reverse()
    _rejects(data, "canonical order|ordered role_name")


def test_coordinates_alone_are_not_a_target(data: dict[str, Any]) -> None:
    target = _step(data, "s06")["target"]
    target["ladder"] = [target["ladder"][-1]]
    _rejects(data, "not raw coordinates")


def test_recorded_rung_must_exist(data: dict[str, Any]) -> None:
    _step(data, "s06")["target"]["recorded_rung"] = 3
    _rejects(data, "outside a 3-rung ladder")


def test_wait_for_unknown_checkpoint(data: dict[str, Any]) -> None:
    _step(data, "s04")["wait_for"]["checkpoint"] = "cp_nowhere"
    _rejects(data, "unknown checkpoint 'cp_nowhere'")


def test_template_must_reference_a_declared_input(data: dict[str, Any]) -> None:
    _step(data, "s05")["value"] = "{{inputs.account_number}}"
    _rejects(data, "unknown inputs.account_number")


def test_credential_field_rejects_a_literal_value(data: dict[str, Any]) -> None:
    _step(data, "s03")["value"] = "correct-horse-battery"
    _rejects(data, "credential field")


def test_credential_field_rejects_a_non_sensitive_input(data: dict[str, Any]) -> None:
    _step(data, "s03")["value"] = "{{inputs.member_id}}"
    _rejects(data, "credential field")


def test_secrets_never_go_into_urls(data: dict[str, Any]) -> None:
    _step(data, "s01")["url"] = "{{surface.entry_url}}?pw={{secrets.operator_password}}"
    _rejects(data, "URLs are logged")


def test_sensitive_inputs_never_go_into_urls(data: dict[str, Any]) -> None:
    data["inputs"].append(
        {"name": "ssn", "type": "string", "description": "d", "required": True, "sensitive": True}
    )
    _step(data, "s01")["url"] = "{{surface.entry_url}}?q={{inputs.ssn}}"
    _rejects(data, "URLs are logged")


def test_checkpoint_timeout_is_only_a_detector_trigger(data: dict[str, Any]) -> None:
    data["checkpoints"]["cp_signin_visible"]["condition"] = {
        "kind": "checkpoint_timeout",
        "checkpoint": "cp_member_loaded",
    }
    _rejects(data, "only detectors may")


def test_duplicate_step_ids(data: dict[str, Any]) -> None:
    _step(data, "s02")["id"] = "s01"
    _rejects(data, "duplicate step id")


def test_success_requires_declared_outputs(data: dict[str, Any]) -> None:
    data["success"]["requires_outputs"].append("checking_balance")
    _rejects(data, "undeclared outputs")


def test_detector_scope_must_name_real_steps(data: dict[str, Any]) -> None:
    data["outcome_detectors"][3]["scope"] = ["s99"]
    _rejects(data, "unknown step 's99'")


def test_recovery_steps_must_exist(data: dict[str, Any]) -> None:
    data["outcome_detectors"][0]["outcome"]["recovery"]["step_ids"] = ["s02", "s42"]
    _rejects(data, "unknown step 's42'")


def test_approval_needs_an_approver(data: dict[str, Any]) -> None:
    data["capability"]["status"] = "approved"
    _rejects(data, "approved_by")


def test_created_at_must_be_timezone_aware(data: dict[str, Any]) -> None:
    data["capability"]["created_at"] = "2026-09-12T14:00:00"
    _rejects(data, "timezone-aware")


def test_output_parse_must_match_type(data: dict[str, Any]) -> None:
    data["outputs"][0]["extract"]["parse"] = "text"
    _rejects(data, "must use parse money_usd")


def test_tenant_override_cannot_lower_risk(data: dict[str, Any]) -> None:
    data["tenant_overrides"] = {
        "maple-cu": {"description": "d", "steps": {"s06": {"risk": "safe"}}}
    }
    _rejects(data, r"may not change \['risk'\]")


def test_tenant_override_must_name_real_steps(data: dict[str, Any]) -> None:
    data["tenant_overrides"] = {"maple-cu": {"description": "d", "steps": {"s77": {}}}}
    _rejects(data, "unknown step 's77'")


def test_desktop_surface_rejects_urls(data: dict[str, Any]) -> None:
    data["surface"] = {
        "kind": "desktop",
        "executable": "CoreLedger.exe",
        "window_title_pattern": "^CoreLedger",
        "app_family": "coreledger",
        "app_version_hint": "4.2.1",
    }
    _rejects(data, "desktop surfaces have no URLs")


def test_invalid_regex_is_rejected(data: dict[str, Any]) -> None:
    data["outcome_detectors"][3]["outcome"]["message_from"]["pattern"] = "No member ("
    _rejects(data, "invalid regex")


def test_capability_id_shape(data: dict[str, Any]) -> None:
    data["capability"]["id"] = "ReadBalance"
    _rejects(data, "String should match pattern")
