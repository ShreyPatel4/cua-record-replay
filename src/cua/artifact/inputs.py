"""Input coercion and template rendering: caller strings become typed values before any step runs.

All problems are reported at once so a calling agent can fix its call in a single round trip.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal

from cua.artifact.schema import Capability, InputParam
from cua.vocab import TEMPLATE_RE

TypedInput = str | int | bool | Decimal

_MONEY_RE = re.compile(r"^-?[0-9]+(\.[0-9]{1,2})?$")
_INT_RE = re.compile(r"^-?[0-9]+$")


class InputError(ValueError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


class TemplateError(KeyError):
    pass


def _coerce(param: InputParam, value: str) -> TypedInput:
    if param.type == "string":
        return value
    if param.type == "integer":
        if not _INT_RE.match(value):
            raise ValueError("expected an integer")
        return int(value)
    if param.type == "money":
        if not _MONEY_RE.match(value):
            raise ValueError("expected a decimal amount with at most 2 places, e.g. 25.00")
        return Decimal(value)
    if param.type == "boolean":
        if value not in ("true", "false"):
            raise ValueError("expected true or false")
        return value == "true"
    if param.enum_values is None or value not in param.enum_values:
        raise ValueError(f"expected one of {param.enum_values}")
    return value


def coerce_inputs(capability: Capability, raw: Mapping[str, str]) -> dict[str, TypedInput]:
    problems: list[str] = []
    declared = {p.name: p for p in capability.inputs}
    unknown = sorted(set(raw) - set(declared))
    if unknown:
        problems.append(f"unknown inputs {unknown}; this capability takes {sorted(declared)}")
    typed: dict[str, TypedInput] = {}
    for param in capability.inputs:
        if param.name not in raw:
            if param.required:
                problems.append(f"{param.name} is required: {param.description}")
            continue
        value = raw[param.name]
        shown = "[REDACTED]" if param.sensitive else repr(value)
        if param.pattern is not None and not re.fullmatch(param.pattern, value):
            problems.append(f"{param.name}={shown} does not match {param.pattern}")
            continue
        try:
            typed[param.name] = _coerce(param, value)
        except ValueError as exc:
            problems.append(f"{param.name}={shown}: {exc}")
    if problems:
        raise InputError(problems)
    return typed


def render(
    value: str,
    *,
    inputs: Mapping[str, TypedInput],
    secrets: Mapping[str, str],
    surface: Mapping[str, str],
) -> str:
    """Substitute {{scope.name}} templates. Money renders with two decimal places."""
    scopes: dict[str, Mapping[str, object]] = {
        "inputs": inputs,
        "secrets": secrets,
        "surface": surface,
    }

    def substitute(match: re.Match[str]) -> str:
        scope, name = match.group(1), match.group(2)
        if name not in scopes[scope]:
            raise TemplateError(f"no value for {{{{{scope}.{name}}}}}")
        resolved = scopes[scope][name]
        if isinstance(resolved, Decimal):
            return f"{resolved:.2f}"
        if isinstance(resolved, bool):
            return "true" if resolved else "false"
        return str(resolved)

    return TEMPLATE_RE.sub(substitute, value)
