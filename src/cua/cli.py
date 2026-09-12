"""Command line entry point: discover, replay, ops, catalog, and mock subcommands.

Exit codes: 0 success or business outcome, 1 usage or internal error, 2 hard failure, 3 escalated.
"""

from pathlib import Path
from typing import Annotated, NoReturn

import typer
from dotenv import load_dotenv

from cua.log import configure_logging

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


@catalog_app.command("list")
def catalog_list() -> None:
    """List saved capabilities with version and approval status."""
    _not_yet("catalog list", 1)


@catalog_app.command("show")
def catalog_show(capability_id: str) -> None:
    """Print a capability's contract: inputs, outputs, steps, success condition."""
    _not_yet("catalog show", 1)


@catalog_app.command("approve")
def catalog_approve(capability_id: str) -> None:
    """Flip a draft capability to approved so unattended replay may run it."""
    _not_yet("catalog approve", 1)


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
