"""Snapshot parsing: Playwright's ai-mode aria text becomes frame-aware nodes with page boxes.

Uses a captured CoreLedger sign-in snapshot, so no browser is needed to pin the parser.
"""

from __future__ import annotations

from cua.surface.base import A11ySnapshot
from cua.surface.snapshot import mark_clickable, parse_snapshot

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
              - textbox [ref=f2e14] [box=173,37,97,21]
            - cell "Find" [ref=f2e15] [box=273,36,60,23]
            - cell "Say \\"hi\\"" [ref=f2e16] [box=340,36,60,23]
"""
FRAMES = {"e3": ["banner"], "e4": ["main"]}


def _nodes() -> A11ySnapshot:
    nodes = parse_snapshot(CAPTURED, lambda ref: FRAMES[ref])
    return A11ySnapshot(
        nodes=nodes, frame_urls={"": "http://x/", "main": "http://x/members/search"}
    )


def test_nodes_get_their_frame_and_a_page_absolute_box() -> None:
    snapshot = _nodes()
    textbox = snapshot.by_ref("f2e14")
    assert (textbox.role, textbox.frame_path) == ("textbox", ["main"])
    assert textbox.box is not None
    assert (textbox.box.x, textbox.box.y) == (173, 86), "frame offset 0,49 added"
    link = snapshot.by_ref("f1e8")
    assert (link.name, link.frame_path, link.clickable) == ("Sign out", ["banner"], True)
    assert snapshot.by_ref("e4").frame_path == [], "the iframe node itself belongs to its parent"


def test_names_text_runs_and_properties() -> None:
    snapshot = _nodes()
    assert snapshot.by_ref("f2e16").name == 'Say "hi"'
    texts = [n for n in snapshot.nodes if n.role == "text"]
    assert [(t.text, t.ref, t.frame_path) for t in texts] == [("|", None, ["banner"])]
    assert not [n for n in snapshot.nodes if n.role.startswith("/")]


def test_click_handlers_mark_the_smallest_containing_node() -> None:
    snapshot = _nodes()
    # The Find span's center, page-absolute: inside cell "Find" and every row and table around it.
    marked = mark_clickable(snapshot.nodes, [(["main"], 302.0, 96.0), (["banner"], 302.0, 96.0)])
    clickable = [n.ref for n in marked if n.clickable]
    assert clickable == ["f1e8", "f2e15"]


def test_digest_ignores_refs_and_render_lists_targets() -> None:
    snapshot = _nodes()
    renumbered = A11ySnapshot(
        nodes=[
            n.model_copy(update={"ref": f"x{n.ref}" if n.ref else None}) for n in snapshot.nodes
        ],
        frame_urls=snapshot.frame_urls,
    )
    assert renumbered.digest == snapshot.digest
    rendered = snapshot.render()
    assert '[f2e15] cell "Find" @main' in rendered
    assert "[f2e14] textbox @main (173,86 97x21)" in rendered
    assert "rowgroup" not in rendered
