"""Contextual logic — who wrote a field, who reads it *later*, who dropped it.

Layer 3 of the pivot (``docs/ARGUS-PIVOT-CONTRIBUTORS.pdf`` §6).

A graph shares one state object, so a field written early can still be required
late. Step A writes ``b``; B and C run in between and never had a duty to
produce it; D still needs it. Matching step N's output against step N+1's input
blames C — that is the bug this layer exists to fix. Blaming D's crash is the
bug ARGUS already had.

A fat trace records what each step returned. It never records what a later step
expected, and deriving readers from what was written would just be adjacent
matching under a new name. So the readers are **declared**::

    ArgusRecorder(consumers={"b": ["D"]}).attach(app)

Keys may be dotted paths into nested state
(``consumers={"email.body": ["send_email"]}``). A top-level declaration still
means the whole value — ``{"email": {"subject": "...", "body": ""}}`` is a
non-empty ``email``, so blanking a nested leaf needs the leaf path.

Blame is anchored at the **reader**, not at the start of the run. A field that
does not exist yet is not a failure — most pipelines fill state progressively
(``ingest`` writes ``query``, ``retrieve`` writes ``sources``, ``draft`` reads
them). Asking "was it there when the reader started?" is the whole first test;
walking forward from step 0 and stopping at the first row that lacks the field
blames ``ingest`` for not having done ``retrieve``'s job.

Only when the field really is missing at the reader do we walk the field's
history backward:

* **written then dropped** — it was present, then a later row lost it →
  origin is the row that lost it (not the reader, not the reader's neighbour).
* **written empty** — a step wrote the key with ``[]`` / ``""`` / ``None`` →
  origin is that writer (``retrieve`` returning ``{"sources": []}``).
* **never written** — the key appears in no update before the reader → origin
  is the first row, since nothing in a trace says who was supposed to produce
  it. A producer map would; we do not have one and do not guess.

A node that returned a literal ``{}`` is not this layer's business —
``inspector.empty_output`` already blames it. When such a row sits between the
start and the reader, stay quiet rather than add a second, worse-aimed finding.

**Every** declared reader is checked, not only the first one that ran. A field
declared for ``[price, ship]`` can be present when ``price`` reads it and
emptied before ``ship`` does; anchoring on ``price`` alone let an order ship
with no line items.

**Victims are not origins.** ``retrieve`` returns ``[]``; ``rerank`` therefore
writes ``ranked: []``; ``generate`` therefore writes ``citations: []``. Each
"wrote it empty" is true, but only the first is a cause. A row that is itself a
declared reader starved of some field is a victim and is not blamed for what it
went on to write empty.

**Presence-only fields.** A declared consumer means "present and non-empty".
Some fields are legitimately empty on the good path — a PR review's
``issues: []`` *is* the LGTM. Declare them with
``{"issues": {"readers": ["summarize"], "allow_empty": True}}`` and only
absence (or ``None``) is a failure.

``allow_empty`` also covers the **tool responses** of the node that writes the
field (#129). A clean sanctions screen returns ``{"hits": []}`` from its OFAC
tool; without this, every healthy KYC onboarding failed CI on
``empty_result``, and declaring ``allow_empty`` on ``screening`` never reached
the tool key. A tool that raised or returned an error / 4xx / 5xx still fails
hard. Undeclared retrieval lists keep today's RAG default (critical).
"""

from __future__ import annotations

from typing import Any

from argus.findings import _mk
from argus.inspector import _is_empty
from argus.models import Finding

__all__ = [
    "allow_empty_fields",
    "contextual_findings",
    "node_writes_allow_empty",
    "propose_consumers",
]

# ``{"field": ["reader", ...]}`` or
# ``{"field": {"readers": ["reader", ...], "allow_empty": True}}``
# ``field`` may be a dotted path (``email.body``).
ConsumerMap = dict[str, Any]

# Sentinel for a dotted path that does not resolve — distinct from a present
# ``None``, which ``allow_empty`` still treats as absence.
_MISSING = object()


def allow_empty_fields(consumers: ConsumerMap | None) -> frozenset[str]:
    """State fields declared with ``allow_empty=True``."""
    if not consumers:
        return frozenset()
    return frozenset(field for field, spec in consumers.items() if _normalise(spec)[1])


def node_writes_allow_empty(consumers: ConsumerMap | None, update: dict[str, Any] | None) -> bool:
    """Did this node's update write a field declared ``allow_empty``?

    When true, empty retrieval lists in that node's tool responses (and in its
    own update) are warnings, not CI fails — see #129.
    """
    if not isinstance(update, dict):
        return False
    allowed = allow_empty_fields(consumers)
    return bool(allowed.intersection(update))


def propose_consumers(ledger: list[Any]) -> dict[str, list[str]]:
    """Candidate readers for fields a healthy run actually wrote.

    A trace cannot see which keys a function body read: every later node is
    handed the merged state. This lists, for each written field, the later
    nodes that received it and did not write it. The user deletes the nodes
    that only saw the field, then passes the result as ``consumers=``. Nothing
    here is graded, and a guess is never applied on its own (#148).

    Record a healthy run. A dropped field is absent from the starved node's
    input, so that node will not appear.
    """
    paths: list[str] = []
    seen_paths: set[str] = set()
    for row in ledger:
        for path in _written_paths(getattr(row, "update", None)):
            if path not in seen_paths:
                seen_paths.add(path)
                paths.append(path)

    proposed: dict[str, list[str]] = {}
    for path in paths:
        readers: list[str] = []
        seen_nodes: set[str] = set()
        written = False
        for row in ledger:
            if _wrote(row, path):
                written = True
                continue
            if not written or row.node in seen_nodes:
                continue
            if _resolve(row.input_state, path) is _MISSING:
                continue
            seen_nodes.add(row.node)
            readers.append(row.node)
        if readers:
            proposed[path] = readers
    return proposed


def _written_paths(update: Any) -> list[str]:
    """Top-level keys, plus one dotted level when the value is a dict."""
    if not isinstance(update, dict):
        return []
    paths: list[str] = []
    for key, value in update.items():
        if not isinstance(key, str) or key.startswith("__"):
            continue
        paths.append(key)
        if isinstance(value, dict):
            for child in value:
                if isinstance(child, str) and not child.startswith("__"):
                    paths.append(f"{key}.{child}")
    return paths


def contextual_findings(ledger: list[Any], consumers: ConsumerMap | None) -> list[Finding]:
    """Fields a declared reader needs that were never written, or were dropped.

    Returns one critical finding per missing field, blaming the origin row.
    ``node`` is the origin; ``field_path`` is the field; the reader is named in
    the reason. Callers attach these to the origin step — the run-status roll-up
    and ``argus check`` already fail on a step with missing fields.
    """
    if not consumers or not ledger:
        return []

    found: list[tuple[Finding, str]] = []  # (finding, reader that was starved)
    seen: set[tuple[str, str]] = set()
    for field, spec in consumers.items():
        readers, allow_empty = _normalise(spec)
        for reader_at in _reader_indices(ledger, readers):
            result = _blame(ledger, field, reader_at, allow_empty=allow_empty)
            if result is None:
                continue
            finding, reader = result
            key = (finding.node, field)
            if key in seen:
                continue  # one finding per origin+field, however many readers it starved
            seen.add(key)
            found.append((finding, reader))

    # A row that was itself starved is a victim of whatever starved it, not the
    # origin of what it then wrote empty. Keep the findings that name it as the
    # reader; drop the ones that name it as the origin.
    victims = {reader for _finding, reader in found}
    return [f for f, _reader in found if f.node not in victims]


def _normalise(spec: Any) -> tuple[list[str], bool]:
    if isinstance(spec, dict):
        return list(spec.get("readers") or []), bool(spec.get("allow_empty", False))
    return list(spec or []), False


def _blame(
    ledger: list[Any], field: str, reader_at: int, *, allow_empty: bool = False
) -> tuple[Finding, str] | None:
    """Who is answerable for `field` being missing when its reader ran.

    Returns ``(finding, reader_node)`` or None.
    """
    lacks = _absent if allow_empty else _lacks
    before = ledger[:reader_at]
    reader = ledger[reader_at]
    if not before:
        return None  # the reader ran first — nobody upstream to blame

    # The reader produces the field it consumes (accumulator, initialiser). It
    # is not missing — the reader is where it comes from. Unless it had nothing
    # to read *and* wrote the field empty: `validate` turning an absent
    # `line_items` into `[]` is a filter over a missing input, not a producer,
    # and exempting it hid the node upstream that never produced the field.
    if _wrote(reader, field) and (
        not lacks(reader.input_state, field) or not lacks(reader.update, field)
    ):
        return None

    # Anchor on the reader. Not yet written is not a failure.
    if not lacks(reader.input_state, field):
        return None

    # Index the field's history over the rows that ran before the reader.
    held = [i for i, row in enumerate(before) if not lacks(row.state_after, field)]
    wrote = [i for i, row in enumerate(before) if _wrote(row, field)]

    if held:
        # Present, then lost: blame the row that lost it.
        origin_at = next(
            (i for i in range(held[-1] + 1, len(before)) if lacks(before[i].state_after, field)),
            None,
        )
        if origin_at is None:
            # The notebook says it survived every row yet the reader did not get
            # it — the overlay and the real reducer merge disagree. Say nothing
            # rather than blame a row on bad evidence.
            return None
        why = f"`{before[origin_at].node}` dropped it"
    elif wrote:
        # Written, but written empty.
        origin_at = wrote[0]
        why = f"`{before[origin_at].node}` wrote it empty"
    else:
        # Never written by anyone. A node that returned `{}` is already blamed
        # by inspector.empty_output — defer to it instead of adding a second
        # finding aimed at whoever happened to run first.
        if any(row.update == {} for row in before):
            return None
        origin_at = 0
        why = "no step wrote it"

    origin = before[origin_at]
    finding = _mk(
        node=origin.node,
        type_="missing_field",
        severity="critical",
        reason=f"Field `{field}` is read by `{reader.node}` but {why}.",
        source="heuristic",
        field_path=field,
        origin_node=origin.node,
    )
    return finding, reader.node


def _reader_indices(ledger: list[Any], readers: list[str]) -> list[int]:
    names = set(readers)
    return [i for i, row in enumerate(ledger) if row.node in names]


def _resolve(state: Any, field: str) -> Any:
    """Walk a dotted path in ``state``. ``_MISSING`` if any segment is absent."""
    if not isinstance(state, dict):
        return _MISSING
    cur: Any = state
    for part in field.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return _MISSING
        cur = cur[part]
    return cur


def _wrote(row: Any, field: str) -> bool:
    """True when the step's update sets this path (value may be empty).

    A parent overwrite that omits the leaf (``{"email": {"subject": "x"}}``
    with no ``body``) is not a write of ``email.body`` — the drop is visible
    on ``state_after`` and caught by the held-then-lost walk instead.
    """
    return isinstance(row.update, dict) and _resolve(row.update, field) is not _MISSING


def _absent(state: dict[str, Any], field: str) -> bool:
    """Missing or ``None`` — the presence-only rule for ``allow_empty`` fields."""
    value = _resolve(state, field)
    return value is _MISSING or value is None


def _lacks(state: dict[str, Any], field: str) -> bool:
    """Absent, None, blank, or an empty collection.

    Reuses the inspector's own rule so a field dropped to `[]` or `""` reads
    the same here as it does everywhere else in ARGUS. Narrower than that —
    None-only — silently misses the commonest drop: a filter step that removes
    every element and returns `{"docs": []}`. Dotted paths resolve into nested
    dicts the same way.
    """
    value = _resolve(state, field)
    return value is _MISSING or _is_empty(value)
