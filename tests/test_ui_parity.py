"""Guard: every enum value the Python side emits must have a UI label.

The dashboard mirrors Python enums by hand in TypeScript. When a detection rule
is added in `inspector.py` without a matching entry in `failure-labels.ts`, the UI
silently renders a grey "Unknown" chip. These tests fail instead.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
WEBSITE = REPO / "website"

pytestmark = pytest.mark.unit


def _python_failure_types() -> set[str]:
    """Every failure_type literal emitted anywhere in src/argus."""
    found: set[str] = set()
    for path in (REPO / "src" / "argus").rglob("*.py"):
        found |= set(re.findall(r'failure_type="([a-z_]+)"', path.read_text()))
    # Category-mapped types never appear as a literal at the ToolFailure call site.
    inspector = (REPO / "src" / "argus" / "inspector.py").read_text()
    block = re.search(r"_CATEGORY_TO_FAILURE.*?\n}", inspector, re.S)
    assert block, "_CATEGORY_TO_FAILURE not found in inspector.py"
    found |= set(re.findall(r':\s*"([a-z_]+)"', block.group(0)))
    return found


def _ui_failure_labels() -> set[str]:
    """Keys of FAILURE_META in website/lib/failure-labels.ts."""
    src = (WEBSITE / "lib" / "failure-labels.ts").read_text()
    block = re.search(r"FAILURE_META:\s*Record<string,\s*FailureMeta>\s*=\s*{(.*?)\n}", src, re.S)
    assert block, "FAILURE_META object not found in failure-labels.ts"
    return set(re.findall(r"^\s*([a-z_]+):\s*{", block.group(1), re.M))


def _python_behavior_types() -> set[str]:
    """Keys of ``BEHAVIOR_PROFILES`` in anomaly_detector.py.

    Read from the source, not hardcoded: a hardcoded list can only ever confirm
    what the test already knew, so a profile added in Python — exactly the case
    this guard exists for — would never trip it.
    """
    src = (REPO / "src" / "argus" / "anomaly_detector.py").read_text()
    block = re.search(
        r"BEHAVIOR_PROFILES:\s*dict\[str, dict\[str, Any\]\]\s*=\s*{(.*?)\n}", src, re.S
    )
    assert block, "BEHAVIOR_PROFILES not found in anomaly_detector.py"
    found = set(re.findall(r'^ {4}"([a-z_]+)":\s*{', block.group(1), re.M))
    assert found, "no behavior profiles parsed out of BEHAVIOR_PROFILES"
    return found


def _ui_behavior_labels() -> set[str]:
    src = (WEBSITE / "components" / "run-detail" / "BehaviorPanel.tsx").read_text()
    block = re.search(r"BEHAVIOR_LABELS:\s*Record<string,\s*string>\s*=\s*{(.*?)\n}", src, re.S)
    assert block, "BEHAVIOR_LABELS object not found in BehaviorPanel.tsx"
    return set(re.findall(r"^\s*([a-z_]+):", block.group(1), re.M))


def test_every_failure_type_has_a_ui_label() -> None:
    missing = _python_failure_types() - _ui_failure_labels()
    assert not missing, (
        f"failure types with no UI label (they render as grey 'Unknown'): "
        f"{sorted(missing)}. Add them to website/lib/failure-labels.ts."
    )


def test_every_behavior_type_has_a_ui_label() -> None:
    missing = _python_behavior_types() - _ui_behavior_labels()
    assert not missing, (
        f"behavior profiles with no UI label (they render as raw snake_case): "
        f"{sorted(missing)}. Add them to BEHAVIOR_LABELS in BehaviorPanel.tsx."
    )


def _step_statuses() -> set[str]:
    models = (REPO / "src" / "argus" / "models.py").read_text()
    block = re.search(r"StepStatus\s*=\s*Literal\[(.*?)\]", models, re.S)
    assert block, "StepStatus Literal not found in models.py"
    statuses = set(re.findall(r'"([a-z_]+)"', block.group(1)))
    assert statuses, "no step statuses parsed out of models.py"
    return statuses


def test_every_step_status_is_rendered() -> None:
    """All StepStatus values must appear in StatusBadge.tsx."""
    badge = (WEBSITE / "components" / "StatusBadge.tsx").read_text()
    missing = {s for s in _step_statuses() if s not in badge}
    assert not missing, f"step statuses not handled in StatusBadge.tsx: {sorted(missing)}"


def test_every_step_status_is_coloured_in_the_graph() -> None:
    """`mapStatus` must name every status explicitly.

    Its `default` branch is a catch-all, so a status it forgets is painted
    whatever the fallback happens to be rather than flagged — `interrupted`
    rendered as a green pass this way.
    """
    src = (WEBSITE / "lib" / "graph-model.ts").read_text()
    block = re.search(r"function mapStatus\(.*?\n}", src, re.S)
    assert block, "mapStatus not found in lib/graph-model.ts"
    handled = set(re.findall(r"case '([a-z_]+)':", block.group(0)))
    missing = _step_statuses() - handled
    assert not missing, (
        f"step statuses with no explicit case in graph-model.mapStatus "
        f"(they fall through to the default colour): {sorted(missing)}"
    )


def test_tool_failure_union_matches_the_label_map() -> None:
    """`ToolFailure.failure_type` and FAILURE_META must list the same types.

    A type in one but not the other either renders as 'Unknown' or fails to
    type-check at the call site that produces it.
    """
    types_src = (WEBSITE / "lib" / "types.ts").read_text()
    block = re.search(r"failure_type:\n(.*?)\n\s+field_name:", types_src, re.S)
    assert block, "ToolFailure.failure_type union not found in types.ts"
    union = set(re.findall(r"'([a-z_]+)'", block.group(1)))
    labels = _ui_failure_labels()
    assert union == labels, (
        f"only in the union: {sorted(union - labels)}; "
        f"only in FAILURE_META: {sorted(labels - union)}"
    )
