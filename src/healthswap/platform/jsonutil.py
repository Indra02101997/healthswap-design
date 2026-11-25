"""JSON helpers mirroring the C# HsJson options: camelCase API dicts, nulls omitted, readable UTF-8 (Devanagari stays as is).

* `dumps(obj)` / `dumpb(obj)` encode Decimal (as JSON number), UUID, datetime (ISO 8601 with Z), date, Enum and dataclasses
  (field names converted to camelCase). None values inside dicts are dropped (C# JsonIgnoreCondition.WhenWritingNull).
* `loads(text)` parses floats as Decimal so money and scores keep their exact decimal scale.
* `to_plain(obj)` returns the JSON-compatible Python structure without serializing (used by the API layer);
  `keep_decimal=True` keeps Decimal (and JsonElement) values for `netjson.serialize_hs`, which writes the C# scale.
* `JsonElement(value)` wraps an already parsed JSON document that must be written as is (C# JsonElement): its nulls are
  kept even where None members are otherwise dropped.
* `encode_dataclass` / `decode_dataclass` round-trip internal records (snake_case keys) for the cache.
* Dataclass fields declared with `field(metadata={"json_ignore": True})` are never written or read (C# [JsonIgnore]).
"""

from __future__ import annotations

import collections.abc
import dataclasses
import json
import types
import typing
import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from functools import lru_cache
from typing import Any

from healthswap.domain.primitives import iso, parse_iso


def camel(name: str) -> str:
    head, *rest = name.rstrip("_").split("_")
    return head + "".join(p[:1].upper() + p[1:] for p in rest)


def _decimal_number(d: Decimal) -> int | float:
    return int(d) if d == d.to_integral_value() and d.as_tuple().exponent >= 0 else float(d)


class JsonElement:
    """An already parsed JSON document written as is (C# JsonElement, for example a jsonb column or a webhook body).

    `to_plain(..., keep_decimal=True)` passes it through and `netjson.serialize_hs` writes it with its nulls; plain
    `to_plain` converts it with nulls kept.
    """

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    @classmethod
    def parse(cls, text: str | bytes) -> JsonElement:
        """Parses JSON text (floats as Decimal, so numbers keep their scale)."""
        return cls(loads(text))

    def __eq__(self, other: object) -> bool:
        return isinstance(other, JsonElement) and other.value == self.value

    def __hash__(self) -> int:
        return hash(repr(self.value))

    def __repr__(self) -> str:
        return f"JsonElement({self.value!r})"


def to_plain(obj: Any, *, drop_none: bool = True, camel_case: bool = True, keep_decimal: bool = False) -> Any:
    """Converts to JSON-compatible values. Dict keys are kept as given (API dicts are already camelCase).

    `keep_decimal=True` keeps Decimal and JsonElement values (for `netjson.serialize_hs`, which writes the C# decimal scale).
    """
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj.value if isinstance(obj, Enum) else obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return obj if keep_decimal else _decimal_number(obj)
    if isinstance(obj, JsonElement):
        return obj if keep_decimal else to_plain(obj.value, drop_none=False, camel_case=camel_case)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return iso(obj)
    if isinstance(obj, timedelta):
        return obj.total_seconds()
    if isinstance(obj, dict):
        return {str(k.value if isinstance(k, Enum) else k): to_plain(v, drop_none=drop_none, camel_case=camel_case, keep_decimal=keep_decimal)
                for k, v in obj.items() if not (drop_none and v is None)}
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        if hasattr(obj, "to_api"):
            return to_plain(obj.to_api(), drop_none=drop_none, camel_case=camel_case, keep_decimal=keep_decimal)
        out = {}
        for f in dataclasses.fields(obj):
            v = getattr(obj, f.name)
            if (drop_none and v is None) or f.metadata.get("json_ignore"):
                continue
            out[camel(f.name) if camel_case else f.name] = to_plain(v, drop_none=drop_none, camel_case=camel_case, keep_decimal=keep_decimal)
        return out
    if isinstance(obj, (list, tuple, set, frozenset)) or hasattr(obj, "__iter__") and not isinstance(obj, (bytes, bytearray)):
        items = list(obj)
        if isinstance(obj, (set, frozenset)):
            items = sorted(items, key=str)
        return [to_plain(v, drop_none=drop_none, camel_case=camel_case, keep_decimal=keep_decimal) for v in items]
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def dumps(obj: Any, *, drop_none: bool = True, indent: int | None = None) -> str:
    return json.dumps(to_plain(obj, drop_none=drop_none), ensure_ascii=False, separators=(",", ":") if indent is None else None, indent=indent)


def dumpb(obj: Any, *, drop_none: bool = True) -> bytes:
    return dumps(obj, drop_none=drop_none).encode("utf-8")


def loads(text: str | bytes) -> Any:
    return json.loads(text, parse_float=Decimal)


# ---------------------------------------------------------------------------------------------------------------------
# Dataclass codec for internal records (cache payloads). Keys stay snake_case; Decimals become strings to keep scale.
# ---------------------------------------------------------------------------------------------------------------------


def encode_dataclass(obj: Any) -> Any:
    if obj is None or isinstance(obj, (bool, str)):
        return obj
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (int, float)):
        return obj
    if isinstance(obj, Decimal):
        return str(obj)
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, (datetime, date)):
        return iso(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: encode_dataclass(getattr(obj, f.name)) for f in dataclasses.fields(obj) if not f.metadata.get("json_ignore")}
    if isinstance(obj, dict):
        return {str(k.value if isinstance(k, Enum) else k): encode_dataclass(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)) or hasattr(obj, "__iter__") and not isinstance(obj, (bytes, bytearray)):
        return [encode_dataclass(v) for v in obj]
    raise TypeError(f"Cannot encode {type(obj).__name__}")


@lru_cache(maxsize=256)
def _hints(cls: type) -> dict[str, Any]:
    return typing.get_type_hints(cls)


def _decode_value(tp: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in (typing.Union, types.UnionType):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _decode_value(non_none[0], value)
        for candidate in non_none:  # best effort for real unions
            try:
                return _decode_value(candidate, value)
            except (TypeError, ValueError, KeyError):
                continue
        return value
    if tp is Any:
        return value
    if origin in (list, typing.List):  # noqa: UP006
        return [_decode_value(args[0] if args else Any, v) for v in value]
    if origin in (tuple, typing.Tuple):  # noqa: UP006
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_decode_value(args[0], v) for v in value)
        if args:
            return tuple(_decode_value(a, v) for a, v in zip(args, value, strict=False))
        return tuple(value)
    if origin in (set, frozenset, typing.Set, typing.FrozenSet):  # noqa: UP006
        inner = args[0] if args else Any
        return (frozenset if origin in (frozenset, typing.FrozenSet) else set)(_decode_value(inner, v) for v in value)  # noqa: UP006
    if origin in (dict, typing.Dict, typing.Mapping, collections.abc.Mapping, collections.abc.MutableMapping):  # noqa: UP006
        kt, vt = (args + (Any, Any))[:2] if args else (Any, Any)
        return {_decode_value(kt, k): _decode_value(vt, v) for k, v in value.items()}
    if isinstance(tp, type):
        if dataclasses.is_dataclass(tp):
            return decode_dataclass(tp, value)
        if issubclass(tp, Enum):
            return tp(value)
        if tp is Decimal:
            return Decimal(str(value))
        if tp is uuid.UUID:
            return uuid.UUID(str(value))
        if tp is datetime:
            return parse_iso(value)
        if tp is date:
            return date.fromisoformat(value)
        if tp is float:
            return float(value)
        if tp is int:
            return int(value)
        if tp in (str, bool):
            return value
        # Custom collection types (for example OrderedFrozenSet) accept an iterable.
        if isinstance(value, list):
            try:
                return tp(value)
            except TypeError:
                return value
    return value


def decode_dataclass(cls: type, data: dict[str, Any]) -> Any:
    hints = _hints(cls)
    kwargs = {}
    for f in dataclasses.fields(cls):
        if not f.init or f.name not in data or f.metadata.get("json_ignore"):
            continue
        kwargs[f.name] = _decode_value(hints.get(f.name, Any), data[f.name])
    return cls(**kwargs)
