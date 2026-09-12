"""Semver rules for capabilities: classify a change and check the version bump covers it.

Major: a caller or deployment contract broke. Minor: the flow or an additive contract changed.
"""

from __future__ import annotations

from typing import Any, Literal

from cua.artifact.schema import Capability

Bump = Literal["none", "patch", "minor", "major"]
_ORDER: dict[Bump, int] = {"none": 0, "patch": 1, "minor": 2, "major": 3}
_WORDING_KEYS = frozenset({"description", "notes", "review_notes"})
_VOLATILE_META = frozenset(
    {"version", "status", "approved_by", "approved_at", "created_at", "created_from_run_id"}
)
_FLOW_KEYS = (
    "surface",
    "secrets",
    "inputs",
    "outputs",
    "steps",
    "checkpoints",
    "outcome_detectors",
    "success",
    "tenant_overrides",
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


def contract_breaks(old: Capability, new: Capability) -> list[str]:
    """Changes a calling agent or a deployment would notice. Any one of them needs a major bump."""
    breaks: list[str] = []
    old_inputs = {p.name: p for p in old.inputs}
    new_inputs = {p.name: p for p in new.inputs}
    for name, before in old_inputs.items():
        after = new_inputs.get(name)
        if after is None:
            breaks.append(f"input {name} removed")
            continue
        for field in ("type", "pattern", "enum_values", "sensitive"):
            if getattr(before, field) != getattr(after, field):
                breaks.append(f"input {name} {field} changed")
        if after.required and not before.required:
            breaks.append(f"input {name} became required")
    breaks += [
        f"new required input {n}"
        for n, p in new_inputs.items()
        if p.required and n not in old_inputs
    ]

    new_outputs = {o.name: o for o in new.outputs}
    for output in old.outputs:
        after_output = new_outputs.get(output.name)
        if after_output is None:
            breaks.append(f"output {output.name} removed")
        elif (output.type, output.sensitive) != (after_output.type, after_output.sensitive):
            breaks.append(f"output {output.name} type or sensitivity changed")

    new_codes = new.outcome_codes
    for code, kind in old.outcome_codes.items():
        if new_codes.get(code) != kind:
            breaks.append(f"outcome {code} removed or reclassified")

    def secret_shape(c: Capability) -> dict[str, tuple[str, str]]:
        return {s.name: (s.kind, s.env_var) for s in c.secrets}

    if secret_shape(old) != secret_shape(new):
        breaks.append("secrets or their environment variables changed")
    if old.surface.kind != new.surface.kind:
        breaks.append("surface kind changed")
    if old.capability.policy_ref != new.capability.policy_ref:
        breaks.append("policy_ref changed")
    return breaks


def _comparable(capability: Capability) -> dict[str, Any]:
    dump = capability.model_dump(mode="json")
    for volatile in _VOLATILE_META:
        dump["capability"].pop(volatile, None)
    return dump


def required_bump(old: Capability, new: Capability) -> Bump:
    if old.capability.id != new.capability.id:
        raise VersionError(f"different capabilities: {old.capability.id} vs {new.capability.id}")
    if contract_breaks(old, new):
        return "major"

    old_dump, new_dump = _comparable(old), _comparable(new)
    if any(_strip_wording(old_dump[k]) != _strip_wording(new_dump[k]) for k in _FLOW_KEYS):
        return "minor"
    return "patch" if old_dump != new_dump else "none"


def check_version(old: Capability, new: Capability) -> Bump:
    """Raise unless new.version is a big enough bump for the change. Returns the bump required."""
    required = required_bump(old, new)
    actual = actual_bump(old.capability.version, new.capability.version)
    if _ORDER[actual] < _ORDER[required]:
        detail = "; ".join(contract_breaks(old, new)) if required == "major" else ""
        raise VersionError(
            f"{new.capability.id}: this change needs a {required} bump, but "
            f"{old.capability.version} -> {new.capability.version} is {actual}"
            + (f" ({detail})" if detail else "")
        )
    return required
