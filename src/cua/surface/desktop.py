"""DesktopSurface: the designed-but-unbuilt adapter for native apps through OS accessibility APIs.

It exists so the seam is real: the artifact, resolver, recorder, and replay engine stay unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import NoReturn

from cua.artifact.schema import Rung
from cua.locate.backend import ElementFacts
from cua.surface.base import (
    A11ySnapshot,
    Action,
    ActResult,
    DialogGuard,
    Element,
    Observation,
    RequestGuard,
    SurfaceEvent,
)


class DesktopSurface:
    """Would wrap Windows UI Automation (IUIAutomation), macOS AXUIElement, or Linux AT-SPI.

    Mapping, the same role/name/bounds triple the web surface uses:

    - snapshot: walk the automation tree from the app's top window. UIA ControlType or AXRole
      becomes role, Name or AXTitle/AXDescription becomes name, BoundingRectangle or AXFrame becomes
      the box. frame_path becomes the window and pane path (window title, then pane names).
    - role_name rung: supported (ControlType plus Name; AXRole plus AXTitle).
    - label_text rung: supported through UIA LabeledBy and AXTitleUIElement, and same_row through
      grid and table patterns (UIA GridItem, AXRow).
    - text_exact rung: supported on text elements' Name or Value.
    - anchor_relative rung: supported, computed from bounding rectangles exactly as on the web.
    - bbox rung: supported, normalized to the window's client area instead of the frame viewport.
    - act: UIA Invoke, Value, SelectionItem and ExpandCollapse patterns (AXPress, AXValue on
      macOS), falling back to synthesized input at the element center.
    - policy hooks: no URLs, so navigate steps and url_matches or status_code conditions are
      rejected by the schema; the request guard becomes a window-title and process allowlist,
      and native modal dialogs are answered through the same dialog guard.
    """

    def _unbuilt(self) -> NoReturn:
        raise NotImplementedError(
            "DesktopSurface is designed, not built; see the class docstring for the API mapping"
        )

    def observe(self) -> Observation:
        self._unbuilt()

    def snapshot(self) -> A11ySnapshot:
        self._unbuilt()

    def screenshot(self) -> bytes:
        self._unbuilt()

    def act(self, action: Action, *, dialog_guard: DialogGuard | None = None) -> ActResult:
        self._unbuilt()

    def install_request_guard(self, guard: RequestGuard) -> None:
        self._unbuilt()

    def drain_events(self) -> list[SurfaceEvent]:
        self._unbuilt()

    def element_for_ref(self, ref: str) -> Element:
        self._unbuilt()

    def frame_url(self, frame_path: Sequence[str]) -> str:
        self._unbuilt()

    def frame_text(self, frame_path: Sequence[str]) -> str:
        self._unbuilt()

    def frame_status(self, frame_path: Sequence[str]) -> int | None:
        self._unbuilt()

    def settle(self, quiet_ms: int, timeout_ms: int) -> bool:
        self._unbuilt()

    def wait(self, ms: int) -> None:
        self._unbuilt()

    def start_trace(self) -> None:
        self._unbuilt()

    def stop_trace(self, path: Path | None) -> bool:
        self._unbuilt()

    def session_tokens(self) -> list[str]:
        self._unbuilt()

    def candidates(self, rung: Rung, frame_path: Sequence[str]) -> list[Element]:
        self._unbuilt()

    def same_element(self, a: Element, b: Element) -> bool:
        self._unbuilt()

    def describe(self, element: Element) -> ElementFacts:
        self._unbuilt()
