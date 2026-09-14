"""Accessibility snapshot parsing: Playwright's ai-mode aria text into frame-aware, boxed nodes.

Boxes inside a frame are frame-relative in that text; parsing adds each frame's content offset so
every box is page-absolute. Input values are dropped here, before any node exists.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from cua.surface.base import Box, SnapshotNode, value_digest

_LINE_RE = re.compile(r"^(?P<indent> *)- (?P<body>.*)$")
_NODE_RE = re.compile(
    r'^(?P<role>[a-z][a-zA-Z]*)(?: "(?P<name>(?:[^"\\]|\\.)*)")?'
    r"(?P<attrs>(?: \[[^\]]*\])*)(?P<colon>:)?(?: (?P<inline>.*))?$"
)
_ATTR_RE = re.compile(r"\[([^\]=]+)(?:=([^\]]*))?\]")
# Roles whose inline text is the control's current value, which may be a typed secret.
VALUE_ROLES = frozenset({"textbox", "searchbox", "combobox", "spinbutton", "slider"})

IframeInfo = tuple[list[str], float, float]


def _fold(text: str) -> str:
    return " ".join(text.replace("\u2014", "-").replace("\u2013", "-").split()).lower()


def _unquote(value: str) -> str:
    value = value.strip()
    if value.startswith('"') and value.endswith('"'):
        loaded = json.loads(value)
        return loaded if isinstance(loaded, str) else value
    return value


def _unwrap_yaml_quotes(body: str) -> str:
    """Playwright single-quotes a whole node line when its text contains ': ' or ' #'."""
    if not body.startswith("'"):
        return body
    index = 1
    while index < len(body):
        if body[index] == "'":
            if index + 1 < len(body) and body[index + 1] == "'":
                index += 2
                continue
            return body[1:index].replace("''", "'") + body[index + 1 :]
        index += 1
    return body


@dataclass
class _Scope:
    depth: int
    frame_path: list[str]
    dx: float
    dy: float


def parse_snapshot(text: str, iframe_info: Callable[[str], IframeInfo]) -> list[SnapshotNode]:
    """Parse `aria_snapshot(mode="ai", boxes=True)` output taken on the page root.

    iframe_info maps an iframe node's ref to that frame's path and the border width between the
    frame element's box and its content, so nested boxes land where the screenshot shows them.
    """
    nodes: list[SnapshotNode] = []
    stack: list[_Scope] = [_Scope(depth=-1, frame_path=[], dx=0.0, dy=0.0)]
    for raw in text.splitlines():
        line = _LINE_RE.match(raw)
        if line is None:
            continue
        depth = len(line.group("indent")) // 2
        body = _unwrap_yaml_quotes(line.group("body"))
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

        is_value = role in VALUE_ROLES
        nodes.append(
            SnapshotNode(
                ref=ref,
                role=role,
                name=name,
                text="" if is_value else inline[:80],
                value_present=is_value and bool(inline),
                value_digest=value_digest(inline) if is_value and inline else None,
                frame_path=list(scope.frame_path),
                box=box,
                clickable=attrs.get("cursor") == "pointer",
                depth=depth,
            )
        )
        if role == "iframe" and ref is not None:
            child, border_x, border_y = iframe_info(ref)
            origin_x = box.x if box else scope.dx
            origin_y = box.y if box else scope.dy
            stack.append(
                _Scope(
                    depth=depth, frame_path=child, dx=origin_x + border_x, dy=origin_y + border_y
                )
            )
    return nodes


@dataclass(frozen=True)
class ClickTarget:
    """An element with a click handler, found by a DOM pass the accessibility tree misses."""

    ref: str
    frame_path: list[str]
    box: Box
    text: str


def merge_click_targets(
    nodes: list[SnapshotNode], targets: Iterable[ClickTarget]
) -> list[SnapshotNode]:
    """Mark the node each click handler belongs to, or add a node when the tree has none.

    The handler belongs to the smallest node containing its center (ties go to the deeper node,
    since a row and its only cell share a box) when that node's label is the handler's text.
    Otherwise, for example a span inside a paragraph, the handler becomes its own node.
    """
    flagged: set[int] = set()
    extra: dict[int, list[SnapshotNode]] = {}
    for target in targets:
        cx, cy = target.box.x + target.box.w / 2, target.box.y + target.box.h / 2
        containing = [
            (node.box.area, -node.depth, index)
            for index, node in enumerate(nodes)
            if node.ref is not None
            and node.box is not None
            and node.frame_path == target.frame_path
            and node.box.contains(cx, cy)
        ]
        owner = min(containing)[2] if containing else None
        if owner is not None:
            label = _fold(nodes[owner].name or nodes[owner].text)
            if not target.text or _fold(target.text) == label:
                flagged.add(owner)
                continue
        depth = nodes[owner].depth + 1 if owner is not None else 0
        extra.setdefault(owner if owner is not None else len(nodes) - 1, []).append(
            SnapshotNode(
                ref=target.ref,
                role="clickable",
                name=target.text[:80],
                frame_path=target.frame_path,
                box=target.box,
                clickable=True,
                depth=depth,
            )
        )
    merged: list[SnapshotNode] = list(extra.get(-1, []))
    for index, node in enumerate(nodes):
        merged.append(node.model_copy(update={"clickable": True}) if index in flagged else node)
        merged.extend(extra.get(index, []))
    return merged
