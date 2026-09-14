"""Policy fit: does a capability stay inside the policy it names, before any browser opens.

Checked at load for the base and every tenant, so an off-policy entry_url never reaches replay.
"""

from __future__ import annotations

from cua.artifact.schema import (
    Capability,
    ClickStep,
    NavigateStep,
    PressKeyStep,
    RoleNameRung,
    SelectOptionStep,
    Step,
    TextExactRung,
    TypeTextStep,
    WebSurface,
)
from cua.policy.gate import PolicyGate
from cua.policy.models import Policy, ProposedAction
from cua.vocab import RiskClass, template_refs


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
        rated = _rated_risk(gate, step, entry_url)
        if rated == "irreversible" and step.risk != "irreversible":
            errors.append(
                f"step {step.id} is declared {step.risk}, but policy {policy.id} rates it "
                "irreversible; declare it irreversible so replay never retries it"
            )
    return errors


def _rated_risk(gate: PolicyGate, step: Step, entry_url: str) -> RiskClass | None:
    """The risk the gate gives a step from what the artifact records about it: the action, the
    target's recorded name or text, the key, and the dialog answer. Page-dependent rules are
    checked again at run time, where replay uses the higher of declared and computed risk."""
    target_text = None
    target = getattr(step, "target", None)
    if target is not None:
        fingerprint = target.fingerprint
        texts = [
            r.name if isinstance(r, RoleNameRung) else r.text
            for r in target.ladder
            if isinstance(r, RoleNameRung | TextExactRung)
        ]
        target_text = fingerprint.name or fingerprint.text or next(iter(texts), None)
    navigate_url = None
    if isinstance(step, NavigateStep):
        navigate_url = step.url.replace("{{surface.entry_url}}", entry_url)
        if template_refs(navigate_url):
            return None
    dialog = step.dialog if isinstance(step, ClickStep) else None
    proposed = ProposedAction(
        phase="action",
        action=step.action,
        frame_url=entry_url or "about:blank",
        navigate_url=navigate_url,
        target_text=target_text,
        key=step.key if isinstance(step, PressKeyStep) else None,
        dialog_message=dialog.message_pattern if dialog else None,
        dialog_response=dialog.response if dialog else None,
    )
    if not isinstance(
        step, ClickStep | TypeTextStep | SelectOptionStep | PressKeyStep | NavigateStep
    ):
        return None
    return gate.check(proposed).risk
