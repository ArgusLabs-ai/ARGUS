"""Root-cause correlation layer for ARGUS.

Analyzes a completed RunRecord and produces a CorrelationReport containing:
  - degradation origin detection (where did signals first appear)
  - propagation chain tracing (how did degradation spread)
  - causal summary (developer-readable narrative)
  - execution timeline (per-step event tags)
  - replay impact (if run has a parent)

All logic is deterministic — no LLM calls.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

from argus.models import (
    CorrelationReport,
    DegradationOrigin,
    NodeEvent,
    PropagationChain,
    PropagationLink,
    ReplayImpact,
    RunRecord,
    TimelineEvent,
)

# ── Signal weighting ───────────────────────────────────────────────────────────

_PLACEHOLDER_PREFIXES = ("PH-", "NL-")

# Single source of truth for signal weights. Both the scorer
# (_node_signal_weight / _node_structural_weight) and the human-facing
# _confidence_breakdown read these — change a weight here and both stay in sync.
_W_TOOL_CRITICAL = 3.0
_W_TOOL_WARNING = 1.5
_W_MISSING_FIELD = 0.5
_W_ANOMALY_CRITICAL = 2.0
_W_ANOMALY = 0.8
_W_CRASH_FLOOR = 2.0
_W_BEHAVIORAL_ONLY_CAP = 0.4
_ANOMALY_CRITICAL_THRESHOLD = 0.7

# Statuses that mean a node did not really contribute an execution (a retried
# attempt that was superseded, or a skipped branch). Defined once, used wherever
# events are filtered.
_INACTIVE_STATUSES = frozenset({"retried", "skipped"})


def _node_signal_weight(event: NodeEvent) -> float:
    """Assign a numeric degradation weight to a node's collected signals.

    NOTE: semantic_signals are NOT counted separately because they are
    already represented as tool_failures via Rule 7 of inspect_tool_outputs.
    Counting both would double-count the same underlying heuristic detection.

    NOTE: empty_fields are excluded from weight. They represent optional
    fields that are absent or empty in the accumulated state — in
    sequential pipelines this is usually "not yet produced" rather than
    "dropped". Only missing_fields (required fields) contribute weight.
    """
    weight = 0.0
    insp = event.inspection
    if insp is not None:
        for tf in insp.tool_failures:
            weight += _W_TOOL_CRITICAL if tf.severity == "critical" else _W_TOOL_WARNING
        # semantic_signals intentionally excluded — already in tool_failures
        weight += len(insp.missing_fields) * _W_MISSING_FIELD
        # empty_fields intentionally excluded — informational only
    for anomaly in event.anomaly_signals:
        weight += (
            _W_ANOMALY_CRITICAL
            if anomaly.suspicion_score > _ANOMALY_CRITICAL_THRESHOLD
            else _W_ANOMALY
        )
    if event.status == "crashed":
        weight = max(weight, _W_CRASH_FLOOR)
    return round(weight, 3)


def _node_structural_weight(event: NodeEvent) -> float:
    """Signal weight from structural evidence only (no behavioral anomalies).

    Used for origin suppression: behavioral anomalies alone at an upstream
    node should NOT prevent a downstream crash from being identified as an
    independent origin. Only concrete structural evidence (tool failures,
    missing fields, crashes) counts.

    NOTE: semantic_signals excluded — already represented in tool_failures.
    NOTE: empty_fields excluded — informational only.
    """
    weight = 0.0
    insp = event.inspection
    if insp is not None:
        for tf in insp.tool_failures:
            weight += _W_TOOL_CRITICAL if tf.severity == "critical" else _W_TOOL_WARNING
        # semantic_signals intentionally excluded — already in tool_failures
        weight += len(insp.missing_fields) * _W_MISSING_FIELD
        # empty_fields intentionally excluded — informational only
    if event.status == "crashed":
        weight = max(weight, _W_CRASH_FLOOR)
    return round(weight, 3)


def _is_behavioral_only(event: NodeEvent) -> bool:
    """True if the node's signals are exclusively behavioral anomalies
    with no structural evidence (tool failures, semantic signals, missing fields).
    """
    insp = event.inspection
    has_structural = False
    if insp is not None:
        has_structural = bool(
            insp.tool_failures or insp.semantic_signals or insp.missing_fields or insp.empty_fields
        )
    return not has_structural and bool(event.anomaly_signals)


def _collect_signal_types(event: NodeEvent) -> list[str]:
    """Return a deduplicated list of high-level signal type labels for an event."""
    types: list[str] = []
    if event.inspection is not None:
        if event.inspection.tool_failures:
            types.append("tool_failure")
        for sig in event.inspection.semantic_signals:
            if sig.sig_id.startswith(_PLACEHOLDER_PREFIXES):
                types.append("placeholder")
            elif sig.sig_id.startswith(("CM-", "MP-")):
                types.append("corrupted_output")
            elif sig.sig_id.startswith("SP-"):
                types.append("suspicious_phrase")
            elif sig.sig_id.startswith("RF-"):
                types.append("repeated_filler")
            elif sig.sig_id.startswith("ES-"):
                types.append("empty_semantic")
        if event.inspection.missing_fields:
            types.append("missing_field")
    if event.anomaly_signals:
        types.append("behavioral_anomaly")
    if event.status == "crashed":
        types.append("crash")
    return list(dict.fromkeys(types))  # deduplicate preserving order


def _brief_signal_summary(event: NodeEvent) -> str:
    """Return a short (≤80 char) summary of the most prominent signal."""
    parts: list[str] = []
    insp = event.inspection
    if insp is not None:
        if insp.tool_failures:
            tf = insp.tool_failures[0]
            parts.append(f"tool {tf.failure_type}")
        if insp.missing_fields:
            parts.append(f"missing: {', '.join(insp.missing_fields[:2])}")
        if insp.semantic_signals:
            parts.append(insp.semantic_signals[0].sig_id)
    if event.anomaly_signals:
        parts.append(f"anomaly: {event.anomaly_signals[0].anomaly_id}")
    return "; ".join(parts) if parts else "no signals"


# ── Graph helpers ──────────────────────────────────────────────────────────────


def _build_reachable(
    edge_map: dict[str, list[str]],
    all_nodes: list[str],
) -> dict[str, set[str]]:
    """BFS transitive closure: {node: set of all downstream reachable nodes}."""
    reachable: dict[str, set[str]] = {}
    for node in all_nodes:
        visited: set[str] = set()
        queue = list(edge_map.get(node, []))
        while queue:
            nxt = queue.pop()
            if nxt in visited:
                continue
            visited.add(nxt)
            queue.extend(edge_map.get(nxt, []))
        reachable[node] = visited
    return reachable


def _event_by_name(events: list[NodeEvent]) -> dict[str, list[NodeEvent]]:
    result: dict[str, list[NodeEvent]] = defaultdict(list)
    for e in events:
        result[e.node_name].append(e)
    return dict(result)


def _compute_weights(events: list[NodeEvent]) -> dict[str, float]:
    """Max signal weight per node name (handles nodes that run multiple times)."""
    weights: dict[str, float] = {}
    for e in events:
        w = _node_signal_weight(e)
        if e.node_name not in weights or w > weights[e.node_name]:
            weights[e.node_name] = w
    return weights


def _compute_structural_weights(events: list[NodeEvent]) -> dict[str, float]:
    """Max structural weight per node name (excludes behavioral anomalies)."""
    weights: dict[str, float] = {}
    for e in events:
        w = _node_structural_weight(e)
        if e.node_name not in weights or w > weights[e.node_name]:
            weights[e.node_name] = w
    return weights


def _dedup_links(links: list[PropagationLink]) -> list[PropagationLink]:
    """For duplicate (source, target, signal_type), keep highest confidence."""
    best: dict[tuple[str, str, str], PropagationLink] = {}
    for link in links:
        key = (link.source_node, link.target_node, link.signal_type)
        if key not in best or link.confidence > best[key].confidence:
            best[key] = link
    return list(best.values())


# ── Origin detection ───────────────────────────────────────────────────────────


def _confidence_breakdown(
    event: NodeEvent, *, dampened: bool
) -> tuple[tuple[str, float], ...]:
    """List the signal weights that fed the origin confidence score."""
    items: list[tuple[str, float]] = []
    insp = event.inspection
    if insp is not None:
        crit = sum(1 for tf in insp.tool_failures if tf.severity == "critical")
        warn = sum(1 for tf in insp.tool_failures if tf.severity != "critical")
        if crit:
            items.append(("tool_failure (critical)", round(crit * _W_TOOL_CRITICAL, 3)))
        if warn:
            items.append(("tool_failure (warning)", round(warn * _W_TOOL_WARNING, 3)))
        if insp.missing_fields:
            items.append(
                ("missing_fields", round(len(insp.missing_fields) * _W_MISSING_FIELD, 3))
            )
    for anomaly in event.anomaly_signals:
        if anomaly.suspicion_score > _ANOMALY_CRITICAL_THRESHOLD:
            items.append(("behavioral_anomaly (critical)", _W_ANOMALY_CRITICAL))
        else:
            items.append(("behavioral_anomaly", _W_ANOMALY))
    if event.status == "crashed":
        items.append(("crash", _W_CRASH_FLOOR))
    if dampened:
        items.append(("behavioral-only cap", _W_BEHAVIORAL_ONLY_CAP))
    return tuple(items)


def _find_degradation_origins(
    events: list[NodeEvent],
    edge_map: dict[str, list[str]],
) -> list[DegradationOrigin]:
    """Find nodes where degradation clearly begins (signal jump from clean predecessors).

    Uses structural weight (excluding behavioral anomalies) for predecessor
    suppression. Behavioral anomalies alone at an upstream node should not
    prevent a downstream crash from being identified as its own origin.

    Crash origins are prioritised over behavioral-anomaly-only origins.
    """
    weights = _compute_weights(events)
    structural_weights = _compute_structural_weights(events)

    # Map events by name for behavioral-only checks
    events_by_name: dict[str, NodeEvent] = {}
    for e in events:
        if e.node_name not in events_by_name:
            events_by_name[e.node_name] = e

    predecessor_map: dict[str, list[str]] = defaultdict(list)
    for src, dests in edge_map.items():
        for dst in dests:
            predecessor_map[dst].append(src)

    seen: set[str] = set()
    origins: list[DegradationOrigin] = []

    for event in sorted(events, key=lambda e: e.step_index):
        node = event.node_name
        if node in seen:
            continue
        seen.add(node)

        w = weights.get(node, 0.0)
        if w == 0.0:
            continue

        # Use structural weight for predecessor suppression — behavioral
        # anomalies alone should not suppress downstream origins.
        pred_structural = [structural_weights.get(p, 0.0) for p in predecessor_map.get(node, [])]
        max_pred_structural = max(pred_structural, default=0.0)

        # Origin criterion: structural signal jumped from clean predecessors
        # OR ≥50% of weight is new relative to predecessor structural weight
        is_jump = max_pred_structural == 0.0 or ((w - max_pred_structural) / w) >= 0.5
        if not is_jump:
            continue

        confidence = min(1.0, max(0.0, (w - max_pred_structural) / w))
        sig_types = _collect_signal_types(event)

        # Dampen confidence for behavioral-anomaly-only origins — these are
        # observations, not structural failures, and should not dominate
        # crash-based origins in the final ranking.
        if _is_behavioral_only(event):
            confidence = min(confidence, 0.40)

        dampened = _is_behavioral_only(event)

        if not predecessor_map.get(node):
            reason = f"first node in execution with degradation signals (weight {w:.1f})"
        elif max_pred_structural == 0.0:
            if _is_behavioral_only(event):
                reason = (
                    f"behavioral anomaly detected (weight {w:.1f}); "
                    f"no structural evidence of degradation"
                )
            else:
                reason = f"all predecessors were clean; degradation starts here (weight {w:.1f})"
        else:
            reason = (
                f"weight jumped from {max_pred_structural:.1f} "
                f"(predecessor structural max) to {w:.1f}; "
                f"likely degradation onset"
            )

        origins.append(
            DegradationOrigin(
                node_name=node,
                step_index=event.step_index,
                signal_types=tuple(sig_types),
                confidence=round(confidence, 3),
                reason=reason,
                confidence_breakdown=_confidence_breakdown(event, dampened=dampened),
            )
        )

    # Sort: highest confidence first; demote crash sites (symptoms, not causes)
    def _origin_sort_key(o: DegradationOrigin) -> tuple[float, int]:
        ev = events_by_name.get(o.node_name)
        is_crash = 0 if (ev and ev.status == "crashed") else 1
        return (o.confidence, is_crash)

    origins.sort(key=_origin_sort_key, reverse=True)
    return origins[:3]


# ── Propagation detectors ──────────────────────────────────────────────────────


def _detect_field_drop_cascade(
    events: list[NodeEvent],
    edge_map: dict[str, list[str]],
) -> list[PropagationLink]:
    """Detect missing/empty fields in node_i propagating to downstream failures.

    A field is only considered "dropped" if it was actually produced by some
    node up to and including node_i (i.e. it existed in the accumulated
    state) and then became missing/empty at node_j. Fields that were NEVER
    produced by anyone are "not yet produced" — the normal progressive
    state accumulation pattern in sequential pipelines — and are excluded
    from cascade detection.
    """
    links: list[PropagationLink] = []
    all_names = [e.node_name for e in events]
    reachable = _build_reachable(edge_map, all_names)
    by_name = _event_by_name(events)

    # Track which fields have been produced (non-empty) by any node up to
    # each step index. A field is only "droppable" after it has been produced.
    produced_by_step: dict[int, set[str]] = {}
    cumulative: set[str] = set()
    for event in sorted(events, key=lambda e: e.step_index):
        if event.output_dict:
            for k, v in event.output_dict.items():
                if v is not None and v != "" and v != [] and v != {}:
                    cumulative.add(k)
        produced_by_step[event.step_index] = set(cumulative)

    for event_i in events:
        if event_i.inspection is None:
            continue
        flagged = set(event_i.inspection.missing_fields + event_i.inspection.empty_fields)
        if not flagged:
            continue

        # Only consider fields that were actually produced before or at
        # this step. "Not yet produced" fields are excluded.
        produced_so_far = produced_by_step.get(event_i.step_index, set())
        dropped = flagged & produced_so_far
        if not dropped:
            continue

        for node_j in reachable.get(event_i.node_name, set()):
            for event_j in by_name.get(node_j, []):
                if event_j.step_index <= event_i.step_index:
                    continue
                j_insp = event_j.inspection
                j_missing = (
                    set(
                        getattr(j_insp, "missing_fields", []) + getattr(j_insp, "empty_fields", [])
                    )
                    if j_insp
                    else set()
                )

                overlap = dropped & j_missing
                if overlap:
                    field = next(iter(overlap))
                    links.append(
                        PropagationLink(
                            source_node=event_i.node_name,
                            target_node=event_j.node_name,
                            signal_type="field_drop",
                            confidence=0.85,
                            evidence=(f"field '{field}' dropped at source, missing at target"),
                        )
                    )

                exc = event_j.exception or ""
                for field in dropped:
                    if f"KeyError: '{field}'" in exc or f'KeyError: "{field}"' in exc:
                        links.append(
                            PropagationLink(
                                source_node=event_i.node_name,
                                target_node=event_j.node_name,
                                signal_type="field_drop",
                                confidence=0.95,
                                evidence=(
                                    f"field '{field}' dropped at source, caused KeyError at target"
                                ),
                            )
                        )
                        break

    return _dedup_links(links)


def _detect_placeholder_propagation(
    events: list[NodeEvent],
    edge_map: dict[str, list[str]],
) -> list[PropagationLink]:
    """Detect placeholder/null-like content flowing from upstream
    outputs into downstream inputs."""
    links: list[PropagationLink] = []
    all_names = [e.node_name for e in events]
    reachable = _build_reachable(edge_map, all_names)
    by_name = _event_by_name(events)

    for event_i in events:
        if event_i.inspection is None:
            continue
        ph_signals = [
            s
            for s in event_i.inspection.semantic_signals
            if s.sig_id.startswith(_PLACEHOLDER_PREFIXES) and len(s.evidence) > 4
        ]
        if not ph_signals:
            continue

        for node_j in reachable.get(event_i.node_name, set()):
            for event_j in by_name.get(node_j, []):
                if event_j.step_index <= event_i.step_index:
                    continue
                input_str = str(event_j.input_state or {})
                j_signals = event_j.inspection.semantic_signals if event_j.inspection else []
                linked = False
                for sig in ph_signals:
                    if sig.evidence and sig.evidence in input_str:
                        links.append(
                            PropagationLink(
                                source_node=event_i.node_name,
                                target_node=event_j.node_name,
                                signal_type="placeholder",
                                confidence=0.90,
                                evidence=(
                                    f"[{sig.sig_id}] text '{sig.evidence[:40]}' "
                                    f"found in downstream input"
                                ),
                            )
                        )
                        linked = True
                        break

                if not linked:
                    j_ph = [s for s in j_signals if s.sig_id.startswith(_PLACEHOLDER_PREFIXES)]
                    if j_ph:
                        links.append(
                            PropagationLink(
                                source_node=event_i.node_name,
                                target_node=event_j.node_name,
                                signal_type="semantic_collapse",
                                confidence=0.70,
                                evidence=(
                                    f"placeholder degradation in both nodes "
                                    f"({ph_signals[0].sig_id} → {j_ph[0].sig_id})"
                                ),
                            )
                        )

    return _dedup_links(links)


def _detect_anomaly_cascade(
    events: list[NodeEvent],
    edge_map: dict[str, list[str]],
) -> list[PropagationLink]:
    """Detect behavioral anomalies cascading from upstream to downstream nodes.

    Only creates links when the SAME anomaly ID appears in both source and
    target nodes — concrete evidence that the same behavioral degradation
    pattern manifests at both ends of the edge.

    Does NOT create speculative links based on unrelated anomaly types at
    downstream nodes (e.g. upstream has BA-001 and downstream has BA-006).
    Unrelated anomalies should be treated as independent observations, not
    causal propagation.
    """
    links: list[PropagationLink] = []
    all_names = [e.node_name for e in events]
    reachable = _build_reachable(edge_map, all_names)
    by_name = _event_by_name(events)

    for event_i in events:
        critical = [a for a in event_i.anomaly_signals if a.suspicion_score > 0.7]
        if not critical:
            continue
        source_ids = {a.anomaly_id for a in critical}

        for node_j in reachable.get(event_i.node_name, set()):
            for event_j in by_name.get(node_j, []):
                if event_j.step_index <= event_i.step_index:
                    continue
                j_ids = {a.anomaly_id for a in event_j.anomaly_signals}
                matching = source_ids & j_ids
                if matching:
                    mid = next(iter(matching))
                    links.append(
                        PropagationLink(
                            source_node=event_i.node_name,
                            target_node=event_j.node_name,
                            signal_type="anomaly_cascade",
                            confidence=0.65,
                            evidence=f"anomaly {mid} present in both nodes",
                        )
                    )

    return _dedup_links(links)


# ── Chain assembly ─────────────────────────────────────────────────────────────


def _classify_chain(chain_links: list[PropagationLink], origin_has_tool_failure: bool) -> str:
    if origin_has_tool_failure:
        return "tool_failure_cascade"
    if not chain_links:
        return "mixed_degradation"
    from collections import Counter

    counts = Counter(lnk.signal_type for lnk in chain_links)
    types_present = set(counts.keys())
    if len(types_present) >= 2:
        return "mixed_degradation"
    dominant = counts.most_common(1)[0][0]
    mapping = {
        "field_drop": "field_drop_cascade",
        "placeholder": "placeholder_propagation",
        "semantic_collapse": "semantic_collapse",
        "anomaly_cascade": "anomaly_cascade",
    }
    return mapping.get(dominant, "mixed_degradation")


_CHAIN_TEMPLATES = {
    "tool_failure_cascade": "{origin} returned tool failure; {downstream} degraded as a result",
    "semantic_collapse": "semantic degradation at {origin} propagated through {downstream}",
    "placeholder_propagation": (
        "placeholder output at {origin} flowed into downstream inputs at {downstream}"
    ),
    "field_drop_cascade": "{origin} dropped field(s); caused silent failure at {downstream}",
    "anomaly_cascade": "behavioral anomaly at {origin} cascaded into {downstream}",
    "mixed_degradation": "mixed degradation starting at {origin} affected {downstream}",
}


def _build_chain_summary(chain_type: str, nodes: list[str]) -> str:
    origin = nodes[0] if nodes else "unknown"
    downstream = nodes[1:] if len(nodes) > 1 else []
    ds_str = ", ".join(downstream) if downstream else "no downstream nodes"
    template = _CHAIN_TEMPLATES.get(chain_type, "degradation at {origin} affected {downstream}")
    return template.format(origin=origin, downstream=ds_str)


def _dfs_chain(
    current: str,
    by_source: dict[str, list[PropagationLink]],
    path: list[str],
    chain_links: list[PropagationLink],
    visited: set[str],
) -> None:
    visited.add(current)
    for link in by_source.get(current, []):
        if link.target_node not in visited:
            chain_links.append(link)
            path.append(link.target_node)
            _dfs_chain(link.target_node, by_source, path, chain_links, visited)


def _assemble_chains(
    links: list[PropagationLink],
    events: list[NodeEvent],
) -> list[PropagationChain]:
    if not links:
        return []

    by_name = _event_by_name(events)
    by_source: dict[str, list[PropagationLink]] = defaultdict(list)
    all_targets: set[str] = {lnk.target_node for lnk in links}
    for lnk in links:
        by_source[lnk.source_node].append(lnk)

    roots = [s for s in by_source if s not in all_targets]
    if not roots:
        roots = [links[0].source_node]

    chains: list[PropagationChain] = []
    for root in roots:
        path: list[str] = [root]
        chain_links: list[PropagationLink] = []
        _dfs_chain(root, by_source, path, chain_links, visited=set())

        root_events = by_name.get(root, [])
        origin_has_tf = any((e.inspection and e.inspection.has_tool_failure) for e in root_events)
        chain_type = _classify_chain(chain_links, origin_has_tf)
        summary = _build_chain_summary(chain_type, path)

        chains.append(
            PropagationChain(
                chain_type=chain_type,
                nodes=tuple(path),
                links=tuple(chain_links),
                summary=summary,
            )
        )

    chains.sort(key=lambda c: -sum(lnk.confidence for lnk in c.links))
    return chains


# ── Timeline ───────────────────────────────────────────────────────────────────


def _build_timeline(
    events: list[NodeEvent],
    origins: list[DegradationOrigin],
    chains: list[PropagationChain],
) -> list[TimelineEvent]:
    origin_names = {o.node_name for o in origins}
    chain_nodes = {n for c in chains for n in c.nodes[1:]}

    timeline: list[TimelineEvent] = []
    for event in sorted(events, key=lambda e: e.step_index):
        if event.status == "crashed":
            event_type = "crash"
            exc_first = (event.exception or "").splitlines()[0][:60]
            label = f"CRASH: {exc_first}" if exc_first else f"CRASH: {event.node_name}"
            signal_summary = "node raised an exception"
        elif event.node_name in origin_names:
            event_type = "degradation_onset"
            label = f"ORIGIN: degradation starts at {event.node_name}"
            signal_summary = _brief_signal_summary(event)
        elif event.node_name in chain_nodes:
            event_type = "propagation"
            label = f"PROPAGATION: degradation reached {event.node_name}"
            signal_summary = _brief_signal_summary(event)
        elif _node_signal_weight(event) == 0.0:
            event_type = "node_ok"
            label = f"{event.node_name}: clean"
            signal_summary = "no signals"
        else:
            event_type = "node_ok"
            label = f"{event.node_name}: minor signals"
            signal_summary = _brief_signal_summary(event)

        timeline.append(
            TimelineEvent(
                step_index=event.step_index,
                node_name=event.node_name,
                event_type=event_type,
                label=label,
                signal_summary=signal_summary,
            )
        )

    return timeline


# ── Causal summary ─────────────────────────────────────────────────────────────


def _build_causal_summary(
    origins: list[DegradationOrigin],
    chains: list[PropagationChain],
    overall_status: str,
) -> str:
    if overall_status == "clean" or (not origins and not chains):
        return "No degradation detected — all nodes passed cleanly."

    if not origins:
        return (
            f"Degradation detected across {len(chains)} propagation chain(s); "
            f"no single clear origin identified."
        )

    primary = origins[0]
    conf_pct = f"{primary.confidence:.0%}"

    # Flag low-confidence or behavioral-only origins
    is_low_confidence = primary.confidence < 0.5
    is_behavioral = (
        all(st == "behavioral_anomaly" for st in primary.signal_types)
        if primary.signal_types
        else False
    )

    if not chains:
        sigs = ", ".join(primary.signal_types) if primary.signal_types else "unknown"
        if is_behavioral:
            return (
                f"Behavioral anomaly observed at {primary.node_name} "
                f"(confidence: {conf_pct}). "
                f"Signals: {sigs}. "
                f"No structural propagation evidence found."
            )
        return (
            f"Degradation originated at {primary.node_name} "
            f"(confidence: {conf_pct}). "
            f"Signals: {sigs}. No downstream propagation detected."
        )

    chain = chains[0]
    downstream_count = len(chain.nodes) - 1
    chain_label = chain.chain_type.replace("_", " ")

    if is_low_confidence or is_behavioral:
        return (
            f"Possible degradation at {primary.node_name} "
            f"(confidence: {conf_pct}). "
            f"{chain.summary}. "
            f"Causal link is uncertain — "
            f"downstream failures may be independent."
        )

    return (
        f"Degradation originated at {primary.node_name} "
        f"(confidence: {conf_pct}). "
        f"{chain.summary}. "
        f"{downstream_count} downstream node(s) affected via {chain_label}."
    )


# ── Replay impact ──────────────────────────────────────────────────────────────

_FAILURE_STATUSES = frozenset({"fail", "crashed", "semantic_fail", "degraded_input"})


def _event_is_failure(event: NodeEvent | None) -> bool:
    if event is None:
        return True
    if event.status in _FAILURE_STATUSES:
        return True
    insp = event.inspection
    if insp is None:
        return False
    return bool(insp.is_silent_failure or insp.has_tool_failure or insp.missing_fields)


def _last_active_event(record: RunRecord, node_name: str) -> NodeEvent | None:
    events = [
        e
        for e in record.steps
        if e.node_name == node_name and e.status not in _INACTIVE_STATUSES
    ]
    return events[-1] if events else None


def _original_failure_node(original: RunRecord) -> str | None:
    if original.first_failure_step:
        return original.first_failure_step
    if original.root_cause_chain:
        return original.root_cause_chain[0]
    for event in original.steps:
        if event.status in _INACTIVE_STATUSES:
            continue
        if _event_is_failure(event):
            return event.node_name
    return None


def compare_replay(replay: RunRecord, original: RunRecord) -> ReplayImpact:
    """Compare replay signal weights against the original run to identify improvements."""
    orig_weights = _compute_weights(_active_events(original.steps))
    replay_weights = _compute_weights(_active_events(replay.steps))
    common = set(orig_weights) & set(replay_weights)

    improved_nodes = sorted(n for n in common if orig_weights[n] - replay_weights[n] > 0.5)
    regressed_nodes = sorted(n for n in common if replay_weights[n] - orig_weights[n] > 0.5)

    edge_map = replay.graph_edge_map or {}
    key_fix_node: str | None = None
    best_downstream = -1
    for node in improved_nodes:
        successors = edge_map.get(node, [])
        count = sum(
            1
            for s in successors
            if s in improved_nodes
            or (s in common and replay_weights.get(s, 0.0) < orig_weights.get(s, 0.0))
        )
        if count > best_downstream:
            best_downstream = count
            key_fix_node = node

    if not improved_nodes and not regressed_nodes:
        summary = "Replay produced no measurable change in signal weights."
    elif improved_nodes and not regressed_nodes:
        fix = f"Key fix: {key_fix_node}. " if key_fix_node else ""
        summary = f"{fix}{len(improved_nodes)} node(s) improved, none regressed."
    elif regressed_nodes and not improved_nodes:
        summary = (
            f"Replay introduced regressions at {len(regressed_nodes)} node(s) "
            f"with no improvements."
        )
    else:
        direction = "positive" if len(improved_nodes) > len(regressed_nodes) else "negative"
        summary = (
            f"{len(improved_nodes)} node(s) improved, {len(regressed_nodes)} regressed. "
            f"Net change is {direction}."
        )

    fail_node = _original_failure_node(original)
    resolved: bool | None = None
    if fail_node:
        orig_ev = _last_active_event(original, fail_node)
        replay_ev = _last_active_event(replay, fail_node)
        if orig_ev is not None and _event_is_failure(orig_ev):
            resolved = replay_ev is not None and not _event_is_failure(replay_ev)
            verdict = "resolved" if resolved else "not resolved"
            summary = f"Original failure at {fail_node}: {verdict}. {summary}"
        else:
            fail_node = None

    return ReplayImpact(
        improved_nodes=improved_nodes,
        regressed_nodes=regressed_nodes,
        key_fix_node=key_fix_node,
        downstream_improvement_count=len(improved_nodes),
        summary=summary,
        original_failure_node=fail_node,
        original_failure_resolved=resolved,
    )


# ── Main entry point ───────────────────────────────────────────────────────────


def _active_events(events: list[NodeEvent]) -> list[NodeEvent]:
    """Drop superseded retry attempts and unchosen branches from correlation."""
    return [e for e in events if e.status not in _INACTIVE_STATUSES]


def correlate(record: RunRecord) -> CorrelationReport:
    """Produce a CorrelationReport for a completed RunRecord."""
    events = _active_events(record.steps)
    if not events:
        return CorrelationReport(
            run_id=record.run_id,
            degradation_origins=[],
            propagation_chains=[],
            causal_summary="No steps recorded.",
            timeline=[],
        )

    edge_map = record.graph_edge_map or {}

    origins = _find_degradation_origins(events, edge_map)

    all_links = _dedup_links(
        _detect_field_drop_cascade(events, edge_map)
        + _detect_placeholder_propagation(events, edge_map)
        + _detect_anomaly_cascade(events, edge_map)
    )
    chains = _assemble_chains(all_links, events)

    timeline = _build_timeline(events, origins, chains)
    summary = _build_causal_summary(origins, chains, record.overall_status)

    return CorrelationReport(
        run_id=record.run_id,
        degradation_origins=origins,
        propagation_chains=chains,
        causal_summary=summary,
        timeline=timeline,
    )
