"""Discovery run: wires policy, secrets, redaction, evidence, browser, and model around the loop.

When the loop reaches done: confirm parameters with a human, build the draft, check it, save it.
"""

from __future__ import annotations

import re
import secrets as token
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from playwright.sync_api import Browser, sync_playwright
from pydantic import BaseModel, ConfigDict, Field

from cua.artifact.catalog import SUFFIX, Catalog, CatalogError, dump_json
from cua.discover.loop import DiscoveryConfig, DiscoveryLoop, DiscoveryOutcome, Secret
from cua.discover.model import ModelClient
from cua.discover.record import (
    ArtifactMeta,
    BuildError,
    ParamProposal,
    SecretSpec,
    build_capability,
    propose_parameters,
)
from cua.evidence.writer import EvidenceWriter
from cua.policy.enforce import GatedSurface
from cua.policy.gate import PolicyGate
from cua.policy.models import load_policy
from cua.policy.redact import Redactor
from cua.replay.conditions import holds
from cua.surface.playwright import PlaywrightSurface

ConfirmParams = Callable[[list[ParamProposal]], list[ParamProposal]]
EXTRA_SECRET_ENV = ("ANTHROPIC_API_KEY",)
CAPABILITY_ID_RE = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}")
DISCOVERED_VERSION = "1.0.0"


class DiscoveryError(RuntimeError):
    """The run could not start: unknown policy, off-policy entry URL, or a missing secret."""


@dataclass(frozen=True)
class DiscoverRequest:
    goal: str
    entry_url: str
    capability_id: str
    name: str
    policy_id: str
    policy_file: Path
    secrets: Sequence[SecretSpec]
    evidence_root: Path
    artifacts_root: Path
    app_version_hint: str = "unknown"
    max_steps: int = 25
    timeout_s: float = 180.0
    allow_irreversible: bool = False
    headed: bool = False
    extra_env_secrets: Sequence[str] = field(default_factory=lambda: EXTRA_SECRET_ENV)


class ResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ParameterRecord(ResultModel):
    name: str = Field(description="Proposed input name, as finally accepted or declined.")
    label: str = Field(description="Field the literal was typed into.")
    pattern: str | None = Field(description="Proposed pattern.")
    accepted: bool = Field(description="Whether the human made it an input.")


class DiscoveryResult(ResultModel):
    run_id: str = Field(description="Discovery run id; also the evidence directory name.")
    goal: str = Field(description="Goal as given, redacted.")
    entry_url: str = Field(description="Where the run started.")
    model: str = Field(description="Model that drove the run.")
    policy: str = Field(description="Policy id the gate enforced.")
    status: Literal["recorded", "stopped", "failed"] = Field(
        description="recorded: a draft artifact was saved. stopped: the loop ended without done "
        "(stuck, gave up, budget). failed: done was reached but no valid artifact came of it, or "
        "the model or entry page failed."
    )
    stop_reason: str = Field(description="Why the loop ended.")
    message: str = Field(description="Human-readable detail, redacted.")
    turns: int = Field(ge=0, description="Model turns used.")
    duration_ms: int = Field(ge=0, description="Loop wall time.")
    input_tokens: int = Field(ge=0, description="Model input tokens.")
    output_tokens: int = Field(ge=0, description="Model output tokens.")
    steps_recorded: int = Field(ge=0, description="Steps in the recorded flow.")
    outputs: list[str] = Field(description="Declared output names; values never appear here.")
    parameters: list[ParameterRecord] = Field(description="Parameter proposals and decisions.")
    capability: str | None = Field(description="id@version of the saved draft.")
    artifact_path: str | None = Field(description="Where the draft was saved.")
    evidence_dir: str = Field(description="This run's evidence directory.")


def _secret_values(specs: Sequence[SecretSpec], environ: Mapping[str, str]) -> dict[str, Secret]:
    missing = [s.env_var for s in specs if not environ.get(s.env_var)]
    if missing:
        raise DiscoveryError(f"missing secrets in the environment: {missing}")
    return {s.name: Secret(spec=s, value=environ[s.env_var]) for s in specs}


def new_run_id(now: datetime) -> str:
    return f"disc_{now:%Y%m%dT%H%M%SZ}_{token.token_hex(2)}"


def run_discovery(
    request: DiscoverRequest,
    *,
    model: ModelClient,
    environ: Mapping[str, str],
    confirm: ConfirmParams,
    browser: Browser | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> DiscoveryResult:
    policy = load_policy(request.policy_file, request.policy_id)
    gate = PolicyGate(policy)
    refusal = gate.url_block_reason(request.entry_url)
    if refusal is not None:
        raise DiscoveryError(f"the entry URL is outside policy {request.policy_id}: {refusal}")
    # Checked before any model call: a run that cannot be saved should not be paid for.
    if not CAPABILITY_ID_RE.fullmatch(request.capability_id):
        raise DiscoveryError(
            f"capability id {request.capability_id!r} must be dotted, like "
            "<app_family>.<entity>.<verb_phrase>"
        )
    existing = request.artifacts_root / f"{request.capability_id}@{DISCOVERED_VERSION}{SUFFIX}"
    if existing.exists():
        raise DiscoveryError(
            f"{existing} already exists; discovery records {DISCOVERED_VERSION}, so choose another "
            "--capability-id or remove that draft first"
        )
    secrets = _secret_values(request.secrets, environ)
    redactor = Redactor(
        [s.value for s in secrets.values() if s.spec.kind == "credential"]
        + [environ[k] for k in request.extra_env_secrets if environ.get(k)],
        identities=[s.value for s in secrets.values() if s.spec.kind == "identity"],
    )
    started = now().replace(microsecond=0)
    run_id = new_run_id(started)
    evidence = EvidenceWriter(
        request.evidence_root, run_id, redactor, sensitive_pages=policy.sensitive_pages
    )
    config = DiscoveryConfig(
        goal=request.goal,
        entry_url=request.entry_url,
        max_steps=request.max_steps,
        timeout_s=request.timeout_s,
        allow_irreversible=request.allow_irreversible,
    )

    def with_browser(chromium: Browser) -> DiscoveryResult:
        surface = PlaywrightSurface.launch(chromium, control=lambda: None)
        try:
            gated = GatedSurface(surface, surface, gate)
            loop = DiscoveryLoop(
                surface,
                gated,
                model,
                config,
                secrets=secrets,
                redactor=redactor,
                evidence=evidence,
            )
            outcome = loop.run()
            return _conclude(outcome, surface)
        finally:
            surface.close()

    def _conclude(outcome: DiscoveryOutcome, surface: PlaywrightSurface) -> DiscoveryResult:
        base = {
            "run_id": run_id,
            "goal": redactor.text(request.goal),
            "entry_url": request.entry_url,
            "model": model.model,
            "policy": request.policy_id,
            "stop_reason": outcome.stop_reason,
            "turns": outcome.turns,
            "duration_ms": outcome.duration_ms,
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "evidence_dir": str(evidence.dir),
        }
        record = outcome.record
        if record is None:
            status = (
                "failed" if outcome.stop_reason in ("model_error", "entry_failed") else "stopped"
            )
            return DiscoveryResult(
                **base,
                status=status,
                message=redactor.free_text(outcome.message),
                steps_recorded=0,
                outputs=[],
                parameters=[],
                capability=None,
                artifact_path=None,
            )

        def keep_final_screen() -> None:
            with suppress(Exception):
                evidence.snapshot("a11y_final.json", surface.snapshot())

        proposals = propose_parameters(record)
        accepted = confirm(proposals)
        accepted_literals = {p.literal for p in accepted}
        parameters = [
            ParameterRecord(
                name=next((a.name for a in accepted if a.literal == p.literal), p.name),
                label=p.label,
                pattern=p.pattern,
                accepted=p.literal in accepted_literals,
            )
            for p in proposals
        ]
        evidence.event(
            "human",
            "parameters_confirmed",
            parameters=[p.model_dump() for p in parameters],
        )
        common = {
            **base,
            "steps_recorded": len(record.steps),
            "outputs": [o.name for o in record.outputs],
            "parameters": parameters,
        }
        meta = ArtifactMeta(
            capability_id=request.capability_id,
            name=request.name,
            policy_ref=request.policy_id,
            app_version_hint=request.app_version_hint,
            model=model.model,
            run_id=run_id,
            evidence_run=str(evidence.dir),
            created_at=started,
        )
        try:
            capability = build_capability(record, accepted, meta, redactor)
        except BuildError as exc:
            keep_final_screen()
            return DiscoveryResult(
                **common,
                status="failed",
                message=redactor.text(str(exc)),
                capability=None,
                artifact_path=None,
            )
        evidence.event(
            "discovery",
            "checkpoints_derived",
            steps=[
                {
                    "step_id": step.id,
                    "wait": step.wait_for.kind,
                    "checkpoint": getattr(step.wait_for, "checkpoint", None),
                }
                for step in capability.steps
            ],
        )
        success = capability.checkpoints[capability.success.checkpoint]
        values = {p.name: p.literal for p in accepted}
        success_holds = holds(success.condition, surface, values)
        evidence.event(
            "discovery",
            "success_checkpoint",
            checkpoint=capability.success.checkpoint,
            holds=success_holds,
        )
        if not success_holds:
            keep_final_screen()
            return DiscoveryResult(
                **common,
                status="failed",
                message=f"the derived success checkpoint {capability.success.checkpoint} does not "
                "hold on the final screen, so the draft was not saved",
                capability=None,
                artifact_path=None,
            )
        (evidence.dir / "artifact.capability.json").write_text(
            dump_json(capability), encoding="utf-8"
        )
        try:
            path = Catalog(request.artifacts_root, request.policy_file).save(capability)
        except CatalogError as exc:
            keep_final_screen()
            return DiscoveryResult(
                **common,
                status="failed",
                message=redactor.text(f"the draft is valid but was not saved: {exc}"),
                capability=None,
                artifact_path=None,
            )
        meta_id = f"{capability.capability.id}@{capability.capability.version}"
        evidence.event("discovery", "artifact_saved", capability=meta_id, path=str(path))
        return DiscoveryResult(
            **common,
            status="recorded",
            message=f"saved draft {meta_id}; review it and add outcome detectors before approval",
            capability=meta_id,
            artifact_path=str(path),
        )

    try:
        if browser is not None:
            result = with_browser(browser)
        else:
            with sync_playwright() as pw:
                chromium = pw.chromium.launch(headless=not request.headed)
                try:
                    result = with_browser(chromium)
                finally:
                    chromium.close()
    except BaseException as exc:
        crashed = DiscoveryResult(
            run_id=run_id,
            goal=redactor.text(request.goal),
            entry_url=request.entry_url,
            model=model.model,
            policy=request.policy_id,
            status="failed",
            stop_reason="crashed",
            message=redactor.free_text(f"discovery crashed: {type(exc).__name__}: {exc}"),
            turns=0,
            duration_ms=0,
            input_tokens=0,
            output_tokens=0,
            steps_recorded=0,
            outputs=[],
            parameters=[],
            capability=None,
            artifact_path=None,
            evidence_dir=str(evidence.dir),
        )
        with suppress(Exception):
            evidence.write_json("result.json", crashed.model_dump(mode="json"))
        evidence.finish()
        raise
    evidence.write_json("result.json", result.model_dump(mode="json"))
    evidence.finish()
    return result
