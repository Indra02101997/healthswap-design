"""Deterministic knowledge-plane rules: ingredient parsing, allergens, diet, QA rules, GTIN, entity resolution, survivorship (port of KnowledgeTests.cs)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import pytest

from healthswap.domain.knowledge.parsing import (
    AdditiveEntry,
    AllergenDeriver,
    DietClassifier,
    IngredientEntry,
    IngredientParser,
    IngredientTaxonomy,
)
from healthswap.domain.knowledge.products import AllergenPresence, Nutrition
from healthswap.domain.knowledge.quality import EntityResolution, QaAction, QaInput, QaRules, Survivorship
from healthswap.domain.primitives import NetQuantity, gtin_is_restricted_circulation, gtin_try_normalize, utc_now

TAX = IngredientTaxonomy(
    [
        ("refined wheat flour", IngredientEntry("refined wheat flour", ("REFINED_FLOUR",), ("WHEAT", "GLUTEN_CEREALS"))),
        ("maida", IngredientEntry("refined wheat flour", ("REFINED_FLOUR",), ("WHEAT", "GLUTEN_CEREALS"))),
        ("palm oil", IngredientEntry("palm oil", ("PALM_OIL", "OIL"), ())),
        ("sugar", IngredientEntry("sugar", ("ADDED_SUGAR",), ())),
        ("salt", IngredientEntry("salt", ("SALT",), ())),
        ("milk solids", IngredientEntry("milk solids", ("DAIRY",), ("MILK",))),
        ("lemon juice", IngredientEntry("lemon juice", ("FRUIT_VEG_LEGUME",), ())),
        ("carbonated water", IngredientEntry("carbonated water", (), ())),
        ("dehydrated vegetables", IngredientEntry("dehydrated vegetables", ("FRUIT_VEG_LEGUME",), ())),
    ],
    [
        AdditiveEntry("322", "Lecithins", ("EMULSIFIER",), ("lecithin", "soy lecithin")),
        AdditiveEntry("500", "Sodium carbonates", ("RAISING_AGENT",), ("sodium bicarbonate",)),
        AdditiveEntry("503", "Ammonium carbonates", ("RAISING_AGENT",), ()),
        AdditiveEntry("129", "Allura Red AC", ("COLOUR",), ("red 40",)),
        AdditiveEntry("951", "Aspartame", ("SWEETENER",), ("aspartame",)),
        AdditiveEntry("150d", "Sulphite ammonia caramel", ("COLOUR",), ("caramel colour",)),
    ],
)


def test_class_titles_with_ins_numbers_become_additives() -> None:
    r = IngredientParser.parse("Refined wheat flour (maida), Sugar, Raising agents (INS 503(ii), INS 500(ii)), Emulsifier (INS 322), Colour (E129)", TAX)
    assert r.additives == ("503", "500", "322", "129")
    assert "ARTIFICIAL_COLOUR" in r.flags
    assert "ADDED_SUGAR" in r.flags
    assert "WHEAT" in r.allergen_hints
    assert "SOY" not in r.allergen_hints  # lecithin implies soy only when the label says soy


def test_soy_lecithin_implies_soy() -> None:
    r = IngredientParser.parse("Sugar, Emulsifier (soya lecithin INS 322)", TAX)
    assert "SOY" in r.allergen_hints


def test_us_additive_names_and_sweeteners_are_detected() -> None:
    r = IngredientParser.parse("Carbonated water, Red 40, Aspartame", TAX)
    assert "129" in r.additives
    assert "951" in r.additives
    assert "NNS" in r.flags


def test_quid_percentages_drive_the_fruit_veg_share() -> None:
    r = IngredientParser.parse("Carbonated water, Lemon juice (3%)", TAX)
    assert r.fvln_pct_estimate == D(3)
    assert r.fvln_from_quid


def test_allergen_statements_split_contains_and_may_contain() -> None:
    a = AllergenDeriver.from_statement("Contains wheat, gluten and groundnut. May contain milk, soy, mustard and nuts.")
    assert a["WHEAT"] == AllergenPresence.CONTAINS
    assert a["PEANUTS"] == AllergenPresence.CONTAINS
    assert a["MILK"] == AllergenPresence.MAY_CONTAIN
    assert a["TREE_NUTS"] == AllergenPresence.MAY_CONTAIN


def test_ingredient_allergens_win_over_statements() -> None:
    merged, undeclared = AllergenDeriver.reconcile({"MILK": AllergenPresence.MAY_CONTAIN}, ["MILK"])
    assert merged["MILK"] == AllergenPresence.CONTAINS
    assert undeclared == ("MILK",)


def test_vegan_requires_a_mark() -> None:
    assert DietClassifier.classify(["IN_FSSAI_VEG"], set(), {}).diet_class == "VEG"
    assert DietClassifier.classify(["IN_FSSAI_VEGAN"], set(), {}).diet_class == "VEGAN"
    contradicted = DietClassifier.classify(["IN_FSSAI_VEG"], {"GELATIN"}, {})
    assert contradicted.diet_class == "NON_VEG"
    assert contradicted.veg_mark_contradicted


@pytest.mark.parametrize(
    ("raw", "valid"),
    [
        ("8901234567890", True),
        ("8901234567891", False),
        ("099999000016", True),
        ("12345", False),
    ],
)
def test_gtin_check_digits(raw: str, valid: bool) -> None:
    assert (gtin_try_normalize(raw) is not None) == valid


def test_gtins_normalize_to_14_digits() -> None:
    assert gtin_try_normalize("099999000016") == "00099999000016"
    assert gtin_is_restricted_circulation("02012345000014")


def test_qa01_flags_energy_inconsistency() -> None:
    f = QaRules.run(QaInput(nutrition=Nutrition(energy_kcal=D(100), protein_g=D(10), carbohydrate_g=D(60), total_fat_g=D(20)), now=utc_now()))
    assert any(x.code == "QA-01" for x in f)


def test_qa05_does_not_double_count_us_fibre() -> None:
    # US total carbohydrate includes fibre: 10 + 2 + 85 = 97 g, fibre must not be added again.
    us = Nutrition(protein_g=D(10), total_fat_g=D(2), carbohydrate_g=D(85), fibre_g=D(10), carb_definition="TOTAL_INCL_FIBRE", energy_kcal=D(398))
    assert not any(x.code == "QA-05" for x in QaRules.run(QaInput(nutrition=us, now=utc_now())))
    eu = replace(us, carb_definition="AVAILABLE")
    assert any(x.code == "QA-05" for x in QaRules.run(QaInput(nutrition=eu, now=utc_now())))


def test_qa02_rejects_sugars_above_carbohydrate_and_qa14_quarantines_prohibited_additives() -> None:
    f = QaRules.run(QaInput(nutrition=Nutrition(total_sugars_g=D(20), carbohydrate_g=D(10)), tier_a_additives=["924a"], now=utc_now()))
    assert any(x.code == "QA-02" and x.action == QaAction.REJECT_FIELD for x in f)
    assert any(x.code == "QA-14" and x.action == QaAction.QUARANTINE for x in f)


def test_entity_resolution_never_merges_different_gtins() -> None:
    a = EntityResolution.Candidate("Swaad Masala Noodles 280 g", "Swaad", NetQuantity(D(280), "g"), None, [], "08901234567890")
    b = replace(a, gtin="08909990000019")
    assert EntityResolution.heuristic_score(EntityResolution.features(a, b)) == D("0.05")
    promo = EntityResolution.Candidate("Swaad Masala Noodles 280g Pack of 4", "Swaad", NetQuantity(D(280), "g"), None, [], None)
    assert EntityResolution.heuristic_score(EntityResolution.features(replace(a, gtin=None), promo)) >= EntityResolution.REVIEW_THRESHOLD


def test_survivorship_prefers_trust_tier_and_flags_conflicts() -> None:
    at = utc_now()
    d = Survivorship.select("nutrient.NA.PER_100G", [
        Survivorship.Candidate("nutrient.NA.PER_100G", D(340), "340", 2, at, 1, True),
        Survivorship.Candidate("nutrient.NA.PER_100G", D(280), "280", 1, at - timedelta(days=30), 2, True),
    ])
    assert d.selected is not None and d.selected.source_record_id == 2
    assert d.conflict
    off = Survivorship.select("nutrient.SUGAR.PER_100G", [
        Survivorship.Candidate("nutrient.SUGAR.PER_100G", D(10), "10", 1, at - timedelta(days=90), 1, True),
        Survivorship.Candidate("nutrient.SUGAR.PER_100G", D(30), "30", 4, at, 2, True),
    ])
    assert off.selected is not None and off.selected.source_record_id == 1  # community data never overrides brand data
    assert not off.conflict


def test_sodium_is_derived_from_salt() -> None:
    n = Nutrition(salt_g=D("2.5"))
    assert n.sodium == D(1000)
    assert Nutrition(sodium_mg=D(1000)).salt == D("2.5")
