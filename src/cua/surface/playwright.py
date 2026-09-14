"""PlaywrightSurface: Chromium through Playwright, frame-aware, with the gate on every request.

Rung matching runs as one script per frame (locator.js), so a rung means the same thing when the
recorder writes it and when the resolver replays it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Sequence
from importlib.resources import files
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Dialog,
    ElementHandle,
    Frame,
    Page,
    Request,
    Response,
    Route,
)
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
    BLOCKING_EVENTS,
    A11ySnapshot,
    Action,
    ActResult,
    Box,
    Click,
    DialogGuard,
    DialogInfo,
    Element,
    EventKind,
    ExpectedDialog,
    FrameNotFound,
    Navigate,
    Observation,
    PressKey,
    ReadText,
    RequestCheck,
    RequestGuard,
    Scroll,
    SelectOption,
    StaleRef,
    SurfaceError,
    SurfaceEvent,
    TypeText,
)
from cua.surface.snapshot import ClickTarget, merge_click_targets, parse_snapshot

LOCATOR_JS = files("cua.surface").joinpath("locator.js").read_text(encoding="utf-8")
# Installed in every frame before its scripts run: the time of the last DOM mutation, for settle.
MUTATION_JS = """(() => {
  if (window.__cuaLastMutation !== undefined) return;
  window.__cuaLastMutation = performance.now();
  new MutationObserver(() => { window.__cuaLastMutation = performance.now(); })
    .observe(document, { subtree: true, childList: true, attributes: true, characterData: true });
})();"""
# Download links never reach request routing or request events, so they are cancelled in the page.
DOWNLOAD_JS = """(() => {
  const install = (doc) => {
    if (doc.__cuaDownloadGuard) return;
    doc.__cuaDownloadGuard = true;
    doc.addEventListener("click", (event) => {
      const link = event.target instanceof Element ? event.target.closest("a[download]") : null;
      if (!link) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      if (window.__cuaDownloadBlocked) window.__cuaDownloadBlocked(link.href);
    }, true);
  };
  install(document);
  // document.open() erases every listener; legacy apps that rewrite their page must not lose it.
  const open = Document.prototype.open;
  Document.prototype.open = function (...args) {
    const result = open.apply(this, args);
    this.__cuaDownloadGuard = false;
    install(this);
    return result;
  };
})();"""
POLL_MS = 50
_SETTLE_TYPES = frozenset({"document", "xhr", "fetch"})
_PAGE_SCHEMES = frozenset({"http", "https", "about", ""})


def _first_line(exc: Exception) -> str:
    return str(exc).splitlines()[0] if str(exc) else type(exc).__name__


class PlaywrightSurface:
    """Surface and LocatorBackend over one page in a context this surface owns.

    Targets are the accessibility node a rung or ref names, after mapping a wrapper cell to the one
    control inside it; clicks land on that control's center. `control` raises NotInControl when
    automation does not hold the session; every act and every read calls it.
    """

    @classmethod
    def launch(
        cls,
        browser: Browser,
        *,
        control: Callable[[], None],
        viewport: tuple[int, int] = (1280, 800),
        act_timeout_ms: int = 5000,
    ) -> PlaywrightSurface:
        context = browser.new_context(
            viewport={"width": viewport[0], "height": viewport[1]},
            accept_downloads=False,
            service_workers="block",
        )
        return cls(context, control=control, act_timeout_ms=act_timeout_ms)

    def __init__(
        self, context: BrowserContext, *, control: Callable[[], None], act_timeout_ms: int = 5000
    ) -> None:
        """Takes a context created with accept_downloads=False and service_workers="block"."""
        self._context = context
        self._control = control
        self._timeout = act_timeout_ms
        self._guard: RequestGuard | None = None
        self._dialog_guard: DialogGuard | None = None
        self._expected_dialog: ExpectedDialog | None = None
        self._dialog_seen = False
        self._acting = False
        self._last_act = 0
        self._acting_frame_url = ""
        self._events: list[SurfaceEvent] = []
        self._inflight: set[Request] = set()
        self._last_network = time.monotonic()
        self._answered_by_guard: set[Request] = set()
        self._status: dict[Frame, int] = {}
        self._synthetic: dict[str, Element] = {}
        context.add_init_script(MUTATION_JS)
        context.add_init_script(DOWNLOAD_JS)
        context.expose_binding("__cuaDownloadBlocked", self._on_download_blocked)
        context.route("**/*", self._on_route)
        self.page: Page = context.new_page()
        context.on("page", self._on_page)
        self.page.on("request", self._on_request)
        self.page.on("requestfinished", self._on_request_done)
        self.page.on("requestfailed", self._on_request_done)
        self.page.on("response", self._on_response)
        self.page.on("dialog", self._on_dialog)
        self.page.on("framenavigated", self._on_frame_navigated)

    def close(self) -> None:
        self._context.close()

    # ---- control and policy hooks ---------------------------------------------------------------

    def install_request_guard(self, guard: RequestGuard) -> None:
        if self._guard is not None:
            raise RuntimeError("a request guard is already installed and cannot be replaced")
        self._guard = guard

    def drain_events(self) -> list[SurfaceEvent]:
        events, self._events = self._events, []
        return events

    def _emit(self, kind: EventKind, detail: str, url: str = "") -> None:
        self._events.append(SurfaceEvent(kind, detail, url, act_id=self._last_act or None))

    def _automation_holds_control(self) -> bool:
        try:
            self._control()
        except NotInControl:
            return False
        return True

    def _gated(self) -> RequestGuard | None:
        """The guard, when automation holds control. A human's own browsing is not gated."""
        return self._guard if self._automation_holds_control() else None

    def _frame_url_for(self, request: Request) -> str:
        """The document a request comes from; a popup's requests count as the page's own."""
        try:
            frame = request.frame
        except PlaywrightError:
            return self.page.url
        if frame.page is not self.page:
            return self.page.url
        url = frame.url
        if url in ("", "about:blank") and frame.parent_frame is not None:
            url = frame.parent_frame.url
        return url or "about:blank"

    def _on_route(self, route: Route, request: Request) -> None:
        guard = self._gated()
        if guard is None:
            route.continue_()
            return
        frame_url = self._frame_url_for(request)
        if not (request.resource_type == "document" and request.is_navigation_request()):
            reason = guard(RequestCheck(frame_url, request.url, request.method, "subresource"))
            if reason is None:
                route.continue_()
            else:
                self._emit("request_blocked", reason, request.url)
                route.abort("blockedbyclient")
            return

        reason = guard(RequestCheck(frame_url, request.url, request.method, "navigation"))
        if reason is not None:
            self._emit("navigation_blocked", reason, request.url)
            self._answer_with_no_content(route, request)
            return
        # Fetch the first hop ourselves so a redirect's Location is judged before the browser goes.
        try:
            response = route.fetch(max_redirects=0, timeout=self._timeout)
        except PlaywrightError as exc:
            self._emit("navigation_blocked", f"request failed: {_first_line(exc)}", request.url)
            route.abort("failed")
            return
        location = response.headers.get("location")
        if 300 <= response.status < 400 and location:
            target = urljoin(request.url, location)
            reason = guard(RequestCheck(frame_url, target, "GET", "redirect"))
            if reason is not None:
                self._emit("redirect_blocked", reason, target)
                self._answer_with_no_content(route, request)
                return
        route.fulfill(response=response)

    def _answer_with_no_content(self, route: Route, request: Request) -> None:
        # A 204 answer cancels a navigation and keeps the current document; aborting would replace
        # the frame with the browser's error page and lose the state the run was in.
        self._answered_by_guard.add(request)
        route.fulfill(status=204, body="")

    def _on_request(self, request: Request) -> None:
        if request.resource_type in _SETTLE_TYPES:
            self._inflight.add(request)
            self._last_network = time.monotonic()
        previous = request.redirected_from
        if previous is None:
            return
        self._inflight.discard(previous)
        guard = self._gated()
        if guard is None or request.resource_type != "document":
            return
        # Later hops of a redirect chain are followed by the browser without routing: detect only.
        reason = guard(RequestCheck(self._frame_url_for(request), request.url, "GET", "redirect"))
        if reason is not None:
            self._emit("redirect_off_policy", reason, request.url)

    def _on_request_done(self, request: Request) -> None:
        if request in self._inflight:
            self._inflight.discard(request)
            self._last_network = time.monotonic()

    def _on_response(self, response: Response) -> None:
        request = response.request
        if request.resource_type == "document" and request not in self._answered_by_guard:
            self._status[response.frame] = response.status

    def _on_frame_navigated(self, frame: Frame) -> None:
        scheme = urlsplit(frame.url).scheme
        if scheme not in _PAGE_SCHEMES and self._automation_holds_control():
            self._emit("navigation_off_policy", f"frame loaded a {scheme}: document", frame.url)

    def _on_page(self, page: Page) -> None:
        if page is self.page or not self._automation_holds_control():
            return
        self._emit("popup_closed", "a popup opened while automation held the session", page.url)
        page.close()

    def _on_download_blocked(self, source: Any, href: str) -> None:
        if self._automation_holds_control():
            self._emit("download_blocked", "download links are cancelled in the page", str(href))

    def _on_dialog(self, dialog: Dialog) -> None:
        if not self._automation_holds_control():
            return  # the human's dialog: left for them to answer
        self._dialog_seen = True
        expected = self._expected_dialog if self._acting else None
        summary = f"{dialog.type}: {dialog.message}"
        if (
            expected is None
            or dialog.type != expected.dialog_type
            or not re.search(expected.message_pattern, dialog.message)
        ):
            self._emit("unexpected_dialog", summary)
            dialog.dismiss()
            return
        self._expected_dialog = None
        guard = self._dialog_guard
        info = DialogInfo(
            frame_url=self._acting_frame_url,
            dialog_type=dialog.type,
            message=dialog.message,
            response=expected.response,
        )
        reason = "no dialog guard installed" if guard is None else guard(info)
        if reason is not None:
            self._emit("dialog_refused", f"{summary} ({reason})")
            dialog.dismiss()
            return
        self._emit("dialog_answered", f"{summary} -> {expected.response}")
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

    def _dispatch(self, action: Action) -> str | None:
        if isinstance(action, Navigate):
            self._acting_frame_url = self.page.url
            self.page.goto(action.url, wait_until="commit", timeout=self._timeout)
            return None
        if isinstance(action, PressKey) and action.element is None:
            self.page.keyboard.press(action.key)
            return None
        if isinstance(action, Scroll) and action.element is None:
            sign = 1 if action.direction == "down" else -1
            self.page.mouse.wheel(0, sign * 120 * action.amount)
            return None
        element = action.element
        assert element is not None
        handle = self._handle(element)
        self._acting_frame_url = self._frame(element.frame_path).url
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
            return " ".join(handle.inner_text().split())
        return None

    def act(self, action: Action, *, dialog_guard: DialogGuard | None = None) -> ActResult:
        self._control()
        if self._guard is None:
            raise RuntimeError("install a request guard before acting; use GatedSurface")
        started = time.monotonic()
        self._last_act += 1
        act_id = self._last_act
        expected = action.dialog if isinstance(action, Click) else None
        self._acting, self._dialog_guard = True, dialog_guard
        self._expected_dialog, self._dialog_seen = expected, False
        text: str | None = None
        failure = ""
        try:
            text = self._dispatch(action)
            if expected is not None:
                deadline = time.monotonic() + self._timeout / 1000
                while not self._dialog_seen and time.monotonic() < deadline:
                    self.page.wait_for_timeout(POLL_MS)
                if not self._dialog_seen:
                    failure = f"expected {expected.dialog_type} dialog did not appear"
        except (PlaywrightError, SurfaceError) as exc:
            failure = _first_line(exc)
        finally:
            self._acting, self._dialog_guard, self._expected_dialog = False, None, None

        events = [e for e in self._events if e.act_id == act_id]
        self._events = [e for e in self._events if e.act_id != act_id]
        blocked = [e for e in events if e.kind in BLOCKING_EVENTS]
        unexpected = [e for e in events if e.kind == "unexpected_dialog"]
        result = ActResult(
            ok=not (failure or blocked or unexpected),
            action=action.kind,
            act_id=act_id,
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
        frame = self.page.main_frame
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
        try:
            return frame.evaluate(LOCATOR_JS, arg)
        except PlaywrightError as exc:
            raise SurfaceError(_first_line(exc)) from exc

    def _handles(self, frame: Frame, arg: dict[str, Any]) -> list[ElementHandle]:
        try:
            array = frame.evaluate_handle(LOCATOR_JS, arg)
        except PlaywrightError:
            return []
        try:
            props = array.get_properties()
            keys = sorted((k for k in props if k.isdigit()), key=int)
            found = [props[k].as_element() for k in keys]
        finally:
            array.dispose()
        return [h for h in found if h is not None]

    def _elements(self, frame: Frame, arg: dict[str, Any]) -> list[Element]:
        path = self._path_of(frame)
        return [Element(h, path) for h in self._handles(frame, arg)]

    def _content_offset(self, frame: Frame) -> tuple[float, float] | None:
        """Page position of a child frame's content box: element box plus its border."""
        element = frame.frame_element()
        box = element.bounding_box()
        if box is None:
            return None
        border_x, border_y = element.evaluate("(el) => [el.clientLeft, el.clientTop]")
        return box["x"] + border_x, box["y"] + border_y

    def snapshot(self) -> A11ySnapshot:
        self._control()
        try:
            raw = self.page.locator(":root").aria_snapshot(mode="ai", boxes=True)
        except PlaywrightError as exc:
            raise SurfaceError(_first_line(exc)) from exc

        def iframe_info(ref: str) -> tuple[list[str], float, float]:
            handle = self.page.locator(f"aria-ref={ref}").element_handle(timeout=self._timeout)
            content = handle.content_frame()
            border = handle.evaluate("(el) => [el.clientLeft, el.clientTop]")
            path = list(self._path_of(content)) if content is not None else []
            return path, float(border[0]), float(border[1])

        nodes = parse_snapshot(raw, iframe_info)
        targets: list[ClickTarget] = []
        urls: dict[str, str] = {}
        self._synthetic = {}
        for frame in self.page.frames:
            path = list(self._path_of(frame))
            urls["/".join(path)] = frame.url
            offset = (0.0, 0.0) if frame.parent_frame is None else self._content_offset(frame)
            if offset is None:
                continue
            handles = self._handles(frame, {"op": "click_elements"})
            if not handles:
                continue
            try:
                data = self._run(frame, {"op": "click_data", "els": handles})
            except SurfaceError:
                continue
            for handle, (x, y, w, h, label) in zip(handles, data, strict=True):
                ref = f"c{len(self._synthetic) + 1}"
                self._synthetic[ref] = Element(handle, tuple(path))
                box = Box(x=offset[0] + x, y=offset[1] + y, w=w, h=h)
                targets.append(ClickTarget(ref=ref, frame_path=path, box=box, text=label))
        merged = merge_click_targets(nodes, targets)
        used = {n.ref for n in merged if n.role == "clickable"}
        self._synthetic = {ref: el for ref, el in self._synthetic.items() if ref in used}
        return A11ySnapshot(nodes=merged, frame_urls=urls)

    def screenshot(self) -> bytes:
        self._control()
        return self.page.screenshot(type="png")

    def observe(self) -> Observation:
        return Observation(screenshot_png=self.screenshot(), snapshot=self.snapshot())

    def element_for_ref(self, ref: str) -> Element:
        self._control()
        if ref in self._synthetic:
            return self._synthetic[ref]
        try:
            handle = self.page.locator(f"aria-ref={ref}").element_handle(timeout=1000)
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
        self._control()
        return self._frame(frame_path).url

    def frame_text(self, frame_path: Sequence[str]) -> str:
        self._control()
        return str(self._run(self._frame(frame_path), {"op": "text_of"}))

    def frame_status(self, frame_path: Sequence[str]) -> int | None:
        self._control()
        return self._status.get(self._frame(frame_path))

    def settle(self, quiet_ms: int, timeout_ms: int) -> bool:
        self._control()
        started = time.monotonic()
        deadline = started + timeout_ms / 1000
        while True:
            # The call itself counts as activity: a submit an act just fired reaches the request
            # listener a moment later, and an idle page before that is not a settled one.
            idle_ms = (time.monotonic() - max(started, self._last_network)) * 1000
            if not self._inflight and idle_ms >= quiet_ms and self._quiet_ms() >= quiet_ms:
                return True
            if time.monotonic() >= deadline:
                return False
            self.page.wait_for_timeout(POLL_MS)

    def _quiet_ms(self) -> float:
        quiet = float("inf")
        for frame in self.page.frames:
            try:
                quiet = min(quiet, float(self._run(frame, {"op": "quiet_ms"})))
            except SurfaceError:
                return 0.0
        return quiet

    # ---- locator backend ------------------------------------------------------------------------

    def candidates(self, rung: Rung, frame_path: Sequence[str]) -> list[Element]:
        self._control()
        try:
            frame = self._frame(frame_path)
        except FrameNotFound:
            return []
        if isinstance(rung, RoleNameRung):
            try:
                locator = frame.get_by_role(cast(Any, rung.role), name=rung.name, exact=True)
                handles = locator.element_handles()
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
        self._control()
        if a.frame_path != b.frame_path:
            return False
        try:
            frame = self._frame(a.frame_path)
            return bool(
                self._run(frame, {"op": "same", "a": self._handle(a), "b": self._handle(b)})
            )
        except SurfaceError:
            return False

    def describe(self, element: Element) -> ElementFacts:
        self._control()
        frame = self._frame(element.frame_path)
        facts = self._run(frame, {"op": "describe", "el": self._handle(element)})
        return ElementFacts.model_validate({**facts, "frame_path": list(element.frame_path)})
