'use client'

import Image from 'next/image'
import { useState } from 'react'

// ── Prompt ─────────────────────────────────────────────────────────────────

const LLM_PROMPT = `I want to add ARGUS monitoring to my LangGraph pipeline. Before writing any code, audit my codebase and then integrate it properly.

## STEP 1 — AUDIT MY PIPELINE

Find the file where my StateGraph is defined and check these things:

1. STATE TYPE: Find my state class. ARGUS works best when state is a TypedDict (or Pydantic model / dataclass). If my state is just a plain dict, convert it to a TypedDict with proper field annotations. Example:

   # BAD — plain dict, ARGUS can't check field contracts
   app = graph.compile()
   result = app.invoke({"query": "...", "results": []})

   # GOOD — TypedDict lets ARGUS verify fields between nodes
   class AgentState(TypedDict):
       query: str
       results: list[str]
       summary: str

2. NODE RETURN TYPES: Check every node function. Each should:
   - Accept the state type as its first parameter (type-annotated)
   - Return a dict with only the fields it's responsible for
   - NOT return the entire state — just the fields it modifies

   # BAD — no type hint, returns everything
   def search(state):
       return {**state, "results": [...]}

   # GOOD — typed, returns only what it produces
   def search(state: AgentState) -> dict:
       return {"results": [...]}

3. GRAPH STRUCTURE: Linear, fan-out/fan-in (DAG), and cyclic (loops / back-edges) all persist automatically after the outermost invoke()/batch()/stream() returns — no finalize() call needed.

4. ASYNC CHECK: If node functions are async (async def), ARGUS handles both — just make sure you're using await app.ainvoke() not app.invoke().

5. EXTERNAL CALLS: List which nodes make external API calls (OpenAI, search APIs, databases).

Print a summary of what you found and any fixes needed before proceeding.

## STEP 2 — FIX COMPATIBILITY ISSUES

If you found issues in Step 1, fix them now:
- Convert plain dict state to TypedDict
- Add type annotations to node function parameters
- Make nodes return only their output fields (not the full state)
- Ensure all fields referenced by downstream nodes exist in the TypedDict

## STEP 3 — INTEGRATE ARGUS

Install: pip install argus-agents
(The PyPI package is argus-agents, not argus. Default install includes the CLI, LangGraph adapter, and UI. LLM judge is optional: run argus key set — no extra pip extra.)

Add ArgusWatcher to the file where the graph is built:

from argus import ArgusWatcher

watcher = ArgusWatcher()
app = watcher.attach(graph)            # StateGraph OR already-compiled app
result = app.invoke(initial_state)     # run persists automatically
print(watcher.run_id)

If you prefer compiling yourself:

watcher = ArgusWatcher(graph)          # uncompiled StateGraph
app = graph.compile()
result = app.invoke(initial_state)

## STEP 4 — PICK THE RIGHT CONFIG

Choose parameters based on what you found in the audit.
Note: record_http, semantic_judge, investigate, and persist_state are all enabled by default.

watcher = ArgusWatcher(graph,
    # DETECTION STRICTNESS — catches empty lists, nested errors, type mismatches
    strict=True,

    # IF you want automatic root cause analysis on failures (default: True)
    investigate=True,         # or "always" to investigate every run

    # IF any fields contain secrets or tokens
    redact_keys={"token", "api_key", "password"},

    # ADD validators for nodes that produce critical output:
    validators={
        # example: ensure summaries aren't empty stubs
        "summarize": lambda o: (len(o.get("summary", "")) > 10, "Summary too short"),
        # wildcard: runs on every node
        "*": lambda o: ("error" not in o, "error key present"),
    },
)

app = watcher.attach(graph)
result = app.invoke(initial_state)
# persisted automatically — finalize() is optional

After running the pipeline:
  argus list              # see all recorded runs
  argus show last         # inspect the most recent run
  argus show <id>         # inspect a specific run by ID
  argus ui                # open the web dashboard`

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      onClick={() => { navigator.clipboard.writeText(text); setCopied(true); setTimeout(() => setCopied(false), 2000) }}
      className="flex items-center gap-1.5 text-xs font-medium px-3 py-1.5 rounded transition-colors"
      style={{
        color: copied ? 'var(--success)' : 'var(--muted-foreground)',
        background: copied ? 'rgba(34,197,94,0.08)' : 'rgba(255,255,255,0.04)',
        border: `1px solid ${copied ? 'rgba(34,197,94,0.25)' : 'var(--border)'}`,
      }}
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

// ── Main Component ──────────────────────────────────────────────────────────

export default function GuideContent() {
  return (
    <div className="pb-24 max-w-[800px]">

      {/* ── Header ──────────────────────────────────────────────────────── */}
      <div className="mb-12">
        <h1 className="text-3xl font-semibold tracking-tight text-foreground">
          Guide
        </h1>
        <p className="text-base text-muted-foreground mt-2 leading-relaxed">
          Dashboard walkthrough, integration setup, and configuration reference.
        </p>
      </div>

      {/* ── Quick Start ──────────────────────────────────────────────────── */}
      <section className="mb-16">
        <h2 className="text-xl font-semibold text-foreground mb-3">Quick Start</h2>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-6">
          Get ARGUS monitoring on your pipeline in 5 steps.
        </p>

        <div className="space-y-5 mb-8">
          <Step n={1} title="Install ARGUS" text="Run pip install argus-agents in your project. That ships the CLI, LangGraph adapter, and UI. The package name is argus-agents, not argus." />
          <Step n={2} title="Copy the AI Setup Prompt" text="On the ARGUS landing page, click the AI Setup Prompt button (shown below). This copies a prompt that handles the full integration for you." />
        </div>

        <div className="rounded-lg overflow-hidden mb-8 max-w-[520px]" style={{ border: '1px solid var(--border)' }}>
          <Image src="/guide/ai-setup-prompt.png" alt="Click the AI Setup Prompt button on the landing page" width={520} height={160} className="w-full h-auto block" />
        </div>

        <div className="space-y-5 mb-8">
          <Step n={3} title="Paste into your AI coding tool" text="Paste the prompt into Claude Code, Cursor, or Copilot. The AI will audit your codebase and integrate ARGUS with the right config." />
          <Step n={4} title="Run your pipeline" text="Execute your LangGraph / LangChain pipeline as usual. ARGUS captures the run automatically." />
          <Step n={5} title="Open the dashboard" text="Run argus ui in your terminal. The web dashboard opens in your browser showing your runs." />
        </div>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">After setup</h3>
        <div className="space-y-3 mb-6">
          <Row label="RUN PIPELINE" text="Run your LangGraph pipeline normally in the terminal." />
          <Row label="CHECK DASHBOARD" text="Takes 1-2 seconds — refresh the page if a new run doesn't appear immediately." />
          <Row label="LOGIN (OPTIONAL)" text='Run argus login in terminal and sign in with Google to sync runs to the cloud.' />
        </div>
      </section>

      <hr className="border-border mb-16" />

      {/* ── CLI Commands ──────────────────────────────────────────────────── */}
      <section className="mb-16">
        <h2 className="text-xl font-semibold text-foreground mb-3">CLI Commands</h2>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-6">
          All commands available from your terminal after installing ARGUS.
        </p>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">Viewing runs</h3>
        <CodeBlock title="List all runs">{`argus list`}</CodeBlock>
        <CodeBlock title="Show the most recent run">{`argus show last`}</CodeBlock>
        <CodeBlock title="Show a specific run (full ID or 8-char prefix)">{`argus show <run-id>`}</CodeBlock>
        <CodeBlock title="Inspect raw input/output for a specific node">{`argus inspect <run-id> --step <node-name>`}</CodeBlock>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4 mt-8">Dashboard</h3>
        <CodeBlock title="Open the web dashboard">{`argus ui`}</CodeBlock>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-6">
          Starts a local server on port 7842 and opens the dashboard in your browser.
          Press <Code>Ctrl+C</Code> in the terminal to stop it.
        </p>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">Replay &amp; compare</h3>
        <CodeBlock title="Replay from a specific node">{`argus replay <run-id> <node-name>`}</CodeBlock>
        <CodeBlock title="Replay with a graph factory">{`argus replay <run-id> <node-name> --app my_pipeline:build_graph`}</CodeBlock>
        <CodeBlock title="Replay just one node in isolation">{`argus replay <run-id> <node-name> --only`}</CodeBlock>
        <CodeBlock title="Diff two runs">{`argus diff <run-id-a> <run-id-b>`}</CodeBlock>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4 mt-8">Account &amp; diagnostics</h3>
        <CodeBlock title="Sign in to sync runs to the cloud">{`argus login`}</CodeBlock>
        <CodeBlock title="Sign out and clear stored credentials">{`argus logout`}</CodeBlock>
        <CodeBlock title="Check current login status">{`argus whoami`}</CodeBlock>
        <CodeBlock title="Diagnose integration issues">{`argus doctor`}</CodeBlock>
        <CodeBlock title="Check for updates">{`argus update`}</CodeBlock>
      </section>

      <hr className="border-border mb-16" />

      {/* ── AI Integration Prompt ───────────────────────────────────────── */}
      <section className="mb-16">
        <h2 className="text-xl font-semibold text-foreground mb-3">
          AI Integration Prompt
        </h2>
        <p className="text-[15px] text-muted-foreground mb-5 leading-relaxed max-w-[620px]">
          This is the full prompt copied by the AI Setup Prompt button. Paste it into
          Claude Code, Cursor, or Copilot. It audits your pipeline for compatibility,
          fixes issues, and adds ARGUS with the right config.
        </p>
        <div
          className="rounded-lg overflow-hidden"
          style={{ border: '1px solid var(--border)', background: 'var(--card)' }}
        >
          <div
            className="flex items-center justify-between px-5 py-3"
            style={{ borderBottom: '1px solid var(--border)', background: 'rgba(255,255,255,0.02)' }}
          >
            <span className="text-xs font-mono text-muted-foreground">prompt.txt</span>
            <CopyButton text={LLM_PROMPT} />
          </div>
          <pre
            className="px-5 py-4 text-[13px] leading-[1.75] font-mono overflow-x-auto whitespace-pre-wrap text-foreground"
            style={{ background: 'var(--card)', maxHeight: '400px', overflowY: 'auto' }}
          >
            {LLM_PROMPT}
          </pre>
        </div>
      </section>

      <hr className="border-border mb-16" />

      {/* ── Runs List ───────────────────────────────────────────────────── */}
      <section className="mb-16">
        <h2 className="text-xl font-semibold text-foreground mb-3">Runs List</h2>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-6">
          Your pipeline execution history. Every run with ARGUS attached shows up here automatically.
        </p>

        <div className="rounded-lg overflow-hidden mb-8" style={{ border: '1px solid var(--border)' }}>
          <Image src="/guide/runs-list.png" alt="Runs list" width={1200} height={700} className="w-full h-auto block" />
        </div>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">Summary cards</h3>
        <div className="grid grid-cols-2 gap-x-8 gap-y-5 mb-8">
          <Field label="Total Runs" text="Pipeline executions recorded in your workspace." />
          <Field label="Clean" text="Runs where every node passed." />
          <Field label="Failed" text="Runs with at least one failure or crash." />
          <Field label="Pass Rate" text="Clean runs as a percentage of total." />
        </div>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">Table columns</h3>
        <div className="space-y-3 mb-6">
          <Row label="RUN ID" text="Unique identifier. Click to open the detail view." />
          <Row label="STATUS" text="Overall result: clean, silent failure, crashed, or semantic fail." />
          <Row label="GRAPH" text="Node execution path shown as a chain." />
          <Row label="STEPS" text="Number of nodes that executed." />
          <Row label="FIRST FAILURE" text="First node that produced bad output — the likely root cause." />
          <Row label="SHAPE" text="Whether all expected nodes ran (full) or the run was cut short (partial)." />
        </div>

        <p className="text-[15px] text-muted-foreground leading-[1.7]">
          The <strong className="text-foreground font-medium">Evaluation</strong> panel lets you filter runs by constraints
          like <Code>overall_status == clean</Code>.
        </p>
      </section>

      <hr className="border-border mb-16" />

      {/* ── Run Detail ──────────────────────────────────────────────────── */}
      <section className="mb-16">
        <h2 className="text-xl font-semibold text-foreground mb-3">Run Detail</h2>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-6">
          Full picture of a single pipeline execution — metrics, execution trace, AI analysis, and initial state.
        </p>

        <div className="rounded-lg overflow-hidden mb-8" style={{ border: '1px solid var(--border)' }}>
          <Image src="/guide/run-detail-1.png" alt="Run detail header and metrics" width={1200} height={700} className="w-full h-auto block" />
        </div>

        <div className="grid grid-cols-2 gap-x-8 gap-y-5 mb-8">
          <Field label="Header" text="Run ID, status, timestamp, duration, step count, and ARGUS version." />
          <Field label="Root Cause Chain" text="Traces failures back to the originating node, not the node that complained." />
        </div>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">Metrics</h3>
        <div className="grid grid-cols-2 gap-x-8 gap-y-5 mb-8">
          <Field label="Duration" text="Wall-clock time for the full run." />
          <Field label="Success Rate" text="Percentage of nodes that passed." />
          <Field label="Failures" text="Nodes with any failure status." />
          <Field label="Severity" text="Worst level seen: ok, warning, or critical." />
          <Field label="Completed" text="Whether the pipeline reached the final node." />
        </div>

        <div className="rounded-lg overflow-hidden mb-8" style={{ border: '1px solid var(--border)' }}>
          <Image src="/guide/run-detail-2.png" alt="Execution timeline and AI analysis" width={1200} height={700} className="w-full h-auto block" />
        </div>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">Execution timeline</h3>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-5">
          Nodes listed in execution order with name, output type, duration, and status.
          Failed nodes show a root cause annotation — which field was missing and which
          upstream node dropped it. Expand any row to see full I/O JSON.
        </p>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">Node statuses</h3>
        <div className="space-y-3 mb-8">
          <Row label="Pass" text="Node executed successfully with no issues detected." />
          <Row label="Fail" text="Structural problem — missing fields, tool errors, or silent failures." />
          <Row label="Crashed" text="Node threw an exception during execution." />
          <Row label="Semantic fail" text="Output passes structural checks but fails LLM quality review." />
          <Row label="Degraded input" text="Node ran but received incomplete state from a failed upstream node." />
          <Row label="Skipped" text="Node was on an unchosen conditional branch — never activated. Shown as gray dashed boxes in the graph." />
          <Row label="Interrupted" text="Execution was interrupted (e.g. GraphInterrupt)." />
          <Row label="Retried" text="Node ran multiple times in a loop — earlier iterations marked retried when the final pass succeeded." />
        </div>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">AI Analysis</h3>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-5">
          When a provider key is set (OpenAI, Anthropic, or Google — via <Code>argus key set</Code>),
          ARGUS investigates non-clean runs automatically. The analysis panel has three sections:
        </p>
        <div className="space-y-3 mb-8 pl-1">
          <Row label="Root Cause Node" text="The node that first produced broken state." />
          <Row label="Reason" text="Why the node failed and how it propagated downstream." />
          <Row label="How to Fix" text="Numbered action items targeting specific nodes." />
        </div>

        <div className="rounded-lg overflow-hidden mb-6" style={{ border: '1px solid var(--border)' }}>
          <Image src="/guide/run-detail-3.png" alt="AI fix steps and correlation" width={1200} height={700} className="w-full h-auto block" />
        </div>

        <div className="rounded-lg overflow-hidden mb-8" style={{ border: '1px solid var(--border)' }}>
          <Image src="/guide/run-detail-4.png" alt="Behavior and initial state" width={1200} height={700} className="w-full h-auto block" />
        </div>

        <div className="grid grid-cols-2 gap-x-8 gap-y-5">
          <Field label="Correlation" text="Confirms the true origin node with failure signals and a confidence score." />
          <Field label="Behavior" text="Raw initial state your pipeline received — the exact input at invocation time." />
        </div>
      </section>

      <hr className="border-border mb-16" />

      {/* ── Compare ─────────────────────────────────────────────────────── */}
      <section className="mb-16">
        <h2 className="text-xl font-semibold text-foreground mb-3">Compare</h2>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-6">
          Side-by-side diff of two runs. Useful for verifying fixes, catching regressions,
          or understanding performance differences.
        </p>

        <div className="rounded-lg overflow-hidden mb-5" style={{ border: '1px solid var(--border)' }}>
          <Image src="/guide/compare-1.png" alt="Compare page overview" width={1200} height={700} className="w-full h-auto block" />
        </div>
        <div className="rounded-lg overflow-hidden mb-8" style={{ border: '1px solid var(--border)' }}>
          <Image src="/guide/compare-2.png" alt="Compare node diff" width={1200} height={700} className="w-full h-auto block" />
        </div>

        <div className="space-y-4 mb-6">
          <Step n={1} title="Open Compare" text="Sidebar link, or the Compare button on any run detail page." />
          <Step n={2} title="Enter two run IDs" text="Run A is typically the broken run, Run B is the fix." />
          <Step n={3} title="Read the verdict" text="Winner banner shows which run performed better and why." />
          <Step n={4} title="Read the node diff" text="Status in A vs B per node. Missing nodes labelled only in A / only in B." />
        </div>
      </section>

      <hr className="border-border mb-16" />

      {/* ── Approvals ──────────────────────────────────────────────────── */}
      <section className="mb-16">
        <h2 className="text-xl font-semibold text-foreground mb-3">Approvals</h2>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-6">
          Gate deployments on ARGUS results. Runs that meet your criteria get approved;
          everything else is held for review.
        </p>

        <div className="rounded-lg overflow-hidden mb-8" style={{ border: '1px solid var(--border)' }}>
          <Image src="/guide/approvals.png" alt="Approvals page" width={1200} height={700} className="w-full h-auto block" />
        </div>
      </section>

      <hr className="border-border mb-16" />

      {/* ── Rerun ───────────────────────────────────────────────────────── */}
      <section className="mb-16">
        <h2 className="text-xl font-semibold text-foreground mb-3">Rerun</h2>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-6">
          Re-execute from a specific node using the frozen input state from a previous run.
          Test a fix without re-running the full pipeline or making upstream LLM calls.
          Use <Code>record_http=True</Code> for fully deterministic reruns from disk.
        </p>

        <div className="space-y-4 mb-8">
          <Step n={1} title="Open the failing run" text="Click the run ID to open its detail page." />
          <Step n={2} title="Find the root cause node" text="Red banner at the top names the originating node." />
          <Step n={3} title="Click the rerun icon" text="Each node row has a rerun icon. Click it on the root cause node." />
          <Step n={4} title="Wait for the new run" text="ARGUS re-executes from that node forward, creates a new run." />
          <Step n={5} title="Compare to confirm" text="Diff the original against the rerun — broken nodes should now pass." />
        </div>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">From the CLI</h3>
        <CodeBlock title="Rerun from a specific node">{`argus replay <run-id> <node-name>`}</CodeBlock>
        <CodeBlock title="With graph factory">{`argus replay <run-id> <node-name> --app my_pipeline:build_graph`}</CodeBlock>
        <CodeBlock title="Diff the results">{`argus diff <original-run-id> <rerun-run-id>`}</CodeBlock>
      </section>

      <hr className="border-border mb-16" />

      {/* ── Configuration Reference ─────────────────────────────────────── */}
      <section className="mb-16">
        <h2 className="text-xl font-semibold text-foreground mb-3">Configuration Reference</h2>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-6">
          All <Code>ArgusWatcher</Code> parameters.
          Graph is the only positional argument — everything else is keyword-only.
        </p>

        <CodeBlock title="ArgusWatcher(graph, **kwargs)">
{`watcher = ArgusWatcher(
    graph,                  # uncompiled StateGraph — or omit and call attach()

    # --- Output control ---
    max_field_size=50_000,  # max chars per field before truncation (default: 50k)
    redact_keys={"token", "api_key"},  # field names to scrub from stored outputs
    persist_state=True,     # save run records to .argus/runs/ (default: True)

    # --- Detection strictness ---
    strict=True,            # extra checks: nested error keys, rate-limit responses,
                            # empty lists, type mismatches. recommended for CI/staging.

    # --- Semantic validators ---
    validators={
        "summarize": lambda o: (len(o.get("summary","")) > 10, "Summary too short"),
        "*": lambda o: ("error" not in o, "error key present"),  # runs on every node
    },

    # --- LLM investigation ---
    investigate=True,       # LLM root-cause analysis on failures (default: True)
                            # set to "always" for every node, False to disable

    # --- Deterministic rerun ---
    record_http=True,       # saves every outbound API call to disk. (default: True)
                            # reruns replay from disk — zero extra cost.

    # --- LLM semantic judge ---
    semantic_judge=True,    # LLM reviews every node's output for subtle quality issues.
                            # (default: True) needs a provider key — see 'argus key set'.
    judge_model="gpt-4o",  # tier hint: capable model. Auto-mapped to your active
                            # provider (Claude/Gemini). "gpt-4o-mini" = cheaper tier.

    # --- Latency thresholds ---
    config=ArgusConfig(
        node_timeout_ms=30_000,  # flag nodes that take >=95% of this (likely truncated)
        min_expected_ms=500,     # flag LLM nodes completing faster (likely cached/stale)
    ),
)

app = watcher.attach(graph)
result = app.invoke(initial_state)`}
        </CodeBlock>

        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-4">
          Access <Code>watcher.run_id</Code> after the run. Most parameters are
          enabled by default — <Code>record_http</Code>, <Code>semantic_judge</Code>,
          <Code>investigate</Code>, and <Code>persist_state</Code> are all <Code>True</Code> out of the box.
        </p>
        <div
          className="rounded-lg px-5 py-4 mb-8"
          style={{ background: 'rgba(234,179,8,0.06)', border: '1px solid rgba(234,179,8,0.2)' }}
        >
          <p className="text-[14px] leading-[1.7]">
            <strong className="text-foreground">Use <Code>watcher.attach(graph)</Code> — one call for StateGraph or compiled apps.</strong>
            <span className="text-muted-foreground">
              {' '}Runs persist when the outermost <Code>invoke()</Code> / <Code>batch()</Code> / <Code>stream()</Code> returns, including cyclic graphs.
              <Code>finalize()</Code> is an optional idempotent flush, not required.
            </span>
          </p>
        </div>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">record_http</h3>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-3">
          Captures every HTTP request/response during the original run. On rerun, serves
          recorded responses back — same data, zero cost, fully reproducible.
        </p>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-8">
          <strong className="text-foreground font-medium">Enable</strong> when nodes call paid APIs and you want cheap, identical reruns.{' '}
          <strong className="text-foreground font-medium">Skip</strong> when you want the rerun to hit the real API.
        </p>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4">semantic_judge</h3>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-5">
          ARGUS catches ~80% of production failures deterministically — missing fields, empty results,
          type mismatches, placeholder outputs. The remaining ~20% are subtle: wrong tone, unhelpful
          responses, outdated info. The semantic judge covers those.
        </p>
        <div className="space-y-3 mb-5">
          <Row label="Deterministic first" text="Structural checks run first — free, instant, reproducible." />
          <Row label="LLM second" text="Judge only reviews what structural checks couldn't decide." />
          <Row label="Per-node" text="Each output evaluated in context of its input and the pipeline's purpose." />
        </div>
        <p className="text-[15px] text-muted-foreground leading-[1.7]">
          Requires a provider key (OpenAI, Anthropic, or Google) — set via <Code>argus key set</Code>.
          {' '}<strong className="text-foreground font-medium">Enable</strong> for complex multi-agent pipelines.
          {' '}<strong className="text-foreground font-medium">Skip</strong> for simple pipelines or zero-cost monitoring.
        </p>

        <h3 className="text-sm font-semibold uppercase tracking-widest text-muted-foreground mb-4 mt-8">Latency Thresholds</h3>
        <p className="text-[15px] text-muted-foreground leading-[1.7] mb-3">
          Detects timing-correlated degradation — no LLM calls, purely algorithmic.
          Pass thresholds via <Code>ArgusConfig</Code>:
        </p>
        <div className="space-y-3 mb-5">
          <Row label="Near timeout" text="node_timeout_ms — flags nodes that take ≥95% of the timeout (likely truncated output)." />
          <Row label="Suspiciously fast" text="min_expected_ms — flags LLM nodes that complete too quickly (likely cached or stale)." />
          <Row label="Fast + failed" text="Combines both: fast completion with existing quality issues = cached failure." />
        </div>
        <p className="text-[15px] text-muted-foreground leading-[1.7]">
          Both thresholds are optional and <Code>None</Code> by default — latency checks only run when configured.
        </p>
      </section>
    </div>
  )
}

// ── Primitives ──────────────────────────────────────────────────────────────

function Code({ children }: { children: string }) {
  return (
    <code className="text-[13px] font-mono px-1.5 py-0.5 rounded bg-card text-foreground">
      {children}
    </code>
  )
}

function Field({ label, text }: { label: string; text: string }) {
  return (
    <div>
      <p className="text-[15px] font-medium text-foreground mb-1">{label}</p>
      <p className="text-[15px] text-muted-foreground leading-[1.7]">{text}</p>
    </div>
  )
}

function Row({ label, text }: { label: string; text: string }) {
  return (
    <div className="flex gap-4 items-baseline">
      <span
        className="text-xs font-mono font-medium px-2 py-1 rounded shrink-0"
        style={{ background: 'rgba(91,106,240,0.08)', color: 'var(--primary)', border: '1px solid rgba(91,106,240,0.15)' }}
      >
        {label}
      </span>
      <span className="text-[15px] text-muted-foreground leading-[1.7]">{text}</span>
    </div>
  )
}

function Step({ n, title, text }: { n: number; title: string; text: string }) {
  return (
    <div className="flex gap-4 items-start">
      <span
        className="w-6 h-6 rounded flex items-center justify-center text-xs font-mono font-medium shrink-0 mt-0.5"
        style={{ background: 'rgba(255,255,255,0.04)', color: 'var(--muted-foreground)', border: '1px solid var(--border)' }}
      >
        {n}
      </span>
      <p className="text-[15px] leading-[1.7]">
        <span className="font-medium text-foreground">{title}</span>
        <span className="text-muted-foreground"> — {text}</span>
      </p>
    </div>
  )
}

function CodeBlock({ children, title }: { children: string; title?: string }) {
  return (
    <div className="my-5 rounded-lg overflow-hidden" style={{ border: '1px solid var(--border)' }}>
      {title && (
        <div
          className="px-5 py-2.5 text-xs font-mono text-muted-foreground"
          style={{ background: 'rgba(255,255,255,0.02)', borderBottom: '1px solid var(--border)' }}
        >
          {title}
        </div>
      )}
      <pre
        className="px-5 py-4 text-[13px] leading-[1.75] font-mono overflow-x-auto text-foreground"
        style={{ background: 'var(--card)' }}
      >
        {children}
      </pre>
    </div>
  )
}
