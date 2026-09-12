"""Surface contracts: what any automation backend offers to perceive, act, and report side effects.

The artifact, the locator ladder, and the replay engine see only these types, never Playwright.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from cua.vocab import KeyName

FramePathT = tuple[str, ...]


class SurfaceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Box(SurfaceModel):
    x: float = Field(description="Left edge in CSS pixels from the top-left of the page.")
    y: float = Field(description="Top edge in CSS pixels from the top-left of the page.")
    w: float = Field(ge=0, description="Width in CSS pixels.")
    h: float = Field(ge=0, description="Height in CSS pixels.")

    def contains(self, px: float, py: float) -> bool:
        return self.x <= px <= self.x + self.w and self.y <= py <= self.y + self.h

    @property
    def area(self) -> float:
        return self.w * self.h


class SnapshotNode(SurfaceModel):
    ref: str | None = Field(
        description="Handle valid only for the snapshot it came from. Null for plain text runs."
    )
    role: str = Field(description="Accessible role, or 'text' for a text run.")
    name: str = Field(default="", description="Accessible name.")
    text: str = Field(default="", max_length=80, description="Inline visible text, truncated.")
    frame_path: list[str] = Field(description="Frame names from the top document down.")
    box: Box | None = Field(default=None, description="Page-absolute box, if the node has one.")
    clickable: bool = Field(
        default=False,
        description="Has a pointer cursor or contains an element with a click handler, which the "
        "accessibility tree marks inconsistently on legacy markup.",
    )
    depth: int = Field(ge=0, description="Nesting depth in the accessibility tree.")


class A11ySnapshot(SurfaceModel):
    nodes: list[SnapshotNode] = Field(description="Every node, in tree order.")
    frame_urls: dict[str, str] = Field(description="'/'-joined frame path -> URL; '' is the top.")

    @property
    def digest(self) -> str:
        """Hash of what is on screen, ignoring refs, which are reassigned on every snapshot."""
        lines = [
            f"{'/'.join(n.frame_path)}|{n.role}|{n.name}|{n.text}|{n.clickable}" for n in self.nodes
        ]
        lines += [f"{k}={v}" for k, v in sorted(self.frame_urls.items())]
        return hashlib.sha256("\n".join(lines).encode()).hexdigest()

    def by_ref(self, ref: str) -> SnapshotNode:
        for node in self.nodes:
            if node.ref == ref:
                return node
        raise KeyError(f"ref {ref!r} is not in this snapshot")

    def render(self) -> str:
        """Compact listing for a model: interactive and text-bearing nodes only, one per line."""
        out = []
        for node in self.nodes:
            label = node.name or node.text
            if node.ref is None and not label:
                continue
            if node.role in _STRUCTURAL_ROLES and not label and not node.clickable:
                continue
            frame = "/".join(node.frame_path) or "top"
            box = (
                f" ({node.box.x:.0f},{node.box.y:.0f} {node.box.w:.0f}x{node.box.h:.0f})"
                if node.box
                else ""
            )
            ref = f"[{node.ref}] " if node.ref else ""
            click = " clickable" if node.clickable else ""
            shown = f' "{label}"' if label else ""
            out.append(f"{'  ' * node.depth}{ref}{node.role}{shown}{click} @{frame}{box}")
        return "\n".join(out)


_STRUCTURAL_ROLES = frozenset(
    {"document", "generic", "table", "rowgroup", "row", "cell", "iframe", "list", "group"}
)


class Observation(SurfaceModel):
    screenshot_png: bytes = Field(description="Viewport screenshot.")
    snapshot: A11ySnapshot = Field(description="Accessibility snapshot taken with the screenshot.")


@dataclass(frozen=True, eq=False)
class Element:
    """An element the surface resolved. The handle is backend-specific and opaque to callers."""

    handle: object
    frame_path: FramePathT


@dataclass(frozen=True)
class ExpectedDialog:
    dialog_type: Literal["confirm", "alert"]
    message_pattern: str
    response: Literal["accept", "dismiss"]


@dataclass(frozen=True)
class Navigate:
    url: str
    kind: Literal["navigate"] = "navigate"


@dataclass(frozen=True)
class Click:
    element: Element
    dialog: ExpectedDialog | None = None
    kind: Literal["click"] = "click"


@dataclass(frozen=True)
class TypeText:
    element: Element
    text: str
    clear_first: bool = True
    kind: Literal["type_text"] = "type_text"


@dataclass(frozen=True)
class SelectOption:
    element: Element
    label: str
    kind: Literal["select_option"] = "select_option"


@dataclass(frozen=True)
class PressKey:
    key: KeyName
    element: Element | None = None
    kind: Literal["press_key"] = "press_key"


@dataclass(frozen=True)
class Scroll:
    direction: Literal["up", "down"]
    amount: int = 1
    element: Element | None = None
    kind: Literal["scroll"] = "scroll"


@dataclass(frozen=True)
class ReadText:
    element: Element
    kind: Literal["read_text"] = "read_text"


Action = Navigate | Click | TypeText | SelectOption | PressKey | Scroll | ReadText


@dataclass(frozen=True)
class NavigationRequest:
    """A frame document load about to be sent, or a server redirect already being followed."""

    frame_url: str
    url: str
    method: str
    redirect: bool


@dataclass(frozen=True)
class DialogInfo:
    frame_url: str
    dialog_type: str
    message: str
    response: Literal["accept", "dismiss"]


# A guard returns None to allow, or the reason it refuses.
NavigationGuard = Callable[[NavigationRequest], str | None]
DialogGuard = Callable[[DialogInfo], str | None]

EventKind = Literal[
    "navigation_blocked",
    "redirect_off_policy",
    "dialog_answered",
    "dialog_refused",
    "unexpected_dialog",
]


@dataclass(frozen=True)
class SurfaceEvent:
    """A side effect the surface observed while acting: things the caller must not miss."""

    kind: EventKind
    detail: str
    url: str = ""


@dataclass
class ActResult:
    ok: bool
    action: str
    code: Literal["ACTION_FAILED", "POLICY_BLOCKED", "CONFIRMATION_REQUIRED"] | None = None
    message: str = ""
    text: str | None = None
    duration_ms: int = 0
    events: list[SurfaceEvent] = field(default_factory=list)


class StaleRef(LookupError):
    """A ref from an older snapshot no longer points at anything."""


class FrameNotFound(LookupError):
    pass


@runtime_checkable
class Surface(Protocol):
    def observe(self) -> Observation: ...

    def snapshot(self) -> A11ySnapshot: ...

    def screenshot(self) -> bytes: ...

    def act(self, action: Action, *, dialog_guard: DialogGuard | None = None) -> ActResult:
        """Perform one action. Raises NotInControl unless automation holds the session."""
        ...

    def install_navigation_guard(self, guard: NavigationGuard) -> None:
        """Every frame document request and redirect passes this guard; act refuses without one."""
        ...

    def drain_events(self) -> list[SurfaceEvent]:
        """Side effects observed since the last drain, including ones that arrived after act."""
        ...

    def element_for_ref(self, ref: str) -> Element: ...

    def frame_url(self, frame_path: Sequence[str]) -> str: ...

    def frame_text(self, frame_path: Sequence[str]) -> str: ...

    def frame_status(self, frame_path: Sequence[str]) -> int | None: ...

    def settle(self, quiet_ms: int, timeout_ms: int) -> bool:
        """True once no document request is in flight and the DOM has been quiet for quiet_ms."""
        ...
