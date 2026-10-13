"""Declarative behavior contracts, written as Markdown spec files.

A behavior spec is a written contract for agent conduct: "always confirm
before deleting", "never claim a tool ran when it did not". Each ``## ``
(H2) section is one independently-judgeable clause, so a run can be judged
clause-by-clause and the per-clause verdicts folded deterministically (the
judge never aggregates — see :mod:`argus.behavior_judge` and the fold in
:mod:`argus.models`).

Specs live at ``.argus/behaviors/<name>/BEHAVIOR.md`` or, for single-file
specs, ``.argus/behaviors/<name>.md`` (``ARGUS_DIR`` is honored via
``storage.argus_dir``, same as runs/checks).

ponytail: the brief said YAML frontmatter, but PyYAML is not a declared
dependency of this package (same call as ``judge_specs.py``) — the
frontmatter here is deliberately a tiny ``key: value`` subset that a
hand-rolled parser handles in a few lines, and unknown keys are ignored so
the format can grow without breaking old readers.

Reviewer, not cop: a behavior verdict is a judgment on the record, never a
CI gate on its own — same rule as the coherence judge in
:mod:`argus.semantic_checker`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("argus")

_NAME_MAX = 64
_DESCRIPTION_MAX = 1024
_KEBAB_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")  # a code-fence line: marker run, rest


@dataclass(frozen=True)
class Diagnostic:
    """One parse/validation problem with a spec file.

    Mirrors the shape consumers already know from ``models.Finding`` —
    severity, a stable machine-readable code, a human sentence, and where it
    lives — but targets a file on disk rather than a run, so it carries
    path/line/column instead of node/field.
    """

    severity: str  # "error" | "warning"
    code: str  # stable machine-readable, e.g. "duplicate-clause"
    message: str  # full sentence readable without opening the file
    path: Path = field(compare=False)  # source file the problem lives in
    line: int = 0  # 1-based; 0 = file-level
    column: int = 0  # 1-based


@dataclass(frozen=True)
class BehaviorClause:
    """One independently-judgeable behavior clause (an H2 section)."""

    name: str  # the heading text
    text: str  # heading + body, verbatim — the judge quotes substrings of this


@dataclass(frozen=True)
class BehaviorSpec:
    """One parsed behavior spec: a named contract with judgeable clauses."""

    name: str  # kebab-case, ≤64 chars, from frontmatter
    description: str  # ≤1024 chars, from frontmatter
    clauses: tuple[BehaviorClause, ...]  # one per H2 section, in file order
    path: Path = field(compare=False)  # source file, for diagnostics


def default_behaviors_dir() -> Path:
    """``<project-root>/.argus/behaviors`` — does not create the directory."""
    from argus.storage import argus_dir

    return argus_dir() / "behaviors"


def _parse_frontmatter(
    lines: list[str], path: Path
) -> tuple[dict[str, str], dict[str, int], list[Diagnostic], int]:
    """Split ``---`` frontmatter from the Markdown body.

    Returns ``(values, key_lines, diagnostics, body_start_line)``.
    ``key_lines`` maps each frontmatter key to its 1-based line so a
    diagnostic can point at the offending value, not just the file.
    ``body_start_line`` is the 1-based line number of the first body line
    (0 when there is no body). Structural problems (no opening fence, no
    closing fence, a line that is not ``key: value``) are errors and the
    caller skips the file — a spec whose contract cannot be read must not
    be half-judged.
    """
    diagnostics: list[Diagnostic] = []
    if not lines or lines[0].strip() != "---":
        diagnostics.append(
            Diagnostic(
                "error",
                "missing-frontmatter",
                f"{path.name}: file must start with a '---' frontmatter fence; skipped",
                path,
                1,
                1,
            )
        )
        return {}, {}, diagnostics, 0

    values: dict[str, str] = {}
    key_lines: dict[str, int] = {}
    closing = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            closing = i
            break
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*(.*)$", lines[i])
        if m is None:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "invalid-frontmatter-line",
                    f"{path.name}: line {i + 1} is not a 'key: value' pair; skipped",
                    path,
                    i + 1,
                    1,
                )
            )
            continue
        values[m.group(1)] = m.group(2).strip()
        key_lines[m.group(1)] = i + 1
    if closing < 0:
        diagnostics.append(
            Diagnostic(
                "error",
                "unterminated-frontmatter",
                f"{path.name}: frontmatter has no closing '---' fence; skipped",
                path,
                1,
                1,
            )
        )
        return {}, {}, diagnostics, 0
    return values, key_lines, diagnostics, closing + 2  # 1-based line after the fence


def _fence_after(line: str, fence: str | None) -> str | None:
    """The open code-fence marker once ``line`` is read (``None`` = outside one).

    A fence closes only on the same marker character, at least as long, with
    nothing after it — so a ``~~~`` block is not ended by a backtick fence.
    """
    m = _FENCE_RE.match(line)
    if m is None:
        return fence
    run, rest = m.groups()
    if fence is None:
        # a backtick fence's info string cannot hold backticks: that is inline code
        return None if run[0] == "`" and "`" in rest else run
    if run[0] == fence[0] and len(run) >= len(fence) and not rest.strip():
        return None
    return fence


def _split_clauses(
    body_lines: list[str], body_start: int, path: Path
) -> tuple[list[BehaviorClause], list[Diagnostic]]:
    """Group the body into clauses at each ``## ``, rejecting duplicate names.

    The clause text keeps the heading line so the judge's ``violated_clause``
    citation can quote it verbatim and a reader can find the section. A
    duplicate H2 is an error: two clauses that cannot be told apart would
    make per-clause verdicts ambiguous, which defeats the machine-verifiable
    fold. A ``##`` line inside a fenced code block is example text, not a
    heading. An unnamed heading or a heading with no body has nothing to
    judge: it is skipped with a warning, never judged on its heading alone.
    """
    clauses: list[BehaviorClause] = []
    diagnostics: list[Diagnostic] = []
    seen: set[str] = set()
    current_name: str | None = None
    current_line = 0
    current_lines: list[str] = []
    fence: str | None = None

    def _flush() -> None:
        nonlocal current_name, current_lines
        if current_name is not None:
            if "\n".join(current_lines[1:]).strip():
                clauses.append(BehaviorClause(current_name, "\n".join(current_lines).strip()))
            else:
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        "empty-clause",
                        f"{path.name}: clause '{current_name}' at line {current_line} "
                        "has no body text; skipped",
                        path,
                        current_line,
                        1,
                    )
                )
        current_name = None
        current_lines = []

    for offset, line in enumerate(body_lines):
        lineno = body_start + offset
        if fence is None and (line.startswith("## ") or line.rstrip() == "##"):
            name = line[2:].strip()
            _flush()
            if not name:
                diagnostics.append(
                    Diagnostic(
                        "warning",
                        "empty-clause-heading",
                        f"{path.name}: '##' heading at line {lineno} has no name; "
                        "its section is skipped",
                        path,
                        lineno,
                        1,
                    )
                )
            else:
                if name in seen:
                    diagnostics.append(
                        Diagnostic(
                            "error",
                            "duplicate-clause",
                            f"{path.name}: duplicate clause heading '{name}' "
                            f"at line {lineno}; skipped",
                            path,
                            lineno,
                            1,
                        )
                    )
                seen.add(name)
                current_name = name
                current_line = lineno
                current_lines = [line]
        else:
            fence = _fence_after(line, fence)
            if current_name is not None:
                current_lines.append(line)
        # content before the first H2 belongs to no clause — ignored, it is
        # intro prose; only H2 sections are judgeable contract text
    _flush()
    return clauses, diagnostics


def parse_behavior_file(path: str | Path) -> tuple[BehaviorSpec | None, list[Diagnostic]]:
    """Parse and validate one spec file. Returns ``(spec, diagnostics)``.

    Never raises: a malformed file yields diagnostics and ``None`` — one bad
    spec must not take down the ``load_behavior_specs`` sweep or a graded
    run. Any error-severity diagnostic means the file is skipped entirely;
    a spec whose contract is partly wrong is not silently half-applied.
    """
    p = Path(path)
    diagnostics: list[Diagnostic] = []
    try:
        text = p.read_text(encoding="utf-8-sig")  # a BOM would hide the opening fence
    except (OSError, UnicodeDecodeError) as exc:
        return None, [
            Diagnostic("error", "unreadable", f"{p.name}: unreadable ({exc}); skipped", p, 1, 1)
        ]

    lines = text.splitlines()
    values, key_lines, fm_diags, body_start = _parse_frontmatter(lines, p)
    diagnostics.extend(fm_diags)
    if any(d.severity == "error" for d in fm_diags):
        return None, diagnostics

    name = values.get("name", "")
    name_line = key_lines.get("name", 2)
    if not name:
        diagnostics.append(
            Diagnostic(
                "error",
                "missing-name",
                f"{p.name}: frontmatter is missing the required 'name' key; skipped",
                p,
                name_line,
                1,
            )
        )
    elif len(name) > _NAME_MAX:
        diagnostics.append(
            Diagnostic(
                "error",
                "name-too-long",
                f"{p.name}: 'name' is {len(name)} chars, max {_NAME_MAX}; skipped",
                p,
                name_line,
                1,
            )
        )
    elif not _KEBAB_RE.match(name):
        diagnostics.append(
            Diagnostic(
                "error",
                "invalid-name",
                f"{p.name}: 'name' must be kebab-case (lowercase letters, digits, "
                f"hyphens); got '{name}'; skipped",
                p,
                name_line,
                1,
            )
        )

    description = values.get("description", "")
    desc_line = key_lines.get("description", name_line + 1)
    if not description:
        diagnostics.append(
            Diagnostic(
                "error",
                "missing-description",
                f"{p.name}: frontmatter is missing the required 'description' key; skipped",
                p,
                desc_line,
                1,
            )
        )
    elif len(description) > _DESCRIPTION_MAX:
        diagnostics.append(
            Diagnostic(
                "error",
                "description-too-long",
                f"{p.name}: 'description' is {len(description)} chars, "
                f"max {_DESCRIPTION_MAX}; skipped",
                p,
                desc_line,
                1,
            )
        )

    body_lines = lines[body_start - 1 :] if body_start else []
    clauses, clause_diags = _split_clauses(body_lines, body_start, p)
    diagnostics.extend(clause_diags)
    if not clauses and not any(d.severity == "error" for d in clause_diags):
        diagnostics.append(
            Diagnostic(
                "warning",
                "no-clauses",
                f"{p.name}: no '## ' clauses — nothing to judge; the spec will fold to NA",
                p,
                body_start,
                1,
            )
        )

    if any(d.severity == "error" for d in diagnostics):
        for d in diagnostics:
            if d.severity == "error":
                logger.warning("argus behavior spec %s: %s", p, d.message)
        return None, diagnostics

    return (
        BehaviorSpec(name=name, description=description, clauses=tuple(clauses), path=p),
        diagnostics,
    )


def _discover_files(directory: Path) -> list[Path]:
    """Both layouts: ``<dir>/<name>/BEHAVIOR.md`` and ``<dir>/<name>.md``.

    Directory-layout files win on a name collision with a flat file — the
    directory is the canonical multi-file home; a stray flat file with the
    same name is almost certainly a stale copy, and judging both would
    double-report the behavior.
    """
    files: list[Path] = []
    seen_names: set[str] = set()
    try:
        for sub in sorted(directory.iterdir()):
            if sub.is_dir():
                candidate = sub / "BEHAVIOR.md"
                if candidate.is_file():
                    files.append(candidate)
                    seen_names.add(sub.name)
        for flat in sorted(directory.glob("*.md")):
            if flat.stem not in seen_names:
                files.append(flat)
    except OSError as exc:
        logger.warning("argus behavior specs: cannot list %s: %s", directory, exc)
    return files


def load_behavior_specs(directory: str | Path) -> tuple[list[BehaviorSpec], list[Diagnostic]]:
    """Parse every spec under ``directory``; never raises.

    A missing directory is a no-op (behavior specs are opt-in). Malformed
    files produce diagnostics and are skipped, never crash the sweep. Two
    files declaring the same ``name`` would fold into one verdict, so the
    first (in discovery order) wins and the later one is an error.
    """
    base = Path(directory)
    if not base.is_dir():
        return [], []
    specs: list[BehaviorSpec] = []
    diagnostics: list[Diagnostic] = []
    first_by_name: dict[str, Path] = {}
    for path in _discover_files(base):
        spec, diags = parse_behavior_file(path)
        diagnostics.extend(diags)
        if spec is None:
            continue
        if spec.name in first_by_name:
            diagnostics.append(
                Diagnostic(
                    "error",
                    "duplicate-spec-name",
                    f"{path.name}: spec name '{spec.name}' is already defined in "
                    f"{first_by_name[spec.name]}; skipped",
                    path,
                )
            )
            continue
        first_by_name[spec.name] = path
        specs.append(spec)
    return specs, diagnostics


def validate_behaviors(path: str | Path) -> list[Diagnostic]:
    """All diagnostics for the specs at ``path`` — for the CLI and tests.

    ``path`` is a directory or one spec file. Loading and validation are the
    same code path, so what the CLI reports is exactly what the judge would
    see; the caller decides whether warnings are worth a nonzero exit (they
    are not). The one difference: a path someone named that does not exist is
    an error here, because ``load_behavior_specs`` treats an absent directory
    as a no-op (specs are opt-in) and a typo must not read as "OK".
    """
    p = Path(path)
    if p.is_file():
        return parse_behavior_file(p)[1]
    if not p.is_dir():
        return [
            Diagnostic(
                "error",
                "path-not-found",
                f"{p.name}: no such file or directory; nothing was validated",
                p,
            )
        ]
    return load_behavior_specs(p)[1]
