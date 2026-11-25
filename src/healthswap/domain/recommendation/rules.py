"""Eligibility rules: product-level static rules (R1-R4, R7) and request-time filters (R5-R7) (Recommendation/Rules.cs).

Rule identifiers ("R1".."R7") and reject reason codes are the exact C# strings; `RequestRules.reject` returns the first
failing reason or None.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import dataclass
from decimal import Decimal

from healthswap.domain.knowledge.parsing import equals_ignore_case, to_upper_invariant
from healthswap.domain.knowledge.products import AllergenPresence, decimal_div, decimal_max, round_decimal
from healthswap.domain.primitives import NetQuantity
from healthswap.domain.scoring.health_profiles import HealthProfileData, MarketPack
from healthswap.domain.scoring.models import INT32_MAX, PointsModel

_ZERO = Decimal(0)
DECIMAL_MAX = Decimal("79228162514264337593543950335")


@dataclass(frozen=True, slots=True)
class StaticRuleResult:
    eligible: bool
    health_delta: Decimal
    failed: tuple[str, ...]
    reason_codes: tuple[str, ...]
    introduced_tier_c: tuple[str, ...]


# (front-of-pack code, nutrient key, solid low default, liquid low default)
_LIMITED_NUTRIENTS: tuple[tuple[str, str, Decimal, Decimal], ...] = (
    ("SUGAD", "LIMITED_SUGARS_G", Decimal("5.0"), Decimal("2.5")),
    ("NA", "SODIUM_MG", Decimal(120), Decimal(60)),
    ("FASAT", "SAT_FAT_G", Decimal("1.5"), Decimal("0.75")),
    ("FATRN", "TRANS_FAT_G", Decimal("0.1"), Decimal("0.1")),
)


def _or_zero(value: Decimal | None) -> Decimal:
    return value if value is not None else _ZERO


class StaticRules:
    """
    Product-level eligibility rules evaluated offline by the swap-graph builder and re-checked at request time when a
    profile version changed (doc 03 section 3: R1, R2, R3, R4, R7).
    """

    @staticmethod
    def check(a: HealthProfileData, b: HealthProfileData, substitutability: Decimal, category_allowed: bool, pack: MarketPack) -> StaticRuleResult:
        failed: list[str] = []
        rules = pack.rules
        delta = _or_zero(b.hsi) - _or_zero(a.hsi)

        # R1 substitutable
        if not category_allowed or substitutability < rules.tau_sub:
            failed.append("R1")

        # R2 meaningfully healthier: delta >= dMin and the primary model strictly better; equal result needs 2 x dMin.
        if a.hsi is None or b.hsi is None:
            failed.append("R2")
        else:
            model = pack.primary
            ra = INT32_MAX if a.primary is None else PointsModel.grade_rank(model, a.primary)
            rb = INT32_MAX if b.primary is None else PointsModel.grade_rank(model, b.primary)
            strictly_better = rb < ra
            equal = rb == ra
            if not (delta >= rules.delta_min and (strictly_better or (equal and delta >= 2 * rules.delta_min))):
                failed.append("R2")

        # R3 no critical regression in any limited nutrient (per 100 g/ml) and no worse front-of-pack band.
        liquid = b.nutrition.is_liquid
        state = "LIQUID" if liquid else "SOLID"
        for fop, key, solid_low, liquid_low in _LIMITED_NUTRIENTS:
            av = a.nutrition.get(key)
            bv = b.nutrition.get(key)
            if bv is None:
                if av is not None and key != "TRANS_FAT_G":
                    failed.append("R3")
                continue
            threshold = next(
                (t for t in pack.fop if t.nutrient == fop and t.basis != "PER_SERVING_PCT_DV" and (t.physical_state == "ANY" or t.physical_state == state)),
                None,
            )
            low = threshold.low_max if threshold is not None else (liquid_low if liquid else solid_low)
            limit = decimal_max((1 + rules.limited_nutrient_tolerance) * _or_zero(av), low)
            if bv > limit:
                failed.append("R3")
                break
            ba = a.fop_bands.get(fop)
            bb = b.fop_bands.get(fop)
            if ba is not None and bb is not None and bb > ba:
                failed.append("R3")
                break
        if "R3" not in failed:
            for fop in ("SUGAR", "SALT"):
                ba = a.fop_bands.get(fop)
                bb = b.fop_bands.get(fop)
                if ba is not None and bb is not None and bb > ba:
                    failed.append("R3")
                    break

        # R4 no new concerns: no Tier A, no Tier B absent from A, NOVA(B) <= NOVA(A), new Tier C only with 2 x dMin.
        a_codes = {x.code for x in a.additives}
        introduced_c = [x.code for x in b.additives if x.tier == "C" and x.code not in a_codes]
        na, nb = a.nova, b.nova
        if b.has_additive_tier("A") or any(x.tier == "B" and x.code not in a_codes for x in b.additives):
            failed.append("R4")
        elif na is not None and (nb is None or nb > na):
            failed.append("R4")
        elif introduced_c and delta < 2 * rules.delta_min:
            failed.append("R4")

        # R7 data sufficiency.
        if b.data_sufficiency != "SUFFICIENT" or b.confidence < rules.min_alternative_confidence or a.data_sufficiency == "INSUFFICIENT":
            failed.append("R7")
        if not b.eligible:
            failed.append("R7")

        return StaticRuleResult(
            len(failed) == 0, round_decimal(delta, 2), tuple(dict.fromkeys(failed)), tuple(StaticRules.reason_codes(a, b)), tuple(introduced_c)
        )

    @staticmethod
    def reason_codes(a: HealthProfileData, b: HealthProfileData) -> list[str]:
        codes: list[str] = []

        def lower(key: str, code: str, min_rel: Decimal) -> None:
            av = a.nutrition.get(key)
            bv = b.nutrition.get(key)
            if av is not None and av > 0 and bv is not None and decimal_div(av - bv, av) >= min_rel:
                codes.append(code)

        def higher(key: str, code: str) -> None:
            av = _or_zero(a.nutrition.get(key))
            bv = b.nutrition.get(key)
            if bv is not None and bv > av * Decimal("1.25") and bv - av >= 1:
                codes.append(code)

        lower("LIMITED_SUGARS_G", "LOWER_SUGAR", Decimal("0.10"))
        lower("SODIUM_MG", "LOWER_SODIUM", Decimal("0.10"))
        lower("SAT_FAT_G", "LOWER_SAT_FAT", Decimal("0.10"))
        higher("FIBRE_G", "HIGHER_FIBRE")
        higher("PROTEIN_G", "HIGHER_PROTEIN")
        if a.nova is not None and b.nova is not None and b.nova < a.nova:
            codes.append("LESS_PROCESSED")
        if sum(1 for x in a.additives if x.tier in ("B", "C")) > sum(1 for x in b.additives if x.tier in ("B", "C")):
            codes.append("FEWER_ADDITIVES_OF_CONCERN")
        if "ARTIFICIAL_COLOURS" in a.flags and "ARTIFICIAL_COLOURS" not in b.flags:
            codes.append("NO_ARTIFICIAL_COLOURS")
        return codes


@dataclass(frozen=True, slots=True, kw_only=True)
class ShopperConstraints:
    """Shopper constraints after consent handling (doc 03 R5, R7, R8)."""

    diet_type: str = "ANY"
    allergens: Collection[str] = ()
    strict_may_contain: bool = True
    goals: Collection[str] = ()
    avoid_ingredients: Collection[str] = ()
    excluded_brands: Collection[str] = ()
    religious_preferences: Collection[str] = ()
    # True, or unknown under a STRICT minors policy: no caffeine, no personalization.
    treat_as_minor: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class CommercialGuards:
    """Partner commercial guards and catalog rules (HLD 18, doc 03 R6)."""

    max_normalized_price_ratio: Decimal = Decimal("1.75")
    max_abs_premium_per_unit: Decimal = Decimal(150)
    pack_ratio_min: Decimal = Decimal("0.5")
    pack_ratio_max: Decimal = Decimal("2.0")
    blocked_brands: Collection[str] = ()
    excluded_categories: Collection[str] = ()


@dataclass(frozen=True, slots=True)
class PriceContext:
    original_unit_price: Decimal | None
    alternative_unit_price: Decimal | None


_AVOID_FLAGS = {
    "NON_SUGAR_SWEETENERS": "NNS",
    "ARTIFICIAL_COLOURS": "ARTIFICIAL_COLOUR",
    "PALM_OIL": "PALM_OIL",
    "ADDED_MSG": "ADDED_MSG",
    "CAFFEINE": "CAFFEINE",
}
_RELIGIOUS_MARKS = {"HALAL": "HALAL_CERTIFIED", "KOSHER": "KOSHER_CERTIFIED"}


class RequestRules:
    """Request-time filters: safety and preferences (R5), data sufficiency for allergic shoppers (R7) and commercial guards (R6)."""

    @staticmethod
    def reject(
        original: HealthProfileData, alt: HealthProfileData, shopper: ShopperConstraints, guards: CommercialGuards, price: PriceContext,
        verified_allergen_confidence: Decimal,
    ) -> str | None:
        if not alt.eligible or alt.recalled or alt.quarantined:
            return "R6_NOT_ELIGIBLE"

        # R5 allergens (CONTAINS always; MAY_CONTAIN when strict) and verified allergen data for declared allergens (R7).
        for allergen in shopper.allergens:
            presence = alt.allergens.get(allergen)
            if presence is not None:
                if presence == AllergenPresence.CONTAINS:
                    return "R5_ALLERGEN"
                if presence == AllergenPresence.MAY_CONTAIN and shopper.strict_may_contain:
                    return "R5_ALLERGEN_MAY_CONTAIN"
            if allergen == "GLUTEN_CEREALS" and alt.allergens.get("WHEAT") == AllergenPresence.CONTAINS:
                return "R5_ALLERGEN"
        if len(shopper.allergens) > 0 and not alt.allergens_verified and alt.allergen_confidence < verified_allergen_confidence:
            return "R7_ALLERGEN_UNVERIFIED"

        # R5 diet type (unknown diet data never passes a diet constraint).
        diet = alt.diet_class
        if shopper.diet_type == "ANY":
            diet_ok = True
        elif shopper.diet_type == "VEGETARIAN":
            diet_ok = diet in ("VEG", "VEGAN")
        elif shopper.diet_type == "EGGETARIAN":
            diet_ok = diet in ("VEG", "VEGAN", "EGG")
        elif shopper.diet_type == "VEGAN":
            diet_ok = diet == "VEGAN"
        elif shopper.diet_type == "JAIN":
            diet_ok = diet in ("VEG", "VEGAN") and alt.jain_friendly_inferred
        elif shopper.diet_type == "PESCATARIAN":
            diet_ok = diet in ("VEG", "VEGAN", "EGG") or (diet == "NON_VEG" and "MEAT" not in alt.ingredient_flags)
        else:
            diet_ok = False
        if not diet_ok:
            return "R5_DIET"
        if shopper.diet_type != "ANY" and "VEG_MARK_CONTRADICTED" in alt.flags:
            return "R5_DIET"

        for pref in shopper.religious_preferences:
            mark = _RELIGIOUS_MARKS.get(pref)
            if mark is not None and mark not in alt.marks:
                return "R5_RELIGIOUS_MARK"
        for avoid in shopper.avoid_ingredients:
            flag = _AVOID_FLAGS.get(avoid)
            if flag is not None and flag in alt.ingredient_flags:
                return "R5_AVOID_" + avoid
        if shopper.treat_as_minor and "CAFFEINE" in alt.ingredient_flags:
            return "R5_MINOR_CAFFEINE"
        brand = alt.brand
        if brand is not None:
            if any(equals_ignore_case(b, brand) for b in shopper.excluded_brands):
                return "R5_EXCLUDED_BRAND"
            if any(equals_ignore_case(b, brand) for b in guards.blocked_brands):
                return "R6_BLOCKED_BRAND"
        category = to_upper_invariant(alt.category_code)
        if any(category.startswith(to_upper_invariant(c)) for c in guards.excluded_categories):
            return "R5_EXCLUDED_CATEGORY"

        # R6 commercial guards.
        so, sa = original.net_quantity, alt.net_quantity
        if so is not None and sa is not None and so.is_comparable_to(sa):
            pack_ratio = decimal_div(sa.value, so.value)
            if pack_ratio < guards.pack_ratio_min or pack_ratio > guards.pack_ratio_max:
                return "R6_PACK_RATIO"
        po, pa = price.original_unit_price, price.alternative_unit_price
        if po is not None and pa is not None and po > 0:
            ratio = RequestRules.normalized_price_ratio(po, original.net_quantity, pa, alt.net_quantity)
            if ratio > guards.max_normalized_price_ratio:
                return "R6_PRICE_RATIO"
            if pa - po > guards.max_abs_premium_per_unit:
                return "R6_ABS_PREMIUM"
        return None

    @staticmethod
    def normalized_price_ratio(original_price: Decimal, original_size: NetQuantity | None, alt_price: Decimal, alt_size: NetQuantity | None) -> Decimal:
        """Price per 100 g/ml (alternative / original); pack-price ratio when sizes are not comparable."""
        if original_price <= 0:
            return DECIMAL_MAX
        so, sa = original_size, alt_size
        if so is not None and sa is not None and so.is_comparable_to(sa) and so.value > 0 and sa.value > 0:
            return round_decimal(decimal_div(decimal_div(alt_price, sa.value), decimal_div(original_price, so.value)), 4)
        return round_decimal(decimal_div(alt_price, original_price), 4)
