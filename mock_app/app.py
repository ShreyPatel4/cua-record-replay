"""Flask routes for CoreLedger: frameset shell, sign-in, member search, detail, sub-account flow.

Markup is intentionally legacy: nested tables, no ids or test ids, span and td click handlers.
"""

from __future__ import annotations

import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from functools import wraps
from typing import Any, Literal, TypeVar, cast
from urllib.parse import urlencode, urlsplit

from flask import (
    Flask,
    Response,
    current_app,
    g,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
)
from werkzeug.wrappers import Response as WerkzeugResponse

from mock_app.data import ACCOUNT_TYPES, Ledger, Member, money
from mock_app.injections import (
    DEFAULT,
    DEFAULT_SLOW_MS,
    DESCRIPTIONS,
    INJECTION_NAMES,
    ArmedInjection,
    Default,
    InjectionError,
    InjectionState,
    validate_name,
    validate_params,
)
from mock_app.settings import MockSettings

SESSION_COOKIE = "CLSESSID"
MEMBER_ID_RE = re.compile(r"^[0-9]{5}$")

AnyResponse = str | Response | WerkzeugResponse | tuple[str, int]
F = TypeVar("F", bound=Callable[..., AnyResponse])


@dataclass
class SessionStore:
    """Server-side session tokens. A cookie that is present but unknown or stale is expired."""

    ttl_s: int
    _expiry: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def create(self) -> str:
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._expiry[token] = time.monotonic() + self.ttl_s
        return token

    def check(self, token: str | None) -> Literal["valid", "expired", "absent"]:
        if not token:
            return "absent"
        with self._lock:
            expiry = self._expiry.get(token)
        return "valid" if expiry is not None and expiry > time.monotonic() else "expired"

    def revoke(self, token: str | None) -> None:
        if token:
            with self._lock:
                self._expiry.pop(token, None)


@dataclass
class CoreLedgerState:
    settings: MockSettings
    ledger: Ledger
    faults: InjectionState
    sessions: SessionStore


def state_of(app: Flask) -> CoreLedgerState:
    return cast(CoreLedgerState, app.extensions["coreledger"])


def _state() -> CoreLedgerState:
    return state_of(current_app)


def _one_shot() -> dict[str, ArmedInjection]:
    """Parse ?inject=a,b and ?inject_<param>=v for this request only."""
    cached = g.get("one_shot")
    if cached is not None:
        return cast(dict[str, ArmedInjection], cached)
    requested: dict[str, ArmedInjection] = {}
    if _state().settings.allow_query_injection:
        raw_params = {
            k[len("inject_") :]: v for k, v in request.args.items() if k.startswith("inject_")
        }
        for raw in request.args.getlist("inject"):
            for name in filter(None, (n.strip() for n in raw.split(","))):
                params = {k: v for k, v in raw_params.items() if k in _param_keys(name)}
                requested[name] = ArmedInjection(name, 0, validate_params(name, params))
    g.one_shot = requested
    return requested


def _param_keys(name: str) -> set[str]:
    """Which inject_<param> args belong to a fault, so one request can combine several faults."""
    validate_name(name)
    return {"slow": {"ms"}, "interstitial": {"sticky"}}.get(name, set()) | (
        {"on"} if name in ("permission_denied", "app_error") else set()
    )


def _fault(name: str, at: str | None = None) -> dict[str, str] | None:
    armed = _state().faults.fire(name, at=at, one_shot=_one_shot())
    return None if armed is None else armed.params


def _next_path() -> str:
    """Current path for the post-login redirect, minus ?inject= so a one-shot fault cannot loop."""
    kept = [(k, v) for k, v in request.args.items(multi=True) if not k.startswith("inject")]
    return request.path + (f"?{urlencode(kept)}" if kept else "")


def _safe_next(target: str | None) -> str:
    """Only same-origin absolute paths. Rejects //host, /\\host, control chars, and schemes."""
    fallback = "/members/search"
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return fallback
    if any(ord(ch) < 0x20 or ch in "\\\x7f" for ch in target):
        return fallback
    parts = urlsplit(target)
    return target if not parts.scheme and not parts.netloc else fallback


def login_required(view: F) -> F:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> AnyResponse:
        token = request.cookies.get(SESSION_COOKIE)
        status = _state().sessions.check(token)
        if status == "valid":
            return view(*args, **kwargs)
        query = {"next": _next_path()}
        if status == "expired":
            query["expired"] = "1"
        response = redirect(f"/login?{urlencode(query)}")
        response.delete_cookie(SESSION_COOKIE)
        return response

    return cast(F, wrapper)


def _expire_session_and_redirect() -> WerkzeugResponse:
    _state().sessions.revoke(request.cookies.get(SESSION_COOKIE))
    query = {"next": _next_path(), "expired": "1"}
    response = redirect(f"/login?{urlencode(query)}")
    response.delete_cookie(SESSION_COOKIE)
    return response


def _app_error() -> tuple[str, int]:
    return render_template("error_500.html"), 500


def _access_denied(reason: str) -> tuple[str, int]:
    return render_template("error_403.html", reason=reason), 403


def _not_found(member_id: str) -> tuple[str, int]:
    return render_template("error_404.html", member_id=member_id), 404


def _parse_deposit(raw: str) -> Decimal | None:
    cleaned = raw.strip().replace("$", "").replace(",", "")
    try:
        value = Decimal(cleaned)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def create_app(settings: MockSettings | None = None) -> Flask:
    settings = settings or MockSettings.from_env()
    app = Flask(__name__)
    app.extensions["coreledger"] = CoreLedgerState(
        settings=settings,
        ledger=Ledger(),
        faults=InjectionState(),
        sessions=SessionStore(ttl_s=settings.session_ttl_s),
    )
    app.jinja_env.globals["money"] = money

    @app.errorhandler(InjectionError)
    def bad_injection(exc: InjectionError) -> tuple[str, int]:
        return str(exc), 400

    # ---- shell -------------------------------------------------------------------------------

    @app.get("/")
    def shell() -> str:
        signed_in = _state().sessions.check(request.cookies.get(SESSION_COOKIE)) == "valid"
        return render_template(
            "frameset.html", main_src="/members/search" if signed_in else "/login"
        )

    @app.get("/banner")
    def banner() -> str:
        return render_template("banner.html")

    # ---- auth --------------------------------------------------------------------------------

    @app.get("/login")
    def login_form() -> str:
        return render_template(
            "login.html",
            next=request.args.get("next", ""),
            expired=request.args.get("expired") == "1",
            error=None,
        )

    @app.post("/login")
    def login_submit() -> AnyResponse:
        st = _state()
        user_ok = hmac.compare_digest(
            request.form.get("operator", "").encode(), st.settings.operator_user.encode()
        )
        pass_ok = hmac.compare_digest(
            request.form.get("password", "").encode(), st.settings.operator_password.encode()
        )
        if not (user_ok and pass_ok):
            return render_template(
                "login.html",
                next=request.form.get("next", ""),
                expired=False,
                error="Invalid operator ID or password.",
            )
        response = redirect(_safe_next(request.form.get("next")), code=303)
        response.set_cookie(SESSION_COOKIE, st.sessions.create(), httponly=True, samesite="Lax")
        return response

    @app.get("/logout")
    def logout() -> WerkzeugResponse:
        _state().sessions.revoke(request.cookies.get(SESSION_COOKIE))
        response = redirect("/login")
        response.delete_cookie(SESSION_COOKIE)
        return response

    # ---- members -----------------------------------------------------------------------------

    @app.get("/members/search")
    @login_required
    def member_search() -> AnyResponse:
        if _fault("app_error", at="search") is not None:
            return _app_error()
        drift = _fault("layout_drift") is not None
        q = request.args.get("q", "").strip()
        message = None
        if q:
            if not MEMBER_ID_RE.match(q):
                message = "Member number must be 5 digits."
            elif _fault("not_found") is not None or q not in _state().ledger.members:
                message = f"No member found for member number {q}."
            else:
                return redirect(f"/members/{q}")
        return render_template("search.html", q=q, message=message, drift=drift)

    def _member_or_none(member_id: str) -> Member | None:
        return _state().ledger.members.get(member_id)

    @app.get("/members/<member_id>")
    @login_required
    def member_detail(member_id: str) -> AnyResponse:
        if _fault("session_expired", at="detail") is not None:
            return _expire_session_and_redirect()
        slow = _fault("slow")
        if slow is not None:
            time.sleep(int(slow.get("ms", DEFAULT_SLOW_MS)) / 1000)
        if _fault("app_error", at="detail") is not None:
            return _app_error()
        member = _member_or_none(member_id)
        if member is None:
            return _not_found(member_id)
        denied = _fault("permission_denied", at="detail") is not None
        if member.restricted or denied:
            return _access_denied("Your operator role cannot open restricted member records.")
        interstitial = _fault("interstitial")
        return render_template(
            "member.html",
            member=member,
            interstitial=interstitial is not None,
            sticky=interstitial is not None and interstitial.get("sticky") == "1",
        )

    @app.get("/members/<member_id>/subaccounts/new")
    @login_required
    def subaccount_form(member_id: str) -> AnyResponse:
        member = _member_or_none(member_id)
        if member is None:
            return _not_found(member_id)
        if member.restricted:
            return _access_denied("Your operator role cannot open restricted member records.")
        return render_template(
            "subaccount_form.html", member=member, types=ACCOUNT_TYPES, form={}, errors={}
        )

    @app.post("/members/<member_id>/subaccounts/create")
    @login_required
    def subaccount_create(member_id: str) -> AnyResponse:
        st = _state()
        # Before anything is written: an expiry here creates nothing, which is what lets replay
        # report it plainly instead of leaving a caller wondering whether the record exists.
        if _fault("session_expired", at="create") is not None:
            return _expire_session_and_redirect()
        if _fault("app_error", at="create") is not None:
            return _app_error()
        member = _member_or_none(member_id)
        if member is None:
            return _not_found(member_id)
        denied = _fault("permission_denied", at="create") is not None
        if member.restricted or denied:
            return _access_denied("Your operator role cannot open sub-accounts for this member.")
        form = {k: request.form.get(k, "") for k in ("acct_type", "nickname", "initial_deposit")}
        errors: dict[str, str] = {}
        if form["acct_type"] not in ACCOUNT_TYPES:
            errors["acct_type"] = "Choose an account type."
        if len(form["nickname"]) > 20:
            errors["nickname"] = "Nickname must be 20 characters or fewer."
        # Consume the fault before looking at the input so its count never depends on the value.
        forced_invalid = _fault("validation_error") is not None
        deposit = _parse_deposit(form["initial_deposit"])
        if deposit is None:
            errors["initial_deposit"] = "Initial deposit must be a dollar amount."
        elif forced_invalid or deposit < Decimal("5.00"):
            errors["initial_deposit"] = "Initial deposit must be at least $5.00."
        if errors or deposit is None:
            return render_template(
                "subaccount_form.html", member=member, types=ACCOUNT_TYPES, form=form, errors=errors
            )
        sub = st.ledger.open_subaccount(member_id, form["acct_type"], form["nickname"], deposit)
        return redirect(f"/members/{member_id}/subaccounts/{sub.number}/confirmation", code=303)

    @app.get("/members/<member_id>/subaccounts/<number>/confirmation")
    @login_required
    def subaccount_confirmation(member_id: str, number: str) -> AnyResponse:
        sub = _state().ledger.subaccounts.get(number)
        member = _member_or_none(member_id)
        if sub is None or member is None or sub.member_id != member_id:
            return _not_found(member_id)
        return render_template(
            "subaccount_done.html",
            member=member,
            sub=sub,
            type_label=ACCOUNT_TYPES[sub.account_type],
        )

    # ---- failure injection control (dev only, denied to the agent by policy) ----------------

    @app.get("/__control")
    def control_page() -> str:
        st = _state()
        return render_template(
            "control.html",
            names=INJECTION_NAMES,
            descriptions=DESCRIPTIONS,
            armed=st.faults.snapshot(),
            subaccount_count=len(st.ledger.subaccounts),
        )

    @app.post("/__control")
    def control_form() -> AnyResponse:
        st = _state()
        action = request.form.get("action", "")
        name = request.form.get("name", "")
        if action == "arm":
            raw_times = request.form.get("times", "").strip().lower()
            times: int | Default | None
            if not raw_times:
                times = DEFAULT
            elif raw_times in ("none", "null"):
                times = None
            elif raw_times.isdigit():
                times = int(raw_times)
            else:
                raise InjectionError(f"times must be a positive integer or none, got {raw_times!r}")
            params = dict(
                pair.strip().split("=", 1)
                for pair in request.form.get("params", "").split(";")
                if "=" in pair
            )
            st.faults.arm(name, times=times, params=params)
        elif action == "clear":
            st.faults.clear(name)
        elif action == "reset":
            st.faults.reset()
            st.ledger.reset()
        else:
            return f"unknown action {action!r}", 400
        return redirect("/__control", code=303)

    @app.get("/__control/api/state")
    def control_state() -> Response:
        st = _state()
        armed = {
            k: {"remaining": v.remaining, "params": v.params}
            for k, v in st.faults.snapshot().items()
        }
        return jsonify(armed=armed, subaccounts=len(st.ledger.subaccounts))

    @app.post("/__control/api/arm")
    def control_arm() -> AnyResponse:
        body = request.get_json(silent=True) or {}
        try:
            params = body.get("params") or {}
            if not isinstance(params, dict):
                raise InjectionError("params must be an object")
            armed = _state().faults.arm(
                str(body.get("name", "")), times=body.get("times", DEFAULT), params=params
            )
        except InjectionError as exc:
            return make_response(jsonify(error=str(exc)), 400)
        return jsonify(name=armed.name, remaining=armed.remaining, params=armed.params)

    @app.post("/__control/api/clear")
    def control_clear() -> AnyResponse:
        body = request.get_json(silent=True) or {}
        try:
            _state().faults.clear(str(body.get("name", "")))
        except InjectionError as exc:
            return make_response(jsonify(error=str(exc)), 400)
        return jsonify(ok=True)

    @app.post("/__control/api/reset")
    def control_reset() -> Response:
        st = _state()
        st.faults.reset()
        st.ledger.reset()
        return jsonify(ok=True)

    return app
