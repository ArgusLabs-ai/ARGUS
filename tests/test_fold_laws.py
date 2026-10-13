"""The behavior fold's safety laws, checked against the real ``fold_behavior_verdicts``.

``docs/MATH-CERTIFICATES.md`` proves these laws for the count model of the fold
over *all* clause counts (Z3, via Jacobian's ``smt.solve``). This file closes the
other half: the real function equals that model. The fold reads judgments only
through ``any`` / ``all`` / emptiness, so its result depends on *which kinds of
judgment are present*, never on how many. Checking every subset of the kinds
therefore covers every possible list, of any length.
"""

from __future__ import annotations

import itertools
import random

import pytest

from argus.models import BehaviorJudgment, BehaviorVerdict, fold_behavior_verdicts

V = BehaviorVerdict
_NA_REASONS = [None, "insufficient_evidence", "not_applicable", "behavior_not_judgeable", "other"]
# Every distinct combination of the three fields the fold reads.
_KINDS = [
    (evaluated, verdict, reason)
    for evaluated in (True, False)
    for verdict in V
    for reason in (_NA_REASONS if verdict is V.NA else [None])
]


def _judgment(kind) -> BehaviorJudgment:
    evaluated, verdict, reason = kind
    return BehaviorJudgment(
        behavior="b",
        clause="c",
        verdict=verdict,
        na_reason=reason,
        confidence=1.0,
        cited_steps=(),
        violated_clause=None,
        reason="",
        judge_model="m",
        evaluated=evaluated,
    )


def _role(kind) -> str:
    """F = an evaluated FALSE, B = a blocker (unjudged, or NA for missing evidence),
    N = an NA that hides nothing, T = an evaluated TRUE."""
    evaluated, verdict, reason = kind
    if evaluated and verdict is V.FALSE:
        return "F"
    if not evaluated or (verdict is V.NA and reason == "insufficient_evidence"):
        return "B"
    return "N" if verdict is V.NA else "T"


def _model(kinds) -> BehaviorVerdict:
    """The count model proved in docs/MATH-CERTIFICATES.md."""
    roles = [_role(k) for k in kinds]
    if "F" in roles:
        return V.FALSE
    if not roles or "B" in roles or "T" not in roles:
        return V.NA
    return V.TRUE


def _fold(kinds) -> BehaviorVerdict:
    return fold_behavior_verdicts([_judgment(k) for k in kinds])


def _disagrees_with_model(fold) -> bool:
    for mask in range(1 << len(_KINDS)):
        present = [k for i, k in enumerate(_KINDS) if mask >> i & 1]
        if fold([_judgment(k) for k in present]) is not _model(present):
            return True
    return False


def _all_na_reads_true(judgments):
    result = fold_behavior_verdicts(judgments)
    all_na = bool(judgments) and all(j.evaluated and j.verdict is V.NA for j in judgments)
    return V.TRUE if all_na and result is V.NA else result


# Real folds with one safety rule removed each: the check above must reject every one.
_MUTANTS = {
    "unjudged clauses ignored": lambda js: fold_behavior_verdicts([j for j in js if j.evaluated]),
    "an unjudged clause outranks a violated one": lambda js: (
        V.NA if any(not j.evaluated for j in js) else fold_behavior_verdicts(js)
    ),
    "all-NA read as TRUE": _all_na_reads_true,
}


def test_there_are_fourteen_kinds_of_judgment():
    # If a field the fold reads gains a value, this fails and the proofs must be redone.
    assert len(_KINDS) == 14


def test_the_fold_equals_the_proved_model_for_every_possible_list():
    """All 2**14 sets of present kinds == every list of judgments, whatever its length."""
    for mask in range(1 << len(_KINDS)):
        present = [k for i, k in enumerate(_KINDS) if mask >> i & 1]
        assert _fold(present) is _model(present), present


def test_the_count_of_a_kind_never_matters():
    rng = random.Random(20261013)
    for _ in range(300):
        kinds = [rng.choice(_KINDS) for _ in range(rng.randint(1, 6))]
        assert _fold(kinds) is _fold(kinds * rng.randint(2, 5))


def test_the_order_of_judgments_never_matters():
    rng = random.Random(20261014)
    for _ in range(2000):
        kinds = [rng.choice(_KINDS) for _ in range(rng.randint(0, 12))]
        shuffled = list(kinds)
        rng.shuffle(shuffled)
        assert _fold(kinds) is _fold(shuffled)


@pytest.mark.parametrize("size", [0, 1, 2, 3])
def test_an_unjudged_clause_never_helps_a_behavior_pass(size):
    """The class of bug the reviews kept finding: a fail-open fold."""
    blockers = [k for k in _KINDS if _role(k) == "B"]
    for combo in itertools.combinations_with_replacement(_KINDS, size):
        for blocker in blockers:
            assert _fold(list(combo) + [blocker]) is not V.TRUE


def test_a_violated_clause_always_wins():
    for combo in itertools.combinations_with_replacement(_KINDS, 2):
        assert _fold(list(combo) + [(True, V.FALSE, None)]) is V.FALSE


def test_no_judgments_is_never_a_verdict():
    assert _fold([]) is V.NA


def test_true_needs_a_real_true_and_nothing_hidden():
    for mask in range(1 << len(_KINDS)):
        present = [k for i, k in enumerate(_KINDS) if mask >> i & 1]
        if _fold(present) is V.TRUE:
            roles = {_role(k) for k in present}
            assert "T" in roles and not roles & {"F", "B"}


def test_the_check_accepts_the_real_fold():
    assert not _disagrees_with_model(fold_behavior_verdicts)


@pytest.mark.parametrize("name", list(_MUTANTS))
def test_the_check_rejects_a_broken_fold(name):
    assert _disagrees_with_model(_MUTANTS[name])
