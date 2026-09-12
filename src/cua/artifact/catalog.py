"""Catalog: capabilities on disk as reviewable JSON, one file per id and version, plus approval.

Files keep schema field order and end with a newline so review diffs stay small and readable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.overrides import resolve_for_tenant
from cua.artifact.schema import Capability
from cua.artifact.versioning import check_version, parse_version

SUFFIX = ".capability.json"
# The hand-written reference shape from phase 1. Not a catalog entry, but replayable by path.
EXAMPLE_FILE = "example" + SUFFIX


class CatalogError(ValueError):
    pass


class CatalogEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(description="Capability id.")
    version: str = Field(description="Semver.")
    status: str = Field(description="draft, approved, or deprecated.")
    name: str = Field(description="Human name.")
    path: str = Field(description="File path.")


def dump_json(capability: Capability) -> str:
    data = capability.model_dump(mode="json", exclude_none=True)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def load_capability(path: Path) -> Capability:
    """Parse and validate, including every tenant override, so a bad override fails at load time."""
    capability = Capability.model_validate_json(path.read_text(encoding="utf-8"))
    for tenant in capability.tenant_overrides:
        resolve_for_tenant(capability, tenant)
    return capability


class Catalog:
    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def filename(capability: Capability) -> str:
        return f"{capability.capability.id}@{capability.capability.version}{SUFFIX}"

    def _load_all(self) -> list[tuple[Capability, Path]]:
        found: list[tuple[Capability, Path]] = []
        for path in sorted(self.root.glob(f"*{SUFFIX}")):
            if path.name == EXAMPLE_FILE:
                continue
            capability = load_capability(path)
            if path.name != self.filename(capability):
                raise CatalogError(
                    f"{path.name} holds {self.filename(capability).removesuffix(SUFFIX)}; "
                    f"expected file name {self.filename(capability)}"
                )
            found.append((capability, path))
        return found

    def entries(self) -> list[CatalogEntry]:
        return [
            CatalogEntry(
                id=c.capability.id,
                version=c.capability.version,
                status=c.capability.status,
                name=c.capability.name,
                path=str(p),
            )
            for c, p in self._load_all()
        ]

    def get(self, capability_id: str, version: str | None = None) -> tuple[Capability, Path]:
        matches = [(c, p) for c, p in self._load_all() if c.capability.id == capability_id]
        if version is not None:
            matches = [(c, p) for c, p in matches if c.capability.version == version]
        else:
            matches = [(c, p) for c, p in matches if c.capability.status != "deprecated"]
        if not matches:
            wanted = f"{capability_id}@{version}" if version else capability_id
            raise CatalogError(f"no capability {wanted} in {self.root}")
        return max(matches, key=lambda cp: parse_version(cp[0].capability.version))

    def save(self, capability: Capability, *, overwrite: bool = False) -> Path:
        path = self.root / self.filename(capability)
        if path.exists() and not overwrite:
            raise CatalogError(f"{path} already exists; bump the version instead of overwriting")
        previous = [
            c
            for c, _ in self._load_all()
            if c.capability.id == capability.capability.id
            and parse_version(c.capability.version) < parse_version(capability.capability.version)
        ]
        if previous:
            latest = max(previous, key=lambda c: parse_version(c.capability.version))
            check_version(latest, capability)
        self.root.mkdir(parents=True, exist_ok=True)
        path.write_text(dump_json(capability), encoding="utf-8")
        return path

    def approve(
        self, capability_id: str, version: str, approved_by: str, *, now: datetime | None = None
    ) -> Path:
        capability, path = self.get(capability_id, version)
        if capability.capability.status != "draft":
            raise CatalogError(
                f"{capability_id}@{version} is {capability.capability.status}, not draft"
            )
        data = capability.model_dump(mode="json")
        approved_at = (now or datetime.now(UTC)).replace(microsecond=0)
        data["capability"].update(
            status="approved", approved_by=approved_by, approved_at=approved_at.isoformat()
        )
        approved = Capability.model_validate(data)
        path.write_text(dump_json(approved), encoding="utf-8")
        return path
