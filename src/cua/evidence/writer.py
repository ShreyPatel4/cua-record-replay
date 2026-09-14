"""Evidence writer: a directory per run holding a redacted JSON-lines log, screenshots, a manifest.

Every string value passes the redactor before it is written; binary files get a hash and a flag.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from cua.policy.gate import UnsafePath, normalize_path, path_matches
from cua.policy.redact import Redactor
from cua.surface.base import A11ySnapshot

Actor = Literal["model", "discovery", "replay", "human", "policy", "operator"]
# Values that are machine identifiers, not page data: redacting them only corrupts them.
UNREDACTED_KEYS = frozenset({"ts", "digest", "sha256"})


def redact_values(value: Any, redactor: Redactor) -> Any:
    """Redact string values anywhere in a JSON-like structure. Keys and numbers are kept."""
    if isinstance(value, str):
        return redactor.text(value)
    if isinstance(value, dict):
        return {
            k: v if k in UNREDACTED_KEYS else redact_values(v, redactor) for k, v in value.items()
        }
    if isinstance(value, list | tuple):
        return [redact_values(v, redactor) for v in value]
    return value


class EvidenceWriter:
    def __init__(
        self,
        root: Path,
        run_id: str,
        redactor: Redactor,
        *,
        sensitive_pages: Sequence[str] = (),
    ) -> None:
        self.dir = root / run_id
        self.dir.mkdir(parents=True, exist_ok=False)
        self.run_id = run_id
        self._redactor = redactor
        self._sensitive_pages = list(sensitive_pages)
        self._sensitive: set[str] = set()

    def event(self, actor: Actor, event: str, **fields: Any) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "actor": actor,
            "event": event,
            **fields,
        }
        line = json.dumps(redact_values(record, self._redactor), ensure_ascii=False)
        with (self.dir / "run.jsonl").open("a", encoding="utf-8") as log:
            log.write(line + "\n")

    def is_sensitive_page(self, urls: Iterable[str]) -> bool:
        for url in urls:
            try:
                path = normalize_path(urlsplit(url).path)
            except UnsafePath:
                return True
            if any(path_matches(glob, path) for glob in self._sensitive_pages):
                return True
        return False

    def screenshot(self, name: str, png: bytes, *, page_urls: Iterable[str]) -> str:
        """Saved as is, with no image redaction; flagged when a frame shows a sensitive page."""
        (self.dir / name).write_bytes(png)
        if self.is_sensitive_page(page_urls):
            self._sensitive.add(name)
        return name

    def flag_sensitive(self, name: str) -> None:
        """Mark a file sensitive after the fact, such as a screenshot showing a sensitive output."""
        self._sensitive.add(name)

    def snapshot(self, name: str, snapshot: A11ySnapshot) -> str:
        data = snapshot.redacted(self._redactor.text).model_dump(mode="json")
        return self.write_json(name, data)

    def write_json(self, name: str, data: Any) -> str:
        text = json.dumps(redact_values(data, self._redactor), indent=2, ensure_ascii=False)
        (self.dir / name).write_text(text + "\n", encoding="utf-8")
        return name

    def finish(self) -> Path:
        """Write manifest.json listing every file with its sha256 and sensitive flag."""
        files = []
        for path in sorted(self.dir.rglob("*")):
            if not path.is_file() or path.name == "manifest.json":
                continue
            rel = path.relative_to(self.dir).as_posix()
            files.append(
                {
                    "path": rel,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "bytes": path.stat().st_size,
                    "sensitive": rel in self._sensitive,
                }
            )
        manifest = {
            "run_id": self.run_id,
            "files": files,
            "purge_note": "Files flagged sensitive are screenshots of sign-in pages or of screens "
            "showing a sensitive output. Screenshots are not image-redacted; delete flagged files "
            "before sharing evidence outside the team.",
        }
        path = self.dir / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return path
