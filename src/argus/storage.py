from __future__ import annotations

import dataclasses
import json
import os
from pathlib import Path
from typing import Any

from argus.models import (
    AnomalySignal,
    BehaviorConfig,
    CorrelationReport,
    DegradationOrigin,
    FieldMismatch,
    InspectionResult,
    LLMInvestigationResult,
    NodeDiffSummary,
    NodeEvent,
    PropagationChain,
    PropagationLink,
    ReplayComparisonResult,
    ReplayImpact,
    RunRecord,
    SemanticCheckResult,
    SemanticHypothesis,
    SemanticSignal,
    SuggestedSignature,
    TimelineEvent,
    ToolChainFinding,
    ToolFailure,
    ValidatorResult,
)

_ARGUS_DIR = ".argus"
_RUNS_DIR = "runs"

# Bump when the RunRecord schema changes in a backward-incompatible way.
# Old runs without this field are treated as schema version "0".
SCHEMA_VERSION = "1"


def resolve_project_root(start: Path | None = None) -> Path:
    """Directory that owns ``.argus/`` (runs, checkpoints, UI).

    Precedence:
    1. ``ARGUS_DIR`` if set (explicit override)
    2. nearest ancestor with ``.git`` or ``pyproject.toml``
    3. nearest ancestor that already has a ``.argus/`` directory
    4. ``start`` (default: cwd)
    """
    env = os.environ.get("ARGUS_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()

    here = (start or Path.cwd()).resolve()
    found_argus: Path | None = None
    for directory in [here, *here.parents]:
        if (directory / ".git").exists() or (directory / "pyproject.toml").is_file():
            return directory
        if found_argus is None and (directory / _ARGUS_DIR).is_dir():
            found_argus = directory
    return found_argus or here


def argus_dir(start: Path | None = None) -> Path:
    """``<project-root>/.argus`` — does not create the directory."""
    return resolve_project_root(start) / _ARGUS_DIR


def runs_dir(*, create: bool = False) -> Path:
    """``<project-root>/.argus/runs``.

    Pass ``create=True`` when writing (same as ``_runs_path()``).
    """
    base = argus_dir() / _RUNS_DIR
    if create:
        base.mkdir(parents=True, exist_ok=True)
    return base


def _runs_path() -> Path:
    return runs_dir(create=True)


def _to_json_serializable(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _to_json_serializable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _to_json_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_json_serializable(i) for i in obj]
    return str(obj)


def save_run(record: RunRecord) -> Path:
    """Write a completed RunRecord to .argus/runs/<run-id>.json atomically.

    If the user is logged in to ARGUS cloud, the run is also pushed
    to Supabase in a background thread (non-blocking).
    """
    path = _runs_path() / f"{record.run_id}.json"
    tmp = path.with_suffix(".tmp")
    data = _to_json_serializable(record)
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.rename(path)

    # Update hit metadata for learned/shared signatures, then prune stale ones
    try:
        from argus.signature_stats import prune_stale_signatures, update_hit_metadata

        update_hit_metadata(data)
        prune_stale_signatures()
    except Exception:
        pass  # hit tracking and pruning are best-effort

    # Cloud sync — push synchronously so the thread isn't killed on process exit
    try:
        from argus.cloud import is_logged_in, push_run

        if is_logged_in():
            push_run(data)
    except Exception:
        pass  # cloud sync is best-effort

    return path


def _run_search_roots() -> list[Path]:
    """Roots to scan for leftover ``.argus/runs`` dirs (legacy cwd writes).

    Project root is always first. Cwd is added only when it is outside the
    project tree — never walk an ancestor (e.g. ``/``) which would scan the
    whole filesystem.
    """
    project = resolve_project_root()
    roots = [project]
    cwd = Path.cwd().resolve()
    if cwd != project and cwd not in project.parents:
        roots.append(cwd)
    return roots


def _candidate_runs_dirs() -> list[Path]:
    """Project-root runs dir first, then nested/legacy ``.argus/runs`` dirs."""
    primary = _runs_path()
    seen = {primary.resolve()}
    dirs = [primary]
    for root in _run_search_roots():
        try:
            for sub_runs in root.rglob(".argus/runs"):
                resolved = sub_runs.resolve()
                if resolved in seen or not sub_runs.is_dir():
                    continue
                seen.add(resolved)
                dirs.append(sub_runs)
        except OSError:
            continue
    return dirs


def load_run_text(run_id: str) -> str:
    """Return the raw JSON text for a run (by id or 8-char prefix)."""
    return resolve_run_path(run_id).read_text(encoding="utf-8")


def resolve_run_path(run_id: str) -> Path:
    """Return the exact JSON path for a run id or unique prefix."""
    last_error: FileNotFoundError | None = None
    for directory in _candidate_runs_dirs():
        try:
            return _resolve_run_path(run_id, directory)
        except FileNotFoundError as exc:
            last_error = exc
            continue
    root = resolve_project_root()
    raise FileNotFoundError(f"No run found for id '{run_id}' under {root}") from last_error


def load_run(run_id: str) -> RunRecord:
    """Load a RunRecord by run-id (or 8-char prefix).

    Prefers ``<project-root>/.argus/runs/`` so CLI and UI agree, then
    searches nested/legacy ``.argus/runs`` directories under the project
    (and cwd, if cwd is outside the project).
    """
    path = resolve_run_path(run_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    return _deserialize_run(data)


def _run_json_files_newest_first(runs_dir: Path) -> list[Path]:
    """Sort run JSON paths by real time order, not lexicographic run_id.

    run_id ends with random hex; two runs in the same second can order wrong
    if sorted by filename (e.g. crash run appearing \"newer\" than the next).
    """
    files = list(runs_dir.glob("*.json"))
    if not files:
        return []

    def sort_key(path: Path) -> tuple[str, float]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            started = data.get("started_at") or ""
        except Exception:
            started = ""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (started, mtime)

    return sorted(files, key=sort_key, reverse=True)


def list_runs() -> list[dict[str, Any]]:
    """Return summary metadata for all runs, newest first."""
    runs_dir = _runs_path()
    summaries = []
    for f in _run_json_files_newest_first(runs_dir):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            summaries.append(
                {
                    "run_id": data.get("run_id", f.stem),
                    "started_at": data.get("started_at", ""),
                    "overall_status": data.get("overall_status", "unknown"),
                    "duration_ms": data.get("duration_ms"),
                    "step_count": len(data.get("steps", [])),
                }
            )
        except Exception:
            continue
    return summaries


def last_run_id() -> str | None:
    runs_dir = _runs_path()
    files = _run_json_files_newest_first(runs_dir)
    if not files:
        return None
    data = json.loads(files[0].read_text(encoding="utf-8"))
    return data.get("run_id")


def _resolve_run_path(run_id: str, runs_dir: Path) -> Path:
    exact = runs_dir / f"{run_id}.json"
    if exact.exists():
        return exact
    # prefix match
    matches = [f for f in runs_dir.glob("*.json") if f.stem.startswith(run_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        names = [f.stem for f in matches]
        raise ValueError(f"Ambiguous run-id prefix '{run_id}' matches: {names}")
    raise FileNotFoundError(f"No run found for id '{run_id}' in {runs_dir}")


def list_replay_children(run_id: str) -> list[dict[str, Any]]:
    """Return summary metadata for all runs whose parent_run_id matches run_id."""
    runs_dir = _runs_path()
    children: list[dict[str, Any]] = []
    for f in runs_dir.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if data.get("parent_run_id") == run_id:
                children.append(
                    {
                        "run_id": data.get("run_id", f.stem),
                        "started_at": data.get("started_at", ""),
                        "overall_status": data.get("overall_status", "unknown"),
                        "duration_ms": data.get("duration_ms"),
                        "step_count": len(data.get("steps", [])),
                        "replay_from_step": data.get("replay_from_step"),
                        "parent_run_id": run_id,
                    }
                )
        except Exception:
            continue
    children.sort(key=lambda r: r.get("started_at", ""))
    return children


def build_replay_tree(run_id: str, max_depth: int = 5) -> dict[str, Any]:
    """Build a tree of the original run and its replay descendants."""
    runs_dir = _runs_path()

    # Load the root run summary
    try:
        _resolve_run_path(run_id, runs_dir)
    except (FileNotFoundError, ValueError):
        return {"error": f"Run '{run_id}' not found"}

    def _build(rid: str, depth: int) -> dict[str, Any]:
        node: dict[str, Any] = {"run_id": rid}
        try:
            p = _resolve_run_path(rid, runs_dir)
            d = json.loads(p.read_text(encoding="utf-8"))
            node["started_at"] = d.get("started_at", "")
            node["overall_status"] = d.get("overall_status", "unknown")
            node["duration_ms"] = d.get("duration_ms")
            node["step_count"] = len(d.get("steps", []))
            node["replay_from_step"] = d.get("replay_from_step")
        except Exception:
            node["overall_status"] = "unknown"

        if depth < max_depth:
            kids = list_replay_children(rid)
            node["children"] = [_build(c["run_id"], depth + 1) for c in kids]
        else:
            node["children"] = []
        return node

    return _build(run_id, 0)


def _deserialize_run(data: dict[str, Any]) -> RunRecord:
    steps = [_deserialize_event(s) for s in data.get("steps", [])]
    bc_data = data.get("behavior_config")
    behavior_config = BehaviorConfig(**bc_data) if bc_data else None
    corr_data = data.get("correlation")
    correlation = _deserialize_correlation(corr_data) if corr_data else None
    llm_inv_data = data.get("llm_investigation")
    llm_investigation = _deserialize_llm_investigation(llm_inv_data) if llm_inv_data else None
    rc_data = data.get("replay_comparison")
    replay_comparison = _deserialize_replay_comparison(rc_data) if rc_data else None
    loop_analyses = _deserialize_loop_analyses(data.get("loop_analyses", []))
    tool_chain_findings = _deserialize_tool_chain_findings(data.get("tool_chain_findings", []))
    return RunRecord(
        run_id=data["run_id"],
        argus_version=data.get("argus_version", "unknown"),
        started_at=data.get("started_at", ""),
        completed_at=data.get("completed_at"),
        duration_ms=data.get("duration_ms"),
        overall_status=data.get("overall_status", "unknown"),
        first_failure_step=data.get("first_failure_step"),
        root_cause_chain=data.get("root_cause_chain", []),
        graph_node_names=data.get("graph_node_names", []),
        graph_edge_map=data.get("graph_edge_map", {}),
        initial_state=data.get("initial_state", {}),
        steps=steps,
        schema_version=data.get("schema_version", "0"),
        parent_run_id=data.get("parent_run_id"),
        replay_from_step=data.get("replay_from_step"),
        is_cyclic=data.get("is_cyclic", False),
        app_factory_ref=data.get("app_factory_ref"),
        node_fn_refs=data.get("node_fn_refs"),
        node_fn_paths=data.get("node_fn_paths"),
        subgraph_run_ids=data.get("subgraph_run_ids", []),
        interrupted=data.get("interrupted", False),
        interrupt_node=data.get("interrupt_node"),
        behavior_config=behavior_config,
        correlation=correlation,
        llm_investigation=llm_investigation,
        replay_comparison=replay_comparison,
        loop_analyses=loop_analyses,
        tool_chain_findings=tool_chain_findings,
        dry_run=data.get("dry_run", False),
        coverage_summary=data.get("coverage_summary", {}),
    )


def _deserialize_correlation(data: dict[str, Any]) -> CorrelationReport:
    origins = [
        DegradationOrigin(
            node_name=o["node_name"],
            step_index=o["step_index"],
            signal_types=tuple(o.get("signal_types", [])),
            confidence=o["confidence"],
            reason=o["reason"],
            confidence_breakdown=tuple(
                (str(item[0]), float(item[1]))
                for item in o.get("confidence_breakdown", [])
            ),
        )
        for o in data.get("degradation_origins", [])
    ]
    chains = [
        PropagationChain(
            chain_type=c["chain_type"],
            nodes=tuple(c.get("nodes", [])),
            links=tuple(
                PropagationLink(
                    source_node=lnk["source_node"],
                    target_node=lnk["target_node"],
                    signal_type=lnk["signal_type"],
                    confidence=lnk["confidence"],
                    evidence=lnk["evidence"],
                )
                for lnk in c.get("links", [])
            ),
            summary=c["summary"],
        )
        for c in data.get("propagation_chains", [])
    ]
    timeline = [
        TimelineEvent(
            step_index=t["step_index"],
            node_name=t["node_name"],
            event_type=t["event_type"],
            label=t["label"],
            signal_summary=t["signal_summary"],
        )
        for t in data.get("timeline", [])
    ]
    ri_data = data.get("replay_impact")
    replay_impact: ReplayImpact | None = None
    if ri_data:
        replay_impact = ReplayImpact(
            improved_nodes=ri_data.get("improved_nodes", []),
            regressed_nodes=ri_data.get("regressed_nodes", []),
            key_fix_node=ri_data.get("key_fix_node"),
            downstream_improvement_count=ri_data.get("downstream_improvement_count", 0),
            summary=ri_data.get("summary", ""),
            original_failure_node=ri_data.get("original_failure_node"),
            original_failure_resolved=ri_data.get("original_failure_resolved"),
        )
    return CorrelationReport(
        run_id=data["run_id"],
        degradation_origins=origins,
        propagation_chains=chains,
        causal_summary=data.get("causal_summary", ""),
        timeline=timeline,
        replay_impact=replay_impact,
    )


def _deserialize_llm_investigation(data: dict[str, Any]) -> LLMInvestigationResult:
    hypotheses = [
        SemanticHypothesis(
            hypothesis=h["hypothesis"],
            confidence=h["confidence"],
            supporting_evidence=tuple(h.get("supporting_evidence", [])),
            category=h.get("category", "other"),
        )
        for h in data.get("causal_hypotheses", [])
    ]
    suggested_sigs = [
        SuggestedSignature(
            pattern=s["pattern"],
            match_strategy=s.get("match_strategy", "contains_ci"),
            proposed_category=s.get("proposed_category", "unknown"),
            severity=s.get("severity", "warning"),
            description=s.get("description", ""),
            evidence=tuple(s.get("evidence", [])),
            confidence=s.get("confidence", 0.0),
            reasoning=s.get("reasoning", ""),
        )
        for s in data.get("suggested_signatures", [])
    ]
    return LLMInvestigationResult(
        triggered=data.get("triggered", False),
        trigger_reasons=data.get("trigger_reasons", []),
        root_cause_explanation=data.get("root_cause_explanation", ""),
        causal_hypotheses=hypotheses,
        degradation_narrative=data.get("degradation_narrative", ""),
        cross_node_connections=data.get("cross_node_connections", []),
        observations=data.get("observations", []),
        debugging_suggestions=data.get("debugging_suggestions", []),
        confidence=data.get("confidence", 0.0),
        suggested_signatures=suggested_sigs,
        model_used=data.get("model_used", ""),
        prompt_tokens=data.get("prompt_tokens", 0),
        completion_tokens=data.get("completion_tokens", 0),
        investigation_duration_ms=data.get("investigation_duration_ms", 0.0),
        error=data.get("error"),
    )


def _deserialize_replay_comparison(data: dict[str, Any]) -> ReplayComparisonResult:
    node_summaries = [
        NodeDiffSummary(
            node_name=ns.get("node_name", ""),
            status_before=ns.get("status_before", ""),
            status_after=ns.get("status_after", ""),
            summary=ns.get("summary", ""),
            verdict=ns.get("verdict", "changed"),
        )
        for ns in data.get("node_summaries", [])
    ]
    return ReplayComparisonResult(
        structural_summary=data.get("structural_summary", ""),
        failure_analysis=data.get("failure_analysis", ""),
        root_cause_delta=data.get("root_cause_delta", ""),
        key_insights=data.get("key_insights", []),
        recommendation=data.get("recommendation", ""),
        confidence=data.get("confidence", 0.0),
        node_summaries=node_summaries,
        model_used=data.get("model_used", ""),
        prompt_tokens=data.get("prompt_tokens", 0),
        completion_tokens=data.get("completion_tokens", 0),
        duration_ms=data.get("duration_ms", 0.0),
        error=data.get("error"),
    )


def _deserialize_loop_analyses(
    items: list[dict[str, Any]],
) -> list:
    from argus.models import LoopAnalysisResult, LoopIterationDiff

    results = []
    for la in items:
        diffs = [
            LoopIterationDiff(
                from_attempt=d.get("from_attempt", 0),
                to_attempt=d.get("to_attempt", 0),
                summary=d.get("summary", ""),
                fields_changed=d.get("fields_changed", []),
            )
            for d in la.get("iteration_diffs", [])
        ]
        results.append(
            LoopAnalysisResult(
                node_name=la.get("node_name", ""),
                total_iterations=la.get("total_iterations", 0),
                summary=la.get("summary", ""),
                is_stalled=la.get("is_stalled", False),
                stall_details=la.get("stall_details"),
                unnecessary_retries=la.get("unnecessary_retries", 0),
                unnecessary_details=la.get("unnecessary_details"),
                iteration_diffs=diffs,
                model_used=la.get("model_used", ""),
                prompt_tokens=la.get("prompt_tokens", 0),
                completion_tokens=la.get("completion_tokens", 0),
                duration_ms=la.get("duration_ms", 0.0),
                error=la.get("error"),
            )
        )
    return results


def _deserialize_tool_chain_findings(
    items: list[dict[str, Any]],
) -> list[ToolChainFinding]:
    return [
        ToolChainFinding(
            finding_id=f.get("finding_id", ""),
            finding_type=f.get("finding_type", ""),
            severity=f.get("severity", "warning"),
            nodes_involved=tuple(f.get("nodes_involved", ())),
            description=f.get("description", ""),
            evidence=f.get("evidence", ""),
            confidence=f.get("confidence", 0.0),
        )
        for f in items
    ]


def _deserialize_event(data: dict[str, Any]) -> NodeEvent:
    insp_data = data.get("inspection")
    inspection = None
    if insp_data:
        mismatches = [FieldMismatch(**m) for m in insp_data.get("type_mismatches", [])]
        tool_failures = [ToolFailure(**tf) for tf in insp_data.get("tool_failures", [])]
        semantic_signals = [
            SemanticSignal(
                sig_id=s["sig_id"],
                category=s["category"],
                severity=s["severity"],
                description=s["description"],
                field_path=tuple(s["field_path"]),
                evidence=s["evidence"],
            )
            for s in insp_data.get("semantic_signals", [])
        ]
        inspection = InspectionResult(
            is_silent_failure=insp_data.get("is_silent_failure", False),
            missing_fields=insp_data.get("missing_fields", []),
            empty_fields=insp_data.get("empty_fields", []),
            type_mismatches=mismatches,
            severity=insp_data.get("severity", "ok"),
            message=insp_data.get("message", ""),
            unannotated_successors=insp_data.get("unannotated_successors", []),
            suspicious_empty_keys=insp_data.get("suspicious_empty_keys", []),
            tool_failures=tool_failures,
            has_tool_failure=insp_data.get("has_tool_failure", False),
            semantic_signals=semantic_signals,
            degraded_fields=insp_data.get("degraded_fields", []),
            degraded_upstream_node=insp_data.get("degraded_upstream_node"),
        )
    validator_results = [ValidatorResult(**v) for v in data.get("validator_results", [])]
    anomaly_signals = [AnomalySignal(**a) for a in data.get("anomaly_signals", [])]
    sc = data.get("semantic_check")
    if sc:
        # Convert list→tuple for frozen tuple fields (JSON round-trip produces lists)
        if "evidence_considered" in sc:
            sc["evidence_considered"] = tuple(sc["evidence_considered"])
        if "overridden_signals" in sc:
            sc["overridden_signals"] = tuple(sc["overridden_signals"])
        semantic_check = SemanticCheckResult(**sc)
    else:
        semantic_check = None
    return NodeEvent(
        step_index=data.get("step_index", 0),
        node_name=data.get("node_name", ""),
        status=data.get("status", "pass"),
        input_state=data.get("input_state", {}),
        output_dict=data.get("output_dict"),
        duration_ms=data.get("duration_ms", 0.0),
        timestamp_utc=data.get("timestamp_utc", ""),
        exception=data.get("exception"),
        inspection=inspection,
        attempt_index=data.get("attempt_index", 0),
        validator_results=validator_results,
        is_subgraph_entry=data.get("is_subgraph_entry", False),
        subgraph_run_id=data.get("subgraph_run_id"),
        behavior_type=data.get("behavior_type"),
        anomaly_signals=anomaly_signals,
        semantic_check=semantic_check,
        total_iterations=data.get("total_iterations"),
    )
