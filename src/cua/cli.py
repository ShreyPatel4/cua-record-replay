"""Command line entry point: discover, replay, ops, catalog, and mock subcommands.

Exit codes: 0 success or business outcome, 1 usage or internal error, 2 hard failure, 3 escalated.
"""

from pathlib import Path
from typing import Annotated, NoReturn

import typer
from dotenv import load_dotenv

from cua.log import configure_logging

ARTIFACTS_DIR = Path("artifacts")

app = typer.Typer(no_args_is_help=True, add_completion=False, help=__doc__)
ops_app = typer.Typer(no_args_is_help=True, help="Operator commands for paused runs.")
catalog_app = typer.Typer(no_args_is_help=True, help="Browse and approve saved capabilities.")
mock_app_cli = typer.Typer(no_args_is_help=True, help="Run the local CoreLedger mock app.")
app.add_typer(ops_app, name="ops")
app.add_typer(catalog_app, name="catalog")
app.add_typer(mock_app_cli, name="mock")


def _not_yet(command: str, phase: int) -> NoReturn:
    typer.echo(f"cua {command}: not implemented yet (lands in phase {phase})", err=True)
    raise typer.Exit(code=1)


@app.callback()
def main(
    log_level: Annotated[str, typer.Option(help="DEBUG, INFO, WARNING, ERROR.")] = "INFO",
) -> None:
    load_dotenv(override=False)
    configure_logging(log_level)


@app.command()
def discover(
    goal: Annotated[str, typer.Option(help="Natural language goal.")],
    target: Annotated[str, typer.Option(help="Entry URL of the target application.")],
    policy: Annotated[str, typer.Option(help="Policy id from policy/allowlist.yaml.")] = (
        "coreledger-readonly"
    ),
    max_steps: Annotated[int, typer.Option(min=1)] = 25,
    timeout_s: Annotated[int, typer.Option(min=1)] = 180,
    allow_irreversible: Annotated[bool, typer.Option()] = False,
    headed: Annotated[bool, typer.Option()] = False,
) -> None:
    """Run the LLM agent on a goal and record the successful run as a draft capability."""
    _not_yet("discover", 3)


@app.command()
def replay(
    artifact: Annotated[Path, typer.Argument(help="Capability JSON file or catalog id.")],
    inputs: Annotated[
        list[str] | None, typer.Option("--input", "-i", help="name=value, repeatable.")
    ] = None,
    repeat: Annotated[int, typer.Option(min=1, help="Run N times and report stability.")] = 1,
    allow_draft: Annotated[bool, typer.Option()] = False,
    confirm_irreversible: Annotated[bool, typer.Option()] = False,
    headed: Annotated[bool, typer.Option()] = False,
) -> None:
    """Replay a capability deterministically, with no model in the loop."""
    _not_yet("replay", 4)


@ops_app.command("list")
def ops_list() -> None:
    """Show runs paused for a human."""
    _not_yet("ops list", 5)


@ops_app.command("show")
def ops_show(run_id: str) -> None:
    """Print an intervention request and open its screenshot."""
    _not_yet("ops show", 5)


@ops_app.command("take-control")
def ops_take_control(run_id: str) -> None:
    """Take the live session from automation and start capturing human actions."""
    _not_yet("ops take-control", 5)


@ops_app.command("hand-back")
def ops_hand_back(
    run_id: str, note: Annotated[str, typer.Option(help="What you did and why.")] = ""
) -> None:
    """Return control to automation; it re-verifies the checkpoint before continuing."""
    _not_yet("ops hand-back", 5)


@ops_app.command("abort")
def ops_abort(run_id: str) -> None:
    """Finish a paused run as escalated with reason 'operator aborted'."""
    _not_yet("ops abort", 5)


RootOption = Annotated[Path, typer.Option("--root", help="Catalog directory.")]


@catalog_app.command("list")
def catalog_list(root: RootOption = ARTIFACTS_DIR) -> None:
    """List saved capabilities with version and approval status."""
    from cua.artifact.catalog import Catalog

    entries = Catalog(root).entries()
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

    from cua.artifact.catalog import Catalog, CatalogError

    try:
        capability, path = Catalog(root).get(capability_id, version)
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
    from cua.artifact.catalog import Catalog, CatalogError

    catalog = Catalog(root)
    try:
        capability, _ = catalog.get(capability_id, version)
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
