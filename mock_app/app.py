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
from urllib.parse import urlencode

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
    DEFAULT_SLOW_MS,
    DESCRIPTIONS,
    INJECTION_NAMES,
    InjectionState,
    UnknownInjectionError,
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


def _one_shot() -> dict[str, dict[str, str]]:
    """Parse ?inject=a,b and ?inject_<param>=v for this request only."""
    cached = g.get("one_shot")
    if cached is not None:
        return cast(dict[str, dict[str, str]], cached)
    requested: dict[str, dict[str, str]] = {}
    if _state().settings.allow_query_injection:
        params = {
            k[len("inject_") :]: v for k, v in request.args.items() if k.startswith("inject_")
        }
        for raw in request.args.getlist("inject"):
            for name in filter(None, (n.strip() for n in raw.split(","))):
                InjectionState.validate(name)
                requested[name] = dict(params)
    g.one_shot = requested
    return requested


def _fault(
    name: str, at: str | None = None, default_at: str | None = None
) -> dict[str, str] | None:
    armed = _state().faults.fire(name, at=at, default_at=default_at, one_shot=_one_shot())
    return None if armed is None else armed.params


def _safe_next(target: str | None) -> str:
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return "/members/search"


def login_required(view: F) -> F:
    @wraps(view)
    def wrapper(*args: Any, **kwargs: Any) -> AnyResponse:
        token = request.cookies.get(SESSION_COOKIE)
        status = _state().sessions.check(token)
        if status == "valid":
            return view(*args, **kwargs)
        query = {"next": request.full_path.rstrip("?")}
        if status == "expired":
            query["expired"] = "1"
        response = redirect(f"/login?{urlencode(query)}")
        response.delete_cookie(SESSION_COOKIE)
        return response

    return cast(F, wrapper)


def _expire_session_and_redirect() -> WerkzeugResponse:
    _state().sessions.revoke(request.cookies.get(SESSION_COOKIE))
    query = {"next": request.full_path.rstrip("?"), "expired": "1"}
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

    @app.errorhandler(UnknownInjectionError)
    def unknown_injection(exc: UnknownInjectionError) -> tuple[str, int]:
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
        if _fault("app_error", at="search", default_at="detail") is not None:
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
        if _fault("session_expired") is not None:
            return _expire_session_and_redirect()
        slow = _fault("slow")
        if slow is not None:
            time.sleep(int(slow.get("ms", DEFAULT_SLOW_MS)) / 1000)
        if _fault("app_error", at="detail", default_at="detail") is not None:
            return _app_error()
        member = _member_or_none(member_id)
        if member is None:
            return _not_found(member_id)
        denied = _fault("permission_denied", at="detail", default_at="create") is not None
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
        if _fault("app_error", at="create", default_at="detail") is not None:
            return _app_error()
        member = _member_or_none(member_id)
        if member is None:
            return _not_found(member_id)
        denied = _fault("permission_denied", at="create", default_at="create") is not None
        if member.restricted or denied:
            return _access_denied("Your operator role cannot open sub-accounts for this member.")
        form = {k: request.form.get(k, "") for k in ("acct_type", "nickname", "initial_deposit")}
        errors: dict[str, str] = {}
        if form["acct_type"] not in ACCOUNT_TYPES:
            errors["acct_type"] = "Choose an account type."
        if len(form["nickname"]) > 20:
            errors["nickname"] = "Nickname must be 20 characters or fewer."
        deposit = _parse_deposit(form["initial_deposit"])
        if deposit is None:
            errors["initial_deposit"] = "Initial deposit must be a dollar amount."
        elif deposit < Decimal("5.00") or _fault("validation_error") is not None:
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
    def control_form() -> WerkzeugResponse:
        st = _state()
        action = request.form.get("action", "")
        name = request.form.get("name", "")
        if action == "arm":
            raw_times = request.form.get("times", "").strip()
            params = dict(
                pair.split("=", 1)
                for pair in request.form.get("params", "").split(";")
                if "=" in pair
            )
            st.faults.arm(name, times=int(raw_times) if raw_times else -1, params=params)
        elif action == "clear":
            st.faults.clear(name)
        elif action == "reset":
            st.faults.reset()
            st.ledger.reset()
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
        times = body.get("times", -1)
        try:
            armed = _state().faults.arm(
                str(body.get("name", "")), times=times, params=dict(body.get("params") or {})
            )
        except (UnknownInjectionError, ValueError, TypeError) as exc:
            return make_response(jsonify(error=str(exc)), 400)
        return jsonify(name=armed.name, remaining=armed.remaining, params=armed.params)

    @app.post("/__control/api/clear")
    def control_clear() -> AnyResponse:
        body = request.get_json(silent=True) or {}
        try:
            _state().faults.clear(str(body.get("name", "")))
        except UnknownInjectionError as exc:
            return make_response(jsonify(error=str(exc)), 400)
        return jsonify(ok=True)

    @app.post("/__control/api/reset")
    def control_reset() -> Response:
        st = _state()
        st.faults.reset()
        st.ledger.reset()
        return jsonify(ok=True)

    return app
