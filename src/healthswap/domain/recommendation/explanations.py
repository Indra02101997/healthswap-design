"""Comparison fact sheets, claim-safe templates (en, hi, es) and the validator chain for LLM-polished copy (Recommendation/Explanations.cs).

Text lengths and truncation count UTF-16 code units like C# string.Length. Numbers are formatted like the C# invariant
culture: decimal.ToString() keeps the scale, the custom formats "0", "0.0" and "0.####" round half away from zero, and no
format prints "-0". The validator reproduces the .NET regular expressions it uses: \\d is any Unicode decimal digit,
[^\\p{L}] is a UTF-16 unit that is not a letter, and \\b uses the .NET word characters (letters, Mn, Nd, Pc, ZWNJ/ZWJ);
a supplementary-plane character is a surrogate pair, i.e. two units that are neither letters nor word characters.
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Context, Decimal

from healthswap.domain.knowledge.parsing import to_lower_invariant, to_upper_invariant, utf16_len
from healthswap.domain.knowledge.products import decimal_div, decimal_min, round_decimal
from healthswap.domain.scoring.health_profiles import AdditiveDetail, HealthProfileData, MarketPack

_ZERO = Decimal(0)
_ONE = Decimal(1)
_TEN = Decimal(10)
_HUNDRED = Decimal(100)
# C# multiplies these decimals exactly; Python's default 28-digit context could round a 29-digit operand.
_WIDE = Context(prec=100)
_DECIMAL_MAX = Decimal("79228162514264337593543950335")
_LONE_SURROGATE_RX = re.compile("[\ud800-\udfff]")


def _ordinal(s: str) -> bytes:
    """Sort key of StringComparer.Ordinal (UTF-16 code units)."""
    return s.encode("utf-16-be", "surrogatepass")


def _utf16_prefix(s: str, units: int) -> str:
    """C# s[..units]: the first `units` UTF-16 code units (may split a surrogate pair, like C#)."""
    if s.isascii() or max(s) <= "\uffff":
        return s[:units]
    return s.encode("utf-16-le", "surrogatepass")[: 2 * units].decode("utf-16-le", "surrogatepass")


def _cs(value: Decimal) -> str:
    """C# decimal.ToString(CultureInfo.InvariantCulture): plain notation, scale kept, never "-0"."""
    return format(abs(value) if value.is_zero() else value, "f")


def _quantize(value: Decimal, places: int) -> Decimal:
    return value.quantize(_ONE.scaleb(-places), rounding=ROUND_HALF_UP, context=_WIDE)


def _fixed(value: Decimal, places: int) -> str:
    """C# ToString("0") (places 0) / ToString("0.0") (places 1), invariant culture."""
    q = _quantize(value, places)
    return format(abs(q) if q.is_zero() else q, "f")


def _optional_digits(value: Decimal, places: int) -> str:
    """C# ToString("0.####") (places 4), invariant culture: trailing zeros dropped."""
    q = _quantize(value, places)
    if q.is_zero():
        return "0"
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


# ---------------------------------------------------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class Fact:
    """One deterministic, verifiable comparison fact (doc 03 section 8). `from_` is C# From (`from` is a keyword)."""

    id: str
    # NUTRIENT, ADDITIVE_REMOVED, ADDITIVE_INTRODUCED or PROCESSING.
    kind: str
    nutrient: str | None = None
    from_: Decimal = _ZERO
    to: Decimal = _ZERO
    unit: str = "g"
    basis: str = "PER_100G"
    rel_change: Decimal | None = None
    direction: str = "SAME"
    impact: Decimal = _ZERO
    claim_eligible: bool = False
    must_disclose: bool = False
    codes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class FactSheet:
    basis: str
    facts: tuple[Fact, ...]
    hash: str


# (key, API name, unit, role, health weight)
_NUTRIENTS: tuple[tuple[str, str, str, str, Decimal], ...] = (
    ("LIMITED_SUGARS_G", "totalSugarsG", "g", "LIMIT", Decimal("1.0")),
    ("SODIUM_MG", "sodiumMg", "mg", "LIMIT", Decimal("0.9")),
    ("SAT_FAT_G", "saturatedFatG", "g", "LIMIT", Decimal("0.9")),
    ("TRANS_FAT_G", "transFatG", "g", "LIMIT", Decimal("1.0")),
    ("FIBRE_G", "dietaryFibreG", "g", "ENCOURAGE", Decimal("0.6")),
    ("PROTEIN_G", "proteinG", "g", "ENCOURAGE", Decimal("0.4")),
)
_MINIMUM_DIFFERENCES = {
    "LIMITED_SUGARS_G": Decimal("1.0"),
    "SODIUM_MG": Decimal(40),
    "SAT_FAT_G": Decimal("0.5"),
    "TRANS_FAT_G": Decimal("0.2"),
    "FIBRE_G": Decimal("1.0"),
    "PROTEIN_G": Decimal("2.0"),
}
_NUTRIENT_CODES = {
    "LIMITED_SUGARS_G": "SUGAR",
    "SODIUM_MG": "NA",
    "SAT_FAT_G": "FASAT",
    "TRANS_FAT_G": "FATRN",
    "FIBRE_G": "FIBTG",
    "PROTEIN_G": "PROCNT",
}


class FactSheets:
    @staticmethod
    def build(a: HealthProfileData, b: HealthProfileData, pack: MarketPack) -> FactSheet:
        """Facts for swapping `a` (original) to `b` (alternative), per 100 g/ml of the alternative's basis."""
        basis = "PER_100ML" if b.nutrition.is_liquid else "PER_100G"
        facts: list[Fact] = []
        i = 0
        use_added = a.nutrition.added_sugars_g is not None and b.nutrition.added_sugars_g is not None
        for key, api, unit, role, weight in _NUTRIENTS:
            k = "SUGARS_G" if key == "LIMITED_SUGARS_G" and not use_added else key
            api_name = "addedSugarsG" if key == "LIMITED_SUGARS_G" and use_added else api
            av = a.nutrition.get(k)
            bv = b.nutrition.get(k)
            if av is None or bv is None:
                continue
            av = FactSheets.round(av, unit)
            bv = FactSheets.round(bv, unit)
            if av == bv:
                continue
            rel = None if av == 0 else round_decimal(decimal_div(bv - av, av), 4)
            better = bv < av if role == "LIMIT" else bv > av
            threshold = pack.comparative_threshold(_NUTRIENT_CODES.get(key, key))
            # Differences below a meaningful absolute amount never become claims or headline facts (no "100% less trans fat" for 0.1 g).
            material = abs(bv - av) >= FactSheets.minimum_difference(key)
            claim_eligible = material and rel is not None and abs(rel) >= threshold and better
            impact = weight * decimal_min(Decimal(3), abs(rel if rel is not None else _ONE)) if better and material else _ZERO
            i += 1
            facts.append(
                Fact(
                    id=f"f{i}", kind="NUTRIENT", nutrient=api_name, from_=av, to=bv, unit=unit, basis=basis, rel_change=rel,
                    direction="BETTER" if better else "WORSE", impact=round_decimal(impact, 4), claim_eligible=claim_eligible,
                )
            )

        a_colours = {x.code for x in a.additives if _is_colour(x)}
        b_colours = {x.code for x in b.additives if _is_colour(x)}
        removed_colours = sorted(a_colours - b_colours, key=_ordinal)
        if removed_colours and not b_colours:
            i += 1
            facts.append(
                Fact(
                    id=f"f{i}", kind="ADDITIVE_REMOVED", nutrient="artificialColours", codes=tuple(removed_colours), direction="BETTER",
                    impact=Decimal("0.5"), basis=basis,
                )
            )

        a_codes = {x.code for x in a.additives}
        introduced = sorted((x.code for x in b.additives if x.tier in ("B", "C") and x.code not in a_codes), key=_ordinal)
        if introduced:
            sweetener = any(x.code in introduced and x.functional_class == "SWEETENER" for x in b.additives)
            i += 1
            facts.append(
                Fact(
                    id=f"f{i}", kind="ADDITIVE_INTRODUCED", nutrient="sweeteners" if sweetener else "additives", codes=tuple(introduced),
                    direction="WORSE", must_disclose=True, impact=_ZERO, basis=basis,
                )
            )
        na, nb = a.nova, b.nova
        if na is not None and nb is not None and nb < na:
            i += 1
            facts.append(
                Fact(
                    id=f"f{i}", kind="PROCESSING", nutrient="nova", from_=Decimal(na), to=Decimal(nb), unit="", direction="BETTER",
                    impact=Decimal("0.3") * (na - nb), basis=basis,
                )
            )

        return FactSheet(basis, tuple(facts), _hash(basis, facts))

    @staticmethod
    def select(sheet: FactSheet, max_count: int = 2) -> list[Fact]:
        """Top facts by health impact plus every must-disclose fact (doc 03 8.2)."""
        better = [f for f in sheet.facts if f.direction == "BETTER" and f.impact > 0]
        better.sort(key=lambda f: _ordinal(f.id))
        better.sort(key=lambda f: f.impact, reverse=True)
        return better[: max(max_count, 0)] + [f for f in sheet.facts if f.must_disclose]

    @staticmethod
    def minimum_difference(key: str) -> Decimal:
        """Smallest per-100 g/ml difference worth mentioning (below typical label rounding and analytical tolerance)."""
        return _MINIMUM_DIFFERENCES.get(key, _ZERO)

    @staticmethod
    def round(v: Decimal, unit: str) -> Decimal:
        return round_decimal(v, 0) if unit == "mg" else round_decimal(v, 1)


def _is_colour(a: AdditiveDetail) -> bool:
    return a.functional_class == "COLOUR" and a.tier in ("B", "C")


def _hash(basis: str, facts: Iterable[Fact]) -> str:
    canonical = basis + "|" + "|".join(f"{f.kind}:{f.nutrient or ''}:{_cs(f.from_)}:{_cs(f.to)}:{','.join(f.codes)}" for f in facts)
    # Encoding.UTF8 replaces lone surrogates with U+FFFD.
    return hashlib.sha256(_LONE_SURROGATE_RX.sub("\ufffd", canonical).encode("utf-8")).hexdigest()[:32]


# ---------------------------------------------------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderedExplanation:
    short: str
    bullets: tuple[str, ...]
    fact_ids_used: tuple[str, ...]
    generator: str
    ai_generated: bool
    locale: str


_NUTRIENT_NAMES: dict[str, dict[str, str]] = {
    "en": {
        "totalSugarsG": "sugar", "addedSugarsG": "added sugar", "sodiumMg": "sodium", "saturatedFatG": "saturated fat",
        "transFatG": "trans fat", "dietaryFibreG": "fibre", "proteinG": "protein",
    },
    "hi": {
        "totalSugarsG": "चीनी", "addedSugarsG": "अतिरिक्त चीनी", "sodiumMg": "सोडियम", "saturatedFatG": "संतृप्त वसा",
        "transFatG": "ट्रांस वसा", "dietaryFibreG": "फाइबर", "proteinG": "प्रोटीन",
    },
    "es": {
        "totalSugarsG": "azúcar", "addedSugarsG": "azúcar añadido", "sodiumMg": "sodio", "saturatedFatG": "grasa saturada",
        "transFatG": "grasa trans", "dietaryFibreG": "fibra", "proteinG": "proteína",
    },
}


class ExplanationTemplates:
    """Deterministic, claim-safe templates (en-IN, en-US, hi-IN, es-US). Claim wording only above thresholds."""

    @staticmethod
    def lang(locale: str) -> str:
        head = locale.split("-")[0]
        return head if head in ("hi", "es") else "en"

    @staticmethod
    def render(selected: Sequence[Fact], locale: str) -> RenderedExplanation:
        lang = ExplanationTemplates.lang(locale)
        sentences = [(f.id, ExplanationTemplates.sentence(f, lang, locale)) for f in selected]
        short_text = " · ".join(text for _, text in sentences)
        if utf16_len(short_text) > 90:
            short_text = sentences[0][1]
        if utf16_len(short_text) > 90:
            short_text = _utf16_prefix(short_text, 87) + "..."
        bullets = tuple(text if utf16_len(text) <= 80 else _utf16_prefix(text, 77) + "..." for _, text in sentences[:3])
        return RenderedExplanation(short_text, bullets, tuple(fid for fid, _ in sentences), "TEMPLATE", False, locale)

    @staticmethod
    def sentence(f: Fact, lang: str, locale: str) -> str:
        per = "100 ml" if f.basis == "PER_100ML" else "100 g"
        per_hi = "100 मिली" if f.basis == "PER_100ML" else "100 ग्राम"
        codes = ", ".join(f.codes)
        if f.kind == "ADDITIVE_REMOVED":
            if lang == "hi":
                return f"कोई कृत्रिम रंग नहीं (INS {codes} हटाए गए)"
            if lang == "es":
                return f"Sin colorantes artificiales (INS {codes})"
            return f"No artificial colours (INS {codes})"
        if f.kind == "ADDITIVE_INTRODUCED":
            sweet = f.nutrient == "sweeteners"
            if lang == "hi":
                return f"स्वीटनर शामिल है (INS {codes})" if sweet else f"योजक शामिल है (INS {codes})"
            if lang == "es":
                return f"Contiene edulcorante (INS {codes})" if sweet else f"Contiene aditivo (INS {codes})"
            return f"Contains sweetener (INS {codes})" if sweet else f"Contains additive (INS {codes})"
        if f.kind == "PROCESSING":
            if lang == "hi":
                return f"कम प्रोसेस्ड (NOVA {_n(f.to)} बनाम {_n(f.from_)})"
            if lang == "es":
                return f"Menos procesado (NOVA {_n(f.to)} vs {_n(f.from_)})"
            return f"Less processed (NOVA {_n(f.to)} vs {_n(f.from_)})"

        nutrient = f.nutrient if f.nutrient is not None else ""
        name = _NUTRIENT_NAMES[lang].get(nutrient, nutrient)
        unit_hi = "मि.ग्रा." if f.unit == "mg" else "ग्राम"
        from_text = ExplanationTemplates.v(f.from_, f.unit)
        to_text = ExplanationTemplates.v(f.to, f.unit)
        rel = f.rel_change
        if f.claim_eligible and rel is not None:
            pct = ExplanationTemplates.pct(rel)
            if rel < 0:
                if lang == "hi":
                    return f"प्रति {per_hi} {pct}% कम {name}"
                if lang == "es":
                    return f"{pct}% menos {name} por {per}"
                return f"{pct}% less {name} per {per}"
            mult = ExplanationTemplates.multiplier(f.from_, f.to)
            if mult is not None:
                if lang == "hi":
                    return f"प्रति {per_hi} {mult} गुना {name} ({to_text} बनाम {from_text} {unit_hi})"
                if lang == "es":
                    return f"{mult} veces la {name} por {per} ({to_text} g vs {from_text} g)"
                return f"{mult}x the {name} per {per} ({to_text} {f.unit} vs {from_text} {f.unit})"
            if lang == "hi":
                return f"प्रति {per_hi} {pct}% अधिक {name}"
            if lang == "es":
                return f"{pct}% más {name} por {per}"
            return f"{pct}% more {name} per {per}"
        if lang == "hi":
            return f"प्रति {per_hi} {name}: {to_text} बनाम {from_text} {unit_hi}"
        if lang == "es":
            return f"{to_text} {f.unit} vs {from_text} {f.unit} de {name} por {per}"
        return f"{to_text} {f.unit} vs {from_text} {f.unit} {name} per {per}"

    @staticmethod
    def pct(rel: Decimal) -> int:
        """Percent is floored so the copy never overstates the difference."""
        return math.floor(_WIDE.multiply(rel.copy_abs(), _HUNDRED))

    @staticmethod
    def multiplier(from_: Decimal, to: Decimal) -> str | None:
        if from_ <= 0 or decimal_div(to, from_) < 2:
            return None
        m = decimal_div(_WIDE.multiply(decimal_div(to, from_), _TEN).to_integral_value(rounding=ROUND_FLOOR), _TEN)
        if m == m.to_integral_value(rounding=ROUND_FLOOR):
            return str(int(m))
        return _fixed(m, 1)

    @staticmethod
    def v(v: Decimal, unit: str) -> str:
        """A fact value as printed in copy: whole milligrams, grams with one decimal."""
        return _fixed(round_decimal(v, 0), 0) if unit == "mg" else _fixed(round_decimal(v, 1), 1)


def _n(v: Decimal) -> str:
    """C# ((int)v).ToString(): truncation toward zero."""
    return str(int(v))


# ---------------------------------------------------------------------------------------------------------------------
# Validator
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ValidationReport:
    passed: bool
    checks: Mapping[str, bool]
    problems: tuple[str, ...]


_NUMBER_RX = re.compile(r"\d+(?:[.,]\d+)?")
# decimal.TryParse(n, NumberStyles.Number, InvariantCulture) for the strings the validator canonicalizes: ASCII digits,
# "," group separators after the first digit, an optional fraction and a leading sign.
_CANON_RX = re.compile(r"([+-]?)([0-9][0-9,]*)(?:\.([0-9]*))?")
_NET_WORD_CATEGORIES = frozenset({"Lu", "Ll", "Lt", "Lm", "Lo", "Mn", "Nd", "Pc"})
_SPANISH_WORDS = ("por", "menos", "más", "sin", "contiene", "veces", "de", "procesado")


def _canon(n: str) -> str:
    m = _CANON_RX.fullmatch(n)
    if m is None:
        return n
    text = m[1] + m[2].replace(",", "") + ("." + m[3] if m[3] else "")
    d = Decimal(text)
    if abs(d) > _DECIMAL_MAX:
        return n
    return _optional_digits(d, 4)


def _is_letter_unit(ch: str) -> bool:
    """char.IsLetter / \\p{L} on a UTF-16 unit (a supplementary character is never a letter unit)."""
    return ord(ch) <= 0xFFFF and ch.isalpha()


def _is_word_unit(ch: str) -> bool:
    """.NET regex word character (RegexCharClass.IsBoundaryWordChar)."""
    return ch in "\u200c\u200d" or (ord(ch) <= 0xFFFF and unicodedata.category(ch) in _NET_WORD_CATEGORIES)


def _contains_term(text: str, term: str) -> bool:
    """Regex.IsMatch(text, $"(^|[^\\p{L}]){Regex.Escape(term)}([^\\p{L}]|$)")."""
    start = text.find(term)
    while start >= 0:
        end = start + len(term)
        if (start == 0 or not _is_letter_unit(text[start - 1])) and (end == len(text) or not _is_letter_unit(text[end])):
            return True
        start = text.find(term, start + 1)
    return False


def _has_word(text: str, word: str) -> bool:
    """Regex.IsMatch(text, $@"\\b{word}\\b") for a word that starts and ends with a letter."""
    start = text.find(word)
    while start >= 0:
        end = start + len(word)
        if (start == 0 or not _is_word_unit(text[start - 1])) and (end == len(text) or not _is_word_unit(text[end])):
            return True
        start = text.find(word, start + 1)
    return False


class ExplanationValidator:
    """
    Validator chain for LLM-polished explanations (doc 03 9.3): schema/length, numeric fidelity (every number must come
    from the facts), banned claims per locale, language ID, and mandatory disclosures. Any failure falls back to the template.
    """

    BANNED_TERMS: Mapping[str, tuple[str, ...]] = {
        "en": (
            "cure", "cures", "prevent", "prevents", "treat", "treats", "treatment", "diabetic-safe", "diabetic", "doctor recommended",
            "detox", "immunity booster", "boosts immunity", "chemical-free", "toxic", "poison", "guilt-free", "healthiest",
            "100% healthy", "clinically proven", "superfood", "miracle",
        ),
        "hi": ("इलाज", "रोकथाम", "डिटॉक्स", "रोग प्रतिरोधक", "ज़हर", "जहर", "विषैला", "सबसे स्वस्थ", "मधुमेह", "डॉक्टर द्वारा", "चमत्कार", "केमिकल-मुक्त"),
        "es": (
            "cura", "curar", "previene", "prevenir", "tratamiento", "desintoxica", "detox", "inmunidad", "tóxico", "veneno",
            "sin culpa", "el más saludable", "clínicamente probado", "milagro",
        ),
    }

    @staticmethod
    def validate(
        short_text: str, bullets: Sequence[str], facts: Sequence[Fact], locale: str, regulated_healthy_allowed: bool = False
    ) -> ValidationReport:
        checks: dict[str, bool] = {}
        problems: list[str] = []
        lang = ExplanationTemplates.lang(locale)
        all_text = " ".join([short_text, *bullets])

        checks["schema"] = 0 < utf16_len(short_text) <= 90 and len(bullets) <= 3 and all(utf16_len(b) <= 80 for b in bullets)
        if not checks["schema"]:
            problems.append("Length or bullet limits exceeded")

        allowed = ExplanationValidator.allowed_numbers(facts)
        normalized_digits = ExplanationValidator.to_ascii_digits(all_text)
        numbers = (m.group().replace(",", ".") for m in _NUMBER_RX.finditer(normalized_digits))
        unexpected = list(dict.fromkeys(n for n in numbers if _canon(n) not in allowed))
        checks["numericFidelity"] = not unexpected
        if unexpected:
            problems.append("Numbers not present in facts: " + ", ".join(unexpected))

        lower = to_lower_invariant(all_text)
        banned = [t for t in ExplanationValidator.BANNED_TERMS[lang] if _contains_term(lower, to_lower_invariant(t))]
        if not regulated_healthy_allowed and lang == "en" and _has_word(lower, "healthy"):
            banned.append("healthy")
        if not regulated_healthy_allowed and lang == "es" and _has_word(lower, "saludable"):
            banned.append("saludable")
        checks["claims"] = not banned
        if banned:
            problems.append("Banned terms: " + ", ".join(banned))

        letters = [ch for ch in all_text if _is_letter_unit(ch)]
        devanagari = sum(1 for ch in letters if "\u0900" <= ch <= "\u097f")
        if lang == "hi":
            checks["language"] = len(letters) > 0 and devanagari >= len(letters) * 0.3
        elif lang == "es":
            checks["language"] = devanagari == 0 and any(_has_word(lower, w) for w in _SPANISH_WORDS)
        else:
            checks["language"] = devanagari == 0
        if not checks["language"]:
            problems.append("Language does not match " + locale)

        upper = to_upper_invariant(all_text)
        missing = [f.id for f in facts if f.must_disclose and not all(to_upper_invariant(c) in upper for c in f.codes)]
        checks["mustDisclose"] = not missing
        if missing:
            problems.append("Mandatory disclosures missing: " + ", ".join(missing))

        return ValidationReport(all(checks.values()), checks, tuple(problems))

    @staticmethod
    def allowed_numbers(facts: Iterable[Fact]) -> set[str]:
        """Canonical numbers the copy may contain: fact values (as given, 1 decimal, whole), percentages, multipliers, INS digits, 100."""
        allowed = {"100"}
        for f in facts:
            for v in (f.from_, f.to):
                allowed.add(_canon(_cs(v)))
                allowed.add(_canon(_fixed(round_decimal(v, 1), 1)))
                allowed.add(_canon(_fixed(round_decimal(v, 0), 0)))
            rel = f.rel_change
            if rel is not None:
                allowed.add(str(ExplanationTemplates.pct(rel)))
                allowed.add(str(int(_WIDE.multiply(rel.copy_abs(), _HUNDRED).quantize(_ONE, rounding=ROUND_HALF_UP, context=_WIDE))))
            m = ExplanationTemplates.multiplier(f.from_, f.to)
            if m is not None:
                allowed.add(_canon(m))
                allowed.add(_canon(_fixed(round_decimal(decimal_div(f.to, f.from_), 1), 1)))
            for code in f.codes:
                for d in _NUMBER_RX.finditer(code):
                    allowed.add(_canon(d.group()))
        return allowed

    @staticmethod
    def to_ascii_digits(s: str) -> str:
        """Devanagari digits (U+0966..U+096F) to ASCII digits."""
        return s.translate(_DEVANAGARI_DIGITS)


_DEVANAGARI_DIGITS = {0x0966 + i: ord("0") + i for i in range(10)}
