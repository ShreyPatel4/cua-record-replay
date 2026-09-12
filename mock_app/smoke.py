"""Black-box smoke checks that hit every CoreLedger route and every failure injection over HTTP.

Used by pytest (one test per check) and by `cua mock smoke` against a separately booted server.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import parse_qs, urlsplit

HOSTILE_MARKUP = re.compile(r"""\s(id|data-testid|data-test|data-qa)\s*=""", re.IGNORECASE)


@dataclass
class Page:
    status: int
    url: str
    body: str


class SmokeFailure(AssertionError):
    pass


class Client:
    """Tiny cookie-keeping HTTP client over urllib; follows redirects, never raises on 4xx/5xx."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )

    def request(
        self, path: str, *, form: dict[str, str] | None = None, json_body: Any = None
    ) -> Page:
        data: bytes | None = None
        headers: dict[str, str] = {}
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
        elif json_body is not None:
            data = json.dumps(json_body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base_url + path, data=data, headers=headers)  # noqa: S310
        try:
            with self._opener.open(req, timeout=30) as resp:
                return Page(resp.status, resp.geturl(), resp.read().decode())
        except urllib.error.HTTPError as err:
            return Page(err.code, err.geturl(), err.read().decode())

    def login(self, user: str, password: str) -> Page:
        return self.request("/login", form={"operator": user, "password": password, "next": ""})

    def arm(
        self, name: str, times: int | Literal["default"] | None = "default", **params: str
    ) -> None:
        """Omitting times on the wire means the per-injection default."""
        body: dict[str, Any] = {"name": name, "params": params}
        if times != "default":
            body["times"] = times
        page = self.request("/__control/api/arm", json_body=body)
        _expect(page.status == 200, f"arm {name} returned {page.status}: {page.body}")

    def reset(self) -> None:
        self.request("/__control/api/reset", json_body={})


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)


def _expect_in(needle: str, page: Page, context: str) -> None:
    _expect(needle in page.body, f"{context}: expected {needle!r} at {page.url} ({page.status})")


def _expect_hostile(page: Page) -> None:
    match = HOSTILE_MARKUP.search(page.body)
    _expect(
        match is None, f"{page.url} contains stable-selector markup: {match and match.group(0)}"
    )


VALID_FORM = {"acct_type": "CERT", "nickname": "Rainy day", "initial_deposit": "25.00"}


def check_shell(c: Client, user: str, password: str) -> None:
    page = c.request("/")
    _expect_in("<frameset", page, "shell")
    _expect_in('name="main"', page, "shell")
    _expect_hostile(page)
    _expect_hostile(c.request("/banner"))


def check_login(c: Client, user: str, password: str) -> None:
    anon = c.request("/members/search")
    _expect("/login" in anon.url, f"unauthenticated search should land on sign-in, got {anon.url}")
    _expect_in("Operator sign-in", anon, "login page")
    _expect_hostile(anon)
    bad = c.login(user, password + "-wrong")
    _expect_in("Invalid operator ID or password.", bad, "bad login")
    good = c.login(user, password)
    _expect(good.url.endswith("/members/search"), f"login should land on search, got {good.url}")
    _expect_in("Member Lookup", good, "search after login")
    _expect_hostile(good)


def check_happy_path(c: Client, user: str, password: str) -> None:
    c.login(user, password)
    detail = c.request("/members/search?q=10007")
    _expect(
        detail.url.endswith("/members/10007"), f"search should redirect to detail: {detail.url}"
    )
    _expect_in("Member profile", detail, "detail")
    _expect_in("Share Savings", detail, "detail")
    _expect_in("$4,210.55", detail, "detail")
    _expect("System notice" not in detail.body, "no interstitial unless injected")
    _expect_hostile(detail)
    form = c.request("/members/10007/subaccounts/new")
    _expect_in("Create sub-account", form, "sub-account form")
    _expect_in("confirm(", form, "sub-account form")
    _expect_hostile(form)
    done = c.request("/members/10007/subaccounts/create", form=VALID_FORM)
    _expect_in("Sub-account opened", done, "confirmation")
    _expect(re.search(r"10007-S\d\d", done.body) is not None, "confirmation shows sub-account no")
    _expect_hostile(done)
    _expect_hostile(c.request("/__control"))


def check_not_found(c: Client, user: str, password: str) -> None:
    c.login(user, password)
    natural = c.request("/members/search?q=99999")
    _expect_in("No member found for member number 99999.", natural, "natural not found")
    bad_format = c.request("/members/search?q=12ab")
    _expect_in("Member number must be 5 digits.", bad_format, "bad format")
    c.arm("not_found")
    forced = c.request("/members/search?q=10007")
    _expect_in("No member found", forced, "injected not found")


def check_validation_error(c: Client, user: str, password: str) -> None:
    c.login(user, password)
    natural = c.request(
        "/members/10007/subaccounts/create", form={**VALID_FORM, "initial_deposit": "1.00"}
    )
    _expect(natural.status == 200, f"validation error is a normal page, got {natural.status}")
    _expect_in("Initial deposit must be at least $5.00.", natural, "natural validation")
    c.arm("validation_error")
    forced = c.request("/members/10007/subaccounts/create", form=VALID_FORM)
    _expect_in("Initial deposit must be at least $5.00.", forced, "injected validation")


def check_interstitial(c: Client, user: str, password: str) -> None:
    c.login(user, password)
    c.arm("interstitial", times=1)
    first = c.request("/members/10007")
    _expect_in("System notice", first, "interstitial")
    _expect_in('class="card" style="visibility:hidden"', first, "card hidden until OK")
    second = c.request("/members/10007")
    _expect("System notice" not in second.body, "times=1 interstitial fires once")
    _expect("visibility:hidden" not in second.body, "card visible without interstitial")


def check_slow(c: Client, user: str, password: str) -> None:
    c.login(user, password)
    c.arm("slow", ms="400")
    started = time.monotonic()
    page = c.request("/members/10007")
    elapsed = time.monotonic() - started
    _expect(elapsed >= 0.4, f"slow detail should take >= 0.4 s, took {elapsed:.3f}")
    _expect_in("Member profile", page, "slow detail eventually renders")


def check_session_expired(c: Client, user: str, password: str) -> None:
    c.login(user, password)
    c.arm("session_expired")
    kicked = c.request("/members/10007")
    _expect("/login" in kicked.url, f"expired session should land on sign-in, got {kicked.url}")
    _expect_in("Your session has expired", kicked, "expired banner")
    back = c.login(user, password)
    _expect_in("Member Lookup", back, "re-login works")
    again = c.request("/members/10007")
    _expect_in("Member profile", again, "default session_expired fires once")
    c.arm("session_expired", times=None)
    c.request("/members/10007")
    c.login(user, password)
    recur = c.request("/members/10007")
    _expect("/login" in recur.url, "times=None session_expired recurs")


def check_permission_denied(c: Client, user: str, password: str) -> None:
    c.login(user, password)
    restricted = c.request("/members/10013")
    _expect(restricted.status == 403, f"restricted member detail is 403, got {restricted.status}")
    _expect_in("Access denied", restricted, "restricted detail")
    create = c.request("/members/10013/subaccounts/create", form=VALID_FORM)
    _expect(create.status == 403, f"restricted create is 403, got {create.status}")
    c.arm("permission_denied")
    detail = c.request("/members/10007")
    _expect(detail.status == 200, "on=create permission_denied must not fire on detail")
    forced = c.request("/members/10007/subaccounts/create", form=VALID_FORM)
    _expect(forced.status == 403, f"injected create is 403, got {forced.status}")
    _expect_in("Access denied", forced, "injected create")


def check_app_error(c: Client, user: str, password: str) -> None:
    c.login(user, password)
    c.arm("app_error")
    detail = c.request("/members/10007")
    _expect(detail.status == 500, f"app_error detail is 500, got {detail.status}")
    _expect_in("Internal Server Error", detail, "app_error")
    still = c.request("/members/10007")
    _expect(still.status == 500, "app_error persists until cleared")
    c.request("/__control/api/clear", json_body={"name": "app_error"})
    _expect(c.request("/members/10007").status == 200, "clearing app_error restores detail")


def check_layout_drift(c: Client, user: str, password: str) -> None:
    c.login(user, password)
    before = c.request("/members/search")
    _expect(">Find</span>" in before.body, "baseline search has Find")
    c.arm("layout_drift")
    after = c.request("/members/search")
    _expect(">Search</span>" in after.body, "drifted search has Search")
    _expect(">Find</span>" not in after.body, "drifted search no longer has Find")
    same_row = re.search(
        r'name="q"[^>]*></td>\s*<td[^>]*>&nbsp;</td>\s*<td><span[^>]*>Search</span>', after.body
    )
    _expect(same_row is not None, "drifted action is one cell right, in the textbox row")
    _expect_in('onsubmit="return false"', after, "span click is the only submit")
    _expect_hostile(after)


def check_query_param_injection(c: Client, user: str, password: str) -> None:
    c.login(user, password)
    once = c.request("/members/10007?inject=app_error")
    _expect(once.status == 500, f"?inject=app_error is 500, got {once.status}")
    _expect(c.request("/members/10007").status == 200, "?inject does not persist")
    notice = c.request("/members/10007?inject=interstitial")
    _expect_in("System notice", notice, "?inject=interstitial")
    state = json.loads(c.request("/__control/api/state").body)
    _expect(state["armed"] == {}, f"?inject must not mutate armed state: {state}")
    unknown = c.request("/members/10007?inject=bogus")
    _expect(unknown.status == 400, f"unknown injection is 400, got {unknown.status}")
    kicked = c.request("/members/10007?inject=session_expired")
    next_param = parse_qs(urlsplit(kicked.url).query).get("next", [""])[0]
    _expect(next_param == "/members/10007", f"next must drop ?inject to avoid a loop: {next_param}")


def check_control_validation(c: Client, user: str, password: str) -> None:
    bad_bodies: list[dict[str, Any]] = [
        {"name": "bogus"},
        {"name": "slow", "times": 0},
        {"name": "slow", "times": 1.5},
        {"name": "slow", "params": {"ms": "abc"}},
        {"name": "permission_denied", "params": {"on": "bogus"}},
        {"name": "not_found", "params": {"ms": "10"}},
    ]
    for body in bad_bodies:
        page = c.request("/__control/api/arm", json_body=body)
        _expect(page.status == 400, f"arm {body} should be 400, got {page.status}")
    form = c.request("/__control", form={"action": "arm", "name": "slow", "times": "abc"})
    _expect(form.status == 400, f"control form with times=abc should be 400, got {form.status}")
    c.login(user, password)
    bad_query = c.request("/members/10007?inject=slow&inject_ms=abc")
    _expect(bad_query.status == 400, f"bad one-shot param should be 400, got {bad_query.status}")
    state = json.loads(c.request("/__control/api/state").body)
    _expect(state["armed"] == {}, f"rejected requests must arm nothing: {state}")


HOSTILE_NEXT = ("//evil.example/x", "/\t/evil.example/y", "/\\evil.example", "https://evil.example")


def check_redirect_safety(c: Client, user: str, password: str) -> None:
    for hostile in HOSTILE_NEXT:
        form = {"operator": user, "password": password, "next": hostile}
        landed = c.request("/login", form=form)
        _expect(
            landed.url == c.base_url + "/members/search",
            f"next={hostile!r} must fall back to search, landed on {landed.url}",
        )
    form = {"operator": user, "password": password, "next": "/members/10007"}
    ok = c.request("/login", form=form)
    _expect(ok.url.endswith("/members/10007"), f"same-origin next is honoured, got {ok.url}")


CHECKS: dict[str, Callable[[Client, str, str], None]] = {
    "shell": check_shell,
    "login": check_login,
    "happy_path": check_happy_path,
    "not_found": check_not_found,
    "validation_error": check_validation_error,
    "interstitial": check_interstitial,
    "slow": check_slow,
    "session_expired": check_session_expired,
    "permission_denied": check_permission_denied,
    "app_error": check_app_error,
    "layout_drift": check_layout_drift,
    "query_param_injection": check_query_param_injection,
    "control_validation": check_control_validation,
    "redirect_safety": check_redirect_safety,
}


def run_check(name: str, base_url: str, user: str, password: str) -> None:
    """Each check gets a fresh cookie jar and a reset server so checks are order independent."""
    client = Client(base_url)
    client.reset()
    try:
        CHECKS[name](client, user, password)
    finally:
        client.reset()


def run_all(base_url: str, user: str, password: str) -> list[tuple[str, str | None]]:
    results: list[tuple[str, str | None]] = []
    for name in CHECKS:
        try:
            run_check(name, base_url, user, password)
            results.append((name, None))
        except SmokeFailure as exc:
            results.append((name, str(exc)))
    return results
