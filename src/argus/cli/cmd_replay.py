from __future__ import annotations

import importlib
import sys
from pathlib import Path
from typing import Any, Callable

from rich.console import Console
from rich.rule import Rule
from rich.text import Text

from argus.cli import print_footer
from argus.models import RunRecord
from argus.replay import ReplayEngine
from argus.state_patch import PatchError, format_changes, preview_patch
from argus.storage import load_run

console = Console()


def _parse_set_value(raw: str) -> Any:
    """Parse a --set value as JSON, falling back to a plain string.

    ``retries=0`` yields the integer 0 and ``ok=true`` yields True, while
    ``query=hello`` stays a string.  Wrap in quotes to force a string:
    ``--set flag='"true"'``.
    """
    import json

    try:
        return json.loads(raw)
    except ValueError:
        return raw


def build_patch(
    patch_file: str | None,
    set_pairs: list[str] | None,
    delete_paths: list[str] | None,
) -> dict[str, Any] | None:
    """Assemble a patch document from CLI options. Returns None if empty.

    A ``--patch`` file provides the base; ``--set`` and ``--delete`` are
    layered on top, so a flag overrides the same path in the file.
    """
    import json

    patch: dict[str, Any] = {}

    if patch_file:
        path = Path(patch_file)
        if not path.exists():
            raise PatchError(f"patch file not found: {patch_file}")
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise PatchError(f"patch file {patch_file} is not valid JSON: {exc}") from exc
        if not isinstance(loaded, dict):
            raise PatchError(
                f"patch file {patch_file} must contain a JSON object, "
                f"got {type(loaded).__name__}"
            )
        patch = loaded

    for pair in set_pairs or []:
        if "=" not in pair:
            raise PatchError(
                f"invalid --set {pair!r} — expected 'path=value' "
                "(e.g. --set meta.retries=0)"
            )
        raw_path, _, raw_value = pair.partition("=")
        raw_path = raw_path.strip()
        if not raw_path:
            raise PatchError(f"invalid --set {pair!r} — the path is empty")
        patch.setdefault("set", {})
        if not isinstance(patch["set"], dict):
            raise PatchError("patch file's 'set' op must be an object to combine with --set")
        patch["set"][raw_path] = _parse_set_value(raw_value)

    for raw_path in delete_paths or []:
        existing = patch.get("delete")
        if existing is None:
            patch["delete"] = [raw_path]
        elif isinstance(existing, str):
            patch["delete"] = [existing, raw_path]
        elif isinstance(existing, list):
            existing.append(raw_path)
        else:
            raise PatchError(
                "patch file's 'delete' op must be a list to combine with --delete"
            )

    return patch or None


def _print_patch_preview(
    record: RunRecord,
    from_step: str,
    patch: dict[str, Any],
    create_missing: bool,
) -> bool:
    """Show what the patch changes. Returns False if it cannot be applied."""
    step = next((e for e in record.steps if e.node_name == from_step), None)
    if step is None:  # pragma: no cover - caller validates first
        return False

    try:
        changes = preview_patch(step.input_state, patch, create_missing=create_missing)
    except PatchError as exc:
        console.print()
        console.print(f"  [red]patch error:[/red] {exc}")
        console.print()
        return False

    console.print()
    console.print(Text("  state patch", style="bold italic"))
    console.print()
    if not changes:
        console.print("  [dim]patch is empty — nothing to change[/dim]")
    for line in format_changes(changes):
        marker, _, rest = line.partition(" ")
        style = {"+": "green", "-": "red", "~": "yellow"}.get(marker, "dim")
        console.print(f"  [bold {style}]{marker}[/bold {style}] {rest}")
    console.print()
    return True


def replay_run(
    run_id: str,
    from_step: str,
    app_module_str: str | None,
    only: bool = False,
    patch: dict[str, Any] | None = None,
    create_missing: bool = False,
    dry_run: bool = False,
) -> None:
    try:
        record = load_run(run_id)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        return

    available = [e.node_name for e in record.steps]
    if from_step not in available:
        msg = (
            f"[red]Error:[/red] node '[bold]{from_step}[/bold]' not found.\n"
            f"Available: {', '.join(available)}"
        )
        if ":" in from_step:
            msg += (
                f"\n\n  [dim]Hint:[/dim] '[bold]{from_step}[/bold]' looks like a "
                f"module:function — did you mean [bold]--app {from_step}[/bold]?"
            )
        console.print(msg)
        return

    # If run has stored node_fn_refs, no factory needed at all
    has_node_refs = bool(record.node_fn_refs)
    factory = None
    if not has_node_refs:
        effective_app = app_module_str or record.app_factory_ref
        if effective_app is None:
            console.print()
            hint = Text()
            hint.append("  argus replay ", style="dim")
            hint.append(run_id, style="italic dim")
            hint.append(f" {from_step}", style="bold")
            hint.append(" --app ", style="dim")
            hint.append("module:factory_fn", style="italic dim")
            console.print(hint)
            console.print()
            return
        factory = _import_factory(effective_app)
        if factory is None:
            return

    # ── Header ────────────────────────────────────────────────────────────
    mode_label = "single node" if only else "from"
    console.print()
    header = Text()
    header.append("argus rerun", style="bold italic")
    header.append(f"  {run_id}", style="italic dim")
    header.append(f"  ↺  {mode_label}  ", style="dim")
    header.append(from_step, style="bold")
    if only:
        header.append("  (isolated)", style="italic dim")
    if patch is not None:
        header.append("  + patch", style="italic yellow")
    console.print(f"  {header}")
    console.print()
    console.print(Rule(style="dim"))
    console.print()
    if only:
        console.print(
            f"  [dim]Re-running [bold]{from_step}[/bold] with original input state[/dim]"
        )
    else:
        console.print(
            f"  [dim]Re-running from [bold]{from_step}[/bold] "
            f"— upstream outputs frozen from [bold]{run_id}[/bold][/dim]"
        )
    # ── State patch preview ───────────────────────────────────────────────
    if patch is not None:
        if not _print_patch_preview(record, from_step, patch, create_missing):
            return
        if dry_run:
            console.print("  [dim]dry run — nothing was executed[/dim]")
            console.print()
            print_footer()
            return
    elif dry_run:
        console.print("  [dim]dry run — no patch given, nothing to preview[/dim]")
        console.print()
        print_footer()
        return

    # Warn about non-deterministic external calls
    console.print()
    console.print(
        "  [yellow]note:[/yellow] [dim]external API calls, DB reads, and timestamps "
        "execute live — results may differ from the original run. "
        "Use [bold]record_http=True[/bold] during recording for deterministic reruns.[/dim]"
    )
    console.print()

    # ── Run replay ────────────────────────────────────────────────────────
    engine = ReplayEngine()
    try:
        if only:
            new_run_id = engine.replay_node(
                run_id=run_id,
                node_name=from_step,
                patch=patch,
                create_missing=create_missing,
            )
        else:
            new_run_id = engine.replay(
                run_id=run_id,
                from_node=from_step,
                app_factory=factory,
                patch=patch,
                create_missing=create_missing,
            )
    except Exception as e:
        console.print(f"[red]Replay failed:[/red] {e}")
        return

    if not new_run_id:
        console.print("[yellow]Warning:[/yellow] Could not locate the new replay run.")
        return
    new_record = load_run(new_run_id)
    name_col = max(len(s.node_name) for s in new_record.steps) + 2

    for step in new_record.steps:
        number = str(step.step_index + 1)
        pad = " " * (name_col - len(step.node_name))
        dur = f"[italic dim]{step.duration_ms:.0f} ms[/italic dim]"

        if step.status == "pass":
            name = f"[bold]{step.node_name}[/bold]"
            icon = "[bold green]✓[/bold green]"
            label = "[bold green]pass[/bold green]"
        elif step.status == "fail":
            name = f"[bold]{step.node_name}[/bold]"
            icon = "[bold yellow]⚠[/bold yellow]"
            label = "[bold yellow]silent failure[/bold yellow]"
        elif step.status == "semantic_fail":
            name = f"[bold]{step.node_name}[/bold]"
            icon = "[bold magenta]⊗[/bold magenta]"
            label = "[bold magenta]semantic fail[/bold magenta]"
        elif step.status == "interrupted":
            name = f"[bold]{step.node_name}[/bold]"
            icon = "[bold yellow]⏸[/bold yellow]"
            label = "[bold yellow]interrupted[/bold yellow]"
        elif step.status == "degraded_input":
            name = f"[bold]{step.node_name}[/bold]"
            icon = "[bold yellow]⚠[/bold yellow]"
            label = "[bold yellow]degraded_input[/bold yellow]"
        elif step.status == "skipped":
            name = f"[bold]{step.node_name}[/bold]"
            icon = "[dim]⊘[/dim]"
            label = "[dim]skipped[/dim]"
        elif step.status == "retried":
            name = f"[bold]{step.node_name}[/bold]"
            icon = "[dim]↻[/dim]"
            label = "[dim]retried[/dim]"
        elif step.status == "crashed":
            name = f"[bold red]{step.node_name}[/bold red]"
            icon = "[bold red]✗[/bold red]"
            label = "[bold red]crashed[/bold red]"
        else:
            name = f"[bold]{step.node_name}[/bold]"
            icon = "[dim]?[/dim]"
            label = f"[dim]{step.status}[/dim]"

        console.print(f"  [dim]{number:>2}[/dim]  {name}{pad}  {dur}   {icon}  {label}")

    console.print()
    console.print(Rule(style="dim"))
    console.print()

    if new_record.overall_status == "clean":
        result = Text.from_markup("  [bold green]●[/bold green]  ")
        result.append("clean", style="bold green")
        result.append(f"    {new_run_id}", style="dim")
    else:
        result = Text.from_markup("  [bold red]●[/bold red]  ")
        result.append(new_record.overall_status, style="bold red")
        result.append(f"    {new_run_id}", style="dim")

    console.print(result)

    from argus.correlator import compare_replay

    impact = compare_replay(new_record, record)
    if impact.original_failure_node:
        console.print()
        if impact.original_failure_resolved:
            console.print(
                f"  original failure  [bold]{impact.original_failure_node}[/bold]"
                f"  [bold green]RESOLVED[/bold green]"
            )
        else:
            console.print(
                f"  original failure  [bold]{impact.original_failure_node}[/bold]"
                f"  [bold red]NOT RESOLVED[/bold red]"
            )

    # ── Auto-diff against original ────────────────────────────────────────
    console.print()
    console.print(Rule(style="dim"))
    _print_inline_diff(record, new_record, only=only)
    console.print()
    print_footer()


def _print_inline_diff(
    original: RunRecord,
    replay: RunRecord,
    only: bool = False,
) -> None:
    """Print a compact inline diff comparing original vs replay."""
    from argus.cli.cmd_diff import (
        _build_node_map,
        _diff_inspection,
        _diff_output,
        _diff_validators,
    )

    orig_map = _build_node_map(original)
    replay_map = _build_node_map(replay)

    # Only compare nodes that exist in both runs
    common = set(orig_map) & set(replay_map)
    if not common:
        console.print("  [dim]no comparable nodes between runs[/dim]")
        console.print()
        return

    console.print()
    console.print(Text("  rerun diff", style="bold italic"))
    console.print()

    fixed = 0
    regressed = 0
    changed = 0

    for name in (s.node_name for s in original.steps if s.node_name in common):
        if name not in common:
            continue
        common.discard(name)  # process each only once

        b = orig_map[name]
        a = replay_map[name]

        status_changed = b.status != a.status
        field_diffs = _diff_output(b.output_dict, a.output_dict)
        insp_diffs = _diff_inspection(b.inspection, a.inspection)
        val_diffs = _diff_validators(b.validator_results, a.validator_results)
        has_changes = status_changed or field_diffs or insp_diffs or val_diffs

        if not has_changes:
            console.print(f"  [bold]{name}[/bold]  [dim]unchanged[/dim]")
            continue

        changed += 1

        if status_changed:
            b_bad = b.status in ("crashed", "fail", "semantic_fail")
            a_good = a.status == "pass"
            a_bad = a.status in ("crashed", "fail", "semantic_fail")

            if b_bad and a_good:
                fixed += 1
                console.print(
                    f"  [bold]{name}[/bold]  "
                    f"[dim]{b.status}[/dim] → [bold green]{a.status}[/bold green]"
                    f"  [bold green]FIXED[/bold green]"
                )
            elif b.status == "pass" and a_bad:
                regressed += 1
                console.print(
                    f"  [bold]{name}[/bold]  "
                    f"[dim]{b.status}[/dim] → [bold red]{a.status}[/bold red]"
                    f"  [bold red]REGRESSION[/bold red]"
                )
            else:
                console.print(
                    f"  [bold]{name}[/bold]  [dim]{b.status}[/dim] → [dim]{a.status}[/dim]"
                )
        else:
            console.print(f"  [bold]{name}[/bold]  [dim]{a.status}[/dim]")

        for line in field_diffs + insp_diffs + val_diffs:
            console.print(f"     [dim]└─[/dim]  {line}")

    console.print()

    # Summary line
    parts: list[str] = []
    if fixed:
        parts.append(f"[bold green]{fixed} fixed[/bold green]")
    if regressed:
        parts.append(f"[bold red]{regressed} regressed[/bold red]")
    if changed and not fixed and not regressed:
        parts.append(f"[bold]{changed} changed[/bold]")
    if not parts:
        parts.append("[dim]no changes[/dim]")
    console.print("  " + "  ·  ".join(parts))
    console.print()


def _import_factory(spec: str) -> Callable[[], Any] | None:
    if ":" not in spec:
        console.print(f"[red]Error:[/red] --app must be 'module:function'. Got: '{spec}'")
        return None

    module_path, fn_name = spec.rsplit(":", 1)
    cwd = str(Path.cwd())
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        console.print(f"[red]Error:[/red] Cannot import '{module_path}': {e}")
        return None

    fn = getattr(module, fn_name, None)
    if fn is None or not callable(fn):
        console.print(
            f"[red]Error:[/red] '{fn_name}' not found or not callable in '{module_path}'"
        )
        return None

    return fn


def inspect_step(run_id: str, step_name: str) -> None:
    import json

    try:
        record = load_run(run_id)
    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        return

    event = next((e for e in record.steps if e.node_name == step_name), None)
    if event is None:
        console.print(
            f"[red]Error:[/red] step '{step_name}' not found.\n"
            f"Available: {', '.join(e.node_name for e in record.steps)}"
        )
        return

    status_style = {
        "pass": "green",
        "fail": "yellow",
        "crashed": "red",
        "semantic_fail": "magenta",
        "degraded_input": "yellow",
        "interrupted": "yellow",
        "skipped": "dim",
        "retried": "dim",
    }.get(event.status, "dim")
    console.print()
    hdr = Text()
    hdr.append(f"  {step_name}", style="bold")
    hdr.append(f"  #{event.step_index + 1}", style="dim")
    hdr.append("  ·  ", style="dim")
    hdr.append(event.status, style=f"bold {status_style}")
    console.print(hdr)
    console.print()
    console.print(Rule(style="dim"))
    console.print()
    console.print("  [dim]input[/dim]")
    console.print()
    console.print_json(json.dumps(event.input_state, default=str, indent=2))
    console.print()
    console.print(Rule(style="dim"))
    console.print()
    console.print("  [dim]output[/dim]")
    console.print()
    if event.output_dict is not None:
        console.print_json(json.dumps(event.output_dict, default=str, indent=2))
    else:
        console.print("  [dim]no output — node crashed[/dim]")
    if event.inspection and event.inspection.severity != "ok":
        console.print()
        console.print(Rule(style="dim"))
        console.print()
        console.print("  [dim]inspection[/dim]")
        console.print()
        console.print(f"  [italic dim]{event.inspection.message}[/italic dim]")
    console.print()
    print_footer()
