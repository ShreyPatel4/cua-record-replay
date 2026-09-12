"""PlaywrightSurface: Chromium through Playwright, frame-aware, with the gate on every navigation.

Rung matching runs as one script per frame (locator.js), so a rung means the same thing when the
recorder writes it and when the resolver replays it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from importlib.resources import files
from typing import Any, cast

from playwright.sync_api import Dialog, ElementHandle, Frame, Page, Request, Response, Route
from playwright.sync_api import Error as PlaywrightError

from cua.artifact.schema import (
    AnchorRelativeRung,
    BBoxRung,
    LabelTextRung,
    RoleNameRung,
    Rung,
    TextExactRung,
)
from cua.locate.backend import ElementFacts
from cua.session.state import NotInControl
from cua.surface.base import (
    A11ySnapshot,
    Action,
    ActResult,
    Click,
    DialogGuard,
    DialogInfo,
    Element,
    ExpectedDialog,
    FrameNotFound,
    Navigate,
    NavigationGuard,
    NavigationRequest,
    Observation,
    PressKey,
    ReadText,
    Scroll,
    SelectOption,
    StaleRef,
    SurfaceEvent,
    TypeText,
)
from cua.surface.snapshot import mark_clickable, parse_snapshot

LOCATOR_JS = files("cua.surface").joinpath("locator.js").read_text(encoding="utf-8")
# Installed in every frame before its scripts run: the time of the last DOM mutation, for settle.
MUTATION_JS = """(() => {
  if (window.__cuaLastMutation !== undefined) return;
  window.__cuaLastMutation = performance.now();
  new MutationObserver(() => { window.__cuaLastMutation = performance.now(); })
    .observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
})();"""
POLL_MS = 50
_BLOCKING_EVENTS = frozenset({"navigation_blocked", "redirect_off_policy", "dialog_refused"})


def _always_in_control() -> None:
    return None


class PlaywrightSurface:
    """Surface and LocatorBackend over one Playwright page.

    Targets are the accessibility node a rung or ref names, after mapping a wrapper cell to the one
    control inside it; clicks land on that control's center.
    """

    def __init__(
        self,
        page: Page,
        *,
        control: Callable[[], None] = _always_in_control,
        act_timeout_ms: int = 5000,
    ) -> None:
        self._page = page
        self._control = control
        self._timeout = act_timeout_ms
        self._navigation_guard: NavigationGuard | None = None
        self._dialog_guard: DialogGuard | None = None
        self._expected_dialog: ExpectedDialog | None = None
        self._acting_frame_url = ""
        self._events: list[SurfaceEvent] = []
        self._inflight: set[Request] = set()
        self._status: dict[Frame, int] = {}
        page.add_init_script(MUTATION_JS)
        page.route("**/*", self._on_route)
        page.on("request", self._on_request)
        page.on("requestfinished", self._on_request_done)
        page.on("requestfailed", self._on_request_done)
        page.on("response", self._on_response)
        page.on("dialog", self._on_dialog)

    # ---- control and policy hooks ---------------------------------------------------------------

    def install_navigation_guard(self, guard: NavigationGuard) -> None:
        self._navigation_guard = guard

    def drain_events(self) -> list[SurfaceEvent]:
        events, self._events = self._events, []
        return events

    def _automation_holds_control(self) -> bool:
        try:
            self._control()
        except NotInControl:
            return False
        return True

    def _navigation_reason(self, request: Request, *, redirect: bool) -> str | None:
        if self._navigation_guard is None or not self._automation_holds_control():
            return None
        frame = request.frame
        frame_url = frame.url
        if frame_url in ("", "about:blank") and frame.parent_frame is not None:
            frame_url = frame.parent_frame.url
        return self._navigation_guard(
            NavigationRequest(
                frame_url=frame_url or "about:blank",
                url=request.url,
                method=request.method,
                redirect=redirect,
            )
        )

    @staticmethod
    def _is_document(request: Request) -> bool:
        return request.resource_type == "document" and request.is_navigation_request()

    def _on_route(self, route: Route, request: Request) -> None:
        if not self._is_document(request):
            route.continue_()
            return
        reason = self._navigation_reason(request, redirect=False)
        if reason is None:
            route.continue_()
            return
        self._events.append(SurfaceEvent("navigation_blocked", reason, request.url))
        # A 204 answer cancels a navigation and keeps the current document; aborting would replace
        # the frame with the browser's error page and lose the state the run was in.
        route.fulfill(status=204, body="")

    def _on_request(self, request: Request) -> None:
        if not self._is_document(request):
            return
        self._inflight.add(request)
        if request.redirected_from is not None:
            # Redirects never reach the route handler; they can be detected here, not prevented.
            self._inflight.discard(request.redirected_from)
            reason = self._navigation_reason(request, redirect=True)
            if reason is not None:
                self._events.append(SurfaceEvent("redirect_off_policy", reason, request.url))

    def _on_request_done(self, request: Request) -> None:
        self._inflight.discard(request)

    def _on_response(self, response: Response) -> None:
        if response.request.resource_type == "document":
            self._status[response.frame] = response.status

    def _on_dialog(self, dialog: Dialog) -> None:
        expected, self._expected_dialog = self._expected_dialog, None
        summary = f"{dialog.type}: {dialog.message}"
        if (
            expected is None
            or dialog.type != expected.dialog_type
            or not re.search(expected.message_pattern, dialog.message)
        ):
            self._events.append(SurfaceEvent("unexpected_dialog", summary))
            dialog.dismiss()
            return
        guard = self._dialog_guard
        info = DialogInfo(
            frame_url=self._acting_frame_url,
            dialog_type=dialog.type,
            message=dialog.message,
            response=expected.response,
        )
        reason = "no dialog guard installed" if guard is None else guard(info)
        if reason is not None:
            self._events.append(SurfaceEvent("dialog_refused", f"{summary} ({reason})"))
            dialog.dismiss()
            return
        self._events.append(SurfaceEvent("dialog_answered", f"{summary} -> {expected.response}"))
        if expected.response == "accept":
            dialog.accept()
        else:
            dialog.dismiss()

    # ---- acting ---------------------------------------------------------------------------------

    @staticmethod
    def _handle(element: Element) -> ElementHandle:
        if not isinstance(element.handle, ElementHandle):
            raise TypeError("element was not produced by a PlaywrightSurface")
        return element.handle

    def act(self, action: Action, *, dialog_guard: DialogGuard | None = None) -> ActResult:
        self._control()
        if self._navigation_guard is None:
            raise RuntimeError("install a navigation guard before acting; use GatedSurface")
        started = time.monotonic()
        self._dialog_guard = dialog_guard
        self._expected_dialog = action.dialog if isinstance(action, Click) else None
        text: str | None = None
        failure = ""
        try:
            if isinstance(action, Navigate):
                self._acting_frame_url = self._page.url
                self._page.goto(action.url, wait_until="commit", timeout=self._timeout)
            elif isinstance(action, PressKey) and action.element is None:
                self._page.keyboard.press(action.key)
            elif isinstance(action, Scroll) and action.element is None:
                sign = 1 if action.direction == "down" else -1
                self._page.mouse.wheel(0, sign * 120 * action.amount)
            else:
                element = action.element
                assert element is not None
                handle = self._handle(element)
                self._acting_frame_url = self.frame_url(element.frame_path)
                if isinstance(action, Click):
                    handle.click(timeout=self._timeout)
                elif isinstance(action, TypeText):
                    if action.clear_first:
                        handle.fill(action.text, timeout=self._timeout)
                    else:
                        handle.type(action.text, timeout=self._timeout)
                elif isinstance(action, SelectOption):
                    handle.select_option(label=action.label, timeout=self._timeout)
                elif isinstance(action, PressKey):
                    handle.press(action.key, timeout=self._timeout)
                elif isinstance(action, Scroll):
                    handle.scroll_into_view_if_needed(timeout=self._timeout)
                elif isinstance(action, ReadText):
                    text = " ".join(handle.inner_text().split())
        except PlaywrightError as exc:
            failure = str(exc).splitlines()[0]
        events = self.drain_events()
        blocked = [e for e in events if e.kind in _BLOCKING_EVENTS]
        unexpected = [e for e in events if e.kind == "unexpected_dialog"]
        result = ActResult(
            ok=not (failure or blocked or unexpected),
            action=action.kind,
            text=text,
            duration_ms=int((time.monotonic() - started) * 1000),
            events=events,
        )
        if blocked:
            result.code, result.message = "POLICY_BLOCKED", blocked[0].detail
        elif unexpected:
            result.code, result.message = (
                "ACTION_FAILED",
                f"unexpected dialog {unexpected[0].detail}",
            )
        elif failure:
            result.code, result.message = "ACTION_FAILED", failure
        return result

    # ---- perceiving -----------------------------------------------------------------------------

    def _path_of(self, frame: Frame) -> tuple[str, ...]:
        path: list[str] = []
        while frame.parent_frame is not None:
            parent = frame.parent_frame
            path.append(frame.name or f"#{parent.child_frames.index(frame)}")
            frame = parent
        return tuple(reversed(path))

    def _frame(self, frame_path: Sequence[str]) -> Frame:
        frame = self._page.main_frame
        for part in frame_path:
            children = frame.child_frames
            if part.startswith("#"):
                index = int(part[1:])
                if index >= len(children):
                    raise FrameNotFound(f"no frame {'/'.join(frame_path)}")
                frame = children[index]
                continue
            named = [child for child in children if child.name == part]
            if not named:
                raise FrameNotFound(f"no frame {'/'.join(frame_path)}")
            frame = named[0]
        return frame

    def _run(self, frame: Frame, arg: dict[str, Any]) -> Any:
        return frame.evaluate(LOCATOR_JS, arg)

    def _elements(self, frame: Frame, arg: dict[str, Any]) -> list[Element]:
        try:
            array = frame.evaluate_handle(LOCATOR_JS, arg)
        except PlaywrightError:
            return []
        try:
            props = array.get_properties()
            handles = [
                props[k].as_element() for k in sorted((k for k in props if k.isdigit()), key=int)
            ]
        finally:
            array.dispose()
        path = self._path_of(frame)
        return [Element(h, path) for h in handles if h is not None]

    def snapshot(self) -> A11ySnapshot:
        raw = self._page.locator(":root").aria_snapshot(mode="ai", boxes=True)

        def iframe_path(ref: str) -> list[str]:
            handle = self._page.locator(f"aria-ref={ref}").element_handle(timeout=self._timeout)
            content = handle.content_frame()
            return list(self._path_of(content)) if content is not None else []

        nodes = parse_snapshot(raw, iframe_path)
        points: list[tuple[list[str], float, float]] = []
        urls: dict[str, str] = {}
        for frame in self._page.frames:
            path = list(self._path_of(frame))
            urls["/".join(path)] = frame.url
            dx = dy = 0.0
            if frame.parent_frame is not None:
                box = frame.frame_element().bounding_box()
                if box is None:
                    continue
                dx, dy = box["x"], box["y"]
            try:
                found = self._run(frame, {"op": "click_points"})
            except PlaywrightError:
                continue
            points += [(path, dx + x, dy + y) for x, y in found]
        return A11ySnapshot(nodes=mark_clickable(nodes, points), frame_urls=urls)

    def screenshot(self) -> bytes:
        return self._page.screenshot(type="png")

    def observe(self) -> Observation:
        return Observation(screenshot_png=self.screenshot(), snapshot=self.snapshot())

    def element_for_ref(self, ref: str) -> Element:
        try:
            handle = self._page.locator(f"aria-ref={ref}").element_handle(timeout=1000)
        except PlaywrightError as exc:
            raise StaleRef(f"ref {ref} no longer resolves; take a new snapshot") from exc
        frame = handle.owner_frame()
        if frame is None:
            raise StaleRef(f"ref {ref} is detached")
        found = self._elements(frame, {"op": "canon", "els": [handle]})
        if not found:
            raise StaleRef(f"ref {ref} is not visible")
        return found[0]

    def frame_url(self, frame_path: Sequence[str]) -> str:
        return self._frame(frame_path).url

    def frame_text(self, frame_path: Sequence[str]) -> str:
        return str(self._run(self._frame(frame_path), {"op": "text_of"}))

    def frame_status(self, frame_path: Sequence[str]) -> int | None:
        return self._status.get(self._frame(frame_path))

    def settle(self, quiet_ms: int, timeout_ms: int) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            if not self._inflight and self._quiet_ms() >= quiet_ms:
                return True
            if time.monotonic() >= deadline:
                return False
            self._page.wait_for_timeout(POLL_MS)

    def _quiet_ms(self) -> float:
        quiet = float("inf")
        for frame in self._page.frames:
            try:
                quiet = min(quiet, float(self._run(frame, {"op": "quiet_ms"})))
            except PlaywrightError:
                return 0.0
        return quiet

    # ---- locator backend ------------------------------------------------------------------------

    def candidates(self, rung: Rung, frame_path: Sequence[str]) -> list[Element]:
        try:
            frame = self._frame(frame_path)
        except FrameNotFound:
            return []
        if isinstance(rung, RoleNameRung):
            try:
                handles = frame.get_by_role(
                    cast(Any, rung.role), name=rung.name, exact=True
                ).element_handles()
            except PlaywrightError:
                return []
            return self._elements(frame, {"op": "canon", "els": handles}) if handles else []
        arg: dict[str, Any]
        if isinstance(rung, LabelTextRung):
            arg = {
                "op": "label",
                "label": rung.label,
                "relation": rung.relation,
                "control": rung.control,
            }
        elif isinstance(rung, TextExactRung):
            arg = {"op": "text", "text": rung.text, "role": rung.role}
        elif isinstance(rung, AnchorRelativeRung):
            arg = {
                "op": "anchor",
                "anchor_text": rung.anchor_text,
                "direction": rung.direction,
                "same_row": rung.same_row,
                "target_kind": rung.target_kind,
                "nth": rung.nth,
            }
        elif isinstance(rung, BBoxRung):
            arg = {"op": "bbox", "x": rung.x, "y": rung.y, "w": rung.w, "h": rung.h}
        else:
            raise TypeError(f"unknown rung {rung!r}")
        return self._elements(frame, arg)

    def same_element(self, a: Element, b: Element) -> bool:
        if a.frame_path != b.frame_path:
            return False
        try:
            frame = self._frame(a.frame_path)
            return bool(
                self._run(frame, {"op": "same", "a": self._handle(a), "b": self._handle(b)})
            )
        except (PlaywrightError, FrameNotFound):
            return False

    def describe(self, element: Element) -> ElementFacts:
        frame = self._frame(element.frame_path)
        facts = self._run(frame, {"op": "describe", "el": self._handle(element)})
        return ElementFacts.model_validate({**facts, "frame_path": list(element.frame_path)})
