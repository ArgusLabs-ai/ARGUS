"""Contextual logic — who wrote a field, who reads it *later*, who dropped it.

Layer 3 of the pivot (``docs/ARGUS-PIVOT-CONTRIBUTORS.pdf`` §6), stubbed.

The contract this layer will own: a graph shares one state object, so a field
written early can still be required late. Step A writes ``b``; B and C run in
between and never had a duty to produce it; D still needs it. Matching step N's
output against step N+1's input blames C — that is the bug this layer exists to
fix. Blaming D's crash is the bug ARGUS already had.

A fat trace cannot answer this on its own: it records what each step returned,
never what a later step expected. The answer needs a consumer map built from the
graph, from state-schema types, or from a short declared list — a separate
branch of work.

Until then this returns nothing, and it returns nothing *honestly*: the recorder
still calls it, so the wiring is real and the empty result is a known gap rather
than a silent skip. Incomplete evidence must never read as "no findings, so it
passed" — that guard lives in ``recorder.IncompleteTraceError``.
"""

from __future__ import annotations

from typing import Any

from argus.models import Finding

__all__ = ["consumer_map", "contextual_findings"]


def consumer_map(ledger: list[Any]) -> dict[str, list[str]]:
    """Map ``field -> [nodes that read it later]``. Not implemented yet.

    Deriving it from written fields alone would only reproduce adjacent-edge
    matching under a new name, so this stays empty until the real reader source
    (state schema or declared list) is wired in.
    """
    return {}


def contextual_findings(ledger: list[Any]) -> list[Finding]:
    """Fields required later but never written, or dropped on the way.

    Empty until :func:`consumer_map` is real — there is no reader information to
    check against.
    """
    return []
