"""Policy: the allowlist file resolves correctly and the gate allows, blocks, or asks as specified.

Uses the real policy/allowlist.yaml so a careless edit to the file fails here, not in a live run.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from cua.policy.gate import PolicyGate, path_matches
from cua.policy.models import (
    Allow,
    Block,
    PolicyError,
    PolicyFile,
    ProposedAction,
    RequiresConfirmation,
    load_policy,
    resolve_policy,
)

from support import ROOT

POLICY_FILE = ROOT / "policy" / "allowlist.yaml"
BASE = "http://127.0.0.1:5050"


@pytest.fixture(scope="module")
def readonly() -> PolicyGate:
    return PolicyGate(load_policy(POLICY_FILE, "coreledger-readonly"))


@pytest.fixture(scope="module")
def subaccount() -> PolicyGate:
    return PolicyGate(load_policy(POLICY_FILE, "coreledger-subaccount"))


def _act(**fields: Any) -> ProposedAction:
    fields.setdefault("frame_url", f"{BASE}/members/search")
    fields.setdefault("action", "click")
    return ProposedAction(**fields)


def test_extends_adds_lists_and_overrides_handling(subaccount: PolicyGate) -> None:
    policy = subaccount.policy
    assert policy.chain == ["coreledger-readonly", "coreledger-subaccount"]
    assert "/members/search" in policy.paths_allow
    assert "/members/*/subaccounts/new" in policy.paths_allow
    assert "/__control/**" in policy.paths_deny
    assert policy.irreversible_handling == "confirm"


def test_ordinary_search_is_allowed(readonly: PolicyGate) -> None:
    decision = readonly.check(
        _act(
            action="navigate",
            navigate_url=f"{BASE}/members/search?q=10007",
            frame_url="about:blank",
        )
    )
    assert decision == Allow(risk="safe")


def test_typing_is_reversible(readonly: PolicyGate) -> None:
    assert readonly.check(_act(action="type_text", target_text="")) == Allow(risk="reversible")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://localhost:5050/members/search", "origin http://localhost:5050"),
        ("https://127.0.0.1:5050/members/search", "origin https://127.0.0.1:5050"),
        ("http://evil.example/members/search", "origin http://evil.example"),
        (f"{BASE}/__control/api/arm", "paths_deny /__control/**"),
        (f"{BASE}/__control", "paths_deny /__control"),
        (f"{BASE}/logout", "paths_deny /logout"),
        (f"{BASE}/members/10007/subaccounts/new", "matches no paths_allow"),
        (f"{BASE}/members/10007?inject=app_error", "query_deny inject"),
        (f"{BASE}/members/10007?inject_ms=1", "query_deny inject_*"),
        ("/members/search", "not an absolute"),
        ("javascript:alert(1)", "not an absolute"),
        (f"{BASE}/%5F_control", "paths_deny /__control"),
        (f"{BASE}/members/..%2F__control", "paths_deny /__control"),
        (f"{BASE}/members/%2e%2e/__control/api/arm", "paths_deny /__control/**"),
        (f"{BASE}//__control", "paths_deny /__control"),
        (f"{BASE}/__control/", "paths_deny /__control/**"),
        (f"{BASE}/members/%252e%252e/__control", "double-encoded"),
        (f"{BASE}/members/search;inject=app_error", "path parameter"),
        ("http://evil.example@127.0.0.1:5050/members/search", "userinfo"),
        ("http://127.0.0.1:5050\\@evil.example/", "backslash"),
        (f"{BASE}/members/search?INJECT=app_error", "query_deny inject"),
        ("http://127.0.0.1:99999/", "invalid port"),
    ],
)
def test_navigation_outside_the_allowlist_is_blocked(
    readonly: PolicyGate, url: str, reason: str
) -> None:
    decision = readonly.check(_act(action="navigate", navigate_url=url, frame_url="about:blank"))
    assert isinstance(decision, Block)
    assert reason in decision.reason


def test_acting_inside_a_disallowed_page_is_blocked(readonly: PolicyGate) -> None:
    decision = readonly.check(
        _act(frame_url=f"{BASE}/members/10007/subaccounts/new", target_text="Cancel")
    )
    assert isinstance(decision, Block)
    assert decision.reason.startswith("frame")


def test_about_blank_is_only_exempt_for_the_first_navigation(readonly: PolicyGate) -> None:
    assert isinstance(readonly.check(_act(frame_url="about:blank")), Block)


def test_action_type_not_allowed() -> None:
    file = PolicyFile.model_validate(
        {
            "policies": {
                "tiny": {
                    "origins": [BASE],
                    "paths_allow": ["/**"],
                    "actions_allow": ["navigate", "click"],
                    "irreversible_handling": "block",
                }
            }
        }
    )
    decision = PolicyGate(resolve_policy(file, "tiny")).check(_act(action="type_text"))
    assert isinstance(decision, Block)
    assert "not in actions_allow" in decision.reason


def test_irreversible_click_escalates_under_readonly(readonly: PolicyGate) -> None:
    decision = readonly.check(_act(target_text="Submit transfer"))
    assert isinstance(decision, RequiresConfirmation)
    assert decision.handling == "escalate"


def test_create_click_needs_confirmation_under_subaccount(subaccount: PolicyGate) -> None:
    decision = subaccount.check(
        _act(frame_url=f"{BASE}/members/10007/subaccounts/new", target_text="Create sub-account")
    )
    assert isinstance(decision, RequiresConfirmation)
    assert decision.handling == "confirm"


def test_the_real_dialog_is_judged_when_it_opens_even_behind_a_bland_button(
    subaccount: PolicyGate,
) -> None:
    form = f"{BASE}/members/10007/subaccounts/new"
    assert subaccount.check(_act(frame_url=form, target_text="Go")) == Allow(risk="safe")
    dialog = {"phase": "dialog", "frame_url": form, "target_text": "Go"}
    message = "Open this sub-account? This cannot be undone."
    accepted = subaccount.check(_act(**dialog, dialog_message=message, dialog_response="accept"))
    assert isinstance(accepted, RequiresConfirmation)
    dismissed = subaccount.check(_act(**dialog, dialog_message=message, dialog_response="dismiss"))
    assert dismissed == Allow(risk="safe")


def test_a_click_that_navigates_off_policy_is_blocked_at_the_request(readonly: PolicyGate) -> None:
    decision = readonly.check(
        _act(
            phase="navigation",
            frame_url=f"{BASE}/banner",
            target_text="Sign out",
            navigate_url=f"{BASE}/logout",
        )
    )
    assert isinstance(decision, Block)
    assert decision.reason == "destination path /logout matches paths_deny /logout"


def test_dot_segments_that_stay_inside_the_allowlist_are_allowed(readonly: PolicyGate) -> None:
    url = f"{BASE}/./members/../members/search"
    decision = readonly.check(_act(action="navigate", navigate_url=url, frame_url="about:blank"))
    assert decision == Allow(risk="safe")


def test_phase_shapes_are_enforced() -> None:
    with pytest.raises(ValidationError, match="need navigate_url"):
        _act(phase="navigation")
    with pytest.raises(ValidationError, match="need navigate_url"):
        _act(action="navigate")
    with pytest.raises(ValidationError, match="dialog_message and dialog_response"):
        _act(phase="dialog", dialog_response="accept")


def test_ipv6_origins_are_supported() -> None:
    file = PolicyFile.model_validate(
        {
            "policies": {
                "v6": {
                    "origins": ["http://[::1]:5050"],
                    "paths_allow": ["/**"],
                    "actions_allow": ["navigate"],
                    "irreversible_handling": "block",
                }
            }
        }
    )
    gate = PolicyGate(resolve_policy(file, "v6"))
    url = "http://[::1]:5050/members/search"
    assert gate.check(_act(action="navigate", navigate_url=url, frame_url="about:blank")) == Allow(
        risk="safe"
    )


def test_declared_risk_is_never_lowered_by_the_gate(readonly: PolicyGate) -> None:
    decision = readonly.check(_act(target_text="Find", declared_risk="irreversible"))
    assert isinstance(decision, RequiresConfirmation)


def test_irreversible_is_blocked_when_handling_is_block() -> None:
    file = PolicyFile.model_validate(
        {
            "policies": {
                "strict": {
                    "origins": [BASE],
                    "paths_allow": ["/**"],
                    "actions_allow": ["click"],
                    "risk": {"irreversible_when": [{"target_text_matches": "(?i)delete"}]},
                    "irreversible_handling": "block",
                }
            }
        }
    )
    decision = PolicyGate(resolve_policy(file, "strict")).check(_act(target_text="Delete member"))
    assert isinstance(decision, Block)
    assert decision.risk == "irreversible"


@pytest.mark.parametrize(
    ("glob", "path", "expected"),
    [
        ("/members/*", "/members/10007", True),
        ("/members/*", "/members/10007/subaccounts/new", False),
        ("/members/*/subaccounts/new", "/members/10007/subaccounts/new", True),
        ("/__control/**", "/__control/api/arm", True),
        ("/__control/**", "/__control", False),
        ("/", "/", True),
        ("/", "/members", False),
    ],
)
def test_glob_semantics(glob: str, path: str, expected: bool) -> None:
    assert path_matches(glob, path) is expected


def test_extends_cycle_is_rejected() -> None:
    file = PolicyFile.model_validate(
        {
            "policies": {
                "a": {"extends": "b", "irreversible_handling": "block"},
                "b": {"extends": "a"},
            }
        }
    )
    with pytest.raises(PolicyError, match="cycle"):
        resolve_policy(file, "a")


def test_unknown_parent_and_missing_handling_are_rejected() -> None:
    with pytest.raises(PolicyError, match="unknown policy 'ghost'"):
        resolve_policy(PolicyFile.model_validate({"policies": {"a": {"extends": "ghost"}}}), "a")
    with pytest.raises(PolicyError, match="never sets irreversible_handling"):
        resolve_policy(PolicyFile.model_validate({"policies": {"a": {}}}), "a")


@pytest.mark.parametrize(
    "entry",
    [
        {"origins": ["http://127.0.0.1:5050/members"]},
        {"origins": ["HTTP://127.0.0.1:5050"]},
        {"paths_allow": ["members/*"]},
        {"risk": {"irreversible_when": [{}]}},
        {"risk": {"irreversible_when": [{"target_text_matches": "(unclosed"}]}},
        {"actions_allow": ["drag"]},
        {"unexpected": True},
    ],
)
def test_malformed_policy_entries_are_rejected(entry: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PolicyFile.model_validate({"policies": {"a": entry}})
