"""GatedSurface: the only code that calls Surface.act, so no action anywhere skips the policy gate.

It judges each action before it happens, then judges every navigation and dialog the action causes.
"""

from __future__ import annotations

from typing import Literal

from cua.locate.backend import LocatorBackend
from cua.policy.gate import PolicyGate
from cua.policy.models import Block, PolicyDecision, ProposedAction, RequiresConfirmation
from cua.surface.base import (
    Action,
    ActResult,
    DialogInfo,
    Navigate,
    NavigationRequest,
    Surface,
)
from cua.vocab import ActionType, RiskClass


class GatedSurface:
    """Wraps a surface with a PolicyGate. A repo test fails if Surface.act is called anywhere else.

    confirm_irreversible is the --confirm-irreversible flag: it satisfies RequiresConfirmation under
    `confirm` handling only. `escalate` handling is never satisfied here; the caller must raise an
    intervention.
    """

    def __init__(
        self,
        surface: Surface,
        backend: LocatorBackend,
        gate: PolicyGate,
        *,
        confirm_irreversible: bool = False,
    ) -> None:
        self.surface = surface
        self._backend = backend
        self._gate = gate
        self._confirm = confirm_irreversible
        self._last_action: ActionType = "navigate"
        self._declared: RiskClass = "safe"
        self._target_text: str | None = None
        surface.install_navigation_guard(self._navigation_guard)

    def _refusal(self, decision: PolicyDecision) -> str | None:
        if isinstance(decision, Block):
            return decision.reason
        if isinstance(decision, RequiresConfirmation):
            if decision.handling == "confirm" and self._confirm:
                return None
            return f"requires confirmation ({decision.handling}): {decision.reason}"
        return None

    def perform(self, action: Action, *, declared_risk: RiskClass = "safe") -> ActResult:
        element = getattr(action, "element", None)
        target_text = None
        if element is not None:
            facts = self._backend.describe(element)
            target_text = facts.name or facts.own_text or None
            frame_url = self.surface.frame_url(element.frame_path)
        else:
            frame_url = self.surface.frame_url(()) or "about:blank"
        proposed = ProposedAction(
            phase="action",
            action=action.kind,
            frame_url=frame_url,
            navigate_url=action.url if isinstance(action, Navigate) else None,
            target_text=target_text,
            declared_risk=declared_risk,
        )
        decision = self._gate.check(proposed)
        refusal = self._refusal(decision)
        if refusal is not None:
            code: Literal["POLICY_BLOCKED", "CONFIRMATION_REQUIRED"] = (
                "POLICY_BLOCKED" if isinstance(decision, Block) else "CONFIRMATION_REQUIRED"
            )
            return ActResult(ok=False, action=action.kind, code=code, message=refusal)
        self._last_action = action.kind
        self._declared = declared_risk
        self._target_text = target_text
        return self.surface.act(action, dialog_guard=self._dialog_guard)

    def _navigation_guard(self, request: NavigationRequest) -> str | None:
        proposed = ProposedAction(
            phase="navigation",
            action=self._last_action,
            frame_url=request.frame_url,
            navigate_url=request.url,
        )
        return self._refusal(self._gate.check(proposed))

    def _dialog_guard(self, info: DialogInfo) -> str | None:
        proposed = ProposedAction(
            phase="dialog",
            action="click",
            frame_url=info.frame_url or "about:blank",
            target_text=self._target_text,
            dialog_message=info.message,
            dialog_response=info.response,
            declared_risk=self._declared,
        )
        return self._refusal(self._gate.check(proposed))
