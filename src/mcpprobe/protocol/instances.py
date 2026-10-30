"""Build one small, valid instance of a JSON Schema, deterministically.

Used to call a tool with arguments it should accept, and to fill an
elicitation form. This is not the fuzzer (that is M3, with Hypothesis); it
picks the most boring valid value: ``const``, then ``default``, then the first
``examples`` or ``enum`` entry, then a minimal value for the type.

Every candidate is checked against the schema afterwards. Anything that
cannot be built, or does not validate, returns ``UNSUPPORTED`` so the caller
skips it instead of sending a guess. ``$ref`` is never followed.

Strings that look like addresses point at reserved ``.invalid`` names, so a
tool that dereferences them fails harmlessly instead of reaching a real host.
"""

from __future__ import annotations

from typing import Any, Final

from jsonschema import Draft202012Validator, validators
from jsonschema.exceptions import SchemaError

UNSUPPORTED: Final = object()
MAX_DEPTH = 8

_FORMATS: dict[str, str] = {
    "uri": "https://mcpprobe.invalid/",
    "url": "https://mcpprobe.invalid/",
    "uri-reference": "https://mcpprobe.invalid/",
    "iri": "https://mcpprobe.invalid/",
    "email": "mcpprobe@mcpprobe.invalid",
    "hostname": "mcpprobe.invalid",
    "date": "2026-01-01",
    "date-time": "2026-01-01T00:00:00Z",
    "time": "00:00:00Z",
    "uuid": "00000000-0000-4000-8000-000000000000",
    "ipv4": "192.0.2.1",
    "ipv6": "2001:db8::1",
}


def validator_for(schema: dict[str, Any]) -> Any:
    """A validator for the schema's declared dialect (2020-12 by default).

    Raises ``SchemaError`` if the schema itself is invalid for its dialect.
    """
    cls = validators.validator_for(schema, default=Draft202012Validator)
    cls.check_schema(schema)
    return cls(schema)


def is_valid(schema: dict[str, Any], instance: Any) -> bool:
    try:
        return bool(validator_for(schema).is_valid(instance))
    except SchemaError:
        return False


def _number(schema: dict[str, Any], integer: bool) -> Any:
    low = schema.get("minimum")
    if isinstance(schema.get("exclusiveMinimum"), (int, float)):
        low = schema["exclusiveMinimum"] + 1
    high = schema.get("maximum")
    if isinstance(schema.get("exclusiveMaximum"), (int, float)):
        high = schema["exclusiveMaximum"] - 1
    value: Any = low if isinstance(low, (int, float)) else 1
    if isinstance(high, (int, float)) and value > high:
        value = high
    if integer:
        value = int(value) if float(value).is_integer() else int(value) + 1
    multiple = schema.get("multipleOf")
    if isinstance(multiple, (int, float)) and multiple > 0 and value % multiple:
        value = (value // multiple + 1) * multiple
    return value


def _build(schema: Any, depth: int) -> Any:
    if schema is True or schema == {}:
        return {}
    if not isinstance(schema, dict) or depth > MAX_DEPTH or "$ref" in schema:
        return UNSUPPORTED
    for key in ("const", "default"):
        if key in schema:
            return schema[key]
    for key in ("examples", "enum"):
        values = schema.get(key)
        if isinstance(values, list) and values:
            return values[0]
    for key in ("anyOf", "oneOf"):
        options = schema.get(key)
        if isinstance(options, list):
            for option in options:
                value = _build(option, depth + 1)
                if value is not UNSUPPORTED:
                    return value
            return UNSUPPORTED

    kind = schema.get("type")
    if isinstance(kind, list):
        kind = next((k for k in kind if k != "null"), kind[0] if kind else None)
    if kind is None and isinstance(schema.get("properties"), dict):
        kind = "object"
    if kind == "object":
        out: dict[str, Any] = {}
        raw_properties = schema.get("properties")
        properties: dict[str, Any] = raw_properties if isinstance(raw_properties, dict) else {}
        for name in schema.get("required") or []:
            value = _build(properties.get(name, {}), depth + 1)
            if value is UNSUPPORTED:
                return UNSUPPORTED
            out[name] = value
        return out
    if kind == "array":
        count = schema.get("minItems", 0)
        if not isinstance(count, int) or count > 32:
            return UNSUPPORTED
        item = _build(schema.get("items", {}), depth + 1) if count else None
        return [item] * count if item is not UNSUPPORTED else UNSUPPORTED
    if kind == "string":
        if isinstance(schema.get("format"), str) and schema["format"] in _FORMATS:
            return _FORMATS[schema["format"]]
        length = schema.get("minLength", 1)
        return "a" * max(length, 1) if isinstance(length, int) and length <= 1024 else UNSUPPORTED
    if kind in ("integer", "number"):
        return _number(schema, integer=kind == "integer")
    if kind == "boolean":
        return False
    if kind == "null":
        return None
    return UNSUPPORTED


def minimal_instance(schema: Any) -> Any:
    """A valid instance of ``schema``, or ``UNSUPPORTED``."""
    value = _build(schema, 0)
    if value is UNSUPPORTED:
        return UNSUPPORTED
    if isinstance(schema, dict) and not is_valid(schema, value):
        return UNSUPPORTED
    return value
