"""Hosted/enterprise Supabase configuration.

NOT shipped in the PyPI package (excluded via pyproject.toml). The hosted
deployment imports this and calls apply_env() at startup so that
src/argus/cloud.py picks up the Supabase config from the environment.

The anon key is RLS-protected and safe to embed; the real secret (the OpenAI
key used by the proxy) lives only in Supabase Edge Function secrets.
"""

from __future__ import annotations

import os

SUPABASE_URL = "https://isnphpbckxfjsxllryrg.supabase.co"
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6ImlzbnBocGJja3hmanN4bGxyeXJnIiwi"
    "cm9sZSI6ImFub24iLCJpYXQiOjE3Nzc0MDcxNjQsImV4cCI6MjA5Mjk4MzE2NH0."
    "nfaMxbDL9E8gyvV7k2r6F8PQhAcxdZ4QVlkVWloJ88Q"
)
PROXY_URL = f"{SUPABASE_URL}/functions/v1/llm-proxy"


def apply_env() -> None:
    """Export hosted Supabase config into the environment if not already set."""
    os.environ.setdefault("ARGUS_SUPABASE_URL", SUPABASE_URL)
    os.environ.setdefault("ARGUS_SUPABASE_ANON_KEY", SUPABASE_ANON_KEY)
