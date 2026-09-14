"""GatedSurface in Chromium: the gate judges actions, every request they cause, and dialogs.

Also the control lock: automation neither acts nor reads while a human holds the session, and the
human's own dialogs and navigation are left alone.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Frame, expect

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

from conftest import HandOff
from support import ROOT, Injector

pytestmark = pytest.mark.browser
POLICY_FILE = ROOT / "policy" / "allowlist.yaml"
CONFIRM = ExpectedDialog(
    dialog_type="confirm", message_pattern=r"cannot be undone", response="accept"
)


def _gate(
    surface: PlaywrightSurface,
    base_url: str,
    policy_id: str = "coreledger-readonly",
    *,
    confirm: bool = False,
    **policy: object,
) -> GatedSurface:
    resolved = load_policy(POLICY_FILE, policy_id).model_copy(
        update={"origins": [base_url], **policy}
    )
    return GatedSurface(surface, surface, PolicyGate(resolved), confirm_irreversible=confirm)


def _main(surface: PlaywrightSurface) -> Frame:
    frame = surface.page.frame(name="main")
    assert frame is not None
    return frame


def _sign_in(surface: PlaywrightSurface, base_url: str, settings: MockSettings) -> Frame:
    surface.page.goto(base_url + "/")
    main = _main(surface)
    main.get_by_label("Operator ID").fill(settings.operator_user)
    main.get_by_label("Password").fill(settings.operator_password)
    main.get_by_role("button", name="Sign in").click()
    expect(main.get_by_text("Member Lookup")).to_be_visible()
    return main


def _by_name(surface: PlaywrightSurface, name: str) -> Element:
    node = next(n for n in surface.snapshot().nodes if n.ref and n.name == name)
    assert node.ref is not None
    return surface.element_for_ref(node.ref)


def _events_of(surface: PlaywrightSurface, result: ActResult) -> list[SurfaceEvent]:
    """The act's own events plus its late arrivals, after the page settles."""
    surface.settle(quiet_ms=200, timeout_ms=5000)
    late = [e for e in surface.drain_events() if e.act_id == result.act_id]
    return [*result.events, *late]


def _open_form(surface: PlaywrightSurface, base_url: str, settings: MockSettings) -> Frame:
    main = _sign_in(surface, base_url, settings)
    main.goto(base_url + "/members/10007/subaccounts/new")
    main.locator("select").select_option(label="Money Market")
    main.locator("input[name=initial_deposit]").fill("25.00")
    return main


def test_navigate_actions_are_judged_before_loading(
    surface: PlaywrightSurface, coreledger: RunningMockApp
) -> None:
    gated = _gate(surface, coreledger.base_url)
    blocked = gated.perform(Navigate(coreledger.base_url + "/__control"))
    assert (blocked.ok, blocked.code) == (False, "POLICY_BLOCKED")
    assert "paths_deny" in blocked.message
    assert surface.page.url == "about:blank"
    assert gated.perform(Navigate(coreledger.base_url + "/")).ok


def test_a_click_that_navigates_off_policy_is_cancelled_in_flight(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated = _gate(surface, coreledger.base_url)
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    result = gated.perform(Click(_by_name(surface, "Sign out")))
    blocked = [e for e in _events_of(surface, result) if e.kind == "navigation_blocked"]
    assert blocked
    assert "paths_deny /logout" in blocked[0].detail
    assert main.url.endswith("/members/search")
    assert surface.frame_status(["main"]) == 200, "the guard's 204 is not the frame's status"
    main.goto(coreledger.base_url + "/members/search")
    expect(main.get_by_text("Member Lookup")).to_be_visible()


def test_a_redirect_off_policy_is_refused_before_the_browser_follows_it(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated = _gate(surface, coreledger.base_url)
    surface.page.goto(coreledger.base_url + "/")
    main = _main(surface)
    expect(main.get_by_text("Operator sign-in")).to_be_visible()
    main.locator("input[name=next]").evaluate("(el) => { el.value = '/__control' }")
    main.get_by_label("Operator ID").fill(mock_settings.operator_user)
    main.get_by_label("Password").fill(mock_settings.operator_password)
    result = gated.perform(Click(_by_name(surface, "Sign in")))
    events = _events_of(surface, result)
    assert [e for e in events if e.kind == "redirect_blocked" and "/__control" in e.url], events
    assert "/__control" not in main.url
    expect(main.get_by_text("Operator sign-in")).to_be_visible()


def test_the_sign_in_redirect_still_works_through_the_gate(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated = _gate(surface, coreledger.base_url)
    assert gated.perform(Navigate(coreledger.base_url + "/")).ok
    assert surface.settle(quiet_ms=200, timeout_ms=5000)
    main = _main(surface)
    expect(main.get_by_text("Operator sign-in")).to_be_visible()
    for name, value in (
        ("Operator ID", mock_settings.operator_user),
        ("Password", mock_settings.operator_password),
    ):
        assert gated.perform(
            TypeText(_by_name(surface, name), value), declared_risk="reversible"
        ).ok
    result = gated.perform(Click(_by_name(surface, "Sign in")))
    expect(main.get_by_text("Member Lookup")).to_be_visible()
    assert not [e for e in _events_of(surface, result) if e.kind.endswith("blocked")]


def test_popups_are_judged_and_closed(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated = _gate(surface, coreledger.base_url)
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    main.set_content(
        "<a href='/members/search' target='_blank'>New window</a>"
        "<span onclick=\"window.open('/__control')\">Control</span>"
    )
    allowed = gated.perform(Click(_by_name(surface, "New window")))
    assert [e.kind for e in _events_of(surface, allowed)] == ["popup_closed"]
    denied = gated.perform(Click(_by_name(surface, "Control")))
    assert "navigation_blocked" in {e.kind for e in _events_of(surface, denied)}
    # A popup whose first request was refused never loads, so Playwright never surfaces it to close;
    # the guarantee is that no page reaches the denied path.
    assert not [p for p in surface.page.context.pages if "/__control" in p.url]


def test_downloads_fetches_and_data_documents_cannot_slip_past(
    surface: PlaywrightSurface,
    coreledger: RunningMockApp,
    mock_settings: MockSettings,
    inject: Injector,
) -> None:
    gated = _gate(surface, coreledger.base_url)
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    inject("interstitial")
    links = (
        "<a href='/logout' download>Export</a>"
        "<span onclick=\\\"fetch('/__control/api/reset', {method: 'POST'})\\\">Reset</span>"
        "<span onclick=\\\"location.href='data:text/html,elsewhere'\\\">Elsewhere</span>"
    )
    # A real navigation, then a legacy document.open() rewrite: the download guard must survive it.
    page_html = f'<script>document.open(); document.write("{links}"); document.close();</script>'
    surface.page.route(
        "**/members/playground",
        lambda route: route.fulfill(body=page_html, content_type="text/html"),
    )
    main.goto(coreledger.base_url + "/members/playground")
    expect(main.get_by_text("Export")).to_be_visible()
    export = gated.perform(Click(_by_name(surface, "Export")))
    assert [e.kind for e in _events_of(surface, export)] == ["download_blocked"]
    reset = gated.perform(Click(_by_name(surface, "Reset")))
    assert [e.kind for e in _events_of(surface, reset)] == ["request_blocked"]
    assert "interstitial" in coreledger.state.faults.snapshot(), "the reset never reached the app"
    elsewhere = gated.perform(Click(_by_name(surface, "Elsewhere")))
    assert "navigation_off_policy" in [e.kind for e in _events_of(surface, elsewhere)]
    # From an off-policy document the frame is stuck (its URL fails the gate), so recovery is a
    # top-level navigation to an allowed page.
    assert gated.perform(Navigate(coreledger.base_url + "/")).ok
    assert surface.settle(quiet_ms=200, timeout_ms=5000)
    expect(_main(surface).get_by_text("Member Lookup")).to_be_visible()


def test_an_act_reports_only_its_own_side_effects(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated = _gate(surface, coreledger.base_url)
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    main.set_content(
        "<input type='text'><script>setTimeout(() => { location.href = '/logout'; }, 50)</script>"
    )
    surface.page.wait_for_timeout(400)
    textbox = surface.element_for_ref(
        next(n.ref for n in surface.snapshot().nodes if n.role == "textbox" and n.ref)
    )
    typed = gated.perform(TypeText(textbox, "hello"), declared_risk="reversible")
    assert typed.ok, typed
    stray = surface.drain_events()
    assert [e.kind for e in stray] == ["navigation_blocked"]
    assert stray[0].act_id != typed.act_id


def test_an_irreversible_click_needs_confirmation_before_anything_happens(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated = _gate(surface, coreledger.base_url, "coreledger-subaccount")
    main = _open_form(surface, coreledger.base_url, mock_settings)
    result = gated.perform(
        Click(_by_name(surface, "Create sub-account"), dialog=CONFIRM), declared_risk="irreversible"
    )
    assert (result.ok, result.code) == (False, "CONFIRMATION_REQUIRED")
    assert len(coreledger.state.ledger.subaccounts) == 0
    expect(main.get_by_text("Open sub-account for")).to_be_visible()


def test_escalate_handling_is_never_satisfied_by_the_confirm_flag(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated = _gate(
        surface,
        coreledger.base_url,
        "coreledger-subaccount",
        confirm=True,
        irreversible_handling="escalate",
    )
    _open_form(surface, coreledger.base_url, mock_settings)
    result = gated.perform(
        Click(_by_name(surface, "Create sub-account"), dialog=CONFIRM), declared_risk="irreversible"
    )
    assert result.code == "CONFIRMATION_REQUIRED"
    assert "escalate" in result.message


def test_a_confirmed_click_answers_the_expected_dialog(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated = _gate(surface, coreledger.base_url, "coreledger-subaccount", confirm=True)
    main = _open_form(surface, coreledger.base_url, mock_settings)
    result = gated.perform(
        Click(_by_name(surface, "Create sub-account"), dialog=CONFIRM), declared_risk="irreversible"
    )
    assert result.ok, result
    assert [e.kind for e in result.events if e.kind.startswith("dialog")] == ["dialog_answered"]
    expect(main.get_by_text("Sub-account opened")).to_be_visible()
    assert len(coreledger.state.ledger.subaccounts) == 1


def test_a_dialog_other_than_the_expected_one_is_dismissed(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated = _gate(surface, coreledger.base_url, "coreledger-subaccount", confirm=True)
    main = _open_form(surface, coreledger.base_url, mock_settings)
    other = ExpectedDialog(
        dialog_type="confirm", message_pattern="Delete this member", response="accept"
    )
    result = gated.perform(
        Click(_by_name(surface, "Create sub-account"), dialog=other), declared_risk="irreversible"
    )
    assert [e.kind for e in result.events] == ["unexpected_dialog"]
    assert result.code == "ACTION_FAILED"
    assert len(coreledger.state.ledger.subaccounts) == 0
    expect(main.get_by_text("Open sub-account for")).to_be_visible()


def test_an_expected_dialog_that_never_opens_disarms_the_surface(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    """The reviewer's case: a failed irreversible click must not leave a later confirm accepted."""
    gated = _gate(surface, coreledger.base_url, "coreledger-subaccount", confirm=True)
    main = _open_form(surface, coreledger.base_url, mock_settings)
    result = gated.perform(
        Click(_by_name(surface, "Account type"), dialog=CONFIRM), declared_risk="irreversible"
    )
    assert (result.ok, result.message) == (False, "expected confirm dialog did not appear")
    answered = main.evaluate("() => confirm('Open this sub-account? This cannot be undone.')")
    assert answered is False
    assert [e.kind for e in surface.drain_events()] == ["unexpected_dialog"]
    assert len(coreledger.state.ledger.subaccounts) == 0


def test_a_human_holding_control_is_left_alone_and_automation_is_locked_out(
    surface: PlaywrightSurface,
    hand_off: HandOff,
    coreledger: RunningMockApp,
    mock_settings: MockSettings,
) -> None:
    gated = _gate(surface, coreledger.base_url, "coreledger-subaccount")
    main = _open_form(surface, coreledger.base_url, mock_settings)
    ref = next(n.ref for n in surface.snapshot().nodes if n.role == "textbox" and n.ref)
    textbox = surface.element_for_ref(ref)
    hand_off.holder = "human"
    with pytest.raises(NotInControl):
        gated.perform(TypeText(textbox, "99.00"), declared_risk="reversible")
    with pytest.raises(NotInControl):
        surface.snapshot()

    # The human's confirm is theirs to answer, and their navigation is not gated.
    surface.page.once("dialog", lambda dialog: dialog.accept())
    main.get_by_text("Create sub-account", exact=True).click()
    expect(main.get_by_text("Sub-account opened")).to_be_visible()
    assert len(coreledger.state.ledger.subaccounts) == 1
    main.goto(coreledger.base_url + "/__control")
    expect(main.get_by_text("Failure injection", exact=False)).to_be_visible()
    hand_off.holder = "automation"
    assert surface.drain_events() == []


def test_a_stale_element_is_a_failed_action_not_a_crash(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    gated = _gate(surface, coreledger.base_url)
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    find = _by_name(surface, "Find")
    main.goto(coreledger.base_url + "/members/10007")
    expect(main.get_by_text("Member profile")).to_be_visible()
    result = gated.perform(Click(find))
    assert (result.ok, result.code) == (False, "ACTION_FAILED")


def test_the_guard_is_required_and_installed_once(
    surface: PlaywrightSurface, coreledger: RunningMockApp
) -> None:
    with pytest.raises(RuntimeError, match="request guard"):
        surface.act(Navigate(coreledger.base_url + "/"))  # tests may call act; src may not
    _gate(surface, coreledger.base_url)
    with pytest.raises(RuntimeError, match="cannot be replaced"):
        surface.install_request_guard(lambda check: None)
