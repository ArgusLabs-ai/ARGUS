"""``argus behavior`` — validate, list, show, and judge behavior contracts.

The judge is a reviewer, never a gate: ``argus behavior judge`` writes its
verdicts onto the run record (read-modify-write, the ``argus locate``
precedent) and prints a fold summary, but the exit code only reflects
whether the command itself worked. A failed behavior does not fail CI —
that call belongs to the deterministic layers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.markup import escape

from argus.behaviors import default_behaviors_dir, load_behavior_specs, validate_behaviors
from argus.models import BehaviorVerdict, fold_behavior_verdicts

console = Console()

_FOLD_LABELS = {
    BehaviorVerdict.TRUE: "pass",
    BehaviorVerdict.FALSE: "fail",
    BehaviorVerdict.NA: "na",
}


def behavior_validate(path: Optional[Path]) -> int:
    """Validate spec files; returns 0 when clean, 2 when any problem exists.

    ``path`` is a directory or one spec file. With no ``path`` the default
    directory is checked, and its absence is fine (specs are opt-in); a
    ``path`` that does not exist is an error, so a typo is not an "OK".

    Exit 2 (not 1) matches the repo's usage-error convention — a malformed
    spec is a problem with what the user pointed at, not a failing check on
    the run — so CI can tell "contract violated" apart from "contract
    unreadable" without parsing output.
    """
    target = Path(path) if path else default_behaviors_dir()
    diagnostics = validate_behaviors(target) if path or target.exists() else []
    errors = [d for d in diagnostics if d.severity == "error"]
    warnings = [d for d in diagnostics if d.severity == "warning"]
    for d in diagnostics:
        loc = f"{d.path}:{d.line}" if d.line else str(d.path)
        style = "red" if d.severity == "error" else "yellow"
        console.print(
            f"  [{style}]{d.severity}[/{style}]  {d.code}  {escape(loc)}  {escape(d.message)}"
        )
    if errors:
        console.print(f"\n  [red]{len(errors)} error(s)[/red], {len(warnings)} warning(s)")
        return 2
    if warnings:
        console.print(f"\n  [yellow]{len(warnings)} warning(s)[/yellow], no errors")
        return 0
    console.print(f"  [green]OK[/green]  no problems in {escape(str(target))}")
    return 0


def behavior_list() -> int:
    """Print every spec: name, clause count, description."""
    specs, diagnostics = load_behavior_specs(default_behaviors_dir())
    for d in diagnostics:
        if d.severity == "error":
            console.print(
                f"  [red]error[/red]  {d.code}  {escape(f'{d.path}:{d.line}')}  "
                f"{escape(d.message)}"
            )
    if not specs:
        console.print("  [yellow]No behavior specs found.[/yellow]")
        console.print(
            f"  [dim]Write one at {escape(str(default_behaviors_dir()))}/<name>/BEHAVIOR.md[/dim]"
        )
        return 0
    name_w = max(len(s.name) for s in specs)
    for spec in specs:
        n = len(spec.clauses)
        plural = "s" if n != 1 else ""
        console.print(
            f"  [bold]{escape(f'{spec.name:<{name_w}}')}[/bold]  {n} clause{plural}  "
            f"[dim]{escape(spec.description)}[/dim]"
        )
    return 0


def behavior_show(name: str) -> int:
    """Pretty-print one spec with its clauses."""
    specs, _ = load_behavior_specs(default_behaviors_dir())
    for spec in specs:
        if spec.name != name:
            continue
        console.print(f"[bold]{escape(spec.name)}[/bold]  [dim]{escape(str(spec.path))}[/dim]")
        console.print(f"  {escape(spec.description)}")
        for i, clause in enumerate(spec.clauses, 1):
            console.print(f"\n  [bold]{i}. {escape(clause.name)}[/bold]")
            body = clause.text.split("\n", 1)
            if len(body) == 2 and body[1].strip():
                console.print(f"     {escape(body[1].strip())}")
        return 0
    console.print(f"  [red]Error:[/red] no behavior spec named '{escape(name)}'.")
    return 1


def behavior_judge(run_id: str, behavior: Optional[str], model: str) -> int:
    """Judge a run against the specs and append the verdicts to the run file.

    The fold summary (pass/fail/na per behavior) is the deliverable; the
    per-clause judgments persist on the record for audit. Unevaluated
    clauses are reported as "unjudged" so an offline run never prints a
    misleading all-NA summary as if the judge had spoken.
    """
    from argus.behavior_judge import judge_run_behaviors
    from argus.storage import load_run, save_run

    try:
        record = load_run(run_id)
    except FileNotFoundError as e:
        console.print(f"  [red]Error:[/red] {escape(str(e))}")
        return 1
    except ValueError as e:
        console.print(f"  [red]Error:[/red] {escape(str(e))}")
        return 1

    specs, diagnostics = load_behavior_specs(default_behaviors_dir())
    for d in diagnostics:
        if d.severity == "error":
            console.print(f"  [yellow]skipping malformed spec:[/yellow] {escape(d.message)}")
    if behavior is not None:
        specs = [s for s in specs if s.name == behavior]
        if not specs:
            console.print(f"  [red]Error:[/red] no behavior spec named '{escape(behavior)}'.")
            return 1
    if not specs:
        console.print("  [yellow]No behavior specs to judge.[/yellow]")
        return 0

    judgments = judge_run_behaviors(record, specs, model=model)

    # A re-run that could not judge a clause (no key, a transient 429, an
    # uncitable FALSE) must not erase a verdict an earlier run already earned:
    # per clause, the stored evaluated judgment stands in for an unjudged one.
    earned = {(j.behavior, j.clause): j for j in record.behavior_judgments if j.evaluated}
    kept: dict[str, int] = {}
    merged = []
    for j in judgments:
        old = earned.get((j.behavior, j.clause))
        if old is not None and not j.evaluated:
            merged.append(old)
            kept[j.behavior] = kept.get(j.behavior, 0) + 1
        else:
            merged.append(j)

    # Read-modify-write, the `argus locate` precedent: append to what the
    # record already carries (prior judgments from an earlier run of this
    # command are replaced for the judged behaviors, others are kept).
    judged_names = {s.name for s in specs}
    record.behavior_judgments = [
        j for j in record.behavior_judgments if j.behavior not in judged_names
    ]
    record.behavior_judgments.extend(merged)
    path = save_run(record)

    by_behavior: dict[str, list] = {}
    for j in merged:
        by_behavior.setdefault(j.behavior, []).append(j)
    for name in (s.name for s in specs):
        js = by_behavior.get(name, [])
        unevaluated = sum(1 for j in js if not j.evaluated)
        fold = fold_behavior_verdicts(js)
        style = {"pass": "green", "fail": "red", "na": "yellow"}[_FOLD_LABELS[fold]]
        line = f"  [{style}]{_FOLD_LABELS[fold]:<4}[/{style}]  {escape(name)}"
        if unevaluated:
            line += f"  [dim]({unevaluated} clause(s) unjudged)[/dim]"
        if kept.get(name):
            line += f"  [dim]({kept[name]} clause(s) kept from an earlier judgment)[/dim]"
        console.print(line)
    console.print(f"\n  [green]Saved to {escape(str(path))}[/green]")
    return 0
