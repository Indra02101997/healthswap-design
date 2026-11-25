"""Source connectors (C# Knowledge/Connectors.cs): each maps a source-specific layout to a `ProductCandidate` (or a
`RecallRecord`), normalized to the canonical model before survivorship (doc 02 sections 6-7). They read files only and
never call remote APIs at request time.

The readers keep the System.Text.Json behaviour the C# implementation relies on:
    * per-product documents are read like JsonNode: property names are case-sensitive, a missing property and JSON null
      are the same, a value of the wrong JSON type raises ValueError (GetValue<T> / AsObject throw in C#);
    * file-level records (the brand-portal envelope, FoodData Central foods) are read like JsonSerializer with
      RefJson.Options (case-insensitive property names);
    * `raw_json` is byte-for-byte what C# stores: the brand-portal element text as written in the file
      (JsonElement.GetRawText, line endings kept), the OFF line, and for FoodData Central the food record re-serialized
      with RefJson.Options (camelCase, nulls omitted, decimals with their scale). The ingestor hashes it.
    * .NET regex classes: \\d is a BMP decimal digit, \\s the .NET white-space set and \\b uses the .NET word characters
      (letters, Mn, Nd, Pc, ZWJ, ZWNJ; UTF-16 units, so a supplementary-plane character is never a word character);
    * TextInfo.ToTitleCase (invariant culture) for FoodData Central's upper-case descriptions.
"""

from __future__ import annotations

import json
import re
import unicodedata
import uuid
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from json.decoder import scanstring
from pathlib import Path
from typing import Any

from healthswap.domain.knowledge.parsing import (
    _parse_invariant_decimal,
    is_null_or_white_space,
    net_regex,
    net_trim,
    to_lower_invariant,
    to_upper_invariant,
)
from healthswap.domain.knowledge.products import AllergenPresence, Basis, Nutrition, decimal_mul, round_decimal
from healthswap.domain.primitives import UTC, gtin_try_normalize
from healthswap.domain.scoring.models import _json_child, _json_object, _json_str_or_none
from healthswap.platform import netjson

_MIN_UTC = datetime.min.replace(tzinfo=UTC)
_UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_INT32 = (-(2**31), 2**31 - 1)
_INT64 = (-(2**63), 2**63 - 1)


@dataclass(frozen=True, slots=True, kw_only=True)
class ProductCandidate:
    """A product record from one source, normalized to the canonical model before survivorship (doc 02 sections 6-7)."""

    source_code: str
    external_id: str
    raw_gtin: str | None = None
    name: str
    brand: str | None = None
    brand_owner: str | None = None
    brand_tier: str = "MAINSTREAM"
    category_code: str | None = None
    markets: tuple[str, ...] = ()
    net_value: Decimal | None = None
    net_unit: str | None = None
    pack_count: int = 1
    physical_state: str = "SOLID"
    serving_value: Decimal | None = None
    serving_unit: str | None = None
    nutrition: Nutrition | None = None
    salt_declared: bool = False
    sodium_declared: bool = False
    per_serving_energy_kcal: Decimal | None = None
    ingredients_text: str | None = None
    allergen_statement: str | None = None
    # Structured allergen tags from the source (OFF allergens_tags / traces_tags) as code -> presence.
    allergen_tags: Mapping[str, AllergenPresence] = field(default_factory=dict)
    dietary_marks: tuple[str, ...] = ()
    marks_inferred: bool = False
    attributes: Mapping[str, str] = field(default_factory=dict)
    is_private_label: bool = False
    verification: str = "UNVERIFIED"
    last_verified_at: datetime | None = None
    observed_at: datetime = _MIN_UTC
    raw_json: str
    partner_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class RecallRecord:
    """A recall notice (C# Connectors.RecallRecord); `gtin` is the normalized GTIN-14 or None."""

    authority: str
    reference: str
    gtin: str | None
    description: str
    classification: str | None
    published_at: datetime
    jurisdiction: str


# ---------------------------------------------------------------------------------------------------------------------
# JSON reading
# ---------------------------------------------------------------------------------------------------------------------


def _reject_constant(name: str) -> Any:
    raise ValueError(f"'{name}' is an invalid JSON literal.")


# Numbers with a fraction or exponent become Decimal (C# decimal keeps the scale); NaN / Infinity are not JSON.
_DECODER = json.JSONDecoder(parse_float=Decimal, parse_constant=_reject_constant)
_JSON_WS = " \t\n\r"


def _read_text(path: str | Path) -> str:
    """File.ReadAllText: UTF-8 (BOM skipped, invalid bytes replaced), line endings kept."""
    with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
        return f.read()


def _fail(expected: str, value: Any) -> ValueError:
    return ValueError(f"The JSON value {value!r} could not be read as {expected}.")


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, Decimal)) and not isinstance(value, bool)


def _decimal_value(value: Decimal | int) -> Decimal:
    """A JSON number as C# decimal: integral values written with an exponent (1e3) have scale 0."""
    d = Decimal(value) if isinstance(value, int) else value
    exponent = d.as_tuple().exponent
    return Decimal(int(d)) if isinstance(exponent, int) and exponent > 0 else d


def _child(node: Any, name: str) -> Any:
    """node?[name] on a JsonNode: None when node is None or the property is missing or null; a non-object raises."""
    if node is None:
        return None
    if not isinstance(node, dict):
        raise _fail("a JSON object", node)
    return node.get(name)


def _object(node: Any) -> dict[str, Any] | None:
    """node?.AsObject()."""
    if node is not None and not isinstance(node, dict):
        raise _fail("a JSON object", node)
    return node


def _array(node: Any) -> list[Any] | None:
    """node?.AsArray()."""
    if node is not None and not isinstance(node, list):
        raise _fail("a JSON array", node)
    return node


def _string(node: Any) -> str | None:
    """node?.GetValue<string>()."""
    if node is not None and not isinstance(node, str):
        raise _fail("a string", node)
    return node


def _required(node: Any) -> Any:
    """node! (the C# code dereferences it; a missing value is a NullReferenceException there)."""
    if node is None:
        raise ValueError("Object reference not set to an instance of an object (a required JSON value is missing or null).")
    return node


def _value_or(value: Any, default: Any) -> Any:
    """C# `value ?? default`."""
    return default if value is None else value


def _decimal(node: Any) -> Decimal | None:
    """node?.GetValue<decimal>()."""
    if node is None:
        return None
    if not _is_number(node):
        raise _fail("a decimal", node)
    return _decimal_value(node)


def _integer(node: Any, bounds: tuple[int, int], expected: str) -> int | None:
    """node?.GetValue<int|long>(): an integral JSON number in range (1.0 and 1e2 are rejected like Utf8Parser does)."""
    if node is None:
        return None
    if isinstance(node, bool) or not isinstance(node, int) or not bounds[0] <= node <= bounds[1]:
        raise _fail(expected, node)
    return node


def _boolean(node: Any) -> bool | None:
    if node is not None and not isinstance(node, bool):
        raise _fail("a boolean", node)
    return node


def _strings(node: Any) -> list[str] | None:
    """node?.AsArray().Select(x => x!.GetValue<string>()).ToList()."""
    items = _array(node)
    return None if items is None else [_string(_required(x)) for x in items]


def _number_or_none(o: Mapping[str, Any] | None, name: str) -> Decimal | None:
    """o?[name] when it is a JSON number (other kinds are ignored, not rejected)."""
    v = None if o is None else o.get(name)
    return _decimal_value(v) if _is_number(v) else None


def _d(o: Mapping[str, Any], name: str) -> Decimal | None:
    return _number_or_none(o, name)


def _dn(o: Mapping[str, Any] | None, name: str) -> Decimal | None:
    v = _number_or_none(o, name)
    return None if v is None else round_decimal(v, 4)


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in _JSON_WS:
        i += 1
    return i


def _syntax_error(s: str, i: int) -> ValueError:
    return ValueError(f"Invalid JSON at offset {i}: {s[i:i + 20]!r}")


def _raw_array_items(s: str, i: int) -> tuple[list[str], int]:
    """Raw text of each element of the array starting at s[i] == '[' (JsonElement.GetRawText) and the end offset."""
    items: list[str] = []
    i = _skip_ws(s, i + 1)
    if i < len(s) and s[i] == "]":
        return items, i + 1
    while True:
        start = i
        _, i = _DECODER.raw_decode(s, start)
        items.append(s[start:i])
        i = _skip_ws(s, i)
        if i < len(s) and s[i] == ",":
            i = _skip_ws(s, i + 1)
        elif i < len(s) and s[i] == "]":
            return items, i + 1
        else:
            raise _syntax_error(s, i)


def _brand_products(text: str, path: str | Path) -> list[str]:
    """RefJson.Read<BrandProductFile>(path).Products as raw element texts (case-insensitive envelope, last duplicate wins)."""
    s = text
    i = _skip_ws(s, 0)
    if i >= len(s) or s[i] != "{":
        if _DECODER.decode(s) is None:
            raise ValueError(f"Empty file {path}")
        raise _fail("a BrandProductFile object", s[i:i + 20])
    products: list[str] | None = None
    i = _skip_ws(s, i + 1)
    if i < len(s) and s[i] == "}":
        i += 1
    else:
        while True:
            if i >= len(s) or s[i] != '"':
                raise _syntax_error(s, i)
            key, i = scanstring(s, i + 1)
            i = _skip_ws(s, i)
            if i >= len(s) or s[i] != ":":
                raise _syntax_error(s, i)
            i = _skip_ws(s, i + 1)
            name = key.lower()
            if name == "products" and i < len(s) and s[i] == "[":
                products, i = _raw_array_items(s, i)
            else:
                value, i = _DECODER.raw_decode(s, i)
                if name == "products":
                    if value is not None:
                        raise _fail("a JSON array ($.products)", value)
                    products = None
                elif name in ("version", "source") and value is not None and not isinstance(value, str):
                    raise _fail(f"a string ($.{key})", value)
            i = _skip_ws(s, i)
            if i < len(s) and s[i] == ",":
                i = _skip_ws(s, i + 1)
            elif i < len(s) and s[i] == "}":
                i += 1
                break
            else:
                raise _syntax_error(s, i)
    if _skip_ws(s, i) != len(s):
        raise _syntax_error(s, i)
    return _required(products)


# ---------------------------------------------------------------------------------------------------------------------
# .NET text helpers
# ---------------------------------------------------------------------------------------------------------------------


def _bmp_class(predicate: Callable[[str], bool]) -> str:
    """A regex character class of the BMP characters (surrogates excluded) satisfying `predicate`."""
    ranges: list[tuple[int, int]] = []
    start = -1
    for cp in range(0x10000):
        ok = not 0xD800 <= cp <= 0xDFFF and predicate(chr(cp))
        if ok and start < 0:
            start = cp
        elif not ok and start >= 0:
            ranges.append((start, cp - 1))
            start = -1
    if start >= 0:
        ranges.append((start, 0xFFFF))
    return "[" + "".join(f"\\u{a:04x}" if a == b else f"\\u{a:04x}-\\u{b:04x}" for a, b in ranges) + "]"


_WORD_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Nd", "Pc"})
_NET_DIGIT = _bmp_class(lambda c: unicodedata.category(c) == "Nd")
_NET_WORD = _bmp_class(lambda c: unicodedata.category(c) in _WORD_CATEGORIES or c in "\u200c\u200d")
_NET_SPACE = net_regex(r"\s")
# (\d+(?:\.\d+)?)\s*(kg|g|ml|l|oz|fl oz)\b - the unit ends with a letter, so \b is "no word character follows".
_QUANTITY_RX = re.compile(f"({_NET_DIGIT}+(?:\\.{_NET_DIGIT}+)?){_NET_SPACE}*(kg|g|ml|l|oz|fl oz)(?!{_NET_WORD})")
_UPC_RX = re.compile(f"{_NET_DIGIT}{{12,14}}")

_LETTERS = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo"})
_WORD_SEPARATORS = frozenset({"Zs", "Zl", "Zp", "Cc", "Cf", "Pc", "Pd", "Ps", "Pe", "Pi", "Pf", "Po", "Sm", "Sc", "Sk", "So"})
# Titlecase digraphs (TextInfo.AddTitlecaseLetter keeps the NDP 2.0 mapping).
_TITLE_DIGRAPHS = {
    "\u01c4": "\u01c5", "\u01c5": "\u01c5", "\u01c6": "\u01c5", "\u01c7": "\u01c8", "\u01c8": "\u01c8", "\u01c9": "\u01c8",
    "\u01ca": "\u01cb", "\u01cb": "\u01cb", "\u01cc": "\u01cb", "\u01f1": "\u01f2", "\u01f2": "\u01f2", "\u01f3": "\u01f2",
}


def to_title_case(s: str) -> str:
    """CultureInfo.InvariantCulture.TextInfo.ToTitleCase: upper-cases the first letter of each word; the rest of a word is
    lower-cased unless it has no lower-case letter (acronyms stay). Digits, marks and apostrophes continue a word."""
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        category = unicodedata.category(ch)
        if category not in _LETTERS:
            out.append(ch)
            i += 1
            continue
        out.append(_TITLE_DIGRAPHS.get(ch) or to_upper_invariant(ch))
        i += 1
        start = i
        has_lower = category == "Ll"
        while i < n:
            c = s[i]
            cat = unicodedata.category(c)
            if cat in _LETTERS:
                has_lower = has_lower or cat == "Ll"
                i += 1
            elif c == "'":
                i += 1
                out.append(to_lower_invariant(s[start:i]) if has_lower else s[start:i])
                start = i
                has_lower = True
            elif cat not in _WORD_SEPARATORS:
                i += 1
            else:
                break
        if i > start:
            out.append(to_lower_invariant(s[start:i]) if has_lower else s[start:i])
        if i < n:
            out.append(s[i])
            i += 1
    return "".join(out)


_US_DATE_RX = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})(?:[ T](\d{1,2}):(\d{2})(?::(\d{2}))?(?:\s*([AaPp][Mm]))?)?")


def _try_parse_utc(text: str | None) -> datetime | None:
    """DateTime.TryParse(text, InvariantCulture, AdjustToUniversal | AssumeUniversal) for the layouts FoodData Central uses:
    ISO 8601 dates and date-times (an offset is converted, no offset means UTC) and M/d/yyyy [h:mm[:ss] [AM|PM]]."""
    if text is None:
        return None
    t = text.strip()
    try:
        value = datetime.fromisoformat(t.replace("z", "Z").replace("Z", "+00:00"))
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    except ValueError:
        pass
    m = _US_DATE_RX.fullmatch(t)
    if m is None or not m.group(3).isascii():
        return None
    try:
        hour = int(m.group(4) or 0)
        if m.group(7):
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if m.group(7).lower() == "pm" else 0)
        return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2)), hour, int(m.group(5) or 0), int(m.group(6) or 0), tzinfo=UTC)
    except ValueError:
        return None


def _parse_yyyymmdd(text: str) -> datetime:
    """DateTime.ParseExact(text, "yyyyMMdd", InvariantCulture, AdjustToUniversal | AssumeUniversal)."""
    if len(text) != 8 or not (text.isascii() and text.isdigit()):
        raise ValueError(f"String '{text}' was not recognized as a valid DateTime.")
    return datetime(int(text[:4]), int(text[4:6]), int(text[6:]), tzinfo=UTC)


# ---------------------------------------------------------------------------------------------------------------------
# FoodData Central records (read like JsonSerializer with RefJson.Options)
# ---------------------------------------------------------------------------------------------------------------------


def _fdc_long(v: Mapping[str, Any], path: str, name: str) -> int:
    key = name.lower()
    if key not in v:
        return 0
    value = _integer(v[key], _INT64, f"an Int64 ({_json_child(path, name)})")
    if value is None:
        raise _fail(f"an Int64 ({_json_child(path, name)})", None)
    return value


def _fdc_decimal(v: Mapping[str, Any], path: str, name: str, nullable: bool) -> Decimal | None:
    key = name.lower()
    if key not in v:
        return None if nullable else Decimal(0)
    value = v[key]
    if value is None and nullable:
        return None
    if not _is_number(value):
        raise _fail(f"a Decimal ({_json_child(path, name)})", value)
    return _decimal_value(value)


def _fdc_strings(v: Mapping[str, Any], path: str, names: tuple[str, ...]) -> dict[str, str | None]:
    return {name: _json_str_or_none(v.get(name.lower()), _json_child(path, name)) for name in names}


def _fdc_nutrient(data: Any, path: str) -> dict[str, Any] | None:
    if data is None:
        return None
    v = _json_object(data, path, "FdcNutrient")
    ref = v.get("nutrient")
    nutrient = None
    if ref is not None:
        rp = _json_child(path, "nutrient")
        nutrient = _fdc_strings(_json_object(ref, rp, "FdcNutrientRef"), rp, ("number", "name", "unitName"))
    return {"nutrient": nutrient, "amount": _fdc_decimal(v, path, "amount", nullable=False)}


def _fdc_food(data: Any, path: str) -> dict[str, Any] | None:
    """One FdcFood as the camelCase document C# serializes (C# property order)."""
    if data is None:
        return None
    v = _json_object(data, path, "FdcFood")
    texts = _fdc_strings(v, path, ("description", "gtinUpc", "brandOwner", "brandName", "brandedFoodCategory", "marketCountry", "ingredients"))
    nutrients = v.get("foodnutrients")
    if nutrients is not None:
        np = _json_child(path, "foodNutrients")
        if not isinstance(nutrients, list):
            raise _fail(f"an array ({np})", nutrients)
        nutrients = [_fdc_nutrient(item, _json_child(np, i)) for i, item in enumerate(nutrients)]
    return {
        "fdcId": _fdc_long(v, path, "fdcId"), **texts,
        "servingSize": _fdc_decimal(v, path, "servingSize", nullable=True),
        **_fdc_strings(v, path, ("servingSizeUnit", "modifiedDate")),
        "foodNutrients": nutrients,
    }


def _fdc_file(data: Any, path: str | Path) -> list[dict[str, Any] | None]:
    if data is None:
        raise ValueError(f"Empty file {path}")
    v = _json_object(data, "", "FdcFile")
    foods = _required(v.get("brandedfoods"))
    if not isinstance(foods, list):
        raise _fail("an array ($.BrandedFoods)", foods)
    return [_fdc_food(item, _json_child("BrandedFoods", i)) for i, item in enumerate(foods)]


_FDC_CATEGORIES = {
    "Cereal": "breakfast.cereals", "Chips, Pretzels & Snacks": "snacks.chips", "Cookies & Biscuits": "biscuits.sweet",
    "Soda": "beverages.carbonated.soft-drinks", "Yogurt": "dairy.yogurt", "Breads & Buns": "bread.sliced",
}

_OFF_CATEGORY_MAP = {
    "en:instant-noodles": "noodles.instant", "en:breakfast-cereals": "breakfast.cereals", "en:cookies": "biscuits.sweet",
    "en:biscuits": "biscuits.sweet", "en:sodas": "beverages.carbonated.soft-drinks", "en:colas": "beverages.carbonated.soft-drinks",
    "en:chips": "snacks.chips", "en:crisps": "snacks.chips", "en:yogurts": "dairy.yogurt", "en:breads": "bread.sliced",
    "en:chocolates": "confectionery.chocolate", "en:ketchup": "sauces.ketchup",
}

_OFF_ALLERGENS = {
    "en:gluten": "GLUTEN_CEREALS", "en:peanuts": "PEANUTS", "en:nuts": "TREE_NUTS", "en:milk": "MILK", "en:soybeans": "SOY",
    "en:mustard": "MUSTARD", "en:sesame-seeds": "SESAME", "en:eggs": "EGGS", "en:fish": "FISH", "en:crustaceans": "CRUSTACEANS",
    "en:celery": "CELERY", "en:lupin": "LUPIN", "en:sulphur-dioxide-and-sulphites": "SULPHITES", "en:molluscs": "MOLLUSCS",
}

_OFF_MARKETS = {"en:india": "IN", "en:united-states": "US"}


class Connectors:
    """Source connectors: each maps a source-specific layout to ProductCandidate. They never call remote APIs at request time.
    The readers are lazy generators like the C# iterators (the file is read when iteration starts)."""

    RecallRecord = RecallRecord

    @staticmethod
    def brand_portal(path: str | Path, now: datetime) -> Iterator[ProductCandidate]:
        """Brand portal submissions (trust tier 1, brand-verified), as in data/seed/kb/products.json."""
        for raw in _brand_products(_read_text(path), path):
            o = _required(_object(_DECODER.decode(raw)))
            n = _object(o.get("nutrition"))
            basis = _value_or(_string(_child(n, "basis")), "PER_100G")
            carb_def = _value_or(_string(o.get("carbDefinition")), "AVAILABLE")
            attributes = _object(o.get("attributes"))
            attrs = {} if attributes is None else {k: _string(_required(v)) for k, v in attributes.items()}
            attrs["brandTier"] = _value_or(_string(o.get("brandTier")), "MAINSTREAM")
            gtin = _string(_required(o.get("gtin")))
            yield ProductCandidate(
                source_code="BRAND_PORTAL", external_id=gtin, raw_gtin=gtin,
                name=_string(_required(o.get("name"))), brand=_string(o.get("brand")), brand_owner=_string(o.get("brandOwner")),
                brand_tier=attrs["brandTier"], category_code=_string(o.get("category")),
                markets=tuple(_strings(o.get("markets")) or ()),
                net_value=_decimal(_child(o.get("netQuantity"), "value")), net_unit=_string(_child(o.get("netQuantity"), "unit")),
                pack_count=_value_or(_integer(o.get("packCount"), _INT32, "an Int32"), 1),
                physical_state=_value_or(_string(o.get("physicalState")), "SOLID"),
                serving_value=_decimal(_child(o.get("servingSize"), "value")), serving_unit=_string(_child(o.get("servingSize"), "unit")),
                nutrition=None if n is None else Nutrition(
                    basis=basis, carb_definition=carb_def,
                    energy_kcal=_d(n, "energyKcal"), protein_g=_d(n, "proteinG"), carbohydrate_g=_d(n, "carbohydrateG"), total_sugars_g=_d(n, "totalSugarsG"),
                    added_sugars_g=_d(n, "addedSugarsG"), total_fat_g=_d(n, "totalFatG"), saturated_fat_g=_d(n, "saturatedFatG"), trans_fat_g=_d(n, "transFatG"),
                    cholesterol_mg=_d(n, "cholesterolMg"), sodium_mg=_d(n, "sodiumMg"), salt_g=_d(n, "saltG"), fibre_g=_d(n, "dietaryFibreG"),
                ),
                sodium_declared=_child(n, "sodiumMg") is not None, salt_declared=_child(n, "saltG") is not None,
                ingredients_text=_string(o.get("ingredientsText")), allergen_statement=_string(o.get("allergenStatement")),
                dietary_marks=tuple(_strings(o.get("dietaryMarks")) or ()),
                attributes=attrs, is_private_label=_value_or(_boolean(o.get("isPrivateLabel")), False),
                verification="BRAND_VERIFIED", last_verified_at=now - timedelta(days=30), observed_at=now, raw_json=raw,
            )

    @staticmethod
    def open_food_facts(path: str | Path, now: datetime) -> Iterator[ProductCandidate]:
        """Open Food Facts JSONL export lines (ODbL, trust tier 4). Process nightly deltas in timestamp order and reconcile
        weekly against the full dump (deltas carry no deletions). Never called at request time (15 reads/min/IP API limit)."""
        with open(path, encoding="utf-8-sig", errors="replace") as f:  # universal newlines: \n, \r\n and \r end a line
            for physical in f:
                line = physical[:-1] if physical.endswith("\n") else physical
                if is_null_or_white_space(line):
                    continue
                o = _required(_object(_DECODER.decode(line)))
                countries = _strings(o.get("countries_tags")) or []
                markets = [_OFF_MARKETS[c] for c in countries if c in _OFF_MARKETS]
                if not markets:
                    continue  # filter by countries_tags for target markets
                nut = _object(o.get("nutriments"))
                allergens: dict[str, AllergenPresence] = {}
                for tag in _array(o.get("traces_tags")) or ():
                    code = _OFF_ALLERGENS.get(_string(_required(tag)))
                    if code is not None:
                        allergens[code] = AllergenPresence.MAY_CONTAIN
                for tag in _array(o.get("allergens_tags")) or ():
                    code = _OFF_ALLERGENS.get(_string(_required(tag)))
                    if code is not None:
                        allergens[code] = AllergenPresence.CONTAINS
                category = None
                for tag in _array(o.get("categories_tags")) or ():  # lazy like Select(...).FirstOrDefault(...)
                    category = _OFF_CATEGORY_MAP.get(_string(_required(tag)))
                    if category is not None:
                        break
                qty, unit = Connectors.parse_quantity(_string(o.get("quantity")))
                labels = _strings(o.get("labels_tags")) or []
                sodium_g = _dn(nut, "sodium_100g")
                code = _string(_required(o.get("code")))
                brands = _string(o.get("brands"))
                liquid = unit in ("ml", "l")
                last_modified = o.get("last_modified_t")
                yield ProductCandidate(
                    source_code="OFF", external_id=code, raw_gtin=code,
                    name=_value_or(_string(o.get("product_name")), "Unnamed product"), brand=None if brands is None else net_trim(brands.split(",")[0]),
                    category_code=category, markets=tuple(markets), net_value=qty, net_unit=unit,
                    physical_state="LIQUID" if liquid else "SOLID",
                    nutrition=None if nut is None else Nutrition(
                        basis=Basis.PER_100ML if liquid else Basis.PER_100G,
                        energy_kcal=_dn(nut, "energy-kcal_100g"), protein_g=_dn(nut, "proteins_100g"), carbohydrate_g=_dn(nut, "carbohydrates_100g"),
                        total_sugars_g=_dn(nut, "sugars_100g"), added_sugars_g=_dn(nut, "added-sugars_100g"), total_fat_g=_dn(nut, "fat_100g"),
                        saturated_fat_g=_dn(nut, "saturated-fat_100g"), trans_fat_g=_dn(nut, "trans-fat_100g"), fibre_g=_dn(nut, "fiber_100g"),
                        salt_g=_dn(nut, "salt_100g"), sodium_mg=None if sodium_g is None else round_decimal(decimal_mul(sodium_g, Decimal(1000)), 1),
                        carb_definition="TOTAL_INCL_FIBRE" if "US" in markets else "AVAILABLE",
                    ),
                    salt_declared=_child(nut, "salt_100g") is not None, sodium_declared=False,
                    ingredients_text=_string(o.get("ingredients_text")), allergen_tags=allergens,
                    dietary_marks=("IN_FSSAI_VEG",) if "en:vegetarian" in labels and "IN" in markets else (), marks_inferred=True,
                    verification="UNVERIFIED",
                    observed_at=now if last_modified is None else _UNIX_EPOCH + timedelta(seconds=_integer(last_modified, _INT64, "an Int64")),
                    raw_json=line,
                )

    @staticmethod
    def usda_fdc_branded(path: str | Path, now: datetime) -> Iterator[ProductCandidate]:
        """USDA FoodData Central Branded Foods (CC0, trust tier 2). Nutrients per 100 g/ml, keyed by nutrient number."""
        for f in _fdc_file(_DECODER.decode(_read_text(path)), path):
            f = _required(f)
            nutrients = f["foodNutrients"]

            def n(number: str) -> Decimal | None:  # called while this food is mapped
                for x in _required(nutrients):
                    if _required(_required(x)["nutrient"])["number"] == number:
                        return x["amount"]
                return None

            unit = f["servingSizeUnit"]
            liquid = unit in ("ml", "MLT")
            brand_name = f["brandName"]
            ingredients = f["ingredients"]
            observed = _try_parse_utc(f["modifiedDate"])
            yield ProductCandidate(
                source_code="USDA_FDC_BRANDED", external_id=str(f["fdcId"]), raw_gtin=f["gtinUpc"],
                name=to_title_case(to_lower_invariant(_required(f["description"]))),
                brand=None if brand_name is None else to_title_case(to_lower_invariant(brand_name)), brand_owner=f["brandOwner"],
                category_code=_FDC_CATEGORIES.get(f["brandedFoodCategory"]), markets=("US",) if f["marketCountry"] == "United States" else (),
                physical_state="LIQUID" if liquid else "SOLID", serving_value=f["servingSize"],
                serving_unit="g" if unit is not None and to_lower_invariant(unit) in ("g", "grm") else "ml" if liquid else "g",
                nutrition=Nutrition(
                    basis=Basis.PER_100ML if liquid else Basis.PER_100G, carb_definition="TOTAL_INCL_FIBRE",
                    energy_kcal=n("208"), protein_g=n("203"), total_fat_g=n("204"), carbohydrate_g=n("205"), total_sugars_g=n("269"),
                    added_sugars_g=n("539"), fibre_g=n("291"), sodium_mg=n("307"), saturated_fat_g=n("606"), trans_fat_g=n("605"), cholesterol_mg=n("601"),
                ),
                sodium_declared=True, ingredients_text=None if ingredients is None else to_title_case(to_lower_invariant(ingredients)),
                verification="UNVERIFIED", observed_at=now if observed is None else observed,
                raw_json=netjson.serialize_hs(f),
            )

    @staticmethod
    def open_fda_enforcement(path: str | Path) -> Iterator[RecallRecord]:
        """openFDA food enforcement reports (weekly). UPCs in code_info / product_description are matched to GTINs."""
        o = _required(_object(_DECODER.decode(_read_text(path))))
        for r in _array(o.get("results")) or ():
            r = _required(r)
            text = (_string(_child(r, "code_info")) or "") + " " + (_string(_child(r, "product_description")) or "")
            upcs = list(dict.fromkeys(m.group(0) for m in _UPC_RX.finditer(text.replace(" ", ""))))
            published = _parse_yyyymmdd(_string(_required(_child(r, "report_date"))))
            for upc in upcs or [None]:
                yield RecallRecord(
                    "FDA", _string(_required(_child(r, "recall_number"))), None if upc is None else gtin_try_normalize(upc),
                    (_string(_child(r, "product_description")) or "") + " - " + (_string(_child(r, "reason_for_recall")) or ""),
                    _string(_child(r, "classification")), published, "US",
                )

    @staticmethod
    def parse_quantity(text: str | None) -> tuple[Decimal | None, str | None]:
        """("280 g" -> (280, "g")): the first number followed by kg, g, ml, l, oz or fl oz; (None, None) otherwise."""
        if is_null_or_white_space(text):
            return None, None
        m = _QUANTITY_RX.search(to_lower_invariant(text))
        return (_parse_invariant_decimal(m.group(1)), m.group(2)) if m else (None, None)
