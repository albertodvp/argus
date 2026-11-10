"""JSON values without `Any`.

`json.loads` and every Hermes payload are untyped. Argus parses JSON with `json.loads` (so NaN, big
integers and error types stay exactly as before) and then validates the result into pydantic's
`JsonValue`, a recursive union of `dict[str, JsonValue]`, `list[JsonValue]`, `str`, `bool`, `int`,
`float` and `None`. Validation is strict: nothing is coerced; a value that is not plain JSON (a
tuple, a non-string key, a Path) raises `pydantic.ValidationError`, which is a `ValueError`.

Narrow a `JsonValue` with `isinstance` (`isinstance(v, dict)` gives `JsonObject`); narrow an
arbitrary `object` (a Hermes return value, a hook argument) with the `as_*` helpers.
"""

from __future__ import annotations

import json
from typing import TypeAlias

from pydantic import JsonValue, TypeAdapter, ValidationError

__all__ = [
    "JSON_LIST",
    "JSON_OBJECT",
    "JSON_VALUE",
    "JsonList",
    "JsonObject",
    "JsonValue",
    "as_list",
    "as_number",
    "as_object",
    "as_str",
    "closed_schema",
    "loads",
    "loads_object",
]

JsonObject: TypeAlias = dict[str, JsonValue]
JsonList: TypeAlias = list[JsonValue]

JSON_VALUE = TypeAdapter[JsonValue](JsonValue)
JSON_OBJECT = TypeAdapter[JsonObject](JsonObject)
JSON_LIST = TypeAdapter[JsonList](JsonList)


def loads(text: str | bytes) -> JsonValue:
    """`json.loads(text)` as a `JsonValue`; raises ValueError on malformed JSON."""
    raw: object = json.loads(text)
    return JSON_VALUE.validate_python(raw, strict=True)


def loads_object(text: str | bytes) -> JsonObject:
    """`json.loads(text)` that must be a JSON object; raises ValueError otherwise."""
    raw: object = json.loads(text)
    return JSON_OBJECT.validate_python(raw, strict=True)


def as_object(value: object) -> JsonObject | None:
    """`value` as a JSON object (a validated copy), or None when it is not one."""
    try:
        return JSON_OBJECT.validate_python(value, strict=True)
    except ValidationError:
        return None


def as_list(value: object) -> JsonList | None:
    """`value` as a JSON array (a validated copy), or None when it is not one."""
    try:
        return JSON_LIST.validate_python(value, strict=True)
    except ValidationError:
        return None


def as_str(value: object) -> str | None:
    """`value` when it is a `str`, else None."""
    return value if isinstance(value, str) else None


def as_number(value: object) -> int | float | None:
    """`value` when it is an `int` or `float` (not a `bool`), else None."""
    if isinstance(value, bool):
        return None
    return value if isinstance(value, (int, float)) else None


def closed_schema(schema: JsonObject) -> JsonObject:
    """A copy of a JSON schema in which every object schema without `additionalProperties` gets
    `additionalProperties: false`: Anthropic's structured outputs reject an object schema without
    it, and OpenAI's strict mode requires it too. An explicit `additionalProperties` is kept."""
    out: JsonObject = {key: _closed(value) for key, value in schema.items()}
    if out.get("type") == "object" and "additionalProperties" not in out:
        out["additionalProperties"] = False
    return out


def _closed(value: JsonValue) -> JsonValue:
    if isinstance(value, dict):
        return closed_schema(value)
    if isinstance(value, list):
        return [_closed(item) for item in value]
    return value
