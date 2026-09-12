"""Redaction: secrets in any encoding, credential fields, and account numbers never persist.

Amounts and dates must survive untouched, or evidence becomes useless for debugging.
"""

from __future__ import annotations

from urllib.parse import quote_plus

import pytest

from cua.policy.redact import REDACTED, Redactor

SECRET = "Tr@ce pass/word+1&x=y"


def test_secret_is_removed_in_raw_and_encoded_forms() -> None:
    redactor = Redactor([SECRET])
    text = f'typed {SECRET}; body password={quote_plus(SECRET)}; json "{SECRET}"'
    cleaned = redactor.text(text)
    assert SECRET not in cleaned
    assert quote_plus(SECRET) not in cleaned
    assert cleaned.count(REDACTED) == 3


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Member no 10007", "Member no *0007"),
        ("No member found for member number 99999.", "No member found for member number *9999."),
        ("Sub-account 10007-S01 opened", "Sub-account *0007-S01 opened"),
        ("account 123456789012", "account ********9012"),
        ("Balance $88,000.00", "Balance $88,000.00"),
        ("Balance 88000.00", "Balance 88000.00"),
        ("Joined 2012-09-05", "Joined 2012-09-05"),
        ("4210.55 and 1234", "4210.55 and 1234"),
    ],
)
def test_account_numbers_keep_only_the_last_four(raw: str, expected: str) -> None:
    assert Redactor().text(raw) == expected


def test_credential_fields_are_fully_redacted() -> None:
    redactor = Redactor()
    assert redactor.typed_value("Password", "anything at all") == REDACTED
    assert redactor.typed_value("PIN", "1234") == REDACTED
    assert redactor.typed_value("Member number", "10007") == "*0007"


def test_sensitive_params_are_redacted_by_name() -> None:
    cleaned = Redactor().params({"member_id": "10007", "ssn": "123-45-6789"}, sensitive=["ssn"])
    assert cleaned == {"member_id": "*0007", "ssn": REDACTED}


def test_short_secrets_are_refused() -> None:
    with pytest.raises(ValueError, match="cannot be redacted reliably"):
        Redactor(["abc"])


def test_from_env_reads_only_set_variables() -> None:
    redactor = Redactor.from_env(["A", "B"], environ={"A": "long-enough-secret", "B": ""})
    assert redactor.text("x long-enough-secret y") == f"x {REDACTED} y"
