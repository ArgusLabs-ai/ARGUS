"""``argus claims`` — extract cross-step claims from a run and verify them.

Reviewer, never gate: the command writes its verdicts onto the run record
(read-modify-write, the ``argus behavior judge`` precedent), refreshes the
``broken_claim`` warning findings, and prints the claim/verdict table. The exit
code reflects whether the command itself worked; a broken claim does not fail CI.
"""

from __future__ import annotations

from rich.console import Console
from rich.markup import escape

console = Console()

_VERDICT_STYLES = {"verified": "green", "broken": "red", "unverifiable": "yellow"}


def claims_run(run_id: str, model: str = "gpt-4o-mini") -> int:
    """Extract claims from every prose step, verify them, save verdicts + findings.

    A re-run that cannot reach the judge (no key, a 429, an unreadable reply)
    keeps the verdicts an earlier run already earned instead of overwriting them
    with nothing; the summary says how many it kept. Every string printed comes
    from a run's own output or from an LLM, so each goes through ``escape``.
    """
    from argus.claim_verifier import (
        MAX_EXTRACT_STEPS,
        extract_run_claims,
        merge_claim_verdicts,
        verify_claims,
        with_claim_findings,
    )
    from argus.storage import load_run, save_run

    try:
        record = load_run(run_id)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"  [red]Error:[/red] {escape(str(e))}")
        return 1

    report = extract_run_claims(record, model)
    new = verify_claims(record, report.claims, model) if report.claims else []
    before = list(record.claim_verdicts)
    verdicts, kept, replaced = merge_claim_verdicts(before, new, report.extracted_steps)

    saved = None
    if verdicts != before:
        record.claim_verdicts = verdicts
        record.findings = with_claim_findings(record.findings, verdicts)
        saved = save_run(record, local_only=True)  # a rewrite: never re-upload the run

    counts = {"verified": 0, "broken": 0, "unverifiable": 0}
    for v in verdicts:
        counts[v.verdict] = counts.get(v.verdict, 0) + 1
        style = _VERDICT_STYLES.get(v.verdict, "dim")
        cited = ", ".join(str(s) for s in v.cited_steps) if v.cited_steps else "none"
        suffix = "" if v.evaluated else "  [dim](not evaluated)[/dim]"
        console.print(
            f"  [{style}]{v.verdict:<12}[/{style}]  {escape(v.claim_id)}  {escape(v.node)}  "
            f"step {v.step_index}{suffix}"
        )
        console.print(f'      [italic]"{escape(v.text)}"[/italic]')
        console.print(f"      [dim]{escape(v.reason)}  cited: {escape(cited)}[/dim]")
        if v.quote:
            console.print(f"      [dim]quoted: {escape(v.quote)}[/dim]")

    if verdicts:
        console.print(
            f"\n  {counts['verified']} verified, "
            f"[red]{counts['broken']} broken[/red], "
            f"{counts['unverifiable']} unverifiable"
        )
    elif not report.failed_steps:
        console.print("  [dim]No claims extracted from this run's outputs.[/dim]")
    if kept:
        console.print(
            f"  [dim]{kept} earlier verdict(s) kept: this run could not redo them.[/dim]"
        )
    if replaced:
        console.print(f"  [dim]{replaced} earlier verdict(s) replaced by this run's claims.[/dim]")
    if report.failed_steps:
        steps = ", ".join(str(s) for s in report.failed_steps)
        console.print(
            f"  [yellow]Could not check step(s) {steps}[/yellow]: the judge was unavailable "
            "or its reply was unreadable (no key? run: argus key set)."
        )
    if report.skipped_steps:
        console.print(
            f"  [dim]Checked the first {MAX_EXTRACT_STEPS} steps with prose; "
            f"{report.skipped_steps} later one(s) were not asked.[/dim]"
        )
    if saved is not None:
        console.print(f"  [green]Saved to {escape(str(saved))}[/green]")
        from argus.cloud import is_logged_in, run_sync_enabled

        # Cloud sync is opt-in (ARGUS_CLOUD_SYNC=1): only then does a cloud copy exist
        # that these verdicts do not reach.
        if run_sync_enabled() and is_logged_in():
            console.print(
                "  [dim]Verdicts are saved locally and are not synced to the cloud.[/dim]"
            )
    else:
        console.print("  [dim]Nothing new to save; the run file was left as it was.[/dim]")
    return 0
