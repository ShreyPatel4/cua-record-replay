"""Input coercion and template rendering against the example capability's contract.

Every problem in a call is reported together, and sensitive values never appear in the error text.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from cua.artifact.catalog import load_capability
from cua.artifact.inputs import InputError, TemplateError, coerce_inputs, render
from cua.artifact.schema import Capability

from support import ROOT

EXAMPLE = ROOT / "artifacts" / "example.capability.json"


@pytest.fixture(scope="module")
def capability() -> Capability:
    return load_capability(EXAMPLE)


def _with_inputs(extra: list[dict[str, Any]]) -> Capability:
    data: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["inputs"] += extra
    return Capability.model_validate(data)


def test_valid_input_is_coerced(capability: Capability) -> None:
    assert coerce_inputs(capability, {"member_id": "10007"}) == {"member_id": "10007"}


def test_all_problems_are_reported_at_once(capability: Capability) -> None:
    with pytest.raises(InputError) as caught:
        coerce_inputs(capability, {"member": "10007"})
    assert len(caught.value.problems) == 2
    assert "unknown inputs ['member']" in str(caught.value)
    assert "member_id is required" in str(caught.value)


def test_pattern_is_a_full_match(capability: Capability) -> None:
    with pytest.raises(InputError, match="does not match"):
        coerce_inputs(capability, {"member_id": "100071"})


def test_types_and_sensitive_values() -> None:
    capability = _with_inputs(
        [
            {
                "name": "deposit",
                "type": "money",
                "description": "d",
                "required": True,
                "sensitive": False,
            },
            {
                "name": "joint",
                "type": "boolean",
                "description": "d",
                "required": False,
                "sensitive": False,
            },
            {
                "name": "pin",
                "type": "string",
                "description": "d",
                "required": True,
                "sensitive": True,
                "pattern": "^[0-9]{4}$",
            },
        ]
    )
    typed = coerce_inputs(
        capability, {"member_id": "10007", "deposit": "25.5", "joint": "true", "pin": "4321"}
    )
    assert typed["deposit"] == Decimal("25.5")
    assert typed["joint"] is True
    with pytest.raises(InputError) as caught:
        coerce_inputs(capability, {"member_id": "10007", "deposit": "25.555", "pin": "98765"})
    message = str(caught.value)
    assert "at most 2 places" in message
    assert "98765" not in message
    assert "pin=[REDACTED]" in message


def test_render_substitutes_every_scope() -> None:
    rendered = render(
        "{{surface.entry_url}}members/{{inputs.member_id}} {{ inputs.deposit }} {{secrets.token}}",
        inputs={"member_id": "10007", "deposit": Decimal("5")},
        secrets={"token": "t0ken-value"},
        surface={"entry_url": "http://127.0.0.1:5050/"},
    )
    assert rendered == "http://127.0.0.1:5050/members/10007 5.00 t0ken-value"


def test_render_refuses_missing_values() -> None:
    with pytest.raises(TemplateError, match=r"inputs\.member_id"):
        render("{{inputs.member_id}}", inputs={}, secrets={}, surface={})
