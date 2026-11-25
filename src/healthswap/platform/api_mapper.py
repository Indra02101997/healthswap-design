"""Maps internal read models to the public API contract (C# ApiMapper.cs, api/openapi.yaml).

Results are plain dicts with the camelCase property names and the property order of the C# contract records. Members
whose value is None are omitted (HsJson WhenWritingNull); numbers stay Decimal (the JSON layer writes them), ids are
strings and timestamps ISO 8601 strings (`primitives.iso`).
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from healthswap.domain.knowledge.parsing import equals_ignore_case, to_upper_invariant
from healthswap.domain.knowledge.products import AllergenPresence, decimal_clamp
from healthswap.domain.primitives import NetQuantity, format_amount, iso
from healthswap.domain.recommendation.explanations import Fact
from healthswap.domain.recommendation.labels import RecommendationLabel, ShopperCopy
from healthswap.domain.scoring.health_profiles import HealthProfileData
from healthswap.domain.scoring.models import ModelScore

_DIETARY_MARKS = {
    "IN_FSSAI_VEG": "VEG", "IN_FSSAI_NON_VEG": "NON_VEG", "IN_FSSAI_VEGAN": "VEGAN", "VEGAN_CERTIFIED": "VEGAN",
    "IN_JAIVIK_BHARAT": "ORGANIC", "US_USDA_ORGANIC": "ORGANIC", "IN_FSSAI_FORTIFIED": "FORTIFIED", "HALAL_CERTIFIED": "HALAL",
    "KOSHER_CERTIFIED": "KOSHER", "GLUTEN_FREE_CLAIM": "GLUTEN_FREE",
}


def _ordinal(s: str) -> bytes:
    """Sort key of StringComparer.Ordinal (UTF-16 code units)."""
    return s.encode("utf-16-be", "surrogatepass")


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drops None members (JsonIgnoreCondition.WhenWritingNull)."""
    return {k: v for k, v in d.items() if v is not None}


def _starts_with_ignore_case(s: str, prefix: str) -> bool:
    return to_upper_invariant(s).startswith(to_upper_invariant(prefix))


class ApiMapper:
    """Maps internal read models to the public API contract (api/openapi.yaml)."""

    @staticmethod
    def money(amount: Decimal, currency: str) -> dict[str, str]:
        """{"amount": "89.00", "currency": "INR"}: rounded half-even to the currency's minor unit."""
        text = format_amount(amount, currency)
        if text.startswith("-") and not text.strip("-0."):  # a rounded negative zero prints without its sign in C#
            text = text[1:]
        return {"amount": text, "currency": currency}

    @staticmethod
    def locales_for(market: str) -> list[str]:
        if market == "IN":
            return ["en-IN", "hi-IN"]
        if market == "US":
            return ["en-US", "es-US"]
        return ["en-" + market]

    @staticmethod
    def default_locale(market: str) -> str:
        return ApiMapper.locales_for(market)[0]

    @staticmethod
    def resolve_locale(market: str, requested: str | None) -> str:
        """Normalizes a requested locale to one with pre-generated explanations for the market."""
        locales = ApiMapper.locales_for(market)
        if requested is None:
            return locales[0]
        exact = next((loc for loc in locales if equals_ignore_case(loc, requested)), None)
        if exact is not None:
            return exact
        lang = requested.split("-")[0]
        return next((loc for loc in locales if _starts_with_ignore_case(loc, lang + "-")), locales[0])

    @staticmethod
    def score(s: ModelScore | None) -> dict[str, Any] | None:
        if s is None:
            return None
        return _compact({
            "model": s.code, "version": s.version, "value": s.value, "grade": s.grade,
            "components": dict(s.components) if len(s.components) > 0 else None,
        })

    @staticmethod
    def summary(hp: HealthProfileData) -> dict[str, Any]:
        return _compact({
            "productId": str(hp.product_id), "hsi": hp.hsi, "hsiGrade": hp.hsi_grade, "primaryScore": ApiMapper.score(hp.primary),
            "flags": list(hp.flags), "dataSufficiency": hp.data_sufficiency,
        })

    @staticmethod
    def net(n: NetQuantity | None) -> dict[str, Any] | None:
        return None if n is None else {"value": n.value, "unit": n.unit}

    @staticmethod
    def panel(hp: HealthProfileData) -> dict[str, Any]:
        n = hp.nutrition
        serving = hp.serving_size
        return _compact({
            "basis": n.basis,
            "servingSize": {"value": serving, "unit": hp.serving_unit} if serving is not None and serving > 0 and hp.serving_unit is not None else None,
            "energyKcal": n.energy_kcal, "proteinG": n.protein_g, "carbohydrateG": n.carbohydrate_g, "totalSugarsG": n.total_sugars_g,
            "addedSugarsG": n.added_sugars_g, "totalFatG": n.total_fat_g, "saturatedFatG": n.saturated_fat_g, "transFatG": n.trans_fat_g,
            "cholesterolMg": n.cholesterol_mg, "sodiumMg": n.sodium, "saltG": n.salt, "dietaryFibreG": n.fibre_g,
            "other": [{"code": "FVLN_PCT", "amount": n.fvln_pct, "unit": "percent"}] if n.fvln_pct is not None else None,
        })

    @staticmethod
    def dietary_marks(marks: Iterable[str], diet_class: str) -> list[str]:
        """Internal regulatory/certification marks -> public DietaryMark enum (ordinal-sorted, distinct)."""
        result = {_DIETARY_MARKS[m] for m in marks if m in _DIETARY_MARKS}
        if diet_class == "EGG":
            result.add("EGG")
        return sorted(result, key=_ordinal)

    @staticmethod
    def internal_marks(marks: Iterable[str] | None, market: str) -> list[str]:
        """Public DietaryMark enum -> internal mark codes (partner catalog input), distinct in input order."""
        india = market == "IN"
        mapping = {
            "VEG": "IN_FSSAI_VEG" if india else None, "NON_VEG": "IN_FSSAI_NON_VEG" if india else None,
            "VEGAN": "IN_FSSAI_VEGAN" if india else "VEGAN_CERTIFIED", "ORGANIC": "IN_JAIVIK_BHARAT" if india else "US_USDA_ORGANIC",
            "FORTIFIED": "IN_FSSAI_FORTIFIED", "HALAL": "HALAL_CERTIFIED", "KOSHER": "KOSHER_CERTIFIED", "GLUTEN_FREE": "GLUTEN_FREE_CLAIM",
        }
        mapped = (mapping.get(m) for m in (marks if marks is not None else ()))
        return list(dict.fromkeys(m for m in mapped if m is not None))

    @staticmethod
    def presence(p: AllergenPresence | str) -> str:
        if p == AllergenPresence.CONTAINS:
            return "CONTAINS"
        return "MAY_CONTAIN" if p == AllergenPresence.MAY_CONTAIN else "FREE_FROM"

    @staticmethod
    def attributions(sources: Iterable[str]) -> list[dict[str, str]]:
        result: list[dict[str, str]] = []
        for s in dict.fromkeys(sources):
            if s == "OFF":
                result.append({
                    "source": "Open Food Facts", "license": "ODbL-1.0",
                    "attributionText": "Contains data from Open Food Facts, available under the Open Database License.",
                })
            if s.startswith("USDA_FDC"):
                result.append({
                    "source": "USDA FoodData Central", "license": "CC0-1.0",
                    "attributionText": "U.S. Department of Agriculture, Agricultural Research Service. FoodData Central.",
                })
        return result

    @staticmethod
    def profile(hp: HealthProfileData, locale: str | None = None) -> dict[str, Any]:
        """The public HealthProfile; badge labels in the resolved locale of the profile's market."""
        loc = ApiMapper.resolve_locale(hp.market, locale)
        category = None
        if hp.category_id is not None:
            category = {"id": str(hp.category_id), "name": hp.category_name if hp.category_name is not None else hp.category_code}
        return _compact({
            "productId": str(hp.product_id), "market": hp.market, "gtin": hp.gtin, "displayName": hp.name, "brand": hp.brand, "category": category,
            "netQuantity": ApiMapper.net(hp.net_quantity), "hsi": hp.hsi, "hsiGrade": hp.hsi_grade, "primaryScore": ApiMapper.score(hp.primary),
            "scores": [ApiMapper.score(s) for s in hp.scores], "novaGroup": hp.nova, "flags": list(hp.flags),
            "badges": [{"code": b.code, "label": ShopperCopy.badge_label(b.code, b.label, loc), "tone": b.tone} for b in hp.badges],
            "nutrition": [ApiMapper.panel(hp)],
            "allergens": [{"code": code, "presence": ApiMapper.presence(p)} for code, p in sorted(hp.allergens.items(), key=lambda kv: _ordinal(kv[0]))],
            "dietaryMarks": ApiMapper.dietary_marks(hp.marks, hp.diet_class),
            "additives": [
                _compact({"insNumber": a.code, "eNumber": a.e_number, "name": a.name, "functionalClass": a.functional_class, "concernTier": str(a.tier)})
                for a in hp.additives
            ],
            "usFdaHealthyEligible": hp.us_fda_healthy, "recalled": hp.recalled, "dataSufficiency": hp.data_sufficiency,
            "dataConfidence": decimal_clamp(hp.confidence, Decimal(0), Decimal(1)), "attributions": ApiMapper.attributions(hp.sources),
            "profileVersion": hp.profile_version, "computedAt": iso(hp.computed_at),
        })

    @staticmethod
    def product(hp: HealthProfileData, partner_sku: str, title: str | None = None) -> dict[str, Any]:
        return _compact({
            "partnerSku": partner_sku, "productId": str(hp.product_id), "title": title if title is not None else hp.name, "brand": hp.brand,
            "netQuantity": ApiMapper.net(hp.net_quantity), "health": ApiMapper.summary(hp),
        })

    @staticmethod
    def label(label: RecommendationLabel) -> dict[str, Any]:
        return _compact({
            "code": label.code, "text": label.text, "shortText": label.short_text, "disclosure": label.disclosure,
            "chargeable": label.chargeable, "feeRule": label.fee_rule,
        })

    @staticmethod
    def details(facts: Iterable[Fact]) -> list[dict[str, Any]]:
        """Nutrient comparisons (NUTRIENT facts only)."""
        return [
            _compact({
                "nutrient": f.nutrient if f.nutrient is not None else "", "basis": f.basis, "from": f.from_, "to": f.to, "unit": f.unit,
                "relativeChange": f.rel_change, "direction": f.direction if f.direction in ("BETTER", "WORSE") else "SAME",
            })
            for f in facts
            if f.kind == "NUTRIENT"
        ]


money = ApiMapper.money
locales_for = ApiMapper.locales_for
default_locale = ApiMapper.default_locale
resolve_locale = ApiMapper.resolve_locale
score = ApiMapper.score
summary = ApiMapper.summary
net = ApiMapper.net
panel = ApiMapper.panel
dietary_marks = ApiMapper.dietary_marks
internal_marks = ApiMapper.internal_marks
presence = ApiMapper.presence
attributions = ApiMapper.attributions
profile = ApiMapper.profile
product = ApiMapper.product
label = ApiMapper.label
details = ApiMapper.details
