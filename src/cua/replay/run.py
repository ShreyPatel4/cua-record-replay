"""Replay runs: pre-run checks, redaction, evidence, tracing, and the engine, once or repeated.

The surface comes from a factory the caller supplies, so nothing in replay imports a browser.
"""

from __future__ import annotations

import json
import os
import secrets as token
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal, Protocol

from cua.artifact.inputs import InputError, TypedInput, coerce_inputs
from cua.artifact.overrides import OverrideError, resolve_for_tenant
from cua.artifact.schema import Capability
from cua.evidence.trace import scrub_trace
from cua.evidence.writer import EvidenceWriter
from cua.policy.enforce import GatedSurface
from cua.policy.fit import capability_policy_errors
from cua.policy.gate import PolicyGate
from cua.policy.models import Policy, PolicyError, load_policy
from cua.policy.redact import Redactor
from cua.replay.conditions import ConditionSurface
from cua.replay.engine import Clock, ReplayEngine
from cua.replay.result import IterationSummary, ReplayResult, StabilityReport, outputs_digest

EXTRA_SECRET_ENV = ("ANTHROPIC_API_KEY",)
TRACE_FILE = "trace.zip"
KEEP_TRACE_FOR = frozenset({"hard_failure", "escalated"})


class ReplaySurface(ConditionSurface, Protocol):
    def close(self) -> None: ...


SurfaceFactory = Callable[[], ReplaySurface]


@dataclass(frozen=True)
class ReplayRequest:
    capability: Capability
    inputs: Mapping[str, str]
    policy_file: Path
    evidence_root: Path
    tenant: str | None = None
    allow_draft: bool = False
    confirm_irreversible: bool = False
    extra_env_secrets: Sequence[str] = field(default_factory=lambda: EXTRA_SECRET_ENV)


def new_run_id(prefix: str, now: datetime) -> str:
    return f"{prefix}_{now:%Y%m%dT%H%M%SZ}_{token.token_hex(2)}"


class _Refused(Exception):
    def __init__(self, code: str, message: str, expected: str, observed: str) -> None:
        super().__init__(code)
        self.code, self.message, self.expected, self.observed = code, message, expected, observed


def _redactor(
    capability: Capability, request: ReplayRequest, environ: Mapping[str, str]
) -> Redactor:
    present = [s for s in capability.secrets if environ.get(s.env_var)]
    return Redactor(
        [environ[s.env_var] for s in present if s.kind == "credential"]
        + [environ[k] for k in request.extra_env_secrets if environ.get(k)],
        identities=[environ[s.env_var] for s in present if s.kind == "identity"],
    )


def _load_policy(request: ReplayRequest) -> tuple[Policy | None, str]:
    try:
        return load_policy(request.policy_file, request.capability.capability.policy_ref), ""
    except (PolicyError, OSError) as exc:
        return None, str(exc)


def _preflight(
    request: ReplayRequest, policy: Policy | None, policy_problem: str, environ: Mapping[str, str]
) -> tuple[Capability, Policy, dict[str, TypedInput], dict[str, str]]:
    """The five pre-run refusals, in the order a caller can fix them. Raises _Refused."""
    base = request.capability
    meta = base.capability
    try:
        capability = resolve_for_tenant(base, request.tenant)
    except OverrideError as exc:
        raise _Refused(
            "TENANT_UNKNOWN",
            str(exc),
            f"a tenant this capability knows: {sorted(base.tenant_overrides) or 'none'}",
            f"tenant {request.tenant!r}",
        ) from exc
    if meta.status != "approved" and not (meta.status == "draft" and request.allow_draft):
        hint = " Approve it, or pass --allow-draft." if meta.status == "draft" else ""
        raise _Refused(
            "DRAFT_NOT_APPROVED",
            f"{meta.id}@{meta.version} is {meta.status}; unattended replay needs an approved "
            f"capability.{hint}",
            "status approved",
            f"status {meta.status}",
        )
    if policy is None:
        raise _Refused(
            "POLICY_MISMATCH",
            f"policy {meta.policy_ref} could not be loaded: {policy_problem}",
            f"policy {meta.policy_ref} in {request.policy_file}",
            policy_problem,
        )
    misfits = capability_policy_errors(capability, policy)
    if misfits:
        raise _Refused(
            "POLICY_MISMATCH",
            "the capability does not fit its policy: " + "; ".join(misfits),
            f"every entry URL, navigation, and action inside {policy.id}",
            "; ".join(misfits),
        )
    try:
        typed = coerce_inputs(capability, request.inputs)
    except InputError as exc:
        raise _Refused(
            "INPUT_INVALID",
            "; ".join(exc.problems),
            "inputs: "
            + ", ".join(
                f"{p.name} ({p.type}{', required' if p.required else ''})"
                for p in capability.inputs
            ),
            "; ".join(exc.problems),
        ) from exc
    missing = [s.env_var for s in capability.secrets if not environ.get(s.env_var)]
    if missing:
        raise _Refused(
            "SECRET_MISSING",
            f"set {', '.join(missing)} in the environment; replay reads secrets only from there",
            "every secret the capability declares",
            f"unset: {', '.join(missing)}",
        )
    values = {s.name: environ[s.env_var] for s in capability.secrets}
    return capability, policy, typed, values


def run_replay(
    request: ReplayRequest,
    *,
    environ: Mapping[str, str],
    open_surface: SurfaceFactory,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    clock: Callable[[ReplaySurface], Clock] | None = None,
) -> ReplayResult:
    """One replay. Pre-run refusals and engine results alike leave result.json and a manifest."""
    base = request.capability
    started = now()
    run_id = new_run_id("replay", started)
    redactor = _redactor(base, request, environ)
    policy, policy_problem = _load_policy(request)
    evidence = EvidenceWriter(
        request.evidence_root,
        run_id,
        redactor,
        sensitive_pages=policy.sensitive_pages if policy else (),
    )
    masked = redactor.params(dict(request.inputs), [p.name for p in base.inputs if p.sensitive])
    try:
        capability, policy, typed, secret_values = _preflight(
            request, policy, policy_problem, environ
        )
    except _Refused as refused:
        result = ReplayResult(
            status="hard_failure",
            capability_id=base.capability.id,
            capability_version=base.capability.version,
            tenant=request.tenant,
            run_id=run_id,
            started_at=started,
            duration_ms=0,
            inputs=masked,
            outcome_code=refused.code,
            message=redactor.text(refused.message),
            expected=redactor.text(refused.expected),
            observed=redactor.text(refused.observed),
            evidence_dir=str(evidence.dir),
        )
        evidence.event("replay", "run_refused", code=refused.code, message=result.message)
        return _conclude(result, base, evidence)

    surface = open_surface()
    try:
        gated = GatedSurface(
            surface, surface, PolicyGate(policy), confirm_irreversible=request.confirm_irreversible
        )
        surface.start_trace()
        engine = ReplayEngine(
            capability,
            surface,
            gated,
            inputs=typed,
            masked_inputs=masked,
            secrets=secret_values,
            redactor=redactor,
            evidence=evidence,
            run_id=run_id,
            started_at=started,
            tenant=request.tenant,
            clock=clock(surface) if clock else None,
        )
        result = engine.run()
        _keep_or_discard_trace(surface, result, redactor, evidence)
    except BaseException as exc:
        evidence.event("replay", "run_crashed", error=type(exc).__name__, message=str(exc))
        evidence.finish()
        raise
    finally:
        surface.stop_trace(None)
        surface.close()
    problems = result.contract_problems(capability)
    if problems:
        evidence.event("replay", "contract_problems", problems=problems)
    return _conclude(result, capability, evidence)


def _keep_or_discard_trace(
    surface: ReplaySurface, result: ReplayResult, redactor: Redactor, evidence: EvidenceWriter
) -> None:
    """Failures keep a trace, scrubbed of secrets and cookies before it reaches evidence. The raw
    archive lives only in a temporary directory, so a crash cannot leave it behind."""
    if result.status not in KEEP_TRACE_FOR:
        surface.stop_trace(None)
        return
    with TemporaryDirectory(prefix="cua-trace-") as tmp:
        raw = Path(tmp) / TRACE_FILE
        if surface.stop_trace(raw):
            scrub_trace(raw, evidence.dir / TRACE_FILE, redactor)
            evidence.flag_sensitive(TRACE_FILE)
            evidence.event("replay", "trace_saved", path=TRACE_FILE)


def _conclude(
    result: ReplayResult, capability: Capability, evidence: EvidenceWriter
) -> ReplayResult:
    evidence.write_json("result.json", result.for_evidence(capability))
    evidence.finish()
    return result


Expected = Literal["success", "business_outcome"]


def run_stability(
    request: ReplayRequest,
    *,
    repeat: int,
    environ: Mapping[str, str],
    open_surface: SurfaceFactory,
    expected_status: Expected = "success",
    before_each: Callable[[int], Mapping[str, str]] | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[StabilityReport, Path]:
    """Replay the same request repeat times into one batch directory and report stability.

    before_each lets a harness re-arm the app before iteration i and returns the conditions it set,
    so a difference the harness caused is never read as flakiness.
    """
    if repeat < 1:
        raise ValueError("repeat must be at least 1")
    batch = request.evidence_root / new_run_id("stability", now())
    key = os.urandom(32)
    iterations: list[IterationSummary] = []
    for index in range(repeat):
        conditions = dict(before_each(index)) if before_each else {}
        result = run_replay(
            replace(request, evidence_root=batch),
            environ=environ,
            open_surface=open_surface,
            now=now,
        )
        iterations.append(
            IterationSummary(
                run_id=result.run_id,
                status=result.status,
                outcome_code=result.outcome_code,
                rungs={s.step_id: s.resolved_strategy for s in result.steps if s.resolved_strategy},
                outputs_digest=outputs_digest(result.outputs, key),
                duration_ms=result.duration_ms,
                conditions=conditions,
            )
        )
    meta = request.capability.capability
    report = StabilityReport(
        capability_id=meta.id,
        capability_version=meta.version,
        expected_status=expected_status,
        iterations=iterations,
    )
    path = batch / "stability.json"
    path.write_text(json.dumps(stability_summary(report), indent=2) + "\n", encoding="utf-8")
    return report, path


def stability_summary(report: StabilityReport) -> dict[str, object]:
    low, median, high = report.duration_spread_ms
    return {
        "capability": f"{report.capability_id}@{report.capability_version}",
        "expected_status": report.expected_status,
        "runs": len(report.iterations),
        "passes": report.passes,
        "determinism": report.determinism,
        "rung_distribution": report.rung_distribution,
        "duration_ms": {"min": low, "median": median, "max": high},
        "iterations": [i.model_dump(mode="json") for i in report.iterations],
        "note": "outputs_digest is an HMAC under a key that existed only in memory for this "
        "report: equal digests mean equal outputs, and a digest cannot be reversed to a value.",
    }
