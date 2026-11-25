"""Substitutability features, runtime ranker features, ranking score and cart selection (Recommendation/Ranking.cs).

C# float[] feature vectors are lists of Python floats holding float32 values (every element is rounded through
`to_float32`), so they compare bit-for-bit with the C# vectors and the ONNX models trained on them.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from healthswap.domain.knowledge.parsing import equals_ignore_case
from healthswap.domain.knowledge.products import decimal_clamp, decimal_div, decimal_max, decimal_mul, round_decimal
from healthswap.domain.scoring.health_profiles import HealthProfileData
from healthswap.domain.text import HashingEmbedder, TextSimilarity, decimal_to_double, double_to_decimal, round_double, to_float32

_ZERO = Decimal(0)
_ONE = Decimal(1)
_FIFTY = Decimal(50)
_F32_HALF = to_float32(0.5)
_F32_07 = to_float32(0.7)


def _ordinal(s: str) -> bytes:
    """Sort key of StringComparer.Ordinal (UTF-16 code units)."""
    return s.encode("utf-16-be", "surrogatepass")


def _to_single(d: Decimal) -> float:
    """C# (float)decimal."""
    return to_float32(decimal_to_double(d))


class Substitutability:
    """
    Substitutability: "would a typical shopper who intended to buy A accept B for the same use?" Health and price are
    deliberately excluded (doc 03 section 6). The calibrated classifier (ONNX, trained in ml/substitutability) consumes
    exactly these features; the heuristic is the transparent fallback.
    """

    FEATURE_NAMES: tuple[str, ...] = (
        "category_distance", "embedding_cos", "form_match", "flavour_match", "use_occasion_match", "diet_class_match",
        "pack_ratio_log", "brand_tier_same", "same_brand", "state_match",
    )
    NAMES = FEATURE_NAMES

    @staticmethod
    def category_distance(a: str, b: str, approved_map: bool) -> int:
        """0 = same leaf category, 1 = sibling leaf under the same parent, 2 = approved cross-category map (curated, takes precedence)."""
        if a == b:
            return 0
        if approved_map:
            return 2
        pa = a.rfind(".")
        pb = b.rfind(".")
        if pa > 0 and pb > 0 and a[:pa] == b[:pb]:
            return 1
        return 3

    @staticmethod
    def features(a: HealthProfileData, b: HealthProfileData, category_distance: int) -> list[float]:
        def match(x: str | None, y: str | None) -> float:
            if x is None or y is None:
                return _F32_HALF
            return 1.0 if equals_ignore_case(x, y) else 0.0

        emb = TextSimilarity.cosine(HashingEmbedder.embed(Substitutability.describe(a)), HashingEmbedder.embed(Substitutability.describe(b)))
        sa, sb = a.net_quantity, b.net_quantity
        if sa is not None and sb is not None and sa.is_comparable_to(sb) and sa.value > 0 and sb.value > 0:
            pack_ratio = to_float32(abs(math.log(decimal_to_double(decimal_div(sb.value, sa.value)))))
        else:
            pack_ratio = _F32_07
        if a.diet_class == b.diet_class:
            diet_match = 1.0
        elif (a.diet_class in ("VEG", "VEGAN")) == (b.diet_class in ("VEG", "VEGAN")):
            diet_match = _F32_07
        else:
            diet_match = 0.0
        return [
            to_float32(float(category_distance)), emb, match(a.form, b.form), match(a.flavour_family, b.flavour_family),
            match(a.use_occasion, b.use_occasion), diet_match, pack_ratio, 1.0 if a.brand_tier == b.brand_tier else 0.0,
            1.0 if a.brand is not None and equals_ignore_case(a.brand, b.brand) else 0.0,
            1.0 if a.physical_state == b.physical_state else 0.0,
        ]

    @staticmethod
    def describe(p: HealthProfileData) -> str:
        category = p.category_name if p.category_name is not None else p.category_code
        return f"{p.name} {p.brand or ''} {category} {p.flavour_family or ''} {p.form or ''}"

    @staticmethod
    def heuristic(f: Sequence[float]) -> Decimal:
        score = {0.0: 0.80, 1.0: 0.62, 2.0: 0.74}.get(f[0], 0.20)
        score += (f[3] - 0.5) * 0.24  # flavour family
        score += (f[2] - 0.5) * 0.30  # product form
        score += (f[4] - 0.5) * 0.20  # use occasion
        score -= to_float32(1 - f[5]) * 0.30  # dietary class (C# float arithmetic)
        score -= max(0.0, f[6] - 0.69) * 0.20  # pack ratio beyond 2x
        score += (f[1] - 0.3) * 0.15  # text similarity
        if f[9] < 1:
            score -= 0.25  # solid vs liquid
        return double_to_decimal(round_double(min(max(score, 0.0), 1.0), 3))


class RankerFeatures:
    """Runtime ranker features (doc 03 section 7). Fee, margin, commission and sponsor payments are forbidden."""

    FEATURE_NAMES: tuple[str, ...] = (
        "delta_hsi", "substitutability", "price_ratio", "is_cheaper", "brand_switch", "pack_ratio_log",
        "placement_cart", "placement_checkout", "placement_pdp", "placement_agent", "daypart_morning", "daypart_evening",
        "goal_match", "category_beverage", "nova_improvement", "same_brand_family",
    )
    NAMES = FEATURE_NAMES

    FORBIDDEN: tuple[str, ...] = ("fee", "margin", "commission", "sponsor", "reco_fee", "premium", "revenue")

    @dataclass(frozen=True, slots=True)
    class Input:
        delta_hsi: Decimal
        substitutability: Decimal
        price_ratio: Decimal | None
        brand_switch: bool
        pack_ratio: Decimal | None
        placement: str
        local_hour: int
        goal_match: Decimal
        beverage: bool
        nova_improvement: int
        same_brand_family: bool

    @staticmethod
    def vector(i: RankerFeatures.Input) -> list[float]:
        pr = i.pack_ratio
        return [
            _to_single(i.delta_hsi), _to_single(i.substitutability), _to_single(i.price_ratio if i.price_ratio is not None else _ONE),
            1.0 if i.price_ratio is not None and i.price_ratio < 1 else 0.0, 1.0 if i.brand_switch else 0.0,
            to_float32(abs(math.log(decimal_to_double(pr)))) if pr is not None and pr > 0 else 0.0,
            1.0 if i.placement == "CART" else 0.0, 1.0 if i.placement == "CHECKOUT" else 0.0, 1.0 if i.placement in ("PDP", "SEARCH") else 0.0,
            1.0 if i.placement == "AGENT" else 0.0, 1.0 if 5 <= i.local_hour < 12 else 0.0, 1.0 if 17 <= i.local_hour < 23 else 0.0,
            _to_single(i.goal_match), 1.0 if i.beverage else 0.0, to_float32(float(i.nova_improvement)), 1.0 if i.same_brand_family else 0.0,
        ]


@dataclass(frozen=True, slots=True, kw_only=True)
class RankWeights:
    health: Decimal = Decimal("0.45")
    substitutability: Decimal = Decimal("0.25")
    acceptance: Decimal = Decimal("0.30")
    price_penalty: Decimal = Decimal("0.15")
    fatigue: Decimal = Decimal("0.20")
    goal: Decimal = Decimal("0.10")


_GOAL_REASON_CODES = {
    "LOWER_SUGAR": "LOWER_SUGAR",
    "LOWER_SODIUM": "LOWER_SODIUM",
    "LOWER_SAT_FAT": "LOWER_SAT_FAT",
    "HIGHER_PROTEIN": "HIGHER_PROTEIN",
    "HIGHER_FIBRE": "HIGHER_FIBRE",
    "LESS_PROCESSED": "LESS_PROCESSED",
    "FEWER_ADDITIVES": "FEWER_ADDITIVES_OF_CONCERN",
}


class Ranking:
    @staticmethod
    def score(
        w: RankWeights, delta_hsi: Decimal, sub: Decimal, p_accept: Decimal | None, price_ratio: Decimal | None, fatigue: Decimal, goal_match: Decimal
    ) -> Decimal:
        """score = wh dHSI^ + ws Sub + wa P(accept) - wp PricePenalty - wf Fatigue + wg GoalMatch (doc 03 section 7)."""
        normalized_delta = decimal_clamp(decimal_div(delta_hsi, _FIFTY), _ZERO, _ONE)
        price_penalty = decimal_max(_ZERO, price_ratio - _ONE) if price_ratio is not None else _ZERO
        if p_accept is None:
            return round_decimal(decimal_mul(normalized_delta, sub), 6)  # L2 degradation: health delta x substitutability
        return round_decimal(
            decimal_mul(w.health, normalized_delta) + decimal_mul(w.substitutability, sub) + decimal_mul(w.acceptance, p_accept)
            - decimal_mul(w.price_penalty, price_penalty) - decimal_mul(w.fatigue, fatigue) + decimal_mul(w.goal, goal_match),
            6,
        )

    @staticmethod
    def goal_match(goals: Collection[str], reason_codes: Collection[str]) -> Decimal:
        """Goal overlays change ranking weights only, never eligibility (doc 03 2.7)."""
        if len(goals) == 0:
            return _ZERO
        hits = sum(1 for g in goals if g in _GOAL_REASON_CODES and _GOAL_REASON_CODES[g] in reason_codes)
        return round_decimal(decimal_div(Decimal(hits), Decimal(len(goals))), 3)

    @staticmethod
    def is_exploration(evaluation_id: uuid.UUID, rate: Decimal) -> bool:
        """Deterministic exploration bucket: about 5% of evaluations shuffle the top 3 (propensities are logged)."""
        b = evaluation_id.bytes_le
        bucket = (b[10] * 256 + b[11]) % 10000
        return bucket < decimal_mul(rate, Decimal(10000))


@dataclass(frozen=True, slots=True)
class CartCandidate:
    line_id: str
    category_code: str
    delta_hsi: Decimal
    quantity: Decimal
    category_weight: Decimal
    price_ratio: Decimal | None


class CartSelection:
    @staticmethod
    def select(candidates: Iterable[CartCandidate], max_suggestions: int) -> list[str]:
        """Up to M lines maximizing sum(dHSI x qty x weight), at most one per category, ties to the lower price ratio."""

        def score(c: CartCandidate) -> Decimal:
            return decimal_mul(decimal_mul(c.delta_hsi, c.quantity), c.category_weight)

        def price(c: CartCandidate) -> Decimal:
            return c.price_ratio if c.price_ratio is not None else _ONE

        ordered = list(candidates)
        ordered.sort(key=lambda c: _ordinal(c.line_id))
        ordered.sort(key=price)
        ordered.sort(key=score, reverse=True)
        first_per_category: dict[str, CartCandidate] = {}
        for c in ordered:
            first_per_category.setdefault(c.category_code, c)
        chosen = list(first_per_category.values())
        chosen.sort(key=price)
        chosen.sort(key=score, reverse=True)
        return [c.line_id for c in chosen[: max(max_suggestions, 0)]]
