"""PolicyGate: the one choke point every discovery and replay action passes before the surface acts.

Order of checks: action type, then every URL involved (origin, deny, allow, query), then risk.
"""

from __future__ import annotations

import fnmatch
import re
from functools import cache
from urllib.parse import parse_qsl, unquote, urlsplit

from cua.policy.models import (
    Allow,
    Block,
    Policy,
    PolicyDecision,
    ProposedAction,
    RequiresConfirmation,
    RiskRule,
)
from cua.vocab import RiskClass, max_risk

_ENCODED_BYTE = re.compile(r"%[0-9A-Fa-f]{2}")


@cache
def _glob_regex(glob: str) -> re.Pattern[str]:
    """'**' matches any depth, '*' matches within one path segment, everything else is literal."""
    parts = re.split(r"(\*\*|\*)", glob)
    body = "".join(".*" if p == "**" else "[^/]*" if p == "*" else re.escape(p) for p in parts)
    return re.compile(f"^{body}$")


def path_matches(glob: str, path: str) -> bool:
    return _glob_regex(glob).match(path) is not None


class UnsafePath(ValueError):
    pass


def normalize_path(raw: str) -> str:
    """The path a server would route: decoded once, slashes collapsed, dot segments resolved.

    Anything a server might read differently from this function is refused instead of guessed at:
    double encoding, backslashes, path parameters, and control characters.
    """
    decoded = unquote(raw or "/")
    if _ENCODED_BYTE.search(decoded):
        raise UnsafePath(f"path {raw!r} is double-encoded")
    if any(ch in decoded for ch in "\\;") or any(
        ord(ch) < 0x20 or ord(ch) == 0x7F for ch in decoded
    ):
        raise UnsafePath(f"path {raw!r} has a backslash, path parameter, or control character")
    segments: list[str] = []
    for segment in decoded.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    trailing = bool(segments) and decoded.endswith(("/", "/.", "/.."))
    return "/" + "/".join(segments) + ("/" if trailing else "")


class PolicyGate:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def check(self, proposed: ProposedAction) -> PolicyDecision:
        if proposed.phase == "subresource":
            assert proposed.navigate_url is not None
            reason = self.url_block_reason(proposed.navigate_url, require_allow=False)
            if reason is None:
                return Allow(risk="safe")
            return Block(reason=f"subresource {reason}", risk="safe")
        risk = max_risk(proposed.declared_risk, self._classify(proposed))
        if proposed.action not in self.policy.actions_allow:
            return Block(reason=f"action {proposed.action!r} is not in actions_allow", risk=risk)

        urls = [("frame", proposed.frame_url)]
        if proposed.navigate_url is not None:
            urls.append(("destination", proposed.navigate_url))
        for label, url in urls:
            if label == "frame" and url == "about:blank" and proposed.action == "navigate":
                continue
            reason = self.url_block_reason(url)
            if reason is not None:
                return Block(reason=f"{label} {reason}", risk=risk)

        if risk == "irreversible":
            reason = self._irreversible_reason(proposed)
            if self.policy.irreversible_handling == "block":
                return Block(reason=f"irreversible action blocked by policy: {reason}", risk=risk)
            return RequiresConfirmation(
                reason=reason, risk="irreversible", handling=self.policy.irreversible_handling
            )
        return Allow(risk=risk)

    def url_block_reason(self, url: str, *, require_allow: bool = True) -> str | None:
        """Why a URL is off policy, or None. require_allow=False skips paths_allow."""
        if "\\" in url:
            return f"url {url!r} contains a backslash"
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return f"url {url!r} is not an absolute http(s) URL"
        if "@" in parts.netloc:
            return f"url {url!r} carries userinfo"
        try:
            port = parts.port
        except ValueError:
            return f"url {url!r} has an invalid port"
        host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        origin = f"{parts.scheme}://{host}" + (f":{port}" if port else "")
        if origin not in self.policy.origins:
            return f"origin {origin} is not allowlisted"
        try:
            path = normalize_path(parts.path)
        except UnsafePath as exc:
            return str(exc)
        for glob in self.policy.paths_deny:
            if path_matches(glob, path):
                return f"path {path} matches paths_deny {glob}"
        if require_allow and not any(path_matches(glob, path) for glob in self.policy.paths_allow):
            return f"path {path} matches no paths_allow entry"
        for name, _ in parse_qsl(parts.query, keep_blank_values=True):
            for pattern in self.policy.query_deny:
                if fnmatch.fnmatchcase(name.lower(), pattern.lower()):
                    return f"query parameter {name!r} matches query_deny {pattern}"
        return None

    def _classify(self, proposed: ProposedAction) -> RiskClass:
        if any(self._rule_matches(r, proposed) for r in self.policy.risk.irreversible_when):
            return "irreversible"
        if any(self._rule_matches(r, proposed) for r in self.policy.risk.reversible_when):
            return "reversible"
        return "safe"

    def _irreversible_reason(self, proposed: ProposedAction) -> str:
        for rule in self.policy.risk.irreversible_when:
            if self._rule_matches(rule, proposed):
                return f"matches irreversible rule {rule.model_dump(exclude_none=True)}"
        return "declared irreversible by the artifact"

    @staticmethod
    def _rule_matches(rule: RiskRule, proposed: ProposedAction) -> bool:
        if rule.action is not None and rule.action != proposed.action:
            pressed_like_click = (
                rule.action == "click"
                and proposed.action == "press_key"
                and proposed.key == "Enter"
                and proposed.target_text is not None
            )
            if not pressed_like_click:
                return False
        if rule.target_text_matches is not None and not re.search(
            rule.target_text_matches, proposed.target_text or ""
        ):
            return False
        if rule.url_matches is not None:
            urls = [proposed.frame_url]
            if proposed.phase == "navigation" and proposed.navigate_url is not None:
                urls.append(proposed.navigate_url)
            try:
                paths = [normalize_path(urlsplit(url).path) for url in urls]
            except UnsafePath:
                return True
            if not any(path_matches(rule.url_matches, path) for path in paths):
                return False
        if rule.dialog_message_matches is not None and not re.search(
            rule.dialog_message_matches, proposed.dialog_message or ""
        ):
            return False
        return rule.dialog_response is None or rule.dialog_response == proposed.dialog_response
