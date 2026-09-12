"""Redactor: masks credentials, configured secrets, and account numbers before anything persists.

Applied to logs, artifacts, intervention requests, human action capture, and model context alike.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Iterable, Mapping
from urllib.parse import quote, quote_plus

from cua.vocab import SENSITIVE_FIELD_RE

REDACTED = "[REDACTED]"

# Five or more digits that are not part of a formatted amount ("$88,000.00", "88000.00") or a longer
# token. Dates (2012-09-05) and short numbers never reach five digits in a row.
_ACCOUNT_NUMBER_RE = re.compile(r"(?<![\w$.,])([0-9]{5,})(?![0-9]|[.,][0-9])")

MIN_SECRET_LEN = 6


def mask_account_number(digits: str) -> str:
    return "*" * (len(digits) - 4) + digits[-4:]


class Redactor:
    def __init__(self, secrets: Iterable[str] = (), *, mask_account_numbers: bool = True) -> None:
        forms: set[str] = set()
        for secret in secrets:
            if len(secret) < MIN_SECRET_LEN:
                raise ValueError(
                    f"secrets shorter than {MIN_SECRET_LEN} characters cannot be redacted reliably"
                )
            forms |= {secret, quote(secret, safe=""), quote_plus(secret), json.dumps(secret)[1:-1]}
            forms.add(base64.b64encode(secret.encode()).decode().rstrip("="))
        # Longest first so an encoded form containing a raw form is replaced whole.
        self._secret_forms = sorted(forms, key=len, reverse=True)
        self._mask_accounts = mask_account_numbers

    @classmethod
    def from_env(
        cls, env_vars: Iterable[str], environ: Mapping[str, str] | None = None
    ) -> Redactor:
        source = os.environ if environ is None else environ
        return cls(source[name] for name in env_vars if source.get(name))

    def text(self, value: str) -> str:
        for form in self._secret_forms:
            value = value.replace(form, REDACTED)
        if self._mask_accounts:
            value = _ACCOUNT_NUMBER_RE.sub(lambda m: mask_account_number(m.group(1)), value)
        return value

    def typed_value(self, field_name: str | None, value: str) -> str:
        """A value typed into a field: fully redacted when the field looks like a credential."""
        if field_name and SENSITIVE_FIELD_RE.search(field_name):
            return REDACTED
        return self.text(value)

    def params(self, values: Mapping[str, str], sensitive: Iterable[str]) -> dict[str, str]:
        hidden = set(sensitive)
        return {k: REDACTED if k in hidden else self.text(v) for k, v in values.items()}
