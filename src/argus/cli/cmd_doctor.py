"""argus doctor — integration diagnostic checks.

Validates that ARGUS can function correctly in the current environment:
LangGraph version, Python version, storage health, and replay readiness.
"""

from __future__ import annotations

import importlib
import json
import sys

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
        return False, "langgraph not installed — run: pip install argus-agents"
    except Exception as e:
        return False, f"langgraph import error: {e}"


def _check_storage() -> tuple[bool, str]:
    from argus.storage import argus_dir, runs_dir  # noqa: PLC0415

    stored_argus = argus_dir()
    stored_runs = runs_dir(create=False)
    hint = (
        "If you expected runs: call watcher.attach(graph) before invoke; "
        "files go under the project root (git / pyproject.toml / $ARGUS_DIR), "
        "not the process cwd; cyclic graphs persist when invoke() returns "
        "(no finalize() needed with attach()); "
        "install with pip install argus-agents so the argus command is available."
    )

    if not stored_argus.exists():
        return True, f".argus/ not yet created at {stored_argus} — {hint}"

    if not stored_runs.exists():
        return True, f".argus/runs/ not yet created at {stored_runs} — {hint}"

    run_files = list(stored_runs.glob("*.json"))
    if not run_files:
        return True, f"0 runs stored in {stored_runs}. {hint}"

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
    from argus.storage import runs_dir  # noqa: PLC0415

    stored_runs = runs_dir(create=False)
    if not stored_runs.exists():
        return True, "no runs yet — replay readiness will be checked after first run"

    run_files = sorted(stored_runs.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
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


def _check_package_identity() -> tuple[bool, str]:
    """Warn if the unrelated PyPI package named ``argus`` is installed."""
    import importlib.metadata as md

    try:
        md.distribution("argus")
    except md.PackageNotFoundError:
        return True, "argus-agents (not the unrelated PyPI package 'argus')"
    return False, (
        "unrelated PyPI package 'argus' is installed and may shadow this CLI. "
        "Uninstall it: pip uninstall argus   then: pip install argus-agents"
    )


def _check_optional_deps() -> tuple[bool, str]:
    """UI report payload only. LLM is key-gated; no pip extra."""
    return True, "no extra packages required (LLM: argus key set)"


def _check_suppressions() -> tuple[bool, str]:
    from argus.suppressions import config_path, load_suppressions  # noqa: PLC0415

    items = load_suppressions()
    if not items:
        return True, "none  [dim](argus ignore <SIG-ID> to silence a noisy signature)[/dim]"
    shown = ", ".join(s.label for s in items[:4])
    more = f" +{len(items) - 4} more" if len(items) > 4 else ""
    return True, f"{len(items)} active: {shown}{more}  [dim]{config_path()}[/dim]"


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
        return True, (
            "heuristic-only — no key set "
            "(argus key set to enable LLM; argus login optional for hosted)"
        )
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
        ("package", _check_package_identity),
        ("langgraph", _check_langgraph),
        ("storage", _check_storage),
        ("llm", _check_llm_mode),
        ("suppressions", _check_suppressions),
        ("replay", _check_replay_readiness),
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
