"""The name-bound judge API (PRD-09): how a registered validator is called, and
how a *score* it returns becomes a :class:`~argus.models.JudgeScore`.

A validator registered through ``validators=`` may declare any of ``output``,
``expected`` and ``metadata``; it is called with exactly those, bound by name.
Everything is deterministic user code: no LLM, no file loading.

Compatibility is the whole point. A validator that declares none of those names
keeps the call it always had, ``fn(output_snap)``, and every return shape the
legacy parser knew (``(ok, message[, severity])``, anything invalid) is still
judged by ``session._parse_validator_return`` and gates as before. Only the
shapes that parser rejected outright can mean a score, and a score is advisory:
at worst a ``warning``, never a changed status or exit code.
"""

from __future__ import annotations

import copy
import functools
import inspect
import numbers
from typing import Any, Callable

from argus.models import JudgeScore

# The only names a judge may declare. Matched exactly: a legacy ``fn(out)`` /
# ``fn(state)`` / ``lambda o: ...`` is not in this set and still gets the output
# positionally.
JUDGE_PARAMS = ("output", "expected", "metadata")

_DIRECTIONS = ("maximize", "minimize")
_DICT_KEYS = frozenset({"score", "label", "explanation", "direction"})
_MAX_TEXT = 200  # label / explanation cap: a run file is not a log


def check_floor(value: Any) -> float:
    """``judge_score_floor`` must be a number in [0, 1] (same rule as ArgusConfig)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not (0.0 <= value <= 1.0):
        raise ValueError(f"judge_score_floor must be between 0.0 and 1.0, got {value!r}")
    return float(value)


def _copy(value: Any) -> Any:
    """A deep copy, so a judge cannot corrupt the recording; the original if it
    cannot be copied (a snapshot is plain data, but ``expected`` is the user's)."""
    try:
        return copy.deepcopy(value)
    except Exception:
        return value


def invoke_validator(
    fn: Callable[..., Any], output_snap: dict[str, Any], expected: Any, metadata: dict[str, Any]
) -> Any:
    """Call a registered validator, binding the judge names it declares.

    A parameter bound by name is any ``output`` / ``expected`` / ``metadata`` that
    can be passed by keyword; ``output`` and ``expected`` arrive as deep copies.
    A callable that declares none of them (``lambda out: ...``, ``def v(state)``,
    ``*args``, a builtin, anything whose signature cannot be read) gets the live
    ``output_snap`` positionally, exactly as before PRD-09. So does one with a
    required parameter outside the vocabulary (``def v(state, expected=None)``) or
    a positional one ahead of the first it binds (``def v(x=None, output=None)``):
    it is called ``fn(output_snap)``, and if that leaves a required parameter
    unfilled the call raises and is reported as ``Validator raised`` (critical),
    as a mismatched legacy validator always was.

    A synchronous call, like every validator: a judge that blocks blocks the
    node it judges (bound it yourself if it can hang). An ``async def`` one
    returns a coroutine, which is an invalid return as ever; it is closed here so
    Python does not also warn that it was never awaited.
    """
    raw = _call(fn, output_snap, expected, metadata)
    if inspect.iscoroutine(raw):
        raw.close()
    return raw


def _bound_names(fn: Callable[..., Any]) -> list[str]:
    """The judge names ``fn`` is called with by keyword; empty for a legacy validator.

    ``fn`` is judged by its own signature (a ``functools.wraps`` wrapper is not
    read through to the function it wraps). It is name-bound only when every
    *required* parameter (no default, not ``*args`` / ``**kwargs``) is a judge name
    that can be passed by keyword, and no positional parameter that cannot be bound
    by name comes before the first one that can: that earlier parameter is where
    ``fn(output_snap)`` always put the output (``def v(x=None, output=None)``). Any
    other required parameter makes it a legacy ``fn(output_snap)`` validator, so an
    optional ``expected=None`` next to ``state`` changes nothing. A name a
    ``functools.partial`` already pins is the caller's, never bound. A signature that
    cannot be read for any reason (a ``__signature__`` property that raises, a
    ``__getattr__`` that fails on a dunder) leaves it a legacy validator.
    """
    try:
        params = list(inspect.signature(fn, follow_wrapped=False).parameters.values())
    except Exception:
        return []
    pinned: set[str] = set()
    inner = fn
    while isinstance(inner, functools.partial):
        pinned.update(inner.keywords)
        inner = inner.func
    keyword = (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
    positional = (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    variadic = (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
    bound: list[str] = []
    for p in params:
        if p.name in JUDGE_PARAMS and p.name not in pinned and p.kind in keyword:
            bound.append(p.name)
        elif p.default is p.empty and p.kind not in variadic:
            return []
        elif p.kind in positional and not bound:
            return []
    return bound


def is_name_bound(fn: Callable[..., Any]) -> bool:
    """Whether ``invoke_validator`` binds ``fn`` by name, so a score return may mean a score.

    A legacy validator (called ``fn(output_snap)``) never does: every return it
    makes goes to the legacy parser, whatever its shape.
    """
    return bool(_bound_names(fn))


def _call(
    fn: Callable[..., Any], output_snap: dict[str, Any], expected: Any, metadata: dict[str, Any]
) -> Any:
    bound = _bound_names(fn)
    if not bound:
        return fn(output_snap)
    available = {"output": output_snap, "expected": expected, "metadata": metadata}
    return fn(**{name: _copy(available[name]) for name in bound})


def _unit(score: Any) -> bool:
    """In [0, 1]; NaN is not. Compared as given, so a huge int cannot overflow a float."""
    return bool(0 <= score <= 1)


def _clip(text: str) -> str:
    return text if len(text) <= _MAX_TEXT else text[: _MAX_TEXT - 1] + "…"


def is_low(score: float, direction: str, floor: float) -> bool:
    """Under the floor, from the score's own good end: maximize wants high, minimize low."""
    return score < floor if direction == "maximize" else score > 1.0 - floor


def coerce_judge_score(
    raw: Any,
    *,
    validator_name: str = "",
    origin_node: str | None = None,
    step_index: int | None = None,
    floor: float = 0.5,
) -> JudgeScore | None:
    """A validator's return as a :class:`JudgeScore`, or ``None`` when it is not a score.

    Score shapes: a number in [0, 1] (a ``bool`` is not one), a dict whose keys
    are within ``score`` / ``label`` / ``explanation`` / ``direction`` and whose
    ``score`` is such a number, or a ``JudgeScore``. ``None`` means "not mine":
    the caller falls back to the legacy parser, so an invalid return gates as
    critical exactly as it always did. Never raises; never mutates ``raw``.

    ``severity`` is ``warning`` under ``floor`` and ``ok`` otherwise.
    """
    score: Any
    label: Any = "score"
    explanation: Any = ""
    direction: Any = "maximize"
    if isinstance(raw, JudgeScore):
        score, label, explanation, direction = raw.score, raw.label, raw.explanation, raw.direction
    elif isinstance(raw, dict):
        if not raw.keys() <= _DICT_KEYS:
            return None
        score = raw.get("score")
        label = raw.get("label", label)
        explanation = raw.get("explanation", explanation)
        direction = raw.get("direction", direction)
    else:
        score = raw
    if isinstance(score, bool) or not isinstance(score, numbers.Real) or not _unit(score):
        return None
    if not isinstance(label, str) or not isinstance(explanation, str):
        return None
    if direction not in _DIRECTIONS:
        return None
    score = float(score)
    return JudgeScore(
        score=score,
        label=_clip(label),
        explanation=_clip(explanation),
        direction=direction,
        validator_name=validator_name,
        severity="warning" if is_low(score, direction, floor) else "ok",
        origin_node=origin_node,
        step_index=step_index,
    )
