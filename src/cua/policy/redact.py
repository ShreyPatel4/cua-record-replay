"""Redactor: masks credentials, identities, and account numbers before anything persists.

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
AMOUNT = "[AMOUNT]"
MIN_SECRET_LEN = 4
# Credentials at least this long are matched anywhere, including inside base64 and longer tokens.
# Shorter ones (a 4-digit PIN) only as whole tokens, or every "1234" in a log would vanish.
SUBSTRING_MIN_LEN = 6

# Amounts and compact timestamps are kept; any other run of five or more digits is an account or
# member number and keeps only its last four. Apply to string values, never to rendered JSON
# numbers, or durations become masked too.
_NUMBER_RE = re.compile(
    r"(?P<keep>"
    r"\$\s?[0-9][0-9,]*(?:\.[0-9]+)?"
    r"|(?<![0-9])[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?(?![0-9])"
    r"|(?<![0-9])[0-9]+\.[0-9]+(?![0-9])"
    r"|(?<![0-9])[0-9]{8}T[0-9]{6}Z?(?![0-9])"
    r"|(?<=:)[0-9]{2,5}(?=[/?#])"
    r")"
    r"|(?<![0-9])(?P<digits>[0-9]{5,})(?![0-9])"
)
_WORD_CHARS = "A-Za-z0-9"
# Money in prose someone wrote about the screen: masked there, kept in app text and messages.
_AMOUNT_RE = re.compile(
    r"\$\s?[0-9][0-9,]*(?:\.[0-9]+)?"
    r"|(?<![0-9])[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?(?![0-9])"
    r"|(?<![0-9.])[0-9]+\.[0-9]{2}(?![0-9])"
)


def _token_pattern(tokens: set[str]) -> re.Pattern[str] | None:
    if not tokens:
        return None
    alternation = "|".join(re.escape(t) for t in sorted(tokens, key=len, reverse=True))
    return re.compile(f"(?<![{_WORD_CHARS}])(?:{alternation})(?![{_WORD_CHARS}])")


def mask_account_number(digits: str) -> str:
    return "*" * (len(digits) - 4) + digits[-4:]


def base64_forms(raw: bytes) -> set[str]:
    """The characters of a value's base64 that are fixed whatever bytes surround it.

    A value can start at any of three byte offsets inside a longer base64 stream, and the chars
    that straddle its edges change with the neighbours, so each offset yields a different core.
    """
    forms: set[str] = set()
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + raw).decode()
        core = encoded[-(-8 * offset // 6) : (8 * (offset + len(raw))) // 6]
        if len(core) >= SUBSTRING_MIN_LEN:
            forms |= {core, core.replace("+", "-").replace("/", "_")}
    return forms


def _check_length(value: str) -> None:
    if len(value) < MIN_SECRET_LEN:
        raise ValueError(
            f"secrets shorter than {MIN_SECRET_LEN} characters cannot be redacted reliably"
        )


class Redactor:
    def __init__(
        self,
        secrets: Iterable[str] = (),
        *,
        identities: Iterable[str] = (),
        mask_account_numbers: bool = True,
    ) -> None:
        substrings: set[str] = set()
        tokens: set[str] = set()
        for secret in secrets:
            _check_length(secret)
            if len(secret) < SUBSTRING_MIN_LEN:
                tokens.add(secret)
                continue
            substrings |= {secret, quote(secret, safe=""), quote_plus(secret)}
            substrings |= {json.dumps(secret)[1:-1]} | base64_forms(secret.encode())
        for identity in identities:
            _check_length(identity)
            tokens |= {identity, quote(identity, safe=""), quote_plus(identity)}
        # Longest first so an encoded form containing a raw form is replaced whole.
        self._substrings = sorted(substrings, key=len, reverse=True)
        self._token_values = tokens
        self._tokens = _token_pattern(tokens)
        self._mask_accounts = mask_account_numbers

    @classmethod
    def from_env(
        cls,
        env_vars: Iterable[str],
        identity_env_vars: Iterable[str] = (),
        environ: Mapping[str, str] | None = None,
    ) -> Redactor:
        source = os.environ if environ is None else environ
        return cls(
            (source[name] for name in env_vars if source.get(name)),
            identities=[source[name] for name in identity_env_vars if source.get(name)],
        )

    def add_secret(self, value: str) -> None:
        """Mask a value learned during a run, such as a sensitive output, from now on.

        Matched as a whole token, so a balance of 15.00 never eats into 115.00 or 05:18:15.004.
        """
        _check_length(value)
        self._token_values = self._token_values | {value}
        self._tokens = _token_pattern(self._token_values)

    def free_text(self, value: str) -> str:
        """Prose written about the screen, such as a model's rationale: amounts are masked too."""
        return _AMOUNT_RE.sub(AMOUNT, self.text(value))

    def text(self, value: str) -> str:
        for form in self._substrings:
            value = value.replace(form, REDACTED)
        if self._tokens is not None:
            value = self._tokens.sub(REDACTED, value)
        if self._mask_accounts:
            value = _NUMBER_RE.sub(
                lambda m: m.group("keep") or mask_account_number(m.group("digits")), value
            )
        return value

    def typed_value(self, field_name: str | None, value: str) -> str:
        """A value typed into a field: fully redacted when the field looks like a credential."""
        if field_name and SENSITIVE_FIELD_RE.search(field_name):
            return REDACTED
        return self.text(value)

    def params(self, values: Mapping[str, str], sensitive: Iterable[str]) -> dict[str, str]:
        hidden = set(sensitive)
        return {k: REDACTED if k in hidden else self.text(v) for k, v in values.items()}
