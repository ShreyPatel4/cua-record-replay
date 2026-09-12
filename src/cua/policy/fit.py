"""Policy fit: does a capability stay inside the policy it names, before any browser opens.

Checked at load for the base and every tenant, so an off-policy entry_url never reaches replay.
"""

from __future__ import annotations

from cua.artifact.schema import Capability, NavigateStep, WebSurface
from cua.policy.gate import PolicyGate
from cua.policy.models import Policy
from cua.vocab import template_refs


def capability_policy_errors(capability: Capability, policy: Policy) -> list[str]:
    errors: list[str] = []
    if capability.capability.policy_ref != policy.id:
        errors.append(
            f"capability names policy {capability.capability.policy_ref}, "
            f"checked against {policy.id}"
        )
    gate = PolicyGate(policy)
    entry_url = capability.surface.entry_url if isinstance(capability.surface, WebSurface) else ""
    if entry_url:
        reason = gate.url_block_reason(entry_url)
        if reason is not None:
            errors.append(f"entry_url is off policy: {reason}")
    for step in capability.steps:
        if step.action not in policy.actions_allow:
            errors.append(f"step {step.id} action {step.action} is not in actions_allow")
        if isinstance(step, NavigateStep) and not any(
            scope == "inputs" for scope, _ in template_refs(step.url)
        ):
            url = step.url.replace("{{surface.entry_url}}", entry_url)
            reason = gate.url_block_reason(url)
            if reason is not None:
                errors.append(f"step {step.id} navigates off policy: {reason}")
    return errors
