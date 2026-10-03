"""``argus baseline`` — record what healthy runs look like, per node."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from argus.storage import load_run
from argus.trace_rules import build_baseline

console = Console()


def baseline_for_runs(
    run_ids: list[str], write: Path | None = None, *, purposes: bool = False
) -> dict[str, Any]:
    """Keys, types and leaf kinds every given run agrees on. Optionally write it.

    Grading uses it only when passed as ``ArgusRecorder(baseline=...)``. It holds
    kinds (``text``, ``nonneg``), never values. ``purposes`` adds a drafted
    sentence per node (an LLM call per run) for the run reviewer.
    """
    records = []
    runs = []
    for run_id in run_ids:
        try:
            record = load_run(run_id)
        except (FileNotFoundError, ValueError) as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(1) from exc
        if record.overall_status != "clean":
            console.print(
                f"[red]Error:[/red] run {record.run_id} is {record.overall_status}; "
                "a baseline must come from healthy runs"
            )
            raise typer.Exit(1)
        records.append(record)
        runs.append(record.steps)
    baseline = build_baseline(runs)
    if purposes:
        from argus.review import draft_purposes  # noqa: PLC0415

        try:
            baseline["purposes"] = draft_purposes(records)
        except Exception as exc:  # noqa: BLE001 — name the cause, keep the shape
            console.print(f"[red]Error:[/red] could not draft node purposes: {exc}")
            raise typer.Exit(1) from exc
    text = json.dumps(baseline, indent=2) + "\n"
    if write is not None:
        write.write_text(text, encoding="utf-8")
        console.print(
            f"wrote {write} from {len(runs)} healthy run(s). "
            "Pass it as ArgusRecorder(baseline=...)."
        )
        if purposes:
            console.print(
                "Edit the drafted `purposes`: the run reviewer reads them to tell a "
                "node's design from a defect."
            )
    else:
        console.print(text, end="")
    return baseline
