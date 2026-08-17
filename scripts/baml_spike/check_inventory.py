"""B3 input: is the tracker's §2 migration inventory live, and is it complete?

The BAML adoption tracker lists six modules that make an LLM call and
hand-parse JSON, calls that list verified, and B3 goes straight to picking one
to pilot a migration on. That skips two questions this script answers instead
of assuming:

  1. Does every listed module actually run? A module nothing imports cannot
     execute, whatever its contents look like.
  2. Is the list complete? It was written by hand.

Both are measured statically. LLM call sites are **discovered**, not taken
from the tracker — every module in the package is scanned for a
`create_chat_completion(...)` call (the shared path) or a
`...chat.completions.create(...)` call (the direct-OpenAI path, tracker B2) —
and the result is then diffed against the §2 list, so a site the tracker
missed shows up as a finding rather than staying invisible.

Reachability is walked from the two entry points the distribution exposes:
`argus` for library users and `argus.cli.main` for the `argus` console script,
per pyproject `[project.scripts]`.

    python scripts/baml_spike/check_inventory.py            # summary table
    python scripts/baml_spike/check_inventory.py --verbose  # + evidence

Import edges are read with `ast`, not by importing anything, so this is safe
to run anywhere and needs no API keys, no login, and no dependencies beyond
the standard library. Function-local imports count as edges — ARGUS uses them
throughout to keep optional paths cheap, and a graph that ignored them would
report most of the package as dead.

Exit code is 0 unless the harness itself broke. An UNREACHABLE verdict is a
finding to act on, not a failure of this script.

Recorded output lives in INVENTORY.md next to this file.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from collections import deque
from typing import NamedTuple

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.normpath(os.path.join(HERE, "..", "..", "src"))
PKG = os.path.join(SRC, "argus")

# The two entry points the distribution exposes. Everything a user can reach
# starts at one of these.
ENTRY_POINTS = ("argus", "argus.cli.main")

# The six modules the tracker's §2 inventory names, for the completeness diff.
TRACKER_SITES = frozenset(
    {
        "argus.llm_investigator",
        "argus.llm_correlator",
        "argus.semantic_checker",
        "argus.heuristic_disambiguator",
        "argus.source_locator",
        "argus.signature_generalizer",
    }
)

# Shared-path entry point. `argus.llm_proxy` defines it rather than calling it,
# so the module is not self-detected.
SHARED_CALL = "create_chat_completion"
# Direct-OpenAI client shape, reached via `embedding_store._get_client()`.
DIRECT_CALL_SUFFIX = "chat.completions.create"


# ── Module discovery ─────────────────────────────────────────────────────────


def discover_modules() -> set[str]:
    """Every importable module name under src/argus."""
    found: set[str] = set()
    for root, _dirs, files in os.walk(PKG):
        if "__pycache__" in root:
            continue
        for fname in files:
            if not fname.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(root, fname), SRC)
            parts = rel[:-3].split(os.sep)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                found.add(".".join(parts))
    return found


MODULES = discover_modules()


def path_for(module: str) -> str | None:
    """Source file backing a module name, package `__init__` included."""
    base = os.path.join(SRC, *module.split("."))
    pkg_init = os.path.join(base, "__init__.py")
    if os.path.isfile(pkg_init):
        return pkg_init
    flat = base + ".py"
    return flat if os.path.isfile(flat) else None


def parse(module: str) -> ast.Module | None:
    path = path_for(module)
    if path is None:
        return None
    with open(path, encoding="utf-8") as fh:
        return ast.parse(fh.read(), filename=path)


# ── Import graph ─────────────────────────────────────────────────────────────


def ancestors(module: str) -> set[str]:
    """Importing a.b.c also executes a/__init__.py and a/b/__init__.py."""
    parts = module.split(".")
    chain = {".".join(parts[: i + 1]) for i in range(len(parts))}
    return chain & MODULES


def imports_of(module: str) -> set[str]:
    """In-package modules `module` imports, at any nesting depth.

    Covers `import a.b`, `from a.b import c` where c is itself a module, and
    relative forms. Function-local imports are included deliberately — they
    are real edges, and ARGUS leans on them heavily.
    """
    tree = parse(module)
    if tree is None:
        return set()

    # A relative import resolves against the containing package, which is the
    # module itself when it is a package `__init__`.
    parts = module.split(".")
    package = parts if os.path.isfile(os.path.join(SRC, *parts, "__init__.py")) else parts[:-1]

    edges: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in MODULES:
                    edges.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - node.level + 1]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if target in MODULES:
                edges.add(target)
            # `from argus import semantic_checker` names a module, not a symbol.
            for alias in node.names:
                candidate = f"{target}.{alias.name}" if target else alias.name
                if candidate in MODULES:
                    edges.add(candidate)
    return edges


def walk_imports() -> dict[str, list[str]]:
    """BFS from the entry points, returning module -> shortest import path."""
    paths: dict[str, list[str]] = {}
    queue: deque[str] = deque()
    for entry in ENTRY_POINTS:
        if entry in MODULES:
            paths[entry] = [entry]
            queue.append(entry)

    while queue:
        current = queue.popleft()
        for edge in imports_of(current):
            for module in {edge} | ancestors(edge):
                if module not in paths:
                    paths[module] = paths[current] + [module]
                    queue.append(module)
    return paths


# ── LLM call-site discovery ──────────────────────────────────────────────────


class Call(NamedTuple):
    line: int
    transport: str


def llm_calls_in(module: str) -> list[Call]:
    """Every LLM call expression in a module, with its transport."""
    tree = parse(module)
    if tree is None:
        return []

    found: list[Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == SHARED_CALL:
            found.append(Call(node.lineno, "shared proxy"))
        elif isinstance(func, ast.Attribute):
            if func.attr == SHARED_CALL:
                found.append(Call(node.lineno, "shared proxy"))
            elif ast.unparse(func).endswith(DIRECT_CALL_SUFFIX):
                found.append(Call(node.lineno, "direct OpenAI"))
    return sorted(found)


def discover_sites() -> dict[str, list[Call]]:
    """Every module in the package that makes an LLM call."""
    sites: dict[str, list[Call]] = {}
    for module in sorted(MODULES):
        calls = llm_calls_in(module)
        if calls:
            sites[module] = calls
    return sites


def transports(calls: list[Call]) -> str:
    return " + ".join(sorted({c.transport for c in calls}))


# ── Report ───────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="show the import path that reaches each site and every call line",
    )
    args = parser.parse_args()

    reachable = walk_imports()
    live = set(reachable)
    sites = discover_sites()

    print(f"package modules   : {len(MODULES)}")
    print(f"reachable         : {len(live)}")
    print(f"entry points      : {', '.join(ENTRY_POINTS)}")
    print(f"LLM call sites    : {len(sites)} discovered, {len(TRACKER_SITES)} in tracker §2")
    print()

    print(f"{'module':<34} {'calls':<7} {'import':<12} {'transport':<15} §2")
    print("-" * 82)
    for module, calls in sites.items():
        listed = "yes" if module in TRACKER_SITES else "MISSING"
        print(
            f"{module:<34} "
            f"{str(len(calls)) + 'x':<7} "
            f"{('reachable' if module in live else 'UNREACHABLE'):<12} "
            f"{transports(calls):<15} "
            f"{listed}"
        )

    # A tracker entry that makes no LLM call at all would be the other kind of
    # drift — listed but not actually a migration site.
    for module in sorted(TRACKER_SITES - set(sites)):
        print(f"{module:<34} {'-':<7} {'?':<12} {'no LLM call':<15} listed")
    print()

    if args.verbose:
        for module, calls in sites.items():
            print(f"── {module}")
            path = reachable.get(module)
            if path is None:
                print("   imported by: nothing — no path from either entry point")
            else:
                print(f"   imported by: {' -> '.join(path)}")
            for call in calls:
                print(f"   LLM call   : {module}:{call.line} ({call.transport})")
            print()

    dead_sites = [m for m in sites if m not in live]
    if dead_sites:
        print("Unreachable LLM call sites:")
        for module in dead_sites:
            print(f"  {module}")
        print()

    dead = sorted(MODULES - live)
    if dead:
        print("All unreachable modules in the package:")
        for module in dead:
            print(f"  {module}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
