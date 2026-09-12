"""PlaywrightSurface and the locator ladder against live CoreLedger in Chromium.

Recording and resolving share one page script, so these tests pin both ends of a ladder at once.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pytest
from playwright.sync_api import Frame, Page, expect

from cua.artifact.catalog import load_capability
from cua.artifact.schema import (
    AnchorRelativeRung,
    ClickStep,
    ElementPresent,
    Fingerprint,
    Target,
    TextExactRung,
    TypeTextStep,
    iter_conditions,
)
from cua.locate.recorder import Recording, record
from cua.locate.resolver import Resolved, Unresolved, resolve
from cua.policy.enforce import GatedSurface
from cua.policy.gate import PolicyGate
from cua.policy.models import load_policy
from cua.surface.base import Click, Element, ReadText, SnapshotNode, TypeText
from cua.surface.desktop import DesktopSurface
from cua.surface.playwright import PlaywrightSurface
from mock_app.server import RunningMockApp
from mock_app.settings import MockSettings

from support import ROOT, Injector

pytestmark = pytest.mark.browser
EXAMPLE = ROOT / "artifacts" / "example.capability.json"
POLICY_FILE = ROOT / "policy" / "allowlist.yaml"


@pytest.fixture
def surface(page: Page) -> PlaywrightSurface:
    return PlaywrightSurface(page)


def gated_for(surface: PlaywrightSurface, base_url: str) -> GatedSurface:
    policy = load_policy(POLICY_FILE, "coreledger-readonly").model_copy(
        update={"origins": [base_url]}
    )
    return GatedSurface(surface, surface, PolicyGate(policy))


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


def _node(surface: PlaywrightSurface, match: Callable[[SnapshotNode], bool]) -> SnapshotNode:
    return next(n for n in surface.snapshot().nodes if n.ref and match(n))


def _element(surface: PlaywrightSurface, role: str, name: str = "") -> Element:
    node = _node(surface, lambda n: n.role == role and n.name == name)
    assert node.ref is not None
    return surface.element_for_ref(node.ref)


def _assert_every_rung_resolves_to(
    surface: PlaywrightSurface, recording: Recording, element: Element
) -> None:
    for rung in recording.target.ladder:
        found = surface.candidates(rung, recording.target.frame_path)
        assert len(found) == 1, f"{rung.strategy} matched {len(found)}"
        assert surface.same_element(found[0], element), f"{rung.strategy} found another element"


def test_snapshot_is_frame_aware_with_page_absolute_boxes(
    page: Page, surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    snapshot = surface.snapshot()
    textbox = next(n for n in snapshot.nodes if n.role == "textbox")
    real = main.get_by_role("textbox").bounding_box()
    assert textbox.frame_path == ["main"]
    assert textbox.box is not None
    assert real is not None
    assert abs(textbox.box.x - real["x"]) < 2
    assert abs(textbox.box.y - real["y"]) < 2, "boxes inside a frame carry the frame offset"
    find = next(n for n in snapshot.nodes if n.role == "cell" and n.name == "Find")
    assert find.clickable, "found through the onclick pass, the aria tree does not mark it"
    sign_out = next(n for n in snapshot.nodes if n.name == "Sign out")
    assert sign_out.frame_path == ["banner"]
    assert snapshot.frame_urls["main"].endswith("/members/search")


def test_hidden_card_is_absent_and_the_digest_tracks_the_screen(
    page: Page,
    surface: PlaywrightSurface,
    coreledger: RunningMockApp,
    mock_settings: MockSettings,
    inject: Injector,
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    before = surface.snapshot()
    assert surface.snapshot().digest == before.digest
    inject("interstitial", times=1)
    main.goto(coreledger.base_url + "/members/10007")
    expect(main.get_by_text("System notice")).to_be_visible()
    notice = surface.snapshot()
    assert notice.digest != before.digest
    assert not [n for n in notice.nodes if n.name == "Share Savings"]
    assert next(n for n in notice.nodes if n.name == "OK").clickable


def test_a_ref_resolves_to_the_control_behind_the_cell(
    page: Page, surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    _sign_in(page, coreledger.base_url, mock_settings)
    facts = surface.describe(_element(surface, "cell", "Find"))
    assert (facts.tag, facts.role, facts.kind, facts.own_text) == (
        "span",
        "cell",
        "clickable",
        "Find",
    )


def test_sign_in_controls_record_by_role_and_label(
    page: Page, surface: PlaywrightSurface, coreledger: RunningMockApp
) -> None:
    page.goto(coreledger.base_url + "/")
    expect(_main(page).get_by_text("Operator sign-in")).to_be_visible()
    operator = _element(surface, "textbox", "Operator ID")
    recording = record(operator, surface)
    assert [r.strategy for r in recording.target.ladder][:2] == ["role_name", "label_text"]
    _assert_every_rung_resolves_to(surface, recording, operator)

    password = _element(surface, "textbox", "Password")
    secret = record(password, surface, sensitive=True)
    assert secret.target.fingerprint.input_type == "password"
    assert secret.target.fingerprint.text is None
    assert secret.target.looks_sensitive


def test_legacy_search_controls_record_without_names(
    page: Page, surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    _sign_in(page, coreledger.base_url, mock_settings)
    textbox = _element(surface, "textbox")
    typed = record(textbox, surface)
    assert [r.strategy for r in typed.target.ladder] == ["label_text", "anchor_relative", "bbox"]
    _assert_every_rung_resolves_to(surface, typed, textbox)

    find = _element(surface, "cell", "Find")
    clicked = record(find, surface)
    assert [r.strategy for r in clicked.target.ladder] == ["text_exact", "anchor_relative", "bbox"]
    anchor = clicked.target.ladder[1]
    assert isinstance(anchor, AnchorRelativeRung)
    assert (anchor.anchor_text, anchor.direction, anchor.same_row, anchor.target_kind) == (
        "Member number",
        "right",
        True,
        "clickable",
    )
    _assert_every_rung_resolves_to(surface, clicked, find)


def test_a_volatile_value_is_recorded_by_position_and_flagged_weak(
    page: Page, surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    main.goto(coreledger.base_url + "/members/10007")
    amount = _element(surface, "cell", "$4,210.55")
    recording = record(amount, surface, volatile_text=True)
    assert recording.weak
    first = recording.target.ladder[0]
    assert isinstance(first, AnchorRelativeRung)
    assert (first.anchor_text, first.direction, first.target_kind) == (
        "Share Savings",
        "right",
        "text",
    )
    assert recording.target.fingerprint.text is None
    _assert_every_rung_resolves_to(surface, recording, amount)


def test_two_matches_is_a_failure_and_a_lower_rung_then_decides(
    page: Page, surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    main.set_content(
        "<table><tr><td>Row A</td><td onclick='void 0'>Go</td></tr>"
        "<tr><td>Row B</td><td onclick='void 0'>Go</td></tr></table>"
    )
    text_only = Target(
        ladder=[TextExactRung(strategy="text_exact", text="Go", confidence=0.6)],
        recorded_rung=0,
        frame_path=["main"],
        fingerprint=Fingerprint(role="cell", name="Go"),
        notes="n",
    )
    ambiguous = resolve(text_only, surface)
    assert isinstance(ambiguous, Unresolved)
    assert (ambiguous.code, ambiguous.attempts[0].matches) == ("TARGET_AMBIGUOUS", 2)

    anchored = text_only.model_copy(
        update={
            "ladder": [
                *text_only.ladder,
                AnchorRelativeRung(
                    strategy="anchor_relative",
                    anchor_text="Row B",
                    direction="right",
                    same_row=True,
                    target_kind="clickable",
                    confidence=0.8,
                ),
            ]
        }
    )
    resolved = resolve(Target.model_validate(anchored.model_dump()), surface)
    assert isinstance(resolved, Resolved)
    assert (resolved.strategy, resolved.drift) == ("anchor_relative", True)


def test_layout_drift_falls_to_the_anchor_rung_and_the_flow_still_works(
    page: Page,
    surface: PlaywrightSurface,
    coreledger: RunningMockApp,
    mock_settings: MockSettings,
    inject: Injector,
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    textbox = record(_element(surface, "textbox"), surface).target
    find = record(_element(surface, "cell", "Find"), surface).target

    inject("layout_drift")
    main.goto(coreledger.base_url + "/members/search")
    expect(main.get_by_text("Search", exact=True)).to_be_visible()

    typed = resolve(textbox, surface)
    clicked = resolve(find, surface)
    assert isinstance(typed, Resolved)
    assert isinstance(clicked, Resolved)
    assert (typed.strategy, typed.drift) == ("label_text", False)
    assert (clicked.strategy, clicked.rung_index, clicked.drift) == ("anchor_relative", 1, True)
    assert [a.matches for a in clicked.attempts] == [0, 1]
    assert surface.describe(clicked.element).own_text == "Search"
    bbox_hit = surface.candidates(find.ladder[2], find.frame_path)
    assert not (bbox_hit and surface.same_element(bbox_hit[0], clicked.element)), (
        "coordinates broke"
    )

    gated = gated_for(surface, coreledger.base_url)
    assert gated.perform(TypeText(typed.element, "10007"), declared_risk="reversible").ok
    assert gated.perform(Click(clicked.element)).ok
    expect(main.get_by_text("Member profile")).to_be_visible()


def test_frame_path_scopes_resolution(
    page: Page, surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    _sign_in(page, coreledger.base_url, mock_settings)
    recorded = record(_element(surface, "cell", "Find"), surface).target
    # Drop the bbox rung: coordinates hit something in any frame, which is why they rank last.
    find = recorded.model_copy(update={"ladder": recorded.ladder[:2]})
    assert isinstance(resolve(find.model_copy(update={"frame_path": []}), surface), Unresolved)
    assert isinstance(
        resolve(find.model_copy(update={"frame_path": ["banner"]}), surface), Unresolved
    )
    sign_out = record(_element(surface, "link", "Sign out"), surface)
    assert sign_out.target.frame_path == ["banner"]
    assert sign_out.target.ladder[0].strategy == "role_name"


def test_the_hand_written_example_resolves_on_the_live_app(
    page: Page, surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    """Every target in the example artifact lands on one element at its recorded rung."""
    capability = load_capability(EXAMPLE)
    steps = {s.id: s for s in capability.steps}
    gated = gated_for(surface, coreledger.base_url)
    page.goto(coreledger.base_url + "/")
    main = _main(page)
    expect(main.get_by_text("Operator sign-in")).to_be_visible()

    def at_recorded_rung(target: Target) -> Element:
        result = resolve(target, surface)
        assert isinstance(result, Resolved), result
        assert result.rung_index == target.recorded_rung
        return result.element

    values = {
        "s02": mock_settings.operator_user,
        "s03": mock_settings.operator_password,
        "s05": "10007",
    }
    for step_id in ("s02", "s03", "s04", "s05", "s06"):
        step = steps[step_id]
        assert isinstance(step, TypeTextStep | ClickStep)
        element = at_recorded_rung(step.target)
        action = (
            TypeText(element, values[step_id]) if isinstance(step, TypeTextStep) else Click(element)
        )
        result = gated.perform(action, declared_risk=step.risk)
        assert result.ok, result
        if step_id == "s04":
            expect(main.get_by_text("Member Lookup")).to_be_visible()
            ready = capability.checkpoints["cp_search_ready"].condition
            for condition in iter_conditions(ready):
                if isinstance(condition, ElementPresent):
                    at_recorded_rung(condition.target)

    expect(main.get_by_text("Member profile")).to_be_visible()
    savings = at_recorded_rung(capability.outputs[0].extract.target)
    assert gated.perform(ReadText(savings)).text == "$4,210.55"
    assert surface.settle(quiet_ms=200, timeout_ms=5000)
    assert surface.frame_status(["main"]) == 200
    assert re.search(r"/members/10007$", surface.frame_url(["main"]))
    assert "Share Savings" in surface.frame_text(["main"])


def test_status_codes_are_tracked_per_frame(
    page: Page,
    surface: PlaywrightSurface,
    coreledger: RunningMockApp,
    mock_settings: MockSettings,
    inject: Injector,
) -> None:
    main = _sign_in(page, coreledger.base_url, mock_settings)
    inject("app_error", times=1, on="detail")
    main.goto(coreledger.base_url + "/members/10007")
    assert surface.settle(quiet_ms=100, timeout_ms=5000)
    assert surface.frame_status(["main"]) == 500
    assert surface.frame_status([]) == 200


def test_desktop_surface_is_a_documented_stub() -> None:
    desktop = DesktopSurface()
    assert "UI Automation" in (DesktopSurface.__doc__ or "")
    with pytest.raises(NotImplementedError, match="designed, not built"):
        desktop.snapshot()
    with pytest.raises(NotImplementedError):
        desktop.candidates(TextExactRung(strategy="text_exact", text="x", confidence=1), [])
