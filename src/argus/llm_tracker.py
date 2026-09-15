"""Automatic LLM token/cost tracking for ARGUS.

Uses two detection strategies:
1. LangChain callback handler (if langchain-core is installed) — captures
   on_llm_end events with token usage automatically.
2. Output dict scanner — looks for common token usage keys in node output
   as a fallback for non-LangChain LLMs.
"""

from __future__ import annotations

import contextvars
import threading
from typing import Any

from argus.models import LLMCallInfo, LLMUsage

try:
    from cloud.pricing import calculate_cost
except ImportError:

    def calculate_cost(
        model_name: str, prompt_tokens: int, completion_tokens: int
    ) -> float | None:
        return None

# ── Optional LangChain imports ──────────────────────────────────────────────

try:
    from langchain_core.callbacks import BaseCallbackHandler  # type: ignore[import]

    _HAS_LANGCHAIN = True
except ImportError:
    _HAS_LANGCHAIN = False

# Contextvar for injecting the handler into LangChain's execution context
_active_handler: contextvars.ContextVar[Any] = contextvars.ContextVar(
    "argus_llm_handler",
    default=None,
)


# ── LangChain callback handler ─────────────────────────────────────────────

if _HAS_LANGCHAIN:

    class ArgusLLMHandler(BaseCallbackHandler):  # type: ignore[misc]
        """Captures LLM call metadata during node execution."""

        def __init__(self) -> None:
            super().__init__()
            self._calls: list[LLMCallInfo] = []
            self._lock = threading.Lock()

        def on_llm_end(self, response: Any, **kwargs: Any) -> None:
            """Extract token usage from LLM response."""
            llm_output = getattr(response, "llm_output", None) or {}
            token_usage = llm_output.get("token_usage") or {}

            # Some providers put usage in response.usage_metadata
            if not token_usage:
                usage_meta = getattr(response, "usage_metadata", None)
                if isinstance(usage_meta, dict):
                    token_usage = usage_meta

            # Try generations[0].generation_info for providers that put it there
            if not token_usage:
                gens = getattr(response, "generations", None) or []
                if gens and gens[0]:
                    gen = gens[0][0] if isinstance(gens[0], list) else gens[0]
                    gen_info = getattr(gen, "generation_info", None) or {}
                    token_usage = gen_info.get("usage", {})

            prompt_tokens = _int_or_zero(
                token_usage.get("prompt_tokens") or token_usage.get("input_tokens")
            )
            completion_tokens = _int_or_zero(
                token_usage.get("completion_tokens") or token_usage.get("output_tokens")
            )
            total_tokens = _int_or_zero(token_usage.get("total_tokens")) or (
                prompt_tokens + completion_tokens
            )

            model_name = (
                llm_output.get("model_name")
                or llm_output.get("model")
                or token_usage.get("model")
                or kwargs.get("name", "")
                or "unknown"
            )

            if total_tokens > 0:
                cost = calculate_cost(str(model_name), prompt_tokens, completion_tokens)
                with self._lock:
                    self._calls.append(
                        LLMCallInfo(
                            model_name=str(model_name),
                            prompt_tokens=prompt_tokens,
                            completion_tokens=completion_tokens,
                            total_tokens=total_tokens,
                            cost_usd=cost,
                        )
                    )

        @property
        def calls(self) -> list[LLMCallInfo]:
            with self._lock:
                return list(self._calls)

else:
    # Stub when langchain-core is not installed
    class ArgusLLMHandler:  # type: ignore[no-redef]
        def __init__(self) -> None:
            self._calls: list[LLMCallInfo] = []

        @property
        def calls(self) -> list[LLMCallInfo]:
            return []


# ── Handler lifecycle ───────────────────────────────────────────────────────


def create_tracker() -> ArgusLLMHandler | None:
    """Create a tracker if LangChain is available."""
    if not _HAS_LANGCHAIN:
        return None
    return ArgusLLMHandler()


def install_handler(handler: ArgusLLMHandler) -> contextvars.Token[Any] | None:
    """Install the handler into LangChain's callback context."""
    if not _HAS_LANGCHAIN or handler is None:
        return None
    try:
        # Set as contextvar so LangChain picks it up
        token = _active_handler.set(handler)

        # Also try to add to the global callback manager if available
        try:
            import langchain_core.callbacks  # type: ignore[import]
            from langchain_core.globals import set_llm_cache  # noqa: F401 — test import

            if hasattr(langchain_core.callbacks, "_configure"):
                pass  # internal, don't touch
        except Exception:
            pass

        # Use the recommended approach: set via config
        try:
            from langchain_core.runnables.config import (  # type: ignore[import]
                var_child_runnable_config,
            )

            current = var_child_runnable_config.get({})
            callbacks = list(current.get("callbacks") or [])
            callbacks.append(handler)
            var_child_runnable_config.set({**current, "callbacks": callbacks})
        except Exception:
            pass

        return token
    except Exception:
        return None


def remove_handler(handler: ArgusLLMHandler, token: Any | None) -> None:
    """Remove the handler from the callback context."""
    if token is not None:
        try:
            _active_handler.reset(token)
        except Exception:
            pass

    # Clean up runnable config
    if _HAS_LANGCHAIN:
        try:
            from langchain_core.runnables.config import (  # type: ignore[import]
                var_child_runnable_config,
            )

            current = var_child_runnable_config.get({})
            callbacks = [cb for cb in (current.get("callbacks") or []) if cb is not handler]
            var_child_runnable_config.set({**current, "callbacks": callbacks})
        except Exception:
            pass


# ── Output dict scanner (fallback) ─────────────────────────────────────────

# Common keys where LLM providers put token usage in output dicts
_USAGE_KEYS = ("usage_metadata", "token_usage", "usage", "llm_usage", "response_metadata")


def scan_output_for_tokens(output_dict: dict[str, Any] | None) -> list[LLMCallInfo]:
    """Scan a node output dict for token usage data."""
    if not output_dict:
        return []

    results: list[LLMCallInfo] = []

    for key in _USAGE_KEYS:
        usage = output_dict.get(key)
        if not isinstance(usage, dict):
            # Check nested: output_dict.get("messages", [{}])[-1].get(key)
            continue

        prompt_tokens = _int_or_zero(
            usage.get("prompt_tokens")
            or usage.get("input_tokens")
            or usage.get("prompt_token_count")
        )
        completion_tokens = _int_or_zero(
            usage.get("completion_tokens")
            or usage.get("output_tokens")
            or usage.get("candidates_token_count")
        )
        total_tokens = _int_or_zero(
            usage.get("total_tokens") or usage.get("total_token_count")
        ) or (prompt_tokens + completion_tokens)

        if total_tokens == 0:
            continue

        model_name = str(
            usage.get("model_name")
            or usage.get("model")
            or output_dict.get("model")
            or output_dict.get("model_name")
            or "unknown"
        )

        cost = calculate_cost(model_name, prompt_tokens, completion_tokens)
        results.append(
            LLMCallInfo(
                model_name=model_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost,
            )
        )
        break  # only take the first match to avoid double-counting

    # Also scan nested message objects for usage metadata
    messages = output_dict.get("messages")
    if isinstance(messages, list) and not results:
        for msg in reversed(messages):  # check latest message first
            if not isinstance(msg, dict):
                # Could be a LangChain message object
                resp_meta = getattr(msg, "response_metadata", None)
                usage_meta = getattr(msg, "usage_metadata", None)
                meta = usage_meta or resp_meta
                if isinstance(meta, dict):
                    prompt_tokens = _int_or_zero(
                        meta.get("input_tokens") or meta.get("prompt_tokens")
                    )
                    completion_tokens = _int_or_zero(
                        meta.get("output_tokens") or meta.get("completion_tokens")
                    )
                    total_tokens = _int_or_zero(meta.get("total_tokens")) or (
                        prompt_tokens + completion_tokens
                    )

                    if total_tokens > 0:
                        model_name = str(meta.get("model_name") or meta.get("model") or "unknown")
                        cost = calculate_cost(model_name, prompt_tokens, completion_tokens)
                        results.append(
                            LLMCallInfo(
                                model_name=model_name,
                                prompt_tokens=prompt_tokens,
                                completion_tokens=completion_tokens,
                                total_tokens=total_tokens,
                                cost_usd=cost,
                            )
                        )
                        break
            elif isinstance(msg, dict):
                for ukey in _USAGE_KEYS:
                    umeta = msg.get(ukey)
                    if isinstance(umeta, dict):
                        prompt_tokens = _int_or_zero(
                            umeta.get("input_tokens") or umeta.get("prompt_tokens")
                        )
                        completion_tokens = _int_or_zero(
                            umeta.get("output_tokens") or umeta.get("completion_tokens")
                        )
                        total_tokens = _int_or_zero(umeta.get("total_tokens")) or (
                            prompt_tokens + completion_tokens
                        )
                        if total_tokens > 0:
                            model_name = str(
                                umeta.get("model_name") or umeta.get("model") or "unknown"
                            )
                            cost = calculate_cost(model_name, prompt_tokens, completion_tokens)
                            results.append(
                                LLMCallInfo(
                                    model_name=model_name,
                                    prompt_tokens=prompt_tokens,
                                    completion_tokens=completion_tokens,
                                    total_tokens=total_tokens,
                                    cost_usd=cost,
                                )
                            )
                            break
                if results:
                    break

    return results


# ── LLMResult dict (recorder callback and LangSmith export) ─────────────────


def call_from_llm_outputs(outputs: dict[str, Any], name: str = "") -> LLMCallInfo | None:
    """One call's model, usage and finish reason from an ``LLMResult`` dict.

    The shape LangChain's tracer exports for an ``llm`` run: ``generations`` with
    each chat message serialized as ``{"lc": 1, "kwargs": {...}}``. The recorder
    rebuilds that same shape, so both paths read one parser. ``None`` when the
    run carries neither tokens nor a finish reason.
    """
    llm_output = outputs.get("llm_output") or {}
    batches = outputs.get("generations") or []
    first = batches[0][0] if batches and batches[0] else {}
    message = first.get("message") or {}
    message = message.get("kwargs", message)
    meta = message.get("response_metadata") or {}
    usage = (
        message.get("usage_metadata")
        or llm_output.get("token_usage")
        or llm_output.get("usage")
        or {}
    )
    prompt_tokens = _int_or_zero(usage.get("input_tokens") or usage.get("prompt_tokens"))
    completion_tokens = _int_or_zero(usage.get("output_tokens") or usage.get("completion_tokens"))
    total_tokens = _int_or_zero(usage.get("total_tokens")) or (prompt_tokens + completion_tokens)
    finish_reason = (
        (first.get("generation_info") or {}).get("finish_reason")
        or meta.get("finish_reason")
        or meta.get("stop_reason")
    )
    if total_tokens == 0 and not finish_reason:
        return None
    model_name = str(
        llm_output.get("model_name")
        or meta.get("model_name")
        or meta.get("model")
        or name
        or "unknown"
    )
    return LLMCallInfo(
        model_name=model_name,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        cost_usd=calculate_cost(model_name, prompt_tokens, completion_tokens),
        finish_reason=str(finish_reason) if finish_reason else None,
    )


# ── Combine both strategies ─────────────────────────────────────────────────


def extract_usage(
    handler: ArgusLLMHandler | None,
    output_snap: dict[str, Any] | None,
) -> LLMUsage | None:
    """Combine callback handler captures + output dict scan into LLMUsage."""
    all_calls: list[LLMCallInfo] = []

    # Strategy 1: LangChain callback captures
    if handler is not None:
        all_calls.extend(handler.calls)

    # Strategy 2: Output dict scan (only if callback didn't capture anything)
    if not all_calls:
        all_calls.extend(scan_output_for_tokens(output_snap))

    return usage_from_calls(all_calls)


def usage_from_calls(all_calls: list[LLMCallInfo]) -> LLMUsage | None:
    """Sum calls into one ``LLMUsage``; ``None`` when there were none."""
    if not all_calls:
        return None

    total_prompt = sum(c.prompt_tokens for c in all_calls)
    total_completion = sum(c.completion_tokens for c in all_calls)
    total_tokens = sum(c.total_tokens for c in all_calls)

    costs = [c.cost_usd for c in all_calls if c.cost_usd is not None]
    total_cost = round(sum(costs), 6) if costs else None

    return LLMUsage(
        calls=all_calls,
        total_prompt_tokens=total_prompt,
        total_completion_tokens=total_completion,
        total_tokens=total_tokens,
        total_cost_usd=total_cost,
    )


# ── Helpers ─────────────────────────────────────────────────────────────────


def _int_or_zero(val: Any) -> int:
    if val is None:
        return 0
    try:
        return int(val)
    except (TypeError, ValueError):
        return 0
