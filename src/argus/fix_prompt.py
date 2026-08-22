"""Deterministic assembly of coding-agent-ready fix prompts.

Turns a recorded :class:`~argus.models.RunRecord` into a plain-markdown prompt a
developer can paste into any coding agent (Claude Code, Cursor, Windsurf, ...).

Three properties are load-bearing and must not regress:

* **Offline** — no network call, no ``argus login``. The source-locator fallback
  is invoked with ``use_llm=False`` for exactly this reason.
* **Deterministic** — the same run id always renders byte-identical output.
* **Jargon-free** — internal status enums and signal ids (``degraded_input``,
  ``CM-003``) are translated to plain English before they reach the prompt.

The prompt targets the *origin* of the failure, not the node where it surfaced.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from argus.models import NodeEvent, RunRecord
from argus.storage import load_run

__all__ = [
    "FixPromptError",
    "FixPromptResult",
    "build_fix_prompt",
    "build_fix_prompt_for_record",
]

# Longest rendered value kept in the evidence section. State snapshots are
# capped at max_field_size (50_000) at capture time — dumping that into a
# prompt buries the instruction for a smaller model.
_MAX_VALUE_CHARS = 800

# Statuses that mean "this node did something wrong", as opposed to
# bookkeeping statuses like "retried".
_FAILING_STATUSES = ("crashed", "fail", "degraded_input", "semantic_fail")

# ── Jargon translation ────────────────────────────────────────────────────────
# Every internal enum a developer would have to decode gets a plain-English
# rendering here. Nothing below this line should leak an ARGUS-internal name.

_STATUS_PLAIN = {
    "crashed": "raised an error and stopped",
    "fail": "produced output missing fields the next node requires",
    "degraded_input": (
        "ran with incomplete input because an earlier node did not produce a field it needed"
    ),
    "semantic_fail": ("produced output that looks structurally correct but is wrong in content"),
    "interrupted": "was interrupted before it finished",
    "pass": "completed without an error being raised",
    "retried": "was retried",
}

_TOOL_FAILURE_PLAIN = {
    "error_response": "the external call returned an error response",
    "rate_limit": "the external call was rate-limited",
    "empty_result": "the external call returned an empty result",
    "error_in_data": "the data that came back contained an error payload",
    "partial_failure": "the external call only partly succeeded",
    "json_in_string": "the output contained a double-encoded stringified JSON object/array instead of a parsed structure",
}

# Present tense, for the "Done when" conditions — the past-tense phrasings above
# read wrong in a forward-looking success criterion.
_TOOL_FAILURE_CONDITION = {
    "error_response": "the external call returns an error response",
    "rate_limit": "the external call is rate-limited",
    "empty_result": "the external call returns an empty result",
    "error_in_data": "the data that comes back contains an error payload",
    "partial_failure": "the external call only partly succeeds",
    "json_in_string": "the output contains a double-encoded stringified JSON object/array instead of a parsed structure",
}


class FixPromptError(Exception):
    """Raised when a run cannot produce a meaningful fix prompt."""


@dataclass
class FixPromptResult:
    """Rendered prompt plus the metadata the dashboard route needs."""

    prompt: str
    node: str
    source_path: Optional[str]


# ── Public API ────────────────────────────────────────────────────────────────


def build_fix_prompt(
    run_id: str,
    *,
    node: Optional[str] = None,
    sanitized: bool = False,
) -> str:
    """Deterministically assemble a coding-agent-ready fix prompt for a run's
    root-cause failure. No LLM call. Node defaults to the origin of
    root_cause_chain; pass `node` to target a specific step instead."""
    record = load_run(run_id)
    return build_fix_prompt_for_record(record, node=node, sanitized=sanitized).prompt


def build_fix_prompt_for_record(
    record: RunRecord,
    *,
    node: Optional[str] = None,
    sanitized: bool = False,
) -> FixPromptResult:
    """Same as :func:`build_fix_prompt` but against an already-loaded record.

    Kept separate so the dashboard route can report the targeted node without
    re-loading, and so tests can build records in memory.
    """
    target = _resolve_target_node(record, node)
    event = _find_node_event(record, target)

    if event is None:
        raise FixPromptError(f"Node '{target}' has no recorded step in run {record.run_id}.")

    if not _has_failure_signal(event):
        raise FixPromptError(
            f"Node '{target}' in run {record.run_id} shows no detected problem — "
            "there is nothing to write a fix prompt about."
        )

    source_cache: dict = {}
    source_path, path_note = _resolve_source(record, target, source_cache)
    symptom_event = _find_symptom_event(record, target)

    prompt = _render(
        record=record,
        target=target,
        event=event,
        source_path=source_path,
        path_note=path_note,
        symptom_event=symptom_event,
        sanitized=sanitized,
        source_cache=source_cache,
    )
    return FixPromptResult(prompt=prompt, node=target, source_path=source_path)


# ── Target resolution ─────────────────────────────────────────────────────────


def _resolve_target_node(record: RunRecord, node: Optional[str]) -> str:
    """Explicit node → root-cause origin → first failing step."""
    if node:
        known = set(record.graph_node_names or []) | {e.node_name for e in record.steps}
        if node not in known:
            available = ", ".join(sorted(known)) or "(none recorded)"
            raise FixPromptError(
                f"Node '{node}' is not part of run {record.run_id}. Available nodes: {available}"
            )
        return node

    # chain[0] is the inspector origin (chronological walk) and, after
    # correlation overwrites the field with degradation_origins, the
    # highest-confidence onset. Either way it is the node to fix.
    if record.root_cause_chain:
        return record.root_cause_chain[0]

    if record.first_failure_step:
        return record.first_failure_step

    raise FixPromptError(
        f"Run {record.run_id} has no recorded failure (status: "
        f"{record.overall_status}). Nothing to fix — pass --node to target a "
        "specific step anyway."
    )


def _find_node_event(record: RunRecord, node_name: str) -> Optional[NodeEvent]:
    """Return the node's event — the last real attempt if the node looped."""
    candidates = [e for e in record.steps if e.node_name == node_name]
    if not candidates:
        return None

    # "retried" marks a superseded attempt; prefer the attempt that stuck.
    real = [e for e in candidates if e.status != "retried"]
    pool = real or candidates
    return max(pool, key=lambda e: (e.attempt_index, e.step_index))


def _find_symptom_event(record: RunRecord, target: str) -> Optional[NodeEvent]:
    """The node where the failure actually surfaced, if not the target itself.

    Only attributes a crash to `target` when the crashed node is actually
    reachable from it in the graph — an unrelated crash elsewhere in the run
    (a parallel branch, an unconnected later node) is not evidence of what
    `target` broke, even if it happens to be the last thing that crashed.
    """
    crashed = [e for e in record.steps if e.status == "crashed"]
    downstream_crashes = [
        e for e in crashed if e.node_name != target and _is_reachable(record, target, e.node_name)
    ]
    if downstream_crashes:
        return max(downstream_crashes, key=lambda e: e.step_index)

    # Same constraint applies to both fallbacks below: they were written
    # assuming target is the chain origin, where first_failure_step / the
    # last hop of a real propagation path naturally sit downstream. Under
    # a --node override to a non-origin node, an unconstrained fallback
    # can point *upstream* of target — exactly the false-exoneration bug
    # the reachability check above exists to prevent, just via a
    # different path.
    if (
        record.first_failure_step
        and record.first_failure_step != target
        and _is_reachable(record, target, record.first_failure_step)
    ):
        return _find_node_event(record, record.first_failure_step)

    path = _propagation_nodes(record, target)
    if len(path) > 1 and path[-1] != target and _is_reachable(record, target, path[-1]):
        return _find_node_event(record, path[-1])

    return None


# session.py overwrites root_cause_chain with degradation_origins only
# when the top origin's confidence is at least this. Must stay in sync.
_CORRELATION_CHAIN_OVERRIDE_MIN = 0.8


def _chain_is_ranked_origins(record: RunRecord) -> bool:
    """True when root_cause_chain is confidence-ranked onsets, not a path.

    ``session.py`` replaces the inspector walk with
    ``[o.node_name for o in correlation.degradation_origins]`` once the
    top origin is high-confidence. Those names are independent onset
    candidates — using the last one as the crash site, or joining them
    with ``→``, invents a propagation that was never recorded.
    """
    corr = record.correlation
    if corr is None or not corr.degradation_origins:
        return False
    if corr.degradation_origins[0].confidence < _CORRELATION_CHAIN_OVERRIDE_MIN:
        return False
    return list(record.root_cause_chain or []) == [o.node_name for o in corr.degradation_origins]


def _propagation_nodes(record: RunRecord, target: str) -> list[str]:
    """Origin→symptom node list, or empty when we only have ranked onsets."""
    if not _chain_is_ranked_origins(record):
        return list(record.root_cause_chain or [])

    corr = record.correlation
    if corr is None:
        return []

    matching = [
        list(chain.nodes)
        for chain in corr.propagation_chains
        if target in chain.nodes and len(chain.nodes) > 1
    ]
    if not matching:
        return []
    for nodes in matching:
        if nodes[0] == target:
            return nodes
    return matching[0]


def _has_failure_signal(event: NodeEvent) -> bool:
    """Whether this event carries anything worth writing a fix prompt about."""
    if event.status in _FAILING_STATUSES or event.exception:
        return True
    if event.anomaly_signals:
        return True
    if event.semantic_check is not None and not event.semantic_check.passed:
        return True

    insp = event.inspection
    if insp is None:
        return False
    return bool(
        insp.is_silent_failure
        or insp.missing_fields
        or insp.empty_fields
        or insp.type_mismatches
        or insp.tool_failures
        or insp.semantic_signals
        or insp.degraded_fields
        or insp.suspicious_empty_keys
    )


# ── Source resolution ─────────────────────────────────────────────────────────


def _resolve_source(
    record: RunRecord,
    node_name: str,
    _cache: Optional[dict] = None,
) -> tuple[Optional[str], str]:
    """Resolve ``file.py:line`` for a node. Returns (path, note).

    ``node_fn_paths`` is recorded relative to the cwd at capture time, so a
    prompt generated from a different directory may hold a path that does not
    resolve. When that happens we re-anchor by basename under the current tree
    and fall back to a note rather than emitting a path that leads nowhere.
    """
    recorded = (record.node_fn_paths or {}).get(node_name)

    if not recorded:
        # locate_node_sources resolves every node in one project scan, so the
        # result is cached across calls — the target and the symptom node
        # would otherwise each pay a full grep + AST walk of the project.
        if _cache is not None and "resolved" in _cache:
            recorded = _cache["resolved"].get(node_name)
        else:
            try:
                # use_llm=False keeps the offline guarantee — the locator's
                # step 4 is an LLM call and its default is use_llm=True.
                resolved = _locate_offline(record)
            except Exception:
                resolved = {}
            if _cache is not None:
                _cache["resolved"] = resolved
            recorded = resolved.get(node_name)

    if not recorded:
        return None, ""

    # Strip a trailing ":<line>" only when the tail really is a line number.
    # Splitting on the first colon breaks "C:\...\file.py:42", and splitting
    # on the last breaks "C:\...\file.py" (no line) — both are recorded
    # shapes, since watcher.py stores absolute paths when relpath fails
    # across Windows drives.
    file_part = recorded
    head, _, tail = recorded.rpartition(":")
    if head and tail.isdigit():
        file_part = head
    if Path(file_part).exists():
        return recorded, ""

    reanchored = _reanchor(file_part)
    if reanchored:
        # The recorded line number belongs to a different checkout — carrying
        # it over would point the agent at an unrelated line. Recompute it
        # against the file we actually found; drop it if we cannot.
        fn_name = _fn_name_for(record, node_name)
        relocated = _find_line(Path(reanchored), fn_name)
        suffix = f":{relocated}" if relocated else ""
        return f"{reanchored}{suffix}", ""

    note = (
        f"> Path recorded as `{recorded}`, relative to the directory the run was "
        "captured in. It does not resolve from the current directory — locate "
        f"`{Path(file_part).name}` in the project before editing."
    )
    return recorded, note


def _locate_offline(record: RunRecord) -> dict:
    from argus.source_locator import locate_node_sources

    return locate_node_sources(record, use_llm=False)


def _fn_name_for(record: RunRecord, node_name: str) -> str:
    """The function implementing a node — from node_fn_refs, else the node name."""
    fn_ref = (record.node_fn_refs or {}).get(node_name, "")
    return fn_ref.split(":")[-1] if fn_ref else node_name


def _find_line(path: Path, fn_name: str) -> Optional[int]:
    """Line of ``def fn_name`` in a file, via source_locator's AST helper."""
    try:
        from argus.source_locator import _find_function_line

        return _find_function_line(path, fn_name)
    except Exception:
        return None


def _reanchor(file_part: str) -> Optional[str]:
    """Find a recorded file by basename under cwd. Unique match only."""
    name = Path(file_part).name
    if not name.endswith(".py"):
        return None

    # Reuse source_locator's noise-directory list — without it, a repo's own
    # .venv/node_modules/site-packages can shadow the real match with a
    # same-named vendored file, or make a genuinely unique match look
    # ambiguous.
    from argus.source_locator import _EXCLUDE_DIRS

    def _is_noise(parts: tuple) -> bool:
        # Mirror source_locator's walk rule: its named excludes *and* every
        # dot-directory (.pytest_cache, .direnv, .cache, ...), not just the
        # ones that happen to be listed.
        return any(part in _EXCLUDE_DIRS or part.startswith(".") for part in parts[:-1])

    matches = sorted(
        p for p in Path.cwd().rglob(name) if not _is_noise(p.relative_to(Path.cwd()).parts)
    )
    if len(matches) != 1:
        return None
    try:
        return str(matches[0].relative_to(Path.cwd()))
    except ValueError:
        return str(matches[0])


# ── Evidence selection ────────────────────────────────────────────────────────


def _fields_of_interest(event: NodeEvent) -> list[str]:
    """Ordered, de-duplicated field names the failure actually implicates.

    Emitting the whole state buries the signal for a smaller model, so evidence
    is narrowed to the fields the diagnostics name.
    """
    out: list[str] = []

    def add(name: Any) -> None:
        if isinstance(name, str) and name and name not in out:
            out.append(name)

    insp = event.inspection
    if insp is not None:
        for f in insp.missing_fields:
            add(f)
        for f in insp.empty_fields:
            add(f)
        for m in insp.type_mismatches:
            add(m.field_name)
        for tf in insp.tool_failures:
            add(tf.field_name)
        for ss in insp.semantic_signals:
            if ss.field_path:
                add(ss.field_path[0])
        for f in insp.degraded_fields:
            add(f)
        for f in insp.suspicious_empty_keys:
            add(f)

    for a in event.anomaly_signals:
        if a.field_path:
            add(a.field_path.split(".")[0])

    for f in _fields_from_exception(event.exception):
        add(f)

    return out


def _fields_from_exception(exc: Optional[str]) -> list[str]:
    """Pull key/attribute names out of a KeyError / AttributeError traceback."""
    if not exc:
        return []
    found: list[str] = []
    for pattern in (
        r"KeyError: '([^']+)'",
        r'KeyError: "([^"]+)"',
        r"AttributeError: .*? has no attribute '([^']+)'",
    ):
        for m in re.finditer(pattern, exc):
            if m.group(1) not in found:
                found.append(m.group(1))
    return found


def _render_state(
    state: Optional[dict],
    fields: list[str],
    *,
    sanitized: bool,
) -> Optional[str]:
    """Render the implicated fields of a state dict, plus a note on the rest."""
    if not state:
        return None

    present = [f for f in fields if f in state]
    if not present:
        # Nothing named — show a bounded sample so the agent still sees shape.
        present = list(state.keys())[:6]

    if sanitized:
        lines = [f"{_inline(k, 80)}: {_describe_shape(state[k])}" for k in present]
        block = _fence("\n".join(lines))
    else:
        subset = {k: state[k] for k in present}
        body, truncated = _dumps(subset)
        # A truncated dump is not valid JSON — fencing it as ```json invites
        # the agent to parse it and treat the parse failure as the bug it was
        # asked to diagnose.
        block = _fence(body, "text" if truncated else "json")

    remaining = [k for k in state.keys() if k not in present]
    if remaining:
        keys = ", ".join(_inline(k, 80) for k in remaining)
        block += f"\n\nOther keys present: {keys}"
    return block


def _fence(content: str, lang: str = "") -> str:
    """Wrap content in a code fence it cannot break out of.

    Recorded values can contain triple backticks. CommonMark closes a fence
    only on a run of at least as many backticks as opened it, so the fence is
    sized one longer than the longest run inside the payload. Without this, a
    traceback or tool response containing "```" ends the block early and its
    remainder is read as top-level instructions by whatever agent the prompt
    is pasted into.
    """
    longest = max((len(run) for run in re.findall(r"`+", content)), default=0)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{content}\n{ticks}"


def _inline(text: Any, limit: int = 200) -> str:
    """Neutralize untrusted text for interpolation into prose.

    Everything ARGUS records — state values, tool responses, LLM output,
    exception messages — is attacker-influenceable in a real pipeline, and
    this prompt is written to be pasted into a coding agent. Collapsing to a
    single line removes the line starts that ``#``/``-``/``>`` need to form
    markdown blocks; neutralizing backticks stops inline-code escapes.
    """
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        flat = flat[:limit] + "…"
    return flat.replace("`", "'")


def _dumps(value: Any) -> tuple[str, bool]:
    """Deterministic JSON rendering. Returns (text, was_truncated)."""
    try:
        text = json.dumps(value, indent=2, default=repr)
    except (TypeError, ValueError):
        text = repr(value)
    if len(text) > _MAX_VALUE_CHARS:
        return text[:_MAX_VALUE_CHARS] + "\n... (truncated)", True
    return text, False


def _describe_shape(value: Any) -> str:
    """Type/shape description with no underlying values — for --sanitized."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return type(value).__name__
    if isinstance(value, str):
        return f"str ({len(value)} chars)" if value else "str (empty)"
    if isinstance(value, (list, tuple)):
        return f"{type(value).__name__} ({len(value)} items)"
    if isinstance(value, dict):
        keys = ", ".join(_inline(k, 40) for k in list(value.keys())[:8])
        return f"dict (keys: {keys})" if keys else "dict (empty)"
    return type(value).__name__


# ── Problem description ───────────────────────────────────────────────────────


def _successors(record: RunRecord, node_name: str) -> list[str]:
    return list((record.graph_edge_map or {}).get(node_name, []))


def _degraded_upstream(event: NodeEvent) -> Optional[str]:
    """The node that failed to produce what this one needed, if any."""
    insp = event.inspection
    if insp is None or not insp.degraded_fields:
        return None
    return insp.degraded_upstream_node


def _is_reachable(record: RunRecord, source: str, dest: str) -> bool:
    """BFS over graph_edge_map: is `dest` downstream of `source`?

    Without this check, a crash anywhere in the run (an unrelated parallel
    branch, an unconnected later node) would otherwise look like evidence of
    what `source` broke.
    """
    edges = record.graph_edge_map or {}
    if not edges:
        return False
    seen = {source}
    queue = list(edges.get(source, []))
    while queue:
        node = queue.pop()
        if node == dest:
            return True
        if node in seen:
            continue
        seen.add(node)
        queue.extend(edges.get(node, []))
    return False


def _join_names(names: list[str]) -> str:
    """'`a`', '`a` and `b`', '`a`, `b` and `c`' — reads as prose, not a dump."""
    ticked = [f"`{n}`" for n in names]
    if not ticked:
        return ""
    if len(ticked) == 1:
        return ticked[0]
    return ", ".join(ticked[:-1]) + f" and {ticked[-1]}"


def _headline(record: RunRecord, target: str, event: NodeEvent, *, sanitized: bool) -> str:
    """One-line symptom for the title. Most concrete signal wins."""
    insp = event.inspection

    if event.status == "crashed" and event.exception:
        label = (
            _exception_type_name(event.exception)
            if sanitized
            else _exception_label(event.exception)
        )
        return f"`{target}` raises {label} and stops the pipeline"

    if insp is not None:
        critical_tools = [tf for tf in insp.tool_failures if tf.severity == "critical"]
        if critical_tools:
            tf = critical_tools[0]
            plain = _TOOL_FAILURE_PLAIN.get(tf.failure_type, "the external call failed")
            return f"`{target}` returns a result even though {plain}"
        if insp.missing_fields:
            succ = _successors(record, target)
            field = _inline(insp.missing_fields[0], 80)
            if len(succ) == 1:
                return f"`{target}` does not produce the `{field}` field that `{succ[0]}` needs"
            return f"`{target}` does not produce the `{field}` field"
        if insp.empty_fields:
            return f"`{target}` produces an empty `{_inline(insp.empty_fields[0], 80)}`"
        if insp.type_mismatches:
            m = insp.type_mismatches[0]
            return (
                f"`{target}` produces `{_inline(m.field_name, 80)}` as "
                f"{_inline(m.actual_type, 60)} instead of "
                f"{_inline(m.expected_type, 60)}"
            )
        if insp.semantic_signals:
            ss = insp.semantic_signals[0]
            where = _inline(ss.dotted_path, 80) or "its output"
            return f"`{target}` produces unusable content in `{where}`"
        if insp.degraded_fields:
            upstream = insp.degraded_upstream_node or "an earlier node"
            return (
                f"`{target}` runs without the "
                f"`{_inline(insp.degraded_fields[0], 80)}` field from `{upstream}`"
            )

    if event.anomaly_signals:
        return f"`{target}` behaves unexpectedly: {_inline(event.anomaly_signals[0].reason)}"

    plain = _STATUS_PLAIN.get(event.status, "produced an unexpected result")
    return f"`{target}` {plain}"


def _exception_label(exc: str) -> str:
    """Last traceback line, trimmed to the exception type and message.

    Neutralized: this lands in the H1 title and in prose, and the message
    half is frequently attacker-influenceable recorded data.
    """
    lines = [ln.strip() for ln in exc.strip().splitlines() if ln.strip()]
    if not lines:
        return "an error"
    return _inline(lines[-1], 120)


# An exception line is "SomeError: message" or "pkg.mod.SomeError: message".
# Anchored to the start and requiring the colon immediately after a dotted
# identifier, so traceback frame lines ("File \"x.py\", line 2, in f") and
# bare continuation lines never match.
_EXC_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*)\s*:")


def _exception_type_name(exc: str) -> str:
    """Just the exception class name (e.g. ``KeyError``) — safe to show under
    ``--sanitized``, unlike the full message, whose argument is frequently
    the literal value that triggered the exception (``KeyError: '<value>'``,
    ``ValueError: invalid literal ... '<value>'``).

    Scans upward for the last line that actually *looks* like an exception
    line. Taking the final line and splitting on ":" is not safe: a
    multi-line message (pydantic's ``Input should be a valid integer
    [input_value='...']``) leaves the recorded value as the last line, and a
    colon-free line would then be returned whole.
    """
    lines = [ln.strip() for ln in exc.strip().splitlines() if ln.strip()]
    for line in reversed(lines):
        m = _EXC_LINE_RE.match(line)
        if m:
            # Drop any module path — "pkg.mod.ValidationError" → "ValidationError".
            return m.group(1).rsplit(".", 1)[-1]
    return "an error"


def _what_went_wrong(
    record: RunRecord,
    target: str,
    event: NodeEvent,
    *,
    sanitized: bool,
) -> list[str]:
    """Plain-English paragraphs. No enum names, no signal ids.

    Under --sanitized, none of the free-text fields that can carry a real
    recorded value (ToolFailure.evidence, FieldMismatch.actual_value_repr,
    SemanticSignal.evidence) are rendered.
    """
    paras: list[str] = []
    insp = event.inspection

    if event.status == "crashed":
        below = "The exception type is below." if sanitized else "The traceback is below."
        paras.append(f"`{target}` raised an error and stopped. {below}")
    elif event.status == "degraded_input" and insp is not None:
        upstream = insp.degraded_upstream_node or "an earlier node"
        fields = ", ".join(f"`{_inline(f, 80)}`" for f in insp.degraded_fields) or "a field"
        paras.append(
            f"`{target}` ran without {fields}, because `{upstream}` never "
            "produced it. It did not raise an error — it carried on with "
            "incomplete input."
        )
    else:
        paras.append(f"`{target}` ran without raising an error, but its output is wrong.")

    if insp is None:
        return paras

    for tf in insp.tool_failures:
        plain = _TOOL_FAILURE_PLAIN.get(tf.failure_type, "the external call failed")
        detail = "" if sanitized else (f" ({_inline(tf.evidence)})" if tf.evidence else "")
        paras.append(
            f"While producing `{_inline(tf.field_name, 80)}`, {plain}{detail}. The "
            "result was kept and passed on as if the call had succeeded."
        )

    if insp.missing_fields:
        # missing_fields is a union over every successor's schema and the
        # per-successor attribution is discarded upstream, so naming a
        # specific successor as the reader would be a guess. With exactly one
        # successor it is unambiguous; otherwise stay accurate and unnamed.
        succ = _successors(record, target)
        for field in insp.missing_fields:
            if len(succ) == 1:
                paras.append(
                    f"The next node (`{succ[0]}`) reads "
                    f"`state['{_inline(field, 80)}']`, but this node's output "
                    f"has no `{_inline(field, 80)}` key."
                )
            else:
                paras.append(
                    f"A later node reads `state['{_inline(field, 80)}']`, but "
                    f"this node's output has no `{_inline(field, 80)}` key."
                )

    for field in insp.empty_fields:
        paras.append(f"`{_inline(field, 80)}` is present in the output but empty.")

    for field in insp.suspicious_empty_keys:
        paras.append(
            f"`{_inline(field, 80)}` was written as an empty value, which will degrade "
            "whatever reads it downstream."
        )

    for m in insp.type_mismatches:
        head = (
            f"`{_inline(m.field_name, 80)}` should be {_inline(m.expected_type, 60)}, "
            f"but it is {_inline(m.actual_type, 60)}"
        )
        if sanitized:
            paras.append(f"{head}.")
        else:
            paras.append(f"{head} ({_inline(m.actual_value_repr)}).")

    for ss in insp.semantic_signals:
        where = _inline(ss.dotted_path, 80) or "the output"
        evidence = (
            "" if sanitized else (f' Example: "{_inline(ss.evidence)}".' if ss.evidence else "")
        )
        paras.append(f"In `{where}`: {_inline(ss.description)}.{evidence}")

    for a in event.anomaly_signals:
        # expected_behavior is a declared threshold/profile and reason is a
        # fixed label, but observed_behavior can carry content lifted from
        # the output (anomaly_detector assigns worst_reason to it).
        expected = _inline(a.expected_behavior)
        if sanitized:
            paras.append(f"Expected {expected}, but the output did not match.")
        else:
            paras.append(f"Expected {expected}, but observed {_inline(a.observed_behavior)}.")

    return paras


def _done_when(
    record: RunRecord,
    target: str,
    event: NodeEvent,
    symptom_event: Optional[NodeEvent],
    *,
    sanitized: bool,
) -> list[str]:
    """Checkable success conditions. This is what makes the prompt verifiable."""
    conds: list[str] = []
    insp = event.inspection

    def label(exc: str) -> str:
        return _exception_type_name(exc) if sanitized else _exception_label(exc)

    if event.status == "crashed" and event.exception:
        conds.append(f"`{target}` runs to completion without raising {label(event.exception)}.")

    if insp is not None:
        for tf in insp.tool_failures:
            plain = _TOOL_FAILURE_CONDITION.get(tf.failure_type, "the external call fails")
            conds.append(
                f"When {plain}, `{target}` either raises or retries — it never "
                f"returns `{_inline(tf.field_name, 80)}` as a successful empty result."
            )
        for field in insp.missing_fields:
            conds.append(f"`{target}`'s output dict contains a `{_inline(field, 80)}` key.")
        for field in insp.empty_fields:
            conds.append(f"`{target}`'s `{_inline(field, 80)}` value is non-empty.")
        for m in insp.type_mismatches:
            conds.append(f"`{_inline(m.field_name, 80)}` is {_inline(m.expected_type, 60)}.")
        for ss in insp.semantic_signals:
            where = _inline(ss.dotted_path, 80) or "the output"
            # ss.description is registry prose; the raw category slug is an
            # internal enum and reads as broken grammar in a success criterion.
            conds.append(f"`{where}` no longer shows this problem: {_inline(ss.description)}.")
        # NOTE: degraded_fields deliberately produces no condition here. The
        # fix for a degraded node is in the upstream node that failed to
        # produce the field — but Constraints forbids editing other nodes, so
        # emitting "receives a usable `x` from `upstream`" as a success
        # criterion would be unsatisfiable as written. The upstream origin is
        # surfaced in "What to do" instead.

    if symptom_event is not None and symptom_event.exception:
        conds.append(
            f"`{symptom_event.node_name}` no longer fails with {label(symptom_event.exception)}."
        )

    if not conds:
        conds.append(f"`{target}` produces the output the next node expects.")
    return conds


# ── Rendering ─────────────────────────────────────────────────────────────────


def _render(
    *,
    record: RunRecord,
    target: str,
    event: NodeEvent,
    source_path: Optional[str],
    path_note: str,
    symptom_event: Optional[NodeEvent],
    sanitized: bool,
    source_cache: Optional[dict] = None,
) -> str:
    fn_name = _fn_name_for(record, target)
    parts: list[str] = []

    # 1 — Objective. First, so it survives truncation in a small context.
    parts.append(f"# Fix: {_headline(record, target, event, sanitized=sanitized)}")

    # 2 — Location.
    if source_path:
        parts.append(f"**Edit this file:** `{source_path}` — function `{fn_name}`")
    else:
        parts.append(
            f"**Edit the function that implements the `{target}` node.** "
            "Its source file was not recorded — search the project for a "
            f"function named `{fn_name}`."
        )
    if path_note:
        parts.append(path_note)

    # 3 — What to do / what went wrong.
    parts.append("## What to do")
    upstream = _degraded_upstream(event)
    if upstream and upstream != target:
        # This node is a victim, not the origin — say so plainly rather than
        # asking for a fix here that only an upstream change can deliver.
        parts.append(
            f"`{target}` is not where the bug is. It received incomplete input "
            f"because `{upstream}` did not produce what it needed. Fix "
            f"`{upstream}` instead — run `argus fix {record.run_id} --node "
            f"{upstream}` for a prompt targeting it. Only change `{target}` if "
            f"you have confirmed `{upstream}` is already correct."
        )
    else:
        parts.append(
            f"Fix the `{target}` node so the problem described below cannot happen "
            "again. Change the behaviour, not the symptom."
        )

    parts.append("## What went wrong")
    parts.extend(_what_went_wrong(record, target, event, sanitized=sanitized))

    # 4 — Evidence.
    evidence = _evidence_section(record, target, event, symptom_event, sanitized)
    if evidence:
        parts.append("## Evidence")
        parts.extend(evidence)

    # 5 — Causal note. Only when the failure surfaced somewhere else.
    if symptom_event is not None:
        parts.append("## Why this file and not the crash site")
        parts.extend(
            _causal_section(
                record,
                target,
                symptom_event,
                sanitized=sanitized,
                source_cache=source_cache,
            )
        )

    # 6 — Done when.
    parts.append("## Done when")
    conds = _done_when(record, target, event, symptom_event, sanitized=sanitized)
    parts.append("\n".join(f"- {c}" for c in conds))

    # 7 — Constraints.
    parts.append("## Constraints")
    parts.append("\n".join(_constraints(record, target, symptom_event)))

    # 8 — Verify.
    parts.append("## Verify")
    parts.append(f"```bash\nargus replay {record.run_id} {target}\n```")
    verify_note = (
        "This re-runs the pipeline from this node using the recorded input and "
        "reports whether the failure is gone."
    )
    if not record.node_fn_refs and not record.app_factory_ref:
        verify_note += (
            " This run has no stored factory-free replay refs — if the command "
            "above asks for one, add `--app module.path:factory_fn` (a zero-arg "
            "callable returning your graph)."
        )
    parts.append(verify_note)

    return "\n\n".join(parts).rstrip() + "\n"


def _evidence_section(
    record: RunRecord,
    target: str,
    event: NodeEvent,
    symptom_event: Optional[NodeEvent],
    sanitized: bool,
) -> list[str]:
    out: list[str] = []
    fields = _fields_of_interest(event)

    rendered_in = _render_state(event.input_state, fields, sanitized=sanitized)
    if rendered_in:
        out.append(f"Input `{target}` received:")
        out.append(rendered_in)

    rendered_out = _render_state(event.output_dict, fields, sanitized=sanitized)
    if rendered_out:
        out.append(f"Output `{target}` produced:")
        out.append(rendered_out)
    elif event.output_dict is None:
        if event.status == "crashed":
            out.append(f"`{target}` produced no output — it raised before returning.")
        else:
            out.append(f"`{target}` returned no output dict at all.")
    else:
        # An empty-but-present dict is falsy, so _render_state declines it —
        # but "returned {}" is precisely the silent failure being reported.
        out.append(f"Output `{target}` produced: `{{}}` — an empty dict, no keys at all.")

    if event.exception:
        out.append(f"`{target}` raised:")
        if sanitized:
            out.append(
                f"`{_exception_type_name(event.exception)}` "
                "(message omitted — may contain a recorded value)."
            )
        else:
            out.append(_fence(event.exception.strip()))

    if symptom_event is not None and symptom_event.exception:
        position = _relative_position(record, target, symptom_event.node_name)
        out.append(f"{position}, `{symptom_event.node_name}` crashed:")
        if sanitized:
            out.append(
                f"`{_exception_type_name(symptom_event.exception)}` "
                "(message omitted — may contain a recorded value)."
            )
        else:
            out.append(_fence(symptom_event.exception.strip()))

    if sanitized:
        out.append(
            "_Values omitted — field names and shapes only. Re-run without "
            "`--sanitized` to include recorded values._"
        )
    return out


def _relative_position(record: RunRecord, target: str, symptom: str) -> str:
    """'Two nodes later' — reads better than raw step indices for an agent.

    Measured in graph hops. root_cause_chain holds only the nodes that were
    flagged as failing, so counting positions in it would call two nodes
    three hops apart "immediately after" each other.
    """
    hops = _hop_distance(record, target, symptom)
    if hops == 1:
        return "Immediately after"
    if hops and hops > 1:
        return f"{hops} nodes later"
    return "Later in the run"


def _hop_distance(record: RunRecord, source: str, dest: str) -> Optional[int]:
    """Shortest edge count from source to dest, or None if unreachable."""
    edges = record.graph_edge_map or {}
    if not edges:
        return None
    frontier = [source]
    seen = {source}
    depth = 0
    while frontier:
        depth += 1
        nxt = []
        for node in frontier:
            for succ in edges.get(node, []):
                if succ == dest:
                    return depth
                if succ not in seen:
                    seen.add(succ)
                    nxt.append(succ)
        frontier = nxt
    return None


def _causal_section(
    record: RunRecord,
    target: str,
    symptom_event: NodeEvent,
    *,
    sanitized: bool,
    source_cache: Optional[dict] = None,
) -> list[str]:
    out: list[str] = []
    symptom = symptom_event.node_name
    # Route through the same exists-check/reanchor/note logic used for the
    # primary target — node_fn_paths is exactly as likely to be stale here.
    symptom_path, symptom_note = _resolve_source(record, symptom, source_cache)
    where = f"`{symptom_path}`" if symptom_path else f"the `{symptom}` node"

    path = _propagation_nodes(record, target)
    if len(path) > 1 and target in path and symptom in path:
        narrative = " → ".join(f"`{n}`" for n in path)
        out.append(
            f"The failure surfaced in {where}, but that is not the bug. It "
            f"propagated: {narrative}."
        )
    else:
        out.append(f"The failure surfaced in {where}, but that is not the bug.")
    if symptom_note:
        out.append(symptom_note)

    # Scoped to the specific symptom node only — not "every other chain
    # node" (root_cause_chain's ordering isn't guaranteed and doesn't
    # reliably include the crash site), and not a blanket claim that
    # everything else in the run is correct.
    out.append(
        f"**Do not edit `{symptom}`.** It is behaving correctly given the "
        f"input it received — the bug is upstream. Fix `{target}` only."
    )

    if not sanitized and record.correlation and record.correlation.causal_summary:
        out.append(f"Recorded analysis: {_inline(record.correlation.causal_summary, 400)}")

    return out


def _constraints(
    record: RunRecord,
    target: str,
    symptom_event: Optional[NodeEvent],
) -> list[str]:
    lines = [f"- Change only the `{target}` node's function."]
    lines.append("- Do not change the pipeline's state schema, key names, or the graph wiring.")
    lines.append("- Do not edit other node functions.")
    if symptom_event is not None:
        lines.append(
            f"- Do not add defensive guards in `{symptom_event.node_name}` to "
            "hide the missing data — fix the source."
        )
    return lines
