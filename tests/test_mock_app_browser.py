"""CoreLedger in real Chromium: frames, span click handlers, confirm() gating, and the interstitial.

Also pins the hostility and drift geometry the locator ladder is designed around.
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Frame, Locator, Page, Request, expect

from mock_app.server import RunningMockApp
from mock_app.settings import MockSettings

from support import Injector

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


def _savings_cell(frame: Frame) -> Locator:
    return frame.locator("td", has_text=re.compile(r"^Share Savings$")).locator(
        "xpath=following-sibling::td[1]"
    )


def test_operator_reads_savings_balance_through_frames(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    main.get_by_role("textbox").fill("10007")
    main.get_by_text("Find", exact=True).click()
    expect(main.get_by_text("Member profile")).to_be_visible()
    expect(_savings_cell(main)).to_have_text("$4,210.55")


def test_legacy_controls_surface_only_as_unnamed_textbox_and_text_cells(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    # Find is present in the aria tree, but as a table cell, never as a button.
    expect(main.get_by_role("button", name="Find")).to_have_count(0)
    expect(main.get_by_role("cell", name="Find", exact=True)).to_have_count(1)
    # The textbox has no accessible name; its label lives in a neighbouring cell.
    expect(main.get_by_role("textbox", name="Member number")).to_have_count(0)
    expect(main.get_by_role("textbox")).to_have_count(1)
    snapshot = main.locator(":root").aria_snapshot()
    assert 'cell "Find"' in snapshot
    assert "button" not in snapshot


def test_confirm_dialog_inside_the_frame_is_the_only_way_to_create(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    main.goto(coreledger.base_url + "/members/10007/subaccounts/new")
    main.locator("select").select_option(label="Money Market")
    deposit = main.locator("td", has_text=re.compile(r"^Initial deposit$")).locator(
        "xpath=following-sibling::td[1]//input"
    )
    deposit.fill("25.00")
    deposit.press("Enter")

    # Frame dialogs reach the page-level handler.
    page.once("dialog", lambda dialog: dialog.dismiss())
    main.get_by_text("Create sub-account", exact=True).click()
    page.once("dialog", lambda dialog: dialog.accept())
    main.get_by_text("Create sub-account", exact=True).click()
    expect(main.get_by_text("Sub-account opened")).to_be_visible()

    # Enter and the dismissed dialog created nothing; only the accepted dialog did.
    assert len(coreledger.state.ledger.subaccounts) == 1


def test_interstitial_hides_the_card_until_ok_is_clicked(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings, inject: Injector
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    inject("interstitial", times=1)
    main.goto(coreledger.base_url + "/members/10007")
    expect(main.get_by_text("System notice")).to_be_visible()
    expect(_savings_cell(main)).to_be_hidden()
    expect(main.get_by_role("link", name="Open sub-account")).to_have_count(0)
    main.get_by_text("OK", exact=True).click()
    expect(main.get_by_text("System notice")).to_be_hidden()
    expect(_savings_cell(main)).to_have_text("$4,210.55")


def test_sticky_interstitial_never_reveals_the_card(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings, inject: Injector
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    inject("interstitial", sticky="1")
    main.goto(coreledger.base_url + "/members/10007")
    main.get_by_text("OK", exact=True).click()
    expect(main.get_by_text("System notice")).to_be_visible()
    expect(_savings_cell(main)).to_be_hidden()


def test_layout_drift_moves_the_action_one_cell_right_in_the_same_row(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings, inject: Injector
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    textbox = main.get_by_role("textbox").bounding_box()
    find_box = main.get_by_text("Find", exact=True).bounding_box()
    inject("layout_drift")
    main.goto(coreledger.base_url + "/members/search")
    expect(main.get_by_text("Find", exact=True)).to_have_count(0)
    search = main.get_by_text("Search", exact=True)
    expect(search).to_have_count(1)
    search_box = search.bounding_box()
    assert textbox is not None
    assert find_box is not None
    assert search_box is not None
    # Text rungs break, the bbox rung breaks (moved right, no overlap), a same-row anchor survives.
    assert abs(search_box["y"] - find_box["y"]) < 6, "drifted action stays in the textbox row"
    assert search_box["x"] > find_box["x"] + find_box["width"], "drifted action moved right"
    assert search_box["x"] > textbox["x"] + textbox["width"], "still right of the textbox"


def test_enter_in_the_search_box_does_not_submit(
    page: Page, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    requests: list[Request] = []
    page.on("request", lambda request: requests.append(request))
    main.get_by_role("textbox").fill("10007")
    main.get_by_role("textbox").press("Enter")
    # A round trip after the key press flushes any navigation request it would have started.
    assert main.evaluate("fetch('/banner').then(r => r.status)") == 200
    assert not [r for r in requests if "q=10007" in r.url]
    expect(main.get_by_text("Member Lookup")).to_be_visible()
