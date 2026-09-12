"""Artifact schema: the example round-trips byte for byte, and each rule rejects bad data.

Every negative test mutates the hand-written example, so each one shows exactly what a rule forbids.
"""

from __future__ import annotations

import json
import re
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


def _detector(data: dict[str, Any], detector_id: str) -> dict[str, Any]:
    return next(d for d in data["outcome_detectors"] if d["id"] == detector_id)


PIN_INPUT = {
    "name": "pin",
    "type": "string",
    "description": "Member PIN.",
    "required": True,
    "sensitive": True,
}


def test_credential_secret_needs_a_credential_looking_target(data: dict[str, Any]) -> None:
    _step(data, "s03")["target"] = _step(data, "s05")["target"]
    _rejects(data, "does not look like a credential field")


def test_identity_secret_may_go_into_an_ordinary_field(data: dict[str, Any]) -> None:
    capability = Capability.model_validate(data)
    operator_step = next(s for s in capability.steps if s.id == "s02")
    assert isinstance(operator_step, schema.TypeTextStep)
    assert not operator_step.target.looks_sensitive


def test_password_input_type_marks_a_target_whatever_its_label(data: dict[str, Any]) -> None:
    _step(data, "s05")["target"]["fingerprint"]["input_type"] = "password"
    _rejects(data, "types into a credential field")


def test_an_anchor_only_credential_target_still_counts(data: dict[str, Any]) -> None:
    step = _step(data, "s03")
    step["target"] = {
        "ladder": [
            {
                "strategy": "anchor_relative",
                "anchor_text": "Password",
                "direction": "right",
                "same_row": True,
                "target_kind": "input",
                "nth": 1,
                "confidence": 0.7,
            }
        ],
        "recorded_rung": 0,
        "frame_path": ["main"],
        "fingerprint": {"role": "textbox"},
        "notes": "n",
    }
    step["value"] = "hunter2-literal"
    _rejects(data, "types into a credential field")


def test_a_credential_target_never_records_its_text(data: dict[str, Any]) -> None:
    _step(data, "s03")["target"]["fingerprint"]["text"] = "hunter2"
    _rejects(data, "must not record its text")


def _select_pin(data: dict[str, Any]) -> None:
    step = _step(data, "s05")
    step.update(action="select_option", option_label="{{inputs.pin}}")
    del step["value"], step["clear_first"]


@pytest.mark.parametrize(
    ("place", "match"),
    [
        ("navigate_url", "URLs are logged"),
        ("checkpoint_text", "conditions are logged"),
        ("detector_url_pattern", "conditions are logged"),
        ("mixed_value", "must be the whole value"),
        ("fingerprint_text", "must not record text"),
        ("option_label", "option label"),
    ],
)
def test_a_sensitive_input_never_lands_in_the_artifact(
    data: dict[str, Any], place: str, match: str
) -> None:
    """Every field that is logged, screenshotted, or stored verbatim refuses a sensitive input."""
    data["inputs"].append(PIN_INPUT)
    if place == "navigate_url":
        _step(data, "s01")["url"] = "{{surface.entry_url}}?q={{inputs.pin}}"
    elif place == "checkpoint_text":
        data["checkpoints"]["cp_member_loaded"]["condition"]["conditions"][1]["text"] = (
            "{{inputs.pin}}"
        )
    elif place == "detector_url_pattern":
        _detector(data, "od_session_expired")["when"]["conditions"][0]["pattern"] = (
            "/login/{{inputs.pin}}"
        )
    elif place == "mixed_value":
        _step(data, "s05")["value"] = "{{inputs.member_id}} {{inputs.pin}}"
    elif place == "fingerprint_text":
        step = _step(data, "s05")
        step["value"] = "{{inputs.pin}}"
        step["target"]["fingerprint"]["text"] = "4821"
    else:
        _select_pin(data)
    _rejects(data, match)


def test_conditions_may_only_template_declared_inputs(data: dict[str, Any]) -> None:
    conditions = data["checkpoints"]["cp_member_loaded"]["condition"]["conditions"]
    conditions[1]["text"] = "{{secrets.operator_id}}"
    _rejects(data, "may only template declared inputs")


def test_accepting_a_confirm_must_be_declared_irreversible(data: dict[str, Any]) -> None:
    step = _step(data, "s06")
    step["dialog"] = {"dialog_type": "confirm", "message_pattern": "Sure\\?", "response": "accept"}
    _rejects(data, "must declare risk irreversible")
    step["dialog"]["response"] = "dismiss"
    Capability.model_validate(data)


def test_run_steps_recovery_needs_an_explicit_scope(data: dict[str, Any]) -> None:
    _detector(data, "od_session_expired")["scope"] = None
    _rejects(data, "needs an explicit scope")


def test_run_steps_cannot_rerun_the_steps_it_protects(data: dict[str, Any]) -> None:
    _detector(data, "od_session_expired")["outcome"]["recovery"]["step_ids"] = ["s02", "s06"]
    _rejects(data, "before the detector's scope")


def test_run_steps_never_repeats_an_irreversible_step(data: dict[str, Any]) -> None:
    _step(data, "s04")["risk"] = "irreversible"
    _rejects(data, "may not re-run step s04")


def test_run_steps_scope_must_wait_on_a_checkpoint(data: dict[str, Any]) -> None:
    _detector(data, "od_session_expired")["scope"] = ["s05"]
    _rejects(data, "needs a checkpoint wait")


def test_timeout_detector_scope_must_wait_on_that_checkpoint(data: dict[str, Any]) -> None:
    _detector(data, "od_slow_member_load")["scope"] = ["s04"]
    _rejects(data, "does not wait on that checkpoint")


def test_business_outcome_field_must_be_a_declared_input(data: dict[str, Any]) -> None:
    _detector(data, "od_member_not_found")["outcome"]["field"] = "no_such_field"
    _rejects(data, "undeclared input 'no_such_field'")


def test_business_outcome_about_a_field_needs_a_message(data: dict[str, Any]) -> None:
    del _detector(data, "od_member_not_found")["outcome"]["message_from"]
    _rejects(data, "needs message_from")


def test_detectors_cannot_reuse_engine_codes(data: dict[str, Any]) -> None:
    _detector(data, "od_app_error")["outcome"]["code"] = "TARGET_NOT_FOUND"
    _rejects(data, "reuses engine code")


def test_one_code_has_one_meaning(data: dict[str, Any]) -> None:
    _detector(data, "od_access_denied")["outcome"] = {
        "type": "hard_failure",
        "code": "MEMBER_NOT_FOUND",
        "escalate": False,
    }
    _rejects(data, "is both")


def test_a_sensitive_output_never_records_its_value(data: dict[str, Any]) -> None:
    data["outputs"][0]["extract"]["target"]["fingerprint"]["text"] = "$4,210.55"
    _rejects(data, "must not record the value")


@pytest.mark.parametrize(
    ("patch", "locked"),
    [
        ({"s03": {"value": "hunter2-literal"}}, "value"),
        (
            {
                "s06": {
                    "dialog": {"dialog_type": "alert", "message_pattern": ".", "response": "accept"}
                }
            },
            "dialog",
        ),
        (
            {"s03": {"target": {"fingerprint": {"input_type": "text"}}}},
            "target.fingerprint.input_type",
        ),
        ({"s04": {"target": {"fingerprint": {"role": "link"}}}}, "target.fingerprint.role"),
        ({"s06": {"on_fail": "escalate"}}, "on_fail"),
        ({"s01": {"url": "http://evil.example/"}}, "url"),
    ],
)
def test_tenant_patches_cannot_touch_locked_fields(
    data: dict[str, Any], patch: dict[str, Any], locked: str
) -> None:
    data["tenant_overrides"] = {"maple-cu": {"description": "d", "steps": patch}}
    _rejects(data, re.escape(f"'{locked}'"))
