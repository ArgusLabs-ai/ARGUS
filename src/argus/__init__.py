"""ARGUS — Production readiness platform for AI agent pipelines.

Detects silent failures, semantic degradation, and handoff contract
violations before deployment. Framework-agnostic core with a
first-class LangGraph adapter.

LangGraph usage:
    from argus import ArgusWatcher

    watcher = ArgusWatcher(validators={
        "my_node": lambda out: (out.get("score", 0) > 0.5, "Score too low"),
    })
    app = watcher.attach(graph)     # StateGraph or compiled app
    result = app.invoke(state)      # persisted automatically

Framework-agnostic usage (Prefect, Temporal, raw Python, etc.):
    from argus import ArgusSession

    session = ArgusSession(validators={"validate": lambda o: (o.get("ok"), "not ok")})
    session.set_edges({"fetch": ["validate"], "validate": ["process"]})

    fetch    = session.wrap("fetch",    fetch_fn)
    validate = session.wrap("validate", validate_fn)
    process  = session.wrap("process",  process_fn)

    state = fetch(initial_state)
    state = validate(state)
    state = process(state)
    session.finalize()
"""

__version__ = "0.10.1"

# Hosted/enterprise activation: when the proprietary `cloud/` package is present
# (full-repo deployment), wire its Supabase config into the environment before
# any submodule reads it. The open-source pip package does not ship `cloud/`, so
# this import fails and silently no-ops — BYOK users stay fully local.
try:  # pragma: no cover - exercised only in hosted deployments
    from cloud.config import apply_env as _apply_cloud_env

    _apply_cloud_env()
except Exception:
    pass

from argus.models import ArgusConfig, LLMInvestigationConfig
from argus.session import ArgusSession
from argus.watcher import ArgusWatcher

__all__ = [
    "ArgusConfig",
    "ArgusWatcher",
    "ArgusSession",
    "LLMInvestigationConfig",
    "__version__",
]
