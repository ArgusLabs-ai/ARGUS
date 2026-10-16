"""What a judge verdict may claim about being reproducible.

A verdict is *deterministic* only when all three hold:

1. the judge asked for it: temperature 0.0 and a seed (``is_pinned``);
2. the route can carry it: the provider takes a seed for that model
   (``providers.honors_seed``: OpenAI and Gemini do, Anthropic has none, the
   hosted proxy drops it, OpenAI reasoning models take no temperature);
3. the live response said so: the adapter marks a reply ``seed_honored`` only
   when the seed really went on the wire. A reply without the mark (a custom
   adapter, a proxy) is not deterministic, whatever the config says.

Seeds are best effort at every provider: this module does not promise bit-for-bit
equal replies, it refuses to *claim* determinism the request could not have had.
OpenAI also names the backend build (``system_fingerprint``); it is recorded so a
reader can tell a changed backend from a changed verdict.

The two LLM judges that rule on a run carry the stamp: the per-step coherence judge
(``SemanticCheckResult``) and the run reviewer (each verified item in ``NodeEvent.review``).
Every verdict records the sampling it was asked under and whether it was deterministic;
``argus show`` / ``check`` / ``diff`` report it.

Two things hang off that claim, and nothing else:

* the verdict cache (``gate``): only a request that can make the claim is read from or
  written to the cache, so a verdict that could roll the dice is never served as if it were
  a ruling, and never poisons a later run;
* the audit trail (``stamp``, ``describe``, ``comparability_warning``): every verdict records
  the sampling it was asked under and whether it was deterministic.

A verdict served from the cache is deterministic by construction (only a deterministic one
is ever stored) and carries no ``system_fingerprint``: the cache keeps the verdict, not the
reply.

Reviewer-not-gate: ``deterministic`` is metadata. ``evaluate_run`` and every exit code
never read it.

Defaults: temperature 0.0, no seed (so no seed is ever sent unless the user set one).
Set ``ArgusConfig(judge_seed=N)`` or ``ARGUS_JUDGE_SEED=N`` to opt in (the cache stays off
until they do).

Limits: temperature 0.0-2.0 (Anthropic stops at 1.0, OpenAI and Gemini at 2.0);
a seed is a 64-bit integer (Gemini documents 32 bits). A value a provider
rejects makes that judge call fail rather than being clamped here (the coherence judge
records a skipped check carrying the provider's error; the reviewer logs it and the run is
graded by the rules alone).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from argus import providers

logger = logging.getLogger("argus")

SEED_ENV = "ARGUS_JUDGE_SEED"
MAX_TEMPERATURE = 2.0
_INT64 = 2**63


def validate_sampling(temperature: Any, seed: Any) -> list[str]:
    """Why a judge sampling config is invalid: one message per problem, [] when fine."""
    errors: list[str] = []
    if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
        errors.append(f"judge_temperature must be a number, got {temperature!r}")
    elif not 0.0 <= temperature <= MAX_TEMPERATURE:  # also rejects nan and inf
        errors.append(
            f"judge_temperature must be between 0.0 and {MAX_TEMPERATURE}, got {temperature!r}"
        )
    if seed is not None and (
        isinstance(seed, bool) or not isinstance(seed, int) or not -_INT64 <= seed < _INT64
    ):
        errors.append(f"judge_seed must be a 64-bit integer or None, got {seed!r}")
    return errors


def env_seed() -> int | None:
    """``$ARGUS_JUDGE_SEED`` as a seed; unset or unusable is None (a bad one is warned about)."""
    raw = os.environ.get(SEED_ENV, "").strip()
    if not raw:
        return None
    try:
        seed = int(raw)
    except ValueError:
        seed = None
    if seed is None or validate_sampling(0.0, seed):
        logger.warning(
            "argus: ignoring %s=%r (not a 64-bit integer); judges run unseeded", SEED_ENV, raw
        )
        return None
    return seed


def resolve_sampling(config: Any = None) -> tuple[float, int | None]:
    """The (temperature, seed) the judges run under; a value is used only where it was set.

    An ``ArgusConfig`` field that is not None beats ``$ARGUS_JUDGE_SEED``. The config is
    not modified. With nothing set the answer is (0.0, None): temperature 0 and no seed.
    """
    temperature = getattr(config, "judge_temperature", None)
    if temperature is None:
        temperature = 0.0
    seed = getattr(config, "judge_seed", None)
    if seed is None:
        seed = env_seed()
    return temperature, seed


def sampling_kwargs(temperature: float, seed: int | None) -> dict[str, Any]:
    """``temperature=`` / ``seed=`` for a judge call, only where they differ from the default.

    A judge is called with nothing extra unless the user set something, so a fake or an
    older adapter with the original signature keeps working.
    """
    kwargs: dict[str, Any] = {} if temperature == 0.0 else {"temperature": temperature}
    if seed is not None:
        kwargs["seed"] = seed
    return kwargs


def is_pinned(temperature: float, seed: int | None) -> bool:
    """Does the config ask for a reproducible reply: temperature 0.0 and a seed?"""
    return temperature == 0.0 and seed is not None


def sampling_params(temperature: float, seed: int | None) -> dict[str, Any]:
    """The sampling part of a judge request: a seed rides along only when one was set.

    The temperature is always a float, so 0 and 0.0 are one value.
    """
    params: dict[str, Any] = {"temperature": float(temperature)}
    if seed is not None:
        params["seed"] = seed
    return params


def gate(cache: Any, route: str, temperature: float, seed: int | None) -> Any:
    """``cache`` when this request can make a reproducibility claim, else None (no read, no write).

    ``route`` is ``llm_proxy.judge_route``'s ``provider:model``.
    """
    provider, _, model = route.partition(":")
    if is_pinned(temperature, seed) and providers.honors_seed(provider, model):
        return cache
    return None


def stamp(
    temperature: float, seed: int | None, result: dict[str, Any], *, cached: bool = False
) -> dict[str, Any]:
    """The audit fields of a verdict: the sampling it was asked under, and whether it held.

    ``result`` is the live reply (the ``seed_honored`` mark is the adapter's); a verdict
    served from the cache (``cached=True``) is deterministic by construction.
    """
    fingerprint = result.get("system_fingerprint")
    return {
        "cached": cached,
        "deterministic": cached
        or (is_pinned(temperature, seed) and result.get("seed_honored") is True),
        "temperature": float(temperature),
        "seed": seed,
        "system_fingerprint": fingerprint if isinstance(fingerprint, str) else "",
    }


def combine(stamps: list[dict[str, Any]]) -> dict[str, Any]:
    """One stamp for a verdict that several calls produced (the reviewer's checker + verifiers).

    Deterministic only if every call was; the sampling is the first call's (they share one
    config); the fingerprints are every distinct one, sorted and comma-joined.
    """
    if not stamps:
        return stamp_from_record({})
    prints = sorted({s["system_fingerprint"] for s in stamps if s.get("system_fingerprint")})
    return {
        "cached": all(s.get("cached") is True for s in stamps),
        "deterministic": all(s.get("deterministic") is True for s in stamps),
        "temperature": stamps[0].get("temperature"),
        "seed": stamps[0].get("seed"),
        "system_fingerprint": ",".join(prints),
    }


def stamp_from_record(data: dict[str, Any]) -> dict[str, Any]:
    """``stamp``'s fields as read back from a hand-editable run file (junk becomes the default).

    A temperature or seed ``validate_sampling`` would refuse is dropped, and a stored
    ``deterministic`` is believed only when the sampling that survives is pinned.
    """
    temperature: Any = data.get("temperature")
    seed: Any = data.get("seed")
    fingerprint = data.get("system_fingerprint")
    if validate_sampling(temperature, None):
        temperature = None
    if validate_sampling(0.0, seed):
        seed = None
    return {
        "cached": data.get("cached") is True,
        "deterministic": data.get("deterministic") is True and is_pinned(temperature, seed),
        "temperature": temperature,
        "seed": seed,
        "system_fingerprint": fingerprint if isinstance(fingerprint, str) else "",
    }


# ── What a run reports ───────────────────────────────────────────────────────


def _verdicts(record: Any) -> list[dict[str, Any]]:
    """The stamps of the judged verdicts a run holds: evaluated coherence checks and the
    reviewer's verified items."""
    found = [
        stamp_from_record(vars(e.semantic_check))
        for e in record.steps
        if e.semantic_check is not None and e.semantic_check.evaluated
    ]
    for e in record.steps:
        reviewed = e.review if isinstance(e.review, list) else []
        found.extend(stamp_from_record(item) for item in reviewed if isinstance(item, dict))
    return found


def _config_label(temperature: float, seed: int | None) -> str:
    return f"temp={float(temperature)}, " + ("no seed" if seed is None else f"seed={seed}")


def _label(verdict: dict[str, Any]) -> str:
    if verdict["temperature"] is None:
        return "sampling not recorded"
    label = _config_label(verdict["temperature"], verdict["seed"])
    if not verdict["deterministic"] and is_pinned(verdict["temperature"], verdict["seed"]):
        label += ", seed not honored by the provider"
    return label


def status(record: Any) -> str | None:
    """``deterministic``, ``non-deterministic`` or ``mixed``; None when nothing was judged."""
    verdicts = _verdicts(record)
    if not verdicts:
        return None
    flags = {v["deterministic"] for v in verdicts}
    return (
        "mixed" if len(flags) == 2 else "deterministic" if flags == {True} else "non-deterministic"
    )


def describe(record: Any) -> str | None:
    """One line on how reproducible this run's judge verdicts are, or None when it has none."""
    verdicts = _verdicts(record)
    kind = status(record)
    if kind is None:
        return None
    bad = [v for v in verdicts if not v["deterministic"]]
    labels = "; ".join(
        dict.fromkeys(_label(v) for v in (verdicts if kind == "deterministic" else bad))
    )
    text = f"{kind} ({labels})"
    if kind == "mixed":
        text = f"mixed ({len(bad)} of {len(verdicts)} verdicts non-deterministic: {labels})"
    prints = sorted({p for v in verdicts for p in v["system_fingerprint"].split(",") if p})
    if prints:
        text += "  system_fingerprint " + ", ".join(prints)
    return text


def comparability_warning(before: Any, after: Any) -> str | None:
    """A warning when two runs' verdicts came from different (temperature, seed) pairs."""

    def configs(record: Any) -> list[tuple[float, int | None]]:
        pairs = {
            (float(v["temperature"]), v["seed"])
            for v in _verdicts(record)
            if v["temperature"] is not None
        }
        return sorted(pairs, key=lambda c: (c[0], c[1] is None, c[1] or 0))

    was, now = configs(before), configs(after)
    if not was or not now or was == now:
        return None
    show = lambda pairs: "; ".join(_config_label(t, s) for t, s in pairs)  # noqa: E731
    return f"verdicts not comparable: judge configs differ (before {show(was)}; after {show(now)})"
