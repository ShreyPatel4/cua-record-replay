"""Accessibility snapshot parsing: Playwright's ai-mode aria text into frame-aware, boxed nodes.

Boxes inside a frame are frame-relative in that text; parsing adds each frame's offset so every box
is page-absolute, which is what a screenshot of the page shows.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from cua.surface.base import Box, SnapshotNode

_LINE_RE = re.compile(r"^(?P<indent> *)- (?P<body>.*)$")
_NODE_RE = re.compile(
    r'^(?P<role>[a-z][a-zA-Z]*)(?: "(?P<name>(?:[^"\\]|\\.)*)")?'
    r"(?P<attrs>(?: \[[^\]]*\])*)(?P<colon>:)?(?: (?P<inline>.*))?$"
)
_ATTR_RE = re.compile(r"\[([^\]=]+)(?:=([^\]]*))?\]")


def _unquote(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, str) else value
    return value


@dataclass
class _Scope:
    depth: int
    frame_path: list[str]
    dx: float
    dy: float


def parse_snapshot(text: str, iframe_path: Callable[[str], list[str]]) -> list[SnapshotNode]:
    """Parse `aria_snapshot(mode="ai", boxes=True)` output taken on the page root.

    iframe_path maps an iframe node's ref to that frame's path, so nodes inside it get the right
    frame and offset. Property lines such as `/url:` are dropped.
    """
    nodes: list[SnapshotNode] = []
    stack: list[_Scope] = [_Scope(depth=-1, frame_path=[], dx=0.0, dy=0.0)]
    for raw in text.splitlines():
        line = _LINE_RE.match(raw)
        if line is None:
            continue
        depth = len(line.group("indent")) // 2
        body = line.group("body")
        if body.startswith("/"):
            continue
        match = _NODE_RE.match(body)
        if match is None:
            continue
        while stack[-1].depth >= depth:
            stack.pop()
        scope = stack[-1]
        attrs = {m.group(1): m.group(2) for m in _ATTR_RE.finditer(match.group("attrs") or "")}
        ref = attrs.get("ref")
        role = match.group("role")
        name = json.loads(f'"{match.group("name")}"') if match.group("name") is not None else ""
        inline = _unquote(match.group("inline") or "")

        box = None
        if attrs.get("box"):
            x, y, w, h = (float(v) for v in attrs["box"].split(","))
            if w > 0 and h > 0:
                box = Box(x=x + scope.dx, y=y + scope.dy, w=w, h=h)

        nodes.append(
            SnapshotNode(
                ref=ref,
                role=role,
                name=name,
                text=inline[:80],
                frame_path=list(scope.frame_path),
                box=box,
                clickable=attrs.get("cursor") == "pointer",
                depth=depth,
            )
        )
        if role == "iframe" and ref is not None:
            child = iframe_path(ref)
            stack.append(
                _Scope(
                    depth=depth,
                    frame_path=child,
                    dx=box.x if box else scope.dx,
                    dy=box.y if box else scope.dy,
                )
            )
    return nodes


def mark_clickable(
    nodes: list[SnapshotNode], points: Iterable[tuple[list[str], float, float]]
) -> list[SnapshotNode]:
    """Flag the smallest boxed node containing each click-handler point in the same frame.

    A row and its only cell share a box, so ties go to the deeper node.

    Legacy markup puts onclick on spans inside cells; the accessibility tree reports only the cell,
    and marks a pointer cursor on some of them but not all.
    """
    flagged: set[int] = set()
    for frame_path, px, py in points:
        containing = [
            (node.box.area, -node.depth, index)
            for index, node in enumerate(nodes)
            if node.ref is not None
            and node.box is not None
            and node.frame_path == frame_path
            and node.box.contains(px, py)
        ]
        if containing:
            flagged.add(min(containing)[2])
    return [
        node.model_copy(update={"clickable": True}) if index in flagged else node
        for index, node in enumerate(nodes)
    ]
