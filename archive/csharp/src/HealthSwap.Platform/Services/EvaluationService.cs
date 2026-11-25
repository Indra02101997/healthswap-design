using System.Diagnostics;
using System.Text.Json;
using System.Threading.Channels;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Domain.Commerce;
using HealthSwap.Domain.Recommendation;
using HealthSwap.Domain.Scoring;
using HealthSwap.Platform.Bus;
using HealthSwap.Platform.Cache;
using HealthSwap.Platform.Data;
using HealthSwap.Platform.Knowledge;
using HealthSwap.Platform.Ml;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace HealthSwap.Platform.Services;

public sealed class EvaluationOptions
{
    /// <summary>Internal compute budget for the hot path; beyond it remaining lines are skipped (degraded TIMEOUT_BUDGET).</summary>
    public int InternalBudgetMs { get; set; } = 100;
    public int SuggestionTtlMinutes { get; set; } = 30;
    public int MaxAlternativesCap { get; set; } = 5;
}

/// <summary>Everything the recorder persists for one evaluation or one product-page recommendation.</summary>
public sealed record EvaluationRecord(Guid PartnerId, Guid? EvaluationId, string CartRef, string? UserRef, string? StoreId, string Market, string Placement,
    int LineCount, bool Degraded, int LatencyMs, string? ExperimentArm, ModelVersions Versions, IReadOnlyList<SuggestionSnapshot> Suggestions,
    IReadOnlyList<object> TrainingRows, DateTime CreatedAt);

/// <summary>
/// Cart evaluation orchestrator (HLD 7.1 and doc 03 section 7): resolve lines, load profiles and preferences, select target
/// lines, read precomputed swap edges, apply request-time rules (R5-R8), rank, select per cart, attach explanations and the
/// RA-1 recommendation label, mint suggestions. Only the hot cache is read on this path (no PostgreSQL, no LLM).
/// </summary>
public sealed class EvaluationService(HotCache cache, PartnerDirectory partners, ReferenceDataRepository refs, PreferenceService prefs, ModelRegistry models,
    EvaluationRecorder recorder, EvaluationOptions options, IClock clock, ILogger<EvaluationService> log)
{
    private static readonly RankWeights Weights = new();

    private sealed record Scored(HealthProfileData Alt, string Sku, InventoryItem? Inventory, decimal HealthDelta, decimal Substitutability, decimal? PriceRatio,
        decimal Score, bool RequiresCheck, IReadOnlyList<string> Reasons, float[] Features);

    private sealed record Shopper(ShopperConstraints Constraints, IReadOnlyCollection<string> Goals, bool Personalize, string? Locale);

    private sealed record LineWork(CartLine Line, HealthProfileData Original, decimal? UnitPrice, List<Scored> Alternatives);

    public async Task<CartEvaluation> EvaluateAsync(PartnerContext ctx, CartEvaluationRequest req, CancellationToken ct)
    {
        var sw = Stopwatch.StartNew();
        var partner = await partners.GetAsync(ctx.PartnerId, ct);
        var cfg = partner.Config;
        Authorize(ctx, partner, req.Market, req.CartRef, req.UserRef, req.StoreId);
        if (!string.Equals(req.Currency, cfg.Currency, StringComparison.Ordinal))
            throw HsException.Unprocessable("HS-CURRENCY-MISMATCH", $"Cart currency must be {cfg.Currency} for this partner");
        if (req.Lines.Count > cfg.MaxLines) throw HsException.TooLarge($"Carts are limited to {cfg.MaxLines} lines");
        foreach (var l in req.Lines)
            if (l.UnitPrice.Currency != req.Currency) throw HsException.Invalid($"Line {l.LineId}: unitPrice currency must be {req.Currency}");

        var pack = await refs.GetPackAsync(req.Market, ct);
        var now = clock.UtcNow;
        var evaluationId = Ids.NewId();
        var placement = req.Context?.Placement ?? "CART";
        var locale = ApiMapper.ResolveLocale(req.Market, req.Locale);
        var degradations = new SortedSet<string>(StringComparer.Ordinal);
        var recoFee = RecoAddFeeRule.Snapshot(cfg.Fee.RecommendationAdd, req.Currency);
        var label = ApiMapper.Label(ShopperCopy.Label(locale, partner.DisplayName, recoFee is not null));
        var graph = await cache.GetActiveGraphAsync(req.Market);
        var versions = new ModelVersions($"{pack.PrimaryModel}@{pack.Primary.Version}+HSI@{pack.Hsi.Version}", pack.Rules.Version, graph ?? "none", models.RankerVersion);

        // 1. Resolve lines to canonical products: partner catalog mapping first, then the GTIN fallback.
        var maps = await cache.GetSkuMapsAsync(ctx.PartnerId, req.Lines.Select(l => l.PartnerSku).ToList());
        var productByLine = new Dictionary<string, Guid>();
        for (var i = 0; i < req.Lines.Count; i++)
        {
            var line = req.Lines[i];
            if (maps[i] is { } m) productByLine[line.LineId] = m.ProductId;
            else if (line.Gtin is { } g && Gtin.TryNormalize(g, out var g14) && await cache.GetProductByGtinAsync(g14) is { } pid) productByLine[line.LineId] = pid;
        }
        var profiles = await cache.GetProfilesAsync(req.Market, productByLine.Values.ToList());
        var scoredLines = req.Lines.Where(l => productByLine.TryGetValue(l.LineId, out var p) && profiles.TryGetValue(p, out var hp) && hp.Hsi is not null).ToList();
        var recoLabels = await cache.GetCartRecoLabelsAsync(ctx.PartnerId, req.CartRef);
        var lineLabels = req.Lines.Where(l => recoLabels.ContainsKey(l.PartnerSku)).Select(l => new LineLabel(l.LineId, l.PartnerSku, recoLabels[l.PartnerSku], label)).ToList();

        var summary = Summary(req, productByLine, profiles, scoredLines, 0);
        var arm = req.Context?.ExperimentArm ?? (InHoldout(cfg, req.UserRef ?? req.CartRef) ? "holdout" : cfg.Experiment.Name);
        async Task<CartEvaluation> RespondAsync(List<LineSuggestion> suggestions, IReadOnlyList<SuggestionSnapshot> snapshots, IReadOnlyList<object> training)
        {
            var result = new CartEvaluation
            {
                EvaluationId = evaluationId, CartRef = req.CartRef, Market = req.Market, StoreId = req.StoreId, CreatedAt = now,
                ExpiresAt = now.AddMinutes(options.SuggestionTtlMinutes), Degraded = degradations.Count > 0,
                DegradationReasons = degradations.Count > 0 ? degradations.ToList() : null,
                Summary = summary with { LinesWithSuggestions = suggestions.Count }, Suggestions = suggestions, LineLabels = lineLabels,
                Disclosures = Disclosures(pack, locale, partner, recoFee is not null, suggestions.SelectMany(s => s.Alternatives.Select(a => a.Product.ProductId)).Distinct(), profiles),
                ModelVersions = versions,
            };
            recorder.Enqueue(new EvaluationRecord(ctx.PartnerId, evaluationId, req.CartRef, req.UserRef, req.StoreId, req.Market, placement, req.Lines.Count,
                result.Degraded, (int)sw.ElapsedMilliseconds, arm, versions, snapshots, training, now));
            await cache.SetEvaluationJsonAsync(ctx.PartnerId, evaluationId, JsonSerializer.Serialize(result, HsJson.Options), TimeSpan.FromMinutes(options.SuggestionTtlMinutes));
            return result;
        }

        // 2. Kill switch, placement policy, holdout arm.
        if (cfg.KillSwitch || cfg.DisabledPlacements.Contains(placement) || !cfg.Placements.Contains(placement))
        {
            degradations.Add("KILL_SWITCH");
            return await RespondAsync([], [], []);
        }
        if (arm == "holdout") return await RespondAsync([], [], []);
        if (graph is null) { degradations.Add("NO_DATA"); return await RespondAsync([], [], []); }

        // 3. Shopper constraints with consent handling and the minors policy.
        var shopper = await ShopperAsync(ctx.PartnerId, cfg, req.UserRef, req.Preferences, req.Context?.IsMinor, ct);

        // 4. Target lines: substitutable, scored, below the category ceiling (p60), not already a HealthSwap recommendation.
        var targets = new List<(CartLine Line, HealthProfileData Hp)>();
        var ceilings = await cache.GetCategoryCeilingsAsync(req.Market, scoredLines.Select(l => profiles[productByLine[l.LineId]].CategoryId).OfType<Guid>().ToList());
        foreach (var l in scoredLines)
        {
            var hp = profiles[productByLine[l.LineId]];
            if (l.IsSubstitutable == false || recoLabels.ContainsKey(l.PartnerSku)) continue;
            if (cfg.ExcludedCategories.Any(c => hp.CategoryCode.StartsWith(c, StringComparison.OrdinalIgnoreCase))) continue;
            if (hp.CategoryId is not { } cid || !ceilings.TryGetValue(cid, out var ceiling) || hp.Hsi >= ceiling) continue;
            targets.Add((l, hp));
        }

        // 5. Candidate alternatives per target line from the precomputed graph.
        var edges = await cache.GetEdgesAsync(req.Market, graph, targets.Select(t => t.Hp.ProductId).ToList());
        var altProfiles = await cache.GetProfilesAsync(req.Market, edges.Values.SelectMany(e => e.Select(x => x.To)).ToList());
        var maxAlternatives = Math.Clamp(cfg.MaxAlternativesPerLine, 1, options.MaxAlternativesCap);
        var work = new List<LineWork>();
        foreach (var (line, hp) in targets)
        {
            // Soft budget: always serve at least one line, then stop computing further lines once over budget.
            if (work.Count > 0 && sw.ElapsedMilliseconds > options.InternalBudgetMs) { degradations.Add("TIMEOUT_BUDGET"); break; }
            if (!edges.TryGetValue(hp.ProductId, out var lineEdges)) continue;
            var unitPrice = Currency.TryParse(line.UnitPrice.Amount, out var up) ? up - (line.LineDiscount is { } d && Currency.TryParse(d.Amount, out var dv) ? dv / Math.Max(1m, line.Quantity) : 0m) : (decimal?)null;
            var alts = await CandidatesAsync(ctx.PartnerId, cfg, pack, req.Market, hp, unitPrice, req.StoreId, req.AvailabilityMode ?? cfg.AvailabilityMode, shopper,
                placement, req.UserRef ?? "cart:" + req.CartRef, lineEdges, altProfiles, degradations, maxAlternatives, evaluationId, ct);
            if (alts.Count > 0) work.Add(new LineWork(line, hp, unitPrice, alts));
        }

        // 6. Cart-level selection: at most M lines, one per category, maximizing health gain x quantity.
        var maxSuggestions = Math.Min(req.Context?.MaxSuggestions ?? cfg.MaxSuggestionsPerCart, cfg.MaxSuggestionsPerCart);
        var selected = CartSelection.Select(work.Select(w => new CartCandidate(w.Line.LineId, w.Original.CategoryCode, w.Alternatives[0].HealthDelta, w.Line.Quantity,
            1m + 0.25m * w.Original.Flags.Count(f => f.StartsWith("HIGH_", StringComparison.Ordinal)), w.Alternatives[0].PriceRatio)), maxSuggestions).ToHashSet();

        // 7. Explanations, labels and suggestion snapshots.
        var suggestions = new List<LineSuggestion>();
        var snapshots = new List<SuggestionSnapshot>();
        var training = new List<object>();
        foreach (var w in work.Where(w => selected.Contains(w.Line.LineId)).OrderBy(w => req.Lines.FindIndex(l => l.LineId == w.Line.LineId)))
        {
            var suggestionId = Ids.NewId();
            var alternatives = new List<Alternative>();
            for (var i = 0; i < w.Alternatives.Count; i++)
                alternatives.Add(await AlternativeAsync(pack, req.Market, locale, w.Original, w.Alternatives[i], i + 1, w.UnitPrice, req.Currency, w.Line.Quantity, label));
            suggestions.Add(new LineSuggestion
            {
                SuggestionId = suggestionId, LineId = w.Line.LineId, Original = ApiMapper.Product(w.Original, w.Line.PartnerSku, w.Line.Title),
                Alternatives = alternatives, ExpiresAt = now.AddMinutes(options.SuggestionTtlMinutes),
            });
            var snapshot = Snapshot(suggestionId, ctx.PartnerId, evaluationId, req.CartRef, req.UserRef, req.Market, req.StoreId, placement, w.Line.LineId,
                w.Line.PartnerSku, w.Original, w.Line.Quantity, w.UnitPrice, w.Line.TaxRate, req.Currency, w.Alternatives, recoFee, pack, graph, now, cfg);
            snapshots.Add(snapshot);
            training.Add(new
            {
                suggestionId, lineId = w.Line.LineId, placement, fromProductId = w.Original.ProductId,
                alternatives = w.Alternatives.Select((a, i) => new { rank = i + 1, productId = a.Alt.ProductId, partnerSku = a.Sku, features = a.Features, score = a.Score }),
            });
            await cache.SetSuggestionAsync(snapshot, snapshot.AttributionExpiresAt - now + TimeSpan.FromHours(1));
        }
        log.LogDebug("Evaluation {Id}: {Targets} targets, {Suggestions} suggestions in {Ms} ms", evaluationId, targets.Count, suggestions.Count, sw.ElapsedMilliseconds);
        return await RespondAsync(suggestions, snapshots, training);
    }

    public async Task<CartEvaluation> GetAsync(Guid partnerId, Guid evaluationId)
    {
        var json = await cache.GetEvaluationJsonAsync(partnerId, evaluationId) ?? throw HsException.NotFound("Cart evaluation " + evaluationId);
        return JsonSerializer.Deserialize<CartEvaluation>(json, HsJson.Options)!;
    }

    /// <summary>Product-page and search alternatives (placement PDP or SEARCH). Mints a suggestion when a cart is known (RA-1).</summary>
    public async Task<AlternativesResponse> AlternativesAsync(PartnerContext ctx, Guid productId, string market, string? storeId, string? cartRef, string? userRef,
        string? placement, string? currency, string? locale, int limit, CancellationToken ct)
    {
        var partner = await partners.GetAsync(ctx.PartnerId, ct);
        var cfg = partner.Config;
        cartRef ??= ctx.SdkCartRef;
        userRef ??= ctx.SdkUserRef;
        storeId ??= ctx.SdkStoreId;
        Authorize(ctx, partner, market, cartRef, userRef, storeId);
        currency ??= cfg.Currency;
        if (currency != cfg.Currency) throw HsException.Unprocessable("HS-CURRENCY-MISMATCH", $"Currency must be {cfg.Currency} for this partner");
        placement ??= "PDP";
        var pack = await refs.GetPackAsync(market, ct);
        var original = await cache.GetProfileAsync(market, productId) ?? throw HsException.NotFound("Product " + productId);
        var loc = ApiMapper.ResolveLocale(market, locale);
        var recoFee = cartRef is null ? null : RecoAddFeeRule.Snapshot(cfg.Fee.RecommendationAdd, currency);
        var label = ApiMapper.Label(ShopperCopy.Label(loc, partner.DisplayName, recoFee is not null));
        var disclosures = Disclosures(pack, loc, partner, recoFee is not null, [], new Dictionary<Guid, HealthProfileData>());
        var graph = await cache.GetActiveGraphAsync(market);
        if (graph is null || cfg.KillSwitch || !cfg.Placements.Contains(placement) || cfg.DisabledPlacements.Contains(placement))
            return new AlternativesResponse { ProductId = productId, Market = market, StoreId = storeId, Disclosures = disclosures };

        var originalSkus = await cache.GetPartnerSkusAsync(ctx.PartnerId, productId);
        decimal? originalPrice = null;
        if (storeId is not null && originalSkus.Length > 0)
            originalPrice = (await cache.GetInventoryAsync(ctx.PartnerId, storeId, originalSkus)).Values.Where(i => i.InStock).Select(i => (decimal?)i.Price).Min();
        var edges = await cache.GetEdgesAsync(market, graph, [productId]);
        if (!edges.TryGetValue(productId, out var list))
            return new AlternativesResponse { ProductId = productId, Market = market, StoreId = storeId, Disclosures = disclosures };
        var altProfiles = await cache.GetProfilesAsync(market, list.Select(e => e.To).ToList());
        var shopper = await ShopperAsync(ctx.PartnerId, cfg, userRef, null, null, ct);
        var degradations = new SortedSet<string>();
        var evaluationKey = Ids.NewId();
        var alts = await CandidatesAsync(ctx.PartnerId, cfg, pack, market, original, originalPrice, storeId, cfg.AvailabilityMode, shopper, placement,
            userRef ?? "cart:" + (cartRef ?? "anonymous"), list, altProfiles, degradations, Math.Clamp(limit, 1, 10), evaluationKey, ct);

        var now = clock.UtcNow;
        Guid? suggestionId = cartRef is null || alts.Count == 0 ? null : Ids.NewId();
        var alternatives = new List<Alternative>();
        for (var i = 0; i < alts.Count; i++)
            alternatives.Add(await AlternativeAsync(pack, market, loc, original, alts[i], i + 1, originalPrice, currency, 1m, label));
        DateTime? expires = null;
        if (suggestionId is { } sid)
        {
            var snapshot = Snapshot(sid, ctx.PartnerId, null, cartRef!, userRef, market, storeId, placement, null, originalSkus.FirstOrDefault() ?? "PRODUCT:" + productId,
                original, 1m, originalPrice, null, currency, alts, recoFee, pack, graph, now, cfg);
            await cache.SetSuggestionAsync(snapshot, snapshot.AttributionExpiresAt - now + TimeSpan.FromHours(1));
            expires = snapshot.ExpiresAt;
            var versions = new ModelVersions($"{pack.PrimaryModel}@{pack.Primary.Version}+HSI@{pack.Hsi.Version}", pack.Rules.Version, graph, models.RankerVersion);
            recorder.Enqueue(new EvaluationRecord(ctx.PartnerId, null, cartRef!, userRef, storeId, market, placement, 1, degradations.Count > 0, 0, cfg.Experiment.Name, versions,
                [snapshot], [new { suggestionId = sid, placement, fromProductId = productId, alternatives = alts.Select((a, i) => new { rank = i + 1, productId = a.Alt.ProductId, partnerSku = a.Sku, features = a.Features, score = a.Score }) }], now));
        }
        return new AlternativesResponse
        {
            ProductId = productId, Market = market, StoreId = storeId, SuggestionId = suggestionId, ExpiresAt = expires, Alternatives = alternatives,
            Disclosures = Disclosures(pack, loc, partner, recoFee is not null, alts.Select(a => a.Alt.ProductId), altProfiles),
        };
    }

    private static void Authorize(PartnerContext ctx, PartnerInfo partner, string market, string? cartRef, string? userRef, string? storeId)
    {
        if (!partner.Markets.Contains(market)) throw HsException.Unprocessable("HS-UNSUPPORTED-MARKET", $"Market {market} is not enabled for this partner");
        if (partner.Status is "SUSPENDED" or "OFFBOARDED") throw HsException.Forbidden("The partner account is not active");
        if (!ctx.IsSdkSession) return;
        if (ctx.SdkMarket is not null && ctx.SdkMarket != market) throw HsException.Forbidden("The SDK session is bound to another market");
        if (cartRef is not null && ctx.SdkCartRef != cartRef) throw HsException.Forbidden("The SDK session is bound to another cart");
        if (ctx.SdkUserRef is not null && userRef is not null && ctx.SdkUserRef != userRef) throw HsException.Forbidden("The SDK session is bound to another user");
        if (ctx.SdkStoreId is not null && storeId is not null && ctx.SdkStoreId != storeId) throw HsException.Forbidden("The SDK session is bound to another store");
    }

    private static bool InHoldout(PartnerConfig cfg, string key)
    {
        if (cfg.Experiment.HoldoutPercent <= 0) return false;
        var hash = System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(cfg.Experiment.Name + ":" + key));
        return BitConverter.ToUInt32(hash, 0) % 100 < cfg.Experiment.HoldoutPercent;
    }

    private async Task<Shopper> ShopperAsync(Guid partnerId, PartnerConfig cfg, string? userRef, UserPreferences? inline, bool? contextMinor, CancellationToken ct)
    {
        var stored = userRef is null ? null : await prefs.LoadForEvaluationAsync(partnerId, userRef, ct);
        var healthConsent = inline?.HealthDataConsent ?? stored?.HealthDataConsent ?? false;
        var isMinor = contextMinor ?? inline?.IsMinor ?? stored?.IsMinor;
        var treatAsMinor = isMinor == true || (isMinor is null && cfg.MinorsPolicy == "STRICT");
        var personalize = !treatAsMinor && (inline?.PersonalizationConsent ?? stored?.PersonalizationConsent ?? false);
        var constraints = new ShopperConstraints
        {
            DietType = inline?.DietType ?? stored?.DietType ?? "ANY",
            Allergens = healthConsent ? (inline?.Allergens ?? stored?.Allergens ?? []) : [],
            StrictMayContain = inline?.StrictMayContain ?? stored?.StrictMayContain ?? true,
            AvoidIngredients = inline?.AvoidIngredients ?? stored?.AvoidIngredients ?? [],
            ExcludedBrands = inline?.ExcludedBrands ?? stored?.ExcludedBrands ?? [],
            ReligiousPreferences = inline?.ReligiousPreferences ?? stored?.ReligiousPreferences ?? [],
            TreatAsMinor = treatAsMinor,
        };
        var goals = healthConsent && personalize ? (IReadOnlyCollection<string>)(inline?.Goals ?? stored?.Goals ?? []) : [];
        return new Shopper(constraints, goals, personalize, inline?.Locale ?? stored?.Locale);
    }

    private async Task<List<Scored>> CandidatesAsync(Guid partnerId, PartnerConfig cfg, MarketPack pack, string market, HealthProfileData original, decimal? originalPrice,
        string? storeId, string availabilityMode, Shopper shopper, string placement, string fatigueKey, SwapEdge[] edges, Dictionary<Guid, HealthProfileData> altProfiles,
        SortedSet<string> degradations, int max, Guid evaluationId, CancellationToken ct)
    {
        var guards = new CommercialGuards
        {
            MaxNormalizedPriceRatio = cfg.PriceGuards.MaxNormalizedPriceRatio, MaxAbsPremiumPerUnit = cfg.PriceGuards.MaxAbsPremiumPerUnit,
            PackRatioMin = cfg.PackSizeRatio.Min, PackRatioMax = cfg.PackSizeRatio.Max, BlockedBrands = cfg.BlockedBrands, ExcludedCategories = cfg.ExcludedCategories,
        };
        var dismissed = await cache.GetDismissedAsync(partnerId, fatigueKey, edges.Select(e => (original.ProductId, e.To)).ToList());
        var localHour = LocalHour(market, clock.UtcNow);
        var pending = new List<(HealthProfileData Alt, SwapEdge Edge, decimal Delta, string[] Skus)>();
        foreach (var e in edges)
        {
            ct.ThrowIfCancellationRequested();
            if (!altProfiles.TryGetValue(e.To, out var alt) || !alt.Eligible) continue;
            if (dismissed.Contains((original.ProductId, e.To))) continue; // R8 fatigue
            var delta = e.HealthDelta;
            if (e.FromVersion != original.VersionId || e.ToVersion != alt.VersionId)
            {
                // A profile changed after the graph build: re-check the static rules before serving the edge.
                var recheck = StaticRules.Check(original, alt, e.Substitutability, true, pack);
                if (!recheck.Eligible) continue;
                delta = recheck.HealthDelta;
            }
            var skus = await cache.GetPartnerSkusAsync(partnerId, alt.ProductId);
            if (skus.Length > 0) pending.Add((alt, e, delta, skus));
        }
        var inventory = storeId is null || pending.Count == 0
            ? new Dictionary<string, InventoryItem>()
            : await cache.GetInventoryAsync(partnerId, storeId, pending.SelectMany(p => p.Skus).Distinct().ToList());
        var staleBefore = clock.UtcNow.AddSeconds(-cfg.InventoryStalenessSeconds);

        var scored = new List<Scored>();
        foreach (var (alt, edge, delta, skus) in pending)
        {
            string? sku = null;
            InventoryItem? inv = null;
            var requiresCheck = availabilityMode == "PARTNER_FILTER" || storeId is null;
            foreach (var s in skus.Order(StringComparer.Ordinal))
            {
                if (inventory.TryGetValue(s, out var i))
                {
                    if (!i.InStock) continue;
                    if (inv is null || i.Price < inv.Price) { sku = s; inv = i; }
                }
                else if (sku is null && requiresCheck) sku = s;
            }
            if (sku is null && availabilityMode == "MIRROR" && storeId is not null) continue; // not in stock at this store
            if (inv is not null && inv.ObservedAt < staleBefore) { requiresCheck = true; degradations.Add("INVENTORY_STALE"); }
            sku ??= skus.Order(StringComparer.Ordinal).First();

            var price = new PriceContext(originalPrice, inv?.Price);
            if (RequestRules.Reject(original, alt, shopper.Constraints, guards, price, pack.Rules.VerifiedAllergenConfidence) is not null) continue;

            var priceRatio = originalPrice is { } po && inv is not null && po > 0 ? RequestRules.NormalizedPriceRatio(po, original.NetQuantity, inv.Price, alt.NetQuantity) : (decimal?)null;
            var packRatio = original.NetQuantity is { } so && alt.NetQuantity is { } sa && so.IsComparableTo(sa) && so.Value > 0 ? sa.Value / so.Value : (decimal?)null;
            var goalMatch = shopper.Personalize ? Ranking.GoalMatch(shopper.Goals, edge.Reasons) : 0m;
            var features = RankerFeatures.Vector(new RankerFeatures.Input(delta, edge.Substitutability, priceRatio, !string.Equals(alt.Brand, original.Brand, StringComparison.OrdinalIgnoreCase),
                packRatio, placement, localHour, goalMatch, alt.CategoryCode.StartsWith("beverages", StringComparison.Ordinal), (original.Nova ?? 0) - (alt.Nova ?? original.Nova ?? 0),
                string.Equals(alt.Brand, original.Brand, StringComparison.OrdinalIgnoreCase)));
            scored.Add(new Scored(alt, sku, inv, delta, edge.Substitutability, priceRatio is null ? null : Math.Round(priceRatio.Value, 2), 0, requiresCheck, edge.Reasons, features));
        }
        if (scored.Count == 0) return scored;

        var pAccept = models.PredictAcceptance(scored.Select(s => s.Features).ToList());
        var ranked = scored.Select((s, i) => s with { Score = Ranking.Score(Weights, s.HealthDelta, s.Substitutability, pAccept?[i], s.PriceRatio, 0m, Ranking.GoalMatch(shopper.Goals, s.Reasons)) })
            .OrderByDescending(s => s.Score).ThenByDescending(s => s.HealthDelta).ThenBy(s => s.Sku, StringComparer.Ordinal).Take(max).ToList();
        if (ranked.Count >= 2 && cfg.ExplorationRate > 0 && Ranking.IsExploration(evaluationId, cfg.ExplorationRate)) (ranked[0], ranked[1]) = (ranked[1], ranked[0]);
        return ranked;
    }

    private async Task<Alternative> AlternativeAsync(MarketPack pack, string market, string locale, HealthProfileData original, Scored a, int rank, decimal? originalPrice,
        string currency, decimal quantity, Contracts.RecommendationLabel label)
    {
        var sheet = FactSheets.Build(original, a.Alt, pack);
        var selected = FactSheets.Select(sheet);
        var cached = await cache.GetExplanationAsync(market, locale, original.ProductId, a.Alt.ProductId, sheet.Hash);
        var rendered = cached is not null
            ? new RenderedExplanation(cached.Short, cached.Bullets, selected.Select(f => f.Id).ToList(), cached.Generator, cached.AiGenerated, locale)
            : ExplanationTemplates.Render(selected.Count > 0 ? selected : sheet.Facts.Take(1).ToList(), locale);
        var explanation = new Explanation
        {
            Short = rendered.Short.Length > 0 ? rendered.Short : ShopperCopy.LabelText(locale), Bullets = rendered.Bullets.Count > 0 ? rendered.Bullets.Take(3).ToList() : null,
            Details = ApiMapper.Details(sheet.Facts), Generator = rendered.Generator, AiGenerated = rendered.AiGenerated, Locale = locale,
            MethodologyUrl = pack.MethodologyUrl, Disclaimer = ShopperCopy.LabelDisclaimer(locale, a.Alt.Nutrition.IsLiquid),
        };
        return new Alternative
        {
            Rank = rank, Product = ApiMapper.Product(a.Alt, a.Sku), Recommendation = label,
            UnitPrice = a.Inventory is { } inv ? ApiMapper.Money(inv.Price, currency) : null,
            UnitPriceDelta = a.Inventory is { } i2 && originalPrice is { } po ? ApiMapper.Money(i2.Price - po, currency) : null,
            NormalizedPriceRatio = a.PriceRatio, HealthDelta = a.HealthDelta, ReasonCodes = a.Reasons.ToList(), Explanation = explanation, Sponsored = false,
            RequiresAvailabilityCheck = a.RequiresCheck, SuggestedQuantity = quantity,
        };
    }

    private SuggestionSnapshot Snapshot(Guid suggestionId, Guid partnerId, Guid? evaluationId, string cartRef, string? userRef, string market, string? storeId,
        string placement, string? lineId, string fromSku, HealthProfileData original, decimal qty, decimal? unitPrice, decimal? taxRate, string currency,
        IReadOnlyList<Scored> alts, RecoFeeSnapshot? recoFee, MarketPack pack, string graph, DateTime now, PartnerConfig cfg) => new()
    {
        SuggestionId = suggestionId, PartnerId = partnerId, EvaluationId = evaluationId, CartRef = cartRef, UserRef = userRef, Market = market, StoreId = storeId,
        Placement = placement, LineId = lineId, FromPartnerSku = fromSku, FromProductId = original.ProductId, FromVersionId = original.VersionId, FromQty = qty,
        FromUnitPrice = unitPrice, FromNetQuantity = original.NetQuantity?.Value, FromNetUnit = original.NetQuantity?.Unit, FromTaxRate = taxRate, Currency = currency,
        Alternatives = alts.Select((a, i) => new AlternativeSnapshot(i + 1, a.Sku, a.Alt.ProductId, a.Alt.VersionId, a.Inventory?.Price, a.Alt.NetQuantity?.Value,
            a.Alt.NetQuantity?.Unit, a.HealthDelta, false, a.Features)).ToList(),
        RecoFee = recoFee, RulesVersion = pack.Rules.Version, GraphVersion = graph, CreatedAt = now, ExpiresAt = now.AddMinutes(options.SuggestionTtlMinutes),
        AttributionExpiresAt = now.AddHours(cfg.AttributionWindowHours),
    };

    private static Contracts.Disclosures Disclosures(MarketPack pack, string locale, PartnerInfo partner, bool addFee, IEnumerable<Guid> shown, IReadOnlyDictionary<Guid, HealthProfileData> profiles) => new()
    {
        FeeDisclosure = ShopperCopy.FeeDisclosure(locale, partner.DisplayName, addFee), LabelDisclaimer = ShopperCopy.GenericDisclaimer(locale),
        MethodologyUrl = pack.MethodologyUrl,
        Attributions = ApiMapper.Attributions(shown.SelectMany(id => profiles.TryGetValue(id, out var hp) ? hp.Sources : [])) is { Count: > 0 } a ? a : null,
    };

    private static CartHealthSummary Summary(CartEvaluationRequest req, Dictionary<string, Guid> productByLine, Dictionary<Guid, HealthProfileData> profiles,
        List<CartLine> scored, int withSuggestions)
    {
        var weighted = scored.Select(l => (Hsi: profiles[productByLine[l.LineId]].Hsi!.Value, Qty: l.Quantity)).ToList();
        var qty = weighted.Sum(w => w.Qty);
        var flags = scored.SelectMany(l => profiles[productByLine[l.LineId]].Flags).GroupBy(f => f).ToDictionary(g => g.Key, g => g.Count());
        return new CartHealthSummary
        {
            LinesTotal = req.Lines.Count, LinesResolved = productByLine.Count, LinesScored = scored.Count, LinesWithSuggestions = withSuggestions,
            CartHsi = qty > 0 ? Math.Round(weighted.Sum(w => w.Hsi * w.Qty) / qty, 1) : null, FlagCounts = flags.Count > 0 ? flags : null,
        };
    }

    private static int LocalHour(string market, DateTime utc)
    {
        try
        {
            var tz = TimeZoneInfo.FindSystemTimeZoneById(market switch { "IN" => "Asia/Kolkata", "US" => "America/New_York", _ => "UTC" });
            return TimeZoneInfo.ConvertTimeFromUtc(utc, tz).Hour;
        }
        catch (TimeZoneNotFoundException) { return utc.Hour; }
    }
}

/// <summary>
/// Asynchronous persistence of evaluations and suggestions (HLD 7.1: the hot path never waits for PostgreSQL). Writes
/// txn.cart_evaluation and txn.suggestion and emits hs.cart-evaluations.v1 (ranker training data with features and positions).
/// </summary>
public sealed class EvaluationRecorder(Db db, ILogger<EvaluationRecorder> log) : BackgroundService
{
    private readonly Channel<EvaluationRecord> _channel = Channel.CreateBounded<EvaluationRecord>(new BoundedChannelOptions(50_000) { FullMode = BoundedChannelFullMode.DropOldest });
    private long _enqueued;
    private long _written;

    public void Enqueue(EvaluationRecord r)
    {
        if (_channel.Writer.TryWrite(r)) Interlocked.Increment(ref _enqueued);
    }

    /// <summary>Waits until everything enqueued so far is persisted (tests, CLI, finalizer safety).</summary>
    public async Task FlushAsync(TimeSpan timeout)
    {
        var target = Interlocked.Read(ref _enqueued);
        var deadline = DateTime.UtcNow + timeout;
        while (Interlocked.Read(ref _written) < target && DateTime.UtcNow < deadline) await Task.Delay(20);
    }

    public async Task WriteAsync(EvaluationRecord r, CancellationToken ct)
    {
        await db.InTxAsync(DbRole.App, r.PartnerId, async u =>
        {
            if (r.EvaluationId is { } evaluationId)
                await u.ExecuteAsync(
                    """
                    insert into txn.cart_evaluation (evaluation_id, created_at, partner_id, cart_ref, user_ref, store_id, market, placement, line_count,
                        suggestion_count, degraded, latency_ms, experiment_arm, model_versions)
                    values (@evaluationId, @at, @partnerId, @cartRef, @userRef, @storeId, @market, @placement, @lines, @suggestions, @degraded, @latency, @arm, @versions::jsonb)
                    """,
                    new
                    {
                        evaluationId, at = r.CreatedAt, partnerId = r.PartnerId, cartRef = r.CartRef, userRef = r.UserRef, storeId = r.StoreId, market = r.Market,
                        placement = r.Placement, lines = (short)Math.Min(r.LineCount, short.MaxValue), suggestions = (short)r.Suggestions.Count, degraded = r.Degraded,
                        latency = r.LatencyMs, arm = r.ExperimentArm, versions = JsonSerializer.Serialize(r.Versions, HsJson.Options),
                    }, ct);
            foreach (var s in r.Suggestions)
                await u.ExecuteAsync(
                    """
                    insert into txn.suggestion (suggestion_id, created_at, evaluation_id, partner_id, cart_ref, user_ref, market, store_id, placement, line_id,
                        from_partner_sku, from_product_id, from_version_id, from_qty, from_unit_price, from_net_quantity, from_net_unit, from_tax_rate, currency,
                        alternatives, reco_fee, rules_version, graph_version, expires_at, attribution_expires_at)
                    values (@SuggestionId, @CreatedAt, @EvaluationId, @PartnerId, @CartRef, @UserRef, @Market, @StoreId, @Placement, @LineId, @FromPartnerSku,
                        @FromProductId, @FromVersionId, @FromQty, @FromUnitPrice, @FromNetQuantity, @FromNetUnit, @FromTaxRate, @Currency, @alternatives::jsonb,
                        @recoFee::jsonb, @RulesVersion, @GraphVersion, @ExpiresAt, @AttributionExpiresAt)
                    """,
                    new
                    {
                        s.SuggestionId, s.CreatedAt, s.EvaluationId, s.PartnerId, s.CartRef, s.UserRef, s.Market, s.StoreId, s.Placement, s.LineId, s.FromPartnerSku,
                        s.FromProductId, s.FromVersionId, s.FromQty, s.FromUnitPrice, s.FromNetQuantity, s.FromNetUnit, s.FromTaxRate, s.Currency,
                        alternatives = JsonSerializer.Serialize(s.Alternatives, HotCache.Json),
                        recoFee = s.RecoFee is null ? null : JsonSerializer.Serialize(s.RecoFee, HotCache.Json), s.RulesVersion, s.GraphVersion, s.ExpiresAt, s.AttributionExpiresAt,
                    }, ct);
            await Outbox.EnqueueAsync(u, r.PartnerId, Topics.CartEvaluations, r.EvaluationId?.ToString() ?? r.Suggestions.FirstOrDefault()?.SuggestionId.ToString() ?? "pdp", new
            {
                r.EvaluationId, r.Market, r.Placement, r.StoreId, r.Degraded, r.LatencyMs, r.ExperimentArm, r.Versions, r.CreatedAt, rows = r.TrainingRows,
            }, ct);
        }, ct);
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await foreach (var r in _channel.Reader.ReadAllAsync(stoppingToken))
        {
            try { await WriteAsync(r, stoppingToken); }
            catch (Exception ex) when (ex is not OperationCanceledException) { log.LogError(ex, "Failed to record evaluation {Id}", r.EvaluationId); }
            finally { Interlocked.Increment(ref _written); }
        }
    }
}
