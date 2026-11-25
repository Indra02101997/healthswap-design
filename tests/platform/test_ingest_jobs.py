"""Pure helpers of the knowledge ingestor and the offline jobs (ports of Knowledge/Ingestor.cs and Knowledge/Jobs.cs). No database.

Expected values come from the compiled C# implementation: ScoringJob.Percentile / Changes / InputsHash, SwapGraphBuilder.Diversify
and ProductIngestor.Num / MergeNutrition were invoked (through reflection where private) with the same inputs, and the payload,
nutrition and number texts were produced by System.Text.Json and .NET formatting with the options the C# code uses. Quirks of
the C# code are pinned on purpose (see the diversification and judge cases).
"""

from __future__ import annotations

import hashlib
import struct
import uuid
from datetime import UTC, datetime
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from healthswap.domain.knowledge.products import AllergenPresence, Nutrition, decimal_div
from healthswap.domain.primitives import FixedClock, NetQuantity
from healthswap.domain.recommendation.explanations import Fact
from healthswap.domain.scoring.health_profiles import AdditiveDetail, HealthProfileData
from healthswap.domain.scoring.models import ModelScore
from healthswap.platform.db import DbRole
from healthswap.platform.hot_cache import SwapEdge
from healthswap.platform.knowledge.ingestor import ProductIngestor, _decimal_fixed, _double_fixed
from healthswap.platform.knowledge.jobs import ExplanationPregen, ScoringJob, SwapGraphBuilder, _g6, _nutrition_json, _try_double
from healthswap.platform.llm.gateway import LlmGateway, ProviderResponse
from healthswap.platform.settings import GraphOptions, LakeOptions, LlmOptions

CONTAINS, MAY_CONTAIN, FREE_FROM = AllergenPresence.CONTAINS, AllergenPresence.MAY_CONTAIN, AllergenPresence.FREE_FROM


# ---------------------------------------------------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------------------------------------------------


class _AuditDb:
    """Records the rec.llm_audit rows the gateway writes (Db.in_tx with one execute)."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.roles: list[DbRole] = []

    def in_tx(self, role: DbRole, partner_id: uuid.UUID | None, fn: Any) -> Any:
        self.roles.append(role)
        return fn(self)

    def execute(self, sql: str, params: dict[str, Any]) -> int:
        assert "insert into rec.llm_audit" in sql
        self.rows.append(params)
        return 1


class _QueueProvider:
    """LLM provider returning queued JSON texts and recording every user content."""

    def __init__(self, *outputs: str) -> None:
        self.outputs = list(outputs)
        self.calls: list[str] = []

    @property
    def name(self) -> str:
        return "stub"

    def complete_json(self, spec: Any, user_content: str, deployment: str) -> ProviderResponse:
        self.calls.append(user_content)
        return ProviderResponse(self.outputs.pop(0), 3, 4, f"stub/{deployment}")

    def embed(self, texts: Any, deployment: str, dimensions: int) -> list[list[float]]:
        raise AssertionError("not used")


def _gateway(*outputs: str) -> tuple[LlmGateway, _QueueProvider, _AuditDb]:
    provider, db = _QueueProvider(*outputs), _AuditDb()
    return LlmGateway(provider, LlmOptions(), db), provider, db  # type: ignore[arg-type]


def _uuid(n: int) -> uuid.UUID:
    return uuid.UUID(f"00000000-0000-0000-0000-{n:012d}")


# ---------------------------------------------------------------------------------------------------------------------
# Scoring job: p60 ceilings, PROFILE_CHANGED kinds, inputs hash
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("values", "q", "expected"), [
    ([], "0.6", None),
    (["42.567"], "0.6", "42.567"),  # a single value is returned as is (not rounded)
    (["10.0", "20.0", "30.0", "40.0", "50.0"], "0.6", "34.00"),
    (["1.00", "1.05"], "0.5", "1.02"),  # Math.Round: a midpoint goes to the even digit
    (["1.00", "1.07"], "0.5", "1.04"),
    (["31.5", "40.25", "47.20", "52.0", "58.75", "66.1"], "0.6", "52.00"),  # exact position: x + 0.0 x 0.0 keeps scale 2
    (["12", "18", "25", "61"], "0.6", "23.6"),  # rounding to 2 places never adds digits
])
def test_percentile_matches_csharp(values: list[str], q: str, expected: str | None) -> None:
    result = ScoringJob.percentile([D(v) for v in values], D(q))
    assert (None if result is None else str(result)) == expected


def test_p60_ceiling_uses_the_pack_percentile_as_a_decimal_fraction() -> None:
    q = decimal_div(D(60), D(100))  # pack.Rules.TargetCeilingPercentile / 100m
    hsis = [D(v) for v in ("20.5", "33.33", "41.0", "47.15", "48.0", "55.55", "60.1", "71.0", "80.25")]
    assert str(ScoringJob.percentile(hsis, q)) == "54.04"


def _hp(
    version: int = 10, profile_version: str = "v1", hsi: D | None = D("51.0"), grade: str = "C", flags: tuple[str, ...] = ("HIGH_SUGAR", "WHOLE_GRAIN"),
    allergens: dict[str, AllergenPresence] | None = None, eligible: bool = True, recalled: bool = False,
) -> HealthProfileData:
    return HealthProfileData(
        market="IN", name="x", category_code="C", profile_version=profile_version, version_id=version, hsi=hsi, hsi_grade=grade, flags=flags,
        allergens={"MILK": CONTAINS, "GLUTEN": MAY_CONTAIN} if allergens is None else allergens, eligible=eligible, recalled=recalled,
    )


@pytest.mark.parametrize(("prev", "now", "expected"), [
    (None, _hp(), ["CREATED"]),
    # Same allergens in another order and the same HSI at another scale: no change.
    (_hp(), _hp(hsi=D("51.00"), allergens={"GLUTEN": MAY_CONTAIN, "MILK": CONTAINS}), []),
    (_hp(), _hp(version=11, profile_version="v2"), ["NUTRITION_UPDATED"]),  # a new label version is not a score-model update
    (_hp(), _hp(profile_version="v2"), ["SCORE_MODEL_UPDATED"]),
    # Flags compare as a sequence (order matters); an allergen presence change is reported on its own.
    (_hp(), _hp(flags=("WHOLE_GRAIN", "HIGH_SUGAR"), allergens={"MILK": CONTAINS, "GLUTEN": CONTAINS}), ["ALLERGENS_UPDATED", "SCORE_CHANGED"]),
    (_hp(), _hp(version=11, hsi=None, allergens={"MILK": CONTAINS}, eligible=False, recalled=True),
     ["NUTRITION_UPDATED", "ALLERGENS_UPDATED", "RECALL_FLAGGED", "SCORE_CHANGED", "ELIGIBILITY_CHANGED"]),
    (_hp(flags=(), allergens={}, recalled=True), _hp(grade="D", flags=(), allergens={}), ["SCORE_CHANGED"]),  # lifting a recall is not an event
])
def test_profile_changed_kinds_match_csharp(prev: HealthProfileData | None, now: HealthProfileData, expected: list[str]) -> None:
    assert ScoringJob.changes(prev, now) == expected


COLA = HealthProfileData(
    market="IN", name="Cola", category_code="SOFT_DRINK", profile_version="v1", version_id=1234, nova=4,
    nutrition=Nutrition(
        basis="PER_100ML", energy_kcal=D("42.0"), carbohydrate_g=D("10.6"), carb_definition="TOTAL_INCL_FIBRE", fibre_g=D("0.4"), total_sugars_g=D("10.60"),
        salt_g=D("0.025"), fvln_estimated=True,
    ),
    additives=(AdditiveDetail("150d", "E150d", "Caramel IV", "COLOUR", "C"), AdditiveDetail("338", "E338", "Phosphoric acid", "ACIDITY_REGULATOR", "B")),
)
BREAD = HealthProfileData(
    market="US", name="Bread", category_code="BREAD", profile_version="v1", version_id=77,
    nutrition=Nutrition(energy_kcal=D("265"), sodium_mg=D("480"), added_sugars_g=D("3.5"), total_sugars_g=D("5"), fvln_pct=D("12.5")),
)


def test_nutrition_json_is_the_hot_cache_json_of_csharp() -> None:
    # JsonSerializer.Serialize(nutrition, HotCache.Json): camelCase, nulls omitted, scale kept, computed properties included.
    assert _nutrition_json(COLA.nutrition) == (
        '{"basis":"PER_100ML","energyKcal":42.0,"carbohydrateG":10.6,"carbDefinition":"TOTAL_INCL_FIBRE","totalSugarsG":10.60,"saltG":0.025,'
        '"fibreG":0.4,"fvlnEstimated":true,"isLiquid":true,"sodium":10.0,"salt":0.025,"energyKj":175.7,"availableCarbohydrate":10.2,"limitedSugars":10.60}'
    )
    assert _nutrition_json(BREAD.nutrition) == (
        '{"basis":"PER_100G","energyKcal":265,"carbDefinition":"AVAILABLE","totalSugarsG":5,"addedSugarsG":3.5,"sodiumMg":480,"fvlnPct":12.5,'
        '"fvlnEstimated":false,"isLiquid":false,"sodium":480,"salt":1.2,"energyKj":1108.8,"limitedSugars":3.5}'
    )


def test_inputs_hash_is_byte_identical_to_csharp() -> None:
    assert ScoringJob._inputs_hash(COLA, ModelScore(code="NUTRISCORE", version="2023")).hex() == (
        "90850951da192948ecdb711687a2239b2ca3a8e784117b57609f86f406938264"
    )
    assert ScoringJob._inputs_hash(BREAD, ModelScore(code="FSA_NPM", version="2004.05")).hex() == (  # no NOVA group: empty last field
        "d05b953616b67cfee556834fc1d73d715eaa7449b69d092705591d1b8589b08e"
    )


# ---------------------------------------------------------------------------------------------------------------------
# Swap graph: version strings, retrieval, diversification, T4 judge
# ---------------------------------------------------------------------------------------------------------------------


def test_graph_version_format() -> None:
    now = datetime(2026, 11, 23, 9, 5, 7, 45678, tzinfo=UTC)
    assert SwapGraphBuilder._graph_version("IN", now, False) == "IN-2026-11-23T09:05:07Z"
    assert SwapGraphBuilder._graph_version("IN", now, True) == "IN-2026-11-23T09:05:07Z.045"  # "fff" truncates
    assert SwapGraphBuilder._graph_version("US", datetime(2026, 1, 2, 23, 59, 59, 999999, tzinfo=UTC), True) == "US-2026-01-02T23:59:59Z.999"


def _profile(n: int, category: str, hsi: str | None, eligible: bool = True) -> HealthProfileData:
    return HealthProfileData(
        product_id=_uuid(n), market="IN", name=f"p{n}", category_code=category, profile_version="v1", hsi=None if hsi is None else D(hsi), eligible=eligible,
    )


def test_candidates_block_by_category_and_rank_by_embedding() -> None:
    a = _profile(1, "snacks.chips", "40")
    profiles = [
        a,
        _profile(2, "snacks.chips", "55"),
        _profile(3, "snacks.popcorn", "60"),  # approved target category
        _profile(4, "snacks.chips", "70", eligible=False),
        _profile(5, "snacks.chips", "40"),  # not healthier
        _profile(6, "snacks.nuts", "80"),  # category not approved
        _profile(7, "snacks.chips", None),
        _profile(8, "snacks.chips", "50"),  # same similarity as 2: input order is kept
    ]
    embedding = {
        _uuid(1): [1.0, 0.0], _uuid(2): [0.6, 0.8], _uuid(3): [0.8, 0.6], _uuid(4): [1.0, 0.0], _uuid(5): [1.0, 0.0], _uuid(6): [1.0, 0.0],
        _uuid(7): [1.0, 0.0], _uuid(8): [0.6, 0.8],
    }
    targets = {"snacks.popcorn": None}
    assert [p.product_id for p in SwapGraphBuilder._candidates(a, profiles, targets, embedding, 200)] == [_uuid(3), _uuid(2), _uuid(8)]
    assert [p.product_id for p in SwapGraphBuilder._candidates(a, profiles, targets, embedding, 2)] == [_uuid(3), _uuid(2)]
    unscored = _profile(9, "snacks.chips", None)
    assert SwapGraphBuilder._candidates(unscored, profiles, targets, {**embedding, _uuid(9): [1.0, 0.0]}, 200) == []  # null HSI: never healthier


HIGH = uuid.UUID("80000000-0000-0000-0000-000000000002")
LOW = uuid.UUID("7fffffff-0000-0000-0000-000000000004")


@pytest.mark.parametrize(("items", "max_count", "expected"), [
    # Ties on health gain x substitutability go to the smaller Guid (Guid.CompareTo is unsigned: 7fffffff < 80000000).
    ([(_uuid(1), "10", "0.9", "MAINSTREAM"), (HIGH, "12", "0.8", "MAINSTREAM"), (_uuid(3), "8", "0.95", "MAINSTREAM"), (LOW, "12", "0.8", "MAINSTREAM"),
      (_uuid(5), "5", "0.7", "MAINSTREAM")], 3,
     [(LOW, "MAINSTREAM"), (HIGH, "MAINSTREAM"), (_uuid(1), "MAINSTREAM")]),
    # Two missing tiers: the second one replaces the first one's representative (C# removes the last element each time).
    ([(_uuid(1), "9", "1", "MAINSTREAM"), (_uuid(2), "8", "1", "MAINSTREAM"), (_uuid(3), "7", "1", "MAINSTREAM"), (_uuid(4), "6", "1", "PREMIUM"),
      (_uuid(5), "5", "1", "PRIVATE_LABEL")], 3,
     [(_uuid(1), "MAINSTREAM"), (_uuid(2), "MAINSTREAM"), (_uuid(5), "PRIVATE_LABEL")]),
    ([(_uuid(1), "9", "1", "MAINSTREAM"), (_uuid(2), "8", "1", "PRIVATE_LABEL"), (_uuid(3), "7", "1", "MAINSTREAM"), (_uuid(4), "6", "1", "MAINSTREAM"),
      (_uuid(5), "5", "1", "PREMIUM")], 3,
     [(_uuid(1), "MAINSTREAM"), (_uuid(2), "PRIVATE_LABEL"), (_uuid(5), "PREMIUM")]),
    ([(_uuid(2), "5", "1", "PREMIUM"), (_uuid(1), "9", "1", "MAINSTREAM")], 3, [(_uuid(1), "MAINSTREAM"), (_uuid(2), "PREMIUM")]),
    # 10.0 x 0.90 = 9.000 equals 9 x 1.0 = 9.0: the scale does not matter.
    ([(_uuid(9), "10.0", "0.90", "MAINSTREAM"), (_uuid(8), "9", "1.0", "MAINSTREAM"), (_uuid(7), "9.5", "0.9", "MAINSTREAM")], 2,
     [(_uuid(8), "MAINSTREAM"), (_uuid(9), "MAINSTREAM")]),
])
def test_diversify_matches_csharp(items: list[tuple[uuid.UUID, str, str, str]], max_count: int, expected: list[tuple[uuid.UUID, str]]) -> None:
    scored = [(SwapEdge(to, D(delta), D(sub), (), 1, 2, "CAT", ()), tier, []) for to, delta, sub, tier in items]
    assert [(e.to, tier) for e, tier, _ in SwapGraphBuilder._diversify(scored, max_count)] == expected


@pytest.mark.parametrize(("value", "expected"), [
    (0.123456789, "0.123457"), (1.5e-05, "1.5E-05"), (-0.0, "-0"), (0.0001, "0.0001"), (1234567.0, "1.23457E+06"), (100000.0, "100000"), (0.5, "0.5"),
    (-0.035742, "-0.035742"), (0.1, "0.1"), (3.4e38, "3.4E+38"),
])
def test_embedding_components_format_like_float_g6(value: float, expected: str) -> None:
    float32 = struct.unpack("<f", struct.pack("<f", value))[0]
    assert _g6(float32) == expected


@pytest.mark.parametrize(("node", "expected"), [(D("0.8125"), 0.8125), (1, 1.0), (True, None), ("0.9", None), (D("1E+400"), None), (None, None)])
def test_try_double_accepts_finite_numbers_only(node: Any, expected: float | None) -> None:
    assert _try_double(node) == expected


def _judge_profile(name: str, form: str | None, flavour: str | None, occasion: str | None, diet: str, net: NetQuantity | None) -> HealthProfileData:
    return HealthProfileData(
        market="IN", name=name, category_code="noodles.instant", profile_version="v1", form=form, flavour_family=flavour, use_occasion=occasion,
        diet_class=diet, net_quantity=net,
    )


JUDGE_A = _judge_profile("Caf\u00e9 <Masala> & Co's \"Noodles\"", "NOODLE", None, "MEAL", "VEG", NetQuantity(D("280.50"), "g"))
JUDGE_B = _judge_profile("\u0939\u093f\u0902\u0926\u0940 Oats + Millet", None, "MASALA", None, "UNKNOWN", None)
# JsonSerializer.Serialize(new { A = Attrs(a), B = Attrs(b) }) with default options: nulls written, non-ASCII and HTML-sensitive characters escaped.
JUDGE_PAYLOAD = (
    '{"A":{"name":"Caf\\u00E9 \\u003CMasala\\u003E \\u0026 Co\\u0027s \\u0022Noodles\\u0022","category":"noodles.instant","form":"NOODLE",'
    '"flavourFamily":null,"useOccasion":"MEAL","dietClass":"VEG","netQuantity":280.50},'
    '"B":{"name":"\\u0939\\u093F\\u0902\\u0926\\u0940 Oats \\u002B Millet","category":"noodles.instant","form":null,"flavourFamily":"MASALA",'
    '"useOccasion":null,"dietClass":"UNKNOWN","netQuantity":null}}'
)


def _judgment(score: str) -> str:
    return f'{{"score":{score},"sameUseOccasion":true,"formMatch":true,"issues":["NONE"],"rationale":"ok"}}'


def test_judge_payload_rounding_cache_and_audit() -> None:
    llm, provider, db = _gateway(_judgment("0.8125"))
    builder = SwapGraphBuilder(None, None, None, llm, None, GraphOptions(), None)  # type: ignore[arg-type]
    assert builder._judge(JUDGE_A, JUDGE_B, "IN") == D("0.812")  # Math.Round((decimal)0.8125, 3): half to even
    assert provider.calls == [f"<products>{JUDGE_PAYLOAD}</products>"]
    assert builder._judge(JUDGE_A, JUDGE_B, "IN") == D("0.812")  # cached: no provider call and no second audit row
    assert len(provider.calls) == 1
    (row,) = db.rows
    assert db.roles == [DbRole.KB_WRITER]
    assert (row["task"], row["pv"], row["model"], row["accepted"], row["validation"]) == ("T4", "T4-v1", "stub/hs-t4-judge", True, '{"schema":true,"range":true}')
    assert row["hash"] == LlmGateway.hash("T4-v1\n" + JUDGE_PAYLOAD)  # the audited input is the payload without the <products> wrapper
    assert row["output"] == _judgment("0.8125")


@pytest.mark.parametrize(("output", "validation"), [
    (_judgment("1.2"), '{"schema":true,"range":false}'),
    (_judgment('"0.9"'), '{"schema":true,"range":false}'),  # a string is not a number
    ('{"score":0.7}', '{"schema":false,"range":true}'),  # schema-invalid output is never used
])
def test_judge_rejections_are_audited(output: str, validation: str) -> None:
    llm, _, db = _gateway(output)
    builder = SwapGraphBuilder(None, None, None, llm, None, GraphOptions(), None)  # type: ignore[arg-type]
    assert builder._judge(JUDGE_A, JUDGE_B, "IN") is None
    assert [(r["accepted"], r["validation"]) for r in db.rows] == [(False, validation)]


# ---------------------------------------------------------------------------------------------------------------------
# Explanations: T5 payload and rejections
# ---------------------------------------------------------------------------------------------------------------------


FACTS = [
    Fact(id="F1", kind="NUTRIENT", nutrient="LIMITED_SUGARS_G", from_=D("10.60"), to=D("4.5"), unit="g", rel_change=D("-57.5"), direction="DOWN",
         impact=D("0.610"), claim_eligible=True, codes=("LOW_SUGAR",)),
    Fact(id="F2", kind="ADDITIVE_REMOVED", basis="PER_100ML", must_disclose=True, codes=("150d", "E150d")),
]
# JsonSerializer.Serialize(new { basis, locale, facts }, HotCache.Json): camelCase, nulls omitted, relaxed escaping, scale kept.
POLISH_PAYLOAD = (
    '{"basis":"PER_100G","locale":"hi-IN","facts":[{"id":"F1","kind":"NUTRIENT","nutrient":"LIMITED_SUGARS_G","from":10.60,"to":4.5,"unit":"g",'
    '"basis":"PER_100G","relChange":-57.5,"direction":"DOWN","impact":0.610,"claimEligible":true,"mustDisclose":false,"codes":["LOW_SUGAR"]},'
    '{"id":"F2","kind":"ADDITIVE_REMOVED","from":0,"to":0,"unit":"g","basis":"PER_100ML","direction":"SAME","impact":0,"claimEligible":false,'
    '"mustDisclose":true,"codes":["150d","E150d"]}]}'
)


def test_polish_payload_and_schema_rejection() -> None:
    llm, provider, db = _gateway("[1]")
    pregen = ExplanationPregen(None, None, llm, None, None)  # type: ignore[arg-type]
    text, accepted, result = pregen.polish(FACTS, "PER_100G", "hi-IN")
    assert (text, accepted, result.error) == (None, False, "SCHEMA_INVALID")
    assert provider.calls == [f"<facts>{POLISH_PAYLOAD}</facts>"]
    assert [(r["task"], r["accepted"], r["validation"], r["hash"]) for r in db.rows] == [
        ("T5", False, '{"schema":false}', LlmGateway.hash("T5-v1\n" + POLISH_PAYLOAD)),
    ]


def test_polish_rejects_unvalidated_text_and_audits_the_checks() -> None:
    llm, _, db = _gateway('{"short":"Guilt-free choice with 99% less sugar","bullets":["Cures cravings"],"factIdsUsed":["F1"]}')
    pregen = ExplanationPregen(None, None, llm, None, None)  # type: ignore[arg-type]
    text, accepted, _ = pregen.polish(FACTS, "PER_100G", "en-IN")
    assert (text, accepted) == (None, False)
    (row,) = db.rows
    assert row["accepted"] is False and '"numericFidelity":false' in row["validation"]


# ---------------------------------------------------------------------------------------------------------------------
# Ingestor helpers
# ---------------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), [
    ("12.50", "12.50"), ("280", "280"), ("-3.0", "-3.0"), ('"5"', None), ("true", None), ("null", None), ("[1]", None), ("{}", None), ("1e30", None),
    ("79228162514264337593543950335", "79228162514264337593543950335"), ("0.000001", "0.000001"),
])
def test_num_reads_decimal_json_values_only(text: str, expected: str | None) -> None:
    result = ProductIngestor._num(text)
    assert (None if result is None else str(result)) == expected


def test_merge_nutrition_reads_the_selected_values_of_the_basis() -> None:
    selected = {
        "nutrient.ENERC_KCAL.PER_100G": ("280", 1, 1, False),
        "nutrient.NA.PER_100G": ("520.0", 1, 1, False),
        "nutrient.SUGAR.PER_100ML": ("5", 1, 1, False),  # another basis
        "nutrient.FAT.PER_100G": ('"x"', 1, 1, False),  # not a number
        "nutrient.FIBTG.PER_100G": ("3.40", 2, 2, True),
    }
    fallback = Nutrition(fvln_pct=D("12.5"), fvln_estimated=True, salt_g=D("1.2"), protein_g=D("9"))
    merged = ProductIngestor._merge_nutrition(selected, "PER_100G", "AVAILABLE", fallback)
    assert _nutrition_json(merged) == (
        '{"basis":"PER_100G","energyKcal":280,"carbDefinition":"AVAILABLE","sodiumMg":520.0,"fibreG":3.40,"fvlnPct":12.5,"fvlnEstimated":true,'
        '"isLiquid":false,"sodium":520.0,"salt":1.30,"energyKj":1171.5}'
    )


def test_allergens_json_uses_the_csharp_enum_names_sorted_by_code() -> None:
    allergens = {"SOY": FREE_FROM, "MILK": CONTAINS, "GLUTEN_CEREALS": MAY_CONTAIN}
    assert ProductIngestor._allergens_json(allergens) == '{"GLUTEN_CEREALS":"MayContain","MILK":"Contains","SOY":"FreeFrom"}'


def test_label_canonical_form() -> None:
    selected = {
        "nutrient.SUGAR.PER_100G": ("5.0", 1, 1, False),
        "ingredients_text": ('"Wheat flour"', 1, 1, False),
        "nutrient.FATRN.PER_100G": ("0", 2, 2, False),
        "allergens": ('{"MILK":"Contains"}', 1, 1, False),
        "nutrient.FAT.PER_100G": ("1", 2, 2, True),
        "nutrient.ENERC_KCAL.PER_100G": ("280", 1, 1, False),
    }
    assert ProductIngestor._label_canonical(selected, "Wheat flour, caf\u00e9", ["IN_FSSAI_VEG"], D("280.50"), None) == (
        '{"n":["nutrient.ENERC_KCAL.PER_100G=280","nutrient.FAT.PER_100G=1","nutrient.FATRN.PER_100G=0","nutrient.SUGAR.PER_100G=5.0"],'
        '"ingredientsText":"Wheat flour, caf\\u00E9","allergens":"{\\u0022MILK\\u0022:\\u0022Contains\\u0022}","DietaryMarks":["IN_FSSAI_VEG"],'
        '"net":280.50,"serving":null}'
    )
    assert '"allergens":null' in ProductIngestor._label_canonical({}, None, [], None, D("30"))


def test_bronze_copy_is_written_once_per_content_hash(tmp_path: Path) -> None:
    clock = FixedClock(datetime(2026, 11, 23, 23, 59, 59, tzinfo=UTC))
    sha = hashlib.sha256(b"payload").digest()
    candidate = SimpleNamespace(source_code="OFF")
    ingestor = ProductIngestor(None, None, None, None, clock, LakeOptions(enabled=True, root=tmp_path))  # type: ignore[arg-type]
    uri = ingestor._write_bronze(candidate, b"payload", sha)  # type: ignore[arg-type]
    assert uri == f"lake://bronze/sources/OFF/2026-11-23/{sha.hex()[:16]}.json"
    path = tmp_path / "bronze" / "sources" / "OFF" / "2026-11-23" / f"{sha.hex()[:16]}.json"
    assert path.read_bytes() == b"payload"
    assert ingestor._write_bronze(candidate, b"changed", sha) == uri  # type: ignore[arg-type]
    assert path.read_bytes() == b"payload"  # immutable: never overwritten
    disabled = ProductIngestor(None, None, None, None, clock, LakeOptions(enabled=False, root=tmp_path / "off"))  # type: ignore[arg-type]
    assert disabled._write_bronze(candidate, b"payload", sha) == uri and not (tmp_path / "off").exists()  # type: ignore[arg-type]


@pytest.mark.parametrize(("value", "expected"), [(0.825, "0.83"), (1.005, "1.01"), (0.5, "0.50"), (0.994999999999, "0.99"), (2.675, "2.68"), (0.0, "0.00")])
def test_double_fixed_formats_like_dotnet_custom_formats(value: float, expected: str) -> None:
    # .NET takes a double to 15 significant digits before applying "0.00" (0.825 is 0.82499999999999996 in binary).
    assert _double_fixed(value, 2) == expected


@pytest.mark.parametrize(("value", "two", "three"), [
    ("0.955", "0.96", "0.955"), ("0.945", "0.95", "0.945"), ("0.9549", "0.95", "0.955"), ("0.70", "0.70", "0.700"), ("1", "1.00", "1.000"),
    ("0.0005", "0.00", "0.001"),
])
def test_decimal_fixed_rounds_half_away_from_zero(value: str, two: str, three: str) -> None:
    assert (_decimal_fixed(D(value), 2), _decimal_fixed(D(value), 3)) == (two, three)
