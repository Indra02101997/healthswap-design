"""OpenApiContract.cs + RequestValidationMiddleware ported: api/openapi.yaml compiled into JSON Schema 2020-12 validators.

The YAML is composed into nodes and converted with the C# scalar rules (YamlDotNet RepresentationModel + `Scalar`), not with a
YAML 1.1 resolver: quoted, literal and folded scalars stay strings, plain `null` / `~` / empty is null, plain `true` /
`false` are booleans, plain integers (Int64) and plain decimals are numbers (Decimal keeps the scale) and everything else -
`yes`, `no`, `2026-11-01`, `1.5e3` - stays a string. Mapping keys are always strings (`200:` is "200").

Each operation's request and response schemas are wrapped like C# `Build`:
`{"$schema": 2020-12, "$id": "https://healthswap.example/contract/req/<operationId>.json", "$defs": components.schemas,
"allOf": [schema]}` with `#/components/schemas/X` references rewritten to `#/$defs/X`.

Validation uses jsonschema's Draft202012Validator with format assertion (date-time, date, uuid, uri, email) and a type
checker that accepts Decimal numbers (request bodies are parsed with Decimal floats). Errors become FieldError(field =
JSON pointer of the instance location or "/", message, code = keyword); anyOf/oneOf branch errors are flattened to their
leaf errors (the C# list output also reports applicator errors such as "allOf"/"properties", which are left out here),
duplicates (field, message) are dropped and the list is capped at 50 like C#. Messages are jsonschema's wording.
"""

from __future__ import annotations

import copy
import json
import re
import threading
from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import ValidationError
from jsonschema.validators import extend

from healthswap.platform.errors import FieldError

DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCHEMA_ID_BASE = "https://healthswap.example/contract/"
MAX_ERRORS = 50
# System.Text.Json JsonDocument default maximum depth.
MAX_JSON_DEPTH = 64

_METHODS = ("get", "post", "put", "patch", "delete")
_QUOTED_STYLES = ("'", '"', "|", ">")
_INT_RX = re.compile(r"[+-]?[0-9]+")
_DECIMAL_RX = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)")
_INT64 = (-(2**63), 2**63 - 1)
_DECIMAL_MAX = Decimal("79228162514264337593543950335")
_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)


# ---------------------------------------------------------------------------------------------------------------------
# YAML -> JSON (C# ToJson / Scalar)
# ---------------------------------------------------------------------------------------------------------------------


def _scalar(node: yaml.ScalarNode) -> Any:
    v = node.value or ""
    if node.style in _QUOTED_STYLES:
        return v
    if v in ("null", "~", ""):
        return None
    if v == "true":
        return True
    if v == "false":
        return False
    if _INT_RX.fullmatch(v):  # long.TryParse(AllowLeadingSign)
        n = int(v)
        if _INT64[0] <= n <= _INT64[1]:
            return n
    if _DECIMAL_RX.fullmatch(v):  # decimal.TryParse(AllowLeadingSign | AllowDecimalPoint)
        try:
            d = Decimal(v)
        except InvalidOperation:  # pragma: no cover - the pattern only admits valid literals
            return v
        if d.copy_abs() <= _DECIMAL_MAX:
            return d
    return v


def _to_json(node: yaml.Node | None) -> Any:
    if isinstance(node, yaml.MappingNode):
        return {str(k.value) if isinstance(k, yaml.ScalarNode) else "": _to_json(v) for k, v in node.value}
    if isinstance(node, yaml.SequenceNode):
        return [_to_json(n) for n in node.value]
    if isinstance(node, yaml.ScalarNode):
        return _scalar(node)
    return None


def load_yaml(text: str) -> Any:
    """The YAML document converted with the C# OpenApiContract scalar rules (see the module docstring)."""
    return _to_json(yaml.compose(text, Loader=_LOADER))


def _rewrite(node: Any) -> Any:
    """OpenAPI component references become local JSON Schema $defs references (in place)."""
    if isinstance(node, dict):
        for key in list(node):
            value = node[key]
            if key == "$ref" and isinstance(value, str) and value.startswith("#/components/schemas/"):
                node[key] = "#/$defs/" + value[len("#/components/schemas/"):]
            elif value is not None:
                _rewrite(value)
    elif isinstance(node, list):
        for child in node:
            if child is not None:
                _rewrite(child)
    return node


# ---------------------------------------------------------------------------------------------------------------------
# Validator: Decimal-aware types and the formats the contract uses
# ---------------------------------------------------------------------------------------------------------------------


def _is_integer(checker: Any, instance: Any) -> bool:
    if isinstance(instance, bool):
        return False
    if isinstance(instance, int):
        return True
    if isinstance(instance, float):
        return instance.is_integer()
    if isinstance(instance, Decimal):
        return instance.is_finite() and instance == instance.to_integral_value()
    return False


def _is_number(checker: Any, instance: Any) -> bool:
    return not isinstance(instance, bool) and isinstance(instance, (int, float, Decimal))


_TYPE_CHECKER = Draft202012Validator.TYPE_CHECKER.redefine_many({"integer": _is_integer, "number": _is_number})
ContractValidator = extend(Draft202012Validator, type_checker=_TYPE_CHECKER)

FORMATS = FormatChecker(formats=())
_DATE_TIME_RX = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})[Tt]([0-9]{2}):([0-9]{2}):([0-9]{2})(\.[0-9]+)?(?:[Zz]|[+-]([0-9]{2}):([0-9]{2}))")
_DATE_RX = re.compile(r"([0-9]{4})-([0-9]{2})-([0-9]{2})")
_UUID_RX = re.compile(r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}")
_URI_RX = re.compile(r"[A-Za-z][A-Za-z0-9+.\-]*:[^\s\x00-\x1f\x7f]+")
_EMAIL_RX = re.compile(r"[^@\s]+@[^@\s]+")


def _valid_date(y: str, m: str, d: str) -> bool:
    try:
        date(int(y), int(m), int(d))
    except ValueError:
        return False
    return True


@FORMATS.checks("date-time")
def _date_time(value: object) -> bool:
    """RFC 3339 date-time with a time zone (leap second 60 allowed, any fraction length)."""
    if not isinstance(value, str):
        return True
    m = _DATE_TIME_RX.fullmatch(value)
    if m is None or not _valid_date(*m.group(1, 2, 3)):
        return False
    hour, minute, second = int(m.group(4)), int(m.group(5)), int(m.group(6))
    if hour > 23 or minute > 59 or second > 60:
        return False
    return m.group(8) is None or (int(m.group(8)) <= 23 and int(m.group(9)) <= 59)


@FORMATS.checks("date")
def _date(value: object) -> bool:
    if not isinstance(value, str):
        return True
    m = _DATE_RX.fullmatch(value)
    return m is not None and _valid_date(*m.groups())


@FORMATS.checks("uuid")
def _uuid(value: object) -> bool:
    return not isinstance(value, str) or _UUID_RX.fullmatch(value) is not None


@FORMATS.checks("uri")
def _uri(value: object) -> bool:
    """An absolute URI: scheme ":" followed by characters without whitespace or controls."""
    return not isinstance(value, str) or _URI_RX.fullmatch(value) is not None


@FORMATS.checks("email")
def _email(value: object) -> bool:
    return not isinstance(value, str) or _EMAIL_RX.fullmatch(value) is not None


def _pointer(path: Iterable[Any]) -> str:
    parts = [str(p).replace("~", "~0").replace("/", "~1") for p in path]
    return "/" + "/".join(parts) if parts else "/"


_DECIMAL_REPR_RX = re.compile(r"Decimal\('([^']*)'\)")


def _message(error: ValidationError) -> str:
    return _DECIMAL_REPR_RX.sub(r"\1", error.message)


def _extra_properties(instance: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    properties = schema.get("properties") or {}
    patterns = [re.compile(p) for p in (schema.get("patternProperties") or {})]
    return [name for name in instance if name not in properties and not any(p.search(name) for p in patterns)]


def _collect(error: ValidationError, out: list[FieldError]) -> None:
    if error.context:  # anyOf / oneOf: report the failing branches' leaf errors
        for child in error.context:
            _collect(child, out)
        return
    if error.validator == "additionalProperties" and error.validator_value is False and isinstance(error.instance, dict) \
            and isinstance(error.schema, Mapping):
        # C# reports each additional property at its own location (the false schema fails there).
        for name in _extra_properties(error.instance, error.schema):
            out.append(FieldError(_pointer([*error.absolute_path, name]), f"Additional property {name!r} is not allowed", "additionalProperties"))
        return
    out.append(FieldError(_pointer(error.absolute_path), _message(error), str(error.validator)))


# ---------------------------------------------------------------------------------------------------------------------
# Contract
# ---------------------------------------------------------------------------------------------------------------------


class OpenApiContract:
    """The OpenAPI 3.1 contract with JSON Schema validators per operation (request bodies and responses)."""

    def __init__(self, yaml_text: str) -> None:
        self.yaml = yaml_text
        self.document: dict[str, Any] = load_yaml(yaml_text)
        components = self.document.get("components") or {}
        self._defs: dict[str, Any] = _rewrite(copy.deepcopy(components.get("schemas") or {}))
        responses: dict[str, Any] = components.get("responses") or {}
        self.operations: list[str] = []
        # operationId -> (HTTP method, path as written in the contract, relative to the /v1 server URL)
        self.routes: dict[str, tuple[str, str]] = {}
        self._requests: dict[str, dict[str, Any]] = {}
        self._responses: dict[tuple[str, str], dict[str, Any]] = {}
        self._validators: dict[int, Any] = {}
        self._lock = threading.Lock()
        for path, item in (self.document.get("paths") or {}).items():
            for method, op in (item or {}).items() if isinstance(item, dict) else ():
                if method not in _METHODS or not isinstance(op, dict) or not isinstance(op.get("operationId"), str):
                    continue
                op_id: str = op["operationId"]
                self.operations.append(op_id)
                self.routes[op_id] = (method.upper(), path)
                body = _get(op, "requestBody", "content", "application/json", "schema")
                if body is not None:
                    self._requests[op_id] = self._build(f"req/{op_id}", body)
                for status, response in (op.get("responses") or {}).items():
                    r = response
                    if isinstance(response, dict) and isinstance(response.get("$ref"), str):
                        r = responses.get(response["$ref"].split("/")[-1])
                    schema = _get(r, "content", "application/json", "schema")
                    if schema is None:
                        schema = _get(r, "content", "application/problem+json", "schema")
                    if schema is not None:
                        self._responses[(op_id, str(status))] = self._build(f"res/{op_id}/{status}", schema)

    @classmethod
    def load(cls, path: Path | str) -> OpenApiContract:
        """The contract at `path` (cached per path: compiling it takes a few tenths of a second)."""
        return _load(str(Path(path).resolve()))

    def _build(self, schema_id: str, schema: Any) -> dict[str, Any]:
        return {
            "$schema": DIALECT, "$id": f"{SCHEMA_ID_BASE}{schema_id}.json", "$defs": self._defs,
            "allOf": [_rewrite(copy.deepcopy(schema))],
        }

    def request_schema(self, operation_id: str) -> dict[str, Any] | None:
        return self._requests.get(operation_id)

    def response_schema(self, operation_id: str, status: int | str) -> dict[str, Any] | None:
        return self._responses.get((operation_id, str(status))) or self._responses.get((operation_id, "default"))

    def _validator(self, schema: dict[str, Any]) -> Any:
        key = id(schema)
        entry = self._validators.get(key)
        if entry is None or entry[0] is not schema:
            with self._lock:
                entry = self._validators.get(key)
                if entry is None or entry[0] is not schema:
                    entry = (schema, ContractValidator(schema, format_checker=FORMATS))
                    self._validators[key] = entry
        return entry[1]

    def validate(self, schema: dict[str, Any], instance: Any) -> list[FieldError]:
        """FieldErrors of `instance` against one of this contract's schemas (empty when valid)."""
        errors: list[FieldError] = []
        for error in self._validator(schema).iter_errors(instance):
            _collect(error, errors)
        seen: set[tuple[str, str]] = set()
        unique: list[FieldError] = []
        for e in errors:
            if (e.field, e.message) not in seen:
                seen.add((e.field, e.message))
                unique.append(e)
        return unique[:MAX_ERRORS]

    def validate_request(self, operation_id: str, instance: Any) -> list[FieldError]:
        schema = self.request_schema(operation_id)
        return [] if schema is None else self.validate(schema, instance)

    def validate_response(self, operation_id: str, status: int, instance: Any) -> list[FieldError]:
        schema = self.response_schema(operation_id, status)
        return [] if schema is None else self.validate(schema, instance)


def _get(node: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


@lru_cache(maxsize=8)
def _load(path: str) -> OpenApiContract:
    return OpenApiContract(Path(path).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueError(f"'{name}' is an invalid JSON literal")


def parse_json(raw: bytes | str) -> Any:
    """Strict JSON (System.Text.Json JsonDocument rules): UTF-8, no NaN/Infinity literals, depth at most 64.

    Floats are parsed as Decimal. Raises ValueError with a readable message for malformed input.
    """
    text = raw.decode("utf-8") if isinstance(raw, (bytes, bytearray)) else raw
    if text.startswith("\ufeff"):
        text = text[1:]
    try:
        value = json.loads(text, parse_float=Decimal, parse_constant=_reject_constant)
    except RecursionError:
        raise ValueError(f"The maximum configured depth of {MAX_JSON_DEPTH} has been exceeded") from None
    if _depth(value) > MAX_JSON_DEPTH:
        raise ValueError(f"The maximum configured depth of {MAX_JSON_DEPTH} has been exceeded")
    return value


def _depth(value: Any) -> int:
    deepest = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        node, depth = stack.pop()
        if isinstance(node, dict):
            depth += 1
            stack.extend((v, depth) for v in node.values())
        elif isinstance(node, list):
            depth += 1
            stack.extend((v, depth) for v in node)
        deepest = max(deepest, depth)
    return deepest
