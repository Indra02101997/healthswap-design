"""Threshold-points nutrient-profiling models, front-of-pack bands, additive concern tiers and the HealthSwap Index (Scoring/Models.cs).

One engine serves Nutri-Score 2023, UK NPM 2004/05, Health Star Rating and the FSSAI INR draft: a regulatory change is a
data change (doc 03 2.1). Decimal arithmetic follows C#: Math.Round is `round_decimal` (banker's rounding that never adds
trailing zeros), divisions use `decimal_div` (C# digits) and Math.Max / Math.Clamp keep the C# operand on ties.

The `from_dict` loaders take already-parsed JSON (json.loads of data/packs/models/*.json, data/packs/XX.json,
data/packs/additives.json or the rec.scoring_model params) and read it like System.Text.Json with the platform's HsJson
options: property names match case-insensitively (dictionary keys do not), unknown properties are ignored, absent
properties keep the C# defaults, C# `required` properties must be present and numbers are strict (JSON numbers only;
integers must be integral Int32 values). JSON numbers parsed as float are read through repr(), so 0.70 stays 0.7 and 5.0
keeps its scale. Unlike System.Text.Json, null (or a missing positional-record argument) is rejected for non-nullable
strings and collections instead of producing a null reference. Every rejection raises ValueError.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from enum import IntEnum
from typing import Any, TypeVar

from healthswap.domain.knowledge.products import ProductData, decimal_clamp, decimal_div, decimal_max, round_decimal

T = TypeVar("T")

INT32_MAX = 2**31 - 1
INT32_MIN = -(2**31)
_ZERO = Decimal(0)
_HUNDRED = Decimal(100)

# ---------------------------------------------------------------------------------------------------------------------
# JSON reading (System.Text.Json semantics, see the module docstring); shared with health_profiles.py
# ---------------------------------------------------------------------------------------------------------------------

_Reader = Callable[[Any, str], Any]
_INT_KEY_RX = re.compile(r"-?[0-9]+")


def _json_at(path: str) -> str:
    return "$." + path if path else "$"


def _json_fail(path: str, expected: str, value: Any) -> ValueError:
    return ValueError(f"{_json_at(path)}: the JSON value {value!r} could not be converted to {expected}")


def _json_child(path: str, name: str | int) -> str:
    if isinstance(name, int):
        return f"{path}[{name}]"
    return f"{path}.{name}" if path else name


def _json_object(data: Any, path: str, type_name: str, required: Sequence[str] = ()) -> dict[str, Any]:
    """Case-insensitive view of a JSON object (PropertyNameCaseInsensitive: the last of several case variants wins)."""
    if not isinstance(data, Mapping):
        raise _json_fail(path, type_name, data)
    values = {str(key).lower(): value for key, value in data.items()}
    missing = [name for name in required if name.lower() not in values]
    if missing:
        names = ", ".join(repr(name) for name in missing)
        raise ValueError(f"{_json_at(path)}: JSON deserialization for type '{type_name}' was missing required properties including: {names}.")
    return values


def _json_props(values: Mapping[str, Any], path: str, spec: Sequence[tuple[str, str, _Reader]]) -> dict[str, Any]:
    """Keyword arguments for the properties present in `values` (absent ones keep the dataclass defaults)."""
    kwargs: dict[str, Any] = {}
    for json_name, attr, read in spec:
        key = json_name.lower()
        if key in values:
            kwargs[attr] = read(values[key], _json_child(path, json_name))
    return kwargs


def _json_decimal(value: Any, path: str) -> Decimal:
    if isinstance(value, Decimal):
        if value.is_finite():
            return value
    elif isinstance(value, int) and not isinstance(value, bool):
        return Decimal(value)
    elif isinstance(value, float) and math.isfinite(value):
        return Decimal(repr(value))
    raise _json_fail(path, "a Decimal", value)


def _json_decimal_or_none(value: Any, path: str) -> Decimal | None:
    return None if value is None else _json_decimal(value, path)


def _json_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not INT32_MIN <= value <= INT32_MAX:
        raise _json_fail(path, "an Int32", value)
    return value


def _json_bool(value: Any, path: str) -> bool:
    if not isinstance(value, bool):
        raise _json_fail(path, "a Boolean", value)
    return value


def _json_str(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise _json_fail(path, "a String", value)
    return value


def _json_str_or_none(value: Any, path: str) -> str | None:
    return None if value is None else _json_str(value, path)


def _json_list(value: Any, path: str, read: Callable[[Any, str], T]) -> tuple[T, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise _json_fail(path, "an array", value)
    return tuple(read(item, _json_child(path, i)) for i, item in enumerate(value))


def _json_map(value: Any, path: str, read: Callable[[Any, str], T]) -> dict[str, T]:
    """Dictionary<string, T>: keys are taken as they are (case-sensitive, like the C# dictionary)."""
    if not isinstance(value, Mapping):
        raise _json_fail(path, "an object", value)
    return {_json_str(key, path): read(item, _json_child(path, key)) for key, item in value.items()}


def _json_date(value: Any, path: str) -> date:
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(_json_str(value, path))
    except ValueError:
        raise _json_fail(path, "a date (YYYY-MM-DD)", value) from None


# ---------------------------------------------------------------------------------------------------------------------
# Threshold-points models
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class PointsComponent:
    """One component of a threshold-points model: points = number of thresholds strictly exceeded."""

    nutrient: str
    thresholds: tuple[Decimal, ...]
    # Optional explicit points per threshold step (for example FVL 40/60/80 % gives 1/2/5).
    points: tuple[int, ...] | None = None

    def score(self, value: Decimal) -> int:
        steps = sum(1 for t in self.thresholds if value > t)
        if steps == 0:
            return 0
        p = self.points
        return p[min(steps, len(p)) - 1] if p else steps

    @staticmethod
    def from_dict(data: Mapping[str, Any], path: str = "") -> PointsComponent:
        values = _json_object(data, path, "PointsComponent", ("nutrient", "thresholds"))
        return PointsComponent(**_json_props(values, path, _POINTS_COMPONENT))


@dataclass(frozen=True, slots=True, kw_only=True)
class ProteinRule:
    # When negative points reach this value protein is not counted ...
    negative_at_least: int = 11
    # ... unless this positive component reaches the given points (UK NPM: FVN = 5). None = no exception.
    unless_component: str | None = None
    unless_points_at_least: int = 0
    protein_component: str = "PROTEIN_G"

    @staticmethod
    def from_dict(data: Mapping[str, Any], path: str = "") -> ProteinRule:
        return ProteinRule(**_json_props(_json_object(data, path, "ProteinRule"), path, _PROTEIN_RULE))


@dataclass(frozen=True, slots=True)
class GradeBand:
    max: Decimal
    grade: str

    @staticmethod
    def from_dict(data: Mapping[str, Any], path: str = "") -> GradeBand:
        values = _json_object(data, path, "GradeBand")
        band_max = _json_decimal(values["max"], _json_child(path, "max")) if "max" in values else _ZERO
        return GradeBand(band_max, _json_str(values.get("grade"), _json_child(path, "grade")))


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelVariant:
    negative: tuple[PointsComponent, ...] = ()
    positive: tuple[PointsComponent, ...] = ()
    protein_rule: ProteinRule | None = None
    grades: tuple[GradeBand, ...] = ()
    # Score bounds used to normalize this variant into the HSI nutrient-profile component (doc 03 2.5).
    hsi_min: Decimal = _ZERO
    hsi_max: Decimal = Decimal(40)
    # Points added when non-sugar sweeteners are present (Nutri-Score 2023 beverages).
    sweetener_penalty: int = 0
    # Fixed grade for variants such as plain water.
    fixed_grade: str | None = None

    @staticmethod
    def from_dict(data: Mapping[str, Any], path: str = "") -> ModelVariant:
        return ModelVariant(**_json_props(_json_object(data, path, "ModelVariant"), path, _MODEL_VARIANT))


@dataclass(frozen=True, slots=True, kw_only=True)
class PointsModelParams:
    """Parameters of a threshold-points nutrient-profiling model, stored in rec.scoring_model.params."""

    code: str
    version: str
    variants: Mapping[str, ModelVariant]
    # scoring_category -> variant name (default FOOD).
    category_variants: Mapping[str, str] = field(default_factory=dict)
    # "POINTS" (lower is better) or "STARS" (grade expressed as stars, still lower points are better).
    grade_kind: str = "POINTS"
    verification: str | None = None

    @staticmethod
    def from_dict(data: Mapping[str, Any], path: str = "") -> PointsModelParams:
        """A model file (data/packs/models/*.json) or rec.scoring_model.params, already parsed with json.loads."""
        values = _json_object(data, path, "PointsModelParams", ("code", "version", "variants"))
        return PointsModelParams(**_json_props(values, path, _POINTS_MODEL_PARAMS))


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelScore:
    code: str
    version: str
    value: Decimal | None = None
    grade: str | None = None
    variant: str = "FOOD"
    components: Mapping[str, int] = field(default_factory=dict)
    # Normalized 0-100 (higher is better) for the HSI nutrient-profile component. [JsonIgnore] in C#: it is not part of
    # the serialized health profile, so a profile read back from the cache has normalized = None.
    normalized: Decimal | None = field(default=None, metadata={"json_ignore": True})


_POINTS_COMPONENT: tuple[tuple[str, str, _Reader], ...] = (
    ("nutrient", "nutrient", _json_str),
    ("thresholds", "thresholds", lambda v, p: _json_list(v, p, _json_decimal)),
    ("points", "points", lambda v, p: None if v is None else _json_list(v, p, _json_int)),
)
_PROTEIN_RULE: tuple[tuple[str, str, _Reader], ...] = (
    ("negativeAtLeast", "negative_at_least", _json_int),
    ("unlessComponent", "unless_component", _json_str_or_none),
    ("unlessPointsAtLeast", "unless_points_at_least", _json_int),
    ("proteinComponent", "protein_component", _json_str),
)
_MODEL_VARIANT: tuple[tuple[str, str, _Reader], ...] = (
    ("negative", "negative", lambda v, p: _json_list(v, p, PointsComponent.from_dict)),
    ("positive", "positive", lambda v, p: _json_list(v, p, PointsComponent.from_dict)),
    ("proteinRule", "protein_rule", lambda v, p: None if v is None else ProteinRule.from_dict(v, p)),
    ("grades", "grades", lambda v, p: _json_list(v, p, GradeBand.from_dict)),
    ("hsiMin", "hsi_min", _json_decimal),
    ("hsiMax", "hsi_max", _json_decimal),
    ("sweetenerPenalty", "sweetener_penalty", _json_int),
    ("fixedGrade", "fixed_grade", _json_str_or_none),
)
_POINTS_MODEL_PARAMS: tuple[tuple[str, str, _Reader], ...] = (
    ("code", "code", _json_str),
    ("version", "version", _json_str),
    ("variants", "variants", lambda v, p: _json_map(v, p, ModelVariant.from_dict)),
    ("categoryVariants", "category_variants", lambda v, p: _json_map(v, p, _json_str)),
    ("gradeKind", "grade_kind", _json_str),
    ("verification", "verification", _json_str_or_none),
)


class PointsModel:
    """
    Generic engine for Nutri-Score 2023, UK NPM 2004/05, Health Star Rating and the FSSAI INR draft: all are
    "negative points minus positive points" models with category variants, a protein rule and grade bands.
    Only the parameters differ, so a regulatory change is a data change (doc 03 2.1, "pluggable and versioned").
    """

    @staticmethod
    def score(p: PointsModelParams, product: ProductData) -> ModelScore:
        variant_name = p.category_variants.get(product.scoring_category)
        if variant_name is None:
            variant_name = "BEVERAGE" if product.nutrition.is_liquid and "BEVERAGE" in p.variants else "FOOD"
        v = p.variants.get(variant_name)
        if v is None:
            variant_name = "FOOD"
            v = p.variants["FOOD"]

        if v.fixed_grade is not None:
            return ModelScore(code=p.code, version=p.version, value=_ZERO, grade=v.fixed_grade, variant=variant_name, normalized=_HUNDRED)

        components: dict[str, int] = {}
        negative = 0
        for c in v.negative:
            value = product.nutrition.get(c.nutrient)
            if value is None:
                return PointsModel._missing(p, variant_name)
            pts = c.score(value)
            components[c.nutrient] = pts
            negative += pts
        if v.sweetener_penalty > 0 and product.has("NNS"):
            components["SWEETENERS"] = v.sweetener_penalty
            negative += v.sweetener_penalty

        positive = 0
        positive_points: dict[str, int] = {}
        for c in v.positive:
            value = product.nutrition.get(c.nutrient)
            # Missing positive inputs score 0 (conservative).
            positive_points[c.nutrient] = c.score(value if value is not None else _ZERO)
        rule = v.protein_rule
        for nutrient, pts in positive_points.items():
            counted = pts
            if rule is not None and nutrient == rule.protein_component and negative >= rule.negative_at_least:
                exception = rule.unless_component is not None and positive_points.get(rule.unless_component, 0) >= rule.unless_points_at_least
                if not exception and product.scoring_category != "CHEESE":
                    counted = 0
            components[nutrient] = -counted
            positive += counted

        score = negative - positive
        first = next((g for g in v.grades if score <= g.max), None)
        grade = first.grade if first is not None else None
        if grade is None and v.grades:
            grade = v.grades[-1].grade
        if v.hsi_max == v.hsi_min:
            normalized = Decimal(50)
        else:
            normalized = decimal_clamp(decimal_div(_HUNDRED * (v.hsi_max - score), v.hsi_max - v.hsi_min), _ZERO, _HUNDRED)
        return ModelScore(
            code=p.code, version=p.version, value=Decimal(score), grade=grade, variant=variant_name, components=components,
            normalized=round_decimal(normalized, 2),
        )

    @staticmethod
    def _missing(p: PointsModelParams, variant: str) -> ModelScore:
        return ModelScore(code=p.code, version=p.version, value=None, grade=None, variant=variant)

    @staticmethod
    def grade_rank(p: PointsModelParams, s: ModelScore) -> int:
        """Grade order for "strictly better" comparisons (R2). Lower index is better; INT32_MAX when ungraded."""
        if s.grade is None:
            return INT32_MAX
        v = p.variants.get(s.variant)
        if v is None:
            v = p.variants["FOOD"]
        idx = next((i for i, g in enumerate(v.grades) if g.grade == s.grade), -1)
        if idx < 0:
            return -1 if s.grade == v.fixed_grade else INT32_MAX
        return idx


# ---------------------------------------------------------------------------------------------------------------------
# Front-of-pack bands
# ---------------------------------------------------------------------------------------------------------------------


class FopBand(IntEnum):
    """Ordered like the C# enum (Low = 0, Medium = 1, High = 2): the eligibility rules compare bands with `>`.

    The C# cache JSON (JsonStringEnumConverter) writes the member names "Low", "Medium" and "High".
    """

    LOW = 0
    MEDIUM = 1
    HIGH = 2


@dataclass(frozen=True, slots=True)
class FopThreshold:
    """Front-of-pack thresholds for one nutrient (reg.fop_threshold)."""

    nutrient: str
    physical_state: str
    basis: str
    low_max: Decimal
    high_min: Decimal
    high_inclusive: bool

    @staticmethod
    def from_dict(data: Mapping[str, Any], path: str = "") -> FopThreshold:
        """One entry of a pack's "fop" array."""
        v = _json_object(data, path, "FopThreshold")

        def dec(name: str) -> Decimal:
            return _json_decimal(v[name.lower()], _json_child(path, name)) if name.lower() in v else _ZERO

        return FopThreshold(
            _json_str(v.get("nutrient"), _json_child(path, "nutrient")),
            _json_str(v.get("physicalstate"), _json_child(path, "physicalState")),
            _json_str(v.get("basis"), _json_child(path, "basis")),
            dec("lowMax"),
            dec("highMin"),
            _json_bool(v["highinclusive"], _json_child(path, "highInclusive")) if "highinclusive" in v else False,
        )


_FOP_NUTRIENT_KEYS = {
    "SUGAR": "SUGARS_G",
    "SUGAD": "LIMITED_SUGARS_G",
    "FASAT": "SAT_FAT_G",
    "FATRN": "TRANS_FAT_G",
    "NA": "SODIUM_MG",
    "SALT": "SALT_G",
    "FAT": "FAT_G",
}


class FopBands:
    @staticmethod
    def compute(p: ProductData, thresholds: Sequence[FopThreshold], daily_values: Mapping[str, Decimal]) -> dict[str, FopBand]:
        """Band per limited nutrient. PER_SERVING_PCT_DV thresholds need a serving size and daily values."""
        result: dict[str, FopBand] = {}
        state = "LIQUID" if p.nutrition.is_liquid else "SOLID"
        groups: dict[str, list[FopThreshold]] = {}
        for threshold in thresholds:
            groups.setdefault(threshold.nutrient, []).append(threshold)
        for group in groups.values():
            t = next((x for x in group if x.physical_state == state), None)
            if t is None:
                t = next((x for x in group if x.physical_state == "ANY"), None)
            if t is None:
                continue
            per100 = p.nutrition.get(FopBands.nutrient_key(t.nutrient))
            if per100 is None:
                continue
            if t.basis == "PER_SERVING_PCT_DV":
                serving = p.serving_grams
                dv = daily_values.get(t.nutrient)
                if serving is None or dv is None or dv <= 0:
                    continue
                value = decimal_div(decimal_div(per100 * serving, _HUNDRED), dv) * _HUNDRED
            else:
                value = per100
            high = value >= t.high_min if t.high_inclusive else value > t.high_min
            result[t.nutrient] = FopBand.HIGH if high else FopBand.LOW if value <= t.low_max else FopBand.MEDIUM
        return result

    @staticmethod
    def nutrient_key(code: str) -> str:
        return _FOP_NUTRIENT_KEYS.get(code, code)


# ---------------------------------------------------------------------------------------------------------------------
# Additive concern tiers
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AdditiveRegulation:
    """Additive concern inputs from regulators and authoritative bodies only (doc 03 2.3). `hazard_tier` is "B", "C" or None."""

    code: str
    name: str
    functional_classes: tuple[str, ...]
    status_by_jurisdiction: Mapping[str, str]
    hazard_tier: str | None

    @staticmethod
    def from_additives_file(data: Mapping[str, Any], as_of: date) -> dict[str, AdditiveRegulation]:
        """data/packs/additives.json (already parsed) -> regulations by INS code, as the platform reads them back from kb/reg.

        Mirrors ReferenceDataLoader.ApplyAdditivesAsync + ReferenceDataRepository.GetPackAsync: only statuses in force
        on `as_of` (effectiveFrom <= as_of < effectiveTo) count, the first one per jurisdiction wins, and the hazard
        tier is the lowest tier letter over all hazard findings. A repeated code replaces the earlier entry.
        """
        root = _json_object(data, "", "AdditivesFile")
        result: dict[str, AdditiveRegulation] = {}
        for i, item in enumerate(_json_list(root.get("additives"), "additives", lambda v, p: v)):
            path = f"additives[{i}]"
            a = _json_object(item, path, "AdditiveFile", ("code", "name"))
            code = _json_str(a["code"], _json_child(path, "code"))
            classes = _json_list(a["classes"], _json_child(path, "classes"), _json_str) if "classes" in a else ()
            statuses: dict[str, str] = {}
            for j, s in enumerate(_json_list(a["statuses"], _json_child(path, "statuses"), lambda v, p: v) if "statuses" in a else ()):
                sp = f"{path}.statuses[{j}]"
                sv = _json_object(s, sp, "AdditiveStatusFile")
                start = _json_date(sv.get("effectivefrom"), _json_child(sp, "effectiveFrom"))
                end = sv.get("effectiveto")
                if start <= as_of and (end is None or _json_date(end, _json_child(sp, "effectiveTo")) > as_of):
                    statuses.setdefault(_json_str(sv.get("jurisdiction"), _json_child(sp, "jurisdiction")), _json_str(sv.get("status"), _json_child(sp, "status")))
            tiers = [
                _json_str(_json_object(h, f"{path}.hazards[{j}]", "AdditiveHazardFile").get("tier"), f"{path}.hazards[{j}].tier")
                for j, h in enumerate(_json_list(a["hazards"], _json_child(path, "hazards"), lambda v, p: v) if "hazards" in a else ())
            ]
            tier = min(tiers) if tiers else None
            result[code] = AdditiveRegulation(code, _json_str(a["name"], _json_child(path, "name")), classes, statuses, tier[0] if tier else None)
        return result


_MAJOR_JURISDICTIONS = frozenset({"EU", "US", "IN", "GB", "CA", "AU"})


class AdditiveTiers:
    @staticmethod
    def resolve(a: AdditiveRegulation | None, market: str) -> str:
        """Tier "A" (prohibited in the market), "B", "C" or "D" (no concern / unknown)."""
        if a is None:
            return "D"
        if a.status_by_jurisdiction.get(market) == "PROHIBITED":
            return "A"
        b_elsewhere = any(k in _MAJOR_JURISDICTIONS and v in ("PROHIBITED", "PHASE_OUT") for k, v in a.status_by_jurisdiction.items())
        if a.hazard_tier == "B" or b_elsewhere:
            return "B"
        if a.hazard_tier == "C":
            return "C"
        return "D"


# ---------------------------------------------------------------------------------------------------------------------
# HealthSwap Index
# ---------------------------------------------------------------------------------------------------------------------


def _default_nova_points() -> dict[int, Decimal]:
    return {1: Decimal(100), 2: Decimal(85), 3: Decimal(70), 4: Decimal(40)}


def _json_nova_points(value: Any, path: str) -> dict[int, Decimal]:
    """Dictionary<int, decimal>: JSON property names must be Int32 numbers."""
    if not isinstance(value, Mapping):
        raise _json_fail(path, "an object", value)
    result: dict[int, Decimal] = {}
    for key, item in value.items():
        if isinstance(key, int) and not isinstance(key, bool):
            number = key
        elif isinstance(key, str) and _INT_KEY_RX.fullmatch(key):
            number = int(key)
        else:
            raise _json_fail(path, "an Int32 property name", key)
        if not INT32_MIN <= number <= INT32_MAX:
            raise _json_fail(path, "an Int32 property name", key)
        result[number] = _json_decimal(item, _json_child(path, str(key)))
    return result


@dataclass(frozen=True, slots=True, kw_only=True)
class HsiParams:
    weight_nutrient_profile: Decimal = Decimal("0.70")
    weight_processing: Decimal = Decimal("0.15")
    weight_additives: Decimal = Decimal("0.15")
    nova_points: Mapping[int, Decimal] = field(default_factory=_default_nova_points)
    tier_b_penalty: Decimal = Decimal(30)
    tier_c_penalty: Decimal = Decimal(10)
    version: str = "1.0"

    @staticmethod
    def from_dict(data: Mapping[str, Any], path: str = "") -> HsiParams:
        """A pack's "hsi" object (also part of the MARKET_PACK params)."""
        return HsiParams(**_json_props(_json_object(data, path, "HsiParams"), path, _HSI_PARAMS))


_HSI_PARAMS: tuple[tuple[str, str, _Reader], ...] = (
    ("weightNutrientProfile", "weight_nutrient_profile", _json_decimal),
    ("weightProcessing", "weight_processing", _json_decimal),
    ("weightAdditives", "weight_additives", _json_decimal),
    ("novaPoints", "nova_points", _json_nova_points),
    ("tierBPenalty", "tier_b_penalty", _json_decimal),
    ("tierCPenalty", "tier_c_penalty", _json_decimal),
    ("version", "version", _json_str),
)


class Hsi:
    """HealthSwap Index (doc 03 2.5): HSI = 0.70 NP + 0.15 PR + 0.15 AD; weights renormalized when NOVA is unknown."""

    @staticmethod
    def compute(np: Decimal | None, nova: int | None, tier_b: int, tier_c: int, p: HsiParams) -> Decimal | None:
        if np is None:
            return None
        ad = decimal_max(_ZERO, _HUNDRED - p.tier_b_penalty * tier_b - p.tier_c_penalty * tier_c)
        pr = p.nova_points.get(nova) if nova is not None else None
        if pr is not None:
            value = p.weight_nutrient_profile * np + p.weight_processing * pr + p.weight_additives * ad
        else:
            value = decimal_div(p.weight_nutrient_profile * np + p.weight_additives * ad, p.weight_nutrient_profile + p.weight_additives)
        return round_decimal(decimal_clamp(value, _ZERO, _HUNDRED), 2)

    @staticmethod
    def grade(hsi: Decimal | None) -> str | None:
        if hsi is None:
            return None
        if hsi >= 80:
            return "A"
        if hsi >= 65:
            return "B"
        if hsi >= 50:
            return "C"
        if hsi >= 35:
            return "D"
        return "E"
