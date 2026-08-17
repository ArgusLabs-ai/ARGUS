# Contributing to ARGUS

Thanks for your interest in contributing. ARGUS is a production readiness platform for AI agent pipelines — there's a lot of surface area and we welcome help across the board.

## Areas Where Contribution Is Needed

### Framework Adapters — Planned (Not Yet Open for PRs)

ARGUS currently has a first-class adapter for **LangGraph** only. The core engine (`ArgusSession`, detection pipeline, replay, correlation) is still under active development — new fields are added to `NodeEvent`/`RunRecord` and the `wrap()` API is evolving with each release.

Building adapters now would mean rewriting them every few versions. Once the core stabilizes, we'll open adapter work for:

- **CrewAI** — auto-instrument crew tasks and agent handoffs
- **Google ADK** — wrap agent actions and tool responses
- **AutoGen** — wrap multi-agent conversations and tool calls
- **LlamaIndex** — monitor query pipelines and retrieval nodes
- **Haystack** — instrument pipeline components
- **DSPy** — track module executions and optimizer runs
- **SmolAgents** — wrap tool-calling agent steps

**Want to help when the time comes?** Open an issue titled "Adapter interest: [framework]" so we can ping you when the API is stable. In the meantime, you can use `ArgusSession` directly with any framework via manual `wrap()` calls.

### Detection Signatures

The semantic signature registry (`src/argus/data/signatures.json`) ships with 61 patterns across 6 categories. More real-world patterns are needed:

- **LLM refusal variants** — new refusal phrasings from Claude, Gemini, Llama, Mistral
- **Hallucination markers** — confident-sounding but fabricated outputs
- **Rate limit responses** — provider-specific throttling patterns (Anthropic, Cohere, etc.)
- **Multilingual placeholders** — non-English placeholder/filler text detection
- **Tool call failures** — patterns from function calling that look like success but aren't

To add signatures, edit `src/argus/data/signatures.json` and include a fixture run that demonstrates the detection.

### Exporters & Integrations

ARGUS currently has no export integrations. These would be high-impact contributions:

- **OpenTelemetry** — emit spans/traces compatible with OTel collectors
- **Datadog / New Relic** — push run metrics as custom events
- **Prometheus** — expose a `/metrics` endpoint for scraping
- **Slack / Discord / PagerDuty** — alerting on pipeline failures
- **Webhooks** — generic POST on run completion with configurable payloads

### Web UI — Planned Pages

Several pages in the dashboard are stubbed but not yet implemented (marked "soon" in the sidebar):

- **Traces** — distributed tracing view across pipeline runs
- **Evaluation** — benchmark pipelines against golden datasets
- **Graphs** — visualize pipeline topology and evolution over time
- **Alerts** — configurable alert rules (failure rate thresholds, latency spikes)
- **Datasets** — manage test datasets for regression testing
- **Settings** — UI for configuration (currently CLI-only)
- **Logs Comparison** — side-by-side log diff in the Compare view

### Unit Tests

The test suite is currently integration/smoke-style. Dedicated unit tests are needed for core modules:

- `inspector.py` — detection rules, root cause chain building
- `anomaly_detector.py` — behavioral anomaly detection
- `correlator.py` — cross-node correlation analysis
- `replay.py` — state restoration and selective rerun
- `semantic_checker.py` — LLM judge coherence checks
- `registry.py` — signature matching strategies
- `http_recorder.py` — HTTP record/playback for deterministic reruns
- `heuristic_engine.py` — pattern matching and candidate promotion
- `cloud.py` — Supabase sync logic

### Detection Improvements for Complex Pipelines

Stress-testing with real-world pipeline patterns revealed areas where detection could be stronger. These are concrete improvements, not bugs — the current behavior is conservative by design, but better coverage would catch more issues in production:

- **Terminal node degradation** — when the last node in a pipeline operates on degraded upstream data but produces syntactically valid output, ARGUS marks it `pass`. It correctly blames the upstream node, but the terminal node looks clean. A `degraded_input` status on terminal nodes (even without successor validation) would give clearer signal.
- **`has_tool_failure` vs warning-severity failures** — rate limits (HTTP 429), partial batch failures, and nested errors are all detected in `tool_failures` but with `severity="warning"`. The boolean `has_tool_failure` only fires on `"critical"`. Consumers checking only the boolean miss these. Options: a separate `has_tool_warnings` flag, or promote rate limits to critical.
- **Domain-specific hedging detection** — the semantic registry catches "I apologize" and "As an AI" but misses domain hedging like "No documents available" or "Unable to retrieve data". More signatures in `data/signatures.json` for retrieval-failure and empty-result hedging would help.
- **Subtle field drops in untyped pipelines** — if a node silently drops a field and no downstream node crashes or has type annotations, ARGUS stays quiet. This is correct (no consumer complained), but optional structural warnings for fields present in input but absent in output would catch data loss earlier.
- **Confidence-mismatch escalation** — a node returning `confidence: 0.98` with `documents: []` gets flagged for the empty list but not for the contradiction. A cross-field coherence check (high confidence + empty/error data = suspicious) would catch nodes that lie about their certainty.

### Documentation

- **Framework-specific guides** — step-by-step setup for CrewAI, Google ADK, AutoGen, etc. (once adapters ship)
- **CI/CD integration examples** — GitHub Actions, GitLab CI, Jenkins
- **Advanced usage** — custom validators, semantic judge configuration, HTTP recording workflows

---

## Ways to Contribute

- **Fixture runs** — real agent output dicts that expose failure classes ARGUS should detect
- **Bug reports** — open an issue with a minimal reproduction
- **Detection improvements** — PRs against `src/argus/inspector.py` or `src/argus/registry.py`
- **Adapter interest** — open an issue so we can notify you when the API stabilizes
- **UI pages** — React components under `website/`

## Adding Fixture Runs

Fixtures are the fastest way to contribute. No need to write tests — just drop your run output dicts.

1. Find or create the right subdirectory under `fixtures/` (e.g. `fixtures/unverified_completion/runs/`)
2. Add your JSON file(s) — see `fixtures/README.md` for the expected format
3. Open a PR with a short description of the run setup and what the agent did/didn't do

## Development Setup

```bash
pip install -e ".[dev]"
pytest tests/
```

## Running Tests

```bash
pytest tests/ -v
pytest tests/test_smoke.py::test_name -v      # single test
pytest --cov=src --cov-report=term-missing     # with coverage
```

## Code Style

```bash
ruff check src/     # lint
ruff format src/    # format
mypy src/argus      # type check
```

## Opening Issues

For bug reports, include:
- ARGUS version (`pip show argus-agents`)
- A minimal run output dict that reproduces the issue
- What ARGUS reported vs what you expected

## Pull Requests

- Keep PRs focused — one fix or feature per PR
- For new detection logic, include a fixture run that the old code misses and the new code catches
- Don't add co-author attribution in commits
- **Update docs with your PR.** If your change adds, moves, or removes a module, updates a public API, or changes how something works — update `CLAUDE.md` (architecture notes, key files table) and any relevant docs in the same PR. Don't leave it for a follow-up.
- **Add a label to your PR.** Pick the one that fits best:

  | Label | When to use |
  |-------|-------------|
  | `bugfix` | Fixes a bug |
  | `enhancement` | New feature or capability |
  | `refactor` | Internal restructuring, no behavior change |
  | `investigation` | Spike or research — no production code touched |
  | `documentation` | Docs-only changes |
  | `dead-code` | Removing unreachable or unused code |

## Contribution Terms (CLA)

ARGUS is **open-core**: the core in `src/argus/` is Apache-2.0, while `cloud/`
and `supabase/` are proprietary. To keep this model workable, first-time
contributors must sign our lightweight [Contributor License Agreement](CLA.md).
A bot will prompt you on your first pull request — signing is a one-time comment.

The `cloud/` and `supabase/` directories are **not open to external
contributions**. PRs that modify them will be closed.

## Review & Merge Process

- `master` is protected: **all changes land via pull request** — no direct pushes.
- CI (ruff + pytest on Python 3.9/3.11/3.12) must pass.
- At least one **code owner** approval is required (see [CODEOWNERS](.github/CODEOWNERS)).
- Only maintainers merge to `master`.

## Reporting Security Issues

Do not open a public issue. Follow [SECURITY.md](SECURITY.md).
