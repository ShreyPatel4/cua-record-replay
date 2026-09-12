"""CoreLedger in real Chromium: frames, span click handlers, confirm() gating, and the interstitial.

Also pins the hostility claims the locator ladder is designed around, so they cannot quietly soften.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Frame, Page, expect

from mock_app.server import RunningMockApp
from mock_app.settings import MockSettings

from conftest import Injector

pytestmark = pytest.mark.browser


def _main_frame(page: Page) -> Frame:
    frame = page.frame(name="main")
    assert frame is not None, "frameset must expose a frame named main"
    return frame


def _sign_in(page: Page, base_url: str, settings: MockSettings) -> Frame:
    page.goto(base_url + "/")
    main = _main_frame(page)
    main.get_by_label("Operator ID").fill(settings.operator_user)
    main.get_by_label("Password").fill(settings.operator_password)
    main.get_by_role("button", name="Sign in").click()
    expect(main.get_by_text("Member Lookup")).to_be_visible()
    return main


def test_operator_reads_savings_balance_through_frames(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    main.get_by_role("textbox").fill("10007")
    main.get_by_text("Find", exact=True).click()
    expect(main.get_by_text("Member profile")).to_be_visible()
    savings = main.locator("td", has_text=re.compile(r"^Share Savings$")).locator(
        "xpath=following-sibling::td[1]"
    )
    expect(savings).to_have_text("$4,210.55")


def test_legacy_controls_have_no_accessible_role_or_name(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    # The Find span is invisible to role-based targeting, and the textbox has no accessible name.
    expect(main.get_by_role("button", name="Find")).to_have_count(0)
    expect(main.get_by_role("textbox", name="Member number")).to_have_count(0)
    snapshot = main.locator("body").aria_snapshot()
    assert "button" not in snapshot


def test_confirm_dialog_is_the_only_way_to_create(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    _sign_in(page, coreledger.base_url, mock_settings)
    # Open the form top-level so dialog handling is on the page, not a frame.
    page.goto(coreledger.base_url + "/members/10007/subaccounts/new")
    page.locator("select").select_option(label="Money Market")
    # Nested tables mean an outer <tr> also "has text" Initial deposit; anchor on the label cell.
    deposit = page.locator("td", has_text=re.compile(r"^Initial deposit$")).locator(
        "xpath=following-sibling::td[1]//input"
    )
    deposit.fill("25.00")
    deposit.press("Enter")
    expect(page.get_by_text("Create sub-account")).to_be_visible()

    page.once("dialog", lambda dialog: dialog.dismiss())
    page.get_by_text("Create sub-account").click()
    expect(page.get_by_text("Create sub-account")).to_be_visible()
    assert not coreledger.state.ledger.subaccounts

    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_text("Create sub-account").click()
    expect(page.get_by_text("Sub-account opened")).to_be_visible()
    assert len(coreledger.state.ledger.subaccounts) == 1


def test_interstitial_blocks_until_ok_is_clicked(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings, inject: Injector
) -> None:
    _sign_in(page, coreledger.base_url, mock_settings)
    inject("interstitial", times=1)
    page.goto(coreledger.base_url + "/members/10007")
    notice = page.get_by_text("System notice")
    expect(notice).to_be_visible()
    page.get_by_text("OK", exact=True).click()
    expect(notice).to_be_hidden()


def test_layout_drift_renames_and_moves_the_search_action(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings, inject: Injector
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    find_box = main.get_by_text("Find", exact=True).bounding_box()
    inject("layout_drift")
    main.goto(coreledger.base_url + "/members/search")
    expect(main.get_by_text("Find", exact=True)).to_have_count(0)
    search_box = main.get_by_text("Search", exact=True).bounding_box()
    assert find_box is not None
    assert search_box is not None
    assert search_box["y"] > find_box["y"] + 5, "drifted action should move to a lower row"
