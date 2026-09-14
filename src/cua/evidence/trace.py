"""Trace archives for evidence: a Playwright trace copied with secrets and session cookies removed.

Cookie values are learned from the archive itself, then scrubbed from every member and encoding.
"""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from cua.policy.redact import MIN_SECRET_LEN, REDACTED, Redactor

# Header values that carry a live session or a credential, whatever the page did.
_SECRET_HEADERS = frozenset({"cookie", "set-cookie", "authorization", "proxy-authorization"})
_JSON_LINE_SUFFIXES = (".trace", ".network")
# The same headers written as text, as Playwright's own log records do ("set-cookie: A=b; ...").
_HEADER_TEXT = re.compile(
    r"(?im)^(?P<head>[ \t]*(?P<name>set-cookie|cookie|proxy-authorization|authorization)"
    r"[ \t]*:[ \t]*)(?P<value>[^\r\n]+)"
)


def _cookie_values(header: str, value: str) -> list[str]:
    """Cookie values in a Cookie header (every pair) or a Set-Cookie header (the first pair)."""
    name = header.lower()
    if name not in ("cookie", "set-cookie"):
        return [value.split(" ", 1)[-1]] if value else []
    pairs = value.split(";") if name == "cookie" else value.split(";")[:1]
    found = []
    for pair in pairs:
        _, sep, cookie = pair.strip().partition("=")
        if sep and cookie.strip():
            found.append(cookie.strip())
    return found


class _Masker:
    """Walks one decoded JSON record, blanking header values and collecting what they held."""

    def __init__(self) -> None:
        self.values: set[str] = set()

    def walk(self, value: Any, parent_key: str = "") -> tuple[Any, bool]:
        if isinstance(value, str):
            return self._text(value)
        changed = False
        if isinstance(value, dict):
            name = value.get("name")
            secret = value.get("value")
            is_header = isinstance(name, str) and name.lower() in _SECRET_HEADERS
            if (
                (is_header or parent_key == "cookies")
                and isinstance(secret, str)
                and secret != REDACTED
            ):
                self.values.update(
                    _cookie_values(name, secret) if is_header else [secret]  # type: ignore[arg-type]
                )
                value["value"] = REDACTED
                changed = True
            for key, nested in list(value.items()):
                value[key], nested_changed = self.walk(nested, key)
                changed |= nested_changed
        elif isinstance(value, list):
            for index, nested in enumerate(value):
                value[index], nested_changed = self.walk(nested, parent_key)
                changed |= nested_changed
        return value, changed

    def _text(self, text: str) -> tuple[str, bool]:
        def mask(match: re.Match[str]) -> str:
            self.values.update(_cookie_values(match.group("name"), match.group("value")))
            return match.group("head") + REDACTED

        masked, count = _HEADER_TEXT.subn(mask, text)
        return masked, count > 0


def _mask_lines(data: bytes, masker: _Masker) -> bytes:
    lines = []
    for line in data.split(b"\n"):
        try:
            record = json.loads(line) if line.strip() else None
        except ValueError:
            record = None
        if record is not None:
            record, changed = masker.walk(record)
            if changed:
                line = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode()
        lines.append(line)
    return b"\n".join(lines)


def scrub_trace(
    source: Path, dest: Path, redactor: Redactor, session_tokens: Iterable[str] = ()
) -> None:
    """Write a copy of the trace at source to dest with no secret left in any member.

    Header and cookie values are blanked where they appear as data, then every value seen there,
    plus the live session tokens the caller passes (cookies deleted earlier in the run are only
    in the archive), is added to the redactor and replaced in every member in every encoding.
    Account numbers are left alone: masking digit runs would corrupt the trace viewer's offsets.
    """
    masker = _Masker()
    with zipfile.ZipFile(source) as src:
        members = [
            (
                info,
                _mask_lines(src.read(info), masker)
                if info.filename.endswith(_JSON_LINE_SUFFIXES)
                else src.read(info),
            )
            for info in src.infolist()
        ]
    for value in {*masker.values, *session_tokens}:
        if len(value) >= MIN_SECRET_LEN and value != REDACTED:
            redactor.add_credential(value)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
        for info, data in members:
            out.writestr(info.filename, redactor.scrub_bytes(data))
