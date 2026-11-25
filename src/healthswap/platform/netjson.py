"""System.Text.Json output text, for the places where the C# implementation hashes, measures or stores the exact JSON it wrote.

* `to_json_string(value)`: JsonNode.ToJsonString() and JsonSerializer.Serialize(value) with default options. Compact, nulls
  written, JavaScriptEncoder.Default escaping: everything outside printable ASCII and the HTML-sensitive characters
  " & ' + < > ` are written as \\uXXXX (uppercase hex, UTF-16 units); \\n \\r \\t \\b \\f and \\\\ use short escapes.
* `serialize_hs(value)`: JsonSerializer.Serialize(value, HsJson.Options) (also RefJson.Options). Compact, members whose
  value is None are omitted (JsonIgnoreCondition.WhenWritingNull; the dicts stand for C# objects), and
  UnsafeRelaxedJsonEscaping: non-ASCII text stays readable, \\" and \\\\ are escaped, control characters, U+007F-U+009F,
  U+2028/U+2029, lone surrogates and characters outside the BMP are escaped.
* `serialize_web(value)`: the MCP SDK serializer (C# tool results): None members omitted, JavaScriptEncoder.Default escaping.

Values are JSON-shaped Python values: dict (string keys, insertion order = C# property order), list or tuple, str, bool,
None, int, Decimal (a C# decimal, written with its scale: 0.70 stays 0.70), float (a C# double: shortest round-trip
text, integral values without ".0", exponents written as E+XX / E-XX like .NET), Single (a C# float: the shortest text
that round-trips through float32) and jsonutil.JsonElement (a C# JsonElement: written as is, nulls included).
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from decimal import Decimal
from typing import Any

import numpy as np

from healthswap.platform.jsonutil import JsonElement

_DEFAULT_UNSAFE = frozenset('"&\'+<>\\`')
_SHORT = {"\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b", "\f": "\\f", "\\": "\\\\"}


def _units(ch: str) -> str:
    """\\uXXXX escapes of one character (a surrogate pair for characters outside the BMP)."""
    o = ord(ch)
    if o <= 0xFFFF:
        return f"\\u{o:04X}"
    o -= 0x10000
    return f"\\u{0xD800 + (o >> 10):04X}\\u{0xDC00 + (o & 0x3FF):04X}"


def _escape_default(s: str) -> str:
    if s.isascii() and s.isprintable() and not any(c in _DEFAULT_UNSAFE for c in s):
        return s
    out: list[str] = []
    for ch in s:
        if " " <= ch <= "~" and ch not in _DEFAULT_UNSAFE:
            out.append(ch)
        else:
            out.append(_SHORT.get(ch) or _units(ch))
    return "".join(out)


def _escape_relaxed(s: str) -> str:
    if s.isascii() and s.isprintable() and '"' not in s and "\\" not in s:
        return s
    out: list[str] = []
    for ch in s:
        o = ord(ch)
        if ch == '"':
            out.append('\\"')
        elif ch in _SHORT:
            out.append(_SHORT[ch])
        elif o < 0x20 or 0x7F <= o <= 0x9F or o in (0x2028, 0x2029) or 0xD800 <= o <= 0xDFFF or o > 0xFFFF:
            out.append(_units(ch))
        else:
            out.append(ch)
    return "".join(out)


def decimal_text(d: Decimal) -> str:
    """A C# decimal as System.Text.Json writes it (invariant, scale kept, never "-0", never an exponent)."""
    if not d.is_finite():
        raise ValueError(f"Not a finite decimal: {d}")
    return format(abs(d) if d.is_zero() else d, "f")


class Single(float):
    """A C# float (float32) value, written by `single_text`; build it from a value that is already float32-rounded."""

    __slots__ = ()


def double_text(v: float) -> str:
    """A C# double as System.Text.Json (and double.ToString()) writes it: shortest round-trip text, .NET exponent style."""
    if not math.isfinite(v):
        raise ValueError(".NET number values such as positive and negative infinity cannot be written as valid JSON.")
    if v == 0:
        return "-0" if math.copysign(1.0, v) < 0 else "0"
    return _dotnet_shortest(repr(v), v < 0, 15)


def single_text(v: float) -> str:
    """A C# float as System.Text.Json (and float.ToString()) writes it: the shortest text that round-trips through float32,
    positional for decimal exponents -4..8 (.NET switches to E notation past max(digits, 9) integer digits)."""
    if not math.isfinite(v):
        raise ValueError(".NET number values such as positive and negative infinity cannot be written as valid JSON.")
    if v == 0:
        return "-0" if math.copysign(1.0, v) < 0 else "0"
    return _dotnet_shortest(np.format_float_scientific(np.float32(v), unique=True, trim="-"), v < 0, 9)


def _dotnet_shortest(r: str, negative: bool, positional_below: int) -> str:
    """Rewrites Python/numpy shortest round-trip text (r) in the .NET "R"/"G" layout: positional when the decimal exponent
    of the first significant digit is in (-5, positional_below), otherwise d.dddE+XX / E-XX."""
    mantissa, _, exponent = r.partition("e")
    if exponent:
        exp = int(exponent)
    else:
        exp = 0
    digits = mantissa.lstrip("-").replace(".", "")
    point = len(mantissa.lstrip("-").split(".")[0])
    stripped = digits.lstrip("0")
    leading = len(digits) - len(stripped)
    digits = stripped.rstrip("0") or "0"
    exp10 = exp + point - leading - 1  # exponent of the first significant digit
    sign = "-" if negative else ""
    if -5 < exp10 < positional_below:  # positional notation
        if exp10 >= 0:
            whole = digits[: exp10 + 1].ljust(exp10 + 1, "0")
            frac = digits[exp10 + 1 :]
        else:
            whole = "0"
            frac = "0" * (-exp10 - 1) + digits
        return sign + whole + ("." + frac if frac else "")
    tail = "." + digits[1:] if len(digits) > 1 else ""
    return f"{sign}{digits[0]}{tail}E{'-' if exp10 < 0 else '+'}{abs(exp10):02d}"


def _number(value: int | float | Decimal) -> str:
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, Single):
        return single_text(value)
    if isinstance(value, float):
        return double_text(value)
    return str(value)


def _write(value: Any, out: list[str], escape: Callable[[str], str], drop_none: bool) -> None:
    if value is None:
        out.append("null")
    elif value is True:
        out.append("true")
    elif value is False:
        out.append("false")
    elif isinstance(value, str):
        out.append('"' + escape(value) + '"')
    elif isinstance(value, (int, float, Decimal)):
        out.append(_number(value))
    elif isinstance(value, JsonElement):
        _write(value.value, out, escape, False)
    elif isinstance(value, Mapping):
        out.append("{")
        first = True
        for key, item in value.items():
            if drop_none and item is None:
                continue
            if not first:
                out.append(",")
            first = False
            out.append('"' + escape(str(key)) + '":')
            _write(item, out, escape, drop_none)
        out.append("}")
    elif isinstance(value, (list, tuple)):
        out.append("[")
        for i, item in enumerate(value):
            if i:
                out.append(",")
            _write(item, out, escape, drop_none)
        out.append("]")
    else:
        raise TypeError(f"Object of type {type(value).__name__} is not JSON serializable")


def to_json_string(value: Any) -> str:
    """JsonNode.ToJsonString() / JsonSerializer.Serialize(value) with default JsonSerializerOptions."""
    out: list[str] = []
    _write(value, out, _escape_default, False)
    return "".join(out)


def serialize_hs(value: Any) -> str:
    """JsonSerializer.Serialize(value, HsJson.Options): None members of objects omitted, relaxed escaping."""
    out: list[str] = []
    _write(value, out, _escape_relaxed, True)
    return "".join(out)


def serialize_web(value: Any) -> str:
    """JsonSerializer.Serialize(value) with the MCP SDK options (web defaults, WhenWritingNull): None members of objects
    omitted, JavaScriptEncoder.Default escaping (the text content of C# MCP tool results)."""
    out: list[str] = []
    _write(value, out, _escape_default, True)
    return "".join(out)
