"""Trace archives for evidence: a Playwright trace copied with secrets and session cookies removed.

Works on bytes, so every member is covered: event logs, network logs, DOM snapshots, request bodies.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any

from cua.policy.redact import REDACTED, Redactor

# Header values that carry a live session or a credential, whatever the page did.
_SECRET_HEADERS = frozenset({"cookie", "set-cookie", "authorization", "proxy-authorization"})
_JSON_LINE_SUFFIXES = (".trace", ".network")


def _mask_headers(value: Any, parent_key: str = "") -> bool:
    """Blank header and cookie values in place. True when anything changed."""
    changed = False
    if isinstance(value, dict):
        name = value.get("name")
        header = isinstance(name, str) and name.lower() in _SECRET_HEADERS
        if (header or parent_key == "cookies") and value.get("value") not in (None, REDACTED):
            value["value"] = REDACTED
            changed = True
        for key, nested in value.items():
            changed |= _mask_headers(nested, key)
    elif isinstance(value, list):
        for nested in value:
            changed |= _mask_headers(nested, parent_key)
    return changed


def _mask_header_lines(data: bytes) -> bytes:
    lines = []
    for line in data.split(b"\n"):
        try:
            record = json.loads(line) if line.strip() else None
        except ValueError:
            record = None
        if record is not None and _mask_headers(record):
            line = json.dumps(record, separators=(",", ":"), ensure_ascii=False).encode()
        lines.append(line)
    return b"\n".join(lines)


def scrub_trace(source: Path, dest: Path, redactor: Redactor) -> None:
    """Write a copy of the trace at source to dest: cookie and auth headers blanked, and every
    secret the redactor knows replaced in every member. Account numbers are left alone, since
    masking digit runs would corrupt timestamps and offsets the trace viewer relies on."""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.endswith(_JSON_LINE_SUFFIXES):
                data = _mask_header_lines(data)
            out.writestr(info.filename, redactor.scrub_bytes(data))
