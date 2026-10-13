"""Data types for cross-step claim verification (PRD-10).

Kept out of ``models.py`` so the feature adds one import and one field there;
``claim_verifier`` holds the logic, ``storage`` calls
:func:`deserialize_claim_verdicts` on load.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CLAIM_VERDICTS = frozenset({"verified", "broken", "unverifiable"})

# What a claim asserts about its field refs (the extractor classifies it; Python
# only trusts a kind to *narrow* what it checks, never to widen it):
#   populated - the field(s) hold data after the claiming step (saved, written, counted)
#   emptied   - the field(s) were cleared or consumed
#   value     - one field holds a specific value (``Claim.value``)
#   other     - anything else, including a promise about the next action
CLAIM_KINDS = frozenset({"populated", "emptied", "value", "other"})


@dataclass(frozen=True)
class Claim:
    """One claim a node's prose output made about shared state.

    The failure class this captures is *consistency over time*: "saved 3
    candidates to `results`" (then `results` is empty), "confirmed the total is
    142" (then downstream computes 153). A claim asserts nothing until a
    :class:`ClaimVerdict` checks it against the state recorded *after* the
    claiming step.

    ``claim_id`` is step-cited (``CLM-<step_index>-<n>``). ``field_refs`` are
    state keys (dotted paths allowed) the extractor judged the claim to be
    about; with ``kind`` they decide whether a zero-LLM check applies at all.
    """

    claim_id: str
    node: str  # the promising node
    step_index: int  # where the claim was made
    text: str  # the claim in the node's own words
    field_refs: tuple[str, ...]
    extracted_by: str  # judge model that extracted it
    confidence: float  # 0.0-1.0, the extractor's self-assessment
    kind: str = "other"  # one of CLAIM_KINDS
    value: str | None = None  # the literal a ``value`` claim asserts, as text


@dataclass(frozen=True)
class ClaimVerdict:
    """One claim checked against the recorded later state.

    Three-valued, like ``BehaviorVerdict``: "the run never showed this" is
    neither ``verified`` (absence of a check is not proof) nor ``broken`` (the
    trace may be partial). ``evaluated=False`` marks every path where no real
    verification happened (judge offline, an unreadable or ungrounded reply):
    the slot keeps the claim visible but counts as nothing, and a re-run keeps
    an earlier evaluated verdict in its place.

    ``node`` / ``text`` are denormalized from the claim so a verdict alone can
    drive a ``broken_claim`` finding. ``judge_model == ""`` means the verdict is
    pure Python over the run record. ``quote`` is the excerpt of a cited
    step's state an LLM verdict rests on (empty on deterministic verdicts).
    """

    claim_id: str
    node: str
    step_index: int
    text: str
    verdict: str  # one of CLAIM_VERDICTS
    cited_steps: tuple[int, ...]  # step_index values into the run's steps
    reason: str
    confidence: float
    judge_model: str
    evaluated: bool = True
    quote: str = ""


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def deserialize_claim_verdicts(items: Any) -> list[ClaimVerdict]:
    """Verdicts written by `argus claims`, tolerant of a hand-edited run file.

    A row that cannot be read (not an object, no integer ``step_index``) is
    skipped rather than making the whole run unloadable. A verdict string that
    says nothing loads as ``unverifiable``, never ``broken``, and only a literal
    ``evaluated: true`` counts as evaluated; a ``broken`` with no citation is
    unevaluated, as the verifier would have made it.
    """
    out: list[ClaimVerdict] = []
    for v in items if isinstance(items, list) else []:
        if not isinstance(v, dict) or not _is_int(v.get("step_index")):
            continue
        verdict = str(v.get("verdict", "")).strip().lower()
        if verdict not in CLAIM_VERDICTS:
            verdict = "unverifiable"
        try:
            confidence = float(v.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        if confidence != confidence:  # NaN
            confidence = 0.0
        raw_cited = v.get("cited_steps")
        cited = tuple(s for s in (raw_cited if isinstance(raw_cited, list) else []) if _is_int(s))
        out.append(
            ClaimVerdict(
                claim_id=str(v.get("claim_id", "")),
                node=str(v.get("node", "")),
                step_index=v["step_index"],
                text=str(v.get("text", "")),
                verdict=verdict,
                cited_steps=cited,
                reason=str(v.get("reason", "")),
                confidence=min(1.0, max(0.0, confidence)),
                judge_model=str(v.get("judge_model", "")),
                evaluated=v.get("evaluated") is True and not (verdict == "broken" and not cited),
                quote=str(v.get("quote", "")),
            )
        )
    return out
