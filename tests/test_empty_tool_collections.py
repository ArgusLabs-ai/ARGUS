"""S1 / S2: an empty FHIR `entry` or Datadog `series` is a failed lookup.

`{"resourceType": "Bundle", "total": 0, "entry": []}` is "patient not found";
`{"status": "ok", "series": []}` is a metrics query that matched nothing. Both
are the same shape as `documents: []` and grade the same way: critical, unless
the node writes a field declared `allow_empty`.
"""

from __future__ import annotations

import pytest

from argus.inspector import inspect_tool_calls


def _sev(found, field):
    return next((t.severity for t in found if t.field_name == field), None)


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload,field",
    [
        ({"resourceType": "Bundle", "total": 0, "entry": []}, "fhir.entry"),
        ({"status": "ok", "series": []}, "fhir.series"),
    ],
)
def test_an_empty_lookup_is_critical(payload, field):
    found = inspect_tool_calls([{"name": "fhir", "output": payload}])
    assert _sev(found, field) == "critical", [(t.field_name, t.severity) for t in found]


@pytest.mark.unit
@pytest.mark.parametrize("payload", [{"entry": []}, {"series": []}])
def test_allow_empty_still_softens_it(payload):
    found = inspect_tool_calls([{"name": "fhir", "output": payload}], allow_empty=True)
    assert {t.severity for t in found} <= {"warning"}


@pytest.mark.unit
@pytest.mark.parametrize(
    "payload",
    [
        {"entry": [{"resource": {"resourceType": "Patient"}}]},
        {"series": [{"metric": "p99", "pointlist": [[1, 2.0]]}]},
        {"journal_entry": []},
    ],
)
def test_a_filled_lookup_or_other_key_is_not_critical(payload):
    found = inspect_tool_calls([{"name": "fhir", "output": payload}])
    assert all(t.severity != "critical" for t in found), [
        (t.field_name, t.severity) for t in found
    ]
