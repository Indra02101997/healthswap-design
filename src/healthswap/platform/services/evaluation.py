"""Services/EvaluationService.cs ported: the cart-evaluation hot path (EvaluationService: evaluate, get, alternatives) and the
asynchronous evaluation recorder (EvaluationRecorder, a BackgroundWorker that writes txn.cart_evaluation, txn.suggestion and the
hs.cart-evaluations.v1 ranker training rows).

Requests and results are camelCase dicts shaped like api/openapi.yaml: CartEvaluationRequest -> CartEvaluation and the product
alternatives query -> AlternativesResponse (ids are strings, timestamps ISO 8601 strings, numbers Decimal, None members omitted,
C# property order). Requests are read with `CartEvaluationRequest.from_api` (the API validates them against the contract first;
anything malformed that gets here is a 400 HS-REQ-INVALID).

Port notes:
    * Pipeline order, the soft internal budget (always serves at least one line), degradation reasons (KILL_SWITCH, NO_DATA,
      TIMEOUT_BUDGET, INVENTORY_STALE), the holdout hash, candidate filtering, ranking order, exploration, cart selection,
      explanations, the RA-1 label and every message are those of the C#. Only the hot cache is read for products and swaps;
      the partner directory, jurisdiction pack and shopper preferences come from their in-process caches and fall back to
      PostgreSQL on a miss, exactly like the C# services they call.
    * Decimal arithmetic that reaches the response or the ranking uses the C# decimal helpers (decimal_div / decimal_mul and
      round_decimal = Math.Round), so prices, ratios and the cart HSI carry the C# digits and scale.
    * The cached evaluation behind GET /cart-evaluations/{id} is the System.Text.Json text of the result
      (netjson.serialize_hs), so a re-read returns the same values with their scale.
    * Suggestion snapshots, txn.suggestion.alternatives and reco_fee use the Python hot-cache JSON (HotCache.to_json:
      snake_case keys); SuggestionStore (events.py) reads them back.
"""

from __future__ import annotations

import hashlib
import logging
import queue
import threading
import time
import uuid
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from healthswap.domain.commerce.fees import RecoAddFeeRule, RecoFeeSnapshot
from healthswap.domain.commerce.partner_config import PartnerConfig
from healthswap.domain.knowledge.parsing import equals_ignore_case, to_upper_invariant
from healthswap.domain.knowledge.products import decimal_div, decimal_max, decimal_mul, round_decimal
from healthswap.domain.primitives import Clock, as_utc, gtin_try_normalize, iso, new_id, to_decimal, try_parse_amount
from healthswap.domain.recommendation.explanations import ExplanationTemplates, FactSheets, RenderedExplanation
from healthswap.domain.recommendation.labels import ShopperCopy
from healthswap.domain.recommendation.ranking import CartCandidate, CartSelection, RankerFeatures, Ranking, RankWeights
from healthswap.domain.recommendation.rules import CommercialGuards, PriceContext, RequestRules, ShopperConstraints, StaticRules
from healthswap.domain.scoring.health_profiles import HealthProfileData, MarketPack
from healthswap.domain.text import _utf8
from healthswap.platform import jsonutil, netjson
from healthswap.platform.api_mapper import ApiMapper
from healthswap.platform.bus import Outbox, Topics
from healthswap.platform.db import Db, DbRole, Uow
from healthswap.platform.errors import HsError
from healthswap.platform.hot_cache import AlternativeSnapshot, HotCache, InventoryItem, ShopperPrefsDoc, SuggestionSnapshot, SwapEdge
from healthswap.platform.knowledge.reference_data import ReferenceDataRepository
from healthswap.platform.ml.registry import ModelRegistry
from healthswap.platform.services.catalog import PreferenceService
from healthswap.platform.settings import EvaluationOptions
from healthswap.platform.tenancy import PartnerContext, PartnerDirectory, PartnerInfo
from healthswap.platform.workers import BackgroundWorker

__all__ = [
    "CartEvaluationRequest", "CartLine", "EvaluationOptions", "EvaluationRecord", "EvaluationRecorder", "EvaluationService", "ModelVersions",
]

log = logging.getLogger(__name__)

_ZERO = Decimal(0)
_ONE = Decimal(1)
_QUARTER = Decimal("0.25")
_WEIGHTS = RankWeights()
_TIME_ZONES = {"IN": "Asia/Kolkata", "US": "America/New_York"}


def _ordinal(s: str) -> bytes:
    """Sort key of StringComparer.Ordinal (UTF-16 code units)."""
    return s.encode("utf-16-be", "surrogatepass")


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    """Drops None members (JsonIgnoreCondition.WhenWritingNull)."""
    return {k: v for k, v in d.items() if v is not None}


def _coalesce(*values: Any) -> Any:
    """C# `a ?? b ?? c`: the first value that is not None."""
    return next((v for v in values if v is not None), None)


def _clamp(value: int, minimum: int, maximum: int) -> int:
    """C# Math.Clamp(int, int, int)."""
    if minimum > maximum:
        raise ValueError(f"'{minimum}' cannot be greater than {maximum}.")
    return minimum if value < minimum else maximum if value > maximum else value


def _starts_with_ignore_case(s: str, prefix: str) -> bool:
    """C# s.StartsWith(prefix, StringComparison.OrdinalIgnoreCase)."""
    return to_upper_invariant(s).startswith(to_upper_invariant(prefix))


def _elapsed_ms(start: float) -> int:
    """Stopwatch.ElapsedMilliseconds (truncated) since `start` (time.perf_counter)."""
    return int((time.perf_counter() - start) * 1000)


def _try_parse_currency(text: str | None) -> Decimal | None:
    """C# Currency.TryParse: NumberStyles.AllowLeadingSign | AllowDecimalPoint (no surrounding white space)."""
    if text is None or text != text.strip():
        return None
    return try_parse_amount(text)


# ---------------------------------------------------------------------------------------------------------------------
# Requests (HealthSwap.Contracts records, read from the API dicts)
# ---------------------------------------------------------------------------------------------------------------------


def _field(m: Mapping[str, Any], key: str, what: str) -> Any:
    if not isinstance(m, Mapping):
        raise HsError.invalid(f"{what} must be an object")
    return m.get(key)


def _str(m: Mapping[str, Any], key: str, what: str, required: bool = False) -> str | None:
    value = _field(m, key, what)
    if value is None:
        if required:
            raise HsError.invalid(f"{what}.{key} is required")
        return None
    if not isinstance(value, str):
        raise HsError.invalid(f"{what}.{key} must be a string")
    return value


def _number(m: Mapping[str, Any], key: str, what: str) -> Decimal | None:
    value = _field(m, key, what)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise HsError.invalid(f"{what}.{key} must be a number")
    return to_decimal(value)


def _bool(m: Mapping[str, Any], key: str, what: str) -> bool | None:
    value = _field(m, key, what)
    if value is not None and not isinstance(value, bool):
        raise HsError.invalid(f"{what}.{key} must be a boolean")
    return value


def _int(m: Mapping[str, Any], key: str, what: str) -> int | None:
    value = _field(m, key, what)
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise HsError.invalid(f"{what}.{key} must be an integer")
    return value


@dataclass(frozen=True, slots=True)
class MoneyInput:
    """Contracts.Money as sent by the partner: a decimal string and an ISO 4217 code."""

    amount: str
    currency: str

    @staticmethod
    def from_api(m: Any, what: str) -> MoneyInput:
        return MoneyInput(_str(m, "amount", what, True) or "", _str(m, "currency", what, True) or "")


@dataclass(frozen=True, slots=True)
class CartLine:
    """Contracts.CartLine."""

    line_id: str
    partner_sku: str
    quantity: Decimal
    unit_price: MoneyInput
    gtin: str | None = None
    title: str | None = None
    line_discount: MoneyInput | None = None
    tax_rate: Decimal | None = None
    is_substitutable: bool | None = None

    @staticmethod
    def from_api(m: Any, what: str = "line") -> CartLine:
        unit_price = _field(m, "unitPrice", what)
        if unit_price is None:
            raise HsError.invalid(f"{what}.unitPrice is required")
        discount = m.get("lineDiscount")
        quantity = _number(m, "quantity", what)
        return CartLine(
            line_id=_str(m, "lineId", what, True) or "", partner_sku=_str(m, "partnerSku", what, True) or "",
            quantity=quantity if quantity is not None else _ZERO, unit_price=MoneyInput.from_api(unit_price, f"{what}.unitPrice"),
            gtin=_str(m, "gtin", what), title=_str(m, "title", what),
            line_discount=None if discount is None else MoneyInput.from_api(discount, f"{what}.lineDiscount"),
            tax_rate=_number(m, "taxRate", what), is_substitutable=_bool(m, "isSubstitutable", what),
        )


@dataclass(frozen=True, slots=True)
class CartEvaluationRequest:
    """Contracts.CartEvaluationRequest (CartContext flattened; `preferences` is the inline UserPreferences dict)."""

    cart_ref: str
    market: str
    currency: str
    lines: tuple[CartLine, ...]
    user_ref: str | None = None
    store_id: str | None = None
    locale: str | None = None
    availability_mode: str | None = None
    placement: str | None = None
    experiment_arm: str | None = None
    is_minor: bool | None = None
    max_suggestions: int | None = None
    preferences: Mapping[str, Any] | None = None

    @staticmethod
    def from_api(req: Any) -> CartEvaluationRequest:
        what = "request"
        lines = _field(req, "lines", what)
        if not isinstance(lines, Sequence) or isinstance(lines, (str, bytes)):
            raise HsError.invalid("request.lines is required")
        context = req.get("context") or {}
        preferences = req.get("preferences")
        if preferences is not None and not isinstance(preferences, Mapping):
            raise HsError.invalid("request.preferences must be an object")
        return CartEvaluationRequest(
            cart_ref=_str(req, "cartRef", what, True) or "", market=_str(req, "market", what, True) or "",
            currency=_str(req, "currency", what, True) or "", lines=tuple(CartLine.from_api(x, f"lines[{i}]") for i, x in enumerate(lines)),
            user_ref=_str(req, "userRef", what), store_id=_str(req, "storeId", what), locale=_str(req, "locale", what),
            availability_mode=_str(req, "availabilityMode", what), placement=_str(context, "placement", "context"),
            experiment_arm=_str(context, "experimentArm", "context"), is_minor=_bool(context, "isMinor", "context"),
            max_suggestions=_int(context, "maxSuggestions", "context"), preferences=preferences,
        )


# ---------------------------------------------------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelVersions:
    """Contracts.ModelVersions: the versions an evaluation was computed with."""

    scoring: str
    rules: str
    swap_graph: str
    ranker: str | None = None

    def to_api(self) -> dict[str, Any]:
        return _compact({"scoring": self.scoring, "rules": self.rules, "swapGraph": self.swap_graph, "ranker": self.ranker})


@dataclass(frozen=True, slots=True)
class EvaluationRecord:
    """Everything the recorder persists for one evaluation or one product-page recommendation."""

    partner_id: uuid.UUID
    evaluation_id: uuid.UUID | None
    cart_ref: str
    user_ref: str | None
    store_id: str | None
    market: str
    placement: str
    line_count: int
    degraded: bool
    latency_ms: int
    experiment_arm: str | None
    versions: ModelVersions
    suggestions: tuple[SuggestionSnapshot, ...]
    # Ranker training rows (C# anonymous objects): camelCase dicts serialized into the hs.cart-evaluations.v1 payload.
    training_rows: tuple[Any, ...]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class _Scored:
    alt: HealthProfileData
    sku: str
    inventory: InventoryItem | None
    health_delta: Decimal
    substitutability: Decimal
    price_ratio: Decimal | None
    score: Decimal
    requires_check: bool
    reasons: tuple[str, ...]
    features: list[float] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Shopper:
    constraints: ShopperConstraints
    goals: tuple[str, ...]
    personalize: bool
    locale: str | None


@dataclass(frozen=True, slots=True)
class _LineWork:
    line: CartLine
    original: HealthProfileData
    unit_price: Decimal | None
    alternatives: list[_Scored]


# ---------------------------------------------------------------------------------------------------------------------
# EvaluationService
# ---------------------------------------------------------------------------------------------------------------------


class EvaluationService:
    """
    Cart evaluation orchestrator (HLD 7.1 and doc 03 section 7): resolve lines, load profiles and preferences, select target
    lines, read precomputed swap edges, apply request-time rules (R5-R8), rank, select per cart, attach explanations and the
    RA-1 recommendation label, mint suggestions. Only the hot cache is read on this path (no PostgreSQL, no LLM).
    """

    def __init__(self, cache: HotCache, partners: PartnerDirectory, refs: ReferenceDataRepository, prefs: PreferenceService, models: ModelRegistry,
                 recorder: EvaluationRecorder, options: EvaluationOptions, clock: Clock) -> None:
        self.cache = cache
        self.partners = partners
        self.refs = refs
        self.prefs = prefs
        self.models = models
        self.recorder = recorder
        self.options = options
        self.clock = clock

    # --- POST /cart-evaluations ---------------------------------------------------------------------------------------

    def evaluate(self, ctx: PartnerContext, req: Mapping[str, Any] | CartEvaluationRequest) -> dict[str, Any]:
        """CartEvaluationRequest dict -> CartEvaluation dict."""
        start = time.perf_counter()
        r = req if isinstance(req, CartEvaluationRequest) else CartEvaluationRequest.from_api(req)
        partner = self.partners.get(ctx.partner_id)
        cfg = partner.config
        self.authorize(ctx, partner, r.market, r.cart_ref, r.user_ref, r.store_id)
        if r.currency != cfg.currency:
            raise HsError.unprocessable("HS-CURRENCY-MISMATCH", f"Cart currency must be {cfg.currency} for this partner")
        if len(r.lines) > cfg.max_lines:
            raise HsError.too_large(f"Carts are limited to {cfg.max_lines} lines")
        for line in r.lines:
            if line.unit_price.currency != r.currency:
                raise HsError.invalid(f"Line {line.line_id}: unitPrice currency must be {r.currency}")

        pack = self.refs.get_pack(r.market)
        now = self.clock.utc_now()
        evaluation_id = new_id()
        placement = r.placement if r.placement is not None else "CART"
        locale = ApiMapper.resolve_locale(r.market, r.locale)
        degradations: set[str] = set()
        reco_fee = RecoAddFeeRule.snapshot(cfg.fee.recommendation_add, r.currency)
        label = ApiMapper.label(ShopperCopy.label(locale, partner.display_name, reco_fee is not None))
        graph = self.cache.get_active_graph(r.market)
        versions = self._versions(pack, graph if graph is not None else "none")
        ttl = timedelta(minutes=self.options.suggestion_ttl_minutes)

        # 1. Resolve lines to canonical products: partner catalog mapping first, then the GTIN fallback.
        maps = self.cache.get_sku_maps(ctx.partner_id, [line.partner_sku for line in r.lines])
        product_by_line: dict[str, uuid.UUID] = {}
        for line, m in zip(r.lines, maps, strict=True):
            if m is not None:
                product_by_line[line.line_id] = m.product_id
            elif line.gtin is not None:
                g14 = gtin_try_normalize(line.gtin)
                pid = None if g14 is None else self.cache.get_product_by_gtin(g14)
                if pid is not None:
                    product_by_line[line.line_id] = pid
        profiles = self.cache.get_profiles(r.market, list(product_by_line.values()))
        # Profiles of the products the response shows (originals and alternatives), for the data-source attributions.
        shown_profiles: dict[uuid.UUID, HealthProfileData] = dict(profiles)
        scored_lines = [
            line for line in r.lines
            if line.line_id in product_by_line and product_by_line[line.line_id] in profiles and profiles[product_by_line[line.line_id]].hsi is not None
        ]
        reco_labels = self.cache.get_cart_reco_labels(ctx.partner_id, r.cart_ref)
        line_labels = [
            {"lineId": line.line_id, "partnerSku": line.partner_sku, "suggestionId": str(reco_labels[line.partner_sku]), "label": label}
            for line in r.lines if line.partner_sku in reco_labels
        ]

        summary = self.summary(len(r.lines), product_by_line, profiles, scored_lines, 0)
        arm = r.experiment_arm if r.experiment_arm is not None else (
            "holdout" if self.in_holdout(cfg, r.user_ref if r.user_ref is not None else r.cart_ref) else cfg.experiment.name
        )

        def respond(suggestions: list[dict[str, Any]], shown: Sequence[uuid.UUID], snapshots: Sequence[SuggestionSnapshot],
                    training: Sequence[Any]) -> dict[str, Any]:
            reasons = self.degradation_reasons(degradations)
            result = _compact({
                "evaluationId": str(evaluation_id), "cartRef": r.cart_ref, "market": r.market, "storeId": r.store_id, "createdAt": iso(now),
                "expiresAt": iso(now + ttl), "degraded": reasons is not None, "degradationReasons": reasons,
                "summary": {**summary, "linesWithSuggestions": len(suggestions)}, "suggestions": suggestions, "lineLabels": line_labels,
                "disclosures": self.disclosures(pack, locale, partner, reco_fee is not None, list(dict.fromkeys(shown)), shown_profiles),
                "modelVersions": versions.to_api(),
            })
            self.recorder.enqueue(EvaluationRecord(
                ctx.partner_id, evaluation_id, r.cart_ref, r.user_ref, r.store_id, r.market, placement, len(r.lines), result["degraded"],
                _elapsed_ms(start), arm, versions, tuple(snapshots), tuple(training), now,
            ))
            self.cache.set_evaluation_json(ctx.partner_id, evaluation_id, netjson.serialize_hs(result), ttl)
            return result

        # 2. Kill switch, placement policy, holdout arm.
        if self.placement_disabled(cfg, placement):
            degradations.add("KILL_SWITCH")
            return respond([], [], [], [])
        if arm == "holdout":
            return respond([], [], [], [])
        if graph is None:
            degradations.add("NO_DATA")
            return respond([], [], [], [])

        # 3. Shopper constraints with consent handling and the minors policy.
        shopper = self._shopper(ctx.partner_id, cfg, r.user_ref, r.preferences, r.is_minor)

        # 4. Target lines: substitutable, scored, below the category ceiling (p60), not already a HealthSwap recommendation.
        targets: list[tuple[CartLine, HealthProfileData]] = []
        category_ids = [cid for line in scored_lines if (cid := profiles[product_by_line[line.line_id]].category_id) is not None]
        ceilings = self.cache.get_category_ceilings(r.market, category_ids)
        for line in scored_lines:
            hp = profiles[product_by_line[line.line_id]]
            if line.is_substitutable is False or line.partner_sku in reco_labels:
                continue
            if any(_starts_with_ignore_case(hp.category_code, c) for c in cfg.excluded_categories):
                continue
            cid = hp.category_id
            if cid is None or cid not in ceilings or hp.hsi >= ceilings[cid]:
                continue
            targets.append((line, hp))

        # 5. Candidate alternatives per target line from the precomputed graph.
        edges = self.cache.get_edges(r.market, graph, [hp.product_id for _, hp in targets])
        alt_profiles = self.cache.get_profiles(r.market, [x.to for e in edges.values() for x in e])
        shown_profiles.update(alt_profiles)
        max_alternatives = _clamp(cfg.max_alternatives_per_line, 1, self.options.max_alternatives_cap)
        availability_mode = r.availability_mode if r.availability_mode is not None else cfg.availability_mode
        fatigue_key = r.user_ref if r.user_ref is not None else "cart:" + r.cart_ref
        work: list[_LineWork] = []
        for line, hp in targets:
            # Soft budget: always serve at least one line, then stop computing further lines once over budget.
            if self.budget_exceeded(len(work), _elapsed_ms(start), self.options.internal_budget_ms):
                degradations.add("TIMEOUT_BUDGET")
                break
            line_edges = edges.get(hp.product_id)
            if line_edges is None:
                continue
            unit_price = self.unit_price(line)
            alts = self._candidates(
                ctx.partner_id, cfg, pack, r.market, hp, unit_price, r.store_id, availability_mode, shopper, placement, fatigue_key, line_edges,
                alt_profiles, degradations, max_alternatives, evaluation_id,
            )
            if alts:
                work.append(_LineWork(line, hp, unit_price, alts))

        # 6. Cart-level selection: at most M lines, one per category, maximizing health gain x quantity.
        max_suggestions = min(r.max_suggestions if r.max_suggestions is not None else cfg.max_suggestions_per_cart, cfg.max_suggestions_per_cart)
        selected = set(CartSelection.select(
            [
                CartCandidate(
                    w.line.line_id, w.original.category_code, w.alternatives[0].health_delta, w.line.quantity,
                    _ONE + decimal_mul(_QUARTER, Decimal(sum(1 for f in w.original.flags if f.startswith("HIGH_")))), w.alternatives[0].price_ratio,
                )
                for w in work
            ],
            max_suggestions,
        ))

        # 7. Explanations, labels and suggestion snapshots.
        suggestions: list[dict[str, Any]] = []
        shown: list[uuid.UUID] = []
        snapshots: list[SuggestionSnapshot] = []
        training: list[Any] = []
        line_index = {}
        for i, line in enumerate(r.lines):
            line_index.setdefault(line.line_id, i)
        for w in sorted((w for w in work if w.line.line_id in selected), key=lambda w: line_index[w.line.line_id]):
            suggestion_id = new_id()
            alternatives = [
                self._alternative(pack, r.market, locale, w.original, a, i + 1, w.unit_price, r.currency, w.line.quantity, label)
                for i, a in enumerate(w.alternatives)
            ]
            suggestions.append({
                "suggestionId": str(suggestion_id), "lineId": w.line.line_id, "original": ApiMapper.product(w.original, w.line.partner_sku, w.line.title),
                "alternatives": alternatives, "expiresAt": iso(now + ttl),
            })
            shown.append(w.original.product_id)
            shown.extend(a.alt.product_id for a in w.alternatives)
            snapshot = self._snapshot(
                suggestion_id, ctx.partner_id, evaluation_id, r.cart_ref, r.user_ref, r.market, r.store_id, placement, w.line.line_id,
                w.line.partner_sku, w.original, w.line.quantity, w.unit_price, w.line.tax_rate, r.currency, w.alternatives, reco_fee, pack, graph, now, cfg,
            )
            snapshots.append(snapshot)
            training.append({
                "suggestionId": suggestion_id, "lineId": w.line.line_id, "placement": placement, "fromProductId": w.original.product_id,
                "alternatives": self._training_alternatives(w.alternatives),
            })
            self.cache.set_suggestion(snapshot, snapshot.attribution_expires_at - now + timedelta(hours=1))
        log.debug("Evaluation %s: %d targets, %d suggestions in %d ms", evaluation_id, len(targets), len(suggestions), _elapsed_ms(start))
        return respond(suggestions, shown, snapshots, training)

    # --- GET /cart-evaluations/{evaluationId} -------------------------------------------------------------------------

    def get(self, partner_id: uuid.UUID, evaluation_id: uuid.UUID) -> dict[str, Any]:
        """The cached CartEvaluation until it expires; HS-NOT-FOUND afterwards or for another partner."""
        text = self.cache.get_evaluation_json(partner_id, evaluation_id)
        if text is None:
            raise HsError.not_found(f"Cart evaluation {evaluation_id}")
        return jsonutil.loads(text)

    # --- GET /products/{productId}/alternatives -----------------------------------------------------------------------

    def alternatives(self, ctx: PartnerContext, product_id: uuid.UUID, market: str, store_id: str | None = None, cart_ref: str | None = None,
                     user_ref: str | None = None, placement: str | None = None, currency: str | None = None, locale: str | None = None,
                     limit: int = 5) -> dict[str, Any]:
        """Product-page and search alternatives (placement PDP or SEARCH) -> AlternativesResponse. Mints a suggestion when a cart is
        known (RA-1)."""
        partner = self.partners.get(ctx.partner_id)
        cfg = partner.config
        cart_ref = _coalesce(cart_ref, ctx.sdk_cart_ref)
        user_ref = _coalesce(user_ref, ctx.sdk_user_ref)
        store_id = _coalesce(store_id, ctx.sdk_store_id)
        self.authorize(ctx, partner, market, cart_ref, user_ref, store_id)
        currency = _coalesce(currency, cfg.currency)
        if currency != cfg.currency:
            raise HsError.unprocessable("HS-CURRENCY-MISMATCH", f"Currency must be {cfg.currency} for this partner")
        placement = _coalesce(placement, "PDP")
        pack = self.refs.get_pack(market)
        original = self.cache.get_profile(market, product_id)
        if original is None:
            raise HsError.not_found(f"Product {product_id}")
        loc = ApiMapper.resolve_locale(market, locale)
        reco_fee = None if cart_ref is None else RecoAddFeeRule.snapshot(cfg.fee.recommendation_add, currency)
        label = ApiMapper.label(ShopperCopy.label(loc, partner.display_name, reco_fee is not None))
        disclosures = self.disclosures(pack, loc, partner, reco_fee is not None, [], {})
        empty = _compact({"productId": str(product_id), "market": market, "storeId": store_id, "alternatives": [], "disclosures": disclosures})
        graph = self.cache.get_active_graph(market)
        if graph is None or cfg.kill_switch or placement not in cfg.placements or placement in cfg.disabled_placements:
            return empty

        original_skus = self.cache.get_partner_skus(ctx.partner_id, product_id)
        original_price: Decimal | None = None
        if store_id is not None and len(original_skus) > 0:
            prices = [i.price for i in self.cache.get_inventory(ctx.partner_id, store_id, original_skus).values() if i.in_stock]
            original_price = min(prices) if prices else None
        edges = self.cache.get_edges(market, graph, [product_id]).get(product_id)
        if edges is None:
            return empty
        alt_profiles = self.cache.get_profiles(market, [e.to for e in edges])
        shopper = self._shopper(ctx.partner_id, cfg, user_ref, None, None)
        degradations: set[str] = set()
        evaluation_key = new_id()
        alts = self._candidates(
            ctx.partner_id, cfg, pack, market, original, original_price, store_id, cfg.availability_mode, shopper, placement,
            user_ref if user_ref is not None else "cart:" + (cart_ref if cart_ref is not None else "anonymous"), edges, alt_profiles, degradations,
            _clamp(limit, 1, 10), evaluation_key,
        )

        now = self.clock.utc_now()
        suggestion_id = None if cart_ref is None or len(alts) == 0 else new_id()
        alternatives = [self._alternative(pack, market, loc, original, a, i + 1, original_price, currency, _ONE, label) for i, a in enumerate(alts)]
        expires: datetime | None = None
        if suggestion_id is not None:
            from_sku = original_skus[0] if original_skus else f"PRODUCT:{product_id}"
            snapshot = self._snapshot(
                suggestion_id, ctx.partner_id, None, cart_ref, user_ref, market, store_id, placement, None, from_sku, original, _ONE, original_price,
                None, currency, alts, reco_fee, pack, graph, now, cfg,
            )
            self.cache.set_suggestion(snapshot, snapshot.attribution_expires_at - now + timedelta(hours=1))
            expires = snapshot.expires_at
            training = {"suggestionId": suggestion_id, "placement": placement, "fromProductId": product_id, "alternatives": self._training_alternatives(alts)}
            self.recorder.enqueue(EvaluationRecord(
                ctx.partner_id, None, cart_ref, user_ref, store_id, market, placement, 1, len(degradations) > 0, 0, cfg.experiment.name,
                self._versions(pack, graph), (snapshot,), (training,), now,
            ))
        return _compact({
            "productId": str(product_id), "market": market, "storeId": store_id, "suggestionId": None if suggestion_id is None else str(suggestion_id),
            "expiresAt": iso(expires), "alternatives": alternatives,
            "disclosures": self.disclosures(pack, loc, partner, reco_fee is not None, [a.alt.product_id for a in alts], alt_profiles),
        })

    # --- policy helpers (C# private statics; public for tests) ---------------------------------------------------------

    @staticmethod
    def authorize(ctx: PartnerContext, partner: PartnerInfo, market: str, cart_ref: str | None, user_ref: str | None, store_id: str | None) -> None:
        if market not in partner.markets:
            raise HsError.unprocessable("HS-UNSUPPORTED-MARKET", f"Market {market} is not enabled for this partner")
        if partner.status in ("SUSPENDED", "OFFBOARDED"):
            raise HsError.forbidden("The partner account is not active")
        if not ctx.is_sdk_session:
            return
        if ctx.sdk_market is not None and ctx.sdk_market != market:
            raise HsError.forbidden("The SDK session is bound to another market")
        if cart_ref is not None and ctx.sdk_cart_ref != cart_ref:
            raise HsError.forbidden("The SDK session is bound to another cart")
        if ctx.sdk_user_ref is not None and user_ref is not None and ctx.sdk_user_ref != user_ref:
            raise HsError.forbidden("The SDK session is bound to another user")
        if ctx.sdk_store_id is not None and store_id is not None and ctx.sdk_store_id != store_id:
            raise HsError.forbidden("The SDK session is bound to another store")

    @staticmethod
    def placement_disabled(cfg: PartnerConfig, placement: str) -> bool:
        """Kill switch, a disabled placement or a placement the partner did not enable (degradation KILL_SWITCH)."""
        return cfg.kill_switch or placement in cfg.disabled_placements or placement not in cfg.placements

    @staticmethod
    def in_holdout(cfg: PartnerConfig, key: str) -> bool:
        """Stable holdout bucket: SHA-256(experiment name + ":" + key), first 4 bytes little-endian, modulo 100."""
        if cfg.experiment.holdout_percent <= 0:
            return False
        digest = hashlib.sha256(_utf8(cfg.experiment.name + ":" + key)).digest()
        return int.from_bytes(digest[:4], "little") % 100 < cfg.experiment.holdout_percent

    @staticmethod
    def budget_exceeded(lines_served: int, elapsed_ms: int, budget_ms: int) -> bool:
        """Soft internal budget: at least one line is always served, later lines stop once the budget is exceeded."""
        return lines_served > 0 and elapsed_ms > budget_ms

    @staticmethod
    def degradation_reasons(degradations: Collection[str]) -> list[str] | None:
        """CartEvaluation.degradationReasons: ordinal-sorted (C# SortedSet), None when the evaluation is not degraded."""
        return sorted(degradations, key=_ordinal) if len(degradations) > 0 else None

    @staticmethod
    def unit_price(line: CartLine) -> Decimal | None:
        """Unit price after the line discount spread over the quantity; None when the price does not parse."""
        up = _try_parse_currency(line.unit_price.amount)
        if up is None:
            return None
        discount = _ZERO
        if line.line_discount is not None:
            dv = _try_parse_currency(line.line_discount.amount)
            if dv is not None:
                discount = decimal_div(dv, decimal_max(_ONE, line.quantity))
        return up - discount

    @staticmethod
    def local_hour(market: str, utc: datetime) -> int:
        """Shopper-local hour for the daypart features (IN: Asia/Kolkata, US: America/New_York, else UTC)."""
        try:
            return as_utc(utc).astimezone(ZoneInfo(_TIME_ZONES.get(market, "UTC"))).hour
        except ZoneInfoNotFoundError:
            return as_utc(utc).hour

    @staticmethod
    def summary(lines_total: int, product_by_line: Mapping[str, uuid.UUID], profiles: Mapping[uuid.UUID, HealthProfileData], scored: Sequence[CartLine],
                with_suggestions: int) -> dict[str, Any]:
        """CartHealthSummary: quantity-weighted cart HSI (1 decimal) and flag counts in order of first appearance."""
        hsi_total, qty = _ZERO, _ZERO
        flags: dict[str, int] = {}
        for line in scored:
            hp = profiles[product_by_line[line.line_id]]
            hsi_total += decimal_mul(hp.hsi, line.quantity)
            qty += line.quantity
            for f in hp.flags:
                flags[f] = flags.get(f, 0) + 1
        return _compact({
            "linesTotal": lines_total, "linesResolved": len(product_by_line), "linesScored": len(scored), "linesWithSuggestions": with_suggestions,
            "cartHsi": round_decimal(decimal_div(hsi_total, qty), 1) if qty > 0 else None, "flagCounts": flags if flags else None,
        })

    @staticmethod
    def disclosures(pack: MarketPack, locale: str, partner: PartnerInfo, add_fee: bool, shown: Sequence[uuid.UUID],
                    profiles: Mapping[uuid.UUID, HealthProfileData]) -> dict[str, Any]:
        """Contracts.Disclosures; data attributions for the shown products found in `profiles`."""
        attributions = ApiMapper.attributions(s for pid in shown if pid in profiles for s in profiles[pid].sources)
        return _compact({
            "feeDisclosure": ShopperCopy.fee_disclosure(locale, partner.display_name, add_fee), "labelDisclaimer": ShopperCopy.generic_disclaimer(locale),
            "methodologyUrl": pack.methodology_url, "attributions": attributions if attributions else None,
        })

    # --- internals ------------------------------------------------------------------------------------------------------

    def _versions(self, pack: MarketPack, graph: str) -> ModelVersions:
        return ModelVersions(f"{pack.primary_model}@{pack.primary.version}+HSI@{pack.hsi.version}", pack.rules.version, graph, self.models.ranker_version)

    def _shopper(self, partner_id: uuid.UUID, cfg: PartnerConfig, user_ref: str | None, inline: Mapping[str, Any] | None,
                 context_minor: bool | None) -> _Shopper:
        stored: ShopperPrefsDoc | None = None if user_ref is None else self.prefs.load_for_evaluation(partner_id, user_ref)

        def pick(key: str, attr: str) -> Any:
            return _coalesce(None if inline is None else inline.get(key), None if stored is None else getattr(stored, attr))

        health_consent = _coalesce(pick("healthDataConsent", "health_data_consent"), False)
        is_minor = _coalesce(context_minor, pick("isMinor", "is_minor"))
        treat_as_minor = is_minor is True or (is_minor is None and cfg.minors_policy == "STRICT")
        personalize = not treat_as_minor and _coalesce(pick("personalizationConsent", "personalization_consent"), False)
        constraints = ShopperConstraints(
            diet_type=_coalesce(pick("dietType", "diet_type"), "ANY"),
            allergens=tuple(_coalesce(pick("allergens", "allergens"), ())) if health_consent else (),
            strict_may_contain=_coalesce(pick("strictMayContain", "strict_may_contain"), True),
            avoid_ingredients=tuple(_coalesce(pick("avoidIngredients", "avoid_ingredients"), ())),
            excluded_brands=tuple(_coalesce(pick("excludedBrands", "excluded_brands"), ())),
            religious_preferences=tuple(_coalesce(pick("religiousPreferences", "religious_preferences"), ())),
            treat_as_minor=treat_as_minor,
        )
        goals = tuple(_coalesce(pick("goals", "goals"), ())) if health_consent and personalize else ()
        return _Shopper(constraints, goals, personalize, pick("locale", "locale"))

    def _candidates(self, partner_id: uuid.UUID, cfg: PartnerConfig, pack: MarketPack, market: str, original: HealthProfileData,
                    original_price: Decimal | None, store_id: str | None, availability_mode: str, shopper: _Shopper, placement: str, fatigue_key: str,
                    edges: Sequence[SwapEdge], alt_profiles: Mapping[uuid.UUID, HealthProfileData], degradations: set[str], max_count: int,
                    evaluation_id: uuid.UUID) -> list[_Scored]:
        guards = CommercialGuards(
            max_normalized_price_ratio=cfg.price_guards.max_normalized_price_ratio, max_abs_premium_per_unit=cfg.price_guards.max_abs_premium_per_unit,
            pack_ratio_min=cfg.pack_size_ratio.min, pack_ratio_max=cfg.pack_size_ratio.max, blocked_brands=cfg.blocked_brands,
            excluded_categories=cfg.excluded_categories,
        )
        dismissed = self.cache.get_dismissed(partner_id, fatigue_key, [(original.product_id, e.to) for e in edges])
        hour = self.local_hour(market, self.clock.utc_now())
        pending: list[tuple[HealthProfileData, SwapEdge, Decimal, list[str]]] = []
        for e in edges:
            alt = alt_profiles.get(e.to)
            if alt is None or not alt.eligible:
                continue
            if (original.product_id, e.to) in dismissed:  # R8 fatigue
                continue
            delta = e.health_delta
            if e.from_version != original.version_id or e.to_version != alt.version_id:
                # A profile changed after the graph build: re-check the static rules before serving the edge.
                recheck = StaticRules.check(original, alt, e.substitutability, True, pack)
                if not recheck.eligible:
                    continue
                delta = recheck.health_delta
            skus = self.cache.get_partner_skus(partner_id, alt.product_id)
            if len(skus) > 0:
                pending.append((alt, e, delta, skus))
        inventory: dict[str, InventoryItem] = {}
        if store_id is not None and pending:
            inventory = self.cache.get_inventory(partner_id, store_id, list(dict.fromkeys(s for p in pending for s in p[3])))
        stale_before = self.clock.utc_now() - timedelta(seconds=cfg.inventory_staleness_seconds)

        scored: list[_Scored] = []
        for alt, edge, delta, skus in pending:
            sku: str | None = None
            inv: InventoryItem | None = None
            requires_check = availability_mode == "PARTNER_FILTER" or store_id is None
            ordered = sorted(skus, key=_ordinal)
            for s in ordered:
                i = inventory.get(s)
                if i is not None:
                    if not i.in_stock:
                        continue
                    if inv is None or i.price < inv.price:
                        sku, inv = s, i
                elif sku is None and requires_check:
                    sku = s
            if sku is None and availability_mode == "MIRROR" and store_id is not None:
                continue  # not in stock at this store
            if inv is not None and inv.observed_at < stale_before:
                requires_check = True
                degradations.add("INVENTORY_STALE")
            if sku is None:
                sku = ordered[0]

            price = PriceContext(original_price, None if inv is None else inv.price)
            if RequestRules.reject(original, alt, shopper.constraints, guards, price, pack.rules.verified_allergen_confidence) is not None:
                continue

            price_ratio = None
            if original_price is not None and inv is not None and original_price > 0:
                price_ratio = RequestRules.normalized_price_ratio(original_price, original.net_quantity, inv.price, alt.net_quantity)
            so, sa = original.net_quantity, alt.net_quantity
            pack_ratio = decimal_div(sa.value, so.value) if so is not None and sa is not None and so.is_comparable_to(sa) and so.value > 0 else None
            goal_match = Ranking.goal_match(shopper.goals, edge.reasons) if shopper.personalize else _ZERO
            same_brand = equals_ignore_case(alt.brand, original.brand)
            nova_original = original.nova if original.nova is not None else 0
            nova_alt = _coalesce(alt.nova, original.nova, 0)
            features = RankerFeatures.vector(RankerFeatures.Input(
                delta, edge.substitutability, price_ratio, not same_brand, pack_ratio, placement, hour, goal_match, alt.category_code.startswith("beverages"),
                nova_original - nova_alt, same_brand,
            ))
            scored.append(_Scored(
                alt, sku, inv, delta, edge.substitutability, None if price_ratio is None else round_decimal(price_ratio, 2), _ZERO, requires_check,
                tuple(edge.reasons), features,
            ))
        if not scored:
            return scored

        p_accept = self.models.predict_acceptance([s.features for s in scored])
        ranked = [
            replace(s, score=Ranking.score(
                _WEIGHTS, s.health_delta, s.substitutability, None if p_accept is None else p_accept[i], s.price_ratio, _ZERO,
                Ranking.goal_match(shopper.goals, s.reasons),
            ))
            for i, s in enumerate(scored)
        ]
        # OrderByDescending(score).ThenByDescending(healthDelta).ThenBy(sku, ordinal): stable sorts, least significant key first.
        ranked.sort(key=lambda s: _ordinal(s.sku))
        ranked.sort(key=lambda s: s.health_delta, reverse=True)
        ranked.sort(key=lambda s: s.score, reverse=True)
        ranked = ranked[:max_count]
        if len(ranked) >= 2 and cfg.exploration_rate > 0 and Ranking.is_exploration(evaluation_id, cfg.exploration_rate):
            ranked[0], ranked[1] = ranked[1], ranked[0]
        return ranked

    def _alternative(self, pack: MarketPack, market: str, locale: str, original: HealthProfileData, a: _Scored, rank: int,
                     original_price: Decimal | None, currency: str, quantity: Decimal, label: dict[str, Any]) -> dict[str, Any]:
        sheet = FactSheets.build(original, a.alt, pack)
        selected = FactSheets.select(sheet)
        cached = self.cache.get_explanation(market, locale, original.product_id, a.alt.product_id, sheet.hash)
        if cached is not None:
            rendered = RenderedExplanation(cached.short, tuple(cached.bullets), tuple(f.id for f in selected), cached.generator, cached.ai_generated, locale)
        else:
            rendered = ExplanationTemplates.render(selected if len(selected) > 0 else list(sheet.facts[:1]), locale)
        explanation = _compact({
            "short": rendered.short if len(rendered.short) > 0 else ShopperCopy.label_text(locale),
            "bullets": list(rendered.bullets[:3]) if len(rendered.bullets) > 0 else None,
            "details": ApiMapper.details(sheet.facts), "generator": rendered.generator, "aiGenerated": rendered.ai_generated, "locale": locale,
            "methodologyUrl": pack.methodology_url, "disclaimer": ShopperCopy.label_disclaimer(locale, a.alt.nutrition.is_liquid),
        })
        inv = a.inventory
        return _compact({
            "rank": rank, "product": ApiMapper.product(a.alt, a.sku), "recommendation": label,
            "unitPrice": None if inv is None else ApiMapper.money(inv.price, currency),
            "unitPriceDelta": None if inv is None or original_price is None else ApiMapper.money(inv.price - original_price, currency),
            "normalizedPriceRatio": a.price_ratio, "healthDelta": a.health_delta, "reasonCodes": list(a.reasons), "explanation": explanation,
            "sponsored": False, "requiresAvailabilityCheck": a.requires_check, "suggestedQuantity": quantity,
        })

    def _snapshot(self, suggestion_id: uuid.UUID, partner_id: uuid.UUID, evaluation_id: uuid.UUID | None, cart_ref: str, user_ref: str | None, market: str,
                  store_id: str | None, placement: str, line_id: str | None, from_sku: str, original: HealthProfileData, qty: Decimal,
                  unit_price: Decimal | None, tax_rate: Decimal | None, currency: str, alts: Sequence[_Scored], reco_fee: RecoFeeSnapshot | None,
                  pack: MarketPack, graph: str, now: datetime, cfg: PartnerConfig) -> SuggestionSnapshot:
        nq = original.net_quantity
        return SuggestionSnapshot(
            suggestion_id=suggestion_id, partner_id=partner_id, evaluation_id=evaluation_id, cart_ref=cart_ref, user_ref=user_ref, market=market,
            store_id=store_id, placement=placement, line_id=line_id, from_partner_sku=from_sku, from_product_id=original.product_id,
            from_version_id=original.version_id, from_qty=qty, from_unit_price=unit_price, from_net_quantity=None if nq is None else nq.value,
            from_net_unit=None if nq is None else nq.unit, from_tax_rate=tax_rate, currency=currency,
            alternatives=tuple(
                AlternativeSnapshot(
                    i + 1, a.sku, a.alt.product_id, a.alt.version_id, None if a.inventory is None else a.inventory.price,
                    None if a.alt.net_quantity is None else a.alt.net_quantity.value, None if a.alt.net_quantity is None else a.alt.net_quantity.unit,
                    a.health_delta, False, list(a.features),
                )
                for i, a in enumerate(alts)
            ),
            reco_fee=reco_fee, rules_version=pack.rules.version, graph_version=graph, created_at=now,
            expires_at=now + timedelta(minutes=self.options.suggestion_ttl_minutes), attribution_expires_at=now + timedelta(hours=cfg.attribution_window_hours),
        )

    @staticmethod
    def _training_alternatives(alts: Sequence[_Scored]) -> list[dict[str, Any]]:
        return [
            {"rank": i + 1, "productId": a.alt.product_id, "partnerSku": a.sku, "features": list(a.features), "score": a.score}
            for i, a in enumerate(alts)
        ]


# ---------------------------------------------------------------------------------------------------------------------
# EvaluationRecorder
# ---------------------------------------------------------------------------------------------------------------------


class EvaluationRecorder(BackgroundWorker):
    """
    Asynchronous persistence of evaluations and suggestions (HLD 7.1: the hot path never waits for PostgreSQL). Writes
    txn.cart_evaluation and txn.suggestion and emits hs.cart-evaluations.v1 (ranker training data with features and positions).
    """

    # Bounded channel (BoundedChannelFullMode.DropOldest).
    CAPACITY = 50_000

    def __init__(self, db: Db) -> None:
        super().__init__()
        self.db = db
        self._queue: queue.Queue[EvaluationRecord] = queue.Queue(maxsize=self.CAPACITY)
        self._write_lock = threading.Lock()
        self._counter_lock = threading.Lock()
        self._enqueued = 0
        self._written = 0

    @property
    def enqueued(self) -> int:
        with self._counter_lock:
            return self._enqueued

    @property
    def written(self) -> int:
        """Records handled by the worker (written or failed)."""
        with self._counter_lock:
            return self._written

    def enqueue(self, r: EvaluationRecord) -> None:
        """Queues a record without blocking; when the queue is full the oldest record is dropped."""
        with self._write_lock:
            while True:
                try:
                    self._queue.put_nowait(r)
                    break
                except queue.Full:
                    try:
                        self._queue.get_nowait()
                    except queue.Empty:
                        continue
                    self._queue.task_done()
            with self._counter_lock:
                self._enqueued += 1

    def flush(self, timeout: float | timedelta = 10.0) -> bool:
        """Waits until everything enqueued so far is persisted (tests, CLI, finalizer safety). False when the timeout elapsed first."""
        seconds = timeout.total_seconds() if isinstance(timeout, timedelta) else float(timeout)
        target = self.enqueued
        deadline = time.monotonic() + seconds
        while self.written < target and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.written >= target

    def write(self, r: EvaluationRecord) -> None:
        def work(u: Uow) -> None:
            if r.evaluation_id is not None:
                u.execute(
                    """
                    insert into txn.cart_evaluation (evaluation_id, created_at, partner_id, cart_ref, user_ref, store_id, market, placement, line_count,
                        suggestion_count, degraded, latency_ms, experiment_arm, model_versions)
                    values (@evaluationId, @at, @partnerId, @cartRef, @userRef, @storeId, @market, @placement, @lines, @suggestions, @degraded, @latency, @arm, @versions::jsonb)
                    """,
                    {
                        "evaluationId": r.evaluation_id, "at": r.created_at, "partnerId": r.partner_id, "cartRef": r.cart_ref, "userRef": r.user_ref,
                        "storeId": r.store_id, "market": r.market, "placement": r.placement, "lines": min(r.line_count, 32767),
                        "suggestions": len(r.suggestions), "degraded": r.degraded, "latency": r.latency_ms, "arm": r.experiment_arm,
                        "versions": netjson.serialize_hs(r.versions.to_api()),
                    },
                )
            for s in r.suggestions:
                u.execute(
                    """
                    insert into txn.suggestion (suggestion_id, created_at, evaluation_id, partner_id, cart_ref, user_ref, market, store_id, placement, line_id,
                        from_partner_sku, from_product_id, from_version_id, from_qty, from_unit_price, from_net_quantity, from_net_unit, from_tax_rate, currency,
                        alternatives, reco_fee, rules_version, graph_version, expires_at, attribution_expires_at)
                    values (@SuggestionId, @CreatedAt, @EvaluationId, @PartnerId, @CartRef, @UserRef, @Market, @StoreId, @Placement, @LineId, @FromPartnerSku,
                        @FromProductId, @FromVersionId, @FromQty, @FromUnitPrice, @FromNetQuantity, @FromNetUnit, @FromTaxRate, @Currency, @alternatives::jsonb,
                        @recoFee::jsonb, @RulesVersion, @GraphVersion, @ExpiresAt, @AttributionExpiresAt)
                    """,
                    {
                        "SuggestionId": s.suggestion_id, "CreatedAt": s.created_at, "EvaluationId": s.evaluation_id, "PartnerId": s.partner_id,
                        "CartRef": s.cart_ref, "UserRef": s.user_ref, "Market": s.market, "StoreId": s.store_id, "Placement": s.placement,
                        "LineId": s.line_id, "FromPartnerSku": s.from_partner_sku, "FromProductId": s.from_product_id, "FromVersionId": s.from_version_id,
                        "FromQty": s.from_qty, "FromUnitPrice": s.from_unit_price, "FromNetQuantity": s.from_net_quantity, "FromNetUnit": s.from_net_unit,
                        "FromTaxRate": s.from_tax_rate, "Currency": s.currency, "alternatives": HotCache.to_json(list(s.alternatives)),
                        "recoFee": None if s.reco_fee is None else HotCache.to_json(s.reco_fee), "RulesVersion": s.rules_version,
                        "GraphVersion": s.graph_version, "ExpiresAt": s.expires_at, "AttributionExpiresAt": s.attribution_expires_at,
                    },
                )
            if r.evaluation_id is not None:
                key = str(r.evaluation_id)
            else:
                key = str(r.suggestions[0].suggestion_id) if r.suggestions else "pdp"
            Outbox.enqueue(u, r.partner_id, Topics.CART_EVALUATIONS, key, {
                "evaluationId": r.evaluation_id, "market": r.market, "placement": r.placement, "storeId": r.store_id, "degraded": r.degraded,
                "latencyMs": r.latency_ms, "experimentArm": r.experiment_arm, "versions": r.versions, "createdAt": r.created_at, "rows": list(r.training_rows),
            })

        self.db.in_tx(DbRole.APP, r.partner_id, work)

    def execute(self) -> None:
        for r in self.read_all(self._queue):
            try:
                self.write(r)
            except Exception:
                self.log.exception("Failed to record evaluation %s", r.evaluation_id)
            finally:
                with self._counter_lock:
                    self._written += 1
