"""Command line entry point: discover, replay, ops, catalog, and mock subcommands.

Exit codes: 0 success or business outcome, 1 usage or internal error, 2 hard failure, 3 escalated.
"""

import re
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer
from dotenv import load_dotenv

from cua.log import configure_logging

if TYPE_CHECKING:
    from cua.artifact.catalog import Catalog
    from cua.artifact.schema import Capability
    from cua.discover.record import SecretSpec
    from cua.discover.run import ConfirmParams
    from cua.session.state import SessionState

# Environment variables whose values are secrets worth redacting out of an operator's own words.
_SECRETISH = re.compile(r"(?i)(pass|pin|secret|token|key|credential)")
ARTIFACTS_DIR = Path("artifacts")
POLICY_FILE = Path("policy/allowlist.yaml")

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
ops_app = typer.Typer(no_args_is_help=True, help="Operator commands for paused runs.")
catalog_app = typer.Typer(no_args_is_help=True, help="Browse and approve saved capabilities.")
mock_app_cli = typer.Typer(no_args_is_help=True, help="Run the local CoreLedger mock app.")
app.add_typer(ops_app, name="ops")
app.add_typer(catalog_app, name="catalog")
app.add_typer(mock_app_cli, name="mock")


@app.callback()
def main(
    log_level: Annotated[str, typer.Option(help="DEBUG, INFO, WARNING, ERROR.")] = "INFO",
) -> None:
    load_dotenv(override=False)
    configure_logging(log_level)


DEFAULT_SECRETS = [
    "operator_id=CORELEDGER_OPERATOR_USER:identity",
    "operator_password=CORELEDGER_OPERATOR_PASSWORD:credential",
]


def _secret_specs(raw: list[str]) -> "list[SecretSpec]":
    from cua.discover.record import SecretSpec

    specs = []
    for item in raw:
        name, _, rest = item.partition("=")
        env_var, _, kind = rest.partition(":")
        if not name or not env_var or kind not in ("", "credential", "identity"):
            typer.echo(f"--secret {item!r}: expected name=ENV_VAR[:credential|identity]", err=True)
            raise typer.Exit(code=1)
        specs.append(SecretSpec(name=name, env_var=env_var, kind=kind or "credential"))  # type: ignore[arg-type]
    return specs


def _confirm_parameters(assume_yes: bool) -> "ConfirmParams":
    import re

    from cua.discover.record import ParamProposal

    def confirm(proposals: list[ParamProposal]) -> list[ParamProposal]:
        accepted = []
        if not proposals:
            typer.echo("No typed value matches the goal, so the draft takes no inputs.")
        for proposal in proposals:
            steps = ", ".join(f"s{i + 1:02d}" for i in proposal.step_indexes)
            typer.echo(
                f"{steps}: typed {proposal.literal!r} into {proposal.label!r}, "
                "a value the goal names."
            )
            if assume_yes:
                typer.echo(f"  accepted as {{{{inputs.{proposal.name}}}}} (--yes)")
                accepted.append(proposal)
                continue
            if not typer.confirm(
                f"  Make it the input {{{{inputs.{proposal.name}}}}}?", default=True
            ):
                continue
            name = typer.prompt("  Input name", default=proposal.name)
            while not re.fullmatch(r"[a-z][a-z0-9_]*", name):
                name = typer.prompt("  snake_case, starting with a letter", default=proposal.name)
            accepted.append(ParamProposal(**{**proposal.__dict__, "name": name}))
        return accepted

    return confirm


@app.command()
def discover(
    goal: Annotated[str, typer.Option(help="Natural language goal.")],
    target: Annotated[str, typer.Option(help="Entry URL of the target application.")],
    capability_id: Annotated[
        str, typer.Option(help="Dotted id for the draft: <app_family>.<entity>.<verb_phrase>.")
    ] = "coreledger.member.discovered_flow",
    name: Annotated[
        str | None, typer.Option(help="Short human name. Defaults to the goal.")
    ] = None,
    policy: Annotated[str, typer.Option(help="Policy id from policy/allowlist.yaml.")] = (
        "coreledger-readonly"
    ),
    secret: Annotated[
        list[str] | None,
        typer.Option(
            help="name=ENV_VAR[:credential|identity], repeatable. Defaults to CoreLedger."
        ),
    ] = None,
    app_version: Annotated[str, typer.Option(help="Build the flow is recorded against.")] = (
        "unknown"
    ),
    max_steps: Annotated[int, typer.Option(min=1)] = 25,
    timeout_s: Annotated[int, typer.Option(min=1)] = 180,
    allow_irreversible: Annotated[bool, typer.Option()] = False,
    headed: Annotated[bool, typer.Option()] = False,
    yes: Annotated[
        bool, typer.Option("--yes", help="Accept proposed input parameters without prompting.")
    ] = False,
    model: Annotated[str | None, typer.Option(help="Defaults to CUA_MODEL.")] = None,
    evidence_root: Annotated[Path, typer.Option(help="Where the run directory goes.")] = Path(
        "evidence/_scratch"
    ),
    artifacts: Annotated[Path, typer.Option(help="Catalog directory for the draft.")] = (
        ARTIFACTS_DIR
    ),
) -> None:
    """Run the LLM agent on a goal and record the successful run as a draft capability."""
    import os
    import sys

    from cua.discover.model import AnthropicModel
    from cua.discover.run import DiscoverRequest, DiscoveryError, run_discovery
    from cua.policy.models import PolicyError

    if not yes and not sys.stdin.isatty():
        typer.echo(
            "discovery ends by confirming input parameters; run in a terminal or pass --yes",
            err=True,
        )
        raise typer.Exit(code=1)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        typer.echo(
            "ANTHROPIC_API_KEY is not set; discovery needs a model. Replay does not.", err=True
        )
        raise typer.Exit(code=1)
    request = DiscoverRequest(
        goal=goal,
        entry_url=target,
        capability_id=capability_id,
        name=name or goal,
        policy_id=policy,
        policy_file=POLICY_FILE,
        secrets=_secret_specs(secret or DEFAULT_SECRETS),
        evidence_root=evidence_root,
        artifacts_root=artifacts,
        app_version_hint=app_version,
        max_steps=max_steps,
        timeout_s=timeout_s,
        allow_irreversible=allow_irreversible,
        headed=headed,
    )
    client = AnthropicModel(api_key, model or os.environ.get("CUA_MODEL") or "claude-sonnet-4-6")
    try:
        result = run_discovery(
            request, model=client, environ=os.environ, confirm=_confirm_parameters(yes)
        )
    except (DiscoveryError, PolicyError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(result.model_dump_json(indent=2))
    raise typer.Exit(code=0 if result.status == "recorded" else 2)


def _load_for_replay(artifact: str, version: str | None, root: Path) -> "Capability":
    from cua.artifact.catalog import Catalog, CatalogError, load_capability

    path = Path(artifact)
    try:
        if path.suffix == ".json" or path.exists():
            return load_capability(path)
        capability, _ = Catalog(root).get(artifact, version, include_drafts=True)
    except (CatalogError, OSError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    return capability


def _pairs(raw: list[str], option: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for item in raw:
        name, sep, value = item.partition("=")
        if not sep or not name or name in pairs:
            typer.echo(f"{option} {item!r}: expected a unique name=value", err=True)
            raise typer.Exit(code=1)
        pairs[name] = value
    return pairs


@app.command()
def replay(
    artifact: Annotated[
        str,
        typer.Argument(help="Capability JSON file, or a catalog id."),
    ],
    inputs: Annotated[
        list[str] | None, typer.Option("--input", "-i", help="name=value, repeatable.")
    ] = None,
    version: Annotated[
        str | None, typer.Option(help="Catalog version. Defaults to the latest.")
    ] = None,
    tenant: Annotated[str | None, typer.Option(help="Apply this tenant's overrides.")] = None,
    repeat: Annotated[int, typer.Option(min=1, help="Run N times and report stability.")] = 1,
    expect: Annotated[
        str, typer.Option(help="What a passing --repeat iteration is: success or business_outcome.")
    ] = "success",
    allow_draft: Annotated[
        bool, typer.Option(help="Replay a capability not yet approved.")
    ] = False,
    confirm_irreversible: Annotated[
        bool, typer.Option(help="Allow irreversible steps under confirm handling.")
    ] = False,
    headed: Annotated[bool, typer.Option()] = False,
    escalation_timeout: Annotated[
        float,
        typer.Option(
            min=0,
            help="Seconds a paused run waits for an operator. 0 attaches no operator channel, so "
            "an escalation ends the run as a hard failure. Ignored with --repeat: a stability "
            "batch is unattended by definition.",
        ),
    ] = 300.0,
    evidence_root: Annotated[Path, typer.Option(help="Where run directories go.")] = Path(
        "evidence/_scratch"
    ),
    artifacts: Annotated[Path, typer.Option(help="Catalog directory.")] = ARTIFACTS_DIR,
) -> None:
    """Replay a capability deterministically, with no model in the loop.

    Prints the ReplayResult (or, with --repeat, the stability report) as JSON. Exit 0 for success
    and business outcomes, 2 for a hard failure, 3 when escalated.
    """
    import json
    import os

    from playwright.sync_api import sync_playwright

    from cua.evidence.writer import EvidenceWriter
    from cua.policy.redact import Redactor
    from cua.replay.run import (
        Control,
        ReplayRequest,
        ReplaySurface,
        run_replay,
        run_stability,
        stability_summary,
    )
    from cua.session.operator import OperatorChannel
    from cua.surface.playwright import PlaywrightSurface

    if expect not in ("success", "business_outcome"):
        typer.echo("--expect must be success or business_outcome", err=True)
        raise typer.Exit(code=1)
    capability = _load_for_replay(artifact, version, artifacts)
    request = ReplayRequest(
        capability=capability,
        inputs=_pairs(inputs or [], "--input"),
        policy_file=POLICY_FILE,
        evidence_root=evidence_root,
        tenant=tenant,
        allow_draft=allow_draft,
        confirm_irreversible=confirm_irreversible,
    )
    with sync_playwright() as pw:
        chromium = pw.chromium.launch(headless=not headed)
        try:

            def open_surface(control: "Control") -> PlaywrightSurface:
                return PlaywrightSurface.launch(chromium, control=control)

            def open_escalation(
                evidence: "EvidenceWriter", surface: "ReplaySurface", redactor: "Redactor"
            ) -> OperatorChannel:
                return OperatorChannel(
                    run_id=evidence.run_id,
                    capability_id=capability.capability.id,
                    evidence=evidence,
                    surface=surface,
                    redactor=redactor,
                    resume_command=lambda run: (
                        f"uv run cua ops take-control {run} --evidence-root {evidence_root}"
                    ),
                    wait_s=escalation_timeout,
                )

            if repeat == 1:
                result = run_replay(
                    request,
                    environ=os.environ,
                    open_surface=open_surface,
                    open_escalation=open_escalation if escalation_timeout > 0 else None,
                )
                typer.echo(result.model_dump_json(indent=2))
                code = result.exit_code
            else:
                report, path = run_stability(
                    request,
                    repeat=repeat,
                    environ=os.environ,
                    open_surface=open_surface,
                    expected_status="business_outcome"
                    if expect == "business_outcome"
                    else "success",
                )
                typer.echo(json.dumps({**stability_summary(report), "report": str(path)}, indent=2))
                code = (
                    0 if report.passes == repeat and report.determinism != "nondeterministic" else 2
                )
        finally:
            chromium.close()
    raise typer.Exit(code=code)


EvidenceRootOption = Annotated[
    Path, typer.Option("--evidence-root", help="Where run directories live.")
]
OperatorOption = Annotated[
    str | None, typer.Option("--operator", help="Who you are. Defaults to $CUA_OPERATOR or $USER.")
]


def _operator_id(given: str | None) -> str:
    """The id that lands in the session file, the intervention, and the result."""
    import os

    who = given or os.environ.get("CUA_OPERATOR") or os.environ.get("USER") or "operator"
    return who.strip() or "operator"


def _sessions(root: Path) -> "list[tuple[Path, SessionState]]":
    """Every run under the evidence root that has a session file, newest first."""
    from cua.session.state import SessionState

    found = []
    for path in sorted(root.glob("*/session_state.json")) + sorted(
        root.glob("*/*/session_state.json")
    ):
        try:
            found.append((path, SessionState.model_validate_json(path.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return sorted(found, key=lambda pair: pair[1].updated_at, reverse=True)


def _session(run_id: str, root: Path) -> "tuple[Path, SessionState]":
    for path, state in _sessions(root):
        if state.run_id == run_id or path.parent.name == run_id:
            return path, state
    typer.echo(f"no session for run {run_id!r} under {root}", err=True)
    raise typer.Exit(code=1)


def _act_on(
    run_id: str,
    root: Path,
    event: str,
    actor: str,
    *,
    operator: str | None = None,
    note: str = "",
) -> None:
    """One transition on one run's session file, with the errors an operator can act on."""
    import os
    from typing import cast

    from cua.policy.redact import Redactor
    from cua.session.state import (
        Actor,
        Event,
        IllegalTransition,
        NotInControl,
        StaleState,
        StateStore,
    )

    path, state = _session(run_id, root)
    # The note is free text in a committed evidence file, and an operator writes what they did,
    # which is where a credential turns up. Redaction knows the environment's secrets; anything
    # else the operator invents is why the file is flagged sensitive in the manifest.
    known = Redactor(
        [v for k, v in os.environ.items() if _SECRETISH.search(k) and len(v) >= 4],
    )
    note = known.free_text(note)
    try:
        updated = StateStore(path).transition(
            cast(Event, event),
            cast(Actor, actor),
            expected_version=state.version,
            operator_id=operator,
            note=note,
        )
    except (IllegalTransition, NotInControl) as exc:
        typer.echo(f"{run_id} is {state.phase}: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    except StaleState as exc:
        typer.echo(f"{run_id} changed while you were reading it: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"{run_id}: {state.phase} -> {updated.phase} (controller {updated.controller})")


def _alive(state: "SessionState") -> bool:
    """Whether the process that owns the browser is still there. A paused run whose process died
    cannot be taken: nothing would answer the hand-back."""
    import os

    if state.phase == "finished" or state.owner_pid is None:
        return state.phase != "finished"
    try:
        os.kill(state.owner_pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@ops_app.command("list")
def ops_list(evidence_root: EvidenceRootOption = Path("evidence/_scratch")) -> None:
    """Show runs with a session, paused ones first."""
    rows = _sessions(evidence_root)
    if not rows:
        typer.echo(f"no sessions under {evidence_root}")
        return
    waiting = [r for r in rows if r[1].phase in ("paused_for_human", "human_active", "resuming")]
    for path, state in waiting + [r for r in rows if r not in waiting]:
        held = f" held by {state.operator_id}" if state.operator_id else ""
        dead = "" if _alive(state) else "  [dead: the run that owned this session is gone]"
        typer.echo(
            f"{state.run_id}  {state.phase:<16} controller={state.controller:<10}"
            f" updated={state.updated_at:%H:%M:%S}{held}  {path.parent}{dead}"
        )


def _screen_lines(snapshot: dict[str, object]) -> list[str]:
    """What the run could see when it stopped, in the order a person reads a screen.

    The accessibility snapshot is the automation's own perception, so showing it is showing the
    operator what the run was looking at, not a second guess at it.
    """
    nodes = snapshot.get("nodes")
    frames = snapshot.get("frame_urls")
    lines: list[str] = []
    if isinstance(frames, dict):
        for path, url in frames.items():
            lines.append(f"    frame {path or 'top':8} {url}")
    if not isinstance(nodes, list):
        return lines
    for node in nodes:
        if not isinstance(node, dict):
            continue
        said = str(node.get("name") or node.get("text") or "").strip()
        if not said:
            continue
        where = "/".join(str(part) for part in node.get("frame_path") or []) or "top"
        lines.append(f"    {where:8} {node.get('role') or ''!s:10} {said[:70]}")
    return lines


@ops_app.command("show")
def ops_show(
    run_id: str,
    evidence_root: EvidenceRootOption = Path("evidence/_scratch"),
    open_screenshot: Annotated[
        bool,
        typer.Option(
            "--open/--no-open", help="Open the screenshot in the default viewer. On by default."
        ),
    ] = True,
    as_json: Annotated[
        bool, typer.Option("--json", help="The raw request, for a tool rather than a person.")
    ] = False,
) -> None:
    """Show why a run stopped, what it was looking at, and what a human has done so far."""
    import json
    import subprocess
    import sys

    path, state = _session(run_id, evidence_root)
    run_dir = path.parent
    request_file = run_dir / "intervention.json"
    if not request_file.exists():
        typer.echo(f"{run_id} is {state.phase} and has no intervention request", err=True)
        raise typer.Exit(code=1)
    request = json.loads(request_file.read_text(encoding="utf-8"))
    if as_json:
        typer.echo(json.dumps({**request, "phase": state.phase}, indent=2))
        return

    held = f" held by {state.operator_id}" if state.operator_id else ""
    typer.echo(f"run {run_id}  {state.phase}{held}")
    typer.echo(f"  capability {request.get('capability_id') or request.get('goal')}")
    typer.echo(f"  stopped at {request.get('step_id')}: {request.get('reason_code')}")
    typer.echo(f"  because    {request.get('reason_text', '')}")
    typer.echo(f"  url        {request.get('current_url', '')}")
    typer.echo(f"  expires    {request.get('expires_at', '')}")

    snapshot_file = run_dir / str(request.get("a11y_snapshot_path") or "")
    if snapshot_file.is_file():
        typer.echo("\n  what the run could see:")
        for line in _screen_lines(json.loads(snapshot_file.read_text(encoding="utf-8"))):
            typer.echo(line)

    actions_file = run_dir / "human_actions.jsonl"
    if actions_file.is_file():
        done = [json.loads(line) for line in actions_file.read_text().splitlines() if line.strip()]
        if done:
            typer.echo(f"\n  what a human has done in this window ({len(done)} actions):")
            for action in done[-8:]:
                target = action.get("target_name") or action.get("target_role") or ""
                value = f" value={action['value']}" if action.get("value") else ""
                typer.echo(f"    {action['at'][11:19]} {action['event']:9} {target[:28]}{value}")

    if request.get("suggested_actions"):
        typer.echo("\n  what you might do:")
        for hint in request["suggested_actions"]:
            typer.echo(f"    - {hint}")
    typer.echo(f"\n  {request.get('resume_command', '')}")

    shot = run_dir / str(request.get("screenshot_path") or "")
    if shot.is_file():
        typer.echo(f"  screenshot {shot}")
        if open_screenshot:
            opener = "open" if sys.platform == "darwin" else "xdg-open"
            subprocess.run([opener, str(shot)], check=False)  # noqa: S603


@ops_app.command("take-control")
def ops_take_control(
    run_id: str,
    evidence_root: EvidenceRootOption = Path("evidence/_scratch"),
    operator: OperatorOption = None,
) -> None:
    """Take the live session from automation. The run stops touching the browser and starts
    recording what you do."""
    who = _operator_id(operator)
    _, state = _session(run_id, evidence_root)
    if not _alive(state):
        typer.echo(
            f"{run_id}: the run that owned this session is gone, so nothing would answer a "
            "hand-back. Nothing to take.",
            err=True,
        )
        raise typer.Exit(code=1)
    _act_on(run_id, evidence_root, "take_control", "operator", operator=who)
    typer.echo(
        f"{who} holds run {run_id}. Work in the open browser window, then:\n"
        f"  uv run cua ops hand-back {run_id} --evidence-root {evidence_root} "
        '--note "what you did"'
    )


@ops_app.command("hand-back")
def ops_hand_back(
    run_id: str,
    note: Annotated[str, typer.Option(help="What you did and why.")] = "",
    evidence_root: EvidenceRootOption = Path("evidence/_scratch"),
    operator: OperatorOption = None,
) -> None:
    """Return control to automation. It re-runs the outcome detectors and re-verifies the step's
    checkpoint before it carries on, and never repeats the step you were paused on."""
    _act_on(
        run_id,
        evidence_root,
        "hand_back",
        "operator",
        operator=_operator_id(operator),
        note=note,
    )


@ops_app.command("abort")
def ops_abort(
    run_id: str,
    evidence_root: EvidenceRootOption = Path("evidence/_scratch"),
    operator: OperatorOption = None,
    note: Annotated[str, typer.Option(help="Why you ended it.")] = "operator aborted",
) -> None:
    """Finish a run as escalated without resuming it."""
    _act_on(run_id, evidence_root, "abort", "operator", operator=_operator_id(operator), note=note)


RootOption = Annotated[Path, typer.Option("--root", help="Catalog directory.")]


def _catalog(root: Path) -> "Catalog":
    """Catalog checked against the policy file when one exists at the default location."""
    from cua.artifact.catalog import Catalog

    return Catalog(root, POLICY_FILE if POLICY_FILE.exists() else None)


@catalog_app.command("list")
def catalog_list(root: RootOption = ARTIFACTS_DIR) -> None:
    """List saved capabilities with version and approval status."""
    entries = _catalog(root).entries()
    if not entries:
        typer.echo(f"no capabilities in {root}")
        return
    for entry in entries:
        typer.echo(f"{entry.id}@{entry.version}  {entry.status:<10} {entry.name}")


@catalog_app.command("show")
def catalog_show(
    capability_id: str,
    version: Annotated[str | None, typer.Option(help="Defaults to the latest version.")] = None,
    root: RootOption = ARTIFACTS_DIR,
) -> None:
    """Print a capability's caller-facing contract as JSON: inputs, outputs, success, status."""
    import json

    from cua.artifact.catalog import CatalogError

    try:
        capability, path = _catalog(root).get(capability_id, version, include_drafts=True)
    except CatalogError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    contract = {
        "id": capability.capability.id,
        "version": capability.capability.version,
        "status": capability.capability.status,
        "description": capability.capability.description,
        "policy": capability.capability.policy_ref,
        "inputs": [i.model_dump(mode="json", exclude_none=True) for i in capability.inputs],
        "outputs": [
            o.model_dump(mode="json", include={"name", "type", "description", "sensitive"})
            for o in capability.outputs
        ],
        "business_outcomes": sorted(
            {
                d.outcome.code
                for d in capability.outcome_detectors
                if d.outcome.type == "business_outcome"
            }
        ),
        "steps": len(capability.steps),
        "tenants": sorted(capability.tenant_overrides),
        "path": str(path),
    }
    typer.echo(json.dumps(contract, indent=2))


@catalog_app.command("approve")
def catalog_approve(
    capability_id: str,
    by: Annotated[str, typer.Option("--by", help="Who is approving. Recorded in the artifact.")],
    version: Annotated[str | None, typer.Option(help="Defaults to the latest version.")] = None,
    root: RootOption = ARTIFACTS_DIR,
) -> None:
    """Flip a draft capability to approved so unattended replay may run it."""
    from cua.artifact.catalog import CatalogError

    catalog = _catalog(root)
    try:
        capability, _ = catalog.get(capability_id, version, include_drafts=True)
        path = catalog.approve(capability_id, capability.capability.version, by)
    except CatalogError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"approved {capability_id}@{capability.capability.version} -> {path}")


@mock_app_cli.command("serve")
def mock_serve(
    host: Annotated[str, typer.Option()] = "127.0.0.1",
    port: Annotated[int | None, typer.Option(help="Defaults to CORELEDGER_PORT or 5050.")] = None,
) -> None:
    """Start CoreLedger in the foreground. Failure injection lives at /__control."""
    from mock_app.server import serve_forever
    from mock_app.settings import MissingSettingError, MockSettings

    try:
        settings = MockSettings.from_env()
    except MissingSettingError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    serve_forever(settings, host=host, port=port)


BaseUrlOption = Annotated[str, typer.Option(help="Where CoreLedger is running.")]


def _control(base_url: str, action: str, body: dict[str, object]) -> None:
    """POST to the CoreLedger control API. Replay policy denies /__control; this is the harness."""
    import json
    import urllib.error
    import urllib.request

    request = urllib.request.Request(  # noqa: S310 - a local harness URL the operator typed
        f"{base_url.rstrip('/')}/__control/api/{action}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:  # noqa: S310
            typer.echo(response.read().decode())
    except urllib.error.HTTPError as exc:
        typer.echo(f"{action} refused: {exc.read().decode()}", err=True)
        raise typer.Exit(code=1) from exc
    except urllib.error.URLError as exc:
        typer.echo(f"cannot reach {base_url}: {exc.reason}; is `cua mock serve` running?", err=True)
        raise typer.Exit(code=1) from exc


@mock_app_cli.command("inject")
def mock_inject(
    name: Annotated[str, typer.Argument(help="Injection name, e.g. interstitial or slow.")],
    times: Annotated[
        str | None,
        typer.Option(help="Uses before it clears itself, or 'none' for until cleared."),
    ] = None,
    param: Annotated[
        list[str] | None, typer.Option(help="name=value, repeatable, e.g. --param ms=5000.")
    ] = None,
    base_url: BaseUrlOption = "http://127.0.0.1:5050",
) -> None:
    """Arm a failure injection on a running CoreLedger."""
    body: dict[str, object] = {"name": name, "params": _pairs(param or [], "--param")}
    if times is not None:
        if times.lower() in ("none", "null"):
            body["times"] = None
        elif times.isdigit():
            body["times"] = int(times)
        else:
            typer.echo("--times must be a positive integer or none", err=True)
            raise typer.Exit(code=1)
    _control(base_url, "arm", body)


@mock_app_cli.command("clear")
def mock_clear(
    name: Annotated[str, typer.Argument(help="Injection to disarm.")],
    base_url: BaseUrlOption = "http://127.0.0.1:5050",
) -> None:
    """Disarm one failure injection."""
    _control(base_url, "clear", {"name": name})


@mock_app_cli.command("reset")
def mock_reset(base_url: BaseUrlOption = "http://127.0.0.1:5050") -> None:
    """Disarm every injection and drop sub-accounts opened since start."""
    _control(base_url, "reset", {})


@mock_app_cli.command("smoke")
def mock_smoke(
    base_url: Annotated[str, typer.Option()] = "http://127.0.0.1:5050",
) -> None:
    """Hit every route and every failure injection on a running CoreLedger."""
    from mock_app.settings import MissingSettingError, MockSettings
    from mock_app.smoke import run_all

    try:
        settings = MockSettings.from_env()
    except MissingSettingError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    results = run_all(base_url, settings.operator_user, settings.operator_password)
    for name, failure in results:
        typer.echo(f"ok   {name}" if failure is None else f"FAIL {name}: {failure}")
    failed = sum(1 for _, failure in results if failure is not None)
    typer.echo(f"{len(results) - failed}/{len(results)} checks passed")
    if failed:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
