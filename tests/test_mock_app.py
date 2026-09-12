"""CoreLedger over HTTP plus the injection model's unit rules (counts, sites, param validation).

The HTTP checks live in mock_app.smoke so CI can run them against a CLI-booted server too.
"""

from __future__ import annotations

import pytest

from mock_app.injections import (
    DEFAULT_SLOW_MS,
    INJECTION_NAMES,
    InjectionError,
    InjectionState,
)
from mock_app.server import RunningMockApp
from mock_app.settings import MissingSettingError, MockSettings
from mock_app.smoke import CHECKS, run_check


@pytest.mark.parametrize("check", list(CHECKS))
def test_smoke_check(check: str, coreledger: RunningMockApp, mock_settings: MockSettings) -> None:
    run_check(
        check, coreledger.base_url, mock_settings.operator_user, mock_settings.operator_password
    )


def test_every_injection_has_a_smoke_check() -> None:
    assert set(INJECTION_NAMES) <= set(CHECKS)


def test_default_slow_delay_sits_in_the_4_to_8_second_band() -> None:
    assert 4000 <= DEFAULT_SLOW_MS <= 8000


def test_settings_refuse_to_start_without_a_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORELEDGER_OPERATOR_USER", "operator")
    monkeypatch.delenv("CORELEDGER_OPERATOR_PASSWORD", raising=False)
    with pytest.raises(MissingSettingError, match="CORELEDGER_OPERATOR_PASSWORD"):
        MockSettings.from_env(load_dotenv_file=False)


def test_query_injection_can_be_disabled_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CORELEDGER_OPERATOR_USER", "operator")
    monkeypatch.setenv("CORELEDGER_OPERATOR_PASSWORD", "long-enough-password")
    monkeypatch.setenv("CORELEDGER_ALLOW_QUERY_INJECTION", "0")
    assert MockSettings.from_env(load_dotenv_file=False).allow_query_injection is False


def test_settings_repr_never_shows_the_password() -> None:
    settings = MockSettings(operator_user="operator", operator_password="hunter2-but-longer")
    assert "hunter2" not in repr(settings)


def test_sited_fault_is_not_consumed_by_other_sites() -> None:
    faults = InjectionState()
    faults.arm("permission_denied", times=1)
    assert faults.fire("permission_denied", at="detail") is None
    assert faults.fire("permission_denied", at="create") is not None
    assert faults.fire("permission_denied", at="create") is None


def test_counted_fault_expires_and_uncounted_fault_persists() -> None:
    faults = InjectionState()
    faults.arm("interstitial", times=2)
    faults.arm("app_error", times=None)
    assert [faults.fire("interstitial") is not None for _ in range(3)] == [True, True, False]
    assert all(faults.fire("app_error", at="detail") is not None for _ in range(5))


def test_session_expired_defaults_to_one_shot() -> None:
    assert InjectionState().arm("session_expired").remaining == 1


@pytest.mark.parametrize(
    ("name", "times", "params"),
    [
        ("bogus", 1, {}),
        ("slow", 0, {}),
        ("slow", 1.5, {}),
        ("slow", True, {}),
        ("slow", 1, {"ms": "abc"}),
        ("slow", 1, {"ms": "999999"}),
        ("permission_denied", 1, {"on": "bogus"}),
        ("interstitial", 1, {"sticky": "yes"}),
        ("not_found", 1, {"ms": "10"}),
    ],
)
def test_bad_arm_requests_fail_loudly(name: str, times: object, params: dict[str, str]) -> None:
    with pytest.raises(InjectionError):
        InjectionState().arm(name, times=times, params=params)  # type: ignore[arg-type]
