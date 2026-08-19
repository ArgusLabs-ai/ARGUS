from __future__ import annotations

from typing import Annotated, Optional

import typer
from rich.console import Console
from rich.text import Text

from argus.cli.cmd_diff import diff_runs
from argus.cli.cmd_doctor import doctor
from argus.cli.cmd_fix import fix_run
from argus.cli.cmd_locate import locate_sources
from argus.cli.cmd_login import login, logout, whoami
from argus.cli.cmd_open_ui import open_ui
from argus.cli.cmd_replay import inspect_step, replay_run
from argus.cli.cmd_show import show_last, show_list, show_run
from argus.cli.cmd_stats import stats
from argus.cli.cmd_update import check_for_update

app = typer.Typer(
    name="argus",
    no_args_is_help=False,
    add_completion=False,
    context_settings={"help_option_names": ["--help", "-h"]},
)

show_app = typer.Typer(help="Show run details.", no_args_is_help=True)
app.add_typer(show_app, name="show")

open_app = typer.Typer(help="Open Argus tools.", no_args_is_help=True)
app.add_typer(open_app, name="open")

_console = Console()

_WORDMARK = [
    "┌─┐ ┬─┐ ┌─┐ ┬ ┬ ┌─┐",
    "├─┤ ├┬┘ │ ┬ │ │ └─┐",
    "┴ ┴ ┴└─ └─┘ └─┘ └─┘",
]

_SETUP_LINES = [
    ("from argus import ArgusWatcher", ""),
    ("watcher = ArgusWatcher()", ""),
    ("watcher.watch(graph)", "# graph = your StateGraph, before .compile()"),
    ("app = graph.compile()", ""),
    ("app.invoke(initial_state)", ""),
    ("watcher.finalize()", "# persists the run to .argus/runs/"),
]

_COMMANDS = [
    ("ui", "start the web dashboard and open it in browser"),
    ("list", "list all recorded runs, newest first"),
    ("show last", "inspect the most recent run"),
    ("show run <id>", "inspect a specific run  (full id or 8-char prefix)"),
    ("replay <id> <node>", "re-run from a saved node checkpoint"),
    ("replay <id> <node> --only", "re-run just that node in isolation"),
    ("replay <id> <node> --set k=v", "edit the state, then resume from that node"),
    ("replay <id> <node> --app mod:fn", "replay with a live graph factory"),
    ("inspect <id> --step <node>", "dump raw input / output state for a node"),
    ("diff <id>", "diff a replay run against its original"),
    ("diff <id-a> <id-b>", "diff any two runs side-by-side"),
    ("fix <id>", "print a fix prompt for the root cause, ready to paste"),
    ("login", "sign in with Google to sync runs to the cloud"),
    ("logout", "clear stored credentials"),
    ("whoami", "show current login status"),
    ("update", "check GitHub for a newer release and upgrade"),
    ("doctor", "diagnose integration issues (LangGraph, storage)"),
]

_WHEN_TO_USE = [
    ("list", "after a run — get the run id for further commands"),
    ("show", "understand what happened: statuses, warnings, root cause"),
    ("replay", "re-run from a broken node after fixing the code (warns about live calls)"),
    ("replay --set", "fix a bad value in the saved state and resume — no code change"),
    ("inspect", "read exact input/output JSON for a specific step"),
    ("fix", "hand the root cause to a coding agent as a ready-made prompt"),
    ("diff", "verify a fix actually changed behaviour between runs"),
]

_OPTIONS = [
    (
        "replay  --app  module.path:fn",
        "str",
        "zero-arg callable returning StateGraph or CompiledGraph",
    ),
    ("replay  --set  path=value", "str", "patch a state value before replaying (repeatable)"),
    ("replay  --delete  path", "str", "drop a state field before replaying (repeatable)"),
    ("replay  --patch  file.json", "str", "state patch document to apply before replaying"),
    ("replay  --dry-run", "flag", "show what the patch changes without executing"),
    ("replay  --create-missing", "flag", "let the patch add keys that don't exist yet"),
    ("inspect --step / -s  <node>", "str", "node name to inspect  (required)"),
    ("show run <id>", "str", "full run id or 8-char prefix"),
    (
        "diff <id-a> <id-b>",
        "str",
        "two run ids; omit second to auto-diff vs original",
    ),
]

_STATUSES = [
    ("[bold green]✓[/bold green]  pass", "node completed, output looks healthy"),
    (
        "[bold yellow]⚠[/bold yellow]  silent failure",
        "node ran but returned empty / missing fields",
    ),
    ("[bold magenta]⊗[/bold magenta]  semantic fail", "validator rejected the output"),
    ("[bold red]✗[/bold red]  crashed", "node raised an exception"),
    ("[bold yellow]⏸[/bold yellow]  interrupted", "human-in-the-loop pause"),
]


@app.callback(invoke_without_command=True)
def _banner(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is not None:
        return

    _console.print()

    tagline = "agentic realtime guard & unified scope"
    pad = " " * 4

    for i, line in enumerate(_WORDMARK):
        if i == 1:
            suffix = Text(f"{pad}{tagline}", style="dim")
        else:
            suffix = Text()
        _console.print(f"  {line}", style="bold", end="")
        _console.print(suffix)

    _console.print()
    _console.print("  [dim]─────────────────────────────────────────────────────────[/dim]")
    _console.print()

    # ── Setup ──────────────────────────────────────────────────────────────
    _console.print("  [dim]setup[/dim]")
    _console.print()
    code_w = max(len(code) for code, _ in _SETUP_LINES)
    for code, comment in _SETUP_LINES:
        row = Text()
        row.append(f"    {code:<{code_w}}  ", style="")
        if comment:
            row.append(comment, style="dim")
        _console.print(row)

    _console.print()
    _console.print("  [dim]─────────────────────────────────────────────────────────[/dim]")
    _console.print()

    # ── Commands ────────────────────────────────────────────────────────────
    _console.print("  [dim]commands[/dim]")
    _console.print()
    cmd_w = max(len(cmd) for cmd, _ in _COMMANDS)
    for cmd, desc in _COMMANDS:
        row = Text()
        row.append(f"  argus {cmd:<{cmd_w}}  ", style="bold")
        row.append(desc, style="dim")
        _console.print(row)

    _console.print()
    _console.print("  [dim]─────────────────────────────────────────────────────────[/dim]")
    _console.print()

    # ── When to use ─────────────────────────────────────────────────────────
    _console.print("  [dim]when to use[/dim]")
    _console.print()
    wtu_w = max(len(cmd) for cmd, _ in _WHEN_TO_USE)
    for cmd, desc in _WHEN_TO_USE:
        row = Text()
        row.append(f"  {cmd:<{wtu_w}}  ", style="bold")
        row.append(desc, style="dim")
        _console.print(row)

    _console.print()
    _console.print("  [dim]─────────────────────────────────────────────────────────[/dim]")
    _console.print()

    # ── Options ─────────────────────────────────────────────────────────────
    _console.print("  [dim]options[/dim]")
    _console.print()
    opt_w = max(len(opt) for opt, _, _ in _OPTIONS)
    for opt, typ, desc in _OPTIONS:
        row = Text()
        row.append(f"  {opt:<{opt_w}}  ", style="bold")
        row.append(f"[{typ}]  ", style="italic dim")
        row.append(desc, style="dim")
        _console.print(row)

    _console.print()
    _console.print("  [dim]─────────────────────────────────────────────────────────[/dim]")
    _console.print()

    # ── Node statuses ────────────────────────────────────────────────────────
    _console.print("  [dim]node statuses[/dim]")
    _console.print()
    for icon_label, desc in _STATUSES:
        row = Text.from_markup(f"  {icon_label:<26}  ")
        row.append(desc, style="dim")
        _console.print(row)

    _console.print()
    _console.print("  [dim]─────────────────────────────────────────────────────────[/dim]")
    _console.print()
    _console.print(
        "  [dim]run  [bold]argus <command> --help[/bold]  for per-command flag details[/dim]"
    )
    _console.print()


@show_app.command("last")
def cmd_show_last() -> None:
    """Show the most recent run."""
    show_last()


@show_app.command("run")
def cmd_show_run(
    run_id: Annotated[str, typer.Argument(help="Run ID or 8-char prefix.")],
) -> None:
    """Show details for a specific run."""
    show_run(run_id)


@app.command("list")
def cmd_list() -> None:
    """List all recorded runs in reverse chronological order."""
    show_list()


@app.command("replay")
def cmd_replay(
    run_id: Annotated[str, typer.Argument(help="Run ID or 8-char prefix to replay.")],
    from_step: Annotated[str, typer.Argument(help="Node name to replay from.")],
    app: Annotated[
        Optional[str],
        typer.Option(
            help="'module.path:factory_fn' — zero-arg callable returning a StateGraph.",
        ),
    ] = None,
    only: Annotated[
        bool,
        typer.Option(
            "--only",
            help="Re-run only the specified node in isolation (skip downstream).",
        ),
    ] = False,
    patch: Annotated[
        Optional[str],
        typer.Option(
            "--patch",
            help="JSON file with a state patch to apply before replaying.",
        ),
    ] = None,
    set_: Annotated[
        Optional[list[str]],
        typer.Option(
            "--set",
            help="Patch a value: 'path=value' (repeatable). e.g. --set meta.retries=0",
        ),
    ] = None,
    delete: Annotated[
        Optional[list[str]],
        typer.Option(
            "--delete",
            help="Remove a field before replaying (repeatable). e.g. --delete docs",
        ),
    ] = None,
    create_missing: Annotated[
        bool,
        typer.Option(
            "--create-missing",
            help="Allow the patch to add keys that don't exist yet.",
        ),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Show what the patch changes without running anything.",
        ),
    ] = False,
) -> None:
    """Re-run a pipeline from a saved step, optionally patching its state first."""
    from argus.cli.cmd_replay import build_patch
    from argus.state_patch import PatchError

    try:
        patch_doc = build_patch(patch, set_, delete)
    except PatchError as exc:
        _console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(code=1) from None

    replay_run(
        run_id=run_id,
        from_step=from_step,
        app_module_str=app,
        only=only,
        patch=patch_doc,
        create_missing=create_missing,
        dry_run=dry_run,
    )


@app.command("inspect")
def cmd_inspect(
    run_id: Annotated[str, typer.Argument(help="Run ID or 8-char prefix.")],
    step: Annotated[str, typer.Option("--step", "-s", help="Node name to inspect.")],
) -> None:
    """Dump full input/output state snapshot for a specific step."""
    inspect_step(run_id=run_id, step_name=step)


@app.command("locate")
def cmd_locate(
    run_id: Annotated[str, typer.Argument(help="Run ID or 8-char prefix.")],
    no_save: Annotated[
        bool,
        typer.Option("--no-save", help="Display results without saving to the run record."),
    ] = False,
) -> None:
    """Auto-locate source files for all nodes in a run."""
    locate_sources(run_id, save=not no_save)


@app.command("ui")
def cmd_ui(
    app: Annotated[
        Optional[str],
        typer.Option(
            help="'module.path:factory_fn' — zero-arg callable returning a StateGraph. Enables replay from the UI.",  # noqa: E501
        ),
    ] = None,
) -> None:
    """Start the web dashboard and open it in the browser."""
    open_ui(app_module_str=app)


@open_app.command("ui")
def cmd_open_ui(
    app: Annotated[
        Optional[str],
        typer.Option(
            help="'module.path:factory_fn' — zero-arg callable returning a StateGraph. Enables replay from the UI.",  # noqa: E501
        ),
    ] = None,
) -> None:
    """Start the web dashboard and open it in the browser."""
    open_ui(app_module_str=app)


@app.command("diff")
def cmd_diff(
    run_id_a: Annotated[str, typer.Argument(help="Run ID or replay run ID.")],
    run_id_b: Optional[str] = typer.Argument(
        default=None, help="Second run ID. Omit for auto-diff."
    ),
) -> None:
    """Compare two runs node-by-node: status, duration, and output field changes."""
    diff_runs(run_id_a, run_id_b)


@app.command("fix")
def cmd_fix(
    run_id: Annotated[str, typer.Argument(help="Run ID or 8-char prefix.")],
    node: Annotated[
        Optional[str],
        typer.Option("--node", help="Target a specific node instead of the root cause."),
    ] = None,
    output: Annotated[
        Optional[str],
        typer.Option("--output", "-o", help="Write the prompt to a file instead of stdout."),
    ] = None,
    sanitized: Annotated[
        bool,
        typer.Option("--sanitized", help="Strip recorded values, keep field names and shapes."),
    ] = False,
) -> None:
    """Print a ready-to-paste fix prompt for the run's root-cause failure."""
    fix_run(run_id, node=node, output=output, sanitized=sanitized)


@app.command("login")
def cmd_login() -> None:
    """Sign in with Google to sync runs to the cloud dashboard."""
    login()


@app.command("logout")
def cmd_logout() -> None:
    """Clear stored cloud credentials."""
    logout()


@app.command("whoami")
def cmd_whoami() -> None:
    """Show current cloud login status."""
    whoami()


@app.command("update")
def cmd_update() -> None:
    """Check GitHub for a newer release and upgrade if one is available."""
    check_for_update()


@app.command("doctor")
def cmd_doctor() -> None:
    """Diagnose integration issues: LangGraph version, storage, replay readiness."""
    doctor()


@app.command("stats")
def cmd_stats(
    all_sigs: Annotated[
        bool,
        typer.Option("--all", "-a", help="Include builtin signatures"),
    ] = False,
    sig: Annotated[
        Optional[str],
        typer.Option("--sig", "-s", help="Show stats for a specific signature ID"),
    ] = None,
    disable: Annotated[
        Optional[str],
        typer.Option("--disable", help="Disable a custom signature by ID"),
    ] = None,
    enable: Annotated[
        Optional[str],
        typer.Option("--enable", help="Re-enable a disabled custom signature"),
    ] = None,
    dispute: Annotated[
        Optional[str],
        typer.Option("--dispute", help="Flag a signature hit as false positive"),
    ] = None,
    run_id: Annotated[
        Optional[str],
        typer.Option("--run", help="Run ID for dispute context"),
    ] = None,
    prune: Annotated[
        bool,
        typer.Option("--prune", help="Remove stale signatures that haven't proven useful"),
    ] = False,
) -> None:
    """Show signature effectiveness stats and manage learned patterns."""
    stats(
        all_sigs=all_sigs,
        sig=sig,
        disable=disable,
        enable=enable,
        dispute=dispute,
        run_id=run_id,
        prune=prune,
    )
