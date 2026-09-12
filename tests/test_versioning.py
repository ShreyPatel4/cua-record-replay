"""Semver rules: a contract break needs a major bump, a flow change a minor one, wording a patch.

Each case mutates the example artifact once and checks both classification and enforcement.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest

from cua.artifact.schema import Capability
from cua.artifact.versioning import Bump, VersionError, actual_bump, check_version, required_bump

from support import ROOT

EXAMPLE = ROOT / "artifacts" / "example.capability.json"
Mutation = Callable[[dict[str, Any]], None]


def _base() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    return loaded


def _variant(mutate: Mutation, version: str = "1.0.0") -> Capability:
    data = _base()
    mutate(data)
    data["capability"]["version"] = version
    return Capability.model_validate(data)


def _noop(data: dict[str, Any]) -> None:
    return None


def _reword(data: dict[str, Any]) -> None:
    data["steps"][0]["description"] = "Open the shell."


def _approve(data: dict[str, Any]) -> None:
    data["capability"].update(
        status="approved", approved_by="reviewer", approved_at="2026-09-12T15:00:00Z"
    )


def _shorter_wait(data: dict[str, Any]) -> None:
    data["steps"][5]["wait_for"]["timeout_ms"] = 2500


def _optional_input(data: dict[str, Any]) -> None:
    data["inputs"].append(
        {
            "name": "branch",
            "type": "string",
            "description": "d",
            "required": False,
            "sensitive": False,
        }
    )


def _required_input(data: dict[str, Any]) -> None:
    data["inputs"].append(
        {
            "name": "branch",
            "type": "string",
            "description": "d",
            "required": True,
            "sensitive": False,
        }
    )


def _tighter_pattern(data: dict[str, Any]) -> None:
    data["inputs"][0]["pattern"] = "^1[0-9]{4}$"


def _drop_output(data: dict[str, Any]) -> None:
    data["outputs"] = []
    data["success"]["requires_outputs"] = []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (_noop, "none"),
        (_approve, "none"),
        (_reword, "patch"),
        (_shorter_wait, "minor"),
        (_optional_input, "minor"),
        (_required_input, "major"),
        (_tighter_pattern, "major"),
        (_drop_output, "major"),
    ],
)
def test_change_classification(mutate: Mutation, expected: Bump) -> None:
    assert required_bump(_variant(_noop), _variant(mutate)) == expected


def test_minor_change_with_a_patch_bump_is_refused() -> None:
    with pytest.raises(VersionError, match="needs a minor bump"):
        check_version(_variant(_noop), _variant(_shorter_wait, version="1.0.1"))


def test_minor_change_with_a_minor_bump_is_accepted() -> None:
    assert check_version(_variant(_noop), _variant(_shorter_wait, version="1.1.0")) == "minor"


def test_contract_break_needs_a_major_bump() -> None:
    with pytest.raises(VersionError, match="needs a major bump"):
        check_version(_variant(_noop), _variant(_required_input, version="1.1.0"))
    assert check_version(_variant(_noop), _variant(_required_input, version="2.0.0")) == "major"


def test_versions_cannot_go_backwards() -> None:
    with pytest.raises(VersionError, match="backwards"):
        actual_bump("1.2.0", "1.1.9")


def test_different_capabilities_are_not_comparable() -> None:
    def rename(data: dict[str, Any]) -> None:
        data["capability"]["id"] = "coreledger.member.read_checking_balance"

    with pytest.raises(VersionError, match="different capabilities"):
        required_bump(_variant(_noop), _variant(rename))
