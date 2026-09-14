"""PlaywrightSurface and the locator ladder against live CoreLedger in Chromium.

Recording and resolving share one page script, so these tests pin both ends of a ladder at once.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

import pytest
from playwright.sync_api import Frame, expect

from cua.artifact.catalog import load_capability
from cua.artifact.schema import (
    AnchorRelativeRung,
    ClickStep,
    ElementPresent,
    Fingerprint,
    LabelTextRung,
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
from cua.session.state import NotInControl
from cua.surface.base import Click, Element, ReadText, SnapshotNode, TypeText
from cua.surface.desktop import DesktopSurface
from cua.surface.playwright import PlaywrightSurface
from mock_app.server import RunningMockApp
from mock_app.settings import MockSettings

from conftest import HandOff
from support import ROOT, Injector

pytestmark = pytest.mark.browser
EXAMPLE = ROOT / "artifacts" / "example.capability.json"
POLICY_FILE = ROOT / "policy" / "allowlist.yaml"


def gated_for(surface: PlaywrightSurface, base_url: str) -> GatedSurface:
    policy = load_policy(POLICY_FILE, "coreledger-readonly").model_copy(
        update={"origins": [base_url]}
    )
    return GatedSurface(surface, surface, PolicyGate(policy))


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


def _node(surface: PlaywrightSurface, match: Callable[[SnapshotNode], bool]) -> SnapshotNode:
    return next(n for n in surface.snapshot().nodes if n.ref and match(n))


def _element(surface: PlaywrightSurface, role: str, name: str = "") -> Element:
    node = _node(surface, lambda n: n.role == role and n.name == name)
    assert node.ref is not None
    return surface.element_for_ref(node.ref)


def _record_node(surface: PlaywrightSurface, node: SnapshotNode, **kwargs: bool) -> Recording:
    assert node.ref is not None
    element = surface.element_for_ref(node.ref)
    return record(element, surface, role_hint=node.role, name_hint=node.name or None, **kwargs)


def _assert_every_rung_resolves_to(
    surface: PlaywrightSurface, recording: Recording, element: Element
) -> None:
    for rung in recording.target.ladder:
        found = surface.candidates(rung, recording.target.frame_path)
        assert len(found) == 1, f"{rung.strategy} matched {len(found)}"
        assert surface.same_element(found[0], element), f"{rung.strategy} found another element"


def test_snapshot_is_frame_aware_with_page_absolute_boxes(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
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


def test_typed_values_never_reach_the_snapshot(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    surface.page.goto(coreledger.base_url + "/")
    main = _main(surface)
    main.get_by_label("Operator ID").fill(mock_settings.operator_user)
    main.get_by_label("Password").fill(mock_settings.operator_password)
    snapshot = surface.snapshot()
    dumped = snapshot.model_dump_json() + snapshot.render()
    assert mock_settings.operator_password not in dumped
    assert mock_settings.operator_user not in dumped
    password = next(n for n in snapshot.nodes if n.name == "Password")
    assert password.value_present


def test_hidden_card_is_absent_and_the_digest_tracks_the_screen(
    surface: PlaywrightSurface,
    coreledger: RunningMockApp,
    mock_settings: MockSettings,
    inject: Injector,
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    before = surface.snapshot()
    assert surface.snapshot().digest == before.digest
    inject("interstitial", times=1)
    main.goto(coreledger.base_url + "/members/10007")
    expect(main.get_by_text("System notice")).to_be_visible()
    notice = surface.snapshot()
    assert notice.digest != before.digest
    assert not [n for n in notice.nodes if n.name == "Share Savings"]
    assert next(n for n in notice.nodes if n.name == "OK").clickable


def test_names_with_yaml_punctuation_and_handlers_inside_text_are_targetable(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    main.set_content(
        "<table><tr><td onclick='void 0'>Status: open</td><td>Note #1</td></tr></table>"
        "<p>Read the <span onclick=\"this.textContent='opened'\">details</span> first.</p>"
    )
    snapshot = surface.snapshot()
    names = {n.name for n in snapshot.nodes}
    assert {"Status: open", "Note #1"} <= names
    details = next(n for n in snapshot.nodes if n.role == "clickable" and n.name == "details")
    assert details.ref is not None
    gated = gated_for(surface, coreledger.base_url)
    assert gated.perform(Click(surface.element_for_ref(details.ref))).ok
    expect(main.get_by_text("opened", exact=True)).to_be_visible()


def test_a_ref_resolves_to_the_control_behind_the_cell(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    _sign_in(surface, coreledger.base_url, mock_settings)
    facts = surface.describe(_element(surface, "cell", "Find"))
    assert (facts.tag, facts.role, facts.kind, facts.own_text) == (
        "span",
        "cell",
        "clickable",
        "Find",
    )


def test_sign_in_controls_record_by_role_and_label(
    surface: PlaywrightSurface, coreledger: RunningMockApp
) -> None:
    surface.page.goto(coreledger.base_url + "/")
    expect(_main(surface).get_by_text("Operator sign-in")).to_be_visible()
    operator = _node(surface, lambda n: n.role == "textbox" and n.name == "Operator ID")
    recording = _record_node(surface, operator)
    assert [r.strategy for r in recording.target.ladder][:2] == ["role_name", "label_text"]
    assert operator.ref is not None
    _assert_every_rung_resolves_to(surface, recording, surface.element_for_ref(operator.ref))

    password = _node(surface, lambda n: n.name == "Password")
    secret = _record_node(surface, password, sensitive=True)
    fingerprint = secret.target.fingerprint
    assert (fingerprint.input_type, fingerprint.kind, fingerprint.text) == (
        "password",
        "input",
        None,
    )
    assert secret.target.looks_sensitive


def test_legacy_search_controls_record_without_names(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    _sign_in(surface, coreledger.base_url, mock_settings)
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


def test_data_values_are_recorded_by_position_without_their_text(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
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
    assert "4,210" not in recording.target.model_dump_json()
    _assert_every_rung_resolves_to(surface, recording, amount)

    member_number = _element(surface, "cell", "10007")
    numbered = record(member_number, surface, sensitive=True)
    assert "10007" not in numbered.target.model_dump_json()


def test_a_select_records_through_its_row_label(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    main.goto(coreledger.base_url + "/members/10007/subaccounts/new")
    select = _element(surface, "combobox")
    recording = record(select, surface)
    label = recording.target.ladder[0]
    assert isinstance(label, LabelTextRung)
    assert (label.label, label.relation, label.control) == ("Account type", "same_row", "combobox")
    assert "bbox" not in [r.strategy for r in recording.target.ladder]
    _assert_every_rung_resolves_to(surface, recording, select)


def test_labels_below_and_off_table_rows(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    main.set_content(
        "<div style='width:300px'>Account name</div><input type='text' style='display:block'>"
        "<div style='margin-top:30px;display:flex;gap:8px'><span>Code</span>"
        "<input type='text'></div>"
    )
    first, second = main.locator("input").element_handles()
    below = record(Element(first, ("main",)), surface)
    relations = [(r.label, r.relation) for r in below.target.ladder if isinstance(r, LabelTextRung)]
    assert relations == [("Account name", "below")]
    beside = record(Element(second, ("main",)), surface)
    beside_label = beside.target.ladder[0]
    assert isinstance(beside_label, LabelTextRung)
    assert (beside_label.label, beside_label.relation) == ("Code", "same_row")
    _assert_every_rung_resolves_to(surface, beside, Element(second, ("main",)))


def test_position_ties_are_two_matches_not_a_pick(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    main.set_content(
        "<div style='position:absolute;left:100px;top:10px;width:200px'>Actions</div>"
        "<span onclick='void 0' style='position:absolute;left:20px;top:60px;width:100px'>Go</span>"
        "<span onclick='void 0' style='position:absolute;left:180px;top:60px;width:100px'>Go</span>"
    )
    target = Target(
        ladder=[
            AnchorRelativeRung(
                strategy="anchor_relative",
                anchor_text="Actions",
                direction="below",
                same_row=False,
                target_kind="clickable",
                confidence=0.8,
            )
        ],
        recorded_rung=0,
        frame_path=["main"],
        fingerprint=Fingerprint(kind="clickable"),
        notes="n",
    )
    result = resolve(target, surface)
    assert isinstance(result, Unresolved)
    assert (result.code, result.attempts[0].matches) == ("TARGET_AMBIGUOUS", 2)


def test_two_matches_is_a_failure_and_a_lower_rung_then_decides(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    main.set_content(
        "<table><tr><td>Row A</td><td onclick='void 0'>Go</td></tr>"
        "<tr><td>Row B</td><td onclick='void 0'>Go</td></tr></table>"
    )
    text_only = Target(
        ladder=[TextExactRung(strategy="text_exact", text="Go", confidence=0.6)],
        recorded_rung=0,
        frame_path=["main"],
        fingerprint=Fingerprint(role="cell", name="Go", kind="clickable"),
        notes="n",
    )
    ambiguous = resolve(text_only, surface)
    assert isinstance(ambiguous, Unresolved)
    assert (ambiguous.code, ambiguous.attempts[0].matches) == ("TARGET_AMBIGUOUS", 2)

    anchor = AnchorRelativeRung(
        strategy="anchor_relative",
        anchor_text="Row B",
        direction="right",
        same_row=True,
        target_kind="clickable",
        confidence=0.8,
    )
    anchored = text_only.model_copy(update={"ladder": [*text_only.ladder, anchor]})
    resolved = resolve(anchored, surface)
    assert isinstance(resolved, Resolved)
    assert (resolved.strategy, resolved.drift) == ("anchor_relative", True)


def test_layout_drift_falls_to_the_anchor_rung_and_the_flow_still_works(
    surface: PlaywrightSurface,
    coreledger: RunningMockApp,
    mock_settings: MockSettings,
    inject: Injector,
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
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
    assert clicked.identity_changed, "Find became Search: resolved, and flagged"
    assert surface.describe(clicked.element).own_text == "Search"

    coordinates_only = find.model_copy(update={"ladder": [find.ladder[0], find.ladder[2]]})
    assert isinstance(resolve(coordinates_only, surface), Unresolved), (
        "the bbox lands on the inserted status cell, which is not a clickable"
    )

    gated = gated_for(surface, coreledger.base_url)
    assert gated.perform(TypeText(typed.element, "10007"), declared_risk="reversible").ok
    assert gated.perform(Click(clicked.element)).ok
    expect(main.get_by_text("Member profile")).to_be_visible()


def test_frame_paths_scope_resolution_including_unnamed_nested_frames(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    recorded = record(_element(surface, "cell", "Find"), surface).target
    find = recorded.model_copy(update={"ladder": recorded.ladder[:2]})
    assert isinstance(resolve(find.model_copy(update={"frame_path": []}), surface), Unresolved)
    assert isinstance(
        resolve(find.model_copy(update={"frame_path": ["banner"]}), surface), Unresolved
    )
    sign_out = record(_element(surface, "link", "Sign out"), surface)
    assert sign_out.target.frame_path == ["banner"]
    assert sign_out.target.ladder[0].strategy == "role_name"

    main.set_content(
        "<div style='height:40px'>Header</div>"
        "<iframe style='border:6px solid gray' srcdoc=\"<span onclick='void 0'>Deep link</span>\">"
        "</iframe>"
    )
    inner = main.child_frames[0]
    expect(inner.get_by_text("Deep link")).to_be_visible()
    node = _node(surface, lambda n: (n.name or n.text) == "Deep link" and n.box is not None)
    assert node.frame_path == ["main", "#0"]
    real = inner.get_by_text("Deep link").bounding_box()
    assert node.box is not None
    assert real is not None
    assert abs(node.box.y - real["y"]) < 3, "nested frame borders are part of the offset"
    deep = record(surface.element_for_ref(node.ref or ""), surface)
    assert isinstance(resolve(deep.target, surface), Resolved)


def test_the_hand_written_example_resolves_on_the_live_app(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    """Every target in the example artifact lands on one element at its recorded rung."""
    capability = load_capability(EXAMPLE)
    steps = {s.id: s for s in capability.steps}
    gated = gated_for(surface, coreledger.base_url)
    surface.page.goto(coreledger.base_url + "/")
    main = _main(surface)
    expect(main.get_by_text("Operator sign-in")).to_be_visible()

    def at_recorded_rung(target: Target) -> Element:
        result = resolve(target, surface)
        assert isinstance(result, Resolved), result
        assert result.rung_index == target.recorded_rung
        assert not result.identity_changed
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
    surface: PlaywrightSurface,
    coreledger: RunningMockApp,
    mock_settings: MockSettings,
    inject: Injector,
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    inject("app_error", times=1, on="detail")
    main.goto(coreledger.base_url + "/members/10007")
    assert surface.settle(quiet_ms=100, timeout_ms=5000)
    assert surface.frame_status(["main"]) == 500
    assert surface.frame_status([]) == 200


def test_settle_waits_for_fetches_not_just_documents(
    surface: PlaywrightSurface,
    coreledger: RunningMockApp,
    mock_settings: MockSettings,
    inject: Injector,
) -> None:
    main = _sign_in(surface, coreledger.base_url, mock_settings)
    inject("slow", times=1, ms="800")
    started = time.monotonic()
    main.evaluate("() => { fetch('/members/10007'); }")
    assert surface.settle(quiet_ms=100, timeout_ms=5000)
    assert time.monotonic() - started >= 0.7


def test_settle_waits_for_a_navigation_that_starts_just_after_the_call(
    surface: PlaywrightSurface, coreledger: RunningMockApp, mock_settings: MockSettings
) -> None:
    """Found by probe: settle returned before a submit's request reached the listener, so the next
    snapshot still showed the sign-in page. A submit scheduled 100 ms out makes the race certain."""
    surface.page.goto(coreledger.base_url + "/")
    main = _main(surface)
    main.get_by_label("Operator ID").fill(mock_settings.operator_user)
    main.get_by_label("Password").fill(mock_settings.operator_password)
    assert surface.settle(quiet_ms=300, timeout_ms=5000)
    main.evaluate("() => { setTimeout(() => document.forms[0].submit(), 100); }")
    assert surface.settle(quiet_ms=300, timeout_ms=5000)
    labels = [n.name or n.text for n in surface.snapshot().nodes if n.frame_path == ["main"]]
    assert "Member Lookup" in labels
    assert "Operator sign-in" not in labels


def test_reads_refuse_while_a_human_holds_control(
    surface: PlaywrightSurface,
    hand_off: HandOff,
    coreledger: RunningMockApp,
    mock_settings: MockSettings,
) -> None:
    _sign_in(surface, coreledger.base_url, mock_settings)
    find = _element(surface, "cell", "Find")
    hand_off.holder = "human"
    for read in (
        surface.snapshot,
        surface.screenshot,
        lambda: surface.describe(find),
        lambda: surface.frame_text(["main"]),
        lambda: surface.settle(100, 1000),
    ):
        with pytest.raises(NotInControl):
            read()


def test_desktop_surface_is_a_documented_stub() -> None:
    desktop = DesktopSurface()
    assert "UI Automation" in (DesktopSurface.__doc__ or "")
    with pytest.raises(NotImplementedError, match="designed, not built"):
        desktop.snapshot()
    with pytest.raises(NotImplementedError):
        desktop.candidates(TextExactRung(strategy="text_exact", text="x", confidence=1), [])
