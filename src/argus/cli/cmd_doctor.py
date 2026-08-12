"""argus doctor — integration diagnostic checks.

Validates that ARGUS can function correctly in the current environment:
LangGraph version, Python version, storage health, and replay readiness.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

from rich.console import Console
from rich.text import Text

from argus.cli import print_footer

console = Console()


def _check_python_version() -> tuple[bool, str]:
    v = sys.version_info
    version_str = f"{v.major}.{v.minor}.{v.micro}"
    if v >= (3, 9):
        return True, f"Python {version_str}"
    return False, f"Python {version_str} — ARGUS requires >=3.9"


def _check_langgraph() -> tuple[bool, str]:
    try:
        import langgraph  # type: ignore[import]

        version = getattr(langgraph, "__version__", None)
        if not version:
            try:
                from importlib.metadata import version as pkg_version

                version = pkg_version("langgraph")
            except Exception:
                version = "unknown"
        # Check for StateGraph availability
        from langgraph.graph import StateGraph  # type: ignore[import]  # noqa: F401

        # Check for compile kwargs support (checkpointer, interrupt_before)
        try:
            import re

            nums = re.findall(r"\d+", version)
            major = int(nums[0]) if nums else 0
            minor = int(nums[1]) if len(nums) > 1 else 0
            if major == 0 and minor < 2:
                return False, (
                    f"langgraph {version} — ARGUS requires >=0.2.0. "
                    f"Run: pip install --upgrade langgraph"
                )
        except (ValueError, IndexError):
            pass  # can't parse version, assume OK
        return True, f"langgraph {version}"
    except ImportError:
        return False, "langgraph not installed — run: pip install langgraph>=0.2.0"
    except Exception as e:
        return False, f"langgraph import error: {e}"


def _check_storage() -> tuple[bool, str]:
    argus_dir = Path.cwd() / ".argus"
    runs_dir = argus_dir / "runs"

    if not argus_dir.exists():
        return True, ".argus/ not yet created (will be created on first run)"

    if not runs_dir.exists():
        return True, ".argus/runs/ not yet created"

    run_files = list(runs_dir.glob("*.json"))
    if not run_files:
        return True, ".argus/runs/ exists, 0 runs stored"

    # Try loading the most recent run to check integrity
    errors = 0
    for f in run_files[:5]:  # spot-check first 5
        try:
            json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            errors += 1

    total = len(run_files)
    if errors > 0:
        return False, f"{total} runs stored, {errors} corrupted (of {min(5, total)} checked)"
    return True, f"{total} runs stored, all healthy"


def _check_replay_readiness() -> tuple[bool, str]:
    """Check if the most recent run has node_fn_refs for factory-free replay."""
    runs_dir = Path.cwd() / ".argus" / "runs"
    if not runs_dir.exists():
        return True, "no runs yet — replay readiness will be checked after first run"

    run_files = sorted(runs_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not run_files:
        return True, "no runs yet"

    try:
        data = json.loads(run_files[0].read_text(encoding="utf-8"))
    except Exception:
        return False, "cannot read latest run file"

    refs = data.get("node_fn_refs")
    if not refs:
        return False, (
            "latest run has no node_fn_refs — replay requires --app flag. "
            "Re-record with the latest ARGUS to enable factory-free replay."
        )

    # Try importing each node function
    import os

    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    importable = 0
    failed: list[str] = []
    for node_name, ref in refs.items():
        if ":" not in ref:
            failed.append(node_name)
            continue
        module_path, qualname = ref.rsplit(":", 1)
        try:
            mod = importlib.import_module(module_path)
            obj = mod
            for attr in qualname.split("."):
                obj = getattr(obj, attr)
            importable += 1
        except Exception:
            failed.append(node_name)

    if failed:
        return False, (
            f"{importable}/{len(refs)} node functions importable. "
            f"Failed: {', '.join(failed)}. "
            f"Ensure these modules are on sys.path."
        )
    return True, f"all {importable} node functions importable for replay"


def _check_optional_deps() -> tuple[bool, str]:
    parts: list[str] = []

    # langgraph (ArgusWatcher)
    try:
        import langgraph  # type: ignore[import]  # noqa: F401

        parts.append("langgraph ✓")
    except ImportError:
        parts.append("langgraph ✗ (pip install argus-agents[langgraph])")

    # OpenAI for LLM investigation
    try:
        import openai  # type: ignore[import]  # noqa: F401

        from argus.llm_proxy import is_available as _llm_ok

        if _llm_ok():
            parts.append("openai ✓ (proxy active)")
        else:
            parts.append("openai ✓ (not logged in — run: argus login)")
    except ImportError:
        parts.append("openai ✗ (pip install argus-agents[llm])")

    # python-dotenv
    try:
        import dotenv  # type: ignore[import]  # noqa: F401

        parts.append("dotenv ✓")
    except ImportError:
        parts.append("dotenv ✗ (pip install argus-agents[llm])")

    return True, ", ".join(parts)


def _check_llm_mode() -> tuple[bool, str]:
    """Report which LLM path is active: BYOK / hosted / heuristic-only."""
    import os

    from argus.cloud import SUPABASE_URL
    from argus.user_config import get_provider, resolve_key

    provider = get_provider()
    if resolve_key(provider):
        env_names = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}
        env_present = os.environ.get(env_names.get(provider, "")) or (
            provider == "google"
            and (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))
        )
        source = "env" if env_present else "saved"
        return True, f"BYOK ({source}) — calling {provider} directly"
    if SUPABASE_URL is not None:
        from argus.cloud import is_logged_in

        if is_logged_in():
            return True, "hosted proxy (logged in)"
        return True, "hosted available — run: argus login (or set OPENAI_API_KEY)"
    return True, "heuristic-only — no key set (argus key set to enable LLM checks)"


def doctor() -> None:
    """Run all diagnostic checks and print results."""
    console.print()
    header = Text("argus doctor", style="bold italic")
    console.print(f"  {header}")
    console.print()
    console.print("  [dim]checking environment for integration issues...[/dim]")
    console.print()

    checks = [
        ("python", _check_python_version),
        ("langgraph", _check_langgraph),
        ("storage", _check_storage),
        ("llm", _check_llm_mode),
        ("replay", _check_replay_readiness),
        ("optional deps", _check_optional_deps),
    ]

    all_passed = True
    for name, check_fn in checks:
        try:
            passed, message = check_fn()
        except Exception as e:
            passed, message = False, f"check failed: {e}"

        if passed:
            icon = "[bold green]✓[/bold green]"
        else:
            icon = "[bold red]✗[/bold red]"
            all_passed = False

        console.print(f"  {icon}  [bold]{name:<16}[/bold] {message}")

    console.print()

    if all_passed:
        console.print("  [bold green]all checks passed[/bold green]")
    else:
        console.print(
            "  [bold yellow]some checks failed[/bold yellow] — "
            "fix the issues above for reliable ARGUS operation"
        )

    console.print()
    print_footer()
