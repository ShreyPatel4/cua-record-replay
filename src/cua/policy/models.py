"""Policy contracts: the allowlist file shape, the resolved policy, proposed actions, and decisions.

Loading resolves `extends` chains; the matching logic lives in policy.gate.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from cua.vocab import ActionType, KeyName, RiskClass


class PolicyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _compiles(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regex {value!r}: {exc}") from exc
    return value


def _origin(value: str) -> str:
    """Lowercase scheme://host[:port]; IPv6 hosts in brackets, the way the gate rebuilds them."""
    if not re.fullmatch(r"https?://(\[[0-9a-f:.]+\]|[a-z0-9.-]+)(:[0-9]+)?", value):
        raise ValueError(
            f"origin must be lowercase scheme://host[:port] with no path, got {value!r}"
        )
    return value


def _glob(value: str) -> str:
    if not value.startswith("/"):
        raise ValueError(f"path globs must start with '/', got {value!r}")
    return value


Regex = Annotated[str, AfterValidator(_compiles)]
Origin = Annotated[str, AfterValidator(_origin)]
PathGlob = Annotated[str, AfterValidator(_glob)]
Handling = Literal["block", "confirm", "escalate"]


class RiskRule(PolicyModel):
    """All fields that are set must match. Unset fields do not constrain."""

    action: ActionType | None = Field(default=None, description="Action type to match.")
    target_text_matches: Regex | None = Field(
        default=None, description="Regex searched in the target's visible text or name."
    )
    url_matches: PathGlob | None = Field(
        default=None, description="Path glob for the page the action happens on."
    )
    dialog_message_matches: Regex | None = Field(
        default=None, description="Regex searched in a dialog the action answers."
    )
    dialog_response: Literal["accept", "dismiss"] | None = Field(
        default=None, description="Dialog answer to match."
    )

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if not any(v is not None for v in self.model_dump().values()):
            raise ValueError("a risk rule must set at least one field")
        return self


class RiskRules(PolicyModel):
    irreversible_when: list[RiskRule] = Field(
        default_factory=list, description="Any match makes the action irreversible."
    )
    reversible_when: list[RiskRule] = Field(
        default_factory=list, description="Any match makes the action at least reversible."
    )


class PolicyDef(PolicyModel):
    """One entry in policy/allowlist.yaml. List fields add to the parent; scalars override it."""

    extends: str | None = Field(default=None, description="Parent policy id.")
    description: str | None = Field(default=None, description="What this policy is for.")
    origins: list[Origin] = Field(default_factory=list, description="Allowed scheme://host[:port].")
    paths_allow: list[PathGlob] = Field(
        default_factory=list,
        description="Path globs allowed. '*' is one segment, '**' is any depth.",
    )
    paths_deny: list[PathGlob] = Field(
        default_factory=list, description="Path globs denied; deny wins over allow."
    )
    query_deny: list[str] = Field(
        default_factory=list, description="Query parameter name globs that make a URL denied."
    )
    actions_allow: list[ActionType] = Field(
        default_factory=list, description="Allowed action types."
    )
    risk: RiskRules | None = Field(default=None, description="How actions are classified.")
    irreversible_handling: Handling | None = Field(
        default=None, description="What happens to an irreversible action."
    )
    sensitive_pages: list[PathGlob] = Field(
        default_factory=list,
        description="Pages whose screenshots are flagged sensitive in evidence.",
    )


class PolicyFile(PolicyModel):
    policies: dict[str, PolicyDef] = Field(description="Policies by id.")


class Policy(PolicyModel):
    """A fully resolved policy: every field concrete, extends already applied."""

    id: str = Field(description="Policy id.")
    chain: list[str] = Field(description="Resolution chain, root first.")
    origins: list[Origin] = Field(description="Allowed origins.")
    paths_allow: list[PathGlob] = Field(description="Allowed path globs.")
    paths_deny: list[PathGlob] = Field(description="Denied path globs.")
    query_deny: list[str] = Field(description="Denied query parameter name globs.")
    actions_allow: list[ActionType] = Field(description="Allowed actions.")
    risk: RiskRules = Field(description="Risk classification rules.")
    irreversible_handling: Handling = Field(description="Handling for irreversible actions.")
    sensitive_pages: list[PathGlob] = Field(description="Sensitive page globs.")


class PolicyError(ValueError):
    pass


def resolve_policy(policy_file: PolicyFile, policy_id: str) -> Policy:
    chain: list[str] = []
    current: str | None = policy_id
    while current is not None:
        if current in chain:
            raise PolicyError(f"policy extends cycle: {' -> '.join([*chain, current])}")
        if current not in policy_file.policies:
            raise PolicyError(f"unknown policy {current!r}")
        chain.append(current)
        current = policy_file.policies[current].extends
    chain.reverse()

    lists: dict[str, list[str]] = {
        k: []
        for k in (
            "origins",
            "paths_allow",
            "paths_deny",
            "query_deny",
            "actions_allow",
            "sensitive_pages",
        )
    }
    irreversible: list[RiskRule] = []
    reversible: list[RiskRule] = []
    handling: Handling | None = None
    for name in chain:
        entry = policy_file.policies[name]
        for key, values in lists.items():
            values.extend(v for v in getattr(entry, key) if v not in values)
        if entry.risk is not None:
            irreversible.extend(entry.risk.irreversible_when)
            reversible.extend(entry.risk.reversible_when)
        handling = entry.irreversible_handling or handling
    if handling is None:
        raise PolicyError(f"policy {policy_id!r} never sets irreversible_handling")
    return Policy.model_validate(
        {
            "id": policy_id,
            "chain": chain,
            **lists,
            "risk": {"irreversible_when": irreversible, "reversible_when": reversible},
            "irreversible_handling": handling,
        }
    )


def load_policy(path: Path, policy_id: str) -> Policy:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return resolve_policy(PolicyFile.model_validate(raw), policy_id)


class ProposedAction(PolicyModel):
    """Everything the gate needs to judge one action, gathered by the caller before acting.

    One step can reach the gate many times: before the action, for every request it causes, and
    when a native dialog it opened is about to be answered.
    """

    phase: Literal["action", "navigation", "subresource", "dialog"] = Field(
        default="action",
        description="action: before the surface acts. navigation: a frame document request or "
        "redirect hop, judged before the browser follows it. subresource: any other request "
        "(fetch, XHR, images), judged on origin, paths_deny, and query_deny only. dialog: a native "
        "dialog about to be answered, with its real message.",
    )
    action: ActionType = Field(description="Action being performed, or that caused this phase.")
    frame_url: str = Field(description="URL of the document the action happens in.")
    navigate_url: str | None = Field(
        default=None, description="Destination, for navigate actions and the navigation phase."
    )
    target_text: str | None = Field(default=None, description="Visible text or name of the target.")
    key: KeyName | None = Field(
        default=None, description="Key for press_key. Enter on a target is judged like a click."
    )
    dialog_message: str | None = Field(
        default=None,
        description="Dialog message. In the action phase this is the artifact's declared "
        "expectation; the dialog phase re-checks the real message.",
    )
    dialog_response: Literal["accept", "dismiss"] | None = Field(
        default=None, description="How the dialog will be answered."
    )
    declared_risk: RiskClass = Field(
        default="safe", description="Risk the artifact or model declared."
    )

    @model_validator(mode="after")
    def _phase_shape(self) -> Self:
        needs_destination = self.phase in ("navigation", "subresource") or self.action == "navigate"
        if needs_destination and self.navigate_url is None:
            raise ValueError("navigate actions and request phases need navigate_url")
        if self.phase == "dialog" and (self.dialog_message is None or self.dialog_response is None):
            raise ValueError("the dialog phase needs dialog_message and dialog_response")
        return self


class Allow(PolicyModel):
    kind: Literal["allow"] = Field(default="allow", description="Proceed.")
    risk: RiskClass = Field(description="Effective risk class.")


class Block(PolicyModel):
    kind: Literal["block"] = Field(
        default="block", description="Do not act; replay reports POLICY_BLOCKED."
    )
    reason: str = Field(description="Which rule blocked it.")
    risk: RiskClass = Field(description="Effective risk class.")


class RequiresConfirmation(PolicyModel):
    kind: Literal["requires_confirmation"] = Field(
        default="requires_confirmation", description="Act only after explicit confirmation."
    )
    reason: str = Field(description="Which rule made it irreversible.")
    risk: Literal["irreversible"] = Field(description="Always irreversible.")
    handling: Literal["confirm", "escalate"] = Field(
        description="confirm: needs a CLI flag. escalate: needs a human through an intervention."
    )


PolicyDecision = Annotated[Allow | Block | RequiresConfirmation, Field(discriminator="kind")]
