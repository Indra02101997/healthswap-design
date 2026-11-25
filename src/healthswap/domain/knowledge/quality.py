"""Data-quality rules, data sufficiency, entity-resolution features and field survivorship (ported from Knowledge/Quality.cs).

Messages reproduce the C# invariant-culture formatting: `{x}` is decimal.ToString() (scale kept) and `{x:0.#}` /
`{x:0.##}` round half away from zero with optional fraction digits. Entity-resolution features are float32 values like the
C# float[] (see healthswap.domain.text).
"""

from __future__ import annotations

import math
from array import array
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Context, Decimal
from enum import StrEnum

from healthswap.domain.knowledge.parsing import IngredientTaxonomy, equals_ignore_case, is_null_or_white_space, to_upper_invariant
from healthswap.domain.knowledge.products import AllergenPresence, Nutrition, decimal_max
from healthswap.domain.primitives import UTC, NetQuantity, as_utc, gtin_try_normalize, to_decimal
from healthswap.domain.text import HashingEmbedder, TextSimilarity, decimal_to_double, double_to_decimal, round_double, to_float32

_ZERO = Decimal(0)
_MIN_UTC = datetime.min.replace(tzinfo=UTC)
_WIDE = Context(prec=100)


def _or_zero(value: Decimal | None) -> Decimal:
    return value if value is not None else _ZERO


def _text(value: Decimal | int) -> str:
    """C# decimal.ToString() with the invariant culture: plain notation, scale kept, zero never negative."""
    if isinstance(value, Decimal):
        return format(abs(value) if value.is_zero() else value, "f")
    return str(value)


def _custom(value: Decimal | int, max_fraction_digits: int) -> str:
    """C# custom formats "0.#" / "0.##": half away from zero, optional fraction digits, zero never negative."""
    q = to_decimal(value).quantize(Decimal(1).scaleb(-max_fraction_digits), rounding=ROUND_HALF_UP, context=_WIDE)
    if q.is_zero():
        return "0"
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


class QaAction(StrEnum):
    """Values are the C# enum names, which is how the Ingestor prints them ("QA-01 Review - ...")."""

    REVIEW = "Review"
    REJECT_FIELD = "RejectField"
    AUTO_FIX = "AutoFix"
    FLAG = "Flag"
    QUARANTINE = "Quarantine"
    REJECT_IDENTIFIER = "RejectIdentifier"


@dataclass(frozen=True, slots=True)
class QaFinding:
    code: str
    action: QaAction
    field: str
    message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class QaInput:
    nutrition: Nutrition
    per_serving_energy_kcal: Decimal | None = None
    serving_size: Decimal | None = None
    raw_gtin: str | None = None
    declared_allergens: Mapping[str, AllergenPresence] = field(default_factory=dict)
    ingredient_allergens: Collection[str] = ()
    veg_mark_contradicted: bool = False
    tier_a_additives: Collection[str] = ()
    last_verified_at: datetime | None = None
    now: datetime = _MIN_UTC
    salt_declared: bool = False
    sodium_declared: bool = False


class QaRules:
    """Data-quality rules as code (doc 02 section 10). Each rule is deterministic and unit-tested."""

    @staticmethod
    def run(qa_input: QaInput) -> list[QaFinding]:
        f: list[QaFinding] = []
        n = qa_input.nutrition

        # QA-01 energy consistency, using available carbohydrate for the jurisdiction's carbohydrate definition.
        e, p, c, fat = n.energy_kcal, n.protein_g, n.available_carbohydrate, n.total_fat_g
        if e is not None and p is not None and c is not None and fat is not None:
            computed = 4 * p + 4 * c + 9 * fat + 2 * _or_zero(n.fibre_g)
            if abs(e - computed) > decimal_max(Decimal("0.15") * e, Decimal(20)):
                f.append(QaFinding("QA-01", QaAction.REVIEW, "nutrition.energyKcal", f"Declared {_text(e)} kcal vs {_custom(computed, 1)} kcal from macronutrients"))
        # QA-02..QA-04 internal consistency.
        sug, carb = n.total_sugars_g, n.carbohydrate_g
        if sug is not None and carb is not None and sug > carb + Decimal("0.5"):
            f.append(QaFinding("QA-02", QaAction.REJECT_FIELD, "nutrition.totalSugarsG", f"Sugars {_text(sug)} g exceed carbohydrate {_text(carb)} g"))
        add, tot = n.added_sugars_g, n.total_sugars_g
        if add is not None and tot is not None and add > tot + Decimal("0.5"):
            f.append(QaFinding("QA-03", QaAction.REJECT_FIELD, "nutrition.addedSugarsG", f"Added sugars {_text(add)} g exceed total sugars {_text(tot)} g"))
        tf = n.total_fat_g
        if tf is not None and _or_zero(n.saturated_fat_g) + _or_zero(n.trans_fat_g) > tf + Decimal("0.5"):
            f.append(QaFinding("QA-04", QaAction.REJECT_FIELD, "nutrition.saturatedFatG", "Saturated + trans fat exceed total fat"))
        # QA-05 mass balance per 100 g. US "total carbohydrate" already includes fibre, so fibre is added only for
        # the AVAILABLE definition (fixes the double count noted in review).
        if not n.is_liquid:
            fibre = _ZERO if n.carb_definition == "TOTAL_INCL_FIBRE" else _or_zero(n.fibre_g)
            mass = _or_zero(n.protein_g) + _or_zero(n.total_fat_g) + _or_zero(n.carbohydrate_g) + fibre + _or_zero(n.salt)
            if mass > Decimal(105):
                f.append(QaFinding("QA-05", QaAction.REVIEW, "nutrition", f"Macronutrients sum to {_custom(mass, 1)} g per 100 g"))
        # QA-06 sodium range (the robust z-score within category runs in the category-level job).
        na = n.sodium
        if na is not None and na > Decimal(40000):
            f.append(QaFinding("QA-06", QaAction.REVIEW, "nutrition.sodiumMg", f"Sodium {_text(na)} mg per 100 g is implausible"))
        # QA-07 per-serving consistency.
        ps, ss, e100 = qa_input.per_serving_energy_kcal, qa_input.serving_size, n.energy_kcal
        if ps is not None and ss is not None and ss > 0 and e100 is not None:
            expected = e100 * ss / Decimal(100)
            if expected > 0 and abs(ps - expected) / expected > Decimal("0.05"):
                f.append(QaFinding("QA-07", QaAction.REVIEW, "nutrition.perServing", f"Per-serving energy {_text(ps)} vs expected {_custom(expected, 1)}"))
        # QA-08 salt = 2.5 x sodium when both are declared.
        salt, sodium = n.salt_g, n.sodium_mg
        if qa_input.salt_declared and qa_input.sodium_declared and salt is not None and sodium is not None and sodium > 0:
            expected_salt = sodium * Decimal("2.5") / Decimal(1000)
            if abs(salt - expected_salt) / expected_salt > Decimal("0.10"):
                f.append(QaFinding("QA-08", QaAction.REVIEW, "nutrition.saltG", f"Salt {_text(salt)} g vs 2.5 x sodium = {_custom(expected_salt, 2)} g"))
        # QA-10 ingredient-derived allergens must be declared; the stored set is the conservative union.
        undeclared = [a for a in qa_input.ingredient_allergens if qa_input.declared_allergens.get(a) != AllergenPresence.CONTAINS]
        if undeclared:
            f.append(QaFinding("QA-10", QaAction.FLAG, "allergens", "Ingredients imply undeclared allergens: " + ", ".join(undeclared)))
        # QA-11 veg symbol contradicted by ingredients.
        if qa_input.veg_mark_contradicted:
            f.append(QaFinding("QA-11", QaAction.QUARANTINE, "dietaryMarks", "Vegetarian mark contradicted by ingredients; excluded from veg-constrained swaps"))
        # QA-12 stale verification.
        lv = qa_input.last_verified_at
        if lv is not None and as_utc(qa_input.now) - as_utc(lv) > timedelta(days=730):
            f.append(QaFinding("QA-12", QaAction.REVIEW, "verification", "Last verification older than 24 months"))
        # QA-13 GTIN check digit.
        if not is_null_or_white_space(qa_input.raw_gtin) and gtin_try_normalize(qa_input.raw_gtin) is None:
            f.append(QaFinding("QA-13", QaAction.REJECT_IDENTIFIER, "gtin", f"Invalid GTIN {qa_input.raw_gtin}"))
        # QA-14 additive prohibited in the market of sale (Tier A).
        if len(qa_input.tier_a_additives) > 0:
            f.append(QaFinding("QA-14", QaAction.QUARANTINE, "additives", "Prohibited additive in market: INS " + ", ".join(qa_input.tier_a_additives)))
        return f

    @staticmethod
    def fix_sodium_unit(value: Decimal, unit: str) -> tuple[Decimal, QaFinding | None]:
        """QA-09: sodium declared in grams with a value above 10 is almost certainly milligrams."""
        if unit == "g" and value > Decimal(10):
            return value, QaFinding("QA-09", QaAction.AUTO_FIX, "nutrition.sodiumMg", f"Sodium {_text(value)} g interpreted as {_text(value)} mg")
        return (value * Decimal(1000) if unit == "g" else value), None


class DataSufficiencyRules:
    """SUFFICIENT / PARTIAL / INSUFFICIENT (doc 02 7.5)."""

    @staticmethod
    def evaluate(
        n: Nutrition,
        ingredient_resolution: Decimal,
        has_ingredients: bool,
        allergen_confidence: Decimal,
        allergens_verified: bool,
        has_category: bool,
        has_net_quantity: bool,
    ) -> str:
        core = n.energy_kcal is not None and n.limited_sugars is not None and n.saturated_fat_g is not None and n.sodium is not None
        if not core:
            return "INSUFFICIENT"
        full = (
            n.protein_g is not None and n.fibre_g is not None and has_ingredients and ingredient_resolution >= Decimal("0.90")
            and (allergens_verified or allergen_confidence >= Decimal("0.98")) and has_category and has_net_quantity
        )
        return "SUFFICIENT" if full else "PARTIAL"


_F32_0_8 = to_float32(0.8)


class EntityResolution:
    """Pairwise entity-resolution features (doc 02 7.3). The trained model (ONNX) consumes exactly these features."""

    FEATURE_NAMES = ("name_trigram", "name_embedding_cos", "brand_match", "size_equal", "size_ratio_log", "nutrition_cos", "ingredient_jaccard", "gtin_conflict")
    AUTO_MERGE_THRESHOLD = Decimal("0.95")
    REVIEW_THRESHOLD = Decimal("0.75")

    @dataclass(frozen=True, slots=True)
    class Candidate:
        name: str
        brand: str | None
        size: NetQuantity | None
        nutrition: Nutrition | None
        ingredients: Collection[str]
        gtin: str | None

    @staticmethod
    def features(a: EntityResolution.Candidate, b: EntityResolution.Candidate) -> list[float]:
        """The 8 features in FEATURE_NAMES order, as float32 values (C# float[])."""
        trigram = TextSimilarity.trigram_jaccard(a.name, b.name)
        # C# string concatenation renders a null brand as "".
        emb = TextSimilarity.cosine(HashingEmbedder.embed(a.name + " " + (a.brand or "")), HashingEmbedder.embed(b.name + " " + (b.brand or "")))
        brand = EntityResolution._brand_match(a.brand, b.brand)
        size_equal, size_ratio = 0.0, 1.0
        sa, sb = a.size, b.size
        if sa is not None and sb is not None and sa.is_comparable_to(sb):
            va, vb = to_decimal(sa.value), to_decimal(sb.value)
            if abs(va - vb) <= Decimal("0.02") * decimal_max(va, vb):
                size_equal = 1.0
            if va > 0 and vb > 0:
                size_ratio = to_float32(abs(math.log(decimal_to_double(va / vb))))
        nut = EntityResolution._nutrition_cosine(a.nutrition, b.nutrition) if a.nutrition is not None and b.nutrition is not None else 0.5
        ing = TextSimilarity.jaccard(a.ingredients, b.ingredients)
        gtin_conflict = 1.0 if a.gtin is not None and b.gtin is not None and a.gtin != b.gtin else 0.0
        return [trigram, emb, brand, size_equal, size_ratio, nut, ing, gtin_conflict]

    @staticmethod
    def heuristic_score(features: Sequence[float]) -> Decimal:
        """Transparent fallback used until a trained model is deployed (and as the L2 degradation path)."""
        f = array("f", features)
        if f[7] > 0:
            return Decimal("0.05")  # never merge two different GTINs (doc 02 7.3 rule 4)
        z = -6.0 + 3.0 * f[0] + 2.5 * f[1] + 2.0 * f[2] + 1.5 * f[3] - 1.0 * f[4] + 2.0 * f[5] + 1.5 * f[6]
        try:
            exp = math.exp(-z)
        except OverflowError:
            exp = math.inf
        return double_to_decimal(round_double(1.0 / (1.0 + exp), 4))

    @staticmethod
    def decide(score: Decimal) -> str:
        if score >= EntityResolution.AUTO_MERGE_THRESHOLD:
            return "AUTO_MERGE"
        return "REVIEW" if score >= EntityResolution.REVIEW_THRESHOLD else "NEW"

    @staticmethod
    def _brand_match(a: str | None, b: str | None) -> float:
        if a is None or b is None or is_null_or_white_space(a) or is_null_or_white_space(b):
            return 0.5
        na = IngredientTaxonomy.normalize(a)
        nb = IngredientTaxonomy.normalize(b)
        if na == nb:
            return 1.0
        return _F32_0_8 if nb in na or na in nb else 0.0

    @staticmethod
    def _nutrition_cosine(a: Nutrition, b: Nutrition) -> float:
        def vector(n: Nutrition) -> list[float]:
            return [
                decimal_to_double(_or_zero(n.energy_kcal)) / 100, decimal_to_double(_or_zero(n.protein_g)),
                decimal_to_double(_or_zero(n.carbohydrate_g)), decimal_to_double(_or_zero(n.total_sugars_g)),
                decimal_to_double(_or_zero(n.total_fat_g)), decimal_to_double(_or_zero(n.saturated_fat_g)),
                decimal_to_double(_or_zero(n.sodium)) / 100, decimal_to_double(_or_zero(n.fibre_g)),
            ]

        va, vb = vector(a), vector(b)
        dot = na = nb = 0.0
        for x, y in zip(va, vb):
            dot += x * y
            na += x * x
            nb += y * y
        return 0.0 if na == 0 or nb == 0 else to_float32(dot / math.sqrt(na * nb))


class Survivorship:
    """Field-level survivorship (doc 02 7.4): highest trust tier, then most recent label; conflicts go to curation."""

    @dataclass(frozen=True, slots=True)
    class Candidate:
        field_path: str
        number: Decimal | None
        text: str | None
        trust_tier: int
        observed_at: datetime
        source_record_id: int
        qa_passed: bool

    @dataclass(frozen=True, slots=True)
    class Decision:
        field_path: str
        selected: Survivorship.Candidate | None
        conflict: bool
        reason: str | None

    @staticmethod
    def select(field_path: str, candidates: Collection[Survivorship.Candidate]) -> Survivorship.Decision:
        usable = [c for c in candidates if c.qa_passed and (c.number is not None or c.text is not None)]
        if not usable:
            return Survivorship.Decision(field_path, None, False, "NO_VALUE")
        # OrderBy(TrustTier).ThenByDescending(ObservedAt), both stable.
        ordered = sorted(sorted(usable, key=lambda c: as_utc(c.observed_at), reverse=True), key=lambda c: c.trust_tier)
        best = ordered[0]
        # Unverified machine extraction (T5) may only fill gaps; community data (T4) never overrides T1-T3.
        challenger = next((c for c in ordered[1:] if c.trust_tier != best.trust_tier and c.trust_tier <= 3), None)
        if challenger is not None and Survivorship.disagree(field_path, best, challenger):
            return Survivorship.Decision(field_path, best, True, f"T{best.trust_tier} vs T{challenger.trust_tier} disagree beyond tolerance")
        return Survivorship.Decision(field_path, best, False, None)

    @staticmethod
    def disagree(field_path: str, a: Survivorship.Candidate, b: Survivorship.Candidate) -> bool:
        if a.number is not None and b.number is not None:
            x, y = to_decimal(a.number), to_decimal(b.number)
            tolerance = Decimal("0.15") if "SODIUM" in to_upper_invariant(field_path) or ".NA." in field_path else Decimal("0.10")
            denom = decimal_max(abs(x), abs(y))
            return denom > 0 and abs(x - y) / denom > tolerance
        if to_upper_invariant(field_path).startswith("ALLERGEN"):
            return not equals_ignore_case(a.text, b.text)
        return False
