"""A small JSON Schema validator for the published schemas, with no dependency.

tests/test_telemetry_card.py carries a lighter one that skips ``pattern`` and ``anyOf``. The run
card's whole privacy contract is its patterns (a key must look like an identifier), so this one
checks them, and it *refuses* a keyword it does not check: the schema can never rely on a rule
nothing here enforces. The server runs a full validator.
"""

from __future__ import annotations

import math
import re
from typing import Any

_ANNOTATIONS = {"$schema", "$id", "title", "description", "$defs"}
_KEYWORDS = _ANNOTATIONS | {
    "$ref",
    "type",
    "enum",
    "properties",
    "required",
    "additionalProperties",
    "propertyNames",
    "items",
    "anyOf",
    "pattern",
    "minimum",
    "maximum",
    "minLength",
    "maxLength",
    "maxItems",
    "maxProperties",
}
_TYPES: dict[str, Any] = {
    "object": dict,
    "array": list,
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": (int, float),
    "null": type(None),
}


def _is(value: Any, kind: str) -> bool:
    if kind in ("integer", "number") and isinstance(value, bool):
        return False
    if kind == "number" and isinstance(value, float) and not math.isfinite(value):
        return False
    return isinstance(value, _TYPES[kind])


def _resolve(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    node: Any = root
    for part in ref.removeprefix("#/").split("/"):
        node = node[part]
    return node  # type: ignore[no-any-return]


def validate(value: Any, schema: dict[str, Any], root: dict[str, Any] | None = None, path="$"):
    """Raise ``AssertionError`` naming the path of the first thing ``value`` gets wrong."""
    root = schema if root is None else root
    unchecked = set(schema) - _KEYWORDS
    assert not unchecked, f"{path}: the schema uses {sorted(unchecked)}, which nothing here checks"
    if "$ref" in schema:
        return validate(value, _resolve(schema["$ref"], root), root, path)
    if "anyOf" in schema:
        failures = []
        for option in schema["anyOf"]:
            try:
                validate(value, option, root, path)
                break
            except AssertionError as exc:
                failures.append(str(exc))
        else:
            raise AssertionError(f"{path}: matches none of anyOf: {failures}")
    if "type" in schema:
        kinds = [schema["type"]] if isinstance(schema["type"], str) else schema["type"]
        assert any(_is(value, k) for k in kinds), f"{path}: {value!r} is not {kinds}"
    if "enum" in schema:
        assert value in schema["enum"], f"{path}: {value!r} is not one of {schema['enum']}"
    if isinstance(value, str):
        _check_string(value, schema, path)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema:
            assert value >= schema["minimum"], f"{path}: {value} < {schema['minimum']}"
        if "maximum" in schema:
            assert value <= schema["maximum"], f"{path}: {value} > {schema['maximum']}"
    if isinstance(value, list):
        _check_array(value, schema, root, path)
    if isinstance(value, dict):
        _check_object(value, schema, root, path)
    return None


def _check_string(value: str, schema: dict[str, Any], path: str) -> None:
    if "minLength" in schema:
        assert len(value) >= schema["minLength"], f"{path}: {value!r} is too short"
    if "maxLength" in schema:
        assert len(value) <= schema["maxLength"], f"{path}: longer than {schema['maxLength']}"
    if "pattern" in schema:
        pattern = schema["pattern"]
        # ECMA ``^...$`` matches the whole string; Python's ``$`` also matches before a final
        # newline, so match the body with fullmatch and refuse an unanchored pattern.
        assert pattern.startswith("^") and pattern.endswith("$"), f"{path}: unanchored {pattern}"
        assert re.fullmatch(pattern[1:-1], value), f"{path}: {value!r} does not match {pattern}"


def _check_array(value: list[Any], schema: dict[str, Any], root: dict[str, Any], path: str):
    if "maxItems" in schema:
        assert len(value) <= schema["maxItems"], f"{path}: more than {schema['maxItems']} items"
    if "items" in schema:
        for i, item in enumerate(value):
            validate(item, schema["items"], root, f"{path}[{i}]")


def _check_object(value: dict[str, Any], schema: dict[str, Any], root: dict[str, Any], path: str):
    if "maxProperties" in schema:
        assert len(value) <= schema["maxProperties"], f"{path}: more than the allowed keys"
    for key in schema.get("required", []):
        assert key in value, f"{path}: missing {key}"
    properties = schema.get("properties", {})
    extra = schema.get("additionalProperties", True)
    for key, item in value.items():
        if "propertyNames" in schema:
            validate(key, schema["propertyNames"], root, f"{path}.<key {key!r}>")
        if key in properties:
            validate(item, properties[key], root, f"{path}.{key}")
        elif extra is False:
            raise AssertionError(f"{path}: unknown field {key!r}")
        elif isinstance(extra, dict):
            validate(item, extra, root, f"{path}.{key}")
