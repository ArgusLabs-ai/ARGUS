"""User-defined custom semantic checks, declared as JSON spec files.

A spec declares one extra LLM question per node ("does the output leak PII?",
"does the summary cite the input?") with a fixed choice table and severity.
Specs live in ``.argus/checks/*.json`` under the project root (``ARGUS_DIR``
is honored via storage.argus_dir, same as runs/checkpoints).

ponytail: the brief said YAML, but PyYAML is not a declared dependency of
this package (it only appears transitively in some environments), and adding
a dependency for a config file is not justified — JSON carries the same
shape with zero new imports.

Reviewer, not cop — the same rule as the coherence judge. A failing custom
check attaches a ``SemanticSignal`` (at the spec's severity) to the step's
inspection once the coherence verdict has been applied, so it shows up in the
report and the judge never sees it; it never sets ``is_silent_failure`` and
never moves a step's status, so it cannot fail a build on its own.

Verdicts travel the same forced tool-call channel as the coherence judge
(:mod:`argus.semantic_checker`): the model must answer by calling a function
whose arguments are the verdict, with the free-text JSON path kept as a
one-shot fallback.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from argus.models import CheckVerdict

logger = logging.getLogger("argus")

_SEVERITIES = frozenset({"critical", "warning", "info"})
_WHEN_VALUES = frozenset({"any", "warning_signal"})

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

# Tool names must match ^[a-zA-Z0-9_-]{1,64} on OpenAI; spec names are user
# input, so the verdict tool uses one fixed name and carries the check name
# in its description instead.
CHECK_VERDICT_TOOL_NAME = "record_check_verdict"


@dataclass(frozen=True)
class CheckSpec:
    """One custom check parsed from a spec file."""

    name: str
    prompt: str  # may contain {{node}}, {{input}}, {{output}}
    # choice key -> {"score": 0.0-1.0, "label": str}; empty means a pass/fail-style
    # spec (the judge answers a boolean instead). It is empty only when the spec
    # file omits "choices" — an explicit "choices": {} is rejected by _validate_spec.
    choices: dict[str, dict[str, Any]]
    severity: str  # "critical" | "warning" | "info"
    when: str  # "any" | "warning_signal"
    path: Path = field(compare=False)  # source file, for diagnostics


def default_checks_dir() -> Path:
    """``<project-root>/.argus/checks`` — does not create the directory."""
    from argus.storage import argus_dir

    return argus_dir() / "checks"


def substitute_placeholders(
    prompt: str,
    *,
    node_name: str,
    input_state: dict[str, Any],
    output_dict: dict[str, Any],
) -> str:
    """Hand-rolled ``{{var}}`` substitution (no template dependency).

    ``{{node}}`` is the node name; ``{{input}}`` / ``{{output}}`` are the
    compacted, JSON-rendered state dicts (bounded by the same limits as the
    coherence judge so a fat trace cannot blow up the prompt). Unknown
    placeholders are left literally in place — silently blanking text the
    author wrote is a worse failure than leaving a visible marker.
    """

    def _render(value: Any) -> str:
        return json.dumps(value, default=str)

    from argus.semantic_checker import _compact_dict

    values = {
        "node": node_name,
        "input": _render(_compact_dict(input_state)),
        "output": _render(_compact_dict(output_dict)),
    }

    def _sub(match: re.Match[str]) -> str:
        return values.get(match.group(1), match.group(0))

    return _PLACEHOLDER_RE.sub(_sub, prompt)


def _validate_spec(data: Any, path: Path) -> CheckSpec | None:
    """One parsed spec file → CheckSpec, or None (with a logged diagnostic)."""
    where = str(path)
    if not isinstance(data, dict):
        logger.warning("argus check spec %s: top level must be an object; skipped", where)
        return None
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        logger.warning("argus check spec %s: 'name' must be a non-empty string; skipped", where)
        return None
    prompt = data.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        logger.warning("argus check spec %s: 'prompt' must be a non-empty string; skipped", where)
        return None
    severity = data.get("severity", "warning")
    if severity not in _SEVERITIES:
        logger.warning(
            "argus check spec %s: 'severity' must be one of %s; skipped",
            where,
            sorted(_SEVERITIES),
        )
        return None
    when = data.get("when", "any")
    if when not in _WHEN_VALUES:
        logger.warning(
            "argus check spec %s: 'when' must be one of %s; skipped",
            where,
            sorted(_WHEN_VALUES),
        )
        return None
    raw_choices = data.get("choices")
    choices: dict[str, dict[str, Any]] = {}
    if raw_choices is not None:
        if not isinstance(raw_choices, dict) or not raw_choices:
            logger.warning(
                "argus check spec %s: 'choices' must be a non-empty object; skipped", where
            )
            return None
        for key, val in raw_choices.items():
            if not isinstance(key, str) or not key.strip():
                logger.warning(
                    "argus check spec %s: choice keys must be non-empty strings; skipped",
                    where,
                )
                return None
            if not isinstance(val, dict):
                logger.warning(
                    "argus check spec %s: choice %r must map to an object; skipped",
                    where,
                    key,
                )
                return None
            score = val.get("score")
            bad_score = (
                not isinstance(score, (int, float))
                or isinstance(score, bool)
                or not 0.0 <= score <= 1.0
            )
            if bad_score:
                logger.warning(
                    "argus check spec %s: choice %r needs a numeric score in 0..1; skipped",
                    where,
                    key,
                )
                return None
            choices[key] = {"score": float(score), "label": str(val.get("label", key))}  # type: ignore[arg-type]
    return CheckSpec(
        name=name.strip(),
        prompt=prompt,
        choices=choices,
        severity=severity,
        when=when,
        path=path,
    )


def load_check_specs(directory: str | Path) -> list[CheckSpec]:
    """Parse every ``*.json`` spec in ``directory``; never raises.

    A missing directory is a no-op (custom checks are opt-in). An invalid
    spec file is logged and skipped — one malformed file must not take down
    the checks that do parse, and must never crash a graded run.
    """
    specs: list[CheckSpec] = []
    seen: set[str] = set()
    try:
        base = Path(directory)
        if not base.is_dir():
            return specs
        files = sorted(base.glob("*.json"))
    except OSError as exc:
        logger.warning("argus check specs: cannot list %s: %s", directory, exc)
        return specs
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            logger.warning("argus check spec %s: unreadable (%s); skipped", path, exc)
            continue
        spec = _validate_spec(data, path)
        if spec is None:
            continue
        if spec.name in seen:
            # Both would emit the same ``check:<name>`` signal id.
            logger.warning(
                "argus check spec %s: duplicate name %r (an earlier spec has it); skipped",
                path,
                spec.name,
            )
            continue
        seen.add(spec.name)
        specs.append(spec)
    return specs


def build_check_tool(spec: CheckSpec) -> dict[str, Any]:
    """The forced-tool-call schema for one spec.

    Graded specs take a ``verdict`` enum naming one of the spec's choices;
    pass/fail specs take a boolean. Both always explain themselves in
    ``reason``. Built through :func:`argus.semantic_checker.build_verdict_tool`
    so the whole judge surface shares one tool builder.
    """
    from argus.semantic_checker import build_verdict_tool

    properties: dict[str, Any] = {
        "reason": {
            "type": "string",
            "maxLength": 200,
            "description": "One sentence citing the output.",
        }
    }
    required = ["reason"]
    if spec.choices:
        properties["verdict"] = {
            "type": "string",
            "enum": sorted(spec.choices),
            "description": "The verdict choice. "
            + "; ".join(
                f"{key} = {val['label']} (score {val['score']})"
                for key, val in sorted(spec.choices.items())
            ),
        }
        required.append("verdict")
    else:
        properties["pass"] = {
            "type": "boolean",
            "description": "true only if the check's condition holds.",
        }
        required.append("pass")
    return build_verdict_tool(
        CHECK_VERDICT_TOOL_NAME,
        f'Record the verdict for the custom check "{spec.name}". Call exactly once.',
        properties,
        required,
    )


def _unevaluated(spec: CheckSpec, reason: str) -> CheckVerdict:
    return CheckVerdict(
        check_name=spec.name,
        verdict="",
        score=None,
        passed=True,  # non-blocking, like every skip in semantic_checker
        reason=reason,
        severity=spec.severity,
        prompt_tokens=0,
        completion_tokens=0,
        evaluated=False,
    )


def _check_system_prompt(spec: CheckSpec) -> str:
    """The system prompt: fixed instruction + the contract for the content fallback.

    The hosted proxy drops the tool, so the prompt must carry the contract.
    """
    # Same fields as build_check_tool's schema.
    if spec.choices:
        answer = '"verdict": ' + "|".join(json.dumps(key) for key in sorted(spec.choices))
    else:
        answer = '"pass": true|false'
    contract = "{" + answer + ', "reason": "<1 sentence>"}'
    return (
        "You run one custom semantic check on a single node in an "
        "AI pipeline. Answer only the question asked, by calling "
        "the record_check_verdict tool exactly once. If no tool is "
        "available to you, respond with only this JSON object:\n" + contract
    )


def run_check_spec(
    spec: CheckSpec,
    node_name: str,
    input_state: dict[str, Any],
    output_dict: dict[str, Any],
    model: str,
    api_key: str | None = None,
) -> CheckVerdict:
    """Run one custom check against a node's I/O; advisory verdict in, verdict out.

    Uses the same forced tool-call channel as the coherence judge, with the
    free-text path as a one-shot fallback. Any failure (no key, provider
    error, unparseable verdict, unknown choice) returns ``evaluated=False``
    so the caller attaches no signal — a broken check is invisible rather
    than wrong.
    """
    from argus.llm_proxy import create_chat_completion, is_available
    from argus.semantic_checker import _coerce_verdict, _parse_verdict_payload

    if not is_available():
        return _unevaluated(spec, "check skipped: not logged in (run: argus login)")

    user_msg = substitute_placeholders(
        spec.prompt,
        node_name=node_name,
        input_state=input_state,
        output_dict=output_dict,
    )
    result = create_chat_completion(
        model=model,
        messages=[
            {
                "role": "system",
                "content": _check_system_prompt(spec),
            },
            {"role": "user", "content": user_msg},
        ],
        max_tokens=200,
        temperature=0.0,
        tools=[build_check_tool(spec)],
        tool_choice={"type": "function", "function": {"name": CHECK_VERDICT_TOOL_NAME}},
        timeout=8.0,
        api_key=api_key,
    )
    if "error" in result:
        return _unevaluated(spec, f"check skipped: {result['error']}")
    parsed = _parse_verdict_payload(result)
    if parsed is None:
        return _unevaluated(spec, "check skipped: unparseable verdict from judge")
    usage = result.get("usage", {})

    def _tokens() -> tuple[int, int]:
        return (
            int(usage.get("prompt_tokens", 0)),
            int(usage.get("completion_tokens", 0)),
        )

    reason = str(parsed.get("reason", ""))[:200]
    if spec.choices:
        verdict = parsed.get("verdict")
        if not isinstance(verdict, str) or verdict not in spec.choices:
            return _unevaluated(spec, "check skipped: verdict not in the spec's choices")
        score = spec.choices[verdict]["score"]
        pt, ct = _tokens()
        return CheckVerdict(
            check_name=spec.name,
            verdict=verdict,
            score=score,
            passed=score >= 0.5,
            reason=reason,
            severity=spec.severity,
            prompt_tokens=pt,
            completion_tokens=ct,
        )
    verdict_bool = _coerce_verdict(parsed.get("pass"))
    if verdict_bool is None:
        return _unevaluated(spec, "check skipped: judge response carried no pass/fail verdict")
    pt, ct = _tokens()
    return CheckVerdict(
        check_name=spec.name,
        verdict="pass" if verdict_bool else "fail",
        score=None,
        passed=verdict_bool,
        reason=reason,
        severity=spec.severity,
        prompt_tokens=pt,
        completion_tokens=ct,
    )
