"""Write this job's ARGUS verdict to the GitHub job summary.

    python summary.py <run-id> [<run-id> ...]

The gate's exit code already fails the job; this puts the *why* on the PR page,
where the runner's `.argus/` is gone by the time anyone reads it. Never raises:
a summary that crashes must not change the gate's outcome.

Grades with the gate's own strict mode (`GATE_STRICT`, run-id mode only), so the page
and the exit code agree.
With `ARGUS_BUNDLE` set, copies each failed run's file there (by its full id, so a
`run-id:` prefix works) for the artifact upload.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MAX_FIX_PROMPTS = 5  # ponytail: the summary is capped at 1 MiB; the rest are in the artifact


def _check(run_id: str) -> dict | None:
    cmd = ["argus", "check", run_id, "--format", "json"]
    strict = os.environ.get("GATE_STRICT", "").strip()
    if strict:
        cmd += ["--strict", strict]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    try:
        verdict = json.loads(proc.stdout)
    except json.JSONDecodeError:
        verdict = None
    if not isinstance(verdict, dict) or not verdict.get("run_id"):
        print(f"::warning::argus check {run_id} printed no verdict: {proc.stderr.strip()[-300:]}")
        return None
    return verdict


def _fix_prompt(run_id: str) -> str:
    # --sanitized: a job summary is readable by anyone who can read the repo,
    # so recorded state values stay in the artifact, not on the page.
    proc = subprocess.run(["argus", "fix", run_id, "--sanitized"], capture_output=True, text=True)
    return proc.stdout.strip()


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:200]


def _first_failure(verdict: dict) -> str:
    """The first critical finding's type and field, never its reason: a reason can quote a
    recorded value (an API key in a tool error), and the page is readable by the whole repo."""
    for f in verdict.get("findings", []):
        if f.get("severity") == "critical" and not f.get("suppressed"):
            field = f" on `{_cell(f['field_path'])}`" if f.get("field_path") else ""
            return f"`{_cell(f.get('type') or '?')}`{field}"
    return "see the artifact"


def render(verdicts: list[dict], fix_prompts: dict[str, str]) -> str:
    if not verdicts:
        return (
            "## ARGUS\n\n"
            "⚠️ **No agent runs were graded.** Your tests never invoked a LangGraph "
            "graph, so this gate checked nothing. Add a test that runs your agent.\n"
        )

    failed = [v for v in verdicts if not v.get("passed")]
    if not failed:
        return f"## ARGUS\n\n✅ {len(verdicts)} agent run(s) graded, all clean.\n"

    lines = [
        "## ARGUS",
        "",
        f"❌ **{len(failed)} of {len(verdicts)} agent run(s) failed.**",
        "",
        "| Run | Status | Root cause | Failure |",
        "|---|---|---|---|",
    ]
    for v in failed:
        origin = ", ".join(v.get("root_cause_chain") or []) or v.get("first_failure_step") or "—"
        lines.append(
            f"| `{v['run_id']}` | {v.get('overall_status', '?')} | `{_cell(origin)}` "
            f"| {_first_failure(v)} |"
        )
    for run_id, prompt in fix_prompts.items():
        if prompt:
            lines += [
                "",
                f"<details><summary>Fix prompt for <code>{run_id}</code> "
                "(paste into your coding agent)</summary>",
                "",
                "````markdown",
                prompt,
                "````",
                "</details>",
            ]
    lines += [
        "",
        "Full runs are in the **argus-runs** artifact. Unzip it into your project "
        "and run `argus ui`, `argus fix <run-id>` or `argus replay <run-id> <node>`.",
    ]
    return "\n".join(lines) + "\n"


def _bundle(run_ids: list[str]) -> None:
    """Copy the failed runs' files into `$ARGUS_BUNDLE/.argus/runs/` for the artifact."""
    bundle = os.environ.get("ARGUS_BUNDLE")
    if not bundle or not run_ids:
        return
    from argus.storage import runs_dir  # noqa: PLC0415 — the gate's installed argus

    dest = Path(bundle) / ".argus" / "runs"
    dest.mkdir(parents=True, exist_ok=True)
    for run_id in run_ids:
        src = runs_dir() / f"{run_id}.json"
        if src.is_file():
            shutil.copy2(src, dest)


def main(run_ids: list[str]) -> None:
    verdicts = [v for v in (_check(r) for r in run_ids) if v is not None]
    failed = [v["run_id"] for v in verdicts if not v.get("passed")]
    page = render(verdicts, {r: _fix_prompt(r) for r in failed[:MAX_FIX_PROMPTS]})
    _bundle(failed)
    if not verdicts:
        print("::warning::ARGUS graded no agent runs — your tests never invoked a graph.")

    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(page)
    else:
        print(page)


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except Exception as e:  # noqa: BLE001 — never change the gate's outcome
        print(f"::warning::ARGUS summary failed: {e}")
