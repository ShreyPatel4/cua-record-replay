"""CoreLedger over HTTP: every route and every failure injection behaves as the kickoff table says.

The checks themselves live in mock_app.smoke so CI can run them against a CLI-booted server too.
"""

from __future__ import annotations

import pytest

from mock_app.injections import DEFAULT_SLOW_MS, INJECTION_NAMES, InjectionState
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


def test_settings_repr_never_shows_the_password() -> None:
    settings = MockSettings(operator_user="operator", operator_password="hunter2-but-longer")
    assert "hunter2" not in repr(settings)


def test_scoped_fault_is_not_consumed_by_other_sites() -> None:
    faults = InjectionState()
    faults.arm("permission_denied", times=1, params={"on": "create"})
    assert faults.fire("permission_denied", at="detail", default_at="create") is None
    assert faults.fire("permission_denied", at="create", default_at="create") is not None
    assert faults.fire("permission_denied", at="create", default_at="create") is None


def test_counted_fault_expires_and_uncounted_fault_persists() -> None:
    faults = InjectionState()
    faults.arm("interstitial", times=2)
    faults.arm("app_error", times=None)
    fired = [faults.fire("interstitial") is not None for _ in range(3)]
    assert fired == [True, True, False]
    assert all(faults.fire("app_error") is not None for _ in range(5))


def test_session_expired_defaults_to_one_shot() -> None:
    assert InjectionState().arm("session_expired").remaining == 1


def test_unknown_injection_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown injection"):
        InjectionState().arm("bogus")
