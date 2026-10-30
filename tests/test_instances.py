from __future__ import annotations

from typing import Any

import pytest

from mcpprobe.protocol.instances import UNSUPPORTED, is_valid, minimal_instance


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {"message": {"type": "string"}}, "required": ["message"]},
        {"type": "object", "additionalProperties": False},
        {
            "type": "object",
            "properties": {"n": {"type": "integer", "minimum": 5, "maximum": 9}},
            "required": ["n"],
        },
        {
            "type": "object",
            "properties": {"x": {"type": "number", "exclusiveMinimum": 0, "multipleOf": 0.5}},
            "required": ["x"],
        },
        {
            "type": "object",
            "properties": {
                "tags": {
                    "type": "array",
                    "minItems": 2,
                    "items": {"type": "string", "minLength": 3},
                }
            },
            "required": ["tags"],
        },
        {
            "type": "object",
            "properties": {"url": {"type": "string", "format": "uri"}},
            "required": ["url"],
        },
        {
            "type": "object",
            "properties": {"mode": {"enum": ["fast", "slow"]}},
            "required": ["mode"],
        },
        {
            "type": "object",
            "properties": {"v": {"anyOf": [{"type": "null"}, {"type": "boolean"}]}},
            "required": ["v"],
        },
        {"type": ["string", "null"]},
        {"properties": {"a": {"const": 3}}, "required": ["a"]},
        {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "type": "object",
            "properties": {"d": {"type": "string", "default": "x"}},
            "required": ["d"],
        },
        {
            "type": "object",
            "properties": {
                "n": {"type": "number"},
                "b": {"type": "boolean"},
                "z": {"type": "null"},
            },
            "required": ["n", "b", "z"],
        },
        True,
    ],
)
def test_builds_valid_instances(schema: Any) -> None:
    value = minimal_instance(schema)
    assert value is not UNSUPPORTED
    if isinstance(schema, dict):
        assert is_valid(schema, value)


def test_addresses_are_reserved_names() -> None:
    value = minimal_instance(
        {
            "type": "object",
            "properties": {"u": {"format": "uri", "type": "string"}},
            "required": ["u"],
        }
    )
    assert ".invalid" in value["u"]


@pytest.mark.parametrize(
    "schema",
    [
        {"$ref": "#/$defs/x"},
        {
            "type": "object",
            "properties": {"p": {"$ref": "https://example.com/s.json"}},
            "required": ["p"],
        },
        {"type": "string", "pattern": "^[0-9]{4}$"},
        {"type": "array", "minItems": 1000},
        {"type": "mystery"},
        {"anyOf": [{"$ref": "#/a"}]},
        "not a schema",
        {"type": "integer", "minimum": 1, "maximum": 0},
        {"type": "string", "minLength": 99999},
    ],
)
def test_unsupported_or_invalid_is_rejected(schema: Any) -> None:
    assert minimal_instance(schema) is UNSUPPORTED


def test_is_valid_rejects_broken_schema() -> None:
    assert not is_valid({"type": 5}, "x")
