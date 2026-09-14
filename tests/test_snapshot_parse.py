"""Snapshot parsing: Playwright's ai-mode aria text becomes frame-aware nodes with page boxes.

Uses a captured CoreLedger snapshot shape, so no browser is needed to pin the parser.
"""

from __future__ import annotations

from cua.surface.base import A11ySnapshot, Box
from cua.surface.snapshot import ClickTarget, merge_click_targets, parse_snapshot

CAPTURED = """\
- document [ref=e1] [box=0,0,1280,800]:
  - generic [active] [ref=e2] [box=0,0,1280,800]:
    - iframe [ref=e3] [box=0,0,1280,48]:
      - table [ref=f1e2] [box=4,4,1272,23]:
        - rowgroup [ref=f1e3] [box=4,4,1272,23]:
          - row [ref=f1e4] [box=4,4,1272,23]:
            - cell [ref=f1e6] [box=732,4,544,23]:
              - link "Sign out" [ref=f1e8] [cursor=pointer] [box=1197,6,75,19]:
                - /url: /logout
              - text: "|"
    - iframe [ref=e4] [box=0,49,1280,751]:
      - table [ref=f2e9] [box=9,34,326,27]:
        - rowgroup [ref=f2e10] [box=11,36,322,23]:
          - row [ref=f2e11] [box=11,36,322,23]:
            - cell "Member number" [ref=f2e12] [box=11,36,159,23]
            - cell [ref=f2e13] [box=172,36,99,23]:
              - textbox "Operator ID" [ref=f2e14] [box=173,37,97,21]: teller-0417
            - cell "Find" [ref=f2e15] [box=273,36,60,23]
            - cell "Say \\"hi\\"" [ref=f2e16] [box=340,36,60,23]
            - 'cell "Status: open" [ref=f2e17] [box=400,36,60,23]'
            - 'cell "It''s #2" [ref=f2e18] [box=460,36,60,23]':
              - text: inner
      - paragraph [ref=f2e19] [box=9,70,600,20]: See the details for more
"""
FRAMES = {"e3": (["banner"], 0.0, 0.0), "e4": (["main"], 1.0, 1.0)}


def _snapshot() -> A11ySnapshot:
    nodes = parse_snapshot(CAPTURED, lambda ref: FRAMES[ref])
    return A11ySnapshot(
        nodes=nodes, frame_urls={"": "http://x/", "main": "http://x/members/search"}
    )


def test_nodes_get_their_frame_and_a_page_absolute_box() -> None:
    snapshot = _snapshot()
    textbox = snapshot.by_ref("f2e14")
    assert (textbox.role, textbox.frame_path) == ("textbox", ["main"])
    assert textbox.box is not None
    assert (textbox.box.x, textbox.box.y) == (174, 87), "frame box origin plus its 1px border"
    link = snapshot.by_ref("f1e8")
    assert (link.name, link.frame_path, link.clickable) == ("Sign out", ["banner"], True)
    assert snapshot.by_ref("e4").frame_path == [], "the iframe node itself belongs to its parent"


def test_input_values_never_become_node_text() -> None:
    textbox = _snapshot().by_ref("f2e14")
    assert textbox.text == ""
    assert textbox.value_present
    assert "teller-0417" not in _snapshot().model_dump_json()
    assert "teller-0417" not in _snapshot().render()


def test_names_quoting_text_runs_and_properties() -> None:
    snapshot = _snapshot()
    assert snapshot.by_ref("f2e16").name == 'Say "hi"'
    assert snapshot.by_ref("f2e17").name == "Status: open", "single-quoted YAML lines are kept"
    assert snapshot.by_ref("f2e18").name == "It's #2"
    assert snapshot.by_ref("f2e19").text == "See the details for more"
    texts = [(n.text, n.frame_path) for n in snapshot.nodes if n.role == "text"]
    assert texts == [("|", ["banner"]), ("inner", ["main"])]
    assert not [n for n in snapshot.nodes if n.role.startswith("/")]


def test_click_handlers_mark_their_node_or_become_one() -> None:
    snapshot = _snapshot()
    targets = [
        # The Find span inside cell "Find", page-absolute.
        ClickTarget(ref="c1", frame_path=["main"], box=Box(x=276, y=87, w=56, h=23), text="Find"),
        # A span inside running text: the paragraph's label is not the span's text.
        ClickTarget(
            ref="c2", frame_path=["main"], box=Box(x=90, y=121, w=40, h=18), text="details"
        ),
    ]
    merged = merge_click_targets(snapshot.nodes, targets)
    assert [n.ref for n in merged if n.clickable] == ["f1e8", "f2e15", "c2"]
    synthetic = next(n for n in merged if n.ref == "c2")
    paragraph = next(n for n in merged if n.ref == "f2e19")
    assert (synthetic.role, synthetic.name, synthetic.depth) == (
        "clickable",
        "details",
        paragraph.depth + 1,
    )
    assert merged.index(synthetic) == merged.index(paragraph) + 1


def test_digest_ignores_refs_but_notices_typing() -> None:
    snapshot = _snapshot()
    renumbered = A11ySnapshot(
        nodes=[
            n.model_copy(update={"ref": f"x{n.ref}" if n.ref else None}) for n in snapshot.nodes
        ],
        frame_urls=snapshot.frame_urls,
    )
    assert renumbered.digest == snapshot.digest
    retyped = parse_snapshot(
        CAPTURED.replace("teller-0417", "teller-0418"), lambda ref: FRAMES[ref]
    )
    assert A11ySnapshot(nodes=retyped, frame_urls=snapshot.frame_urls).digest != snapshot.digest
    rendered = snapshot.render()
    assert '[f2e15] cell "Find" @main' in rendered
    assert '[f2e14] textbox "Operator ID" filled @main (174,87 97x21)' in rendered
    assert "rowgroup" not in rendered


def test_redacted_snapshot_masks_names_text_and_urls() -> None:
    masked = _snapshot().redacted(
        lambda s: s.replace("Member number", "[X]").replace("search", "s")
    )
    assert masked.by_ref("f2e12").name == "[X]"
    assert masked.frame_urls["main"] == "http://x/members/s"
