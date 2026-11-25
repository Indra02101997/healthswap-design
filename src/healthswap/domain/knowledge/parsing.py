"""Ingredient taxonomy, deterministic ingredient parser, allergen derivation, diet and NOVA classification (doc 02 7.2).

Ported from Knowledge/Parsing.cs. LLMs are never used here. .NET string semantics are kept where they can change results:
    * white space is the .NET set (char.IsWhiteSpace / regex \\s), not Python's (which adds U+001C..U+001F);
    * ToLowerInvariant / ToUpperInvariant are simple (1:1) case mappings (no final sigma, U+0130 and U+0131 unchanged);
    * OrdinalIgnoreCase dictionaries keep the first key spelling and insertion order;
    * lengths and the letter test around alias matches count UTF-16 code units;
    * decimal.Parse(..., InvariantCulture) only accepts ASCII digits (the C# parser throws on other digits, so does this).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Collection, Iterable, Iterator, Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from decimal import Decimal
from typing import Generic, TypeVar

from healthswap.domain.knowledge.products import (
    AllergenPresence,
    OrderedFrozenSet,
    ParsedIngredient,
    decimal_min,
    round_decimal,
)

V = TypeVar("V")

# ---------------------------------------------------------------------------------------------------------------------
# .NET string semantics
# ---------------------------------------------------------------------------------------------------------------------

# char.IsWhiteSpace and the regex class \s: U+0009..U+000D, U+0085 and the Unicode Z categories (Zs, Zl, Zp).
NET_WHITESPACE = "\t\n\x0b\x0c\r \x85\xa0\u1680" + "".join(chr(c) for c in range(0x2000, 0x200B)) + "\u2028\u2029\u202f\u205f\u3000"
_WS_CLASS = "[\t\n\x0b\x0c\r \x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]"
_WS_RUN_RX = re.compile(_WS_CLASS + "+")


def net_regex(pattern: str) -> str:
    """Rewrites \\s in a .NET pattern to the .NET white-space class (only valid outside character classes)."""
    return pattern.replace(r"\s", _WS_CLASS)


def net_trim(s: str) -> str:
    """C# string.Trim()."""
    return s.strip(NET_WHITESPACE)


def is_null_or_white_space(s: str | None) -> bool:
    """C# string.IsNullOrWhiteSpace."""
    return s is None or not s.strip(NET_WHITESPACE)


def utf16_len(s: str) -> int:
    """C# string.Length (UTF-16 code units)."""
    return len(s) if s.isascii() else len(s) + sum(1 for ch in s if ord(ch) > 0xFFFF)


def to_lower_invariant(s: str) -> str:
    """C# ToLowerInvariant: simple case mapping (Python's lower() would apply final sigma and expand U+0130)."""
    if s.isascii():
        return s.lower()
    return "\u0130".join(part.lower() for part in s.replace("\u03a3", "\u03c3").split("\u0130"))


def _simple_upper(ch: str) -> str:
    if ch == "\u0131":
        return ch
    upper = ch.upper()
    if len(upper) == 1:
        return upper
    title = ch.title()
    return title if len(title) == 1 else ch


def to_upper_invariant(s: str) -> str:
    """C# ToUpperInvariant: simple case mapping (no "ß" -> "SS", no ligature expansion, U+0131 unchanged)."""
    if s.isascii():
        return s.upper()
    return "".join(_simple_upper(ch) for ch in s)


def equals_ignore_case(a: str | None, b: str | None) -> bool:
    """C# string.Equals(a, b, StringComparison.OrdinalIgnoreCase)."""
    if a is None or b is None:
        return a is None and b is None
    return to_upper_invariant(a) == to_upper_invariant(b)


def _parse_invariant_decimal(text: str) -> Decimal:
    """C# decimal.Parse(text, CultureInfo.InvariantCulture) for the digit strings matched here (ASCII digits only)."""
    if not text.isascii():
        raise ValueError(f"The input string '{text}' was not in a correct format.")
    return Decimal(text)


def _is_letter_unit(ch: str) -> bool:
    """char.IsLetter on the adjacent UTF-16 unit: for a supplementary-plane character that unit is a surrogate."""
    return ord(ch) <= 0xFFFF and ch.isalpha()


class _IgnoreCaseDict(Generic[V]):
    """Dictionary<string, V>(StringComparer.OrdinalIgnoreCase): first key spelling wins, insertion order is kept."""

    __slots__ = ("_data",)

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, V]] = {}

    def __setitem__(self, key: str, value: V) -> None:
        folded = to_upper_invariant(key)
        existing = self._data.get(folded)
        self._data[folded] = (key if existing is None else existing[0], value)

    def get(self, key: str) -> V | None:
        hit = self._data.get(to_upper_invariant(key))
        return None if hit is None else hit[1]

    def keys(self) -> list[str]:
        return [k for k, _ in self._data.values()]

    def values(self) -> list[V]:
        return [v for _, v in self._data.values()]

    def items(self) -> list[tuple[str, V]]:
        return list(self._data.values())


# ---------------------------------------------------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngredientEntry:
    canonical_name: str
    flags: tuple[str, ...]
    allergens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AdditiveEntry:
    code: str
    name: str
    functional_classes: tuple[str, ...]
    aliases: tuple[str, ...]


_CLASS_TITLES_RAW = {
    "emulsifier": "EMULSIFIER", "emulsifiers": "EMULSIFIER", "emulsifying agent": "EMULSIFIER",
    "raising agent": "RAISING_AGENT", "raising agents": "RAISING_AGENT", "leavening agent": "RAISING_AGENT",
    "leavening agents": "RAISING_AGENT", "leavening": "RAISING_AGENT",
    "colour": "COLOUR", "colours": "COLOUR", "color": "COLOUR", "colors": "COLOUR",
    "permitted synthetic food colour": "COLOUR", "permitted synthetic food colours": "COLOUR",
    "permitted natural colour": "COLOUR", "acidity regulator": "ACIDITY_REGULATOR",
    "acidity regulators": "ACIDITY_REGULATOR", "acidulant": "ACIDITY_REGULATOR", "preservative": "PRESERVATIVE",
    "preservatives": "PRESERVATIVE", "class ii preservative": "PRESERVATIVE", "antioxidant": "ANTIOXIDANT",
    "antioxidants": "ANTIOXIDANT", "stabiliser": "STABILISER", "stabilisers": "STABILISER",
    "stabilizer": "STABILISER", "stabilizers": "STABILISER", "thickener": "THICKENER",
    "thickeners": "THICKENER", "sweetener": "SWEETENER", "sweeteners": "SWEETENER",
    "artificial sweetener": "SWEETENER", "artificial sweeteners": "SWEETENER",
    "flavour enhancer": "FLAVOUR_ENHANCER", "flavour enhancers": "FLAVOUR_ENHANCER",
    "flavor enhancer": "FLAVOUR_ENHANCER", "humectant": "HUMECTANT", "anticaking agent": "ANTICAKING_AGENT",
    "anti-caking agent": "ANTICAKING_AGENT", "firming agent": "FIRMING_AGENT", "glazing agent": "GLAZING_AGENT",
    "flour treatment agent": "FLOUR_TREATMENT_AGENT", "improver": "FLOUR_TREATMENT_AGENT",
}
_CLASS_TITLES = {to_upper_invariant(k): v for k, v in _CLASS_TITLES_RAW.items()}


class IngredientTaxonomy:
    """Ingredient taxonomy and additive master (kb.ingredient, kb.ingredient_alias, kb.additive, kb.additive_alias)."""

    def __init__(self, ingredients: Iterable[tuple[str, IngredientEntry]], additives: Iterable[AdditiveEntry]) -> None:
        self._by_alias: _IgnoreCaseDict[IngredientEntry] = _IgnoreCaseDict()
        self._additive_by_code: _IgnoreCaseDict[AdditiveEntry] = _IgnoreCaseDict()
        self._additive_by_name: _IgnoreCaseDict[AdditiveEntry] = _IgnoreCaseDict()
        for alias, entry in ingredients:
            self._by_alias[IngredientTaxonomy.normalize(alias)] = entry
        # OrderByDescending(Length) is a stable sort; so is sorted(reverse=True).
        self._aliases_longest_first = [(a, utf16_len(a)) for a in sorted(self._by_alias.keys(), key=utf16_len, reverse=True)]
        for a in additives:
            self._additive_by_code[a.code] = a
            self._additive_by_name[IngredientTaxonomy.normalize(a.name)] = a
            for alias in a.aliases:
                self._additive_by_name[IngredientTaxonomy.normalize(alias)] = a
        self._additive_name_patterns = [
            (re.compile("(^|[^a-z])" + re.escape(name) + "([^a-z]|$)"), entry)
            for name, entry in self._additive_by_name.items()
            if utf16_len(name) >= 4
        ]

    def additive_by_code(self, code: str) -> AdditiveEntry | None:
        return self._additive_by_code.get(code)

    @property
    def additives(self) -> tuple[AdditiveEntry, ...]:
        return tuple(self._additive_by_code.values())

    @staticmethod
    def normalize(s: str) -> str:
        t = unicodedata.normalize("NFKC", to_lower_invariant(net_trim(s)))
        t = _WS_RUN_RX.sub(" ", t)
        return t.strip(" .*:-")

    @staticmethod
    def try_class_title(head: str) -> str | None:
        """C# TryClassTitle: the functional class for a class title ("Raising agents" -> RAISING_AGENT), else None."""
        h = IngredientTaxonomy.normalize(head)
        found = _CLASS_TITLES.get(to_upper_invariant(h))
        if found is None and h.endswith("s"):
            found = _CLASS_TITLES.get(to_upper_invariant(h[:-1]))
        return found

    def resolve(self, text: str) -> IngredientEntry | None:
        t = IngredientTaxonomy.normalize(text)
        if not t:
            return None
        e = self._by_alias.get(t)
        if e is not None:
            return e
        if t.endswith("s"):
            e = self._by_alias.get(t[:-1])
            if e is not None:
                return e
        if t.endswith("es"):
            e = self._by_alias.get(t[:-2])
            if e is not None:
                return e
        # Longest known alias contained as whole words, e.g. "refined palm oil" -> "palm oil".
        t_length = utf16_len(t)
        for alias, alias_length in self._aliases_longest_first:
            if alias_length < 3 or alias_length > t_length:
                continue
            idx = t.find(alias)
            if idx < 0:
                continue
            end = idx + len(alias)
            before = idx == 0 or not _is_letter_unit(t[idx - 1])
            after = end == len(t) or not _is_letter_unit(t[end])
            if before and after:
                return self._by_alias.get(alias)
        return None

    def resolve_additive_name(self, text: str) -> AdditiveEntry | None:
        t = IngredientTaxonomy.normalize(text)
        a = self._additive_by_name.get(t)
        if a is not None:
            return a
        for pattern, entry in self._additive_name_patterns:
            if pattern.search(t):
                return entry
        return None


# ---------------------------------------------------------------------------------------------------------------------
# Ingredient parser
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IngredientParseResult:
    tokens: tuple[ParsedIngredient, ...]
    additives: tuple[str, ...]
    flags: AbstractSet[str]
    allergen_hints: AbstractSet[str]
    resolution_ratio: Decimal
    fvln_pct_estimate: Decimal | None
    fvln_from_quid: bool


_PERCENT_RX = re.compile(net_regex(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%"))
_PREFIXED_CODE_RX = re.compile(net_regex(r"\b(?:INS|E)\s*-?\s*(\d{3,4})\s*([a-z])?(?:\s*\(\s*[ivx]+\s*\))?"), re.IGNORECASE)
_BARE_CODE_RX = re.compile(net_regex(r"^\s*(\d{3,4})\s*([a-z])?(?:\s*\(\s*[ivx]+\s*\))?\s*$"), re.IGNORECASE)
_LABEL_PREFIX_RX = re.compile(net_regex(r"^\s*(ingredients?|सामग्री)\s*[:\-]\s*"), re.IGNORECASE)
_SOY_RX = re.compile(r"\bsoy|\bsoya", re.IGNORECASE)
_AND_OR_RX = re.compile(" and/or | & ")
_MSG_CODES = ("621", "627", "631", "635")
_NON_SUGAR_SWEETENERS = frozenset({"950", "951", "952", "954", "955", "960", "961", "962", "969"})
_SYNTHETIC_COLOURS = frozenset({"102", "104", "110", "122", "123", "124", "127", "129", "132", "133", "142", "143", "151", "155", "171"})


def _code_of(m: re.Match[str]) -> str:
    return m.group(1) + to_lower_invariant(m.group(2) or "")


def _split_top_level(text: str) -> Iterator[str]:
    depth = 0
    sb: list[str] = []
    for ch in text:
        if ch == "(":
            depth += 1
        if ch == ")":
            depth = max(0, depth - 1)
        if (ch == "," or ch == ";") and depth == 0:
            piece = net_trim("".join(sb))
            if piece:
                yield piece
            sb.clear()
            continue
        sb.append(ch)
    piece = net_trim("".join(sb))
    if piece:
        yield piece


def _split_head(token: str) -> tuple[str, list[str]]:
    groups: list[str] = []
    head: list[str] = []
    current: list[str] = []
    depth = 0
    for ch in token:
        if ch == "(":
            if depth > 0:
                current.append(ch)
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
            if depth == 0:
                groups.append(net_trim("".join(current)))
                current.clear()
            else:
                current.append(ch)
        elif depth == 0:
            head.append(ch)
        else:
            current.append(ch)
    if current:
        groups.append(net_trim("".join(current)))
    return net_trim(_WS_RUN_RX.sub(" ", "".join(head))), [g for g in groups if g]


def _try_class_title_with_inline_code(head: str) -> tuple[str, str] | None:
    """"Colour INS 150d" -> ("COLOUR", "150d"); None when the text before the code is not a class title."""
    m = _PREFIXED_CODE_RX.search(head)
    if m is None:
        return None
    fclass = IngredientTaxonomy.try_class_title(net_trim(head[: m.start()]))
    return None if fclass is None else (fclass, _code_of(m))


class _TokenWriter:
    """The C# `ref int position` and output list of one Parse call."""

    def __init__(self, taxonomy: IngredientTaxonomy) -> None:
        self.tax = taxonomy
        self.output: list[ParsedIngredient] = []
        self.position = 0

    def next_position(self) -> int:
        self.position += 1
        return self.position

    def parse_list(self, text: str, parent: int | None) -> None:
        for part in _split_top_level(text):
            for piece in _AND_OR_RX.split(part):
                piece = net_trim(piece)
                if piece:
                    self.parse_token(piece, parent)

    def parse_token(self, token: str, parent: int | None) -> None:
        head, groups = _split_head(token)
        if not head and not groups:
            return
        percent: Decimal | None = None
        pm = _PERCENT_RX.search(head)
        if pm is not None:
            percent = _parse_invariant_decimal(pm.group(1))
            head = net_trim(_PERCENT_RX.sub("", head))

        # Class title: every member of the bracket group is an additive code or additive name.
        inline = _try_class_title_with_inline_code(head)
        fclass, inline_code = inline if inline is not None else (IngredientTaxonomy.try_class_title(head), None)
        if fclass is not None:
            class_pos = self.next_position()
            self.output.append(ParsedIngredient(class_pos, parent, "class:" + fclass, None, None, True, None, ()))
            if inline_code is not None:
                self.add_additive(inline_code, head, class_pos)
            for g in groups:
                for member in _split_top_level(g):
                    code = IngredientParser.extract_code(member)
                    if code is not None:
                        self.add_additive(code, member, class_pos)
                        continue
                    by_name = self.tax.resolve_additive_name(member)
                    if by_name is not None:
                        self.add_additive(by_name.code, member, class_pos)
                    else:
                        self.output.append(ParsedIngredient(self.next_position(), class_pos, net_trim(member), None, None, True, None, ()))
            return

        # Plain ingredient, possibly with an alias, percentage, additive code or sub-ingredient list in brackets.
        head_code = IngredientParser.extract_code(head)
        additive_by_name = self.tax.resolve_additive_name(head) if head_code is None else None
        entry = self.tax.resolve(head)
        my_pos = self.next_position()
        sub_lists: list[str] = []
        for g in groups:
            gp = _PERCENT_RX.search(g)
            if gp is not None and not net_trim(_PERCENT_RX.sub("", g)):
                if percent is None:
                    percent = _parse_invariant_decimal(gp.group(1))
                continue
            code = IngredientParser.extract_code(g)
            if code is not None and "," not in g:
                if head_code is None:
                    head_code = code
                continue
            if entry is None and "," not in g:
                alias_entry = self.tax.resolve(g)
                if alias_entry is not None:
                    entry = alias_entry
                    continue
            sub_lists.append(g)

        flags = entry.flags if entry is not None else ()
        if head_code is None and additive_by_name is not None and entry is None:
            head_code = additive_by_name.code
        if head_code is not None:
            a = self.tax.additive_by_code(head_code)
            name = a.name if a is not None else (entry.canonical_name if entry is not None else None)
            self.output.append(ParsedIngredient(my_pos, parent, head, name, percent, True, head_code, flags))
        else:
            name = entry.canonical_name if entry is not None else None
            self.output.append(ParsedIngredient(my_pos, parent, head, name, percent, False, None, flags))
        for sub in sub_lists:
            self.parse_list(sub, my_pos)

    def add_additive(self, code: str, raw: str, parent: int) -> None:
        a = self.tax.additive_by_code(code)
        self.output.append(ParsedIngredient(self.next_position(), parent, net_trim(raw), a.name if a is not None else None, None, True, code, ()))


class IngredientParser:
    """
    Deterministic ingredient tokenizer (doc 02 7.2): nested brackets, percentages (QUID), class titles with INS numbers
    ("Raising agents (INS 500(ii), 503(ii))"), E-numbers and US additive names ("Red 40").
    """

    @staticmethod
    def parse(text: str | None, taxonomy: IngredientTaxonomy) -> IngredientParseResult:
        if text is None or is_null_or_white_space(text):
            return IngredientParseResult((), (), OrderedFrozenSet(), OrderedFrozenSet(), Decimal(0), None, False)

        cleaned = text.replace("[", "(").replace("]", ")").replace("{", "(").replace("}", ")")
        cleaned = _LABEL_PREFIX_RX.sub("", cleaned)
        cleaned = cleaned.rstrip(". ")
        writer = _TokenWriter(taxonomy)
        writer.parse_list(cleaned, None)
        tokens = writer.output

        additives = tuple(dict.fromkeys(t.additive_code for t in tokens if t.additive_code is not None))
        flags = dict.fromkeys(f for t in tokens for f in t.flags)
        allergen_hints: dict[str, None] = {}
        for t in tokens:
            if t.canonical_name is not None:
                e = taxonomy.resolve(t.canonical_name)
                if e is not None:
                    allergen_hints.update(dict.fromkeys(e.allergens))
        for code in additives:
            a = taxonomy.additive_by_code(code)
            if a is None:
                continue
            if "SWEETENER" in a.functional_classes and IngredientParser.is_non_sugar_sweetener(code):
                flags["NNS"] = None
            if "COLOUR" in a.functional_classes and IngredientParser.is_synthetic_colour(code):
                flags["ARTIFICIAL_COLOUR"] = None
            if code in _MSG_CODES:
                flags["ADDED_MSG"] = None
        # Lecithin (INS 322) implies soy only when the label says so (it may be sunflower lecithin).
        if any(t.additive_code == "322" and _SOY_RX.search(t.raw_text) for t in tokens):
            allergen_hints["SOY"] = None

        countable = [t for t in tokens if not t.raw_text.startswith("class:")]
        resolved = sum(1 for t in countable if t.canonical_name is not None or t.additive_code is not None)
        ratio = Decimal(0) if not countable else round_decimal(Decimal(resolved) / Decimal(len(countable)), 3)

        # Fruit/vegetable/legume/nut share: QUID when declared, else a deterministic position-based estimate.
        fvl: Decimal | None = None
        from_quid = False
        fvl_tokens = [t for t in tokens if t.parent_position is None and "FRUIT_VEG_LEGUME" in t.flags]
        if fvl_tokens:
            if all(t.percent is not None for t in fvl_tokens):
                fvl = decimal_min(Decimal(100), sum((t.percent for t in fvl_tokens if t.percent is not None), Decimal(0)))
                from_quid = True
            else:
                top = [t.position for t in tokens if t.parent_position is None]
                total = Decimal(0)
                for t in fvl_tokens:
                    if t.percent is not None:
                        total += t.percent
                        continue
                    rank = top.index(t.position) if t.position in top else -1
                    total += {0: Decimal(40), 1: Decimal(20), 2: Decimal(10)}.get(rank, Decimal(5))
                fvl = decimal_min(Decimal(100), total)
        return IngredientParseResult(tuple(tokens), additives, OrderedFrozenSet(flags), OrderedFrozenSet(allergen_hints), ratio, fvl, from_quid)

    @staticmethod
    def is_non_sugar_sweetener(code: str) -> bool:
        return code in _NON_SUGAR_SWEETENERS

    @staticmethod
    def is_synthetic_colour(code: str) -> bool:
        return code in _SYNTHETIC_COLOURS

    @staticmethod
    def extract_code(text: str) -> str | None:
        """"INS 322(i)" -> "322", "E150d" -> "150d", "500(ii)" -> "500"."""
        m = _PREFIXED_CODE_RX.search(text)
        if m is not None:
            return _code_of(m)
        m = _BARE_CODE_RX.search(text)
        return _code_of(m) if m is not None else None


# ---------------------------------------------------------------------------------------------------------------------
# Allergens
# ---------------------------------------------------------------------------------------------------------------------

_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("tree nuts", ("TREE_NUTS",)), ("tree nut", ("TREE_NUTS",)), ("peanuts", ("PEANUTS",)), ("peanut", ("PEANUTS",)),
    ("groundnut", ("PEANUTS",)), ("groundnuts", ("PEANUTS",)), ("nuts", ("TREE_NUTS",)), ("nut", ("TREE_NUTS",)), ("almond", ("TREE_NUTS",)),
    ("almonds", ("TREE_NUTS",)), ("cashew", ("TREE_NUTS",)), ("cashews", ("TREE_NUTS",)), ("walnuts", ("TREE_NUTS",)), ("pistachios", ("TREE_NUTS",)),
    ("wheat", ("WHEAT", "GLUTEN_CEREALS")), ("gluten", ("GLUTEN_CEREALS",)),
    ("barley", ("GLUTEN_CEREALS",)), ("rye", ("GLUTEN_CEREALS",)), ("oats", ("GLUTEN_CEREALS",)),
    ("milk", ("MILK",)), ("dairy", ("MILK",)), ("lactose", ("MILK",)), ("egg", ("EGGS",)), ("eggs", ("EGGS",)),
    ("soya", ("SOY",)), ("soy", ("SOY",)), ("soybean", ("SOY",)), ("sesame", ("SESAME",)), ("fish", ("FISH",)),
    ("crustacean", ("CRUSTACEANS",)), ("crustaceans", ("CRUSTACEANS",)), ("shellfish", ("CRUSTACEANS",)),
    ("shrimp", ("CRUSTACEANS",)), ("prawn", ("CRUSTACEANS",)), ("mollusc", ("MOLLUSCS",)), ("molluscs", ("MOLLUSCS",)),
    ("mustard", ("MUSTARD",)), ("celery", ("CELERY",)), ("lupin", ("LUPIN",)), ("sulphite", ("SULPHITES",)),
    ("sulphites", ("SULPHITES",)), ("sulfite", ("SULPHITES",)), ("sulfites", ("SULPHITES",)),
    ("sulphur dioxide", ("SULPHITES",)),
)
_KEYWORD_RXS = tuple((keyword, codes, re.compile("[^a-z]" + re.escape(keyword) + "[^a-z]")) for keyword, codes in _KEYWORDS)

_MAY_CONTAIN_RX = re.compile(net_regex(r"may\s+contain(?:\s+traces\s+of)?\s*:?\s*(?P<list>[^.;]+)"), re.IGNORECASE)
_FACILITY_RX = re.compile(
    net_regex(
        r"(?:processed|manufactured|made|packed|produced)\s+(?:in|on)\s+(?:a\s+)?(?:facility|plant|equipment|line|premises)s?\s+"
        r"(?:that|which)\s+(?:also\s+)?(?:processes|handles|uses|packs)\s+(?P<list>[^.;]+)"
    ),
    re.IGNORECASE,
)
_CONTAINS_RX = re.compile(net_regex(r"(?<!may\s)contains?\s*:?\s*(?P<list>[^.;]+)"), re.IGNORECASE)
_NUTS_RX = re.compile(net_regex(r"(^|[^a-z])(tree\s)?nuts?([^a-z]|$)"))


class AllergenDeriver:
    """Deterministic allergen derivation from statements and ingredients (doc 02 7.2). LLMs never decide allergens."""

    @staticmethod
    def from_statement(statement: str | None) -> dict[str, AllergenPresence]:
        result: dict[str, AllergenPresence] = {}
        if statement is None or is_null_or_white_space(statement):
            return result
        s = statement.replace("\n", " ")
        for m in _MAY_CONTAIN_RX.finditer(s):
            AllergenDeriver._add(result, m.group("list"), AllergenPresence.MAY_CONTAIN)
        for m in _FACILITY_RX.finditer(s):
            AllergenDeriver._add(result, m.group("list"), AllergenPresence.MAY_CONTAIN)
        without_may = _MAY_CONTAIN_RX.sub(" ", s)
        without_may = _FACILITY_RX.sub(" ", without_may)
        for m in _CONTAINS_RX.finditer(without_may):
            AllergenDeriver._add(result, m.group("list"), AllergenPresence.CONTAINS)
        return result

    @staticmethod
    def reconcile(
        from_statement: Mapping[str, AllergenPresence], from_ingredients: Iterable[str]
    ) -> tuple[dict[str, AllergenPresence], tuple[str, ...]]:
        """Conservative union: CONTAINS from statement or ingredients wins over MAY_CONTAIN. Returns (merged, undeclared)."""
        result = dict(from_statement)
        missing: list[str] = []
        for code in dict.fromkeys(from_ingredients):
            if from_statement.get(code) != AllergenPresence.CONTAINS:
                missing.append(code)
            result[code] = AllergenPresence.CONTAINS
        return result, tuple(missing)

    @staticmethod
    def _add(result: dict[str, AllergenPresence], items: str, presence: AllergenPresence) -> None:
        lower = " " + to_lower_invariant(items) + " "
        for keyword, codes, rx in _KEYWORD_RXS:
            if not rx.search(lower):
                continue
            # "nuts" inside "peanuts" or "tree nuts" must not double count.
            if keyword in ("nuts", "nut") and ("peanut" in lower and not _NUTS_RX.search(lower)):
                continue
            for code in codes:
                if result.get(code) == AllergenPresence.CONTAINS:
                    continue
                result[code] = presence


# ---------------------------------------------------------------------------------------------------------------------
# Diet and NOVA
# ---------------------------------------------------------------------------------------------------------------------


class DietClassifier:
    """Diet classification from regulatory marks and ingredients; Jain-friendly is inferred only (doc 02 7.2)."""

    @dataclass(frozen=True, slots=True)
    class Result:
        diet_class: str
        jain_friendly_inferred: bool
        veg_mark_contradicted: bool

    @staticmethod
    def classify(marks: Collection[str], flags: AbstractSet[str], allergens: Mapping[str, AllergenPresence]) -> DietClassifier.Result:
        meat = (
            "MEAT" in flags or "FISH_MEAT" in flags or "GELATIN" in flags
            or allergens.get("FISH") == AllergenPresence.CONTAINS
            or allergens.get("CRUSTACEANS") == AllergenPresence.CONTAINS
        )
        egg = "EGG" in flags or allergens.get("EGGS") == AllergenPresence.CONTAINS
        animal_other = "DAIRY" in flags or "HONEY" in flags or allergens.get("MILK") == AllergenPresence.CONTAINS
        veg_mark = "IN_FSSAI_VEG" in marks or "IN_FSSAI_VEGAN" in marks

        if "IN_FSSAI_NON_VEG" in marks or meat:
            diet = "NON_VEG"
        elif egg:
            diet = "EGG"
        # Vegan only from the regulatory/certification mark: an absence of animal ingredients is not proof.
        elif ("IN_FSSAI_VEGAN" in marks or "VEGAN_CERTIFIED" in marks) and not animal_other:
            diet = "VEGAN"
        elif veg_mark or len(flags) > 0:
            diet = "VEG"
        else:
            diet = "UNKNOWN"

        jain = diet in ("VEG", "VEGAN") and "ROOT_VEG" not in flags and "ONION_GARLIC" not in flags and "HONEY" not in flags
        return DietClassifier.Result(diet, jain, veg_mark and (meat or egg))


_ULTRA_PROCESSED_CLASSES = frozenset({"COLOUR", "SWEETENER", "FLAVOUR_ENHANCER", "EMULSIFIER", "THICKENER", "STABILISER", "GLAZING_AGENT", "HUMECTANT"})


class NovaClassifier:
    """NOVA group from ingredient markers (doc 03 2.2). Returns None (excluded from HSI) when it cannot be determined."""

    @staticmethod
    def classify(parsed: IngredientParseResult, taxonomy: IngredientTaxonomy) -> tuple[int | None, Decimal]:
        top = [t for t in parsed.tokens if t.parent_position is None and not t.raw_text.startswith("class:")]
        if not top and not parsed.additives:
            return None, Decimal(0)
        confidence = round_decimal(decimal_min(Decimal(1), parsed.resolution_ratio), 3)
        if parsed.resolution_ratio < Decimal("0.5"):
            return None, confidence

        def ultra_processed_additive(code: str) -> bool:
            a = taxonomy.additive_by_code(code)
            return a is not None and any(c in _ULTRA_PROCESSED_CLASSES for c in a.functional_classes)

        upf = (
            "UPF_MARKER" in parsed.flags or "PHO" in parsed.flags or "NNS" in parsed.flags
            or any(ultra_processed_additive(code) for code in parsed.additives)
        )
        if upf:
            return 4, confidence

        culinary = sum(1 for t in top if "CULINARY" in t.flags)
        processed_markers = "ADDED_SUGAR" in parsed.flags or "SALT" in parsed.flags or "OIL" in parsed.flags or len(parsed.additives) > 0
        if len(top) == 1 and culinary == 1:
            return 2, confidence
        if not processed_markers and len(top) <= 2:
            return 1, confidence
        return 3, confidence
