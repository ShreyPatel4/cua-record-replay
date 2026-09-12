"""Redaction: secrets in any encoding, credential fields, and account numbers never persist.

Amounts, dates, and run ids must survive untouched, or evidence becomes useless for debugging.
"""

from __future__ import annotations

import base64
from urllib.parse import quote_plus

import pytest

from cua.policy.redact import REDACTED, Redactor, base64_forms

SECRET = "Tr@ce pass/word+1&x=y"


def test_secret_is_removed_in_raw_and_encoded_forms() -> None:
    redactor = Redactor([SECRET])
    text = f'typed {SECRET}; body password={quote_plus(SECRET)}; json "{SECRET}"'
    cleaned = redactor.text(text)
    assert SECRET not in cleaned
    assert quote_plus(SECRET) not in cleaned
    assert cleaned.count(REDACTED) == 3


@pytest.mark.parametrize("prefix", ["operator=teller-0417&password=", "a=1&password=", "pw="])
def test_secret_is_removed_from_base64_at_every_byte_offset(prefix: str) -> None:
    body = base64.b64encode((prefix + SECRET + "&next=/members").encode()).decode()
    cleaned = Redactor([SECRET]).text(body)
    assert cleaned != body
    assert not [form for form in base64_forms(SECRET.encode()) if form in cleaned]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Member no 10007", "Member no *0007"),
        ("No member found for member number 99999.", "No member found for member number *9999."),
        ("Sub-account 10007-S01 opened", "Sub-account *0007-S01 opened"),
        ("account 123456789012", "account ********9012"),
        ("members 10007,10008", "members *0007,*0008"),
        ("ACCT10007 and id=10007&x=1", "ACCT*0007 and id=*0007&x=1"),
        ("Balance $88,000.00", "Balance $88,000.00"),
        ("Balance $88000", "Balance $88000"),
        ("Balance 88000.00", "Balance 88000.00"),
        ("Joined 2012-09-05", "Joined 2012-09-05"),
        ("4210.55 and 1234", "4210.55 and 1234"),
        ("evidence/replay_20260912T150000Z_ab12", "evidence/replay_20260912T150000Z_ab12"),
    ],
)
def test_account_numbers_keep_only_the_last_four(raw: str, expected: str) -> None:
    assert Redactor().text(raw) == expected


def test_short_credentials_are_matched_as_whole_tokens() -> None:
    assert Redactor(["4821"]).text("pin 4821 in 48210") == f"pin {REDACTED} in *8210"


def test_identities_are_redacted_as_whole_tokens_only() -> None:
    redactor = Redactor(identities=["teller-0417"])
    assert redactor.text("signed in as teller-0417; teller-04170") == (
        f"signed in as {REDACTED}; teller-*4170"
    )
    assert redactor.text("operator=teller-0417&password=x") == f"operator={REDACTED}&password=x"


def test_credential_fields_are_fully_redacted() -> None:
    redactor = Redactor()
    assert redactor.typed_value("Password", "anything at all") == REDACTED
    assert redactor.typed_value("PIN", "1234") == REDACTED
    assert redactor.typed_value("Member number", "10007") == "*0007"


def test_sensitive_params_are_redacted_by_name() -> None:
    cleaned = Redactor().params({"member_id": "10007", "ssn": "123-45-6789"}, sensitive=["ssn"])
    assert cleaned == {"member_id": "*0007", "ssn": REDACTED}


def test_secrets_too_short_to_match_are_refused() -> None:
    with pytest.raises(ValueError, match="cannot be redacted reliably"):
        Redactor(["abc"])
    with pytest.raises(ValueError, match="cannot be redacted reliably"):
        Redactor(identities=["op"])


def test_from_env_reads_only_set_variables() -> None:
    redactor = Redactor.from_env(
        ["A", "B"], ["C"], environ={"A": "long-enough-secret", "B": "", "C": "teller-0417"}
    )
    assert redactor.text("x long-enough-secret y teller-0417") == f"x {REDACTED} y {REDACTED}"
