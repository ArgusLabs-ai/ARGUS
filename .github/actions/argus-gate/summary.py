"""Write this job's ARGUS verdict to the GitHub job summary.

    python summary.py <run-id> [<run-id> ...]

The gate's exit code already fails the job; this puts the *why* on the PR page,
where the runner's `.argus/` is gone by the time anyone reads it. Never raises:
a summary that crashes must not change the gate's outcome.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

MAX_FIX_PROMPTS = 5  # ponytail: the summary is capped at 1 MiB; the rest are in the artifact


def _check(run_id: str) -> dict | None:
    proc = subprocess.run(
        ["argus", "check", run_id, "--format", "json"], capture_output=True, text=True
    )
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(f"::warning::argus check {run_id} printed no verdict: {proc.stderr.strip()[-300:]}")
        return None


def _fix_prompt(run_id: str) -> str:
    # --sanitized: a job summary is readable by anyone who can read the repo,
    # so recorded state values stay in the artifact, not on the page.
    proc = subprocess.run(["argus", "fix", run_id, "--sanitized"], capture_output=True, text=True)
    return proc.stdout.strip()


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")[:200]


def _first_reason(verdict: dict) -> str:
    for f in verdict.get("findings", []):
        if f.get("severity") == "critical" and not f.get("suppressed"):
            return f.get("reason", "")
    reasons = verdict.get("reasons") or [""]
    return reasons[0]


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
        "| Run | Status | Root cause | Why |",
        "|---|---|---|---|",
    ]
    for v in failed:
        origin = ", ".join(v.get("root_cause_chain") or []) or v.get("first_failure_step") or "—"
        lines.append(
            f"| `{v['run_id']}` | {v.get('overall_status', '?')} | `{_cell(origin)}` "
            f"| {_cell(_first_reason(v))} |"
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


def main(run_ids: list[str]) -> None:
    verdicts = [v for v in (_check(r) for r in run_ids) if v is not None]
    failing = [v["run_id"] for v in verdicts if not v.get("passed")][:MAX_FIX_PROMPTS]
    page = render(verdicts, {r: _fix_prompt(r) for r in failing})
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
