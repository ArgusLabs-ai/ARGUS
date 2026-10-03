"""The run reviewer (#149): a second, independent detector that settles the rules' guesses.

The rules are two kinds of evidence, and treating them alike was the false-alarm
problem a blind probe exposed (``docs/PIVOT-BRANCH.md``, "The run reviewer"):

* **Strict** — nothing healthy produces it: a tool raised, a 5xx or error body,
  ``{}`` with nodes waiting, a blank final answer, a cut-off generation that was
  used, a JSON-parse fallback, a regression against the healthy baseline. These
  fail CI on their own, with or without a reviewer.
* **Heuristic** — usually a failure, sometimes the design: "I've approved a
  refund" matched against tool names (D14), ``has_more`` on a web search (D4), a
  404 that *is* the answer to "does this exist?", a number or ID the rule could
  not trace (D12 / D13), a sentinel (D6), a repeated call (D15), and the
  contextual layer's "never written → blame the first step" guess.

The reviewer reads the whole ledger once, with a one-line purpose per node,
and lists what looks wrong. A second call verifies each item closed-world: the
step's own evidence must prove it, the verifier must name the exact correct
value, and arithmetic is a formula that code evaluates. What survives is
**verified**. Then, per node:

* heuristic hit + verified → fails CI (two independent checks agree);
* heuristic hit, not verified → kept as a warning, does not fail CI;
* rule *warning* + verified → fails CI (``review_confirmed``) — this is how an
  unrendered ``{customer_name}`` or a report that echoes the question gets caught;
* verified, no rule signal at all → a second verifier on a *different* model
  (``o4-mini``) sees the same item cold. Both confirm → fails CI
  (``review_verified``): paying 10x over the PO or the wrong vendor's account has no
  rule signal at all. Only one confirms → advisory finding, never fails CI.

It never clears a strict fail, and it fails a step with no rule behind it only
when two different models verified the item independently. It needs node
purposes — without them the same model flags ~40% of healthy runs — so it is
off unless purposes are given (``ArgusRecorder(purposes=...)`` or the
``purposes`` block of an ``argus baseline --purposes`` file). If any LLM call
fails, the run is graded by the rules alone, exactly as with no reviewer.

Imports nothing from ``langgraph`` / ``langchain_core``.
"""

from __future__ import annotations

import ast
import json
import logging
import operator
import re
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from typing import Any

from argus.models import NodeEvent, ToolFailure

__all__ = [
    "HEURISTIC_RULES",
    "Review",
    "Reviewer",
    "draft_purposes",
    "is_heuristic",
]

logger = logging.getLogger("argus")

DEFAULT_MODEL = "gpt-4.1"  # measured in ship_eval/coverage and blind_eval
# The second, independent verifier for a finding no rule backs. A reasoning model,
# so its errors are not the first verifier's errors (ship_eval/coverage: gpt-4.1 +
# o4-mini both agreeing kept 86% of semantic faults with 1 false flag in 29).
SECOND_MODEL = "o4-mini"
_CONFIDENCE = 0.7
_MAX_STR = 1500
_MAX_LIST = 30
_MAX_LEDGER_CHARS = 80_000

# Trace-rule failure types whose evidence a healthy run can also produce.
HEURISTIC_RULES = frozenset(
    {
        "unfollowed_pagination",  # D4: top-k of a search is the design
        "sentinel_value",  # D6: a real negative, a real "unknown"
        "ungrounded_number",  # D12: a number derived in code the rule cannot trace
        "near_miss_identifier",  # D13: two real IDs one edit apart
        "unperformed_action",  # D14: verb/tool-name matching
        "stuck_loop",  # D15: polling a job is three identical calls
        "status_overstated",  # D17: "requires_approval" vs a node's own "scheduled"
        "missing_field_guess",  # contextual "never written": the origin is a guess
    }
)

# A 404 is how an existence check says "no" (``GET /users/{email}`` → create).
_HTTP_404 = re.compile(r"\bHTTP 404\b")
# A 409 is a judgement call only when the tool says the thing already exists — an
# idempotent create saying "done before". "Not in a stage that can be advanced" is a
# real conflict and stays strict (ship_eval recruiting `ats_409_on_advance`).
_HTTP_409 = re.compile(r"\bHTTP 409\b")
_ALREADY_EXISTS = re.compile(r"already exist|duplicate|exists already", re.I)

# Warning-level signals that describe shape or cross-step bookkeeping, not this
# step's content. A reviewer finding on the same step does not make them a fail.
_NOT_A_SIGNAL = frozenset({"BA-005", "unused_result", "ordering_anomaly"})


def is_heuristic(
    failure_type: str, evidence: str = "", event: Any = None, field: str = ""
) -> bool:
    """True when a healthy run can also produce this critical signal.

    ``event`` / ``field`` locate the tool response behind an ``error_response``, so a
    409 can be read: without them a 409 is strict.
    """
    if failure_type in HEURISTIC_RULES:
        return True
    if failure_type != "error_response":
        return False
    if _HTTP_404.search(evidence or ""):
        return True
    return bool(_HTTP_409.search(evidence or "")) and _says_already_exists(event, field)


def _says_already_exists(event: Any, field: str) -> bool:
    tool = (field or "").split(".")[0]
    return any(
        _ALREADY_EXISTS.search(json.dumps(t.get("output"), default=str))
        for t in getattr(event, "tool_calls", None) or []
        if not tool or t.get("name") == tool
    )


# ── prompts (ported from ship_eval/coverage: checker v3, verifier, purposes) ──
# These are the measured texts, verbatim. Do not edit them without re-running
# blind_eval/validate_review.py: adding the two characters "{var}, " to one example
# list turned a caught Salesforce page-one truncation into a miss, 2/2 → 0/2.

_CHECKER = (
    "You verify ONE run of an AI agent pipeline, step by step, from its ledger. Each step "
    "shows the node, the state it received, the update it returned, its tool calls (inputs and "
    "outputs) and any raw model output.\n\n"
    "What each node is for (written from a healthy run):\n"
    "<PURPOSES>\n\n"
    "For EVERY step, check its update against the evidence that step had: its input state and "
    'its own tool outputs. Report the step if any of these hold:\n  "contradicted"  a value, '
    "number, entity, date or ID in the update differs from the evidence (quote both);\n  "
    '"unsupported"   it states a specific fact, number or statistic nothing in the evidence '
    'gives (quote it);\n  "unperformed"   it says an action happened or a status holds '
    '("sent", "booked", "refunded", "resolved", "approved", "posted") and no tool result it '
    'had confirms it;\n  "wrong_decision" a decision, score-based route or chosen rule '
    "contradicts what the input or earlier steps found (approving despite a sanctions hit or "
    "an uncovered peril, contacting a lead an earlier step scored below the bar, applying "
    "rules for a different jurisdiction, customer, period or environment than the input "
    'names);\n  "wrong_math"    recompute every total, difference, sign and percentage it '
    'states from the evidence; report a mismatch;\n  "unfinished"    template text left in '
    '(e.g. [Name], [TOPIC], {{var}}), a non-answer ("unable to determine", "N/A") when '
    "the evidence to answer was there, or a text field this step is meant to fill left empty, "
    "blanked or dropped.\n"
    "Do NOT report: legitimate outcomes (a denial, manual review, an empty result that is the "
    "real answer), wording or style, steps whose job is to create a new value (an ID, a "
    "timestamp, a draft), or a step that faithfully passes along an upstream problem — report "
    "the step where it first went wrong.\n"
    'Respond JSON: {"failures": [{"node": str, "kind": str, "claim": "<verbatim from the '
    'update>", "evidence": "<verbatim from the evidence, or empty>", "why": str, "confidence": '
    "0.0-1.0}]} — an empty list if the run is sound."
)

_VERIFIER = (
    "You review ONE reported defect in one step of an AI agent pipeline. Most reports you "
    "receive are false alarms, so be skeptical. You get the step's job (written from a healthy "
    "run), what the step received (input state and its own tool outputs), what it returned, "
    "and the report.\n\n"
    "It is a REAL defect only if ALL of these hold:\n"
    "1. Closed world: the evidence shown here proves it. Do NOT use outside domain knowledge, "
    "industry norms, or your opinion of how the business should work. A value that comes from "
    "the step's own configuration or design (a threshold, a plan figure, a label it is meant "
    "to create) is not a defect.\n"
    "2. Concrete: you can state exactly what this step should have written instead, derived "
    "from the evidence (a value, a decision, or 'remove this text' / 'fill this field').\n"
    "3. Its own doing: the step did not merely pass along a value it received.\n\n"
    "If the report is about arithmetic, do NOT compute anything yourself: give the formula as "
    "a Python expression using only numbers copied from the evidence, and the number the step "
    "stated. Code will evaluate it.\n\n"
    'Respond JSON: {"real_defect": bool, "correct_value": str or null, "arithmetic": '
    '{"expression": str, "stated_value": number} or null, "benign_reading": str}'
)

_PURPOSES = (
    "Here is the ledger of one HEALTHY run of an agent pipeline. For each node, write one "
    "sentence describing its job: what it takes in, what it produces, and anything it is "
    "designed to do that might look odd out of context (e.g. it writes a reply before a later "
    "step performs the action, it creates new IDs, it passes a list through). Respond JSON: "
    '{"node": "purpose", ...}'
)


# ── transport ────────────────────────────────────────────────────────────────


class _ReviewError(RuntimeError):
    pass


def _complete(model: str, system: str, user: str, max_tokens: int) -> dict[str, Any]:
    """One JSON completion through ``llm_proxy`` (BYOK first, then the hosted proxy)."""
    from argus.llm_proxy import create_chat_completion  # noqa: PLC0415

    for attempt in range(3):
        r = create_chat_completion(
            model=model,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens=max_tokens,
            temperature=0.0,
            response_format={"type": "json_object"},
            timeout=90.0,
        )
        if "error" not in r:
            try:
                data = json.loads(r["choices"][0]["message"]["content"] or "{}")
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise _ReviewError(f"unreadable reply: {exc}") from exc
            if not isinstance(data, dict):
                raise _ReviewError("reply is not a JSON object")
            return data
        err = str(r["error"])
        wait = re.search(r"try again in ([\d.]+)(ms|s)", err)
        if attempt == 2 or not (wait or "rate" in err.lower()):
            raise _ReviewError(err[:300])
        secs = float(wait.group(1)) / (1000 if wait.group(2) == "ms" else 1) if wait else 5.0
        time.sleep(min(secs, 30.0) + 1.0)
    raise _ReviewError("unreachable")


# ── what the model reads ─────────────────────────────────────────────────────


def _clip(v: Any) -> Any:
    if isinstance(v, str):
        return v if len(v) <= _MAX_STR else f"{v[:_MAX_STR]}…[clipped {len(v) - _MAX_STR} chars]"
    if isinstance(v, dict):
        return {str(k): _clip(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        items = [_clip(x) for x in v[:_MAX_LIST]]
        if len(v) > _MAX_LIST:
            items.append(f"…[{len(v) - _MAX_LIST} more items]")
        return items
    return v


def _dump(v: Any) -> str:
    return json.dumps(_clip(v), default=str)


def _model_text(events: Iterable[NodeEvent]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for e in events:
        for c in getattr(e.llm_usage, "calls", None) or []:
            if c.output_text:
                out.setdefault(e.node_name, []).append(c.output_text)
    return out


def _ledger_text(rows: list[Any], text: dict[str, list[str]]) -> str:
    lines: list[str] = []
    for i, row in enumerate(rows):
        lines.append(f"## step {i} — node `{row.node}`")
        lines.append("input: " + _dump(row.input_state))
        lines.append("update: " + _dump(row.update))
        lines.extend("tool: " + _dump(t) for t in row.tools or [])
        lines.extend("model output: " + _dump(c) for c in text.get(row.node, [])[:2])
    body = "\n".join(lines)
    if len(body) > _MAX_LEDGER_CHARS:
        body = body[:_MAX_LEDGER_CHARS] + "\n…[ledger clipped]"
    return body


# ── arithmetic is settled by code, not by the model ─────────────────────────

_OPS: dict[type, Callable[..., float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(expr: str) -> float:
    def ev(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:  # no `**`: 9**9**9 hangs
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        raise ValueError("not arithmetic")

    return ev(ast.parse(expr.replace(",", ""), mode="eval"))


def _decide(v: dict[str, Any]) -> tuple[bool, str]:
    """The verifier's verdict; an arithmetic claim is decided by evaluating its formula."""
    a = v.get("arithmetic")
    if isinstance(a, dict) and a.get("expression"):
        try:
            got = _safe_eval(str(a["expression"]))
            stated = float(str(a.get("stated_value")).replace(",", ""))
            wrong = abs(got - stated) > max(0.01, 0.005 * abs(got))
            return wrong, f"{a['expression']} = {got:g}; the step stated {stated:g}"
        except (ValueError, TypeError, SyntaxError, ZeroDivisionError, OverflowError):
            pass  # an unusable formula falls back to the verdict
    correction = str(v.get("correct_value") or "").strip()
    return bool(v.get("real_defect")) and bool(correction), correction


# ── the review ───────────────────────────────────────────────────────────────


@dataclass
class Review:
    """What the reviewer verified, by node. ``ok`` is False when any call failed."""

    ok: bool
    verified: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    reported: int = 0
    error: str | None = None

    def confirms(self, node: str) -> bool:
        return self.ok and node in self.verified


@dataclass(frozen=True)
class Reviewer:
    """Checker + verifier over one run's ledger. Needs a purpose per node."""

    purposes: dict[str, str]
    model: str = DEFAULT_MODEL
    second_model: str = SECOND_MODEL
    max_findings: int = 8

    def __call__(self, rows: list[Any], events: list[NodeEvent]) -> Review:
        try:
            return self._review(rows, events)
        except Exception as exc:  # noqa: BLE001 — a reviewer must never take a run down
            logger.warning("argus: run reviewer skipped, grading by rules only (%s)", exc)
            return Review(ok=False, error=str(exc)[:300])

    def _review(self, rows: list[Any], events: list[NodeEvent]) -> Review:
        text = _model_text(events)
        hint = "\n".join(f"- {n}: {p}" for n, p in self.purposes.items())
        reply = _complete(
            self.model, _CHECKER.replace("<PURPOSES>", hint), _ledger_text(rows, text), 1500
        )
        nodes = {r.node for r in rows}
        reported = [
            f
            for f in reply.get("failures") or []
            if isinstance(f, dict)
            and f.get("node") in nodes
            and _num(f.get("confidence")) >= _CONFIDENCE
        ]
        reported.sort(key=lambda f: -_num(f.get("confidence")))
        reported = reported[: self.max_findings]
        last_row = {r.node: r for r in rows}

        def verify(f: dict[str, Any]) -> tuple[dict[str, Any], bool, str, bool]:
            row = last_row[f["node"]]
            ctx = {
                "step": f["node"],
                "job": self.purposes.get(f["node"], "(no description)"),
                "received": _clip(row.input_state),
                "tool_calls": _clip(row.tools),
                "model_output": _clip(text.get(f["node"], [])[:2]),
                "returned": _clip(row.update),
                "report": {k: f.get(k) for k in ("kind", "claim", "evidence", "why")},
            }
            user = json.dumps(ctx, default=str)
            real, how = _decide(_complete(self.model, _VERIFIER, user, 700))
            return f, real, how, real and self._second_opinion(user)

        verified: dict[str, list[dict[str, Any]]] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            for f, real, how, both in pool.map(verify, reported):
                if real:
                    verified.setdefault(f["node"], []).append(
                        {
                            "kind": str(f.get("kind") or ""),
                            "claim": str(f.get("claim") or "")[:300],
                            "why": str(f.get("why") or "")[:500],
                            "correction": how[:300],
                            "two_models": both,
                        }
                    )
        return Review(ok=True, verified=verified, reported=len(reported))

    def _second_opinion(self, user: str) -> bool:
        """Does the second model verify it too? An error is a no, never a failed
        review: this path can only add a fail, so losing it must not cost the rules'
        verdict."""
        try:
            return _decide(_complete(self.second_model, _VERIFIER, user, 4000))[0]
        except Exception as exc:  # noqa: BLE001
            logger.info("argus: second verifier unavailable (%s); finding stays advisory", exc)
            return False


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def draft_purposes(records: Iterable[Any], *, model: str = DEFAULT_MODEL) -> dict[str, str]:
    """One sentence per node from healthy runs — a draft the team edits, not ground truth."""
    from argus.ledger import build_ledger  # noqa: PLC0415

    out: dict[str, str] = {}
    for rec in records:
        rows = build_ledger(rec.steps, rec.initial_state, rec.reducer_kinds, rec.state_keys)
        reply = _complete(model, _PURPOSES, _ledger_text(rows, _model_text(rec.steps)), 800)
        for node, purpose in reply.items():
            if isinstance(purpose, str) and node not in out:
                out[str(node)] = purpose.strip()
    return out


# ── applying a review (grading.finish calls these; all deterministic) ────────


def settle_step_signals(events: list[NodeEvent], review: Review) -> None:
    """Demote a step whose only critical evidence is heuristic and unconfirmed.

    Runs before the whole-trace rules, so a demoted step is not treated as an
    origin that stops their scan. Only a step whose every critical signal is
    accounted for here is touched; anything else stays exactly as the
    inspector left it.
    """
    if not review.ok:
        return
    for e in events:
        insp = e.inspection
        if e.status != "fail" or insp is None or review.confirms(e.node_name):
            continue
        crit = [t for t in insp.tool_failures if t.severity == "critical"]
        if (
            not crit
            or not all(is_heuristic(t.failure_type, t.evidence, e, t.field_name) for t in crit)
            or insp.is_silent_failure
            or insp.missing_fields
            or insp.empty_fields
            or insp.type_mismatches
            or any(s.severity == "critical" for s in insp.semantic_signals)
            or any(a.severity == "critical" for a in e.anomaly_signals)
            or any(v.is_blocking for v in e.validator_results)
        ):
            continue
        for t in crit:
            t.severity = "warning"
            t.evidence = f"{t.evidence} (heuristic; the run reviewer did not confirm it)"
        insp.has_tool_failure = False
        insp.severity = "warning"
        e.status = "pass"


def confirm_warnings(events: list[NodeEvent], review: Review) -> None:
    """Fail a step a rule warned about and the reviewer independently verified.

    Also files every verified finding on its step, so ``argus show`` / the
    findings list say what the reviewer saw: ``confirms`` (the step already
    fails), ``promoted`` (this made it fail) or ``advisory`` (no rule saw
    anything; never gating).
    """
    if not review.ok:
        return
    last = {e.node_name: e for e in events if e.status not in ("skipped", "retried")}
    for node, items in review.verified.items():
        e = last.get(node)
        if e is None:
            continue
        if e.status in ("fail", "crashed", "semantic_fail", "degraded_input"):
            role = "confirms"
        else:
            warning = _warning_signal(e)
            both = next((i for i in items if i.get("two_models")), None)
            if warning is not None:
                role = "promoted"
                _promote(e, warning, items[0])
            elif both is not None:
                role = "two_models"
                _promote(e, None, both)
            else:
                role = "advisory"
        e.review = [{**item, "role": role} for item in items]


def _warning_signal(e: NodeEvent) -> str | None:
    """Name of a warning a rule raised on this step's content, if any."""
    insp = e.inspection
    if insp is not None:
        for t in insp.tool_failures:
            if t.severity == "warning" and t.failure_type not in _NOT_A_SIGNAL:
                return t.failure_type
        for s in insp.semantic_signals:
            if s.severity == "warning":
                return s.sig_id
    for a in e.anomaly_signals:
        if a.severity == "warning" and a.anomaly_id not in _NOT_A_SIGNAL:
            return a.anomaly_id
    return None


def _promote(e: NodeEvent, signal: str | None, item: dict[str, Any]) -> None:
    """Fail a step: a rule warning plus a verified item, or two models' verified item."""
    from argus.models import InspectionResult  # noqa: PLC0415

    why = item.get("why") or item.get("claim") or "see the review"
    if signal is None:
        failure_type, field_name = "review_verified", "review"
        evidence = f"two models verified it independently (no rule saw it): {why}"
    else:
        failure_type, field_name = "review_confirmed", signal
        evidence = f"rule {signal} and the run reviewer agree: {why}"
    if e.inspection is None:
        e.inspection = InspectionResult(
            is_silent_failure=True,
            missing_fields=[],
            empty_fields=[],
            type_mismatches=[],
            severity="critical",
            message=evidence,
        )
    insp = e.inspection
    insp.tool_failures.append(
        ToolFailure(
            failure_type=failure_type,
            field_name=field_name,
            severity="critical",
            evidence=evidence,
        )
    )
    insp.has_tool_failure = True
    insp.is_silent_failure = True
    insp.severity = "critical"
    insp.message = (
        evidence if insp.message == "All checks passed" else f"{insp.message}; {evidence}"
    )
    e.status = "fail"


def hit_stands(review: Review, hit: Any) -> bool:
    """A strict hit always stands; a heuristic one only where the reviewer agrees."""
    return not is_heuristic(
        hit.failure_type, hit.evidence, hit.event, hit.field_name
    ) or review.confirms(hit.event.node_name)


def split_hits(hits: list[Any], review: Review | None) -> tuple[list[Any], list[Any]]:
    """(stand, demoted): a heuristic hit stands only when the reviewer confirms its node."""
    if review is None or not review.ok:
        return list(hits), []
    stand, demoted = [], []
    for h in hits:
        if not hit_stands(review, h):
            demoted.append(
                replace(
                    h, evidence=f"{h.evidence} (heuristic; the run reviewer did not confirm it)"
                )
            )
        else:
            stand.append(h)
    return stand, demoted
