"""Reference data (C# Knowledge/ReferenceData.cs): the file formats of data/packs and data/seed/kb, the loader that applies them to
reg.*, kb.* and rec.scoring_model (the "pack PR" path of doc 02 section 8: reviewed in source control, loaded idempotently) and
the repository that reads jurisdiction packs and the ingredient taxonomy back from PostgreSQL (the source of truth at runtime,
cached for 5 minutes).

File records are frozen dataclasses read like System.Text.Json with RefJson.Options (HsJson): property names match
case-insensitively, unknown properties are ignored and absent ones keep the C# defaults; null (or a missing positional-record
argument) is rejected for non-nullable strings and collections (ValueError) instead of producing a null reference.
`RefJson.read(Type, path)` parses a file (UTF-8, BOM allowed, numbers as Decimal) and calls `Type.from_dict`.

The MARKET_PACK params document is written like C# JsonSerializer.Serialize(meta, RefJson.Options) (camelCase, nulls
omitted, decimals with their scale), so documents written by either implementation read back identically.
"""

from __future__ import annotations

import logging
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from healthswap.domain.knowledge.parsing import AdditiveEntry, IngredientEntry, IngredientTaxonomy, to_upper_invariant
from healthswap.domain.scoring.health_profiles import ClaimThreshold, MarketPack, RulesParams, UsHealthyGroup
from healthswap.domain.scoring.models import (
    AdditiveRegulation,
    FopThreshold,
    HsiParams,
    PointsModelParams,
    _json_child,
    _json_decimal,
    _json_list,
    _json_map,
    _json_object,
    _json_props,
    _json_str,
    _json_str_or_none,
    _Reader,
)
from healthswap.platform import jsonutil, netjson
from healthswap.platform.db import Db, DbRole, Uow
from healthswap.platform.errors import HsError

log = logging.getLogger(__name__)
T = TypeVar("T")

_ZERO = Decimal(0)

# ---------------------------------------------------------------------------------------------------------------------
# JSON reading helpers (System.Text.Json semantics, see the module docstring)
# ---------------------------------------------------------------------------------------------------------------------


def _req_str(values: Mapping[str, Any], path: str, name: str) -> str:
    return _json_str(values.get(name.lower()), _json_child(path, name))


def _opt_str(values: Mapping[str, Any], path: str, name: str) -> str | None:
    return _json_str_or_none(values.get(name.lower()), _json_child(path, name))


def _req_list(values: Mapping[str, Any], path: str, name: str, read: Callable[[Any, str], T]) -> tuple[T, ...]:
    return _json_list(values.get(name.lower()), _json_child(path, name), read)


def _str_list(value: Any, path: str) -> tuple[str, ...]:
    return _json_list(value, path, _json_str)


def _dec_or_zero(values: Mapping[str, Any], path: str, name: str) -> Decimal:
    key = name.lower()
    return _json_decimal(values[key], _json_child(path, name)) if key in values else _ZERO


# ---------------------------------------------------------------------------------------------------------------------
# Reference-data file formats (data/packs, data/seed/kb)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackDocument:
    citation: str
    authority: str
    title: str
    url: str | None
    effective_from: str | None
    jurisdiction: str | None = None

    @staticmethod
    def from_dict(data: Any, path: str = "") -> PackDocument:
        v = _json_object(data, path, "PackDocument")
        return PackDocument(
            _req_str(v, path, "citation"), _req_str(v, path, "authority"), _req_str(v, path, "title"),
            _opt_str(v, path, "url"), _opt_str(v, path, "effectiveFrom"), _opt_str(v, path, "jurisdiction"),
        )


@dataclass(frozen=True, slots=True)
class UsHealthyFile:
    category_prefix: str
    group: str
    added_sugars_max_g: Decimal
    sodium_max_mg: Decimal
    sat_fat_max_g: Decimal
    required_flag: str | None

    @staticmethod
    def from_dict(data: Any, path: str = "") -> UsHealthyFile:
        v = _json_object(data, path, "UsHealthyFile")
        return UsHealthyFile(
            _req_str(v, path, "categoryPrefix"), _req_str(v, path, "group"), _dec_or_zero(v, path, "addedSugarsMaxG"),
            _dec_or_zero(v, path, "sodiumMaxMg"), _dec_or_zero(v, path, "satFatMaxG"), _opt_str(v, path, "requiredFlag"),
        )

    def to_doc(self) -> dict[str, Any]:
        """The camelCase document C# writes for this record (RequiredFlag omitted when null)."""
        return {
            "categoryPrefix": self.category_prefix, "group": self.group, "addedSugarsMaxG": self.added_sugars_max_g,
            "sodiumMaxMg": self.sodium_max_mg, "satFatMaxG": self.sat_fat_max_g, "requiredFlag": self.required_flag,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class PackFile:
    """A jurisdiction pack (data/packs/XX.json)."""

    market: str
    pack_version: str
    status: str = "PREVIEW"
    verification: str | None = None
    primary_model: str
    methodology_url: str = ""
    documents: tuple[PackDocument, ...] = ()
    allergen_rules: tuple[str, ...] = ()
    hsi: HsiParams = field(default_factory=HsiParams)
    rules: RulesParams = field(default_factory=RulesParams)
    fop: tuple[FopThreshold, ...] = ()
    daily_values: Mapping[str, Decimal] = field(default_factory=dict)
    claims: tuple[ClaimThreshold, ...] = ()
    us_healthy: tuple[UsHealthyFile, ...] = ()
    models: tuple[str, ...] = ()

    @staticmethod
    def from_dict(data: Any, path: str = "") -> PackFile:
        values = _json_object(data, path, "PackFile", ("market", "packVersion", "primaryModel"))
        return PackFile(**_json_props(values, path, _PACK_FILE))


_PACK_FILE: tuple[tuple[str, str, _Reader], ...] = (
    ("market", "market", _json_str),
    ("packVersion", "pack_version", _json_str),
    ("status", "status", _json_str),
    ("verification", "verification", _json_str_or_none),
    ("primaryModel", "primary_model", _json_str),
    ("methodologyUrl", "methodology_url", _json_str),
    ("documents", "documents", lambda v, p: _json_list(v, p, PackDocument.from_dict)),
    ("allergenRules", "allergen_rules", _str_list),
    ("hsi", "hsi", HsiParams.from_dict),
    ("rules", "rules", RulesParams.from_dict),
    ("fop", "fop", lambda v, p: _json_list(v, p, FopThreshold.from_dict)),
    ("dailyValues", "daily_values", lambda v, p: _json_map(v, p, _json_decimal)),
    ("claims", "claims", lambda v, p: _json_list(v, p, ClaimThreshold.from_dict)),
    ("usHealthy", "us_healthy", lambda v, p: _json_list(v, p, UsHealthyFile.from_dict)),
    ("models", "models", _str_list),
)


@dataclass(frozen=True, slots=True)
class AdditiveStatusFile:
    jurisdiction: str
    status: str
    effective_from: str
    effective_to: str | None
    citation: str | None

    @staticmethod
    def from_dict(data: Any, path: str = "") -> AdditiveStatusFile:
        v = _json_object(data, path, "AdditiveStatusFile")
        return AdditiveStatusFile(
            _req_str(v, path, "jurisdiction"), _req_str(v, path, "status"), _req_str(v, path, "effectiveFrom"),
            _opt_str(v, path, "effectiveTo"), _opt_str(v, path, "citation"),
        )


@dataclass(frozen=True, slots=True)
class AdditiveHazardFile:
    authority: str
    finding: str
    tier: str
    as_of: str
    citation: str | None

    @staticmethod
    def from_dict(data: Any, path: str = "") -> AdditiveHazardFile:
        v = _json_object(data, path, "AdditiveHazardFile")
        return AdditiveHazardFile(
            _req_str(v, path, "authority"), _req_str(v, path, "finding"), _req_str(v, path, "tier"), _req_str(v, path, "asOf"),
            _opt_str(v, path, "citation"),
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class AdditiveFile:
    code: str
    name: str
    classes: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    statuses: tuple[AdditiveStatusFile, ...] = ()
    hazards: tuple[AdditiveHazardFile, ...] = ()

    @staticmethod
    def from_dict(data: Any, path: str = "") -> AdditiveFile:
        values = _json_object(data, path, "AdditiveFile", ("code", "name"))
        return AdditiveFile(**_json_props(values, path, _ADDITIVE_FILE))


_ADDITIVE_FILE: tuple[tuple[str, str, _Reader], ...] = (
    ("code", "code", _json_str),
    ("name", "name", _json_str),
    ("classes", "classes", _str_list),
    ("aliases", "aliases", _str_list),
    ("statuses", "statuses", lambda v, p: _json_list(v, p, AdditiveStatusFile.from_dict)),
    ("hazards", "hazards", lambda v, p: _json_list(v, p, AdditiveHazardFile.from_dict)),
)


@dataclass(frozen=True, slots=True)
class AdditivesFile:
    """The additive master (data/packs/additives.json)."""

    version: str
    documents: tuple[PackDocument, ...]
    additives: tuple[AdditiveFile, ...]

    @staticmethod
    def from_dict(data: Any, path: str = "") -> AdditivesFile:
        v = _json_object(data, path, "AdditivesFile")
        return AdditivesFile(
            _req_str(v, path, "version"), _req_list(v, path, "documents", PackDocument.from_dict),
            _req_list(v, path, "additives", AdditiveFile.from_dict),
        )


@dataclass(frozen=True, slots=True)
class CategoryFile:
    code: str
    name: str
    physical_state: str
    scoring_category: str
    gpc_brick: str | None = None

    @staticmethod
    def from_dict(data: Any, path: str = "") -> CategoryFile:
        v = _json_object(data, path, "CategoryFile")
        return CategoryFile(
            _req_str(v, path, "code"), _req_str(v, path, "name"), _req_str(v, path, "physicalState"), _req_str(v, path, "scoringCategory"),
            _opt_str(v, path, "gpcBrick"),
        )


@dataclass(frozen=True, slots=True)
class SubstitutionMapFile:
    """An approved cross-category substitution (`from_` is C# From)."""

    markets: tuple[str, ...]
    from_: str
    to: str
    approved_by: str
    notes: str | None

    @staticmethod
    def from_dict(data: Any, path: str = "") -> SubstitutionMapFile:
        v = _json_object(data, path, "SubstitutionMapFile")
        return SubstitutionMapFile(
            _req_list(v, path, "markets", _json_str), _req_str(v, path, "from"), _req_str(v, path, "to"), _req_str(v, path, "approvedBy"),
            _opt_str(v, path, "notes"),
        )


@dataclass(frozen=True, slots=True)
class CategoriesFile:
    """The category taxonomy and approved substitution maps (data/seed/kb/categories.json)."""

    version: str
    categories: tuple[CategoryFile, ...]
    substitution_maps: tuple[SubstitutionMapFile, ...]

    @staticmethod
    def from_dict(data: Any, path: str = "") -> CategoriesFile:
        v = _json_object(data, path, "CategoriesFile")
        return CategoriesFile(
            _req_str(v, path, "version"), _req_list(v, path, "categories", CategoryFile.from_dict),
            _req_list(v, path, "substitutionMaps", SubstitutionMapFile.from_dict),
        )


@dataclass(frozen=True, slots=True)
class IngredientFile:
    name: str
    flags: tuple[str, ...]
    allergens: tuple[str, ...]
    # Aliases per language (BCP 47 code -> aliases), in file order.
    aliases: Mapping[str, tuple[str, ...]]

    @staticmethod
    def from_dict(data: Any, path: str = "") -> IngredientFile:
        v = _json_object(data, path, "IngredientFile")
        return IngredientFile(
            _req_str(v, path, "name"), _req_list(v, path, "flags", _json_str), _req_list(v, path, "allergens", _json_str),
            _json_map(v.get("aliases"), _json_child(path, "aliases"), _str_list),
        )


@dataclass(frozen=True, slots=True)
class IngredientsFile:
    """The ingredient taxonomy (data/seed/kb/ingredients.json)."""

    version: str
    ingredients: tuple[IngredientFile, ...]

    @staticmethod
    def from_dict(data: Any, path: str = "") -> IngredientsFile:
        v = _json_object(data, path, "IngredientsFile")
        return IngredientsFile(_req_str(v, path, "version"), _req_list(v, path, "ingredients", IngredientFile.from_dict))


class RefJson:
    """C# RefJson: reads reference-data files and writes documents with RefJson.Options (HsJson)."""

    @staticmethod
    def read_text(path: str | Path) -> str:
        """File.ReadAllText: UTF-8 with an optional BOM (invalid bytes replaced), line endings kept."""
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as f:
            return f.read()

    @staticmethod
    def load(path: str | Path) -> Any:
        """The parsed JSON of a file (numbers with a fraction or exponent as Decimal)."""
        return jsonutil.loads(RefJson.read_text(path))

    @staticmethod
    def read(tp: Any, path: str | Path) -> Any:
        """C# RefJson.Read<T>(path): `tp` has a `from_dict(data, path)` reader (the records here, PointsModelParams, ...)."""
        data = RefJson.load(path)
        if data is None:
            raise ValueError(f"Empty file {path}")
        return tp.from_dict(data)

    @staticmethod
    def serialize(value: Any) -> str:
        """JsonSerializer.Serialize(value, RefJson.Options) for a camelCase document (see netjson.serialize_hs)."""
        return netjson.serialize_hs(value)


# ---------------------------------------------------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------------------------------------------------


def _distinct_ignore_case(items: Iterable[str]) -> list[str]:
    """Distinct(StringComparer.OrdinalIgnoreCase): the first spelling wins, order kept."""
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        folded = to_upper_invariant(item)
        if folded not in seen:
            seen.add(folded)
            result.append(item)
    return result


def _is_upper(ch: str) -> bool:
    """char.IsUpper on the first UTF-16 unit (a surrogate is not upper case)."""
    return ord(ch) <= 0xFFFF and unicodedata.category(ch) == "Lu"


def _value(row: Mapping[str, Any]) -> Any:
    return next(iter(row.values()))


def _hsi_doc(h: HsiParams) -> dict[str, Any]:
    return {
        "weightNutrientProfile": h.weight_nutrient_profile, "weightProcessing": h.weight_processing, "weightAdditives": h.weight_additives,
        "novaPoints": {str(k): v for k, v in h.nova_points.items()}, "tierBPenalty": h.tier_b_penalty, "tierCPenalty": h.tier_c_penalty,
        "version": h.version,
    }


def _rules_doc(r: RulesParams) -> dict[str, Any]:
    return {
        "version": r.version, "tauSub": r.tau_sub, "deltaMin": r.delta_min, "limitedNutrientTolerance": r.limited_nutrient_tolerance,
        "dismissCooldownDays": r.dismiss_cooldown_days, "minAlternativeConfidence": r.min_alternative_confidence,
        "verifiedAllergenConfidence": r.verified_allergen_confidence, "targetCeilingPercentile": r.target_ceiling_percentile,
        "maxEdgesPerProduct": r.max_edges_per_product, "retrievalCandidates": r.retrieval_candidates,
    }


_MODEL_JURISDICTIONS = {"IN_INR_DRAFT": "IN", "NUTRISCORE_2023": "EU", "UK_NPM_2004": "GB", "HSR": "AU"}


class ReferenceDataLoader:
    """
    Applies versioned reference data (jurisdiction packs, additive master, taxonomy, categories) to reg.*, kb.* and
    rec.scoring_model. This is the "pack PR" path in doc 02 section 8: files reviewed in source control, loaded idempotently.
    Every apply_* method runs in its own hs_kb_writer transaction.
    """

    def __init__(self, db: Db) -> None:
        self.db = db

    def apply_all(self, repo_root: str | Path) -> None:
        packs_dir = Path(repo_root) / "data" / "packs"
        self.apply_additives(RefJson.read(AdditivesFile, packs_dir / "additives.json"))
        for model_path in sorted((packs_dir / "models").glob("*.json")):
            self.apply_model(RefJson.read(PointsModelParams, model_path), RefJson.read_text(model_path))
        for pack_path in sorted(packs_dir.glob("??.json")):
            self.apply_pack(RefJson.read(PackFile, pack_path))
        kb = Path(repo_root) / "data" / "seed" / "kb"
        self.apply_taxonomy(RefJson.read(IngredientsFile, kb / "ingredients.json"))
        self.apply_categories(RefJson.read(CategoriesFile, kb / "categories.json"))

    @staticmethod
    def _upsert_document(u: Uow, d: PackDocument, jurisdiction: str) -> uuid.UUID:
        return _value(u.single(
            """
            insert into reg.document (jurisdiction, authority, title, citation, url, effective_from)
            values (@jurisdiction, @authority, @title, @citation, @url, @effectiveFrom::date)
            on conflict (citation) do update set title = excluded.title, url = coalesce(excluded.url, reg.document.url)
            returning document_id
            """,
            {
                "jurisdiction": d.jurisdiction if d.jurisdiction is not None else jurisdiction, "authority": d.authority, "title": d.title,
                "citation": d.citation, "url": d.url, "effectiveFrom": d.effective_from,
            },
        ))

    def apply_additives(self, file: AdditivesFile) -> None:
        def work(u: Uow) -> None:
            docs: dict[str, uuid.UUID] = {}
            for d in file.documents:
                docs[d.citation] = self._upsert_document(u, d, d.jurisdiction if d.jurisdiction is not None else "EU")
            for a in file.additives:
                us_names = [x for x in a.aliases if x.startswith("FD&C") or _is_upper(x[0])]
                additive_id = _value(u.single(
                    """
                    insert into kb.additive (ins_number, e_number, canonical_name, us_names, functional_classes)
                    values (@code, @eNumber, @name, @usNames::text[], @classes::text[])
                    on conflict (ins_number) do update set canonical_name = excluded.canonical_name, us_names = excluded.us_names,
                        functional_classes = excluded.functional_classes
                    returning additive_id
                    """,
                    {"code": a.code, "eNumber": None if a.code == "924a" else "E" + a.code, "name": a.name, "usNames": us_names, "classes": list(a.classes)},
                ))
                for alias in _distinct_ignore_case([*a.aliases, a.name]):
                    u.execute(
                        "insert into kb.additive_alias (alias, lang, additive_id) values (@alias, 'en', @id) on conflict (alias, lang) do update set additive_id = excluded.additive_id",
                        {"alias": IngredientTaxonomy.normalize(alias), "id": additive_id},
                    )
                for s in a.statuses:
                    u.execute(
                        """
                        insert into reg.additive_status (additive_id, jurisdiction, status, effective_from, effective_to, document_id)
                        values (@id, @j, @status, @from::date, @to::date, @doc)
                        on conflict (additive_id, jurisdiction, effective_from) do update set status = excluded.status, effective_to = excluded.effective_to
                        """,
                        {
                            "id": additive_id, "j": s.jurisdiction, "status": s.status, "from": s.effective_from, "to": s.effective_to,
                            "doc": None if s.citation is None else docs.get(s.citation),
                        },
                    )
                for h in a.hazards:
                    u.execute(
                        """
                        insert into reg.additive_hazard (additive_id, authority, finding, concern_tier, as_of, document_id)
                        values (@id, @authority, @finding, @tier, @asOf::date, @doc)
                        on conflict (additive_id, authority, as_of) do update set finding = excluded.finding, concern_tier = excluded.concern_tier
                        """,
                        {
                            "id": additive_id, "authority": h.authority, "finding": h.finding, "tier": h.tier, "asOf": h.as_of,
                            "doc": None if h.citation is None else docs.get(h.citation),
                        },
                    )

        self.db.in_tx(DbRole.KB_WRITER, None, work)

    def apply_model(self, model: PointsModelParams, raw_json: str) -> None:
        """Activates a points model (the params document is the model file as written, `raw_json`)."""

        def work(u: Uow) -> None:
            u.execute(
                "update rec.scoring_model set status = 'DEPRECATED' where code = @code and version <> @version and status <> 'DEPRECATED'",
                {"code": model.code, "version": model.version},
            )
            u.execute(
                """
                insert into rec.scoring_model (code, version, jurisdiction, params, status, published_at, methodology_url)
                values (@code, @version, @jurisdiction, @params::jsonb, 'ACTIVE', now(), @url)
                on conflict (code, version) do update set params = excluded.params, status = 'ACTIVE'
                """,
                {
                    "code": model.code, "version": model.version, "jurisdiction": _MODEL_JURISDICTIONS.get(model.code), "params": raw_json,
                    "url": f"https://healthswap.example/methodology/models/{model.code}/{model.version}",
                },
            )

        self.db.in_tx(DbRole.KB_WRITER, None, work)

    def apply_pack(self, pack: PackFile) -> None:
        def work(u: Uow) -> None:
            m = pack.market
            for d in pack.documents:
                self._upsert_document(u, d, m)
            for a in pack.allergen_rules:
                u.execute(
                    "insert into reg.allergen_rule (jurisdiction, allergen_code, mandatory_declaration) values (@m, @a, true) on conflict do nothing",
                    {"m": m, "a": a},
                )
            for t in pack.fop:
                u.execute(
                    """
                    insert into reg.fop_threshold (jurisdiction, scheme, nutrient_code, physical_state, basis, low_max, high_min, high_inclusive)
                    values (@m, @scheme, @n, @state, @basis, @low, @high, @incl)
                    on conflict (jurisdiction, scheme, nutrient_code, physical_state) do update set basis = excluded.basis, low_max = excluded.low_max,
                        high_min = excluded.high_min, high_inclusive = excluded.high_inclusive
                    """,
                    {
                        "m": m, "scheme": "US_FDA_FOP_PROPOSED" if m == "US" else f"{m}_FOP_ILLUSTRATIVE", "n": t.nutrient, "state": t.physical_state,
                        "basis": t.basis, "low": t.low_max, "high": t.high_min, "incl": t.high_inclusive,
                    },
                )
            for n, amount in pack.daily_values.items():
                u.execute(
                    "insert into reg.daily_value (jurisdiction, population, nutrient_code, amount) values (@m, 'ADULT', @n, @amount) on conflict (jurisdiction, population, nutrient_code) do update set amount = excluded.amount",
                    {"m": m, "n": n, "amount": amount},
                )
            for c in pack.claims:
                u.execute(
                    """
                    insert into reg.claim_threshold (jurisdiction, claim_code, nutrient_code, physical_state, basis, max_value, min_value, min_relative_difference)
                    values (@m, @claim, @n, @state, @basis, @max, @min, @rel)
                    on conflict (jurisdiction, claim_code, nutrient_code, physical_state) do update set basis = excluded.basis, max_value = excluded.max_value,
                        min_value = excluded.min_value, min_relative_difference = excluded.min_relative_difference
                    """,
                    {
                        "m": m, "claim": c.claim_code, "n": c.nutrient, "state": c.physical_state, "basis": c.basis, "max": c.max_value,
                        "min": c.min_value, "rel": c.min_relative_difference,
                    },
                )

            meta = {
                "primaryModel": pack.primary_model, "models": list(pack.models), "hsi": _hsi_doc(pack.hsi), "rules": _rules_doc(pack.rules),
                "usHealthy": [h.to_doc() for h in pack.us_healthy], "methodologyUrl": pack.methodology_url, "packVersion": pack.pack_version,
                "verification": pack.verification,
            }
            u.execute(
                "update rec.scoring_model set status = 'DEPRECATED' where code = 'MARKET_PACK' and jurisdiction = @m and version <> @v",
                {"m": m, "v": pack.pack_version},
            )
            u.execute(
                """
                insert into rec.scoring_model (code, version, jurisdiction, params, status, published_at, methodology_url)
                values ('MARKET_PACK', @v, @m, @params::jsonb, 'ACTIVE', now(), @url)
                on conflict (code, version) do update set params = excluded.params, status = 'ACTIVE'
                """,
                {"m": m, "v": pack.pack_version, "params": RefJson.serialize(meta), "url": pack.methodology_url},
            )
            for code, version in (("HSI", pack.hsi.version), ("NOVA", "1.0"), ("ADDITIVE_TIERS", "1.0")):
                u.execute(
                    """
                    insert into rec.scoring_model (code, version, jurisdiction, params, status, published_at)
                    values (@code, @version, null, '{}'::jsonb, 'ACTIVE', now()) on conflict (code, version) do update set status = 'ACTIVE'
                    """,
                    {"code": code, "version": version},
                )
            u.execute("update rec.scoring_model set status = 'DEPRECATED' where version = '0.1' and status = 'PREVIEW'")

        self.db.in_tx(DbRole.KB_WRITER, None, work)

    def apply_taxonomy(self, file: IngredientsFile) -> None:
        def work(u: Uow) -> None:
            for i in file.ingredients:
                ingredient_id = _value(u.single(
                    """
                    insert into kb.ingredient (canonical_name, flags, allergens) values (@name, @flags::text[], @allergens::text[])
                    on conflict (canonical_name) do update set flags = excluded.flags, allergens = excluded.allergens
                    returning ingredient_id
                    """,
                    {"name": i.name, "flags": list(i.flags), "allergens": list(i.allergens)},
                ))
                for lang, aliases in i.aliases.items():
                    for alias in _distinct_ignore_case([*aliases, i.name]):
                        u.execute(
                            "insert into kb.ingredient_alias (alias, lang, ingredient_id) values (@alias, @lang, @id) on conflict (alias, lang) do update set ingredient_id = excluded.ingredient_id",
                            {"alias": IngredientTaxonomy.normalize(alias), "lang": lang, "id": ingredient_id},
                        )

        self.db.in_tx(DbRole.KB_WRITER, None, work)

    @staticmethod
    def category_seed_order(categories: Iterable[CategoryFile]) -> list[CategoryFile]:
        """Parents first, otherwise file order (stable): the insert order of apply_categories, hence the row order of kb.category in
        a freshly seeded database (the T3 category enum; ml/finetune/build_datasets.py rebuilds it from categories.json)."""
        return sorted(categories, key=lambda c: c.code.count("."))

    def apply_categories(self, file: CategoriesFile) -> None:
        def work(u: Uow) -> None:
            for c in ReferenceDataLoader.category_seed_order(file.categories):
                parent = c.code[: c.code.rfind(".")] if "." in c.code else None
                u.execute(
                    """
                    insert into kb.category (parent_id, code, name, gpc_brick_code, physical_state, scoring_category)
                    values ((select category_id from kb.category where code = @parent), @code, @name, @gpc, @state, @scoring)
                    on conflict (code) do update set name = excluded.name, parent_id = excluded.parent_id, physical_state = excluded.physical_state,
                        scoring_category = excluded.scoring_category, gpc_brick_code = excluded.gpc_brick_code
                    """,
                    {"parent": parent, "code": c.code, "name": c.name, "gpc": c.gpc_brick, "state": c.physical_state, "scoring": c.scoring_category},
                )
            for mapping in file.substitution_maps:
                for market in mapping.markets:
                    u.execute(
                        """
                        insert into rec.category_substitution (market, from_category_id, to_category_id, approved_by, notes)
                        select @market, f.category_id, t.category_id, @by, @notes from kb.category f, kb.category t where f.code = @from and t.code = @to
                        on conflict do nothing
                        """,
                        {"market": market, "from": mapping.from_, "to": mapping.to, "by": mapping.approved_by, "notes": mapping.notes},
                    )

        self.db.in_tx(DbRole.KB_WRITER, None, work)


# ---------------------------------------------------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PackMeta:
    """The MARKET_PACK params document (C# ReferenceDataRepository.PackMeta; positional, so every member is required)."""

    primary_model: str
    models: tuple[str, ...]
    hsi: HsiParams
    rules: RulesParams
    us_healthy: tuple[UsHealthyFile, ...]
    methodology_url: str
    pack_version: str

    @staticmethod
    def from_dict(data: Any, path: str = "") -> PackMeta:
        v = _json_object(data, path, "PackMeta")
        return PackMeta(
            _req_str(v, path, "primaryModel"), _req_list(v, path, "models", _json_str),
            HsiParams.from_dict(v.get("hsi"), _json_child(path, "hsi")), RulesParams.from_dict(v.get("rules"), _json_child(path, "rules")),
            _req_list(v, path, "usHealthy", UsHealthyFile.from_dict), _req_str(v, path, "methodologyUrl"), _req_str(v, path, "packVersion"),
        )


def _hazard_tier(value: str | None) -> str | None:
    return value[0] if value else None


class ReferenceDataRepository:
    """Loads jurisdiction packs and the taxonomy from the database (the source of truth at runtime). Thread-safe."""

    PackMeta = PackMeta
    TTL_SECONDS = 5 * 60

    def __init__(self, db: Db) -> None:
        self.db = db
        self._packs: dict[str, tuple[MarketPack, float]] = {}
        self._taxonomy: tuple[IngredientTaxonomy, float] | None = None
        self._lock = threading.Lock()

    def markets(self) -> list[str]:
        """Markets with an active jurisdiction pack, sorted."""
        rows = self.db.in_tx(
            DbRole.READ_ONLY, None,
            lambda u: u.query("select jurisdiction from rec.scoring_model where code = 'MARKET_PACK' and status = 'ACTIVE' order by 1"),
        )
        return [r["jurisdiction"] for r in rows]

    def get_pack(self, market: str) -> MarketPack:
        """The active pack for `market` (cached 5 minutes); HsError 422 HS-UNKNOWN-MARKET when there is none."""
        cached = self._packs.get(market)
        if cached is not None and time.monotonic() - cached[1] < self.TTL_SECONDS:
            return cached[0]
        pack = self.db.in_tx(DbRole.READ_ONLY, None, lambda u: self._load_pack(u, market))
        with self._lock:
            self._packs[market] = (pack, time.monotonic())
        return pack

    @staticmethod
    def _load_pack(u: Uow, market: str) -> MarketPack:
        meta_row = u.one(
            "select code, version, params::text as params, jurisdiction from rec.scoring_model where code = 'MARKET_PACK' and jurisdiction = @market and status = 'ACTIVE' order by published_at desc limit 1",
            {"market": market},
        )
        if meta_row is None:
            raise HsError.unprocessable("HS-UNKNOWN-MARKET", f"No active jurisdiction pack for market {market}")
        meta = PackMeta.from_dict(jsonutil.loads(meta_row["params"]))
        models: dict[str, PointsModelParams] = {}
        for r in u.query(
            "select code, version, params::text as params, jurisdiction from rec.scoring_model where code || '@' || version = any(@keys::text[])",
            {"keys": list(meta.models)},
        ):
            models[r["code"]] = PointsModelParams.from_dict(jsonutil.loads(r["params"]))
        fop = tuple(
            FopThreshold(r["nutrient_code"], r["physical_state"], r["basis"], r["low_max"], r["high_min"], r["high_inclusive"])
            for r in u.query(
                "select nutrient_code, physical_state, basis, low_max, high_min, high_inclusive from reg.fop_threshold where jurisdiction = @market",
                {"market": market},
            )
        )
        dvs = {
            r["nutrient_code"]: r["amount"]
            for r in u.query("select nutrient_code, amount from reg.daily_value where jurisdiction = @market and population = 'ADULT'", {"market": market})
        }
        claims = tuple(
            ClaimThreshold(r["claim_code"], r["nutrient_code"], r["physical_state"], r["basis"], r["max_value"], r["min_value"], r["min_relative_difference"])
            for r in u.query(
                "select claim_code, nutrient_code, physical_state, basis, max_value, min_value, min_relative_difference from reg.claim_threshold where jurisdiction = @market",
                {"market": market},
            )
        )
        additive_rows = u.query(
            """
            select a.ins_number, a.canonical_name, a.functional_classes, s.jurisdiction, s.status, h.concern_tier
            from kb.additive a
            left join reg.additive_status s on s.additive_id = a.additive_id and s.effective_from <= current_date
                 and (s.effective_to is null or s.effective_to > current_date)
            left join lateral (select min(concern_tier) as concern_tier from reg.additive_hazard h where h.additive_id = a.additive_id) h on true
            where a.ins_number is not null
            """
        )
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in additive_rows:
            groups.setdefault(r["ins_number"], []).append(r)
        additives: dict[str, AdditiveRegulation] = {}
        for code, rows in groups.items():
            first = rows[0]
            statuses: dict[str, str] = {}
            for r in rows:
                if r["jurisdiction"] is not None:
                    statuses.setdefault(r["jurisdiction"], r["status"])
            additives[code] = AdditiveRegulation(
                first["ins_number"], first["canonical_name"], tuple(first["functional_classes"]), statuses, _hazard_tier(first["concern_tier"])
            )
        us_healthy: dict[str, UsHealthyGroup] = {}
        for h in meta.us_healthy:
            if h.category_prefix in us_healthy:
                raise ValueError(f"An item with the same key has already been added. Key: {h.category_prefix}")
            us_healthy[h.category_prefix] = UsHealthyGroup(h.group, h.added_sugars_max_g, h.sodium_max_mg, h.sat_fat_max_g, h.required_flag)
        return MarketPack(
            market=market, primary_model=meta.primary_model, points_models=models, hsi=meta.hsi, fop=fop, daily_values=dvs, claims=claims,
            additives=additives, rules=meta.rules, methodology_url=meta.methodology_url, pack_version=meta.pack_version,
            us_healthy_by_category_prefix=us_healthy,
        )

    def get_taxonomy(self) -> IngredientTaxonomy:
        """The ingredient taxonomy and additive master (cached 5 minutes)."""
        cached = self._taxonomy
        if cached is not None and time.monotonic() - cached[1] < self.TTL_SECONDS:
            return cached[0]
        tax = self.db.in_tx(DbRole.READ_ONLY, None, self._load_taxonomy)
        self._taxonomy = (tax, time.monotonic())
        return tax

    @staticmethod
    def _load_taxonomy(u: Uow) -> IngredientTaxonomy:
        ingredients = {r["ingredient_id"]: r for r in u.query("select ingredient_id, canonical_name, flags, allergens from kb.ingredient")}
        aliases = u.query("select alias, ingredient_id from kb.ingredient_alias")
        entries: list[tuple[str, IngredientEntry]] = []
        for i in ingredients.values():
            entries.append((i["canonical_name"], IngredientEntry(i["canonical_name"], tuple(i["flags"]), tuple(i["allergens"]))))
        for a in aliases:
            i = ingredients.get(a["ingredient_id"])
            if i is not None:
                entries.append((a["alias"], IngredientEntry(i["canonical_name"], tuple(i["flags"]), tuple(i["allergens"]))))
        additive_aliases: dict[str, list[str]] = {}
        for r in u.query("select a.ins_number, al.alias from kb.additive_alias al join kb.additive a using (additive_id) where a.ins_number is not null"):
            additive_aliases.setdefault(r["ins_number"], []).append(r["alias"])
        additives = [
            AdditiveEntry(r["ins_number"], r["canonical_name"], tuple(r["functional_classes"]), tuple(additive_aliases.get(r["ins_number"], ())))
            for r in u.query("select ins_number, canonical_name, functional_classes from kb.additive where ins_number is not null")
        ]
        return IngredientTaxonomy(entries, additives)

    def invalidate(self) -> None:
        with self._lock:
            self._packs.clear()
            self._taxonomy = None
