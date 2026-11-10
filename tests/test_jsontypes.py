"""argus.jsontypes parses like json.loads and narrows without coercion."""

import math
from pathlib import Path

import pytest
from pydantic import ValidationError

from argus import jsontypes, prompts


def test_loads_keeps_json_loads_semantics() -> None:
    value = jsontypes.loads('[NaN, Infinity, 123456789012345678901234567890, "\\ud800", 1.5]')
    assert isinstance(value, list)
    first = value[0]
    assert isinstance(first, float) and math.isnan(first)
    assert value[1:] == [math.inf, 123456789012345678901234567890, "\ud800", 1.5]


def test_loads_errors_are_value_errors() -> None:
    with pytest.raises(ValueError):
        jsontypes.loads("{")
    with pytest.raises(ValueError):
        jsontypes.loads_object("[1]")


def test_loads_object() -> None:
    assert jsontypes.loads_object(b'{"a": {"b": [null, true]}}') == {"a": {"b": [None, True]}}


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"a": 1}, {"a": 1}),
        ([1], None),
        ({1: 2}, None),
        ({"a": (1, 2)}, None),
        ({"p": Path("/")}, None),
    ],
)
def test_as_object(value: object, expected: object) -> None:
    assert jsontypes.as_object(value) == expected


def test_as_list_and_scalars() -> None:
    assert jsontypes.as_list([1, "a", None]) == [1, "a", None]
    assert jsontypes.as_list((1,)) is None
    assert jsontypes.as_str("x") == "x"
    assert jsontypes.as_str(1) is None
    assert jsontypes.as_number(2) == 2
    assert jsontypes.as_number(2.5) == 2.5
    assert jsontypes.as_number(True) is None
    assert jsontypes.as_number("2") is None


def test_adapters_reject_non_json() -> None:
    bad: object = {"a": object()}
    with pytest.raises(ValidationError):
        jsontypes.JSON_VALUE.validate_python(bad, strict=True)


def _objects(value: jsontypes.JsonValue) -> list[jsontypes.JsonObject]:
    """Every object schema (a dict with "type": "object") inside a schema."""
    if isinstance(value, list):
        return [o for item in value for o in _objects(item)]
    if not isinstance(value, dict):
        return []
    found = [value] if value.get("type") == "object" else []
    return found + [o for item in value.values() for o in _objects(item)]


def test_closed_schema_closes_every_derive_schema() -> None:
    # Anthropic rejects an object schema without additionalProperties: false (HTTP 400)
    schemas = [
        prompts.ASSIGN_SCHEMA,
        prompts.CONS_SCHEMA,
        prompts.OUT_SCHEMA,
        prompts.RUN_SCHEMA,
        prompts.REV_SCHEMA,
        prompts.PROP_SCHEMA,
        prompts.PATCH_SCHEMA,
    ]
    for schema in schemas:
        closed = jsontypes.closed_schema(schema)
        objects = _objects(closed)
        assert objects and all(o["additionalProperties"] is False for o in objects)
        assert _objects(schema) and all("additionalProperties" not in o for o in _objects(schema))


def test_closed_schema_keeps_explicit_and_property_names() -> None:
    schema: jsontypes.JsonObject = {
        "type": "object",
        "properties": {
            "type": {"type": "string", "enum": ["object"]},
            "meta": {"type": "object", "additionalProperties": True, "properties": {}},
        },
    }
    assert jsontypes.closed_schema(schema) == {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "type": {"type": "string", "enum": ["object"]},
            "meta": {"type": "object", "additionalProperties": True, "properties": {}},
        },
    }
