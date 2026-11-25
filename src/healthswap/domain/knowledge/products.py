"""Canonical product records used by scoring, eligibility and explanations (ported from Knowledge/Products.cs).

Mapping notes:
    * C# `decimal` fields are `Decimal`; IReadOnlyList / IReadOnlyCollection fields are tuples.
    * C# `IReadOnlySet<string>` fields are `OrderedFrozenSet`: a C# HashSet enumerates in insertion order, so anything
      derived from these sets (joined messages, lists, JSON arrays) stays deterministic, which a plain frozenset is not
      under Python's per-process string hash randomisation.
    * `round_decimal` is C# Math.Round(decimal, n): banker's rounding that never adds trailing zeros, so values print
      exactly like the C# ToString() in messages.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from decimal import ROUND_HALF_EVEN, Decimal
from enum import StrEnum
from typing import TypeVar

from healthswap.domain.primitives import NetQuantity, to_decimal

T = TypeVar("T")

_ZERO = Decimal(0)


def round_decimal(value: Decimal | int, places: int) -> Decimal:
    """C# Math.Round(decimal, places) (MidpointRounding.ToEven): only rounds when the scale exceeds `places`."""
    d = to_decimal(value)
    exponent = d.as_tuple().exponent
    if not isinstance(exponent, int) or exponent >= -places:
        return d
    return d.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def decimal_max(a: Decimal, b: Decimal) -> Decimal:
    """C# Math.Max(decimal, decimal): returns `a` when the values are equal (keeps its scale)."""
    return a if a >= b else b


def decimal_min(a: Decimal, b: Decimal) -> Decimal:
    """C# Math.Min(decimal, decimal): returns `b` when the values are equal (keeps its scale)."""
    return a if a < b else b


def decimal_clamp(value: Decimal, minimum: Decimal, maximum: Decimal) -> Decimal:
    """C# Math.Clamp(decimal, decimal, decimal): `value` itself (scale kept) when it lies within [minimum, maximum]."""
    if minimum > maximum:
        raise ValueError(f"'{minimum}' cannot be greater than {maximum}.")
    if value < minimum:
        return minimum
    return maximum if value > maximum else value


_DECIMAL_MAX_MANTISSA = (1 << 96) - 1


def _decimal_parts(d: Decimal) -> tuple[int, int, int]:
    """(sign, mantissa, scale) of `d` as a C# decimal; a positive exponent is folded into the mantissa (scale 0)."""
    sign, digits, exponent = d.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError(f"Not a finite decimal: {d}")
    mantissa = int("".join(map(str, digits)))
    return (sign, mantissa * 10**exponent, 0) if exponent > 0 else (sign, mantissa, -exponent)


def _decimal_of(negative: bool, mantissa: int, scale: int) -> Decimal:
    # The tuple constructor is exact (Decimal arithmetic would round a 29-digit C# mantissa to the 28-digit context).
    return Decimal((1 if negative else 0, tuple(map(int, str(mantissa))), -scale))


def decimal_div(a: Decimal, b: Decimal) -> Decimal:
    """C# decimal division (DecCalc.VarDecDiv), digit for digit.

    Python's `/` rounds to 28 significant digits; C# keeps as many digits as fit in the 96-bit mantissa at a scale of at
    most 28 (so 10m / 3m has 29 digits and 1m / 30m only 27), rounds the last one half to even and, when the quotient is
    not exact at the natural scale (scale(a) - scale(b)), strips trailing zeros again. 2.40m / 2m is 1.20 while
    1.0m / 8m is 0.125. Matching the digits keeps nested divisions (normalized price ratios) and decimal -> double
    conversions identical to C#.
    """
    sign_a, ma, sa = _decimal_parts(a)
    sign_b, mb, sb = _decimal_parts(b)
    if mb == 0:
        raise ZeroDivisionError("Attempted to divide by zero.")
    negative = sign_a != sign_b
    natural = sa - sb
    quotient, remainder = divmod(ma, mb)
    if remainder == 0:
        if natural >= 0:
            return _decimal_of(negative, quotient, natural)
        quotient *= 10**-natural
        if quotient > _DECIMAL_MAX_MANTISSA:
            raise OverflowError("Value was either too large or too small for a Decimal.")
        return _decimal_of(negative, quotient, 0)
    low = max(natural, 0)
    scale = max(28, natural)
    quotient = ma * 10 ** (scale - natural) // mb
    if quotient > _DECIMAL_MAX_MANTISSA:
        scale -= len(str(quotient)) - 29
        if ma * 10 ** (scale - natural) // mb > _DECIMAL_MAX_MANTISSA:
            scale -= 1
    if scale < low:
        raise OverflowError("Value was either too large or too small for a Decimal.")
    quotient, remainder = divmod(ma * 10 ** (scale - natural), mb)
    if remainder and (2 * remainder > mb or (2 * remainder == mb and quotient & 1)):
        quotient += 1
        if quotient > _DECIMAL_MAX_MANTISSA:  # OverflowUnscale: one digit less
            scale -= 1
            if scale < 0:
                raise OverflowError("Value was either too large or too small for a Decimal.")
            quotient, remainder = divmod(ma * 10 ** (scale - natural), mb)
            if 2 * remainder > mb or (2 * remainder == mb and quotient & 1):
                quotient += 1
    while scale > 0 and quotient % 10 == 0:
        quotient //= 10
        scale -= 1
    return _decimal_of(negative, quotient, scale)


def decimal_mul(a: Decimal, b: Decimal) -> Decimal:
    """C# decimal multiplication (DecCalc.VarDecMul), digit for digit.

    The exact product keeps the scale sum unless it needs more than a 96-bit mantissa or a scale above 28; it is then
    divided by the fewest powers of ten that fit and rounded half to even (Python's `*` rounds to 28 significant digits
    instead). A zero product has scale 0 unless both mantissas are below 2^32 (0.00m * 0.682909531184505m is 0, while
    0.00m * 0.5m is 0.000), which changes the scale of sums such as the ranking score.
    """
    sign_a, ma, sa = _decimal_parts(a)
    sign_b, mb, sb = _decimal_parts(b)
    negative = sign_a != sign_b
    scale = sa + sb
    product = ma * mb
    if ma >> 32 == 0 and mb >> 32 == 0:
        if scale > 28:
            if scale > 28 + 19:
                return Decimal(0)
            product, remainder = divmod(product, 10 ** (scale - 28))
            half = 10 ** (scale - 28) // 2
            if remainder > half or (remainder == half and product & 1):
                product += 1
            scale = 28
        return _decimal_of(negative, product, scale)
    if product == 0:
        return Decimal(0)
    if product > _DECIMAL_MAX_MANTISSA or scale > 28:
        k = max(scale - 28, 0)
        while product // 10**k > _DECIMAL_MAX_MANTISSA:
            k += 1
        if k > scale:
            raise OverflowError("Value was either too large or too small for a Decimal.")
        if k > 0:
            quotient, remainder = divmod(product, 10**k)
            half = 10**k // 2
            if remainder > half or (remainder == half and quotient & 1):
                quotient += 1
                if quotient > _DECIMAL_MAX_MANTISSA:  # the rounding carried beyond 96 bits: scale by 10 more
                    if k == scale:
                        raise OverflowError("Value was either too large or too small for a Decimal.")
                    quotient, last = divmod(quotient, 10)
                    if last > 5 or (last == 5 and (quotient & 1 or remainder)):
                        quotient += 1
                    k += 1
            product = quotient
            scale -= k
    return _decimal_of(negative, product, scale)


class OrderedFrozenSet(AbstractSet[T]):
    """Immutable, hashable set that iterates in insertion order (the enumeration order of a C# HashSet without removals)."""

    __slots__ = ("_items",)

    def __init__(self, items: Iterable[T] = ()) -> None:
        self._items: dict[T, None] = dict.fromkeys(items)

    @classmethod
    def _from_iterable(cls, iterable: Iterable[T]) -> OrderedFrozenSet[T]:
        return cls(iterable)

    def __contains__(self, item: object) -> bool:
        return item in self._items

    def __iter__(self) -> Iterator[T]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __hash__(self) -> int:
        return self._hash()

    def __repr__(self) -> str:
        return f"OrderedFrozenSet({list(self._items)!r})"


class Basis:
    PER_100G = "PER_100G"
    PER_100ML = "PER_100ML"
    PER_SERVING = "PER_SERVING"


class AllergenPresence(StrEnum):
    CONTAINS = "CONTAINS"
    MAY_CONTAIN = "MAY_CONTAIN"
    FREE_FROM = "FREE_FROM"


@dataclass(frozen=True, slots=True, kw_only=True)
class Nutrition:
    """Nutrition per 100 g (solids) or per 100 ml (liquids) - the primary basis for every model (doc 02 7.1)."""

    basis: str = Basis.PER_100G
    energy_kcal: Decimal | None = None
    protein_g: Decimal | None = None
    carbohydrate_g: Decimal | None = None
    # TOTAL_INCL_FIBRE (US "total carbohydrate") or AVAILABLE (EU/IN).
    carb_definition: str = "AVAILABLE"
    total_sugars_g: Decimal | None = None
    added_sugars_g: Decimal | None = None
    total_fat_g: Decimal | None = None
    saturated_fat_g: Decimal | None = None
    trans_fat_g: Decimal | None = None
    cholesterol_mg: Decimal | None = None
    sodium_mg: Decimal | None = None
    salt_g: Decimal | None = None
    fibre_g: Decimal | None = None
    # Fruit, vegetables, legumes and nuts share (%), from QUID or estimated from ingredients.
    fvln_pct: Decimal | None = None
    fvln_estimated: bool = False

    @property
    def is_liquid(self) -> bool:
        return self.basis == Basis.PER_100ML

    @property
    def sodium(self) -> Decimal | None:
        """Sodium in mg; derived from salt (salt = sodium x 2.5) when only salt is declared."""
        if self.sodium_mg is not None:
            return self.sodium_mg
        return None if self.salt_g is None else round_decimal(self.salt_g * 400, 1)

    @property
    def salt(self) -> Decimal | None:
        if self.salt_g is not None:
            return self.salt_g
        return None if self.sodium_mg is None else round_decimal(self.sodium_mg * Decimal("2.5") / Decimal(1000), 3)

    @property
    def energy_kj(self) -> Decimal | None:
        return None if self.energy_kcal is None else round_decimal(self.energy_kcal * Decimal("4.184"), 1)

    @property
    def available_carbohydrate(self) -> Decimal | None:
        """Available carbohydrate: US total carbohydrate includes dietary fibre."""
        c = self.carbohydrate_g
        if c is None:
            return None
        if self.carb_definition == "TOTAL_INCL_FIBRE":
            return decimal_max(_ZERO, c - (self.fibre_g if self.fibre_g is not None else _ZERO))
        return c

    @property
    def limited_sugars(self) -> Decimal | None:
        """Added sugars when declared, otherwise total sugars (doc 02 7.1: models fall back to total sugars)."""
        return self.added_sugars_g if self.added_sugars_g is not None else self.total_sugars_g

    def get(self, nutrient: str) -> Decimal | None:
        match nutrient:
            case "ENERGY_KCAL":
                return self.energy_kcal
            case "ENERGY_KJ":
                return self.energy_kj
            case "PROTEIN_G":
                return self.protein_g
            case "CARB_G":
                return self.carbohydrate_g
            case "SUGARS_G":
                return self.total_sugars_g
            case "ADDED_SUGARS_G":
                return self.added_sugars_g
            case "LIMITED_SUGARS_G":
                return self.limited_sugars
            case "FAT_G":
                return self.total_fat_g
            case "SAT_FAT_G":
                return self.saturated_fat_g
            case "TRANS_FAT_G":
                return self.trans_fat_g
            case "SODIUM_MG":
                return self.sodium
            case "SALT_G":
                return self.salt
            case "FIBRE_G":
                return self.fibre_g
            case "FVL_PCT":
                return self.fvln_pct
            case _:
                return None


@dataclass(frozen=True, slots=True)
class ParsedIngredient:
    """A parsed ingredient token (doc 02 7.2)."""

    position: int
    parent_position: int | None
    raw_text: str
    canonical_name: str | None
    percent: Decimal | None
    is_additive: bool
    additive_code: str | None
    flags: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductData:
    """Canonical product record used by scoring, eligibility and explanations (one product version)."""

    product_id: uuid.UUID = uuid.UUID(int=0)
    version_id: int = 0
    gtin: str | None = None
    name: str
    brand: str | None = None
    # PREMIUM, MAINSTREAM or PRIVATE_LABEL (used for swap-graph diversity).
    brand_tier: str = "MAINSTREAM"
    category_code: str
    category_id: uuid.UUID | None = None
    # Model routing: GENERAL, BEVERAGE, WATER, FAT_OIL_NUT, CHEESE, RED_MEAT.
    scoring_category: str = "GENERAL"
    physical_state: str = "SOLID"
    net_quantity: NetQuantity | None = None
    pack_count: int = 1
    markets: tuple[str, ...] = ()
    is_private_label: bool = False
    nutrition: Nutrition = field(default_factory=Nutrition)
    serving_size: Decimal | None = None
    serving_unit: str | None = None
    ingredients_text: str | None = None
    ingredients: tuple[ParsedIngredient, ...] = ()
    ingredient_resolution: Decimal = _ZERO
    # Additive codes (INS numbers, for example 322, 150d, 951).
    additives: tuple[str, ...] = ()
    ingredient_flags: AbstractSet[str] = field(default_factory=OrderedFrozenSet)
    allergens: Mapping[str, AllergenPresence] = field(default_factory=dict)
    # True when allergen data is curator- or brand-verified (R7 requirement for shoppers with declared allergens).
    allergens_verified: bool = False
    allergen_confidence: Decimal = _ZERO
    marks: tuple[str, ...] = ()
    # VEGAN, VEG, EGG, NON_VEG or UNKNOWN.
    diet_class: str = "UNKNOWN"
    jain_friendly_inferred: bool = False
    nova: int | None = None
    nova_confidence: Decimal = _ZERO
    data_sufficiency: str = "INSUFFICIENT"
    confidence: Decimal = _ZERO
    verification: str = "UNVERIFIED"
    # Substitutability attributes: form (BAR, NOODLE, BISCUIT, ...), flavour family, use occasion, cuisine.
    form: str | None = None
    flavour_family: str | None = None
    use_occasion: str | None = None
    cuisine: str | None = None
    recalled: bool = False
    quarantined: bool = False

    def has(self, ingredient_flag: str) -> bool:
        return ingredient_flag in self.ingredient_flags

    @property
    def contains_caffeine(self) -> bool:
        return "CAFFEINE" in self.ingredient_flags

    @property
    def serving_grams(self) -> Decimal | None:
        return self.serving_size if self.serving_size is not None and self.serving_unit in ("g", "ml") else None
