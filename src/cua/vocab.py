"""Shared vocabulary: action types, risk classes, identifier shapes, and the template syntax.

Artifact, policy, replay, and discovery all import these so each word means one thing everywhere.
"""

from __future__ import annotations

import re
from typing import Annotated, Literal, get_args

from pydantic import Field

ActionType = Literal[
    "navigate", "click", "type_text", "select_option", "press_key", "scroll", "read_text"
]
ACTION_TYPES: tuple[str, ...] = get_args(ActionType)

RiskClass = Literal["safe", "reversible", "irreversible"]
_RISK_ORDER: dict[str, int] = {"safe": 0, "reversible": 1, "irreversible": 2}

KeyName = Literal["Enter", "Tab", "Escape"]

FrameName = Annotated[str, Field(pattern=r"^([A-Za-z_][A-Za-z0-9_-]*|#[0-9]+)$")]
StepId = Annotated[str, Field(pattern=r"^s[0-9]{2,3}$")]
CheckpointId = Annotated[str, Field(pattern=r"^cp_[a-z0-9_]+$")]
DetectorId = Annotated[str, Field(pattern=r"^od_[a-z0-9_]+$")]
ParamName = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
TenantId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$")]
OutcomeCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]*$")]

# {{inputs.member_id}}, {{secrets.operator_password}}, {{surface.entry_url}}
TEMPLATE_RE = re.compile(r"\{\{\s*(inputs|secrets|surface)\.([a-z][a-z0-9_]*)\s*\}\}")
TemplateScope = Literal["inputs", "secrets", "surface"]

# Field names that mark a value as a credential. Deliberately broad: over-redaction is cheap.
SENSITIVE_FIELD_RE = re.compile(r"(?i)(pass|pin\b|secret|token|otp|ssn)")


def template_refs(value: str) -> list[tuple[str, str]]:
    """Every (scope, name) template reference in a string, in order."""
    return [(m.group(1), m.group(2)) for m in TEMPLATE_RE.finditer(value)]


def is_single_template(value: str) -> bool:
    return TEMPLATE_RE.fullmatch(value.strip()) is not None


def max_risk(a: RiskClass, b: RiskClass) -> RiskClass:
    return a if _RISK_ORDER[a] >= _RISK_ORDER[b] else b
