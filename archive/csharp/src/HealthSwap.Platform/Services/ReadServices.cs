using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Domain.Scoring;
using HealthSwap.Platform.Cache;
using HealthSwap.Platform.Data;
using HealthSwap.Platform.Knowledge;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace HealthSwap.Platform.Services;

/// <summary>Product health profiles and lookups (hot cache only).</summary>
public sealed class ProductService(HotCache cache, PartnerDirectory partners)
{
    public async Task<HealthProfile> GetAsync(PartnerContext ctx, Guid productId, string market, string? locale, CancellationToken ct)
    {
        await EnsureMarketAsync(ctx, market, ct);
        var hp = await cache.GetProfileAsync(market, productId) ?? throw HsException.NotFound("Product " + productId);
        return ApiMapper.Profile(hp, locale);
    }

    public async Task<HealthProfile> LookupAsync(PartnerContext ctx, string market, string? gtin, string? partnerSku, string? locale, CancellationToken ct)
    {
        await EnsureMarketAsync(ctx, market, ct);
        Guid? productId = null;
        if (partnerSku is not null) productId = (await cache.GetSkuMapsAsync(ctx.PartnerId, [partnerSku]))[0]?.ProductId;
        if (productId is null && gtin is not null)
        {
            if (!Gtin.TryNormalize(gtin, out var g14)) throw HsException.Invalid("Invalid GTIN", [new FieldError("/gtin", "Invalid check digit or length", "HS-INVALID-GTIN")]);
            productId = await cache.GetProductByGtinAsync(g14);
        }
        if (gtin is null && partnerSku is null) throw HsException.Invalid("Provide gtin or partnerSku");
        var hp = productId is { } id ? await cache.GetProfileAsync(market, id) : null;
        return hp is null ? throw HsException.NotFound("Product") : ApiMapper.Profile(hp, locale);
    }

    private async Task EnsureMarketAsync(PartnerContext ctx, string market, CancellationToken ct)
    {
        var partner = await partners.GetAsync(ctx.PartnerId, ct);
        if (!partner.Markets.Contains(market)) throw HsException.Unprocessable("HS-UNSUPPORTED-MARKET", $"Market {market} is not enabled for this partner");
        if (ctx.IsSdkSession && ctx.SdkMarket is not null && ctx.SdkMarket != market) throw HsException.Forbidden("The SDK session is bound to another market");
    }
}

/// <summary>Public methodology transparency (GET /scoring-models) and service status (GET /status).</summary>
public sealed class TransparencyService(Db db, ReferenceDataRepository refs, HotCache cache, IClock clock)
{
    private sealed class ModelRow { public string Code { get; set; } = ""; public string Version { get; set; } = ""; public string Status { get; set; } = ""; public string? MethodologyUrl { get; set; } public DateTime? PublishedAt { get; set; } }

    private static readonly Dictionary<string, string> Descriptions = new()
    {
        ["IN_INR_DRAFT"] = "FSSAI Indian Nutrition Rating (draft) - threshold-points model, ILLUSTRATIVE parameters",
        ["NUTRISCORE_2023"] = "Nutri-Score 2023 algorithm (foods, beverages, water), ILLUSTRATIVE parameters",
        ["HSI"] = "HealthSwap Index: 70% nutrient profile, 15% processing (NOVA), 15% additives of concern",
        ["NOVA"] = "NOVA processing classification derived from ingredient markers",
        ["ADDITIVE_TIERS"] = "Additive concern tiers from regulators and authoritative bodies only",
    };

    public async Task<ScoringModelList> ModelsAsync(string? market, CancellationToken ct)
    {
        var markets = market is null ? await refs.MarketsAsync(ct) : [market];
        var items = new List<ScoringModel>();
        foreach (var m in markets)
        {
            MarketPack pack;
            try { pack = await refs.GetPackAsync(m, ct); }
            catch (HsException) { continue; }
            var keys = pack.PointsModels.Values.Select(p => p.Code + "@" + p.Version).Concat([$"HSI@{pack.Hsi.Version}", "NOVA@1.0", "ADDITIVE_TIERS@1.0"]).ToArray();
            var rows = await db.InTxAsync(DbRole.ReadOnly, null, u => u.QueryAsync<ModelRow>(
                "select code, version, status, methodology_url, published_at from rec.scoring_model where code || '@' || version = any(@keys)", new { keys }, ct), ct);
            items.AddRange(rows.Select(r => new ScoringModel
            {
                Code = r.Code, Version = r.Version, Market = m, Status = r.Status, Description = Descriptions.GetValueOrDefault(r.Code),
                DocumentationUrl = r.MethodologyUrl ?? pack.MethodologyUrl, EffectiveFrom = r.PublishedAt?.ToString("yyyy-MM-dd"),
            }));
        }
        return new ScoringModelList(items.OrderBy(i => i.Market).ThenBy(i => i.Code).ToList());
    }

    public async Task<ServiceStatus> StatusAsync(CancellationToken ct)
    {
        var markets = await refs.MarketsAsync(ct);
        var regions = new List<RegionStatus>();
        foreach (var m in markets) regions.Add(new RegionStatus(m, await cache.GetActiveGraphAsync(m) is null ? "DEGRADED" : "OPERATIONAL"));
        var status = regions.Count == 0 || regions.All(r => r.Status == "DEGRADED") ? "DEGRADED" : regions.Any(r => r.Status != "OPERATIONAL") ? "PARTIAL_OUTAGE" : "OPERATIONAL";
        return new ServiceStatus(status, regions, clock.UtcNow);
    }
}

/// <summary>
/// Rebuilds the hot cache from PostgreSQL (the source of truth): profiles, GTIN map, category ceilings and maps, the active swap
/// graph, explanations, partner catalog maps, inventory and preferences. Runs at startup and whenever the knowledge plane changes.
/// </summary>
public sealed class CacheWarmer(Db db, HotCache cache, ReferenceDataRepository refs, PartnerDirectory partners, ILogger<CacheWarmer> log) : BackgroundService
{
    private sealed class EdgeRow { public string GraphVersion { get; set; } = ""; public Guid FromProductId { get; set; } public Guid ToProductId { get; set; } public decimal HealthDelta { get; set; } public decimal Substitutability { get; set; } public string[] ReasonCodes { get; set; } = []; public string Features { get; set; } = "{}"; }
    private sealed class EdgeFeatures { public long FromVersion { get; set; } public long ToVersion { get; set; } public string ToCategory { get; set; } = ""; public string[] IntroducedTierC { get; set; } = []; }
    private sealed class ExplRow { public string Market { get; set; } = ""; public string Locale { get; set; } = ""; public Guid FromProductId { get; set; } public Guid ToProductId { get; set; } public string FactsHash { get; set; } = ""; public string ShortText { get; set; } = ""; public string Details { get; set; } = "{}"; public string Generator { get; set; } = ""; public string? ModelRef { get; set; } }
    private sealed class CatRow { public string Market { get; set; } = ""; public Guid CategoryId { get; set; } public decimal? HsiP60 { get; set; } }
    private sealed class MapRow { public string Market { get; set; } = ""; public string FromCode { get; set; } = ""; public string ToCode { get; set; } = ""; }
    private sealed class SkuRow { public Guid PartnerId { get; set; } public string PartnerSku { get; set; } = ""; public Guid ProductId { get; set; } public decimal MatchConfidence { get; set; } public string MatchState { get; set; } = ""; }
    private sealed class InvRow { public Guid PartnerId { get; set; } public string StoreId { get; set; } = ""; public string PartnerSku { get; set; } = ""; public bool InStock { get; set; } public int? AvailableQty { get; set; } public decimal Price { get; set; } public decimal? Mrp { get; set; } public DateTime ObservedAt { get; set; } }
    private sealed class PrefRow { public Guid PartnerId { get; set; } public string UserRef { get; set; } = ""; public string DietType { get; set; } = "ANY"; public string[] Allergens { get; set; } = []; public bool StrictMayContain { get; set; } public string[] Goals { get; set; } = []; public string[] AvoidIngredients { get; set; } = []; public string[] ExcludedBrands { get; set; } = []; public string[] ReligiousPrefs { get; set; } = []; public string? Locale { get; set; } public bool? IsMinor { get; set; } public bool HealthDataConsent { get; set; } public bool PersonalizationConsent { get; set; } public string? ConsentNoticeVersion { get; set; } public DateTime UpdatedAt { get; set; } }
    private sealed class LabelRow { public Guid PartnerId { get; set; } public string CartRef { get; set; } = ""; public string Sku { get; set; } = ""; public Guid SuggestionId { get; set; } public DateTime Until { get; set; } }

    private string? _knowledgeStamp;
    public DateTime? LastWarmAt { get; private set; }

    private const string StampSql =
        """
        select coalesce((select max(computed_at)::text from rec.health_profile), '') || '|' ||
               coalesce((select string_agg(graph_version, ',' order by graph_version) from rec.swap_graph_version where status = 'ACTIVE'), '') || '|' ||
               coalesce((select max(validated_at)::text from rec.explanation), '') || '|' ||
               coalesce((select max(updated_at)::text from tenant.catalog_item), '') || '|' ||
               coalesce((select count(*)::text from tenant.store_inventory), '')
        """;

    private Task<string?> StampAsync(CancellationToken ct) => db.InTxAsync(DbRole.Ops, null, u => u.ScalarAsync<string>(StampSql, ct: ct), ct);

    public async Task<Dictionary<string, int>> WarmAllAsync(CancellationToken ct)
    {
        var stamp = await StampAsync(ct);
        var counts = await WarmKnowledgeAsync(ct);
        foreach (var (k, v) in await WarmTenantsAsync(ct)) counts[k] = v;
        // Pre-load jurisdiction packs and tenant configuration so the first evaluation does not pay for them.
        refs.Invalidate();
        foreach (var market in await refs.MarketsAsync(ct)) await refs.GetPackAsync(market, ct);
        foreach (var p in await partners.ListAllAsync(ct)) { partners.Invalidate(p.PartnerId); await partners.GetAsync(p.PartnerId, ct); }
        _knowledgeStamp = stamp;
        LastWarmAt = DateTime.UtcNow;
        log.LogInformation("Hot cache warmed: {Counts}", string.Join(", ", counts.Select(kv => $"{kv.Key}={kv.Value}")));
        return counts;
    }

    public async Task<Dictionary<string, int>> WarmKnowledgeAsync(CancellationToken ct)
    {
        var (profiles, cats, maps, graphs, edges, expl) = await db.InTxAsync(DbRole.KbWriter, null, async u =>
        {
            var p = (await u.QueryAsync<string>("select profile_doc::text from rec.health_profile", ct: ct)).ToList();
            var c = (await u.QueryAsync<CatRow>("select market, category_id, hsi_p60 from rec.category_stats", ct: ct)).ToList();
            var m = (await u.QueryAsync<MapRow>(
                "select s.market, f.code as from_code, t.code as to_code from rec.category_substitution s join kb.category f on f.category_id = s.from_category_id join kb.category t on t.category_id = s.to_category_id",
                ct: ct)).ToList();
            var g = (await u.QueryAsync<(string Market, string GraphVersion)>("select market, graph_version from rec.swap_graph_version where status = 'ACTIVE'", ct: ct)).ToList();
            var e = (await u.QueryAsync<EdgeRow>(
                "select graph_version, from_product_id, to_product_id, health_delta, substitutability, reason_codes, features::text as features from rec.v_active_swap_edge", ct: ct)).ToList();
            var x = (await u.QueryAsync<ExplRow>(
                "select market, locale, from_product_id, to_product_id, encode(facts_hash, 'hex') as facts_hash, short_text, details::text as details, generator, model_ref from rec.explanation",
                ct: ct)).ToList();
            return (p, c, m, g, e, x);
        }, ct);

        foreach (var json in profiles)
        {
            var hp = JsonSerializer.Deserialize<HealthProfileData>(json, HotCache.Json)!;
            await cache.SetProfileAsync(hp);
            if (hp.Gtin is not null) await cache.SetGtinAsync(hp.Gtin, hp.ProductId);
        }
        foreach (var c in cats.Where(c => c.HsiP60 is not null)) await cache.SetCategoryCeilingAsync(c.Market, c.CategoryId, c.HsiP60!.Value);
        foreach (var g in maps.GroupBy(m => (m.Market, m.FromCode))) await cache.SetCategoryMapAsync(g.Key.Market, g.Key.FromCode, g.Select(x => x.ToCode).ToList());
        foreach (var (market, graph) in graphs)
        {
            foreach (var g in edges.Where(e => e.GraphVersion == graph).GroupBy(e => e.FromProductId))
                await cache.SetEdgesAsync(market, graph, g.Key, g.Select(e =>
                {
                    var f = JsonSerializer.Deserialize<EdgeFeatures>(e.Features, HotCache.Json) ?? new EdgeFeatures();
                    return new SwapEdge(e.ToProductId, e.HealthDelta, e.Substitutability, e.ReasonCodes, f.FromVersion, f.ToVersion, f.ToCategory, f.IntroducedTierC);
                }).OrderByDescending(e => e.HealthDelta * e.Substitutability).ToArray());
            await cache.SetActiveGraphAsync(market, graph);
        }
        foreach (var x in expl)
        {
            var details = JsonDocument.Parse(x.Details).RootElement;
            var bullets = details.TryGetProperty("bullets", out var b) ? b.EnumerateArray().Select(v => v.GetString() ?? "").ToList() : [];
            await cache.SetExplanationAsync(x.Market, x.Locale, x.FromProductId, x.ToProductId, x.FactsHash,
                new CachedExplanation(x.ShortText, bullets, x.Generator, x.Generator == "LLM_VALIDATED", x.ModelRef));
        }
        return new Dictionary<string, int> { ["profiles"] = profiles.Count, ["edges"] = edges.Count, ["explanations"] = expl.Count, ["graphs"] = graphs.Count };
    }

    public async Task<Dictionary<string, int>> WarmTenantsAsync(CancellationToken ct)
    {
        var (skus, inventory, prefs, labels) = await db.InTxAsync(DbRole.Ops, null, async u =>
        {
            var s = (await u.QueryAsync<SkuRow>(
                "select partner_id, partner_sku, product_id, coalesce(match_confidence, 0) as match_confidence, match_state from tenant.catalog_item where is_listed and product_id is not null and match_state in ('AUTO_MATCHED', 'CONFIRMED')",
                ct: ct)).ToList();
            var i = (await u.QueryAsync<InvRow>("select partner_id, store_id, partner_sku, in_stock, available_qty, price, mrp, observed_at from tenant.store_inventory", ct: ct)).ToList();
            var p = (await u.QueryAsync<PrefRow>(
                """
                select partner_id, user_ref, diet_type, allergens, strict_may_contain, goals, avoid_ingredients, excluded_brands, religious_prefs, locale, is_minor,
                       health_data_consent, personalization_consent, consent_notice_version, updated_at from tenant.user_preference
                """, ct: ct)).ToList();
            // Line labels survive restarts: recommended products added to carts within the attribution window and not undone.
            var l = (await u.QueryAsync<LabelRow>(
                """
                select s.partner_id, s.cart_ref, e.alternative_partner_sku as sku, s.suggestion_id, s.attribution_expires_at as until
                from txn.suggestion_event e join txn.suggestion s on s.suggestion_id = e.suggestion_id
                where e.event_type in ('ACCEPT', 'ADD_TO_CART') and s.attribution_expires_at > now()
                  and not exists (select 1 from txn.suggestion_event x where x.suggestion_id = e.suggestion_id and x.event_type = 'UNDO' and x.occurred_at > e.occurred_at
                                  and (x.alternative_partner_sku is null or x.alternative_partner_sku = e.alternative_partner_sku))
                """, ct: ct)).ToList();
            return (s, i, p, l);
        }, ct);
        foreach (var s in skus)
        {
            await cache.SetSkuMapAsync(s.PartnerId, s.PartnerSku, new SkuMapEntry(s.ProductId, s.MatchConfidence, s.MatchState));
            await cache.AddPartnerSkuAsync(s.PartnerId, s.ProductId, s.PartnerSku);
        }
        foreach (var g in inventory.GroupBy(i => (i.PartnerId, i.StoreId)))
            await cache.SetInventoryAsync(g.Key.PartnerId, g.Key.StoreId, g.Select(i => (i.PartnerSku, new InventoryItem(i.InStock, i.Price, i.Mrp, i.ObservedAt, i.AvailableQty))));
        foreach (var p in prefs)
            await cache.SetPrefsAsync(p.PartnerId, p.UserRef, new ShopperPrefsDoc
            {
                DietType = p.DietType, Allergens = p.Allergens.ToList(), StrictMayContain = p.StrictMayContain, Goals = p.Goals.ToList(), AvoidIngredients = p.AvoidIngredients.ToList(),
                ExcludedBrands = p.ExcludedBrands.ToList(), ReligiousPreferences = p.ReligiousPrefs.ToList(), Locale = p.Locale, IsMinor = p.IsMinor,
                HealthDataConsent = p.HealthDataConsent, PersonalizationConsent = p.PersonalizationConsent, ConsentNoticeVersion = p.ConsentNoticeVersion, UpdatedAt = p.UpdatedAt,
            });
        foreach (var l in labels) await cache.AddCartRecoLabelAsync(l.PartnerId, l.CartRef, l.Sku, l.SuggestionId, l.Until - DateTime.UtcNow);
        return new Dictionary<string, int> { ["skuMaps"] = skus.Count, ["inventory"] = inventory.Count, ["preferences"] = prefs.Count, ["lineLabels"] = labels.Count };
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                if (LastWarmAt is null) await WarmAllAsync(stoppingToken);
                else
                {
                    var stamp = await StampAsync(stoppingToken);
                    if (stamp != _knowledgeStamp) { log.LogInformation("Database changed outside this process; re-warming the hot cache"); await WarmAllAsync(stoppingToken); }
                }
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex) { log.LogWarning(ex, "Cache warm failed; retrying"); }
            try { await Task.Delay(TimeSpan.FromSeconds(15), stoppingToken); } catch (OperationCanceledException) { break; }
        }
    }
}
