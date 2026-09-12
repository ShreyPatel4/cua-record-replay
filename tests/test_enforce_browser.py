"""GatedSurface in Chromium: the gate judges actions, the navigations they cause, and dialogs.

Also the control lock: automation cannot touch the page while a human holds the session.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Frame, Page, expect

from cua.policy.enforce import GatedSurface
from cua.policy.gate import PolicyGate
from cua.policy.models import load_policy
from cua.session.state import NotInControl
from cua.surface.base import (
    ActResult,
    Click,
    Element,
    ExpectedDialog,
    Navigate,
    SurfaceEvent,
    TypeText,
)
from cua.surface.playwright import PlaywrightSurface
from mock_app.server import RunningMockApp
from mock_app.settings import MockSettings

from support import ROOT

pytestmark = pytest.mark.browser
POLICY_FILE = ROOT / "policy" / "allowlist.yaml"
CONFIRM = ExpectedDialog(
    dialog_type="confirm", message_pattern=r"cannot be undone", response="accept"
)


def _gated(
    page: Page, base_url: str, policy_id: str, *, confirm: bool = False, **policy: object
) -> tuple[GatedSurface, PlaywrightSurface]:
    resolved = load_policy(POLICY_FILE, policy_id).model_copy(
        update={"origins": [base_url], **policy}
    )
    surface = PlaywrightSurface(page)
    return GatedSurface(
        surface, surface, PolicyGate(resolved), confirm_irreversible=confirm
    ), surface


def _main(page: Page) -> Frame:
    frame = page.frame(name="main")
    assert frame is not None
    return frame


def _sign_in(page: Page, base_url: str, settings: MockSettings) -> Frame:
    page.goto(base_url + "/")
    main = _main(page)
    main.get_by_label("Operator ID").fill(settings.operator_user)
    main.get_by_label("Password").fill(settings.operator_password)
    main.get_by_role("button", name="Sign in").click()
    expect(main.get_by_text("Member Lookup")).to_be_visible()
    return main


def _ref_element(surface: PlaywrightSurface, name: str) -> Element:
    node = next(n for n in surface.snapshot().nodes if n.ref and n.name == name)
    assert node.ref is not None
    return surface.element_for_ref(node.ref)


def _events_after(surface: PlaywrightSurface, result: ActResult) -> list[SurfaceEvent]:
    surface.settle(quiet_ms=200, timeout_ms=5000)
    return [*result.events, *surface.drain_events()]


def _open_form(page: Page, base_url: str, settings: MockSettings) -> Frame:
    main = _sign_in(page, base_url, settings)
    main.goto(base_url + "/members/10007/subaccounts/new")
    main.locator("select").select_option(label="Money Market")
    main.locator("input[name=initial_deposit]").fill("25.00")
    return main


def test_navigate_actions_are_judged_before_loading(page: Page, coreledger: RunningMockApp) -> None:
    gated, _ = _gated(page, coreledger.base_url, "coreledger-readonly")
    blocked = gated.perform(Navigate(coreledger.base_url + "/__control"))
    assert (blocked.ok, blocked.code) == (False, "POLICY_BLOCKED")
    assert "paths_deny" in blocked.message
    assert page.url == "about:blank"
    assert gated.perform(Navigate(coreledger.base_url + "/")).ok


def test_a_click_that_navigates_off_policy_is_aborted_in_flight(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated, surface = _gated(page, coreledger.base_url, "coreledger-readonly")
    main = _sign_in(page, coreledger.base_url, mock_settings)
    result = gated.perform(Click(_ref_element(surface, "Sign out")))
    events = _events_after(surface, result)
    blocked = [e for e in events if e.kind == "navigation_blocked"]
    assert blocked, events
    assert "paths_deny /logout" in blocked[0].detail
    assert main.url.endswith("/members/search")
    main.goto(coreledger.base_url + "/members/search")
    expect(main.get_by_text("Member Lookup")).to_be_visible()


def test_a_server_redirect_off_policy_is_detected_not_prevented(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated, surface = _gated(page, coreledger.base_url, "coreledger-readonly")
    page.goto(coreledger.base_url + "/")
    main = _main(page)
    expect(main.get_by_text("Operator sign-in")).to_be_visible()
    main.locator("input[name=next]").evaluate("(el) => { el.value = '/__control' }")
    main.get_by_label("Operator ID").fill(mock_settings.operator_user)
    main.get_by_label("Password").fill(mock_settings.operator_password)
    result = gated.perform(Click(_ref_element(surface, "Sign in")))
    events = _events_after(surface, result)
    assert [e for e in events if e.kind == "redirect_off_policy" and "/__control" in e.url], events


def test_an_irreversible_click_needs_confirmation_before_anything_happens(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated, surface = _gated(page, coreledger.base_url, "coreledger-subaccount")
    main = _open_form(page, coreledger.base_url, mock_settings)
    create = _ref_element(surface, "Create sub-account")
    result = gated.perform(Click(create, dialog=CONFIRM), declared_risk="irreversible")
    assert (result.ok, result.code) == (False, "CONFIRMATION_REQUIRED")
    assert len(coreledger.state.ledger.subaccounts) == 0
    expect(main.get_by_text("Open sub-account for")).to_be_visible()


def test_escalate_handling_is_never_satisfied_by_the_confirm_flag(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated, surface = _gated(
        page,
        coreledger.base_url,
        "coreledger-subaccount",
        confirm=True,
        irreversible_handling="escalate",
    )
    _open_form(page, coreledger.base_url, mock_settings)
    result = gated.perform(
        Click(_ref_element(surface, "Create sub-account"), dialog=CONFIRM),
        declared_risk="irreversible",
    )
    assert result.code == "CONFIRMATION_REQUIRED"
    assert "escalate" in result.message


def test_a_confirmed_click_answers_the_expected_dialog(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated, surface = _gated(page, coreledger.base_url, "coreledger-subaccount", confirm=True)
    main = _open_form(page, coreledger.base_url, mock_settings)
    result = gated.perform(
        Click(_ref_element(surface, "Create sub-account"), dialog=CONFIRM),
        declared_risk="irreversible",
    )
    events = _events_after(surface, result)
    assert result.ok, result
    assert [e.kind for e in events if e.kind.startswith("dialog")] == ["dialog_answered"]
    expect(main.get_by_text("Sub-account opened")).to_be_visible()
    assert len(coreledger.state.ledger.subaccounts) == 1


def test_a_dialog_other_than_the_expected_one_is_dismissed(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated, surface = _gated(page, coreledger.base_url, "coreledger-subaccount", confirm=True)
    main = _open_form(page, coreledger.base_url, mock_settings)
    other = ExpectedDialog(
        dialog_type="confirm", message_pattern="Delete this member", response="accept"
    )
    result = gated.perform(
        Click(_ref_element(surface, "Create sub-account"), dialog=other),
        declared_risk="irreversible",
    )
    events = _events_after(surface, result)
    assert [e for e in events if e.kind == "unexpected_dialog"], events
    assert result.code == "ACTION_FAILED"
    assert len(coreledger.state.ledger.subaccounts) == 0
    expect(main.get_by_text("Open sub-account for")).to_be_visible()


def test_automation_cannot_act_while_a_human_holds_control(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    def human_holds_control() -> None:
        raise NotInControl("run r is human_active; controller is human, not automation")

    policy = load_policy(POLICY_FILE, "coreledger-readonly").model_copy(
        update={"origins": [coreledger.base_url]}
    )
    surface = PlaywrightSurface(page, control=human_holds_control)
    gated = GatedSurface(surface, surface, PolicyGate(policy))
    main = _sign_in(page, coreledger.base_url, mock_settings)
    ref = next(n.ref for n in surface.snapshot().nodes if n.role == "textbox" and n.ref)
    textbox = surface.element_for_ref(ref)
    with pytest.raises(NotInControl):
        gated.perform(TypeText(textbox, "10007"), declared_risk="reversible")
    assert main.get_by_role("textbox").input_value() == ""


def test_the_surface_refuses_to_act_without_a_navigation_guard(
    page: Page, coreledger: RunningMockApp
) -> None:
    surface = PlaywrightSurface(page)
    with pytest.raises(RuntimeError, match="navigation guard"):
        surface.act(Navigate(coreledger.base_url + "/"))  # repo rule allows tests to call act
