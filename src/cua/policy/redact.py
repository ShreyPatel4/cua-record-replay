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
# Money in prose someone wrote about the screen: masked there, kept in app text and messages.
_AMOUNT_RE = re.compile(
    r"\$\s?[0-9][0-9,]*(?:\.[0-9]+)?"
    r"|(?<![0-9])[0-9]{1,3}(?:,[0-9]{3})+(?:\.[0-9]+)?(?![0-9])"
    r"|(?<![0-9.])[0-9]+\.[0-9]{2}(?![0-9])"
)
_MONEY_FORM_RE = re.compile(
    r"\(?-?\s*\$\s?(?P<num>[0-9]{1,3}(?:,[0-9]{3})*|[0-9]+)(?P<frac>\.[0-9]{2})?\)?"
)
# A whole token: not inside a word, and not one part of a longer number such as 4.0.00 or 1,15.00.
_BEFORE = r"(?<![A-Za-z0-9])(?<![0-9][.,])"
_AFTER = r"(?![A-Za-z0-9])(?![.,][0-9])"


def _token_patterns(tokens: set[str]) -> tuple[re.Pattern[str], re.Pattern[bytes]] | None:
    if not tokens:
        return None
    ordered = sorted(tokens, key=len, reverse=True)
    text = "|".join(re.escape(t) for t in ordered)
    raw = b"|".join(re.escape(t.encode()) for t in ordered)
    return (
        re.compile(f"{_BEFORE}(?:{text}){_AFTER}"),
        re.compile(_BEFORE.encode() + b"(?:" + raw + b")" + _AFTER.encode()),
    )


def mask_account_number(digits: str) -> str:
    return "*" * (len(digits) - 4) + digits[-4:]


def base64_forms(raw: bytes, min_len: int = SUBSTRING_MIN_LEN) -> set[str]:
    """The characters of a value's base64 that are fixed whatever bytes surround it.

    A value can start at any of three byte offsets inside a longer base64 stream, and the chars
    that straddle its edges change with the neighbours, so each offset yields a different core.
    """
    forms: set[str] = set()
    for offset in range(3):
        encoded = base64.b64encode(b"\0" * offset + raw).decode()
        core = encoded[-(-8 * offset // 6) : (8 * (offset + len(raw))) // 6]
        if len(core) >= min_len:
            forms |= {core, core.replace("+", "-").replace("/", "_")}
    return forms


def _text_forms(value: str) -> set[str]:
    return {value, quote(value, safe=""), quote_plus(value), json.dumps(value)[1:-1]}


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
        self._substrings: list[str] = []
        self._token_values: set[str] = set()
        self._patterns: tuple[re.Pattern[str], re.Pattern[bytes]] | None = None
        self._mask_accounts = mask_account_numbers
        for secret in secrets:
            self.add_credential(secret)
        for identity in identities:
            _check_length(identity)
            # Base64 of an identity is gibberish, so matching it anywhere cannot over-redact, and
            # traces store some captured values (request bodies) base64-encoded.
            self._add(tokens=_text_forms(identity), substrings=base64_forms(identity.encode()))

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

    def _add(self, *, tokens: Iterable[str] = (), substrings: Iterable[str] = ()) -> None:
        new_tokens = {t for t in tokens if t} - self._token_values
        new_substrings = {s for s in substrings if s} - set(self._substrings)
        if new_substrings:
            # Longest first so an encoded form containing a raw form is replaced whole.
            self._substrings = sorted({*self._substrings, *new_substrings}, key=len, reverse=True)
        if new_tokens:
            self._token_values = self._token_values | new_tokens
            self._patterns = _token_patterns(self._token_values)

    def add_credential(self, value: str) -> None:
        """A password, token, or session cookie: every text and base64 form, from now on."""
        _check_length(value)
        # A short credential's base64 cores are short too; over-redacting a trace is cheap.
        encoded = base64_forms(value.encode(), MIN_SECRET_LEN)
        if len(value) < SUBSTRING_MIN_LEN:
            self._add(tokens=_text_forms(value), substrings=encoded)
        else:
            self._add(substrings=_text_forms(value) | encoded)

    def add_secret(self, value: str) -> None:
        """Mask a value learned during a run, such as a sensitive output, from now on.

        Matched as a whole token, so a balance of 15.00 never eats into 115.00 or 05:18:15.004.
        """
        _check_length(value)
        self._add(tokens={value})

    def add_value(self, text: str) -> None:
        """Mask a sensitive value read off the screen, plus the bare number forms of an amount,
        so "$4,210.55" also hides "4,210.55" and "4210.55". A dollar form is masked at any length
        ("$5"); bare forms shorter than four characters are not, or every "100" would vanish.
        """
        shown = " ".join(text.split())
        if not shown:
            return
        forms = {shown}
        match = _MONEY_FORM_RE.fullmatch(shown)
        if match is not None:
            number, frac = match.group("num"), match.group("frac") or ""
            forms |= {f"${number}{frac}", number + frac, number.replace(",", "") + frac}
        self._add(
            tokens={f for f in forms if f.startswith(("$", "(", "-")) or len(f) >= MIN_SECRET_LEN}
        )

    def scrub_bytes(self, data: bytes) -> bytes:
        """Replace secret values, in every encoding the redactor knows, inside raw bytes.

        For binary evidence such as trace archives: account numbers are left alone, because
        masking digit runs would corrupt timestamps and offsets in machine-readable files.
        """
        for form in self._substrings:
            data = data.replace(form.encode(), REDACTED.encode())
        if self._patterns is not None:
            data = self._patterns[1].sub(REDACTED.encode(), data)
        return data

    def free_text(self, value: str) -> str:
        """Prose written about the screen, such as a model's rationale: amounts are masked too."""
        return _AMOUNT_RE.sub(AMOUNT, self.text(value))

    def text(self, value: str) -> str:
        for form in self._substrings:
            value = value.replace(form, REDACTED)
        if self._patterns is not None:
            value = self._patterns[0].sub(REDACTED, value)
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
