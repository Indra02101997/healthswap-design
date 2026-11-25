using System.Globalization;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Domain.Knowledge;
using HealthSwap.Platform.Bus;
using HealthSwap.Platform.Cache;
using HealthSwap.Platform.Data;
using HealthSwap.Platform.Knowledge;
using HealthSwap.Platform.Ml;
using Microsoft.AspNetCore.DataProtection;
using Microsoft.Extensions.Logging;

namespace HealthSwap.Platform.Services;

/// <summary>Partner catalog sync and matching to canonical products (HLD 6.2: GTIN exact, GTIN normalized, fuzzy, enrichment).</summary>
public sealed class CatalogService(Db db, HotCache cache, PartnerDirectory partners, ModelRegistry models, IClock clock, ILogger<CatalogService> log)
{
    private sealed class FuzzyRow { public Guid ProductId { get; set; } public string Name { get; set; } = ""; public string? Brand { get; set; } public decimal? NetQuantity { get; set; } public string? NetQuantityUnit { get; set; } public string? PrimaryGtin { get; set; } public float Sim { get; set; } }
    private sealed class ItemRow
    {
        public string PartnerSku { get; set; } = "";
        public Guid? ProductId { get; set; }
        public string? MatchMethod { get; set; }
        public decimal? MatchConfidence { get; set; }
        public string MatchState { get; set; } = "PENDING";
        public bool IsListed { get; set; }
        public DateTime UpdatedAt { get; set; }
    }

    public sealed record UpsertOutcome(CatalogBatchResult Result, List<(CatalogItem Item, string Market)> NeedsEnrichment);

    public async Task<UpsertOutcome> UpsertAsync(Guid partnerId, IReadOnlyList<CatalogItem> items, Guid? feedId, CancellationToken ct)
    {
        var partner = await partners.GetAsync(partnerId, ct);
        var defaultMarket = partner.Markets[0];
        var results = new List<CatalogItemResult>();
        var cacheOps = new List<(string Sku, Guid? Product, decimal Confidence, string State, bool Listed, Guid? Previous)>();
        var review = new List<object>();
        var enrichment = new List<(CatalogItem, string)>();
        var now = clock.UtcNow;

        await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            foreach (var item in items)
            {
                var errors = new List<FieldError>();
                string? gtin14 = null;
                var gtinNormalized = false;
                if (item.Gtin is { Length: > 0 } raw)
                {
                    if (!Gtin.TryNormalize(raw, out var g)) errors.Add(new FieldError("/gtin", "Invalid GTIN check digit or length", "HS-INVALID-GTIN"));
                    else
                    {
                        gtin14 = Gtin.IsRestrictedCirculation(g) ? null : g;
                        gtinNormalized = raw.Length is 8 or 12;
                    }
                }
                var markets = item.Markets is { Count: > 0 } m ? m : [defaultMarket];
                if (markets.Any(x => !partner.Markets.Contains(x))) errors.Add(new FieldError("/markets", "Market not enabled for this partner", "HS-UNSUPPORTED-MARKET"));
                if (errors.Count > 0)
                {
                    results.Add(new CatalogItemResult { PartnerSku = item.PartnerSku, Status = "REJECTED", Errors = errors });
                    continue;
                }

                var market = markets[0];
                var previous = await u.QueryFirstOrDefaultAsync<ItemRow>(
                    "select partner_sku, product_id, match_method, match_confidence, match_state, is_listed, updated_at from tenant.catalog_item where partner_id = @partnerId and partner_sku = @sku",
                    new { partnerId, sku = item.PartnerSku }, ct);

                Guid? productId = null;
                string method = "NONE", state;
                decimal confidence = 0;
                if (previous is { MatchState: "CONFIRMED" or "REJECTED" })
                {
                    // Manual decisions are sticky until the partner overrides them again.
                    productId = previous.ProductId;
                    method = previous.MatchMethod ?? "MANUAL";
                    confidence = previous.MatchConfidence ?? 1;
                    state = previous.MatchState;
                }
                else
                {
                    if (gtin14 is not null)
                    {
                        productId = await cache.GetProductByGtinAsync(gtin14) ?? await u.ScalarAsync<Guid?>(
                            "select product_id from kb.product where primary_gtin = @gtin14 union select product_id from kb.product_identifier where id_type = 'GTIN' and id_value = @gtin14 limit 1",
                            new { gtin14 }, ct);
                        if (productId is not null) { method = gtinNormalized ? "GTIN_NORMALIZED" : "GTIN_EXACT"; confidence = gtinNormalized ? 0.99m : 1.0m; }
                    }
                    if (productId is null)
                    {
                        var size = NetQuantity.Normalize(item.NetQuantity?.Value, item.NetQuantity?.Unit);
                        var best = (Score: 0m, Row: (FuzzyRow?)null);
                        foreach (var f in await u.QueryAsync<FuzzyRow>(
                                     """
                                     select p.product_id, p.name, b.name as brand, p.net_quantity, p.net_quantity_unit, p.primary_gtin, similarity(p.name, @title) as sim
                                     from kb.product p left join kb.brand b on b.brand_id = p.brand_id
                                     where p.status = 'ACTIVE' and @market = any(p.markets) and similarity(p.name, @title) > 0.25
                                     order by sim desc limit 5
                                     """, new { title = item.Title, market }, ct))
                        {
                            var features = EntityResolution.Features(
                                new EntityResolution.Candidate(item.Title, item.Brand, size, null, [], gtin14),
                                new EntityResolution.Candidate(f.Name, f.Brand, NetQuantity.Normalize(f.NetQuantity, f.NetQuantityUnit), null, [], gtin14 is null ? null : f.PrimaryGtin?.Trim()));
                            var score = models.ScoreEntityMatch(features);
                            if (score > best.Score) best = (score, f);
                        }
                        if (best.Row is not null && best.Score >= EntityResolution.ReviewThreshold)
                        {
                            productId = best.Row.ProductId;
                            method = "FUZZY";
                            confidence = Math.Round(best.Score, 3);
                        }
                    }
                    state = productId is null
                        ? item.Nutrition is { Count: > 0 } && item.IngredientsText is not null ? "PENDING_ENRICHMENT" : "UNMATCHED"
                        : confidence >= EntityResolution.AutoMergeThreshold ? "AUTO_MATCHED" : "NEEDS_REVIEW";
                }

                var listed = item.IsListed ?? true;
                await u.ExecuteAsync(
                    """
                    insert into tenant.catalog_item (partner_id, partner_sku, gtin, title, brand_text, category_path, net_quantity, net_quantity_unit, product_id,
                        match_method, match_confidence, match_state, raw_payload_uri, is_listed, updated_at)
                    values (@partnerId, @sku, @gtin14, @title, @brand, @path, @netValue, @netUnit, @productId, @method, @confidence, @state, @uri, @listed, @now)
                    on conflict (partner_id, partner_sku) do update set gtin = excluded.gtin, title = excluded.title, brand_text = excluded.brand_text,
                        category_path = excluded.category_path, net_quantity = excluded.net_quantity, net_quantity_unit = excluded.net_quantity_unit,
                        product_id = excluded.product_id, match_method = excluded.match_method, match_confidence = excluded.match_confidence,
                        match_state = excluded.match_state, raw_payload_uri = excluded.raw_payload_uri, is_listed = excluded.is_listed, updated_at = excluded.updated_at
                    """,
                    new
                    {
                        partnerId, sku = item.PartnerSku, gtin14, title = item.Title, brand = item.Brand, path = item.CategoryPath?.ToArray(),
                        netValue = item.NetQuantity?.Value, netUnit = item.NetQuantity?.Unit, productId, method, confidence, state,
                        uri = feedId is null ? null : $"feed://{feedId}", listed, now,
                    }, ct);

                var sufficiency = productId is { } pid && await cache.GetProfileAsync(market, pid) is { } hp ? hp.DataSufficiency : "INSUFFICIENT";
                var match = new MatchResult { State = state, Method = method, Confidence = confidence, ProductId = productId, DataSufficiency = sufficiency };
                results.Add(new CatalogItemResult { PartnerSku = item.PartnerSku, Status = "ACCEPTED", Match = match });
                cacheOps.Add((item.PartnerSku, productId, confidence, state, listed, previous?.ProductId));
                if (state is "NEEDS_REVIEW" or "UNMATCHED") review.Add(new { partnerSku = item.PartnerSku, match });
                if (state == "PENDING_ENRICHMENT") enrichment.Add((item, market));
            }
            await Outbox.EnqueueAsync(u, partnerId, Topics.CatalogChanges, partnerId.ToString(), new { feedId, items = cacheOps.Select(c => new { sku = c.Sku, c.Product, c.State }) }, ct);
            if (review.Count > 0) await WebhookFanoutConsumer.RequestAsync(u, partnerId, WebhookTypes.CatalogReview, new { feedId, items = review }, ct);
        }, ct);

        foreach (var (sku, product, conf, state, listed, previous) in cacheOps) await ApplyCacheAsync(partnerId, sku, product, conf, state, listed, previous);
        var accepted = results.Count(r => r.Status == "ACCEPTED");
        log.LogInformation("Catalog upsert for {Partner}: {Accepted}/{Received} accepted, {Review} need review, {Enrich} pending enrichment",
            partnerId, accepted, items.Count, review.Count, enrichment.Count);
        return new UpsertOutcome(new CatalogBatchResult(Ids.NewId(), items.Count, accepted, items.Count - accepted, results), enrichment);
    }

    /// <summary>Only confirmed or high-confidence matches enter the hot path (skumap / p2sku).</summary>
    public async Task ApplyCacheAsync(Guid partnerId, string sku, Guid? product, decimal confidence, string state, bool listed, Guid? previous)
    {
        if (previous is { } prev && prev != product) await cache.RemovePartnerSkuAsync(partnerId, prev, sku);
        if (product is { } p && listed && state is "AUTO_MATCHED" or "CONFIRMED")
        {
            await cache.SetSkuMapAsync(partnerId, sku, new SkuMapEntry(p, confidence, state));
            await cache.AddPartnerSkuAsync(partnerId, p, sku);
        }
        else
        {
            await cache.RemoveSkuMapAsync(partnerId, sku);
            if (product is { } q) await cache.RemovePartnerSkuAsync(partnerId, q, sku);
        }
    }

    public async Task<CatalogItemStatus> GetAsync(Guid partnerId, string sku, CancellationToken ct)
    {
        var partner = await partners.GetAsync(partnerId, ct);
        var row = await db.InTxAsync(DbRole.App, partnerId, u => u.QueryFirstOrDefaultAsync<ItemRow>(
            "select partner_sku, product_id, match_method, match_confidence, match_state, is_listed, updated_at from tenant.catalog_item where partner_id = @partnerId and partner_sku = @sku",
            new { partnerId, sku }, ct), ct) ?? throw HsException.NotFound("Catalog item " + sku);
        var hp = row.ProductId is { } pid ? await cache.GetProfileAsync(partner.Markets[0], pid) : null;
        return new CatalogItemStatus
        {
            PartnerSku = row.PartnerSku, IsListed = row.IsListed, UpdatedAt = row.UpdatedAt, Health = hp is null ? null : ApiMapper.Summary(hp),
            Match = new MatchResult { State = row.MatchState, Method = row.MatchMethod ?? "NONE", Confidence = row.MatchConfidence ?? 0, ProductId = row.ProductId, DataSufficiency = hp?.DataSufficiency ?? "INSUFFICIENT" },
        };
    }

    public async Task DelistAsync(Guid partnerId, string sku, CancellationToken ct)
    {
        var product = await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            var row = await u.QueryFirstOrDefaultAsync<ItemRow>("select partner_sku, product_id, match_state, is_listed from tenant.catalog_item where partner_id = @partnerId and partner_sku = @sku",
                new { partnerId, sku }, ct) ?? throw HsException.NotFound("Catalog item " + sku);
            await u.ExecuteAsync("update tenant.catalog_item set is_listed = false, updated_at = @now where partner_id = @partnerId and partner_sku = @sku", new { partnerId, sku, now = clock.UtcNow }, ct);
            return row.ProductId;
        }, ct);
        await ApplyCacheAsync(partnerId, sku, product, 0, "DELISTED", false, null);
    }

    public async Task<CatalogItemStatus> OverrideMatchAsync(Guid partnerId, string sku, MatchOverrideRequest req, CancellationToken ct)
    {
        if (req.Action == "ASSIGN" && req.ProductId is null) throw HsException.Unprocessable("HS-MATCH-PRODUCT-REQUIRED", "productId is required when action is ASSIGN");
        var (product, previous) = await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            var row = await u.QueryFirstOrDefaultAsync<ItemRow>("select partner_sku, product_id, match_state, is_listed from tenant.catalog_item where partner_id = @partnerId and partner_sku = @sku",
                new { partnerId, sku }, ct) ?? throw HsException.NotFound("Catalog item " + sku);
            Guid? productId = row.ProductId;
            string state, method;
            decimal confidence;
            switch (req.Action)
            {
                case "CONFIRM":
                    if (productId is null) throw HsException.Unprocessable("HS-MATCH-NOTHING-TO-CONFIRM", "The item has no candidate product; use ASSIGN");
                    (state, method, confidence) = ("CONFIRMED", "MANUAL", 1m);
                    break;
                case "REJECT":
                    (state, method, confidence) = ("REJECTED", "MANUAL", 0m);
                    productId = null;
                    break;
                default:
                    if (!await u.ScalarAsync<bool>("select exists (select 1 from kb.product where product_id = @id and status <> 'MERGED')", new { id = req.ProductId }, ct))
                        throw HsException.Unprocessable("HS-UNKNOWN-PRODUCT", "productId does not exist");
                    (state, method, confidence, productId) = ("CONFIRMED", "MANUAL", 1m, req.ProductId);
                    break;
            }
            await u.ExecuteAsync(
                "update tenant.catalog_item set product_id = @productId, match_state = @state, match_method = @method, match_confidence = @confidence, updated_at = @now where partner_id = @partnerId and partner_sku = @sku",
                new { productId, state, method, confidence, now = clock.UtcNow, partnerId, sku }, ct);
            await u.ExecuteAsync("insert into audit.change_log (actor, action, entity_type, entity_id, details) values (@actor, @action, 'CATALOG_ITEM', @id, @details::jsonb)",
                new { actor = "partner:" + partnerId, action = "MATCH_" + req.Action, id = sku, details = JsonSerializer.Serialize(new { req.ProductId, req.Comment }) }, ct);
            return ((Guid?, Guid?))(productId, row.ProductId);
        }, ct);
        var status = await GetAsync(partnerId, sku, ct);
        await ApplyCacheAsync(partnerId, sku, product, status.Match.Confidence, status.Match.State, status.IsListed, previous);
        return status;
    }

    /// <summary>Delists SKUs that are missing from a FULL feed.</summary>
    public async Task<int> DelistMissingAsync(Guid partnerId, IReadOnlyCollection<string> present, CancellationToken ct)
    {
        var missing = await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            var rows = (await u.QueryAsync<ItemRow>("select partner_sku, product_id from tenant.catalog_item where partner_id = @partnerId and is_listed and not (partner_sku = any(@present))",
                new { partnerId, present = present.ToArray() }, ct)).ToList();
            await u.ExecuteAsync("update tenant.catalog_item set is_listed = false, updated_at = now() where partner_id = @partnerId and partner_sku = any(@skus)",
                new { partnerId, skus = rows.Select(r => r.PartnerSku).ToArray() }, ct);
            return rows;
        }, ct);
        foreach (var r in missing) await ApplyCacheAsync(partnerId, r.PartnerSku, r.ProductId, 0, "DELISTED", false, null);
        return missing.Count;
    }

    /// <summary>Converts a partner catalog item to a knowledge-base candidate (source PARTNER_FEED, trust tier 3).</summary>
    public static ProductCandidate ToCandidate(Guid partnerId, CatalogItem item, string market, DateTime now)
    {
        var panel = item.Nutrition?.FirstOrDefault(n => n.Basis is "PER_100G" or "PER_100ML") ?? item.Nutrition?.FirstOrDefault();
        return new ProductCandidate
        {
            SourceCode = "PARTNER_FEED", ExternalId = $"{partnerId:N}:{item.PartnerSku}", RawGtin = item.Gtin, Name = item.Title, Brand = item.Brand,
            CategoryCode = null, Markets = [market], NetValue = item.NetQuantity?.Value, NetUnit = item.NetQuantity?.Unit, PackCount = item.PackCount ?? 1,
            PhysicalState = item.PhysicalState ?? (panel?.Basis == "PER_100ML" ? "LIQUID" : "SOLID"),
            ServingValue = panel?.ServingSize?.Value, ServingUnit = panel?.ServingSize?.Unit,
            Nutrition = panel is null ? null : new Nutrition
            {
                Basis = panel.Basis == "PER_SERVING" ? Basis.Per100G : panel.Basis, CarbDefinition = market == "US" ? "TOTAL_INCL_FIBRE" : "AVAILABLE",
                EnergyKcal = panel.EnergyKcal, ProteinG = panel.ProteinG, CarbohydrateG = panel.CarbohydrateG, TotalSugarsG = panel.TotalSugarsG,
                AddedSugarsG = panel.AddedSugarsG, TotalFatG = panel.TotalFatG, SaturatedFatG = panel.SaturatedFatG, TransFatG = panel.TransFatG,
                CholesterolMg = panel.CholesterolMg, SodiumMg = panel.SodiumMg, SaltG = panel.SaltG, FibreG = panel.DietaryFibreG,
            },
            SodiumDeclared = panel?.SodiumMg is not null, SaltDeclared = panel?.SaltG is not null,
            IngredientsText = item.IngredientsText, AllergenStatement = item.AllergenStatement,
            DietaryMarks = ApiMapper.InternalMarks(item.DietaryMarks, market), Attributes = item.Attributes ?? [],
            IsPrivateLabel = item.IsPrivateLabel ?? false, Verification = "UNVERIFIED", ObservedAt = now, PartnerId = partnerId,
            RawJson = JsonSerializer.Serialize(item, HsJson.Options),
        };
    }
}

/// <summary>
/// Asynchronous enrichment for PENDING_ENRICHMENT items (HLD 6.2): the partner's label data becomes a tier-3 knowledge-base
/// record; the item is then matched (GTIN) or queued for review, and the market is re-scored so the item gets a profile.
/// </summary>
public sealed class CatalogEnrichment(Db db, ProductIngestor ingestor, ScoringJob scoring, CatalogService catalog, IClock clock, ILogger<CatalogEnrichment> log)
{
    public async Task EnrichAsync(Guid partnerId, IReadOnlyList<(CatalogItem Item, string Market)> items, CancellationToken ct)
    {
        if (items.Count == 0) return;
        var report = await ingestor.IngestAsync(items.Select(i => CatalogService.ToCandidate(partnerId, i.Item, i.Market, clock.UtcNow)).ToList(), ct);
        foreach (var market in items.Select(i => i.Market).Distinct()) await scoring.RunAsync(market, ct);
        foreach (var (item, _) in items)
        {
            Guid? productId = null;
            if (item.Gtin is { } raw && Gtin.TryNormalize(raw, out var g))
                productId = await db.InTxAsync(DbRole.App, partnerId, u => u.ScalarAsync<Guid?>("select product_id from kb.product where primary_gtin = @g", new { g }, ct), ct);
            var state = productId is null ? "NEEDS_REVIEW" : "AUTO_MATCHED";
            await db.InTxAsync(DbRole.App, partnerId, async u =>
            {
                await u.ExecuteAsync(
                    "update tenant.catalog_item set product_id = @productId, match_state = @state, match_method = @method, match_confidence = @conf, updated_at = now() where partner_id = @partnerId and partner_sku = @sku and match_state = 'PENDING_ENRICHMENT'",
                    new { productId, state, method = productId is null ? "NONE" : "GTIN_EXACT", conf = productId is null ? 0m : 1m, partnerId, sku = item.PartnerSku }, ct);
                if (productId is null)
                    await WebhookFanoutConsumer.RequestAsync(u, partnerId, WebhookTypes.CatalogReview, new
                    {
                        feedId = (Guid?)null,
                        items = new[] { new { partnerSku = item.PartnerSku, match = new MatchResult { State = state, Method = "NONE", Confidence = 0, DataSufficiency = "PARTIAL" } } },
                    }, ct);
            }, ct);
            await catalog.ApplyCacheAsync(partnerId, item.PartnerSku, productId, productId is null ? 0 : 1, state, item.IsListed ?? true, null);
        }
        log.LogInformation("Enrichment for {Partner}: {Created} products created, {Versions} versions", partnerId, report.Created, report.NewVersions);
    }
}

public sealed class FeedOptions
{
    public string StorageRoot { get; set; } = Path.Combine(AppContext.BaseDirectory, ".data", "feeds");
    public int UploadUrlMinutes { get; set; } = 60;
    public long MaxUploadBytes { get; set; } = 200 * 1024 * 1024;
}

/// <summary>Bulk catalog feeds (POST /catalog/feeds): pre-signed upload URL, JSONL/CSV (optionally gzip), FULL or DELTA mode.</summary>
public sealed class FeedService(Db db, CatalogService catalog, CatalogEnrichment enrichment, IDataProtectionProvider protection, FeedOptions options, IClock clock,
    ILogger<FeedService> log)
{
    private readonly ITimeLimitedDataProtector _protector = protection.CreateProtector("HealthSwap.FeedUpload.v1").ToTimeLimitedDataProtector();

    private sealed class FeedRow
    {
        public Guid FeedId { get; set; }
        public Guid PartnerId { get; set; }
        public string Format { get; set; } = "";
        public string Compression { get; set; } = "";
        public string Mode { get; set; } = "";
        public string Status { get; set; } = "";
        public string Stats { get; set; } = "{}";
        public DateTime UploadExpiresAt { get; set; }
        public string? ErrorReportUri { get; set; }
        public DateTime CreatedAt { get; set; }
        public DateTime? CompletedAt { get; set; }
    }

    public async Task<Feed> CreateAsync(Guid partnerId, FeedCreateRequest req, Func<Guid, string, string> uploadUrl, CancellationToken ct)
    {
        if (req.Format == "PARQUET")
            throw HsException.Unprocessable("HS-FEED-FORMAT-UNSUPPORTED", "PARQUET feeds are not supported by the reference implementation; use JSONL or CSV");
        var feedId = Ids.NewId();
        var expires = clock.UtcNow.AddMinutes(options.UploadUrlMinutes);
        await db.InTxAsync(DbRole.App, partnerId, u => u.ExecuteAsync(
            """
            insert into tenant.catalog_feed (feed_id, partner_id, format, compression, mode, status, expected_items, upload_expires_at, created_at)
            values (@feedId, @partnerId, @format, @compression, @mode, 'AWAITING_UPLOAD', @expected, @expires, @now)
            """, new { feedId, partnerId, format = req.Format, compression = req.Compression ?? "GZIP", mode = req.Mode, expected = req.ExpectedItems, expires, now = clock.UtcNow }, ct), ct);
        var token = _protector.Protect($"{partnerId:N}.{feedId:N}", expires);
        return new Feed { FeedId = feedId, Status = "AWAITING_UPLOAD", UploadUrl = uploadUrl(feedId, token), UploadUrlExpiresAt = expires, CreatedAt = clock.UtcNow };
    }

    public async Task<Feed> GetAsync(Guid partnerId, Guid feedId, Func<Guid, string>? errorReportUrl, CancellationToken ct)
    {
        var row = await db.InTxAsync(DbRole.App, partnerId, u => u.QueryFirstOrDefaultAsync<FeedRow>(
            "select feed_id, partner_id, format, compression, mode, status, stats::text as stats, upload_expires_at, error_report_uri, created_at, completed_at from tenant.catalog_feed where feed_id = @feedId",
            new { feedId }, ct), ct) ?? throw HsException.NotFound("Feed " + feedId);
        var status = row.Status == "AWAITING_UPLOAD" && row.UploadExpiresAt < clock.UtcNow ? "EXPIRED" : row.Status;
        return new Feed
        {
            FeedId = row.FeedId, Status = status, Stats = JsonSerializer.Deserialize<FeedStats>(row.Stats, HsJson.Options), CreatedAt = row.CreatedAt, CompletedAt = row.CompletedAt,
            UploadUrlExpiresAt = status == "AWAITING_UPLOAD" ? row.UploadExpiresAt : null,
            ErrorReportUrl = row.ErrorReportUri is not null && errorReportUrl is not null ? errorReportUrl(row.FeedId) : null,
        };
    }

    /// <summary>Validates the pre-signed token and stores the upload; processing continues in the background.</summary>
    public async Task<(Guid PartnerId, Guid FeedId)> AcceptUploadAsync(Guid feedId, string token, Stream body, CancellationToken ct)
    {
        string payload;
        try { payload = _protector.Unprotect(token, out _); }
        catch (CryptographicException) { throw new HsException(403, "HS-FEED-UPLOAD-DENIED", "Forbidden", "The upload URL is invalid or expired"); }
        var parts = payload.Split('.');
        if (parts.Length != 2 || !Guid.TryParse(parts[1], out var tokenFeed) || tokenFeed != feedId || !Guid.TryParse(parts[0], out var partnerId))
            throw new HsException(403, "HS-FEED-UPLOAD-DENIED", "Forbidden", "The upload URL does not match this feed");
        Directory.CreateDirectory(options.StorageRoot);
        var path = Path.Combine(options.StorageRoot, feedId.ToString("N") + ".upload");
        await using (var file = File.Create(path))
        {
            var buffer = new byte[81920];
            long total = 0;
            int n;
            while ((n = await body.ReadAsync(buffer, ct)) > 0)
            {
                total += n;
                if (total > options.MaxUploadBytes) throw HsException.TooLarge($"Feed uploads are limited to {options.MaxUploadBytes} bytes");
                await file.WriteAsync(buffer.AsMemory(0, n), ct);
            }
        }
        var updated = await db.InTxAsync(DbRole.App, partnerId, u => u.ExecuteAsync(
            "update tenant.catalog_feed set status = 'PROCESSING', storage_uri = @uri where feed_id = @feedId and status = 'AWAITING_UPLOAD'",
            new { feedId, uri = "file://" + path }, ct), ct);
        if (updated == 0) throw HsException.Conflict("HS-FEED-STATE", "The feed is not awaiting an upload");
        return (partnerId, feedId);
    }

    public async Task ProcessAsync(Guid partnerId, Guid feedId, CancellationToken ct)
    {
        var row = await db.InTxAsync(DbRole.App, partnerId, u => u.QuerySingleAsync<FeedRow>(
            "select feed_id, partner_id, format, compression, mode, status, stats::text as stats, upload_expires_at, created_at from tenant.catalog_feed where feed_id = @feedId",
            new { feedId }, ct), ct);
        var path = Path.Combine(options.StorageRoot, feedId.ToString("N") + ".upload");
        var errors = new List<string>();
        var stats = new FeedStats();
        var present = new List<string>();
        var status = "COMPLETED";
        try
        {
            var items = new List<CatalogItem>();
            await using var raw = File.OpenRead(path);
            await using Stream input = row.Compression == "GZIP" ? new GZipStream(raw, CompressionMode.Decompress) : raw;
            using var reader = new StreamReader(input, Encoding.UTF8);
            var lineNo = 0;
            string[]? header = null;
            while (await reader.ReadLineAsync(ct) is { } line)
            {
                lineNo++;
                if (string.IsNullOrWhiteSpace(line)) continue;
                try
                {
                    if (row.Format == "CSV")
                    {
                        var cells = ParseCsvLine(line);
                        if (header is null) { header = cells; continue; }
                        items.Add(FromCsv(header, cells));
                    }
                    else items.Add(JsonSerializer.Deserialize<CatalogItem>(line, HsJson.Options) ?? throw new JsonException("empty line"));
                }
                catch (Exception ex) when (ex is JsonException or FormatException or IndexOutOfRangeException)
                {
                    errors.Add($"line {lineNo}: {ex.Message}");
                }
            }
            var received = items.Count + errors.Count;
            int accepted = 0, rejected = errors.Count, auto = 0, review = 0, unmatched = 0, pending = 0;
            foreach (var chunk in items.Chunk(1000))
            {
                var outcome = await catalog.UpsertAsync(partnerId, chunk, feedId, ct);
                accepted += outcome.Result.Accepted;
                rejected += outcome.Result.Rejected;
                foreach (var r in outcome.Result.Results)
                {
                    if (r.Status == "REJECTED") errors.Add($"{r.PartnerSku}: {string.Join("; ", r.Errors?.Select(e => e.Message) ?? [])}");
                    switch (r.Match?.State)
                    {
                        case "AUTO_MATCHED" or "CONFIRMED": auto++; break;
                        case "NEEDS_REVIEW": review++; break;
                        case "UNMATCHED": unmatched++; break;
                        case "PENDING_ENRICHMENT": pending++; break;
                    }
                }
                present.AddRange(chunk.Select(i => i.PartnerSku));
                await enrichment.EnrichAsync(partnerId, outcome.NeedsEnrichment, ct);
            }
            if (row.Mode == "FULL") await catalog.DelistMissingAsync(partnerId, present, ct);
            stats = new FeedStats { Received = received, Accepted = accepted, Rejected = rejected, AutoMatched = auto, NeedsReview = review, Unmatched = unmatched, PendingEnrichment = pending };
            status = rejected == 0 ? "COMPLETED" : accepted == 0 ? "FAILED" : "PARTIAL";
        }
        catch (Exception ex) when (ex is IOException or InvalidDataException)
        {
            errors.Add(ex.Message);
            status = "FAILED";
        }
        string? errorUri = null;
        if (errors.Count > 0)
        {
            var errorPath = Path.Combine(options.StorageRoot, feedId.ToString("N") + ".errors.txt");
            await File.WriteAllLinesAsync(errorPath, errors, ct);
            errorUri = "file://" + errorPath;
        }
        await db.InTxAsync(DbRole.App, partnerId, u => u.ExecuteAsync(
            "update tenant.catalog_feed set status = @status, stats = @stats::jsonb, error_report_uri = @errorUri, completed_at = @now where feed_id = @feedId",
            new { status, stats = JsonSerializer.Serialize(stats, HsJson.Options), errorUri, now = clock.UtcNow, feedId }, ct), ct);
        log.LogInformation("Feed {Feed} {Status}: {Accepted} accepted, {Rejected} rejected", feedId, status, stats.Accepted, stats.Rejected);
    }

    public string? ErrorReportPath(Guid feedId)
    {
        var p = Path.Combine(options.StorageRoot, feedId.ToString("N") + ".errors.txt");
        return File.Exists(p) ? p : null;
    }

    private static CatalogItem FromCsv(string[] header, string[] cells)
    {
        string? V(string name) { var i = Array.IndexOf(header, name); return i >= 0 && i < cells.Length && cells[i].Length > 0 ? cells[i] : null; }
        var netValue = V("netQuantityValue");
        return new CatalogItem
        {
            PartnerSku = V("partnerSku") ?? throw new FormatException("partnerSku is required"), Title = V("title") ?? throw new FormatException("title is required"),
            Gtin = V("gtin"), Brand = V("brand"), CategoryPath = V("categoryPath")?.Split('|').ToList(),
            NetQuantity = netValue is null ? null : new NetQuantityDto(decimal.Parse(netValue, CultureInfo.InvariantCulture), V("netQuantityUnit") ?? "g"),
            PackCount = V("packCount") is { } pc ? int.Parse(pc, CultureInfo.InvariantCulture) : null,
            IsPrivateLabel = V("isPrivateLabel") is { } pl ? bool.Parse(pl) : null, IsListed = V("isListed") is { } il ? bool.Parse(il) : null,
        };
    }

    /// <summary>RFC 4180 line parser (quoted fields, doubled quotes); multi-line fields are not supported.</summary>
    public static string[] ParseCsvLine(string line)
    {
        var cells = new List<string>();
        var sb = new StringBuilder();
        var quoted = false;
        for (var i = 0; i < line.Length; i++)
        {
            var ch = line[i];
            if (quoted)
            {
                if (ch == '"' && i + 1 < line.Length && line[i + 1] == '"') { sb.Append('"'); i++; }
                else if (ch == '"') quoted = false;
                else sb.Append(ch);
            }
            else if (ch == '"') quoted = true;
            else if (ch == ',') { cells.Add(sb.ToString()); sb.Clear(); }
            else sb.Append(ch);
        }
        cells.Add(sb.ToString());
        return cells.ToArray();
    }
}

/// <summary>Store inventory mirror (MIRROR availability mode): PostgreSQL durable copy + hot cache hash per store.</summary>
public sealed class InventoryService(Db db, HotCache cache, PartnerDirectory partners)
{
    public async Task<InventoryBatchAck> UpsertAsync(Guid partnerId, string storeId, InventoryBatchRequest req, CancellationToken ct)
    {
        var partner = await partners.GetAsync(partnerId, ct);
        var currency = partner.Config.Currency;
        var errors = new List<InventoryError>();
        var accepted = new List<(string Sku, InventoryItem Item)>();
        foreach (var e in req.Entries)
        {
            if (!string.Equals(e.Price.Currency, currency, StringComparison.Ordinal)) { errors.Add(new InventoryError(e.PartnerSku, "HS-CURRENCY-MISMATCH", $"Price currency must be {currency}")); continue; }
            if (!Currency.TryParse(e.Price.Amount, out var price) || price < 0) { errors.Add(new InventoryError(e.PartnerSku, "HS-INVALID-PRICE", "Price must be a non-negative decimal")); continue; }
            decimal? mrp = e.Mrp is { } m && Currency.TryParse(m.Amount, out var mv) ? mv : null;
            accepted.Add((e.PartnerSku, new InventoryItem(e.InStock, price, mrp, req.SnapshotAt, e.AvailableQuantity)));
        }
        var missing = new List<string>();
        await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            if (!await u.ScalarAsync<bool>("select exists (select 1 from tenant.store where partner_id = @partnerId and store_id = @storeId and is_active)", new { partnerId, storeId }, ct))
                throw HsException.Unprocessable("HS-UNKNOWN-STORE", $"Store {storeId} is not registered for this partner");
            foreach (var (sku, item) in accepted)
                await u.ExecuteAsync(
                    """
                    insert into tenant.store_inventory (partner_id, store_id, partner_sku, in_stock, available_qty, price, mrp, currency, observed_at)
                    values (@partnerId, @storeId, @sku, @inStock, @qty, @price, @mrp, @currency, @observed)
                    on conflict (partner_id, store_id, partner_sku) do update set in_stock = excluded.in_stock, available_qty = excluded.available_qty,
                        price = excluded.price, mrp = excluded.mrp, currency = excluded.currency, observed_at = excluded.observed_at
                    where tenant.store_inventory.observed_at <= excluded.observed_at
                    """, new { partnerId, storeId, sku, inStock = item.InStock, qty = item.AvailableQuantity, price = item.Price, mrp = item.Mrp, currency, observed = item.ObservedAt }, ct);
            if (req.Mode == "FULL_SNAPSHOT")
            {
                missing.AddRange(await u.QueryAsync<string>(
                    "update tenant.store_inventory set in_stock = false, observed_at = @observed where partner_id = @partnerId and store_id = @storeId and in_stock and not (partner_sku = any(@skus)) returning partner_sku",
                    new { partnerId, storeId, observed = req.SnapshotAt, skus = accepted.Select(a => a.Sku).ToArray() }, ct));
            }
            await Outbox.EnqueueAsync(u, partnerId, Topics.InventoryUpdates, $"{partnerId}:{storeId}", new { storeId, req.Mode, accepted = accepted.Count, markedOutOfStock = missing.Count, req.SnapshotAt }, ct);
        }, ct);

        var current = await cache.GetInventoryAsync(partnerId, storeId, accepted.Select(a => a.Sku).Concat(missing).ToList());
        var updates = accepted.Where(a => !current.TryGetValue(a.Sku, out var c) || c.ObservedAt <= a.Item.ObservedAt).ToList();
        updates.AddRange(missing.Select(s => (s, (current.GetValueOrDefault(s) ?? new InventoryItem(false, 0, null, req.SnapshotAt, null)) with { InStock = false, ObservedAt = req.SnapshotAt })));
        if (updates.Count > 0) await cache.SetInventoryAsync(partnerId, storeId, updates);
        return new InventoryBatchAck(accepted.Count, errors.Count, errors.Count > 0 ? errors : null);
    }
}

/// <summary>Shopper preferences with consent handling (HLD 11: DPDP / CCPA; health fields need explicit consent).</summary>
public sealed class PreferenceService(Db db, HotCache cache, IClock clock)
{
    private sealed class PrefRow
    {
        public string DietType { get; set; } = "ANY";
        public string[] Allergens { get; set; } = [];
        public bool StrictMayContain { get; set; }
        public string[] Goals { get; set; } = [];
        public string[] AvoidIngredients { get; set; } = [];
        public string[] ExcludedBrands { get; set; } = [];
        public string[] ReligiousPrefs { get; set; } = [];
        public string? Locale { get; set; }
        public bool? IsMinor { get; set; }
        public bool HealthDataConsent { get; set; }
        public bool PersonalizationConsent { get; set; }
        public string? ConsentNoticeVersion { get; set; }
        public DateTime UpdatedAt { get; set; }
    }

    public static void ValidateUserRef(string userRef)
    {
        if (userRef.Length is < 8 or > 128 || !userRef.All(c => char.IsAsciiLetterOrDigit(c) || c is '_' or '-'))
            throw HsException.Invalid("userRef must be a pseudonymous identifier of 8-128 characters [A-Za-z0-9_-]");
    }

    public async Task<UserPreferences> GetAsync(Guid partnerId, string userRef, CancellationToken ct)
    {
        ValidateUserRef(userRef);
        var row = await db.InTxAsync(DbRole.App, partnerId, u => u.QueryFirstOrDefaultAsync<PrefRow>(
            """
            select diet_type, allergens, strict_may_contain, goals, avoid_ingredients, excluded_brands, religious_prefs, locale, is_minor,
                   health_data_consent, personalization_consent, consent_notice_version, updated_at
            from tenant.user_preference where partner_id = @partnerId and user_ref = @userRef
            """, new { partnerId, userRef }, ct), ct) ?? throw HsException.NotFound("Preferences");
        return ToApi(row);
    }

    public async Task<UserPreferences> PutAsync(Guid partnerId, string userRef, UserPreferences p, CancellationToken ct)
    {
        ValidateUserRef(userRef);
        var health = p.HealthDataConsent ?? false;
        var now = clock.UtcNow;
        var doc = new ShopperPrefsDoc
        {
            DietType = p.DietType ?? "ANY", Allergens = health ? p.Allergens ?? [] : [], StrictMayContain = p.StrictMayContain ?? true,
            Goals = health ? p.Goals ?? [] : [], AvoidIngredients = p.AvoidIngredients ?? [], ExcludedBrands = p.ExcludedBrands ?? [],
            ReligiousPreferences = p.ReligiousPreferences ?? [], Locale = p.Locale, IsMinor = p.IsMinor, HealthDataConsent = health,
            PersonalizationConsent = p.PersonalizationConsent ?? false, ConsentNoticeVersion = p.ConsentNoticeVersion, UpdatedAt = now,
        };
        await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            var previous = await u.QueryFirstOrDefaultAsync<PrefRow>("select health_data_consent, personalization_consent from tenant.user_preference where partner_id = @partnerId and user_ref = @userRef",
                new { partnerId, userRef }, ct);
            await u.ExecuteAsync(
                """
                insert into tenant.user_preference (partner_id, user_ref, diet_type, allergens, strict_may_contain, goals, avoid_ingredients, excluded_brands,
                    religious_prefs, locale, is_minor, health_data_consent, personalization_consent, consent_notice_version, updated_at)
                values (@partnerId, @userRef, @DietType, @allergens, @StrictMayContain, @goals, @avoid, @brands, @religious, @Locale, @IsMinor,
                    @HealthDataConsent, @PersonalizationConsent, @ConsentNoticeVersion, @UpdatedAt)
                on conflict (partner_id, user_ref) do update set diet_type = excluded.diet_type, allergens = excluded.allergens,
                    strict_may_contain = excluded.strict_may_contain, goals = excluded.goals, avoid_ingredients = excluded.avoid_ingredients,
                    excluded_brands = excluded.excluded_brands, religious_prefs = excluded.religious_prefs, locale = excluded.locale, is_minor = excluded.is_minor,
                    health_data_consent = excluded.health_data_consent, personalization_consent = excluded.personalization_consent,
                    consent_notice_version = excluded.consent_notice_version, updated_at = excluded.updated_at
                """,
                new
                {
                    partnerId, userRef, doc.DietType, allergens = doc.Allergens.ToArray(), doc.StrictMayContain, goals = doc.Goals.ToArray(),
                    avoid = doc.AvoidIngredients.ToArray(), brands = doc.ExcludedBrands.ToArray(), religious = doc.ReligiousPreferences.ToArray(),
                    doc.Locale, doc.IsMinor, doc.HealthDataConsent, doc.PersonalizationConsent, doc.ConsentNoticeVersion, doc.UpdatedAt,
                }, ct);
            foreach (var (purpose, granted, before) in new[] { ("HEALTH_PREFERENCES", doc.HealthDataConsent, previous?.HealthDataConsent), ("PERSONALIZATION", doc.PersonalizationConsent, previous?.PersonalizationConsent) })
                if (before != granted)
                    await u.ExecuteAsync(
                        "insert into tenant.consent_record (partner_id, user_ref, purpose, granted, notice_version, captured_at, evidence) values (@partnerId, @userRef, @purpose, @granted, @notice, @now, '{\"channel\":\"API\"}'::jsonb)",
                        new { partnerId, userRef, purpose, granted, notice = doc.ConsentNoticeVersion ?? "unspecified", now }, ct);
        }, ct);
        await cache.SetPrefsAsync(partnerId, userRef, doc);
        return ToApi(doc);
    }

    public async Task<ShopperPrefsDoc?> LoadForEvaluationAsync(Guid partnerId, string userRef, CancellationToken ct)
    {
        var cached = await cache.GetPrefsAsync(partnerId, userRef);
        if (cached is not null) return cached;
        var row = await db.InTxAsync(DbRole.App, partnerId, u => u.QueryFirstOrDefaultAsync<PrefRow>(
            """
            select diet_type, allergens, strict_may_contain, goals, avoid_ingredients, excluded_brands, religious_prefs, locale, is_minor,
                   health_data_consent, personalization_consent, consent_notice_version, updated_at
            from tenant.user_preference where partner_id = @partnerId and user_ref = @userRef
            """, new { partnerId, userRef }, ct), ct);
        if (row is null) return null;
        var doc = new ShopperPrefsDoc
        {
            DietType = row.DietType, Allergens = row.Allergens.ToList(), StrictMayContain = row.StrictMayContain, Goals = row.Goals.ToList(),
            AvoidIngredients = row.AvoidIngredients.ToList(), ExcludedBrands = row.ExcludedBrands.ToList(), ReligiousPreferences = row.ReligiousPrefs.ToList(),
            Locale = row.Locale, IsMinor = row.IsMinor, HealthDataConsent = row.HealthDataConsent, PersonalizationConsent = row.PersonalizationConsent,
            ConsentNoticeVersion = row.ConsentNoticeVersion, UpdatedAt = row.UpdatedAt,
        };
        await cache.SetPrefsAsync(partnerId, userRef, doc);
        return doc;
    }

    private static UserPreferences ToApi(PrefRow r) => new()
    {
        DietType = r.DietType, Allergens = r.Allergens.ToList(), StrictMayContain = r.StrictMayContain, Goals = r.Goals.ToList(), AvoidIngredients = r.AvoidIngredients.ToList(),
        ExcludedBrands = r.ExcludedBrands.ToList(), ReligiousPreferences = r.ReligiousPrefs.ToList(), Locale = r.Locale, IsMinor = r.IsMinor,
        HealthDataConsent = r.HealthDataConsent, PersonalizationConsent = r.PersonalizationConsent, ConsentNoticeVersion = r.ConsentNoticeVersion, UpdatedAt = r.UpdatedAt,
    };

    private static UserPreferences ToApi(ShopperPrefsDoc d) => new()
    {
        DietType = d.DietType, Allergens = d.Allergens, StrictMayContain = d.StrictMayContain, Goals = d.Goals, AvoidIngredients = d.AvoidIngredients,
        ExcludedBrands = d.ExcludedBrands, ReligiousPreferences = d.ReligiousPreferences, Locale = d.Locale, IsMinor = d.IsMinor,
        HealthDataConsent = d.HealthDataConsent, PersonalizationConsent = d.PersonalizationConsent, ConsentNoticeVersion = d.ConsentNoticeVersion, UpdatedAt = d.UpdatedAt,
    };
}

/// <summary>Erasure (DELETE /users/{userRef}): preferences deleted, userRef removed from events, orders and charges.</summary>
public sealed class PrivacyService(Db db, HotCache cache, IClock clock, ILogger<PrivacyService> log)
{
    public async Task<ErasureRequest> EraseAsync(Guid partnerId, string userRef, CancellationToken ct)
    {
        PreferenceService.ValidateUserRef(userRef);
        var requestId = Ids.NewId();
        var now = clock.UtcNow;
        var hash = SHA256.HashData(Encoding.UTF8.GetBytes($"{partnerId:N}:{userRef}"));
        await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            await u.ExecuteAsync("insert into tenant.dsar_request (request_id, partner_id, user_ref_hash, request_type, status, received_at) values (@requestId, @partnerId, @hash, 'ERASURE', 'IN_PROGRESS', @now)",
                new { requestId, partnerId, hash, now }, ct);
            var p = new { partnerId, userRef };
            await u.ExecuteAsync("delete from tenant.user_preference where partner_id = @partnerId and user_ref = @userRef", p, ct);
            await u.ExecuteAsync("update tenant.consent_record set user_ref = null where partner_id = @partnerId and user_ref = @userRef", p, ct);
            await u.ExecuteAsync("update txn.cart_evaluation set user_ref = null where partner_id = @partnerId and user_ref = @userRef", p, ct);
            await u.ExecuteAsync("update txn.suggestion set user_ref = null where partner_id = @partnerId and user_ref = @userRef", p, ct);
            await u.ExecuteAsync("update txn.order_header set user_ref = null where partner_id = @partnerId and user_ref = @userRef", p, ct);
            await u.ExecuteAsync("update txn.reco_charge set user_ref = null where partner_id = @partnerId and user_ref = @userRef", p, ct);
            await u.ExecuteAsync("update tenant.dsar_request set status = 'COMPLETED', completed_at = @now where request_id = @requestId", new { requestId, now }, ct);
            await WebhookFanoutConsumer.RequestAsync(u, partnerId, WebhookTypes.ErasureCompleted, new { requestId, completedAt = now }, ct);
        }, ct);
        await cache.DeletePrefsAsync(partnerId, userRef);
        var removed = await cache.DeleteUserKeysAsync(partnerId, userRef);
        log.LogInformation("Erasure {Request} completed for partner {Partner} ({Keys} cache keys removed)", requestId, partnerId, removed);
        return new ErasureRequest(requestId, "COMPLETED", now);
    }
}
