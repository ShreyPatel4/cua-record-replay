"""PolicyGate: the one choke point every discovery and replay action passes before the surface acts.

Order of checks: action type, then every URL involved (origin, deny, allow, query), then risk.
"""

from __future__ import annotations

import fnmatch
import re
from functools import cache
from urllib.parse import parse_qsl, urlsplit

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


@cache
def _glob_regex(glob: str) -> re.Pattern[str]:
    """'**' matches any depth, '*' matches within one path segment, everything else is literal."""
    parts = re.split(r"(\*\*|\*)", glob)
    body = "".join(".*" if p == "**" else "[^/]*" if p == "*" else re.escape(p) for p in parts)
    return re.compile(f"^{body}$")


def path_matches(glob: str, path: str) -> bool:
    return _glob_regex(glob).match(path) is not None


class PolicyGate:
    def __init__(self, policy: Policy) -> None:
        self.policy = policy

    def check(self, proposed: ProposedAction) -> PolicyDecision:
        risk = max_risk(proposed.declared_risk, self._classify(proposed))
        if proposed.action not in self.policy.actions_allow:
            return Block(reason=f"action {proposed.action!r} is not in actions_allow", risk=risk)

        urls = [("frame", proposed.frame_url)]
        if proposed.navigate_url is not None:
            urls.append(("destination", proposed.navigate_url))
        for label, url in urls:
            if label == "frame" and url == "about:blank" and proposed.action == "navigate":
                continue
            reason = self._url_block_reason(url)
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

    def _url_block_reason(self, url: str) -> str | None:
        parts = urlsplit(url)
        if not parts.scheme or not parts.hostname:
            return f"url {url!r} is not absolute"
        origin = f"{parts.scheme}://{parts.hostname}" + (f":{parts.port}" if parts.port else "")
        if origin not in self.policy.origins:
            return f"origin {origin} is not allowlisted"
        path = parts.path or "/"
        for glob in self.policy.paths_deny:
            if path_matches(glob, path):
                return f"path {path} matches paths_deny {glob}"
        if not any(path_matches(glob, path) for glob in self.policy.paths_allow):
            return f"path {path} matches no paths_allow entry"
        for name, _ in parse_qsl(parts.query, keep_blank_values=True):
            for pattern in self.policy.query_deny:
                if fnmatch.fnmatchcase(name, pattern):
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
            return False
        if rule.target_text_matches is not None and not re.search(
            rule.target_text_matches, proposed.target_text or ""
        ):
            return False
        if rule.url_matches is not None and not path_matches(
            rule.url_matches, urlsplit(proposed.frame_url).path or "/"
        ):
            return False
        if rule.dialog_message_matches is not None and not re.search(
            rule.dialog_message_matches, proposed.dialog_message or ""
        ):
            return False
        return rule.dialog_response is None or rule.dialog_response == proposed.dialog_response
