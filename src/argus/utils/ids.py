from __future__ import annotations

import hashlib
import os
import uuid
from collections.abc import Iterable
from datetime import datetime, timezone

# What LangGraph calls a compiled graph nobody named. Not an identity.
_DEFAULT_GRAPH_NAME = "LangGraph"


def generate_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    short_uuid = uuid.uuid4().hex[:6]
    return f"{timestamp}-{short_uuid}"


def resolve_environment() -> str:
    """Where this run happened.

    ``$ARGUS_ENVIRONMENT``, else ``ci`` on GitHub Actions, else ``local``. A blank
    variable counts as unset.
    """
    explicit = os.environ.get("ARGUS_ENVIRONMENT", "").strip()
    if explicit:
        return explicit
    return "ci" if os.environ.get("GITHUB_ACTIONS") == "true" else "local"


def resolve_agent(
    *,
    explicit: str | None = None,
    graph_name: str | None = None,
    app_factory_ref: str | None = None,
    node_names: Iterable[str] = (),
) -> str:
    """Which pipeline a run belongs to. The first of these that is set, blank counting as unset:

    1. ``explicit`` (``ArgusRecorder(agent=...)``)
    2. ``$ARGUS_AGENT``
    3. ``graph_name``, the compiled graph's ``.name``, unless it is LangGraph's default
    4. the module part of ``app_factory_ref`` (``"pkg.graphs:build"`` -> ``"pkg.graphs"``)
    5. ``g_`` + a hash of the sorted node names, so the same graph always lands on the same row
    """
    named = graph_name if graph_name != _DEFAULT_GRAPH_NAME else None
    factory_module = (app_factory_ref or "").partition(":")[0]
    for candidate in (explicit, os.environ.get("ARGUS_AGENT"), named, factory_module):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return "g_" + hashlib.sha1(",".join(sorted(node_names)).encode()).hexdigest()[:10]
