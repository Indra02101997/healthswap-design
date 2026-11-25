"""Text similarity and the deterministic local embedder (ported from Text.cs).

C# `float` (float32) results are reproduced bit-for-bit: values are rounded to float32 where C# stores or computes a
`float`, products inside Cosine are float32 multiplications, and sums are plain left-to-right double additions (Python
3.12+ `sum()` of floats is compensated, so it is not used for them). Trigrams slice UTF-16 code units like C#
`Substring`, and feature hashing uses UTF-8 with lone surrogates replaced by U+FFFD (Encoding.UTF8). The helpers
`to_float32`, `round_double`, `double_to_decimal` and `decimal_to_double` reproduce the C# numeric conversions for other
modules that port float/double code.
"""

from __future__ import annotations

import math
import re
from array import array
from collections.abc import Iterable, Sequence
from collections.abc import Set as AbstractSet
from decimal import Decimal
from typing import TypeVar

from healthswap.domain.knowledge.parsing import IngredientTaxonomy

T = TypeVar("T")

_LONE_SURROGATE_RX = re.compile("[\ud800-\udfff]")
_POW10 = tuple(float(f"1e{i}") for i in range(29))
_TWO_POW_64 = 1.8446744073709552e19


def to_float32(value: float) -> float:
    """C# (float)value: round a double to the nearest float32 (returned as a Python float)."""
    return array("f", (value,))[0]


def round_double(value: float, digits: int) -> float:
    """C# Math.Round(double, digits) (ToEven): scale by 10^digits, round half to even, scale back - not Python's round().

    The sign of zero is kept (Math.Round(-0.4) is -0.0), which Python's int-returning round() would lose.
    """
    if abs(value) < 1e16:
        power10 = _POW10[digits]
        scaled = value * power10
        value = math.copysign(float(round(scaled)), scaled) / power10
    return value


def double_to_decimal(value: float) -> Decimal:
    """C# (decimal)double, i.e. .NET DecCalc.VarDecFromR8.

    The double is scaled by a power of ten in double arithmetic to a 15-digit integer, rounded half to even, and up to 14
    trailing zeros are factored back out of the scale (at most 28 decimal places). A result that rounds to zero is a
    plain, positive 0 (e.g. 4.9e-29 -> 0 but 5.1e-29 -> 0.0000000000000000000000000001).
    """
    if not math.isfinite(value):
        raise OverflowError("Value was either too large or too small for a Decimal.")
    if value == 0:
        return Decimal(0)
    exponent = math.frexp(value)[1]  # the biased IEEE exponent minus 1022
    if exponent < -94:
        return Decimal(0)
    if exponent > 96:
        raise OverflowError("Value was either too large or too small for a Decimal.")
    sign = 1 if value < 0 else 0
    dbl = abs(value)
    power = 14 - ((exponent * 19728) >> 16)
    if power >= 0:
        power = min(power, 28)
        dbl *= _POW10[power]
    elif power != -1 or dbl >= 1e15:
        dbl /= _POW10[-power]
    else:
        power = 0
    if dbl < 1e14 and power < 28:
        dbl *= 10
        power += 1
    mantissa = round(dbl)
    if mantissa == 0:
        return Decimal(0)
    if power < 0:
        return Decimal((sign, tuple(map(int, str(mantissa * 10 ** -power))), 0))
    stripped = 0
    while stripped < min(power, 14) and mantissa % 10 == 0:
        mantissa //= 10
        stripped += 1
    return Decimal((sign, tuple(map(int, str(mantissa))), stripped - power))


def decimal_to_double(value: Decimal | int) -> float:
    """C# (double)decimal: (low64 + high32 * 2^64) / 10^scale in double arithmetic."""
    d = value if isinstance(value, Decimal) else Decimal(value)
    sign, digits, exponent = d.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError(f"Not a finite decimal: {d}")
    mantissa = int("".join(map(str, digits))) if digits else 0
    scale = -exponent
    if scale < 0:
        mantissa *= 10 ** (-scale)
        scale = 0
    result = (float(mantissa & 0xFFFFFFFFFFFFFFFF) + float(mantissa >> 64) * _TWO_POW_64) / _POW10[scale]
    return -result if sign else result


def _utf8(s: str) -> bytes:
    """Encoding.UTF8.GetBytes: lone surrogates become U+FFFD."""
    try:
        return s.encode("utf-8")
    except UnicodeEncodeError:
        return _LONE_SURROGATE_RX.sub("\ufffd", s).encode("utf-8")


class TextSimilarity:
    @staticmethod
    def trigrams(text: str) -> set[str]:
        """Character trigrams of "  " + normalized + " ", sliced in UTF-16 code units like C# Substring."""
        t = "  " + IngredientTaxonomy.normalize(text) + " "
        if t.isascii() or max(t) <= "\uffff":
            return {t[i : i + 3] for i in range(len(t) - 2)}
        units = t.encode("utf-16-le", "surrogatepass")
        return {units[i : i + 6].decode("utf-16-le", "surrogatepass") for i in range(0, len(units) - 4, 2)}

    @staticmethod
    def trigram_jaccard(a: str, b: str) -> float:
        return TextSimilarity.jaccard(TextSimilarity.trigrams(a), TextSimilarity.trigrams(b))

    @staticmethod
    def jaccard(a: Iterable[T], b: Iterable[T]) -> float:
        sa = a if isinstance(a, AbstractSet) else set(a)
        sb = b if isinstance(b, AbstractSet) else set(b)
        if len(sa) == 0 and len(sb) == 0:
            return 0.0
        inter = sum(1 for x in sa if x in sb)
        return to_float32(inter / (len(sa) + len(sb) - inter))

    @staticmethod
    def cosine(a: Sequence[float], b: Sequence[float]) -> float:
        if len(a) != len(b) or len(a) == 0:
            return 0.0
        fa = array("f", a)
        fb = array("f", b)
        ab = array("f", [x * y for x, y in zip(fa, fb)])
        aa = array("f", [x * x for x in fa])
        bb = array("f", [y * y for y in fb])
        dot = na = nb = 0.0
        for i in range(len(fa)):
            dot += ab[i]
            na += aa[i]
            nb += bb[i]
        return 0.0 if na == 0 or nb == 0 else to_float32(dot / math.sqrt(na * nb))


class HashingEmbedder:
    """
    Deterministic local stand-in for the multilingual embedding model (task T7): signed feature hashing of word unigrams
    and character trigrams into 1024 dimensions (the pgvector column size), L2-normalized. Production configures a real
    embedding deployment through the LLM gateway; the vectors are versioned by model_ref so both can coexist.
    """

    DIMENSIONS = 1024
    MODEL_REF = "local/hashing-embedder@1"

    @staticmethod
    def embed(text: str) -> list[float]:
        # Every update is +-1 or +-2, so integer accumulation equals the C# float32 accumulation exactly.
        v = [0] * HashingEmbedder.DIMENSIONS
        normalized = IngredientTaxonomy.normalize(text)
        for word in normalized.split(" "):
            if word:
                HashingEmbedder._add(v, "w:" + word, 2)
        for tri in TextSimilarity.trigrams(normalized):
            HashingEmbedder._add(v, "c:" + tri, 1)
        norm = math.sqrt(sum(x * x for x in v))
        if norm > 0:
            return array("f", [x / norm for x in v]).tolist()
        return [0.0] * HashingEmbedder.DIMENSIONS

    @staticmethod
    def _add(v: list[int], feature: str, weight: int) -> None:
        h = HashingEmbedder._fnv1a(feature)
        v[h % HashingEmbedder.DIMENSIONS] += weight if ((h >> 31) & 1) == 0 else -weight

    @staticmethod
    def _fnv1a(s: str) -> int:
        h = 2166136261
        for b in _utf8(s):
            h = ((h ^ b) * 16777619) & 0xFFFFFFFF
        return h
