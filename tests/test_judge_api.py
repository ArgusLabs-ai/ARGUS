"""PRD-09: the name-bound judge API.

Validators may declare ``output`` / ``expected`` / ``metadata`` and are called
with those bound by name; a NEW return shape (score dict, float, JudgeScore)
becomes a ``JudgeScore`` on the run. Everything a legacy validator could return
before keeps gating exactly as ``session._parse_validator_return`` always did,
and a score never gates above warning.
"""

from __future__ import annotations

import functools
import gc
import inspect
import json
import operator
import warnings
from fractions import Fraction
from pathlib import Path

import pytest
from typer.testing import CliRunner

from argus.cli.main import app
from argus.findings import collect_findings
from argus.judge_api import _bound_names, _call, invoke_validator, is_name_bound
from argus.models import ArgusConfig, JudgeScore, ValidatorResult
from argus.session import ArgusSession
from argus.storage import load_run, resolve_run_path, save_run

runner = CliRunner()


def _drive(
    validators,
    *,
    expected=None,
    config=None,
    run_id=None,
    nodes=("a", "b"),
):
    """Run a pass-through pipeline (one wrapped call per node) and reload it."""
    session = ArgusSession(run_id=run_id, validators=validators, expected=expected, config=config)
    session.set_node_names(list(nodes))
    session.set_edges({a: [b] for a, b in zip(nodes, nodes[1:])})
    state: dict = {}
    for i, name in enumerate(nodes):
        state = session.wrap(name, lambda s, i=i: {**s, "xyz"[i % 3]: i + 1})(state)
    session.finalize()
    return load_run(session.run_id)


def _loop(validators, *, run_id=None, accumulate=None, times=2):
    """One node run `times` times in a cyclic graph (finalize is explicit).

    ``accumulate`` names a field reduced with ``operator.add``: such a step is never
    relabelled ``retried``, so every iteration stays active.
    """
    session = ArgusSession(run_id=run_id, validators=validators)
    if accumulate:
        session.reducer_fields = {accumulate: operator.add}
    session.set_node_names(["a"])
    session.set_edges({"a": ["a"]})
    a = session.wrap("a", lambda s: {**s, "n": s.get("n", 0) + 1})
    state: dict = {}
    for _ in range(times):
        state = a(state)
    session.finalize()
    return load_run(session.run_id)


def _only(record, node="a"):
    (event,) = [e for e in record.steps if e.node_name == node]
    return event


def _check(run_id: str):
    return runner.invoke(app, ["check", run_id, "--format", "json"])


def _finding_types(run_id: str) -> list[dict]:
    return json.loads(_check(run_id).output)["findings"]


# ── legacy contract: every shape the old parser knew gates exactly as before ──


class _Truthy:
    """A numpy-scalar stand-in: truthy, but neither a bool nor a plain int."""

    def __bool__(self):
        return True

    def __str__(self):
        return "ok"


async def _async_validator(output):
    return (True, "never awaited")


def _boom(output):
    raise RuntimeError("boom")


# (raw return, is_valid, severity); message prefix checked separately
_LEGACY = [
    pytest.param((True, "fine"), True, "ok", id="ok-tuple"),
    pytest.param([True, "as a list"], True, "ok", id="ok-list"),
    pytest.param((True, ""), True, "ok", id="valid-empty-message"),
    pytest.param((2, "msg"), True, "ok", id="truthy-int-in-tuple"),
    pytest.param((0.4, "partial"), True, "ok", id="truthy-float-in-tuple"),
    pytest.param((_Truthy(), "numpy-ish"), True, "ok", id="truthy-object-in-tuple"),
    pytest.param((False, "bad"), False, "critical", id="false-tuple"),
    pytest.param((0, "zero"), False, "critical", id="zero-in-tuple"),
    pytest.param((False, "x", "warning"), False, "warning", id="warning-token"),
    pytest.param((False, "x", " WARN "), False, "warning", id="warn-token"),
    pytest.param((False, "x", "blocking"), False, "critical", id="blocking-token"),
    pytest.param((False, "x", "crit"), False, "critical", id="unknown-token"),
    pytest.param((False, "x", None), False, "critical", id="none-token"),
    pytest.param((True, "x", "warning"), True, "ok", id="valid-ignores-token"),
    pytest.param(None, False, "critical", id="none"),
    pytest.param("FAIL: total 139 != 142", False, "critical", id="bare-str"),
    pytest.param(True, False, "critical", id="bare-true"),
    pytest.param(False, False, "critical", id="bare-false"),
    pytest.param((True,), False, "critical", id="one-tuple"),
    pytest.param(1.5, False, "critical", id="float-out-of-range"),
    pytest.param(-0.1, False, "critical", id="float-negative"),
    pytest.param(float("nan"), False, "critical", id="float-nan"),
    pytest.param(float("inf"), False, "critical", id="float-inf"),
    pytest.param(7, False, "critical", id="int-out-of-range"),
    pytest.param({"ok": True}, False, "critical", id="dict-no-score"),
    pytest.param({"score": 0.9, "bogus": 1}, False, "critical", id="dict-unknown-key"),
    pytest.param({"score": 7}, False, "critical", id="dict-score-out-of-range"),
    pytest.param({"score": "high"}, False, "critical", id="dict-score-not-numeric"),
    pytest.param({"score": True}, False, "critical", id="dict-score-bool"),
    pytest.param({"score": 0.5, "direction": "sideways"}, False, "critical", id="dict-bad-dir"),
    pytest.param({"score": 0.5, "label": 3}, False, "critical", id="dict-bad-label"),
    pytest.param({"score": 0.5, "explanation": None}, False, "critical", id="dict-bad-expl"),
    pytest.param(object(), False, "critical", id="object"),
    pytest.param(b"bytes", False, "critical", id="bytes"),
]


@pytest.mark.unit
@pytest.mark.parametrize("raw,is_valid,severity", _LEGACY)
def test_legacy_returns_gate_exactly_as_before(raw, is_valid, severity):
    """Registered validators keep the legacy verdict for every pre-existing shape."""
    record = _drive({"a": lambda out: raw}, run_id="legacy")
    (vr,) = _only(record).validator_results
    assert vr.validator_name == "a:<lambda>"
    assert (vr.is_valid, vr.severity) == (is_valid, severity)
    # The same oracle the base has always used.
    from argus.session import _parse_validator_return

    assert (vr.is_valid, vr.message, vr.severity) == _parse_validator_return(raw)
    blocking = (not is_valid) and severity != "warning"
    assert _only(record).status == ("semantic_fail" if blocking else "pass")
    assert record.judge_scores == []  # the legacy shapes carry no score
    exit_code = runner.invoke(app, ["check", "legacy"]).exit_code
    assert exit_code == (1 if blocking else 0)


@pytest.mark.unit
def test_legacy_invalid_messages_unchanged():
    record = _drive({"a": lambda out: "FAIL: total 139 != 142"})
    (vr,) = _only(record).validator_results
    assert vr.message == "Validator returned invalid result: 'FAIL: total 139 != 142'"


# What `lambda out: <value>` produced on the code before PRD-09 (run there, not
# derived here), as (is_valid, message, severity, node status). A legacy validator
# is called `fn(output_snap)` and judged by the legacy parser for EVERY return, so a
# bare number or a score-shaped dict is as invalid as it always was.
_BAD = "Validator returned invalid result: "
_BASE_RESULTS = [
    pytest.param(True, (False, _BAD + "True", "critical", "semantic_fail"), id="true"),
    pytest.param(False, (False, _BAD + "False", "critical", "semantic_fail"), id="false"),
    pytest.param(0, (False, _BAD + "0", "critical", "semantic_fail"), id="zero"),
    pytest.param(1, (False, _BAD + "1", "critical", "semantic_fail"), id="one"),
    pytest.param(0.0, (False, _BAD + "0.0", "critical", "semantic_fail"), id="zero-float"),
    pytest.param(1.0, (False, _BAD + "1.0", "critical", "semantic_fail"), id="one-float"),
    pytest.param(0.5, (False, _BAD + "0.5", "critical", "semantic_fail"), id="half"),
    pytest.param("yes", (False, _BAD + "'yes'", "critical", "semantic_fail"), id="str"),
    pytest.param(None, (False, _BAD + "None", "critical", "semantic_fail"), id="none"),
    pytest.param((True, "x"), (True, "x", "ok", "pass"), id="tuple-true"),
    pytest.param((False, "x"), (False, "x", "critical", "semantic_fail"), id="tuple-false"),
    pytest.param(
        {"score": 0.9},
        (False, _BAD + "{'score': 0.9}", "critical", "semantic_fail"),
        id="dict-score",
    ),
    pytest.param(
        {"score": 0.3, "label": "l", "explanation": "e", "direction": "minimize"},
        (
            False,
            _BAD + "{'score': 0.3, 'label': 'l', 'explanation': 'e', 'direction': 'minimize'}",
            "critical",
            "semantic_fail",
        ),
        id="dict-full",
    ),
    pytest.param(
        {"ok": True}, (False, _BAD + "{'ok': True}", "critical", "semantic_fail"), id="dict-ok"
    ),
    pytest.param(
        {"score": 7}, (False, _BAD + "{'score': 7}", "critical", "semantic_fail"), id="dict-big"
    ),
    # JudgeScore did not exist before PRD-09; it is just another invalid object here.
    pytest.param(
        JudgeScore(score=0.9, label="x"),
        (False, _BAD + repr(JudgeScore(score=0.9, label="x")), "critical", "semantic_fail"),
        id="judge-score",
    ),
]


@pytest.mark.unit
@pytest.mark.parametrize("raw,base", _BASE_RESULTS)
def test_a_legacy_validator_gets_the_pre_prd09_result_for_every_return(raw, base):
    """A bare `lambda out: <value>` is not a judge: no return of it can become a score."""
    record = _drive({"a": lambda out: raw})
    (vr,) = _only(record).validator_results
    assert (vr.is_valid, vr.message, vr.severity, _only(record).status) == base
    assert record.judge_scores == []


@pytest.mark.unit
def test_async_validator_stays_critical():
    """An `async def` validator returns a coroutine: invalid, critical, as before."""
    record = _drive({"a": _async_validator}, run_id="legacy-async")
    (vr,) = _only(record).validator_results
    assert (vr.is_valid, vr.severity) == (False, "critical")
    assert vr.message.startswith("Validator returned invalid result: <coroutine")
    assert _only(record).status == "semantic_fail"
    assert runner.invoke(app, ["check", "legacy-async"]).exit_code == 1


@pytest.mark.unit
def test_async_validator_does_not_leak_a_never_awaited_warning():
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _drive({"a": _async_validator})
        gc.collect()
    assert [str(w.message) for w in caught if "never awaited" in str(w.message)] == []


@pytest.mark.unit
def test_raising_validator_stays_critical_and_has_no_score():
    record = _drive({"a": _boom})
    (vr,) = _only(record).validator_results
    assert (vr.is_valid, vr.severity, vr.message) == (False, "critical", "Validator raised: boom")
    assert record.judge_scores == []


@pytest.mark.unit
def test_legacy_wildcard_then_specific_order_and_one_argument():
    calls = []

    def wild(output):
        calls.append(("wild", dict(output)))
        return (True, "ok")

    def specific(out):
        calls.append(("specific", dict(out)))
        return (True, "ok")

    record = _drive({"*": wild, "a": specific})
    assert [c[0] for c in calls] == ["wild", "specific", "wild"]
    names = [vr.validator_name for vr in _only(record).validator_results]
    assert names == ["*:wild", "a:specific"]
    assert record.overall_status == "clean"


# ── binding by name ──────────────────────────────────────────────────────────


@pytest.mark.unit
def test_judge_receives_output_expected_metadata_by_name():
    seen = {}

    def judge(output, expected, metadata):
        seen.update(output=output, expected=expected, metadata=metadata)
        return {"score": 0.9, "label": "close", "explanation": "e"}

    record = _drive({"b": judge}, expected={"b": {"total": 142}})
    assert seen["output"] == {"x": 1, "y": 2}
    assert seen["expected"] == {"total": 142}
    assert seen["metadata"] == {"node": "b", "step_index": 1, "run_id": record.run_id}


@pytest.mark.unit
def test_only_the_declared_names_are_bound():
    seen = {}

    def judge(expected, output, threshold=0.5):
        seen.update(expected=expected, output=output, threshold=threshold)
        return 1.0

    _drive({"a": judge}, expected={"a": 7})
    assert seen == {"expected": 7, "output": {"x": 1}, "threshold": 0.5}


@pytest.mark.unit
@pytest.mark.parametrize(
    "name,expected_arg",
    [
        ("expected", 142),
        ("metadata", {"node": "a", "step_index": 0}),
        ("output", {"x": 1}),
    ],
)
def test_a_single_vocabulary_parameter_binds_by_name(name, expected_arg):
    """`def judge(expected)` gets the expected value, not the output snapshot."""
    seen = []
    ns: dict = {}
    exec(f"def judge({name}):\n    seen.append({name})\n    return 1.0\n", {"seen": seen}, ns)
    record = _drive({"a": ns["judge"]}, expected={"a": 142})
    got = seen[0]
    if name == "metadata":
        assert got["node"] == "a" and got["step_index"] == 0 and got["run_id"] == record.run_id
    else:
        assert got == expected_arg
    (js,) = record.judge_scores
    assert js.score == 1.0


@pytest.mark.unit
@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda seen: lambda out: seen.append(out) or (True, "ok"), id="lambda-out"),
        pytest.param(lambda seen: lambda state: seen.append(state) or (True, "ok"), id="state"),
        pytest.param(
            lambda seen: lambda o, strict=False: seen.append(o) or (True, ""), id="o+default"
        ),
        pytest.param(
            lambda seen: lambda *args: seen.append(args[0]) or (True, "ok"), id="varargs"
        ),
        pytest.param(
            lambda seen: functools.partial(lambda tag, out: seen.append(out) or (True, ""), "t"),
            id="partial",
        ),
    ],
)
def test_names_outside_the_vocabulary_get_the_output_positionally(make):
    seen: list = []
    record = _drive({"a": make(seen)})
    assert seen == [{"x": 1}]
    assert record.overall_status == "clean"


@pytest.mark.unit
def test_builtin_and_callable_objects_do_not_break_signature_inspection():
    class Checker:
        def __call__(self, output, expected):
            return {"score": 1.0 if output == expected else 0.0}

    record = _drive({"a": repr, "b": Checker()}, expected={"b": {"x": 1, "y": 2}})
    (vr,) = _only(record, "a").validator_results
    assert vr.validator_name == "a:repr"
    assert "{'x': 1}" in vr.message  # the builtin was handed the output positionally
    assert [js.score for js in record.judge_scores] == [1.0]


@pytest.mark.unit
def test_keyword_only_parameters_bind_by_name():
    def judge(*, output, expected):
        return 1.0 if output["x"] == expected else 0.0

    record = _drive({"a": judge}, expected={"a": 1})
    assert record.judge_scores[0].score == 1.0


@pytest.mark.unit
def test_unbindable_required_parameter_is_a_critical_raise_as_before():
    def judge(snapshot, expected):
        return 1.0

    record = _drive({"a": judge}, expected={"a": 1})
    (vr,) = _only(record).validator_results
    assert (vr.is_valid, vr.severity) == (False, "critical")
    assert vr.message.startswith("Validator raised:")
    assert record.judge_scores == []


@pytest.mark.unit
def test_required_non_vocabulary_parameter_with_unbound_extras_raises_as_before():
    def judge(output, foo):
        return 1.0

    record = _drive({"a": judge})
    (vr,) = _only(record).validator_results
    assert (vr.is_valid, vr.severity) == (False, "critical")
    assert vr.message.startswith("Validator raised:") and "foo" in vr.message
    assert record.judge_scores == []


@pytest.mark.unit
@pytest.mark.parametrize("star", ["", "*, "])
@pytest.mark.parametrize("name", ["expected", "output", "metadata"])
def test_an_optional_vocabulary_name_beside_a_legacy_parameter_stays_legacy(name, star):
    """`def v(state, expected=None)` was called `fn(snap)` before PRD-09 and still is."""
    seen: list = []
    ns: dict = {}
    body = f"    seen.append((state, {name}))\n    return (True, 'ok')\n"
    exec(f"def v(state, {star}{name}=None):\n{body}", {"seen": seen}, ns)
    record = _drive({"a": ns["v"]}, expected={"a": 7})
    (vr,) = _only(record).validator_results
    assert (vr.is_valid, vr.severity) == (True, "ok")
    assert seen == [({"x": 1}, None)]  # called fn(snap): the named one keeps its default
    assert record.overall_status == "clean"
    assert record.judge_scores == []


@pytest.mark.unit
def test_a_legacy_validator_with_an_optional_vocabulary_name_keeps_the_legacy_parser():
    def v(state, expected=None):
        return 1.0

    record = _drive({"a": v})
    (vr,) = _only(record).validator_results
    assert (vr.is_valid, vr.severity) == (False, "critical")
    assert vr.message == "Validator returned invalid result: 1.0"
    assert record.judge_scores == []


# ── which callables are name-bound: wrappers, partials, leading parameters ───
#
# A callable is name-bound when its own signature lets ARGUS pass `output` /
# `expected` / `metadata` by keyword and nothing else needs the output. Anything
# else is called `fn(output_snap)`, exactly as before PRD-09. The corpus pins both
# halves for 63 shapes: the upstream result of `fn(snap)` is a literal, so a
# legacy shape cannot change silently.

_SNAP = {"x": 1}
_EXP = "E"
_META = {"node": "n"}


def _f_out(output):
    return ("out", output)


def _f_state(state):
    return ("state", state)


def _f_oe(output, expected):
    return ("oe", output, expected)


def _f_oe_none(output, expected=None):
    return ("oe?", output, expected)


def _f_oem(output, expected, metadata):
    return ("oem", output, expected, metadata)


def _f_eo_threshold(expected, output, threshold=0.5):
    return ("eot", expected, output, threshold)


def _f_oe_threshold(output, expected, threshold=0.5):
    return ("oet", output, expected, threshold)


def _f_state_exp(state, expected=None):
    return ("se", state, expected)


def _f_state_kwexp(state, *, expected=None):
    return ("s*e", state, expected)


def _f_x_out(x=None, output=None):
    return ("xo", x, output)


def _f_posx_out(x=None, /, output=None):
    return ("x/o", x, output)


def _f_x_kwout(x=0, *, output=None):
    return ("x*o", x, output)


def _f_kwx_out(*, x=None, output=None):
    return ("*xo", x, output)


def _f_out_x(output, x=None):
    return ("ox", output, x)


def _f_posout(output, /):
    return ("o/", output)


def _f_posout_exp(output, /, expected):
    return ("o/e", output, expected)


def _f_posout_expnone(output, /, expected=None):
    return ("o/e?", output, expected)


def _f_optposout_expnone(output=None, /, expected=None):
    return ("o?/e?", output, expected)


def _f_star_kwout(*args, output):
    return ("*ao", args, output)


def _f_out_star(output, *args):
    return ("o*a", output, args)


def _f_star(*args):
    return ("*a", args)


def _f_kwargs(**kw):
    return ("**k", sorted(kw))


def _f_abc(a, b, c):
    return ("abc", a, b, c)


def _f_a_out(a, output):
    return ("a_out", a, output)


class _CallOut:
    def __call__(self, output):
        return ("call.out", output)


class _CallState:
    def __call__(self, state):
        return ("call.state", state)


class _CallStar:
    def __call__(self, *args, **kw):
        return ("call.*", args, sorted(kw))


class _RaisingSignature:
    """Its `__signature__` property raises, so its signature cannot be read at all."""

    __name__ = "raising_signature"

    @property
    def __signature__(self):
        raise RuntimeError("boom")

    def __call__(self, output):
        return (True, "ok")


class _GetattrKeyError:
    """A configured callable: `__getattr__` reads a dict, so a dunder it lacks is a KeyError."""

    __name__ = "getattr_key_error"

    def __init__(self):
        self._cfg = {}

    def __getattr__(self, name):
        return self._cfg[name]

    def __call__(self, output):
        return (True, "ok")


class _Methods:
    def out(self, output):
        return ("m.out", output)

    def state(self, state):
        return ("m.state", state)

    @staticmethod
    def s_out(output):
        return ("s.out", output)

    @staticmethod
    def s_state(state):
        return ("s.state", state)

    @classmethod
    def c_out(cls, output):
        return ("c.out", output)

    @classmethod
    def c_state(cls, state):
        return ("c.state", state)


class _TupleOfOutput(tuple):
    def __new__(cls, output):
        return super().__new__(cls, ("cls", output))


def _wrap_x(f):
    @functools.wraps(f)
    def w(x):
        return f(x)

    return w


def _wrap_star(f):
    @functools.wraps(f)
    def w(*args, **kw):
        return f(*args, **kw)

    return w


def _wrap_named(f):
    @functools.wraps(f)
    def w(output, expected=None):
        return f(output)

    return w


def _wrap_star_with_signature(f):
    w = _wrap_star(f)
    w.__signature__ = inspect.signature(f)
    return w


def _pinned_with_dict(fn, **kw):
    p = functools.partial(fn, **kw)
    p.note = "has a __dict__, so a partial of it is not flattened"
    return p


_RAISE = ("raise", "TypeError")

# (id, callable, names it is bound by (empty: legacy), what upstream's fn(snap) returned,
# what the name-bound call returns). The shapes upstream called fn(snap) on must still be
# called exactly so.
_CORPUS = [
    ("lambda-out", lambda out: ("out", out), (), ("out", _SNAP), None),
    ("lambda-state", lambda state: ("state", state), (), ("state", _SNAP), None),
    ("lambda-default", lambda o=1: ("o=1", o), (), ("o=1", _SNAP), None),
    ("lambda-star", lambda *a: ("*a", a), (), ("*a", (_SNAP,)), None),
    ("lambda-none", lambda: "none", (), _RAISE, None),
    ("lambda-output", lambda output: ("out", output), ("output",), ("out", _SNAP), ("out", _SNAP)),
    (
        "lambda-expected",
        lambda expected: ("exp", expected),
        ("expected",),
        ("exp", _SNAP),
        ("exp", _EXP),
    ),
    (
        "lambda-metadata",
        lambda metadata=None: ("md", metadata),
        ("metadata",),
        ("md", _SNAP),
        ("md", _META),
    ),
    ("def-output", _f_out, ("output",), ("out", _SNAP), ("out", _SNAP)),
    ("def-state", _f_state, (), ("state", _SNAP), None),
    ("def-output-expected", _f_oe, ("output", "expected"), _RAISE, ("oe", _SNAP, _EXP)),
    (
        "def-output-expected-none",
        _f_oe_none,
        ("output", "expected"),
        ("oe?", _SNAP, None),
        ("oe?", _SNAP, _EXP),
    ),
    (
        "def-all-three",
        _f_oem,
        ("output", "expected", "metadata"),
        _RAISE,
        ("oem", _SNAP, _EXP, _META),
    ),
    (
        "def-expected-output-threshold",
        _f_eo_threshold,
        ("expected", "output"),
        _RAISE,
        ("eot", _EXP, _SNAP, 0.5),
    ),
    (
        "def-output-expected-threshold",
        _f_oe_threshold,
        ("output", "expected"),
        _RAISE,
        ("oet", _SNAP, _EXP, 0.5),
    ),
    ("def-output-then-extra", _f_out_x, ("output",), ("ox", _SNAP, None), ("ox", _SNAP, None)),
    ("def-state-optional-expected", _f_state_exp, (), ("se", _SNAP, None), None),
    ("def-state-kwonly-expected", _f_state_kwexp, (), ("s*e", _SNAP, None), None),
    ("def-x-then-output", _f_x_out, (), ("xo", _SNAP, None), None),
    ("def-posonly-x-then-output", _f_posx_out, (), ("x/o", _SNAP, None), None),
    ("def-x-then-kwonly-output", _f_x_kwout, (), ("x*o", _SNAP, None), None),
    ("def-kwonly-x-then-output", _f_kwx_out, ("output",), _RAISE, ("*xo", None, _SNAP)),
    ("def-posonly-output", _f_posout, (), ("o/", _SNAP), None),
    ("def-posonly-output-expected", _f_posout_exp, (), _RAISE, None),
    ("def-posonly-output-optional-expected", _f_posout_expnone, (), ("o/e?", _SNAP, None), None),
    (
        "def-optional-posonly-output-expected",
        _f_optposout_expnone,
        (),
        ("o?/e?", _SNAP, None),
        None,
    ),
    ("def-star-then-kwonly-output", _f_star_kwout, ("output",), _RAISE, ("*ao", (), _SNAP)),
    ("def-output-then-star", _f_out_star, ("output",), ("o*a", _SNAP, ()), ("o*a", _SNAP, ())),
    ("def-star", _f_star, (), ("*a", (_SNAP,)), None),
    ("def-kwargs", _f_kwargs, (), _RAISE, None),
    ("def-three-required", _f_abc, (), _RAISE, None),
    ("def-required-then-output", _f_a_out, (), _RAISE, None),
    ("callable-output", _CallOut(), ("output",), ("call.out", _SNAP), ("call.out", _SNAP)),
    ("callable-state", _CallState(), (), ("call.state", _SNAP), None),
    ("callable-star", _CallStar(), (), ("call.*", (_SNAP,), []), None),
    ("callable-raising-signature", _RaisingSignature(), (), (True, "ok"), None),
    ("callable-getattr-keyerror", _GetattrKeyError(), (), (True, "ok"), None),
    ("method-output", _Methods().out, ("output",), ("m.out", _SNAP), ("m.out", _SNAP)),
    ("method-state", _Methods().state, (), ("m.state", _SNAP), None),
    ("staticmethod-output", _Methods.s_out, ("output",), ("s.out", _SNAP), ("s.out", _SNAP)),
    ("staticmethod-state", _Methods.s_state, (), ("s.state", _SNAP), None),
    ("classmethod-output", _Methods.c_out, ("output",), ("c.out", _SNAP), ("c.out", _SNAP)),
    ("classmethod-state", _Methods.c_state, (), ("c.state", _SNAP), None),
    ("class-output", _TupleOfOutput, ("output",), ("cls", _SNAP), ("cls", _SNAP)),
    ("builtin-len", len, (), 1, None),
    ("builtin-repr", repr, (), "{'x': 1}", None),
    ("builtin-max", max, (), "x", None),
    ("builtin-dict", dict, (), {"x": 1}, None),
    (
        "partial-pinned-expected",
        functools.partial(_f_oe, expected="pinned"),
        ("output",),
        ("oe", _SNAP, "pinned"),
        ("oe", _SNAP, "pinned"),
    ),
    (
        "partial-pinned-output",
        functools.partial(_f_oe, output="pinned"),
        ("expected",),
        _RAISE,
        ("oe", "pinned", _EXP),
    ),
    ("partial-pinned-both", functools.partial(_f_oe, output="o", expected="e"), (), _RAISE, None),
    (
        "partial-pinned-all-three",
        functools.partial(_f_oem, output="o", expected="e", metadata="m"),
        (),
        _RAISE,
        None,
    ),
    (
        "partial-positional-output",
        functools.partial(_f_oe, "pinned"),
        ("expected",),
        ("oe", "pinned", _SNAP),
        ("oe", "pinned", _EXP),
    ),
    (
        "partial-positional-other",
        functools.partial(_f_a_out, "pa"),
        ("output",),
        ("a_out", "pa", _SNAP),
        ("a_out", "pa", _SNAP),
    ),
    (
        "partial-over-state",
        functools.partial(_f_state_exp, expected="pinned"),
        (),
        ("se", _SNAP, "pinned"),
        None,
    ),
    (
        "partial-pinned-one-of-three",
        functools.partial(_f_oem, expected="pinned"),
        ("output", "metadata"),
        _RAISE,
        ("oem", _SNAP, "pinned", _META),
    ),
    (
        "partial-of-partial-flattened",
        functools.partial(functools.partial(_f_oem, expected="pinned"), metadata="M"),
        ("output",),
        ("oem", _SNAP, "pinned", "M"),
        ("oem", _SNAP, "pinned", "M"),
    ),
    (
        "partial-of-partial-not-flattened",
        functools.partial(_pinned_with_dict(_f_oem, expected="pinned"), metadata="M"),
        ("output",),
        ("oem", _SNAP, "pinned", "M"),
        ("oem", _SNAP, "pinned", "M"),
    ),
    ("wraps-x-over-optional-expected", _wrap_x(_f_oe_none), (), ("oe?", _SNAP, None), None),
    ("wraps-x-over-output", _wrap_x(_f_out), (), ("out", _SNAP), None),
    ("wraps-star-over-output", _wrap_star(_f_out), (), ("out", _SNAP), None),
    (
        "wraps-named-over-state",
        _wrap_named(_f_state),
        ("output", "expected"),
        ("state", _SNAP),
        ("state", _SNAP),
    ),
    (
        "wraps-star-with-signature",
        _wrap_star_with_signature(_f_oe_none),
        ("output", "expected"),
        ("oe?", _SNAP, None),
        ("oe?", _SNAP, _EXP),
    ),
]


def _outcome(thunk):
    try:
        return thunk()
    except Exception as e:
        return ("raise", type(e).__name__)


@pytest.mark.unit
@pytest.mark.parametrize(
    "fn,bound,upstream,named", [pytest.param(*row[1:], id=row[0]) for row in _CORPUS]
)
def test_name_binding_over_a_corpus_of_callables(fn, bound, upstream, named):
    assert _bound_names(fn) == list(bound)
    assert is_name_bound(fn) is bool(bound)
    got = _outcome(lambda: _call(fn, dict(_SNAP), _EXP, dict(_META)))
    # a legacy shape is called exactly `fn(output_snap)`; a bound one by keyword
    assert got == (named if bound else upstream)
    assert _outcome(lambda: invoke_validator(fn, dict(_SNAP), _EXP, dict(_META))) == got


@pytest.mark.unit
@pytest.mark.parametrize(
    "fn", [_RaisingSignature(), _GetattrKeyError()], ids=["property", "getattr"]
)
def test_a_callable_whose_signature_cannot_be_read_is_a_valid_legacy_validator(fn):
    """Upstream called it `fn(snap)`; an unreadable signature is not `Validator raised`."""
    session = ArgusSession(validators={"*": fn})
    (vr,) = session._run_validators("a", {"x": 1})
    assert (vr.validator_name, vr.is_valid, vr.severity, vr.message) == (
        f"*:{fn.__name__}",
        True,
        "ok",
        "ok",
    )
    assert session._judge_scores == []


@pytest.mark.unit
def test_the_corpus_is_broad_and_its_ids_are_unique():
    ids = [row[0] for row in _CORPUS]
    assert len(ids) == len(set(ids)) >= 30
    assert any(row[2] for row in _CORPUS) and not all(row[2] for row in _CORPUS)


@pytest.mark.unit
def test_a_legacy_validator_gets_the_live_snapshot_not_a_copy():
    snap = {"x": [1]}
    got = _call(lambda state: state, snap, None, {})
    assert got is snap


# R2: `inspect.signature` follows `__wrapped__`; a wrapper is judged by its own.


@pytest.mark.unit
def test_a_wraps_wrapper_is_called_by_its_own_signature_not_the_wrapped_ones():
    def judge(output, expected=None):
        return (True, "fine")

    @functools.wraps(judge)
    def wrapper(x):
        return judge(x)

    record = _drive({"a": wrapper}, expected={"a": 1})
    (vr,) = _only(record).validator_results
    assert (vr.is_valid, vr.severity) == (True, "ok")
    assert not vr.message.startswith("Validator raised")
    assert record.judge_scores == []


@pytest.mark.unit
def test_a_wraps_wrapper_that_declares_the_judge_names_is_name_bound():
    def inner(state):
        return {"score": 0.9, "label": "inner"}

    @functools.wraps(inner)
    def wrapper(output, expected=None):
        return inner(output)

    (js,) = _drive({"a": wrapper}).judge_scores
    assert js.score == 0.9 and js.label == "inner"


@pytest.mark.unit
def test_a_star_wrapper_hides_the_judge_names_and_is_called_legacy():
    """The trade-off of judging a wrapper by its own signature: `*args, **kwargs` says nothing."""

    def judge(output, expected=None):
        return {"score": 0.9}

    @functools.wraps(judge)
    def wrapper(*args, **kwargs):
        return judge(*args, **kwargs)

    record = _drive({"a": wrapper})
    (vr,) = _only(record).validator_results
    assert (vr.is_valid, vr.severity) == (False, "critical")  # a bare score is legacy-invalid
    assert record.judge_scores == []


@pytest.mark.unit
def test_a_signature_on_the_star_wrapper_makes_it_name_bound_again():
    def judge(output, expected=None):
        return {"score": 0.9}

    @functools.wraps(judge)
    def wrapper(*args, **kwargs):
        return judge(*args, **kwargs)

    wrapper.__signature__ = inspect.signature(judge)  # type: ignore[attr-defined]
    (js,) = _drive({"a": wrapper}).judge_scores
    assert js.score == 0.9


# R1: a partial's pinned keyword is the user's, not a name to bind.


@pytest.mark.unit
def test_a_partial_keeps_the_keyword_it_pinned():
    seen: dict = {}

    def judge(output, expected):
        seen.update(output=output, expected=expected)
        return {"score": 0.9, "label": "pinned"}

    record = _drive(
        {"a": functools.partial(judge, expected="pinned")}, expected={"a": "from the run"}
    )
    assert seen == {"output": {"x": 1}, "expected": "pinned"}
    (js,) = record.judge_scores  # still name-bound through `output`: a score counts
    assert js.score == 0.9


@pytest.mark.unit
def test_a_partial_inside_a_partial_keeps_every_pinned_keyword():
    seen: dict = {}

    def judge(output, expected, metadata):
        seen.update(output=output, expected=expected, metadata=metadata)
        return 1.0

    inner = functools.partial(judge, expected="E!")
    inner.tag = "a __dict__ keeps functools from flattening the next partial into this one"
    record = _drive({"a": functools.partial(inner, metadata="M!")}, expected={"a": 7})
    assert seen == {"output": {"x": 1}, "expected": "E!", "metadata": "M!"}
    assert len(record.judge_scores) == 1


@pytest.mark.unit
def test_a_partial_that_pins_every_judge_name_is_called_as_before():
    """Nothing is left to bind, so it is `fn(output_snap)` as it was: and that raises."""

    def judge(output, expected):
        return 1.0

    p = functools.partial(judge, output="o", expected="e")
    assert not is_name_bound(p)
    record = _drive({"a": p})
    (vr,) = _only(record).validator_results
    assert vr.message.startswith("Validator raised:")
    assert record.judge_scores == []


# R4: the snapshot goes to the first positional parameter; it must not go missing.


@pytest.mark.unit
@pytest.mark.parametrize(
    "params,first",
    [
        ("x=None, output=None", "x"),
        ("x=None, /, output=None", "x"),
        ("x=None, *, expected=None", "x"),
        ("x=0, metadata=None", "x"),
        ("x=None, output=None, expected=None, metadata=None", "x"),
        ("output=None, /, expected=None", "output"),
    ],
)
def test_an_optional_parameter_ahead_of_the_judge_names_keeps_the_legacy_call(params, first):
    """`def v(x=None, output=None)` got the snapshot in `x` before PRD-09 and still does."""
    seen: list = []
    ns: dict = {}
    exec(
        f"def v({params}):\n    seen.append(dict(locals()))\n    return 0.9\n", {"seen": seen}, ns
    )
    record = _drive({"a": ns["v"]}, expected={"a": 7})
    assert seen[0].pop(first) == {"x": 1}
    assert set(seen[0].values()) == {None}  # the judge names keep their defaults
    (vr,) = _only(record).validator_results
    assert (vr.is_valid, vr.severity) == (False, "critical")  # legacy: a bare score is invalid
    assert record.judge_scores == []


@pytest.mark.unit
@pytest.mark.parametrize(
    "params",
    [
        "output, expected, threshold=0.5",
        "output, x=None",
        "expected, output, threshold=0.5, *args",
        "*, x=None, output=None",
        "output=None, x=None",
    ],
)
def test_parameters_after_the_judge_names_do_not_make_it_legacy(params):
    seen: list = []
    ns: dict = {}
    exec(
        f"def v({params}):\n    seen.append(dict(locals()))\n    return 0.9\n", {"seen": seen}, ns
    )
    record = _drive({"a": ns["v"]}, expected={"a": 7})
    assert seen[0]["output"] == {"x": 1}
    assert len(record.judge_scores) == 1


@pytest.mark.unit
def test_expected_is_looked_up_by_node_name_with_wildcard_fallback():
    seen = {}

    def judge(metadata, expected):
        seen[metadata["node"]] = expected
        return 1.0

    _drive({"*": judge}, expected={"a": 1, "*": "fallback"})
    assert seen == {"a": 1, "b": "fallback"}


@pytest.mark.unit
def test_a_falsy_expected_value_is_a_value_not_a_miss():
    seen = {}

    def judge(metadata, expected):
        seen[metadata["node"]] = expected
        return 1.0

    _drive({"*": judge}, expected={"a": 0, "b": {}, "*": "fallback"})
    assert seen == {"a": 0, "b": {}}


@pytest.mark.unit
def test_no_expected_binds_none():
    seen = []
    _drive({"a": lambda output, expected: seen.append(expected) or 1.0})
    assert seen == [None]


@pytest.mark.unit
def test_a_judge_that_mutates_its_inputs_cannot_corrupt_the_recording():
    expected = {"want": [1, 2]}

    def judge(output, expected):
        output["x"] = "CORRUPTED"
        output["injected"] = True
        expected["want"].append(99)
        return 1.0

    record = _drive({"a": judge}, expected={"a": expected})
    assert _only(record).output_dict == {"x": 1}
    assert expected == {"want": [1, 2]}
    assert _only(record, "b").input_state == {"x": 1}


@pytest.mark.unit
def test_a_judge_gets_a_fresh_copy_each_call():
    seen = []

    def judge(output, expected):
        seen.append(dict(expected))
        expected["n"] = expected.get("n", 0) + 1
        return 1.0

    _drive({"*": judge}, expected={"*": {}})
    assert seen == [{}, {}]


# ── scores: new shapes, and the warning ceiling ──────────────────────────────


@pytest.mark.unit
def test_dict_shape_becomes_a_judge_score_row():
    def judge(output, expected, metadata):
        return {
            "score": 0.91,
            "label": "close",
            "explanation": "0.91 of the way",
            "direction": "maximize",
        }

    record = _drive({"a": judge})
    (js,) = record.judge_scores
    assert isinstance(js, JudgeScore)
    assert (js.validator_name, js.origin_node, js.step_index) == ("a:judge", "a", 0)
    assert (js.score, js.label, js.explanation, js.direction) == (
        0.91,
        "close",
        "0.91 of the way",
        "maximize",
    )
    assert js.severity == "ok"
    (vr,) = _only(record).validator_results
    assert (vr.is_valid, vr.severity) == (True, "ok")


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw,score",
    [(0.25, 0.25), (1, 1.0), (0, 0.0), (1.0, 1.0), (Fraction(1, 2), 0.5)],
)
def test_bare_number_in_range_is_a_score(raw, score):
    record = _drive({"a": lambda output: raw})
    (js,) = record.judge_scores
    assert js.score == score and isinstance(js.score, float)
    assert (js.label, js.direction) == ("score", "maximize")


@pytest.mark.unit
def test_returning_a_judge_score_object_is_accepted():
    def judge(output):
        return JudgeScore(score=0.7, label="fine", explanation="x", direction="maximize")

    record = _drive({"a": judge})
    (js,) = record.judge_scores
    assert (js.score, js.label, js.validator_name, js.origin_node) == (0.7, "fine", "a:judge", "a")


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw",
    [
        0.0,
        0.4,
        {"score": 0.1, "label": "bad"},
        {"score": 0.0},
        JudgeScore(score=0.2, label="low"),
    ],
)
def test_a_low_score_never_gates_above_warning(raw):
    """PRD S-09.3: reviewer, not gate — status, run, and exit code are untouched."""
    record = _drive({"a": lambda output: raw}, run_id="lowscore")
    event = _only(record)
    assert event.status == "pass"
    assert record.overall_status == "clean"
    (vr,) = event.validator_results
    assert vr.is_valid is False and vr.severity == "warning" and not vr.is_blocking
    (js,) = record.judge_scores
    assert js.severity == "warning"
    assert runner.invoke(app, ["check", "lowscore"]).exit_code == 0
    assert runner.invoke(app, ["check", "lowscore", "--strict", "warn_as_fail"]).exit_code == 0
    types = {f["type"]: f for f in _finding_types("lowscore")}
    assert types["low_judge_score"]["severity"] == "warning"
    assert all(f["severity"] != "critical" for f in types.values())


@pytest.mark.unit
def test_a_high_score_is_clean_and_has_no_finding():
    record = _drive({"a": lambda output: 0.9}, run_id="highscore")
    assert record.findings == []
    (vr,) = _only(record).validator_results
    assert vr.is_valid is True


@pytest.mark.unit
def test_low_score_finding_names_the_node_and_the_judgment():
    def judge(output):
        return {"score": 0.3, "label": "partial", "explanation": "missing the total"}

    record = _drive({"b": judge}, run_id="lowfind")
    (f,) = record.findings
    assert (f.type, f.severity, f.source, f.node, f.origin_node) == (
        "low_judge_score",
        "warning",
        "validator",
        "b",
        "b",
    )
    assert "partial" in f.reason and "missing the total" in f.reason and "0.30" in f.reason
    assert f.field_path.startswith("b:judge")


@pytest.mark.unit
def test_low_score_does_not_also_file_the_generic_validator_finding():
    record = _drive({"a": lambda output: 0.1})
    assert [f.type for f in record.findings] == ["low_judge_score"]


@pytest.mark.unit
def test_a_legacy_warning_validator_still_files_its_validator_finding():
    record = _drive({"a": lambda out: (False, "terse", "warning")})
    assert [f.type for f in record.findings] == ["validator"]
    assert record.judge_scores == []


@pytest.mark.unit
def test_minimize_direction_is_judged_by_its_own_good_end():
    low = {"score": 0.3, "label": "tight", "direction": "minimize"}
    high = {"score": 0.9, "label": "bloated", "direction": "minimize"}
    good = _drive({"a": lambda output: low}, run_id="min-good")
    bad = _drive({"a": lambda output: high}, run_id="min-bad")
    assert good.judge_scores[0].severity == "ok" and good.findings == []
    assert _only(good).validator_results[0].is_valid is True
    assert bad.judge_scores[0].severity == "warning"
    assert [f.type for f in bad.findings] == ["low_judge_score"]
    assert _only(bad).status == "pass"


@pytest.mark.unit
@pytest.mark.parametrize(
    "floor,score,low",
    [
        (0.5, 0.5, False),
        (0.5, 0.49, True),
        (0.9, 0.85, True),
        (0.0, 0.0, False),
        (1.0, 0.99, True),
    ],
)
def test_the_floor_is_configurable_and_inclusive(floor, score, low):
    record = _drive({"a": lambda output: score}, config=ArgusConfig(judge_score_floor=floor))
    assert (record.judge_scores[0].severity == "warning") is low
    assert bool(record.findings) is low


@pytest.mark.unit
@pytest.mark.parametrize("bad", [-0.1, 1.1, float("nan")])
def test_config_rejects_a_floor_outside_zero_one(bad):
    with pytest.raises(ValueError, match="judge_score_floor"):
        ArgusConfig(judge_score_floor=bad)


@pytest.mark.unit
def test_label_and_explanation_are_capped():
    def judge(output):
        return {"score": 0.9, "label": "L" * 5000, "explanation": "E" * 5000}

    (js,) = _drive({"a": judge}).judge_scores
    assert len(js.label) <= 200 and len(js.explanation) <= 200
    assert js.label.startswith("LLL") and js.explanation.startswith("EEE")


@pytest.mark.unit
def test_a_score_row_exists_per_invocation_even_when_the_node_repeats():
    record = _loop({"a": lambda output: 0.9})
    assert [js.step_index for js in record.judge_scores] == [0, 1]


@pytest.mark.unit
def test_the_same_judge_twice_on_one_node_keeps_both_findings():
    """An accumulator keeps both iterations active: two ids, neither reason dropped."""
    record = _loop(
        {"a": lambda output: {"score": 0.1, "label": f"n={output['n']}"}}, accumulate="n"
    )
    low = [f for f in record.findings if f.type == "low_judge_score"]
    assert len(low) == 2
    assert len({f.id for f in low}) == 2
    assert sorted(f.reason.count("n=") for f in low) == [1, 1]


@pytest.mark.unit
def test_a_retried_iteration_files_no_finding():
    """Retried steps do not count against the run, and neither do their scores."""
    scores = iter([0.1, 0.9])
    record = _loop({"a": lambda output: next(scores)})
    assert [e.status for e in record.steps] == ["retried", "pass"]
    assert len(record.judge_scores) == 2  # both invocations are on record
    assert record.findings == []


@pytest.mark.unit
def test_the_investigator_does_not_list_a_low_judge_score_as_a_failed_validator():
    """A judge is advisory: its low score is not a failed validator in the briefing."""
    from argus.llm_investigator import _compress_node_summary

    def warned(out):
        return (False, "legacy warning", "warning")

    record = _drive(
        {"a": lambda output: {"score": 0.1, "label": "bad"}, "b": warned},
        run_id="inv-judge",
    )
    assert [js.severity for js in record.judge_scores] == ["warning"]
    summaries = {s["node"]: s for s in _compress_node_summary(record)}
    assert "failed_validators" not in summaries["a"]
    # a legacy validator's warning is as it was: still listed
    assert [v["name"] for v in summaries["b"]["failed_validators"]] == ["b:warned"]


def _semantic_fail_run(results, scores):
    from conftest import make_event, make_run_record

    event = make_event(
        node_name="n", step_index=0, status="semantic_fail", validator_results=results
    )
    record = make_run_record(events=[event], status="fail")
    record.judge_scores = scores
    return record


def _low_score():
    return JudgeScore(
        score=0.1,
        label="bad",
        validator_name="*:judge",
        origin_node="n",
        step_index=0,
        severity="warning",
    )


@pytest.mark.unit
def test_a_semantic_failure_does_not_blame_a_low_judge_score():
    """The trigger text names failed validators; a judge's low score is not one."""
    from argus.llm_investigator import should_investigate
    from argus.models import LLMInvestigationConfig

    record = _semantic_fail_run(
        [ValidatorResult("*:judge", False, "bad", "warning")], [_low_score()]
    )
    _, reasons = should_investigate(record, LLMInvestigationConfig(enabled=True))
    assert reasons == ["semantic failure at 'n'"]


@pytest.mark.unit
def test_a_semantic_failure_still_names_legacy_validators_beside_a_judge():
    from argus.llm_investigator import _compress_node_summary, should_investigate
    from argus.models import LLMInvestigationConfig

    record = _semantic_fail_run(
        [
            ValidatorResult("*:legacy", False, "boom", "critical"),
            ValidatorResult("*:warned", False, "w", "warning"),  # a legacy (False, msg, "warning")
            ValidatorResult("*:judge", False, "bad", "warning"),
        ],
        [_low_score()],
    )
    _, reasons = should_investigate(record, LLMInvestigationConfig(enabled=True))
    assert reasons == ["semantic failure at 'n' (validators: *:legacy, *:warned)"]
    (summary,) = _compress_node_summary(record)
    assert [v["name"] for v in summary["failed_validators"]] == ["*:legacy", "*:warned"]


@pytest.mark.unit
def test_collect_findings_unit_distinguishes_steps_and_skips_inactive():
    from conftest import make_event

    steps = [
        make_event(node_name="a", step_index=0),
        make_event(node_name="a", step_index=1),
        make_event(node_name="a", step_index=2, status="retried"),
    ]
    scores = [
        JudgeScore(
            score=0.1,
            label="x",
            validator_name="*:j",
            origin_node="a",
            step_index=i,
            severity="warning",
        )
        for i in range(3)
    ]
    out = collect_findings(steps, judge_scores=scores)
    assert [f.type for f in out] == ["low_judge_score"] * 2
    assert len({f.id for f in out}) == 2


def _null_step_record(run_id, first, last):
    """A retried event with no step index (a hand-edited or old run file) beside judge
    scores that have none either, on an active node; and a retried step 7 with a score."""
    from conftest import make_event, make_run_record

    def score(step, node, value):
        return JudgeScore(
            score=value,
            label="L",
            validator_name="*:j",
            origin_node=node,
            step_index=step,
            severity="warning" if value < 0.5 else "ok",
        )

    steps = [
        make_event(node_name="a", step_index=None, status="retried"),
        make_event(node_name="b", step_index=7, status="retried"),
        make_event(node_name="c", step_index=8),
    ]
    record = make_run_record(events=steps, run_id=run_id)
    record.judge_scores = [score(None, "x", first), score(7, "b", 0.22), score(8, "c", last)]
    return record


@pytest.mark.unit
def test_a_retried_event_without_a_step_index_does_not_hide_the_scores_without_one():
    record = _null_step_record("null-f", 0.11, 0.12)
    out = collect_findings(record.steps, judge_scores=record.judge_scores)
    # the score with no step and the one on step 8 stay; the retried step 7's is dropped
    assert sorted(f.field_path for f in out) == ["*:j@8", "*:j@None"]


@pytest.mark.unit
def test_exception_in_a_judge_produces_no_score_row_and_stays_critical():
    def judge(output):
        raise ValueError("nope")

    record = _drive({"a": judge})
    assert record.judge_scores == []
    assert _only(record).validator_results[0].severity == "critical"


@pytest.mark.unit
def test_a_session_that_begins_a_new_run_does_not_carry_scores_over():
    session = ArgusSession(validators={"a": lambda output: 0.9})
    session.set_node_names(["a"])
    session.wrap("a", lambda s: {**s, "x": 1})({})
    session.finalize()
    first = load_run(session.run_id)
    session.begin_new_run()
    session.wrap("a", lambda s: {**s, "x": 2})({})
    session.finalize()
    second = load_run(session.run_id)
    assert first.run_id != second.run_id
    assert len(first.judge_scores) == 1 and len(second.judge_scores) == 1


# ── persistence ──────────────────────────────────────────────────────────────


@pytest.mark.unit
def test_judge_scores_round_trip():
    from conftest import make_event, make_run_record

    record = make_run_record(events=[make_event()], run_id="rt")
    record.judge_scores.append(
        JudgeScore(
            score=0.91,
            label="close",
            explanation="e",
            direction="minimize",
            validator_name="*:judge",
            severity="warning",
            origin_node="node_a",
            step_index=3,
        )
    )
    save_run(record)
    (js,) = load_run("rt").judge_scores
    assert js == record.judge_scores[0]


def _rewrite(run_id: str, **fields) -> None:
    path = resolve_run_path(run_id)
    data = json.loads(path.read_text())
    for key, value in fields.items():
        if value is KeyError:
            data.pop(key, None)
        else:
            data[key] = value
    path.write_text(json.dumps(data))


@pytest.mark.unit
def test_an_old_record_without_the_key_loads_empty():
    _drive({}, run_id="old")
    _rewrite("old", judge_scores=KeyError)
    assert load_run("old").judge_scores == []


_JUNK_ROWS = {
    "score": None,
    "label": ["x"],
    "explanation": {},
    "direction": 7,
    "validator_name": 5,
    "origin_node": 3,
    "step_index": "x",
    "severity": [],
}


@pytest.mark.unit
@pytest.mark.parametrize(
    "junk",
    [
        None,
        "oops",
        5,
        True,
        {},
        {"a": 1},
        [None, "x", 3, [], True],
        [{"score": "high"}],
        [{"score": float("nan")}],
        [{"score": 1e999}],
        [{"score": 7, "direction": "sideways"}],
        [_JUNK_ROWS],
    ],
    ids=lambda j: type(j).__name__ + str(j)[:20],
)
def test_hand_edited_judge_scores_never_make_a_run_unreadable(junk):
    _drive({"a": lambda output: 0.9}, run_id="edit")
    _rewrite("edit", judge_scores=junk)
    record = load_run("edit")
    assert isinstance(record.judge_scores, list)
    for js in record.judge_scores:
        assert isinstance(js, JudgeScore)
        assert 0.0 <= js.score <= 1.0
        assert js.direction in ("maximize", "minimize")
        assert isinstance(js.label, str) and isinstance(js.explanation, str)
        assert isinstance(js.validator_name, str)
    for args in (["stats", "edit"], ["check", "edit"], ["show", "edit"], ["diff", "edit", "edit"]):
        result = runner.invoke(app, args)
        assert result.exception is None or isinstance(result.exception, SystemExit), args


# ── `argus stats <run>` ──────────────────────────────────────────────────────


def _named(fn, name):
    fn.__name__ = name
    return fn


@pytest.mark.unit
def test_stats_run_lists_each_score_with_its_direction():
    def judge(output):
        return {"score": 0.9, "label": "close", "explanation": "nearly", "direction": "maximize"}

    def tight(output):
        return {"score": 0.2, "label": "tight", "direction": "minimize"}

    record = _drive({"a": judge, "b": tight}, run_id="st-1")
    result = runner.invoke(app, ["stats", record.run_id])
    assert result.exit_code == 0, result.output
    out = result.output
    for token in ("a:judge", "b:tight", "maximize", "minimize", "0.90", "0.20", "close", "nearly"):
        assert token in out, token


@pytest.mark.unit
def test_stats_run_accepts_last_and_a_prefix():
    _drive({"a": lambda output: 0.5}, run_id="20261013-000000-abcdef12")
    assert "a:<lambda>" in runner.invoke(app, ["stats", "last"]).output
    assert "a:<lambda>" in runner.invoke(app, ["stats", "20261013"]).output


@pytest.mark.unit
def test_stats_run_without_scores_says_so():
    record = _drive({"a": lambda out: (True, "ok")}, run_id="st-none")
    result = runner.invoke(app, ["stats", record.run_id])
    assert result.exit_code == 0
    assert "No judge scores" in result.output


@pytest.mark.unit
def test_stats_run_lists_only_the_active_pass_of_a_loop():
    """A retried pass does not count against the run, so its score is not listed."""
    scores = iter([0.11, 0.22, 0.93])
    record = _loop({"a": _named(lambda output: next(scores), "j")}, run_id="st-loop", times=3)
    assert [e.status for e in record.steps] == ["retried", "retried", "pass"]
    assert len(record.judge_scores) == 3  # all three are on record
    out = runner.invoke(app, ["stats", "st-loop"]).output
    assert "0.93" in out
    assert "0.11" not in out and "0.22" not in out


@pytest.mark.unit
def test_stats_run_keeps_the_scores_without_a_step_beside_a_retried_event_without_one():
    save_run(_null_step_record("st-null", 0.11, 0.93))
    out = runner.invoke(app, ["stats", "st-null"]).output
    assert "0.11" in out and "0.93" in out
    assert "0.22" not in out  # the retried step 7's score is still hidden


@pytest.mark.unit
def test_stats_unknown_run_is_a_clean_error():
    result = runner.invoke(app, ["stats", "does-not-exist"])
    assert result.exit_code != 0
    assert "Error" in result.output
    assert not isinstance(result.exception, (KeyError, TypeError, AttributeError))


@pytest.mark.unit
def test_stats_without_a_run_is_still_the_signature_report():
    result = runner.invoke(app, ["stats"])
    assert result.exit_code == 0
    assert "Judge scores" not in result.output


@pytest.mark.unit
def test_stats_run_escapes_everything_user_supplied():
    def judge(output):
        return {
            "score": 0.9,
            "label": "[/oops]",
            "explanation": "[bold]x[/",
            "direction": "maximize",
        }

    record = _drive({"a": _named(judge, "[/x]")}, run_id="st-esc")
    result = runner.invoke(app, ["stats", record.run_id])
    assert result.exit_code == 0, result.output
    assert result.exception is None
    assert "[/x]" in result.output and "[/oops]" in result.output and "[bold]x[/" in result.output


@pytest.mark.unit
@pytest.mark.parametrize(
    "validator,update",
    [
        (lambda output: {"score": 0.2, "label": "bad", "explanation": "got [/x] and [bold"}, {}),
        (lambda out: (False, "legacy [/x]", "warning"), {}),
        (lambda out: (False, "legacy critical [/x]"), {"k": 1}),
    ],
    ids=["judge-score-on-failed-node", "legacy-warning-on-failed-node", "legacy-critical"],
)
def test_show_survives_markup_in_a_validator_message(validator, update):
    """A judge's explanation is free text; `argus show` must print it, not parse it."""
    session = ArgusSession(validators={"a": validator})
    session.set_node_names(["a", "b"])
    session.set_edges({"a": ["b"]})
    a = session.wrap("a", lambda s: update)  # `{}`: an empty update, a silent failure on `a`
    b = session.wrap("b", lambda s: {"k": 1})
    b(a({}))
    session.finalize()
    result = runner.invoke(app, ["show", session.run_id])
    assert result.exception is None, result.exception
    assert "[/x]" in result.output


# ── `argus diff` ─────────────────────────────────────────────────────────────


def _wildcard_judge(scores_by_node):
    def judge(metadata):
        return {"score": scores_by_node[metadata["node"]], "label": "s"}

    return judge


def _row(output: str, node: str) -> str:
    rows = [ln for ln in output.splitlines() if "*:judge" in ln and f"@ {node}" in ln]
    assert len(rows) == 1, (node, output)
    return rows[0]


@pytest.mark.unit
def test_diff_pairs_a_wildcard_judge_per_node():
    before = _drive({"*": _wildcard_judge({"a": 0.9, "b": 0.3})}, run_id="df-a")
    after = _drive({"*": _wildcard_judge({"a": 0.9, "b": 0.8})}, run_id="df-b")
    result = runner.invoke(app, ["diff", before.run_id, after.run_id])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "judge scores" in out
    assert "0.30 → 0.80" in _row(out, "b") and "Δ +0.50" in _row(out, "b")
    assert "0.90 → 0.90" in _row(out, "a") and "Δ +0.00" in _row(out, "a")


@pytest.mark.unit
def test_diff_normalizes_direction_so_positive_is_always_better():
    def make(score):
        return _named(
            lambda output: {"score": score, "label": "s", "direction": "minimize"}, "judge"
        )

    before = _drive({"a": make(0.2)}, run_id="df-m1")
    after = _drive({"a": make(0.7)}, run_id="df-m2")
    out = runner.invoke(app, ["diff", before.run_id, after.run_id]).output
    assert "0.20 → 0.70" in out and "Δ -0.50" in out and "minimize" in out


@pytest.mark.unit
def test_diff_marks_new_and_removed_scores():
    before = _drive({"a": _named(lambda output: 0.5, "old")}, run_id="df-n1")
    after = _drive({"a": _named(lambda output: 0.5, "fresh")}, run_id="df-n2")
    out = runner.invoke(app, ["diff", before.run_id, after.run_id]).output
    assert "a:old" in out and "removed" in out
    assert "a:fresh" in out and "new" in out


@pytest.mark.unit
def test_diff_does_not_compare_scores_across_a_direction_change():
    before = _drive({"a": _named(lambda output: {"score": 0.2}, "j")}, run_id="df-d1")
    after = _drive(
        {"a": _named(lambda output: {"score": 0.2, "direction": "minimize"}, "j")}, run_id="df-d2"
    )
    out = runner.invoke(app, ["diff", before.run_id, after.run_id]).output
    assert "direction changed" in out
    assert "Δ" not in out


@pytest.mark.unit
def test_diff_pairs_repeated_runs_of_a_node_in_order():
    def run(rid, first, second):
        scores = iter([first, second])
        _loop({"a": _named(lambda output: next(scores), "j")}, run_id=rid, accumulate="n")

    run("df-r1", 0.2, 0.4)
    run("df-r2", 0.2, 0.9)
    out = runner.invoke(app, ["diff", "df-r1", "df-r2"]).output
    assert "0.20 → 0.20" in out and "0.40 → 0.90" in out


@pytest.mark.unit
def test_diff_compares_only_the_active_pass_of_a_loop():
    def run(rid, last):
        scores = iter([0.11, 0.22, last])
        _loop({"a": _named(lambda output: next(scores), "j")}, run_id=rid, times=3)

    run("df-l1", 0.4)
    run("df-l2", 0.9)
    out = runner.invoke(app, ["diff", "df-l1", "df-l2"]).output
    assert "0.40 → 0.90" in out
    assert "0.11" not in out and "0.22" not in out
    assert "#" not in out  # one active pass per side: nothing to number


@pytest.mark.unit
def test_diff_keeps_the_scores_without_a_step_beside_a_retried_event_without_one():
    save_run(_null_step_record("df-null1", 0.11, 0.93))
    save_run(_null_step_record("df-null2", 0.31, 0.60))
    out = runner.invoke(app, ["diff", "df-null1", "df-null2"]).output
    assert "0.11 → 0.31" in out and "0.93 → 0.60" in out
    assert "0.22" not in out


@pytest.mark.unit
def test_diff_without_scores_has_no_section_and_exit_zero():
    a = _drive({}, run_id="df-x1")
    b = _drive({}, run_id="df-x2")
    result = runner.invoke(app, ["diff", a.run_id, b.run_id])
    assert result.exit_code == 0
    assert "judge scores" not in result.output


@pytest.mark.unit
def test_diff_escapes_validator_names():
    before = _drive({"a": _named(lambda output: 0.5, "[/x]")}, run_id="df-e1")
    after = _drive({"a": _named(lambda output: 0.6, "[/x]")}, run_id="df-e2")
    result = runner.invoke(app, ["diff", before.run_id, after.run_id])
    assert result.exception is None
    assert "[/x]" in result.output


# ── threading: watcher, new_session, recorder ────────────────────────────────


@pytest.mark.unit
def test_watcher_accepts_expected():
    from argus.watcher import ArgusWatcher

    assert ArgusWatcher(validators={}, expected={"*": 1})._expected == {"*": 1}


@pytest.mark.unit
def test_new_session_threads_expected_and_floor():
    from argus.grading import new_session

    session = new_session(
        ["a"],
        {},
        set(),
        {},
        judge=False,
        validators={},
        strict=False,
        max_field_size=50_000,
        expected={"a": 1},
        judge_score_floor=0.8,
    )
    assert session._expected == {"a": 1}
    assert session._judge_score_floor == 0.8


@pytest.mark.unit
def test_session_floor_kwarg_overrides_the_config():
    session = ArgusSession(config=ArgusConfig(judge_score_floor=0.2), judge_score_floor=0.7)
    assert session._judge_score_floor == 0.7
    assert ArgusSession(config=ArgusConfig(judge_score_floor=0.2))._judge_score_floor == 0.2
    assert ArgusSession()._judge_score_floor == 0.5


@pytest.mark.unit
@pytest.mark.parametrize("bad", [-0.1, 1.5, float("nan"), "0.5", True])
def test_recorder_rejects_a_bad_floor_at_construction(bad):
    pytest.importorskip("langchain_core")
    from argus.recorder import ArgusRecorder

    with pytest.raises(ValueError, match="judge_score_floor"):
        ArgusRecorder(judge_score_floor=bad)


@pytest.mark.integration
def test_recorder_path_binds_expected_and_honors_the_floor():
    """The primary path: ArgusRecorder(validators, expected, judge_score_floor)."""
    pytest.importorskip("langchain_core")
    pytest.importorskip("langgraph")
    from typing import TypedDict

    from langgraph.graph import END, START, StateGraph

    from argus.recorder import ArgusRecorder

    class S(TypedDict, total=False):
        total: int

    def calc(state: S) -> dict:
        return {"total": 139}

    def judge(output, expected):
        return {"score": 0.85, "label": "close", "explanation": f"{output['total']} vs {expected}"}

    g = StateGraph(S)
    g.add_node("calc", calc)
    g.add_edge(START, "calc")
    g.add_edge("calc", END)

    def run(floor):
        recorder = ArgusRecorder(
            validators={"calc": judge},
            expected={"calc": 142},
            judge_score_floor=floor,
            semantic_judge=False,
        )
        recorder.attach(g.compile()).invoke({})
        return load_run(recorder.session.run_id)

    lenient = run(0.5)
    (js,) = lenient.judge_scores
    assert js.explanation == "139 vs 142" and js.severity == "ok"
    assert lenient.findings == []

    strict = run(0.9)
    assert strict.judge_scores[0].severity == "warning"
    assert [f.type for f in strict.findings] == ["low_judge_score"]
    assert strict.overall_status == "clean"


@pytest.mark.integration
def test_watcher_path_binds_expected_honors_the_config_floor_and_resets_per_invoke():
    """ArgusWatcher reuses one session across invokes: scores must not leak between runs."""
    pytest.importorskip("langgraph")
    from typing import TypedDict

    from langgraph.graph import END, StateGraph

    from argus import ArgusWatcher

    class S(TypedDict, total=False):
        total: int

    def calc(state: S) -> dict:
        return {"total": 139}

    def judge(output, expected):
        return {"score": 0.85, "label": "close", "explanation": f"{output['total']} vs {expected}"}

    g = StateGraph(S)
    g.add_node("calc", calc)
    g.set_entry_point("calc")
    g.add_edge("calc", END)
    watcher = ArgusWatcher(
        validators={"calc": judge},
        expected={"calc": 142},
        config=ArgusConfig(judge_score_floor=0.9),
    )
    app = watcher.attach(g)

    app.invoke({})
    first = load_run(watcher.run_id)
    app.invoke({})
    second = load_run(watcher.run_id)

    assert first.run_id != second.run_id
    for record in (first, second):
        (js,) = record.judge_scores
        assert js.explanation == "139 vs 142" and js.severity == "warning"
        assert [f.type for f in record.findings] == ["low_judge_score"]
        assert record.overall_status == "clean"


# ── unit: coerce_judge_score ─────────────────────────────────────────────────


@pytest.mark.unit
class TestCoerce:
    def test_returns_none_for_everything_the_legacy_parser_owns(self):
        from argus.judge_api import coerce_judge_score

        for raw in (None, True, False, "text", (True, "x"), [1, "x"], b"x", object(), 1.5, -1, {}):
            assert coerce_judge_score(raw) is None, raw

    def test_stamps_identity_and_severity(self):
        from argus.judge_api import coerce_judge_score

        js = coerce_judge_score(
            {"score": 0.4}, validator_name="n:v", origin_node="n", step_index=5, floor=0.5
        )
        assert (js.validator_name, js.origin_node, js.step_index) == ("n:v", "n", 5)
        assert js.severity == "warning"

    def test_does_not_mutate_a_returned_judge_score(self):
        from argus.judge_api import coerce_judge_score

        mine = JudgeScore(score=0.9)
        out = coerce_judge_score(mine, validator_name="n:v", origin_node="n", step_index=1)
        assert mine.validator_name == "" and mine.origin_node is None
        assert out is not mine and out.validator_name == "n:v"

    def test_judge_score_with_bad_fields_is_not_a_score(self):
        from argus.judge_api import coerce_judge_score

        assert coerce_judge_score(JudgeScore(score=7)) is None
        assert coerce_judge_score(JudgeScore(score=0.5, direction="up")) is None  # type: ignore[arg-type]
        assert coerce_judge_score(JudgeScore(score=0.5, label=3)) is None  # type: ignore[arg-type]

    def test_huge_int_is_out_of_range_not_an_overflow(self):
        from argus.judge_api import coerce_judge_score

        assert coerce_judge_score(10**400) is None

    @pytest.mark.parametrize("bad", [-0.1, 1.1, float("nan"), "0.5", None, True])
    def test_check_floor_rejects_non_fractions(self, bad):
        from argus.judge_api import check_floor

        with pytest.raises(ValueError, match="judge_score_floor"):
            check_floor(bad)

    def test_check_floor_accepts_the_bounds(self):
        from argus.judge_api import check_floor

        assert (check_floor(0), check_floor(1), check_floor(0.25)) == (0.0, 1.0, 0.25)

    def test_grading_reexports_it(self):
        from argus import grading, judge_api

        assert grading.coerce_judge_score is judge_api.coerce_judge_score


@pytest.mark.unit
def test_no_judge_files_are_auto_loaded(tmp_path):
    """Judges are in-process callables only: a stray .argus/judges/*.py never runs."""
    marker = Path("ran.txt")
    judges = Path(".argus") / "judges"
    judges.mkdir(parents=True)
    (judges / "evil.py").write_text(f"open({str(marker.resolve())!r}, 'w').write('x')\n")
    _drive({"a": lambda output: 0.9})
    assert not marker.exists()
