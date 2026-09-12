"""Semver rules for capabilities: classify a change and check the version bump covers it.

Major: the caller-facing contract broke. Minor: the flow changed. Patch: only wording changed.
"""

from __future__ import annotations

from typing import Literal

from cua.artifact.schema import Capability

Bump = Literal["none", "patch", "minor", "major"]
_ORDER: dict[Bump, int] = {"none": 0, "patch": 1, "minor": 2, "major": 3}
_WORDING_KEYS = frozenset({"description", "notes", "review_notes"})
_VOLATILE_META = frozenset(
    {"version", "status", "approved_by", "approved_at", "created_at", "created_from_run_id"}
)


class VersionError(ValueError):
    pass


def parse_version(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return major, minor, patch


def actual_bump(old: str, new: str) -> Bump:
    o, n = parse_version(old), parse_version(new)
    if n < o:
        raise VersionError(f"version went backwards: {old} -> {new}")
    if n == o:
        return "none"
    if n[0] > o[0]:
        return "major"
    return "minor" if n[1] > o[1] else "patch"


def _strip_wording(value: object) -> object:
    if isinstance(value, dict):
        return {k: _strip_wording(v) for k, v in value.items() if k not in _WORDING_KEYS}
    if isinstance(value, list):
        return [_strip_wording(v) for v in value]
    return value


def required_bump(old: Capability, new: Capability) -> Bump:
    if old.capability.id != new.capability.id:
        raise VersionError(f"different capabilities: {old.capability.id} vs {new.capability.id}")

    old_inputs = {p.name: p for p in old.inputs}
    new_inputs = {p.name: p for p in new.inputs}
    for name, before in old_inputs.items():
        after = new_inputs.get(name)
        if after is None:
            return "major"
        shape = ("type", "pattern", "enum_values", "sensitive")
        if any(getattr(before, f) != getattr(after, f) for f in shape):
            return "major"
        if after.required and not before.required:
            return "major"
    if any(p.required for n, p in new_inputs.items() if n not in old_inputs):
        return "major"

    old_outputs = {o.name: o.type for o in old.outputs}
    new_outputs = {o.name: o.type for o in new.outputs}
    if any(new_outputs.get(name) != kind for name, kind in old_outputs.items()):
        return "major"

    if set(new_inputs) != set(old_inputs) or set(new_outputs) != set(old_outputs):
        return "minor"
    if any(new_inputs[n].required != old_inputs[n].required for n in old_inputs):
        return "minor"

    old_dump = old.model_dump(mode="json")
    new_dump = new.model_dump(mode="json")
    for key in ("capability",):
        for volatile in _VOLATILE_META:
            old_dump[key].pop(volatile, None)
            new_dump[key].pop(volatile, None)
    if _strip_wording(old_dump) != _strip_wording(new_dump):
        flow_keys = (
            "surface",
            "secrets",
            "steps",
            "checkpoints",
            "outcome_detectors",
            "success",
            "tenant_overrides",
            "outputs",
            "inputs",
        )
        if any(_strip_wording(old_dump[k]) != _strip_wording(new_dump[k]) for k in flow_keys):
            return "minor"
        return "patch"
    return "patch" if old_dump != new_dump else "none"


def check_version(old: Capability, new: Capability) -> Bump:
    """Raise unless new.version is a big enough bump for the change. Returns the bump required."""
    required = required_bump(old, new)
    actual = actual_bump(old.capability.version, new.capability.version)
    if _ORDER[actual] < _ORDER[required]:
        raise VersionError(
            f"{new.capability.id}: this change needs a {required} bump, but "
            f"{old.capability.version} -> {new.capability.version} is {actual}"
        )
    return required
