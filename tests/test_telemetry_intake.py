"""Guards on the telemetry intake's deploy files (supabase/telemetry/).

The edge function validates against its own copy of the published schema; a
drift would let it accept cards the docs say cannot exist, or reject real ones.
"""

from __future__ import annotations

import re
from pathlib import Path

from argus.telemetry import send

ROOT = Path(__file__).parent.parent
PUBLISHED = ROOT / "src/argus/data/share_card.schema.json"
FUNCTION = ROOT / "supabase/telemetry/functions/telemetry-intake"
# migration.sql, then permissions.sql: every file applied to the telemetry project.
SQL = "\n".join(f.read_text() for f in sorted((ROOT / "supabase/telemetry").glob("*.sql")))


def test_function_schema_is_the_published_schema():
    assert (FUNCTION / "share_card.schema.json").read_bytes() == PUBLISHED.read_bytes()


def test_every_table_has_row_level_security():
    tables = set(re.findall(r"create table public\.(\w+)", SQL))
    secured = set(re.findall(r"alter table public\.(\w+)\s+enable row level security", SQL))
    assert tables and tables == secured


def test_no_policy_grants_anything_to_anon():
    policies = re.findall(r"create policy[^;]*;", SQL.lower())
    assert policies
    for policy in policies:
        assert re.search(r"\bto authenticated\b", policy), policy
        assert not re.search(r"\bto (anon|public)\b", policy), policy


def test_server_cap_is_above_the_client_cap():
    source = (FUNCTION / "index.ts").read_text()
    server_cap = int(re.search(r"INSTALL_CAP = (\d+)", source).group(1))
    assert server_cap > send.DAILY_CAP
