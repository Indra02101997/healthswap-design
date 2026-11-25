"""Scoring, health profiles, eligibility rules, ranking, fact sheets and explanation templates (port of ScoringAndRecommendationTests.cs)."""

from __future__ import annotations

import json
import random
import uuid
from dataclasses import replace
from decimal import Decimal as D
from functools import lru_cache

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from healthswap.domain.knowledge.parsing import to_upper_invariant
from healthswap.domain.knowledge.products import AllergenPresence, Basis, Nutrition, ProductData, decimal_div, round_decimal
from healthswap.domain.primitives import NetQuantity, utc_now
from healthswap.domain.recommendation.explanations import ExplanationTemplates, ExplanationValidator, Fact, FactSheets
from healthswap.domain.recommendation.ranking import CartCandidate, CartSelection, RankerFeatures, Substitutability
from healthswap.domain.recommendation.rules import CommercialGuards, PriceContext, RequestRules, ShopperConstraints, StaticRules
from healthswap.domain.scoring.health_profiles import HealthProfileBuilder, HealthProfileData, MarketPack
from healthswap.domain.scoring.models import AdditiveRegulation, AdditiveTiers, Hsi, HsiParams, PointsModel, PointsModelParams
from healthswap.paths import repo_root

# FsCheck PositiveInt: any Int32 >= 1.
positive_int = st.integers(min_value=1, max_value=2**31 - 1)
Number = D | int | str

# ---------------------------------------------------------------------------------------------------------------------
# TestData
# ---------------------------------------------------------------------------------------------------------------------


@lru_cache(maxsize=None)
def model(code: str) -> PointsModelParams:
    text = (repo_root() / "data" / "packs" / "models" / f"{code}.json").read_text(encoding="utf-8")
    return PointsModelParams.from_dict(json.loads(text, parse_float=D))


def pack(primary: str = "NUTRISCORE_2023", additives: dict[str, AdditiveRegulation] | None = None) -> MarketPack:
    return MarketPack(market="IN", primary_model=primary, points_models={primary: model(primary)}, additives=additives if additives is not None else {})


def product(
    name: str, category: str, kcal: Number, sugar: Number, sat_fat: Number, sodium: Number, fibre: Number, protein: Number, nova: int | None = 3,
    additives: list[str] | None = None, state: str = "SOLID", net: Number = 280, brand: str | None = "Brand",
) -> ProductData:
    liquid = state == "LIQUID"
    return ProductData(
        product_id=uuid.uuid4(), version_id=random.randint(1, 999_999), name=name, brand=brand, category_code=category, physical_state=state,
        net_quantity=NetQuantity(D(net), "ml" if liquid else "g"),
        nutrition=Nutrition(
            basis=Basis.PER_100ML if liquid else Basis.PER_100G, energy_kcal=D(kcal), total_sugars_g=D(sugar), added_sugars_g=D(sugar),
            saturated_fat_g=D(sat_fat), total_fat_g=D(sat_fat) * 2, trans_fat_g=D(0), sodium_mg=D(sodium), fibre_g=D(fibre), protein_g=D(protein),
            carbohydrate_g=D(60),
        ),
        nova=nova, nova_confidence=D("0.9"), additives=tuple(additives or ()), data_sufficiency="SUFFICIENT", confidence=D("0.95"), diet_class="VEG",
        allergens_verified=True, allergen_confidence=D(1), form="NOODLE", flavour_family="MASALA", use_occasion="MEAL",
    )


# ---------------------------------------------------------------------------------------------------------------------
# ScoringTests
# ---------------------------------------------------------------------------------------------------------------------


class TestScoring:
    def test_more_sugar_never_scores_better(self) -> None:
        m = model("NUTRISCORE_2023")
        low = PointsModel.score(m, product("a", "biscuits.sweet", 450, 5, 5, 300, 3, 6))
        high = PointsModel.score(m, product("b", "biscuits.sweet", 450, 35, 5, 300, 3, 6))
        assert high.value is not None and low.value is not None and high.value > low.value
        assert high.normalized is not None and low.normalized is not None and high.normalized < low.normalized

    def test_hsi_weights_and_renormalization(self) -> None:
        p = HsiParams()
        assert Hsi.compute(D(50), 4, 1, 0, p) == D("51.5")  # 0.70x50 + 0.15x40 + 0.15x70
        assert Hsi.compute(D(50), None, 1, 0, p) == round_decimal(decimal_div(D("0.70") * 50 + D("0.15") * 70, D("0.85")), 2)  # NOVA unknown
        assert Hsi.compute(None, 1, 0, 0, p) is None
        assert Hsi.grade(D(80)) == "A"
        assert Hsi.grade(D("34.99")) == "E"

    def test_prohibited_additive_makes_a_product_ineligible(self) -> None:
        additives = {"924a": AdditiveRegulation("924a", "Potassium bromate", ("FLOUR_TREATMENT_AGENT",), {"IN": "PROHIBITED"}, None)}
        hp = HealthProfileBuilder.build(product("bread", "bread.sliced", 250, 4, 1, 400, 3, 8, additives=["924a"]), pack(additives=additives), utc_now())
        assert hp.hsi is None
        assert not hp.eligible
        assert "PROHIBITED_ADDITIVE" in hp.flags

    def test_additive_tiers_come_from_regulators(self) -> None:
        assert AdditiveTiers.resolve(AdditiveRegulation("171", "TiO2", ("COLOUR",), {"EU": "PROHIBITED"}, "B"), "EU") == "A"
        assert AdditiveTiers.resolve(AdditiveRegulation("171", "TiO2", ("COLOUR",), {"EU": "PROHIBITED"}, "B"), "IN") == "B"
        assert AdditiveTiers.resolve(AdditiveRegulation("951", "Aspartame", ("SWEETENER",), {}, "C"), "IN") == "C"
        assert AdditiveTiers.resolve(None, "IN") == "D"


# ---------------------------------------------------------------------------------------------------------------------
# RecommendationTests
# ---------------------------------------------------------------------------------------------------------------------


def noodle_pair() -> tuple[HealthProfileData, HealthProfileData, MarketPack]:
    pk = pack()
    a = HealthProfileBuilder.build(product("Masala noodles", "noodles.instant", 440, "2.5", "8.5", 1200, 2, "8.5", nova=4), pk, utc_now())
    b = HealthProfileBuilder.build(product("Millet noodles", "noodles.instant", 380, 2, 4, 1050, "6.1", 10, nova=3, net=256), pk, utc_now())
    return a, b, pk


class TestRecommendation:
    def test_healthier_substitutable_product_passes_static_rules(self) -> None:
        a, b, pk = noodle_pair()
        r = StaticRules.check(a, b, D("0.9"), True, pk)
        assert r.eligible, ",".join(r.failed)
        assert "HIGHER_FIBRE" in r.reason_codes

    def test_low_substitutability_fails_r1_and_a_worse_product_fails_r2(self) -> None:
        a, b, pk = noodle_pair()
        assert "R1" in StaticRules.check(a, b, D("0.5"), True, pk).failed
        assert "R2" in StaticRules.check(b, a, D("0.9"), True, pk).failed

    def test_shopper_allergens_and_price_guards_filter_alternatives(self) -> None:
        a, b, _ = noodle_pair()
        with_milk = replace(b, allergens={"MILK": AllergenPresence.MAY_CONTAIN})
        assert (
            RequestRules.reject(a, with_milk, ShopperConstraints(allergens=["MILK"]), CommercialGuards(), PriceContext(D(56), D(89)), D("0.98"))
            == "R5_ALLERGEN_MAY_CONTAIN"
        )
        assert (
            RequestRules.reject(
                a, with_milk, ShopperConstraints(allergens=["MILK"], strict_may_contain=False), CommercialGuards(), PriceContext(D(56), D(89)), D("0.98")
            )
            is None
        )
        assert RequestRules.reject(a, b, ShopperConstraints(), CommercialGuards(), PriceContext(D(56), D(120)), D("0.98")) == "R6_PRICE_RATIO"
        assert round_decimal(RequestRules.normalized_price_ratio(D(56), a.net_quantity, D(89), b.net_quantity), 2) == D("1.74")

    def test_minors_never_get_caffeine(self) -> None:
        a, b, _ = noodle_pair()
        caffeinated = replace(b, ingredient_flags=("CAFFEINE",))
        assert (
            RequestRules.reject(a, caffeinated, ShopperConstraints(treat_as_minor=True), CommercialGuards(), PriceContext(None, None), D("0.98"))
            == "R5_MINOR_CAFFEINE"
        )

    def test_ranker_features_exclude_commercial_signals(self) -> None:
        for f in RankerFeatures.FEATURE_NAMES:
            assert not any(to_upper_invariant(bad) in to_upper_invariant(f) for bad in RankerFeatures.FORBIDDEN)
        v = RankerFeatures.vector(RankerFeatures.Input(D("16.5"), D("0.9"), D("1.74"), True, D("0.91"), "CART", 10, D(0), False, 1, False))
        assert len(v) == len(RankerFeatures.FEATURE_NAMES)
        a, b, _ = noodle_pair()
        assert len(Substitutability.features(a, b, 0)) == len(Substitutability.FEATURE_NAMES)

    def test_approved_category_maps_take_precedence_over_sibling_distance(self) -> None:
        assert Substitutability.category_distance("a.b", "a.b", False) == 0
        assert Substitutability.category_distance("beverages.carbonated.soft-drinks", "beverages.carbonated.flavoured-water", False) == 1
        assert Substitutability.category_distance("beverages.carbonated.soft-drinks", "beverages.carbonated.flavoured-water", True) == 2
        assert Substitutability.category_distance("snacks.chips", "dairy.yogurt", False) == 3

    def test_cart_selection_takes_one_line_per_category(self) -> None:
        picked = CartSelection.select(
            [
                CartCandidate("1", "noodles", D(15), D(2), D(1), D("1.7")), CartCandidate("2", "noodles", D(10), D(1), D(1), D("1.1")),
                CartCandidate("3", "cola", D(50), D(1), D(1), D("1.3")), CartCandidate("4", "chips", D(9), D(1), D(1), None),
            ],
            2,
        )
        assert picked == ["3", "1"]

    def test_trivial_differences_are_not_claims(self) -> None:
        a, b, pk = noodle_pair()
        with_trans = replace(a, nutrition=replace(a.nutrition, trans_fat_g=D("0.1")))
        sheet = FactSheets.build(with_trans, b, pk)
        (trans,) = [f for f in sheet.facts if f.nutrient == "transFatG"]
        assert not trans.claim_eligible
        assert trans.impact == 0
        assert not any(f.nutrient == "transFatG" for f in FactSheets.select(sheet))


# ---------------------------------------------------------------------------------------------------------------------
# ExplanationTests
# ---------------------------------------------------------------------------------------------------------------------


def nutrient(nutrient: str, from_: D, to: D, unit: str = "g", claim: bool = True) -> Fact:
    return Fact(
        id="f1", kind="NUTRIENT", nutrient=nutrient, from_=from_, to=to, unit=unit,
        rel_change=None if from_ == 0 else round_decimal(decimal_div(to - from_, from_), 4), direction="BETTER", impact=D(1), claim_eligible=claim,
    )


class TestExplanation:
    @pytest.mark.parametrize("locale", ["en-IN", "hi-IN", "en-US", "es-US"])
    def test_templates_always_pass_the_validator(self, locale: str) -> None:
        facts = [
            nutrient("totalSugarsG", D("10.6"), D("0.2")),
            replace(nutrient("dietaryFibreG", D("2.0"), D("6.1")), id="f2"),
            Fact(id="f3", kind="ADDITIVE_INTRODUCED", nutrient="sweeteners", codes=("951",), direction="WORSE", must_disclose=True),
        ]
        r = ExplanationTemplates.render(facts, locale)
        report = ExplanationValidator.validate(r.short, r.bullets, facts, locale)
        assert report.passed, "; ".join(report.problems) + " :: " + r.short

    @settings(max_examples=200)
    @given(from_tenths=positive_int, to_tenths=positive_int)
    def test_template_numbers_always_come_from_the_facts(self, from_tenths: int, to_tenths: int) -> None:
        from_ = decimal_div(D(from_tenths % 1000), D(10)) + D("0.1")
        to = decimal_div(D(to_tenths % 1000), D(10))
        fact = nutrient("totalSugarsG", from_, to, claim=to < from_ * D("0.75"))
        r = ExplanationTemplates.render([fact], "en-IN")
        assert ExplanationValidator.validate(r.short, r.bullets, [fact], "en-IN").checks["numericFidelity"]

    def test_validator_rejects_hallucinated_numbers_banned_claims_wrong_language_and_missing_disclosures(self) -> None:
        facts = [nutrient("totalSugarsG", D("10.6"), D("0.2"))]
        assert not ExplanationValidator.validate("Guilt-free choice", [], facts, "en-IN").checks["claims"]
        assert not ExplanationValidator.validate("99% less sugar per 100 ml", [], facts, "en-IN").checks["numericFidelity"]
        assert not ExplanationValidator.validate("98% less sugar per 100 ml", [], facts, "hi-IN").checks["language"]
        assert not ExplanationValidator.validate("Cures diabetes", [], facts, "en-IN").checks["claims"]
        disclose = [Fact(id="f9", kind="ADDITIVE_INTRODUCED", codes=("951",), must_disclose=True)]
        assert not ExplanationValidator.validate("Less sugar", [], disclose, "en-IN").checks["mustDisclose"]
        assert ExplanationValidator.validate("98% less sugar per 100 ml", [], facts, "en-IN").passed

    def test_percentages_are_floored_never_overstated(self) -> None:
        assert ExplanationTemplates.pct(D("-0.9811")) == 98
        assert ExplanationTemplates.multiplier(D("2.0"), D("6.1")) == "3"
        assert ExplanationTemplates.multiplier(D("2.0"), D("3.9")) is None
