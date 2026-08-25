"""Cross-node tool call chain analysis — detects usage anti-patterns.

Runs at finalize time, deterministic, always-on, fail-open.
Detects: retry storms, ordering anomalies, unused results, argument degradation.
"""

from __future__ import annotations

from collections import defaultdict
from difflib import SequenceMatcher

from argus.models import NodeEvent, RunRecord, ToolChainFinding

_SENTINEL_NODES = frozenset({"__start__", "__end__"})


def analyze_tool_chains(record: RunRecord) -> list[ToolChainFinding]:
    """Entry point — run all detectors, return combined findings."""
    findings: list[ToolChainFinding] = []
    events = [e for e in record.steps if e.node_name not in _SENTINEL_NODES]

    for detector in (_detect_retry_storms, _detect_ordering_anomalies,
                     _detect_unused_results, _detect_argument_degradation):
        try:
            findings.extend(detector(events, record.graph_edge_map))
        except Exception:
            pass  # fail-open per detector

    return findings


def _state_to_str(state: dict) -> str:
    """Flatten a state dict to a comparable string."""
    parts = []
    for k in sorted(state):
        v = state[k]
        s = str(v)
        if len(s) > 500:
            s = s[:500]
        parts.append(f"{k}={s}")
    return " ".join(parts)


def _detect_retry_storms(
    events: list[NodeEvent], edge_map: dict[str, list[str]]
) -> list[ToolChainFinding]:
    """TC-001: Same node called 3+ times with similar inputs."""
    findings: list[ToolChainFinding] = []
    by_name: dict[str, list[NodeEvent]] = defaultdict(list)
    for e in events:
        by_name[e.node_name].append(e)

    for node_name, node_events in by_name.items():
        if len(node_events) < 3:
            continue

        # Compare consecutive input similarity
        input_strs = [_state_to_str(e.input_state) for e in node_events]
        similar_count = 0
        for i in range(1, len(input_strs)):
            if not input_strs[i - 1] or not input_strs[i]:
                continue
            ratio = SequenceMatcher(None, input_strs[i - 1], input_strs[i]).ratio()
            if ratio > 0.8:
                similar_count += 1

        # Need at least 2 similar consecutive pairs (i.e., 3 similar inputs)
        if similar_count < 2:
            continue

        count = len(node_events)
        severity = "critical" if count >= 5 else "warning"
        findings.append(ToolChainFinding(
            finding_id="TC-001",
            finding_type="retry_storm",
            severity=severity,
            nodes_involved=(node_name,),
            description=(
                f"`{node_name}` called {count} times with similar inputs — "
                f"agent may be stuck in a retry loop"
            ),
            evidence=f"{count} invocations, {similar_count + 1} with >80% input similarity",
            confidence=min(0.6 + similar_count * 0.1, 0.95),
        ))
    return findings


def _build_reachable(edge_map: dict[str, list[str]]) -> dict[str, set[str]]:
    """Build transitive closure: node -> set of all nodes reachable from it."""
    reachable: dict[str, set[str]] = {}
    for node in edge_map:
        visited: set[str] = set()
        stack = list(edge_map.get(node, []))
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            stack.extend(edge_map.get(n, []))
        reachable[node] = visited
    return reachable


def _detect_ordering_anomalies(
    events: list[NodeEvent], edge_map: dict[str, list[str]]
) -> list[ToolChainFinding]:
    """TC-002: Node that should run later executed first (topology violation)."""
    findings: list[ToolChainFinding] = []
    if not edge_map:
        return findings

    reachable = _build_reachable(edge_map)

    # Build first execution index per node (skip skipped/retried)
    first_exec: dict[str, int] = {}
    for e in events:
        if e.status in ("skipped", "retried"):
            continue
        if e.node_name not in first_exec:
            first_exec[e.node_name] = e.step_index

    # Check: if A is reachable from B (B should run before A),
    # but A ran before B, that's an ordering anomaly.
    # Skip pairs that sit in a cycle (A reachable from B and B from A) —
    # generate → validate → retry is intentional, not a topology violation.
    checked: set[tuple[str, str]] = set()
    for node_b, successors in reachable.items():
        if node_b in _SENTINEL_NODES or node_b not in first_exec:
            continue
        for node_a in successors:
            if node_a in _SENTINEL_NODES or node_a not in first_exec:
                continue
            pair = (node_b, node_a)
            if pair in checked:
                continue
            checked.add(pair)
            if node_b in reachable.get(node_a, set()):
                continue

            # node_a is a successor of node_b, so node_b should run first
            if first_exec[node_a] < first_exec[node_b]:
                findings.append(ToolChainFinding(
                    finding_id="TC-002",
                    finding_type="ordering_anomaly",
                    severity="warning",
                    nodes_involved=(node_a, node_b),
                    description=(
                        f"`{node_a}` ran before `{node_b}` but is a downstream "
                        f"successor — execution order may be wrong"
                    ),
                    evidence=(
                        f"{node_a} at step {first_exec[node_a]}, "
                        f"{node_b} at step {first_exec[node_b]}"
                    ),
                    confidence=0.7,
                ))
    return findings


def _detect_unused_results(
    events: list[NodeEvent], edge_map: dict[str, list[str]]
) -> list[ToolChainFinding]:
    """TC-003: Node A produces output but connected node B ignores it."""
    findings: list[ToolChainFinding] = []
    if not edge_map:
        return findings

    # Build last event per node (final output)
    last_event: dict[str, NodeEvent] = {}
    for e in events:
        if e.status not in ("skipped", "retried") and e.output_dict:
            last_event[e.node_name] = e

    for source_name, targets in edge_map.items():
        if source_name in _SENTINEL_NODES:
            continue
        source_ev = last_event.get(source_name)
        if not source_ev or not source_ev.output_dict:
            continue

        # Flatten source output to searchable text
        source_keys = set(source_ev.output_dict.keys())
        source_text = str(source_ev.output_dict)
        if len(source_text) < 100:
            continue  # too small to be meaningful

        for target_name in targets:
            if target_name in _SENTINEL_NODES:
                continue
            target_ev = last_event.get(target_name)
            if not target_ev or not target_ev.output_dict:
                continue

            target_text = str(target_ev.output_dict)
            target_keys = set(target_ev.output_dict.keys())

            # Check key overlap (source produced keys that target also has)
            key_overlap = source_keys & target_keys
            if key_overlap:
                continue  # keys overlap — likely using the data

            # Check value overlap — any source key name appears in target output
            any_mention = any(k in target_text for k in source_keys if len(k) > 2)
            if any_mention:
                continue

            findings.append(ToolChainFinding(
                finding_id="TC-003",
                finding_type="unused_result",
                severity="warning",
                nodes_involved=(source_name, target_name),
                description=(
                    f"`{target_name}` shows no evidence of using "
                    f"`{source_name}`'s output — results may be ignored"
                ),
                evidence=(
                    f"source keys: {sorted(source_keys)[:5]}, "
                    f"target keys: {sorted(target_keys)[:5]}, no overlap"
                ),
                confidence=0.5,
            ))
    return findings


def _detect_argument_degradation(
    events: list[NodeEvent], edge_map: dict[str, list[str]]
) -> list[ToolChainFinding]:
    """TC-004: Retried node inputs become less specific over attempts."""
    findings: list[ToolChainFinding] = []
    by_name: dict[str, list[NodeEvent]] = defaultdict(list)
    for e in events:
        by_name[e.node_name].append(e)

    for node_name, node_events in by_name.items():
        if len(node_events) < 2:
            continue
        # Sort by attempt_index
        sorted_events = sorted(node_events, key=lambda e: e.attempt_index)
        if sorted_events[-1].attempt_index == 0:
            continue  # not actual retries

        first_str = _state_to_str(sorted_events[0].input_state)
        last_str = _state_to_str(sorted_events[-1].input_state)

        if not first_str:
            continue

        len_ratio = len(last_str) / len(first_str) if first_str else 1.0
        first_words = set(first_str.split())
        last_words = set(last_str.split())
        word_ratio = len(last_words) / len(first_words) if first_words else 1.0

        # Both length and word count decreased by >30%
        if len_ratio < 0.7 and word_ratio < 0.7:
            findings.append(ToolChainFinding(
                finding_id="TC-004",
                finding_type="argument_degradation",
                severity="warning",
                nodes_involved=(node_name,),
                description=(
                    f"`{node_name}` inputs degraded across "
                    f"{len(sorted_events)} retries — queries becoming less specific"
                ),
                evidence=(
                    f"input length: {len(first_str)} -> {len(last_str)} "
                    f"({len_ratio:.0%}), words: {len(first_words)} -> "
                    f"{len(last_words)} ({word_ratio:.0%})"
                ),
                confidence=0.6,
            ))
    return findings
