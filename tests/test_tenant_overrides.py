"""Tenant overrides: one base artifact, specialized per tenant without re-recording.

Pins merge precedence: override beats base, objects deep-merge, ladders replace whole, base intact.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cua.artifact.catalog import load_capability
from cua.artifact.overrides import OverrideError, resolve_for_tenant
from cua.artifact.schema import Capability, ClickStep, TextExactRung, TextPresent, WebSurface

from support import ROOT

EXAMPLE = ROOT / "artifacts" / "example.capability.json"

SEARCH_LADDER = [
    {"strategy": "text_exact", "text": "Search", "confidence": 0.9},
    {
        "strategy": "anchor_relative",
        "anchor_text": "Member number",
        "direction": "right",
        "same_row": True,
        "target_kind": "clickable",
        "nth": 1,
        "confidence": 0.8,
    },
]


def _with_overrides(overrides: dict[str, Any]) -> Capability:
    data: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    data["tenant_overrides"] = overrides
    return Capability.model_validate(data)


@pytest.fixture
def capability() -> Capability:
    return _with_overrides(
        {
            "harbor-fcu": {
                "description": "Build 4.2.2: renamed search action, own host, rebranded sign-in.",
                "entry_url": "http://harbor.example.test:5050/",
                "steps": {"s06": {"target": {"ladder": SEARCH_LADDER, "recorded_rung": 0}}},
                "checkpoints": {
                    "cp_signin_visible": {"condition": {"text": "Harbor FCU operator sign-in"}}
                },
            }
        }
    )


def test_override_wins_and_lists_replace_whole(capability: Capability) -> None:
    resolved = resolve_for_tenant(capability, "harbor-fcu")
    step = next(s for s in resolved.steps if s.id == "s06")
    assert isinstance(step, ClickStep)
    assert len(step.target.ladder) == 2, (
        "ladder is replaced, not spliced onto the base's three rungs"
    )
    assert isinstance(step.target.ladder[0], TextExactRung)
    assert step.target.ladder[0].text == "Search"
    assert step.target.notes == next(s for s in capability.steps if s.id == "s06").target.notes  # type: ignore[union-attr]


def test_objects_deep_merge(capability: Capability) -> None:
    condition = (
        resolve_for_tenant(capability, "harbor-fcu").checkpoints["cp_signin_visible"].condition
    )
    assert isinstance(condition, TextPresent)
    assert condition.text == "Harbor FCU operator sign-in"
    assert condition.frame_path == ["main"], "sibling keys the patch did not mention survive"


def test_entry_url_and_untouched_steps(capability: Capability) -> None:
    resolved = resolve_for_tenant(capability, "harbor-fcu")
    assert isinstance(resolved.surface, WebSurface)
    assert resolved.surface.entry_url == "http://harbor.example.test:5050/"
    assert resolved.steps[:5] == capability.steps[:5]
    assert resolved.tenant_overrides == {}, "a resolved capability cannot be resolved twice"


def test_base_is_not_mutated(capability: Capability) -> None:
    before = capability.model_dump_json()
    resolve_for_tenant(capability, "harbor-fcu")
    assert capability.model_dump_json() == before


def test_no_tenant_means_the_base(capability: Capability) -> None:
    assert resolve_for_tenant(capability, None) is capability


def test_unknown_tenant_is_an_error(capability: Capability) -> None:
    with pytest.raises(OverrideError, match="known tenants: \\['harbor-fcu'\\]"):
        resolve_for_tenant(capability, "maple-cu")


def test_an_override_that_breaks_the_artifact_fails_at_load(tmp_path: Any) -> None:
    broken = _with_overrides(
        {"harbor-fcu": {"description": "d", "steps": {"s06": {"target": {"recorded_rung": 9}}}}}
    )
    with pytest.raises(OverrideError, match="invalid capability"):
        resolve_for_tenant(broken, "harbor-fcu")
    path = tmp_path / "broken.capability.json"
    path.write_text(broken.model_dump_json(), encoding="utf-8")
    with pytest.raises(OverrideError):
        load_capability(path)
