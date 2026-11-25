using System.Text.Json;
using HealthSwap.Domain;
using HealthSwap.Domain.Knowledge;
using HealthSwap.Platform.Bus;
using HealthSwap.Platform.Data;
using Microsoft.Extensions.Logging;

namespace HealthSwap.Platform.Knowledge;

/// <summary>Reads canonical product records (current versions) for scoring, graph builds and the admin plane.</summary>
public sealed class KbReader(Db db, ReferenceDataRepository refs)
{
    private sealed class ProductRow
    {
        public Guid ProductId { get; set; }
        public string? PrimaryGtin { get; set; }
        public string Name { get; set; } = "";
        public string? Brand { get; set; }
        public string? CategoryCode { get; set; }
        public Guid? CategoryId { get; set; }
        public string? CategoryName { get; set; }
        public string ScoringCategory { get; set; } = "GENERAL";
        public string PhysicalState { get; set; } = "SOLID";
        public decimal? NetQuantity { get; set; }
        public string? NetQuantityUnit { get; set; }
        public int PackCount { get; set; }
        public string[] Markets { get; set; } = [];
        public bool IsPrivateLabel { get; set; }
        public string Status { get; set; } = "";
        public string Attributes { get; set; } = "{}";
        public long VersionId { get; set; }
        public decimal? ServingSize { get; set; }
        public string? ServingUnit { get; set; }
        public string? CarbDefinition { get; set; }
        public string? IngredientsText { get; set; }
        public string? AllergenStatement { get; set; }
        public short? NovaGroup { get; set; }
        public decimal? NovaConfidence { get; set; }
        public decimal Confidence { get; set; }
        public string DataSufficiency { get; set; } = "";
        public string Verification { get; set; } = "";
    }

    private sealed class NutrientRow { public long VersionId { get; set; } public string NutrientCode { get; set; } = ""; public string Basis { get; set; } = ""; public decimal Amount { get; set; } public bool IsEstimated { get; set; } }
    private sealed class AllergenRow { public long VersionId { get; set; } public string AllergenCode { get; set; } = ""; public string Presence { get; set; } = ""; public bool Verified { get; set; } }
    private sealed class MarkRow { public long VersionId { get; set; } public string MarkCode { get; set; } = ""; }

    public sealed record LoadedProduct(ProductData Data, string? CategoryName, IReadOnlyList<string> Sources);

    public async Task<List<LoadedProduct>> LoadAsync(string? market, IReadOnlyCollection<Guid>? ids = null, CancellationToken ct = default)
    {
        var taxonomy = await refs.GetTaxonomyAsync(ct);
        var (products, nutrients, allergens, marks, recalled, sources) = await db.InTxAsync(DbRole.KbWriter, null, async u =>
        {
            var p = (await u.QueryAsync<ProductRow>(
                """
                select p.product_id, p.primary_gtin, p.name, b.name as brand, c.code as category_code, c.category_id, c.name as category_name,
                       c.scoring_category, coalesce(p.physical_state, c.physical_state, 'SOLID') as physical_state, p.net_quantity, p.net_quantity_unit,
                       p.pack_count, p.markets, p.is_private_label, p.status, p.attributes::text as attributes,
                       v.version_id, v.serving_size, v.serving_unit, v.carb_definition, v.ingredients_text, v.allergen_statement, v.nova_group,
                       v.nova_confidence, v.confidence, v.data_sufficiency, v.verification
                from kb.product p
                join kb.product_version v on v.version_id = p.current_version_id
                left join kb.brand b on b.brand_id = p.brand_id
                left join kb.category c on c.category_id = p.category_id
                where p.status in ('ACTIVE', 'QUARANTINED') and c.code is not null
                  and (@market::text is null or @market = any(p.markets))
                  and (@ids::uuid[] is null or p.product_id = any(@ids))
                order by p.product_id
                """, new { market, ids = ids?.ToArray() }, ct)).ToList();
            var versions = p.Select(x => x.VersionId).ToArray();
            var productIds = p.Select(x => x.ProductId).ToArray();
            var n = (await u.QueryAsync<NutrientRow>("select version_id, nutrient_code, basis, amount, is_estimated from kb.product_nutrient where version_id = any(@versions)", new { versions }, ct)).ToList();
            var a = (await u.QueryAsync<AllergenRow>("select version_id, allergen_code, presence, verified from kb.product_allergen where version_id = any(@versions)", new { versions }, ct)).ToList();
            var m = (await u.QueryAsync<MarkRow>("select version_id, mark_code from kb.product_mark where version_id = any(@versions)", new { versions }, ct)).ToList();
            var r = (await u.QueryAsync<Guid>("select distinct product_id from kb.recall where product_id = any(@productIds) and (suppress_until is null or suppress_until > now())", new { productIds }, ct)).ToHashSet();
            var s = (await u.QueryAsync<(long VersionId, string Code)>(
                """
                select distinct fp.version_id, s.code from kb.field_provenance fp join kb.source_record sr using (source_record_id) join kb.source s using (source_id)
                where fp.version_id = any(@versions) and fp.is_selected
                """, new { versions }, ct)).ToList();
            return (p, n, a, m, r, s);
        }, ct);

        var nutrientsByVersion = nutrients.ToLookup(x => x.VersionId);
        var allergensByVersion = allergens.ToLookup(x => x.VersionId);
        var marksByVersion = marks.ToLookup(x => x.VersionId);
        var sourcesByVersion = sources.ToLookup(x => x.VersionId, x => x.Code);
        var result = new List<LoadedProduct>(products.Count);
        foreach (var p in products)
        {
            var attrs = JsonSerializer.Deserialize<Dictionary<string, string>>(p.Attributes) ?? [];
            var basis = p.PhysicalState == "LIQUID" ? Basis.Per100Ml : Basis.Per100G;
            var rows = nutrientsByVersion[p.VersionId].ToList();
            if (rows.Count > 0 && rows.All(r => r.Basis != basis)) basis = rows[0].Basis;
            decimal? N(string code) => rows.FirstOrDefault(r => r.NutrientCode == code && r.Basis == basis)?.Amount;
            var nutrition = new Nutrition
            {
                Basis = basis, CarbDefinition = p.CarbDefinition ?? "AVAILABLE", EnergyKcal = N("ENERC_KCAL"), ProteinG = N("PROCNT"), CarbohydrateG = N("CHOCDF"),
                TotalSugarsG = N("SUGAR"), AddedSugarsG = N("SUGAD"), TotalFatG = N("FAT"), SaturatedFatG = N("FASAT"), TransFatG = N("FATRN"),
                CholesterolMg = N("CHOLE"), SodiumMg = N("NA"), FibreG = N("FIBTG"), FvlnPct = N("FVLN_PCT"),
                FvlnEstimated = rows.Any(r => r.NutrientCode == "FVLN_PCT" && r.IsEstimated),
            };
            var parsed = IngredientParser.Parse(p.IngredientsText, taxonomy);
            var allergenMap = allergensByVersion[p.VersionId].ToDictionary(a => a.AllergenCode, a => a.Presence switch
            {
                "CONTAINS" => AllergenPresence.Contains, "MAY_CONTAIN" => AllergenPresence.MayContain, _ => AllergenPresence.FreeFrom,
            });
            var verified = p.Verification is "BRAND_VERIFIED" or "CURATOR_VERIFIED" || allergensByVersion[p.VersionId].Any(a => a.Verified);
            var markList = marksByVersion[p.VersionId].Select(m => m.MarkCode).ToList();
            var diet = DietClassifier.Classify(markList, parsed.Flags, allergenMap);
            var data = new ProductData
            {
                ProductId = p.ProductId, VersionId = p.VersionId, Gtin = p.PrimaryGtin?.Trim(), Name = p.Name, Brand = p.Brand,
                BrandTier = attrs.GetValueOrDefault("brandTier") ?? (p.IsPrivateLabel ? "PRIVATE_LABEL" : "MAINSTREAM"),
                CategoryCode = p.CategoryCode!, CategoryId = p.CategoryId, ScoringCategory = p.ScoringCategory, PhysicalState = p.PhysicalState,
                NetQuantity = NetQuantity.Normalize(p.NetQuantity, p.NetQuantityUnit), PackCount = p.PackCount, Markets = p.Markets,
                IsPrivateLabel = p.IsPrivateLabel, Nutrition = nutrition, ServingSize = p.ServingSize, ServingUnit = p.ServingUnit,
                IngredientsText = p.IngredientsText, Ingredients = parsed.Tokens, IngredientResolution = parsed.ResolutionRatio,
                Additives = parsed.Additives, IngredientFlags = parsed.Flags, Allergens = allergenMap, AllergensVerified = verified,
                AllergenConfidence = verified ? 1.0m : p.AllergenStatement is not null ? 0.9m : allergenMap.Count > 0 ? 0.85m : 0.6m,
                Marks = markList, DietClass = diet.DietClass, JainFriendlyInferred = diet.JainFriendlyInferred || markList.Contains("JAIN_FRIENDLY"),
                Nova = p.NovaGroup, NovaConfidence = p.NovaConfidence ?? 0, DataSufficiency = p.DataSufficiency, Confidence = p.Confidence,
                Verification = p.Verification, Form = attrs.GetValueOrDefault("form"), FlavourFamily = attrs.GetValueOrDefault("flavourFamily"),
                UseOccasion = attrs.GetValueOrDefault("useOccasion"), Cuisine = attrs.GetValueOrDefault("cuisine"),
                Recalled = recalled.Contains(p.ProductId), Quarantined = p.Status == "QUARANTINED",
            };
            result.Add(new LoadedProduct(data, p.CategoryName, sourcesByVersion[p.VersionId].Order(StringComparer.Ordinal).ToList()));
        }
        return result;
    }
}

/// <summary>Recall ingestion (FR-14): match by GTIN, suppress immediately and publish a KB change event for cache invalidation.</summary>
public sealed class RecallIngestor(Db db, ILogger<RecallIngestor> log)
{
    public async Task<int> IngestAsync(IEnumerable<Connectors.RecallRecord> records, CancellationToken ct = default)
    {
        var matched = 0;
        foreach (var r in records)
        {
            var productId = await db.InTxAsync(DbRole.KbWriter, null, async u =>
            {
                var pid = r.Gtin is null ? null : await u.ScalarAsync<Guid?>(
                    "select product_id from kb.product where primary_gtin = @gtin union select product_id from kb.product_identifier where id_type = 'GTIN' and id_value = @gtin limit 1",
                    new { gtin = r.Gtin }, ct);
                await u.ExecuteAsync(
                    """
                    insert into kb.recall (jurisdiction, authority, reference, product_id, gtin, description, classification, published_at)
                    values (@jurisdiction, @authority, @reference, @pid, @gtin, @description, @classification, @published)
                    on conflict (authority, reference, gtin) do update set product_id = coalesce(excluded.product_id, kb.recall.product_id)
                    """,
                    new { jurisdiction = r.Jurisdiction, authority = r.Authority, reference = r.Reference, pid, gtin = r.Gtin, description = r.Description,
                          classification = r.Classification, published = r.PublishedAt }, ct);
                if (pid is not null)
                {
                    await Outbox.EnqueueAsync(u, null, Topics.KbProductChanges, pid.Value.ToString(), new { productId = pid, recall = r.Reference }, ct);
                    await u.ExecuteAsync("insert into kb.curation_task (kind, priority, product_id, rule_code, payload) values ('RECALL_CONFIRM', 'P1', @pid, @reference, @payload::jsonb)",
                        new { pid, reference = r.Reference, payload = JsonSerializer.Serialize(new { r.Authority, r.Description, r.Classification }) }, ct);
                }
                return pid;
            }, ct);
            if (productId is not null) { matched++; log.LogWarning("Recall {Reference} matched product {Product}; suppressed from recommendations", r.Reference, productId); }
        }
        return matched;
    }
}
