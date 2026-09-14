"""Output parsing: an element's visible text into the typed value a capability declares.

Replay uses it to return outputs; discovery uses it to refuse an output that would never parse.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal

from cua.replay.result import DateValue, MoneyValue, OutputValue

ParseKind = Literal["money_usd", "text", "integer", "date_iso"]

_MONEY_RE = re.compile(
    r"^(?P<neg>-|\()?\s*\$\s?(?P<num>[0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?P<frac>\.[0-9]{2})?\)?$"
)
_INTEGER_RE = re.compile(r"^-?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)$")


class ExtractionError(ValueError):
    pass


def parse_value(text: str, parse: ParseKind) -> OutputValue:
    """Parse whitespace-collapsed visible text. Raises ExtractionError without echoing the text."""
    value = " ".join(text.split())
    if parse == "text":
        if not value:
            raise ExtractionError("the element has no visible text")
        return value
    if parse == "money_usd":
        match = _MONEY_RE.match(value)
        if match is None or (match.group("neg") == "(") != value.endswith(")"):
            raise ExtractionError("expected a US dollar amount like $1,234.56")
        amount = Decimal(match.group("num").replace(",", "") + (match.group("frac") or ""))
        return MoneyValue(amount=-amount if match.group("neg") else amount, currency="USD")
    if parse == "integer":
        if not _INTEGER_RE.match(value):
            raise ExtractionError("expected a whole number")
        return int(value.replace(",", ""))
    try:
        return DateValue(value=date.fromisoformat(value))
    except ValueError as exc:
        raise ExtractionError("expected a date like 2026-09-13") from exc
