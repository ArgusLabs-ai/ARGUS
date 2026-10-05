## Website demo data (ARGUS)

This folder contains **fake but realistic** run data you can feed into a UI to
render:

- a run list (dashboard table)
- a node graph (DAG)
- a trace view (per-step timeline)
- silent failures vs crashes vs clean runs
- parallel fan-out panels and cyclic iterations

### Files

- `data/index.json`: list of runs + pointers to the JSON files
- `data/runs/*.json`: one file per run (shape mirrors `argus.models.RunRecord`)
- `data/logs/*.log`: optional plain-text logs for a “logs UI” panel

### Notes

- Timestamps are ISO-8601 `UTC` strings.
- `steps[*].inspection` is populated for silent failures and warnings.
- `overall_status` uses ARGUS statuses: `clean`, `silent_failure`, `crashed`.

### Maintainer preview

The shipped sidebar only lists pages that exist (Runs, Compare, Approvals, Guide,
Changelog, Settings). Add `?preview=1` to any dashboard URL to show the planned
Traces / Evaluation / Graphs / Alerts / Datasets items for design review. They
stay disabled until each has a backing feature.

