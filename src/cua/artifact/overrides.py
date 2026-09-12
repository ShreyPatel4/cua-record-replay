"""Tenant overrides: resolve one base artifact into the concrete capability a given tenant runs.

Objects deep-merge; lists replace whole, since a locator ladder is one unit and is never spliced.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from cua.artifact.schema import Capability, ClickStep, SelectOptionStep, TypeTextStep, WebSurface


class OverrideError(ValueError):
    pass


def deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _credential_steps(capability: Capability) -> set[str]:
    return {
        s.id
        for s in capability.steps
        if isinstance(s, ClickStep | TypeTextStep | SelectOptionStep) and s.target.looks_sensitive
    }


def resolve_for_tenant(capability: Capability, tenant: str | None) -> Capability:
    """The base capability for tenant None; otherwise the base with that tenant's patch applied.

    The result carries no tenant_overrides, so it can never be resolved twice. A target that looks
    like a credential field in the base must still look like one after the patch.
    """
    if tenant is None:
        return capability
    override = capability.tenant_overrides.get(tenant)
    if override is None:
        known = sorted(capability.tenant_overrides)
        raise OverrideError(f"no overrides for tenant {tenant!r}; known tenants: {known}")

    data = capability.model_dump(mode="json")
    positions = {step["id"]: index for index, step in enumerate(data["steps"])}
    for step_id, patch in override.steps.items():
        data["steps"][positions[step_id]] = deep_merge(data["steps"][positions[step_id]], patch)
    for checkpoint_id, patch in override.checkpoints.items():
        data["checkpoints"][checkpoint_id] = deep_merge(data["checkpoints"][checkpoint_id], patch)
    if override.entry_url is not None:
        if not isinstance(capability.surface, WebSurface):
            raise OverrideError("entry_url overrides only apply to web surfaces")
        data["surface"]["entry_url"] = override.entry_url
    data["tenant_overrides"] = {}

    try:
        resolved = Capability.model_validate(data)
    except ValidationError as exc:
        raise OverrideError(
            f"tenant {tenant!r} override produces an invalid capability: {exc}"
        ) from exc
    lost = sorted(_credential_steps(capability) - _credential_steps(resolved))
    if lost:
        raise OverrideError(
            f"tenant {tenant!r} override makes credential targets look ordinary: {lost}"
        )
    return resolved
