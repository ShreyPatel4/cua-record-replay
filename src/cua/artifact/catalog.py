"""Catalog: capabilities on disk as reviewable JSON, one file per id and version, plus approval.

Files keep schema field order and end with a newline so review diffs stay small and readable.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from cua.artifact.overrides import OverrideError, resolve_for_tenant
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
    status: Literal["draft", "approved", "deprecated"] = Field(description="Approval state.")
    name: str = Field(description="Human name.")
    path: str = Field(description="File path.")


def dump_json(capability: Capability) -> str:
    data = capability.model_dump(mode="json", exclude_none=True)
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def load_capability(path: Path, policy_file: Path | None = None) -> Capability:
    """Parse and validate, resolving every tenant, so a bad override fails at load time.

    With a policy file, the base and every tenant must also stay inside the named policy.
    """
    from cua.policy.fit import capability_policy_errors
    from cua.policy.models import PolicyError, load_policy

    try:
        capability = Capability.model_validate_json(path.read_text(encoding="utf-8"))
        variants = [capability] + [
            resolve_for_tenant(capability, t) for t in capability.tenant_overrides
        ]
    except (ValidationError, OverrideError) as exc:
        raise CatalogError(f"{path.name}: {exc}") from exc
    if policy_file is None:
        return capability
    try:
        policy = load_policy(policy_file, capability.capability.policy_ref)
    except PolicyError as exc:
        raise CatalogError(f"{path.name}: {exc}") from exc
    tenants = [None, *capability.tenant_overrides]
    problems = [
        f"{'base' if tenant is None else 'tenant ' + tenant}: {error}"
        for tenant, variant in zip(tenants, variants, strict=True)
        for error in capability_policy_errors(variant, policy)
    ]
    if problems:
        raise CatalogError(f"{path.name} does not fit its policy: " + "; ".join(problems))
    return capability


class Catalog:
    def __init__(self, root: Path, policy_file: Path | None = None) -> None:
        self.root = root
        self.policy_file = policy_file

    @staticmethod
    def filename(capability: Capability) -> str:
        return f"{capability.capability.id}@{capability.capability.version}{SUFFIX}"

    def _load_all(self) -> list[tuple[Capability, Path]]:
        found: list[tuple[Capability, Path]] = []
        problems: list[str] = []
        for path in sorted(self.root.glob(f"*{SUFFIX}")):
            if path.name == EXAMPLE_FILE:
                continue
            try:
                capability = load_capability(path, self.policy_file)
            except CatalogError as exc:
                problems.append(str(exc))
                continue
            if path.name != self.filename(capability):
                problems.append(
                    f"{path.name} holds {self.filename(capability).removesuffix(SUFFIX)}; "
                    f"expected file name {self.filename(capability)}"
                )
                continue
            found.append((capability, path))
        if problems:
            raise CatalogError(f"{len(problems)} bad catalog file(s): " + " | ".join(problems))
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

    def get(
        self, capability_id: str, version: str | None = None, *, include_drafts: bool = False
    ) -> tuple[Capability, Path]:
        """An exact version, or else the latest approved one (latest non-deprecated with drafts)."""
        matches = [(c, p) for c, p in self._load_all() if c.capability.id == capability_id]
        if version is not None:
            matches = [(c, p) for c, p in matches if c.capability.version == version]
            wanted = f"{capability_id}@{version}"
        else:
            allowed = {"approved", "draft"} if include_drafts else {"approved"}
            usable = [(c, p) for c, p in matches if c.capability.status in allowed]
            if not usable and any(c.capability.status == "draft" for c, _ in matches):
                raise CatalogError(
                    f"{capability_id} has no approved version; approve a draft or allow drafts"
                )
            matches = usable
            wanted = capability_id
        if not matches:
            raise CatalogError(f"no capability {wanted} in {self.root}")
        return max(matches, key=lambda cp: parse_version(cp[0].capability.version))

    def save(self, capability: Capability, *, overwrite: bool = False) -> Path:
        if capability.capability.status != "draft":
            raise CatalogError("only drafts are saved; approval happens through catalog approve")
        path = self.root / self.filename(capability)
        if path.exists():
            existing = load_capability(path)
            if existing.capability.status != "draft":
                raise CatalogError(
                    f"{path} is {existing.capability.status} and is never overwritten"
                )
            if not overwrite:
                raise CatalogError(
                    f"{path} already exists; bump the version instead of overwriting"
                )
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
