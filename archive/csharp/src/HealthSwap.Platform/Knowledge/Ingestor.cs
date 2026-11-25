using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using HealthSwap.Domain;
using HealthSwap.Domain.Knowledge;
using HealthSwap.Domain.Scoring;
using HealthSwap.Platform.Bus;
using HealthSwap.Platform.Data;
using HealthSwap.Platform.Llm;
using HealthSwap.Platform.Ml;
using Microsoft.Extensions.Logging;

namespace HealthSwap.Platform.Knowledge;

public sealed record IngestReport(int Received, int Created, int NewVersions, int Unchanged, int Rejected, int CurationTasks, List<string> Messages);

/// <summary>
/// Knowledge-base ingestion (doc 02 sections 6-7, 10, 11): bronze copy with hash and licence, deterministic parsing, QA rules,
/// entity resolution, field-level survivorship with provenance, SCD-2 product versions and CDC events.
/// </summary>
public sealed class ProductIngestor(Db db, ReferenceDataRepository refs, ModelRegistry models, LlmGateway llm, IClock clock, LakeOptions lake, ILogger<ProductIngestor> log)
{
    private static readonly (string Code, Func<Nutrition, decimal?> Get)[] NutrientFields =
    [
        ("ENERC_KCAL", n => n.EnergyKcal), ("PROCNT", n => n.ProteinG), ("CHOCDF", n => n.CarbohydrateG), ("SUGAR", n => n.TotalSugarsG),
        ("SUGAD", n => n.AddedSugarsG), ("FAT", n => n.TotalFatG), ("FASAT", n => n.SaturatedFatG), ("FATRN", n => n.TransFatG),
        ("CHOLE", n => n.CholesterolMg), ("NA", n => n.Sodium), ("FIBTG", n => n.FibreG),
    ];

    private sealed class SourceRow { public short SourceId { get; set; } public short TrustTier { get; set; } public string LicenseCode { get; set; } = ""; }
    private sealed class ExistingProduct { public Guid ProductId { get; set; } public long? CurrentVersionId { get; set; } public string Name { get; set; } = ""; public string? Brand { get; set; } public string[] Markets { get; set; } = []; public string? PrimaryGtin { get; set; } public decimal? NetQuantity { get; set; } public string? NetQuantityUnit { get; set; } public string? LabelHashHex { get; set; } }
    private sealed class ProvRow { public string FieldPath { get; set; } = ""; public string ValueJson { get; set; } = ""; public short TrustTier { get; set; } public long SourceRecordId { get; set; } public DateTime FetchedAt { get; set; } }
    private sealed class FuzzyRow { public Guid ProductId { get; set; } public string Name { get; set; } = ""; public string? Brand { get; set; } public string? PrimaryGtin { get; set; } public decimal? NetQuantity { get; set; } public string? NetQuantityUnit { get; set; } public float Sim { get; set; } }

    public async Task<IngestReport> IngestAsync(IEnumerable<ProductCandidate> candidates, CancellationToken ct = default)
    {
        var taxonomy = await refs.GetTaxonomyAsync(ct);
        var categories = (await db.InTxAsync(DbRole.KbWriter, null, u => u.QueryAsync<string>("select code from kb.category", ct: ct), ct)).ToList();
        int received = 0, created = 0, versions = 0, unchanged = 0, rejected = 0, tasks = 0;
        var messages = new List<string>();
        foreach (var c in candidates)
        {
            received++;
            try
            {
                var r = await IngestOneAsync(c, taxonomy, categories, ct);
                if (r.Created) created++;
                if (r.NewVersion) versions++; else unchanged++;
                tasks += r.Tasks;
                messages.AddRange(r.Messages);
            }
            catch (HsException ex)
            {
                rejected++;
                messages.Add($"{c.SourceCode}:{c.ExternalId} rejected: {ex.Detail}");
            }
        }
        log.LogInformation("Ingest finished: {Received} received, {Created} created, {Versions} new versions, {Rejected} rejected", received, created, versions, rejected);
        return new IngestReport(received, created, versions, unchanged, rejected, tasks, messages);
    }

    private sealed record OneResult(bool Created, bool NewVersion, int Tasks, List<string> Messages);

    private async Task<OneResult> IngestOneAsync(ProductCandidate c, IngredientTaxonomy tax, List<string> categories, CancellationToken ct)
    {
        var now = clock.UtcNow;
        var messages = new List<string>();
        var taskList = new List<(string Kind, string Priority, string Rule, object Payload)>();

        // GTIN (QA-13) and restricted-circulation numbers (keyed by partner SKU instead).
        string? gtin = null;
        if (!string.IsNullOrWhiteSpace(c.RawGtin))
        {
            if (!Gtin.TryNormalize(c.RawGtin, out var g)) throw HsException.Invalid($"QA-13 invalid GTIN {c.RawGtin}");
            gtin = Gtin.IsRestrictedCirculation(g) ? null : g;
        }

        // Category: from the source, else LLM classification (T3, offline) with an allowed-ID enum and a confidence threshold.
        var category = c.CategoryCode is not null && categories.Contains(c.CategoryCode) ? c.CategoryCode : null;
        if (category is null)
        {
            var allowed = categories.Where(x => x.Count(ch => ch == '.') >= 1 && x != "infant-foods").ToList();
            var payload = JsonSerializer.Serialize(new { name = c.Name, brand = c.Brand, allowed });
            var r = await llm.RunAsync(PromptRegistry.CategoryClassifier(allowed), $"<product>{payload}</product>", ct);
            var code = r.Output?["categoryCode"]?.GetValue<string>();
            var conf = r.Output?["confidence"]?.GetValue<double>() ?? 0;
            var accepted = r.Ok && code is not null && allowed.Contains(code) && conf >= 0.8;
            await llm.AuditAsync(LlmTask.T3, r, payload, new Dictionary<string, bool> { ["schema"] = r.Ok, ["allowedId"] = code is not null && allowed.Contains(code), ["confidence"] = conf >= 0.8 }, accepted, ct);
            if (accepted) { category = code; messages.Add($"{c.ExternalId}: category {code} assigned by T3 ({conf:0.00})"); }
            else taskList.Add(("QA_REVIEW", "P2", "CATEGORY", new { reason = "Category could not be classified", suggestion = code, confidence = conf }));
        }

        // Deterministic parsing.
        var parsed = IngredientParser.Parse(c.IngredientsText, tax);
        var statement = AllergenDeriver.FromStatement(c.AllergenStatement);
        foreach (var (code, presence) in c.AllergenTags)
            if (!statement.TryGetValue(code, out var p) || p != AllergenPresence.Contains) statement[code] = presence;
        var allergens = AllergenDeriver.Reconcile(statement, parsed.AllergenHints, out var undeclared);
        var diet = DietClassifier.Classify(c.DietaryMarks, parsed.Flags, allergens);
        var (nova, novaConfidence) = NovaClassifier.Classify(parsed, tax);

        // QA rules as code.
        var tierA = new List<string>();
        foreach (var market in c.Markets)
        {
            var pack = await refs.GetPackAsync(market, ct);
            tierA.AddRange(parsed.Additives.Where(a => AdditiveTiers.Resolve(pack.Additives.GetValueOrDefault(a), market) == 'A'));
        }
        var nutrition = c.Nutrition ?? new Nutrition();
        if (parsed.FvlnPctEstimate is { } fvl && nutrition.FvlnPct is null)
            nutrition = nutrition with { FvlnPct = fvl, FvlnEstimated = !parsed.FvlnFromQuid };
        var findings = QaRules.Run(new QaInput
        {
            Nutrition = nutrition, PerServingEnergyKcal = c.PerServingEnergyKcal, ServingSize = c.ServingValue, RawGtin = c.RawGtin,
            DeclaredAllergens = statement, IngredientAllergens = parsed.AllergenHints.ToList(), VegMarkContradicted = diet.VegMarkContradicted,
            TierAAdditives = tierA.Distinct().ToList(), LastVerifiedAt = c.LastVerifiedAt, Now = now, SaltDeclared = c.SaltDeclared, SodiumDeclared = c.SodiumDeclared,
        });
        foreach (var f in findings.Where(f => f.Action == QaAction.RejectField))
            nutrition = f.Field switch
            {
                "nutrition.totalSugarsG" => nutrition with { TotalSugarsG = null },
                "nutrition.addedSugarsG" => nutrition with { AddedSugarsG = null },
                "nutrition.saturatedFatG" => nutrition with { SaturatedFatG = null, TransFatG = null },
                _ => nutrition,
            };
        foreach (var f in findings.Where(f => f.Action is QaAction.Review or QaAction.Quarantine))
            taskList.Add((f.Code == "QA-14" ? "COMPLIANCE" : "QA_REVIEW", f.Code is "QA-14" or "QA-11" ? "P1" : "P2", f.Code, new { f.Field, f.Message }));
        messages.AddRange(findings.Select(f => $"{c.ExternalId}: {f.Code} {f.Action} - {f.Message}"));

        var net = NetQuantity.Normalize(c.NetValue, c.NetUnit);
        var serving = NetQuantity.Normalize(c.ServingValue, c.ServingUnit);
        var quarantined = tierA.Count > 0;

        return await db.InTxAsync(DbRole.KbWriter, null, async u =>
        {
            var source = await u.QuerySingleAsync<SourceRow>("select source_id, trust_tier, license_code from kb.source where code = @code", new { code = c.SourceCode }, ct);
            var payloadBytes = Encoding.UTF8.GetBytes(c.RawJson);
            var sha = SHA256.HashData(payloadBytes);
            var uri = await WriteBronzeAsync(c, payloadBytes, sha, ct);
            var sourceRecordId = await u.ScalarAsync<long?>(
                """
                insert into kb.source_record (source_id, external_id, partner_id, fetched_at, payload_uri, payload_sha256)
                values (@sid, @ext, @partner, @at, @uri, @sha) on conflict (source_id, external_id, payload_sha256) do nothing returning source_record_id
                """, new { sid = source.SourceId, ext = c.ExternalId, partner = c.PartnerId, at = c.ObservedAt, uri, sha }, ct)
                ?? await u.ScalarAsync<long>("select source_record_id from kb.source_record where source_id = @sid and external_id = @ext and payload_sha256 = @sha",
                    new { sid = source.SourceId, ext = c.ExternalId, sha }, ct);

            // Entity resolution: GTIN exact (never merge different GTINs), then trigram blocking + pairwise model.
            ExistingProduct? existing = null;
            if (gtin is not null)
            {
                existing = await u.QueryFirstOrDefaultAsync<ExistingProduct>(
                    """
                    select p.product_id, p.current_version_id, p.name, b.name as brand, p.markets, p.primary_gtin, p.net_quantity, p.net_quantity_unit,
                           encode(v.label_hash, 'hex') as label_hash_hex
                    from kb.product p left join kb.brand b on b.brand_id = p.brand_id left join kb.product_version v on v.version_id = p.current_version_id
                    where p.primary_gtin = @gtin or p.product_id = (select product_id from kb.product_identifier where id_type = 'GTIN' and id_value = @gtin)
                    limit 1
                    """, new { gtin }, ct);
                if (existing is not null && existing.Brand is not null && c.Brand is not null &&
                    TextSimilarity.TrigramJaccard(existing.Brand, c.Brand) < 0.2 && TextSimilarity.TrigramJaccard(existing.Name, c.Name) < 0.2)
                    taskList.Add(("ER_REVIEW", "P1", "GTIN_CONFLICT", new { gtin, existing = existing.Name, incoming = c.Name }));
            }
            else
            {
                var fuzzy = (await u.QueryAsync<FuzzyRow>(
                    """
                    select p.product_id, p.name, b.name as brand, p.primary_gtin, p.net_quantity, p.net_quantity_unit, similarity(p.name, @name) as sim
                    from kb.product p left join kb.brand b on b.brand_id = p.brand_id
                    where p.status <> 'MERGED' and similarity(p.name, @name) > 0.3 order by sim desc limit 5
                    """, new { name = c.Name }, ct)).ToList();
                foreach (var f in fuzzy)
                {
                    var features = EntityResolution.Features(
                        new EntityResolution.Candidate(c.Name, c.Brand, net, c.Nutrition, parsed.Tokens.Select(t => t.CanonicalName ?? t.RawText).ToList(), null),
                        new EntityResolution.Candidate(f.Name, f.Brand, NetQuantity.Normalize(f.NetQuantity, f.NetQuantityUnit), null, [], f.PrimaryGtin));
                    var score = models.ScoreEntityMatch(features);
                    var decision = EntityResolution.Decide(score);
                    if (decision == "AUTO_MERGE")
                    {
                        existing = await u.QueryFirstOrDefaultAsync<ExistingProduct>(
                            "select p.product_id, p.current_version_id, p.name, null as brand, p.markets, p.primary_gtin, p.net_quantity, p.net_quantity_unit, encode(v.label_hash, 'hex') as label_hash_hex from kb.product p left join kb.product_version v on v.version_id = p.current_version_id where p.product_id = @id",
                            new { id = f.ProductId }, ct);
                        messages.Add($"{c.ExternalId}: ER auto-merge into {f.ProductId} ({score:0.000})");
                        break;
                    }
                    if (decision == "REVIEW")
                        taskList.Add(("ER_REVIEW", "P2", $"ER-{score:0.00}", new { incoming = c.Name, candidate = f.Name, candidateId = f.ProductId, score }));
                }
            }

            var brandId = c.Brand is null ? (Guid?)null : await u.QuerySingleAsync<Guid>(
                """
                insert into kb.brand (name, normalized_name, owner_company) values (@name, @norm, @owner)
                on conflict (normalized_name, owner_company) do update set name = excluded.name returning brand_id
                """, new { name = c.Brand, norm = IngredientTaxonomy.Normalize(c.Brand), owner = c.BrandOwner }, ct);
            var categoryId = category is null ? (Guid?)null : await u.ScalarAsync<Guid?>("select category_id from kb.category where code = @category", new { category }, ct);
            var attributes = JsonSerializer.Serialize(c.Attributes.Count > 0 ? c.Attributes : new Dictionary<string, string> { ["brandTier"] = c.BrandTier });

            var createdProduct = existing is null;
            var productId = existing?.ProductId ?? Guid.CreateVersion7();
            if (createdProduct)
            {
                await u.ExecuteAsync(
                    """
                    insert into kb.product (product_id, primary_gtin, brand_id, category_id, name, net_quantity, net_quantity_unit, pack_count, physical_state,
                                            markets, is_private_label, status, attributes)
                    values (@productId, @gtin, @brandId, @categoryId, @name, @netValue, @netUnit, @pack, @state, @markets, @pl, @status, @attributes::jsonb)
                    """,
                    new { productId, gtin, brandId, categoryId, name = c.Name, netValue = net?.Value, netUnit = net?.Unit, pack = Math.Max(1, c.PackCount),
                          state = c.PhysicalState, markets = c.Markets.ToArray(), pl = c.IsPrivateLabel, status = quarantined ? "QUARANTINED" : "ACTIVE", attributes }, ct);
                if (gtin is not null)
                    await u.ExecuteAsync("insert into kb.product_identifier (id_type, id_value, product_id) values ('GTIN', @gtin, @productId) on conflict do nothing", new { gtin, productId }, ct);
            }
            else
            {
                // Higher-trust sources may refine identity attributes; markets are unioned. Lower tiers never overwrite.
                await u.ExecuteAsync(
                    """
                    update kb.product set markets = (select array(select distinct unnest(markets || @markets))),
                        category_id = coalesce(category_id, @categoryId), brand_id = coalesce(brand_id, @brandId),
                        attributes = case when @tier <= 2 then @attributes::jsonb else attributes end,
                        status = case when @quarantined then 'QUARANTINED' else status end, updated_at = now()
                    where product_id = @productId
                    """, new { markets = c.Markets.ToArray(), categoryId, brandId, attributes, tier = (int)source.TrustTier, quarantined, productId }, ct);
            }
            var idType = c.SourceCode switch { "OFF" => "OFF_CODE", "USDA_FDC_BRANDED" => "FDC_ID", _ => null };
            if (idType is not null)
                await u.ExecuteAsync("insert into kb.product_identifier (id_type, id_value, product_id) values (@idType, @ext, @productId) on conflict do nothing",
                    new { idType, ext = c.ExternalId, productId }, ct);

            // Field-level survivorship against the current version's selected values.
            var current = existing?.CurrentVersionId is { } cv
                ? (await u.QueryAsync<ProvRow>(
                    """
                    select fp.field_path, fp.value_json::text as value_json, fp.trust_tier, fp.source_record_id, sr.fetched_at
                    from kb.field_provenance fp join kb.source_record sr on sr.source_record_id = fp.source_record_id
                    where fp.version_id = @cv and fp.is_selected
                    """, new { cv }, ct)).ToDictionary(r => r.FieldPath)
                : new Dictionary<string, ProvRow>();
            var incoming = new Dictionary<string, (string Json, decimal? Number)>();
            foreach (var (code, get) in NutrientFields)
                if (get(nutrition) is { } v) incoming[$"nutrient.{code}.{nutrition.Basis}"] = (JsonSerializer.Serialize(v), v);
            if (c.IngredientsText is not null) incoming["ingredients_text"] = (JsonSerializer.Serialize(c.IngredientsText), null);
            if (c.AllergenStatement is not null || c.AllergenTags.Count > 0)
                incoming["allergens"] = (JsonSerializer.Serialize(allergens.OrderBy(a => a.Key).ToDictionary(a => a.Key, a => a.Value.ToString())), null);
            if (serving is not null) incoming["serving_size"] = (JsonSerializer.Serialize(serving.Value.Value), serving.Value.Value);

            var selected = new Dictionary<string, (string Json, long SourceRecordId, int Tier, bool Conflict)>();
            foreach (var (field, prov) in current) selected[field] = (prov.ValueJson, prov.SourceRecordId, prov.TrustTier, false);
            var conflicts = 0;
            foreach (var (field, value) in incoming)
            {
                if (!selected.TryGetValue(field, out var existingValue)) { selected[field] = (value.Json, sourceRecordId, source.TrustTier, false); continue; }
                var prov = current[field];
                var decision = Survivorship.Select(field, [
                    new Survivorship.Candidate(field, Num(prov.ValueJson), prov.ValueJson, prov.TrustTier, prov.FetchedAt, prov.SourceRecordId, true),
                    new Survivorship.Candidate(field, value.Number, value.Json, source.TrustTier, c.ObservedAt, sourceRecordId, true),
                ]);
                if (decision.Selected!.SourceRecordId == sourceRecordId) selected[field] = (value.Json, sourceRecordId, source.TrustTier, decision.Conflict);
                else if (decision.Conflict) selected[field] = existingValue with { Conflict = true };
                if (decision.Conflict)
                {
                    conflicts++;
                    taskList.Add(("FIELD_CONFLICT", "P2", field, new { field, selected = decision.Selected.Text, challenger = decision.Selected.SourceRecordId == sourceRecordId ? prov.ValueJson : value.Json, decision.Reason }));
                }
            }

            // SCD-2 versioning by normalized label hash.
            var merged = MergeNutrition(selected, nutrition.Basis, nutrition.CarbDefinition, nutrition);
            var ingredientsText = selected.TryGetValue("ingredients_text", out var it) ? JsonSerializer.Deserialize<string>(it.Json) : c.IngredientsText;
            var labelCanonical = JsonSerializer.Serialize(new
            {
                n = selected.Where(kv => kv.Key.StartsWith("nutrient.", StringComparison.Ordinal)).OrderBy(kv => kv.Key).Select(kv => kv.Key + "=" + kv.Value.Json),
                ingredientsText, allergens = selected.GetValueOrDefault("allergens").Json, c.DietaryMarks, net = net?.Value, serving = serving?.Value,
            });
            var labelHash = SHA256.HashData(Encoding.UTF8.GetBytes(labelCanonical));
            var labelHex = Convert.ToHexStringLower(labelHash);
            var isNewVersion = existing?.LabelHashHex != labelHex;
            long versionId;
            if (isNewVersion)
            {
                if (existing?.CurrentVersionId is { } prev)
                    await u.ExecuteAsync("update kb.product_version set valid_to = @now where version_id = @prev", new { now, prev }, ct);
                // Re-parse the surviving ingredient text (it may come from a higher-tier source than this candidate).
                var finalParsed = ingredientsText == c.IngredientsText ? parsed : IngredientParser.Parse(ingredientsText, tax);
                var allergenConfidence = c.Verification is "BRAND_VERIFIED" or "CURATOR_VERIFIED" ? 1.0m : c.AllergenStatement is not null ? 0.9m : c.AllergenTags.Count > 0 ? 0.85m : 0.6m;
                var tierConfidence = source.TrustTier switch { 1 => 1.0m, 2 => 0.95m, 3 => 0.9m, 4 => 0.85m, _ => 0.7m };
                var confidence = Math.Max(0.5m, tierConfidence - 0.05m * findings.Count(f => f.Action == QaAction.Review));
                var sufficiency = DataSufficiencyRules.Evaluate(merged, finalParsed.ResolutionRatio, ingredientsText is not null, allergenConfidence,
                    c.Verification is "BRAND_VERIFIED" or "CURATOR_VERIFIED", categoryId is not null || existing is not null, net is not null || existing?.NetQuantity is not null);
                var completeness = Math.Round((decimal)NutrientFields.Count(f => f.Get(merged) is not null) / NutrientFields.Length, 3);
                versionId = await u.QuerySingleAsync<long>(
                    """
                    insert into kb.product_version (product_id, valid_from, label_hash, serving_size, serving_unit, carb_definition, ingredients_text,
                        ingredients_lang, allergen_statement, nova_group, nova_confidence, completeness, confidence, data_sufficiency, verification, last_verified_at)
                    values (@productId, @now, @labelHash, @servingSize, @servingUnit, @carb, @ingredientsText, @lang, @statement, @nova, @novaConfidence,
                        @completeness, @confidence, @sufficiency, @verification, @lastVerified)
                    returning version_id
                    """,
                    new { productId, now, labelHash, servingSize = serving?.Value, servingUnit = serving?.Unit is "g" or "ml" ? serving?.Unit : null, carb = merged.CarbDefinition,
                          ingredientsText, lang = ingredientsText is not null && ingredientsText.Any(ch => ch is >= '\u0900' and <= '\u097F') ? "hi" : "en",
                          statement = c.AllergenStatement, nova, novaConfidence, completeness, confidence, sufficiency, verification = c.Verification,
                          lastVerified = c.LastVerifiedAt }, ct);
                await u.ExecuteAsync("update kb.product set current_version_id = @versionId, updated_at = now() where product_id = @productId", new { versionId, productId }, ct);

                foreach (var (code, get) in NutrientFields)
                    if (get(merged) is { } amount)
                    {
                        var src = selected.TryGetValue($"nutrient.{code}.{merged.Basis}", out var s) ? s.SourceRecordId : sourceRecordId;
                        var declared = !(code == "NA" && src == sourceRecordId && c.SaltDeclared && !c.SodiumDeclared);
                        await u.ExecuteAsync(
                            "insert into kb.product_nutrient (version_id, nutrient_code, basis, amount, is_declared, source_record_id, confidence) values (@versionId, @code, @basis, @amount, @declared, @src, @conf)",
                            new { versionId, code, basis = merged.Basis, amount, declared, src, conf = confidence }, ct);
                    }
                if (merged.FvlnPct is { } fvlPct)
                    await u.ExecuteAsync(
                        "insert into kb.product_nutrient (version_id, nutrient_code, basis, amount, is_declared, is_estimated, source_record_id, confidence) values (@versionId, 'FVLN_PCT', @basis, @fvlPct, false, @est, @src, 0.8)",
                        new { versionId, basis = merged.Basis, fvlPct, est = merged.FvlnEstimated, src = sourceRecordId }, ct);
                foreach (var t in finalParsed.Tokens.Where(t => !t.RawText.StartsWith("class:", StringComparison.Ordinal)))
                    await u.ExecuteAsync(
                        """
                        insert into kb.product_ingredient (version_id, position, parent_position, ingredient_id, raw_text, percent)
                        values (@versionId, @pos, @parent, (select ingredient_id from kb.ingredient where canonical_name = @canonical), @raw, @pct)
                        """, new { versionId, pos = (short)t.Position, parent = (short?)t.ParentPosition, canonical = t.CanonicalName, raw = t.RawText.Length > 0 ? t.RawText : "?", pct = t.Percent is > 100 ? null : t.Percent }, ct);
                foreach (var code in finalParsed.Additives)
                    await u.ExecuteAsync(
                        "insert into kb.product_additive (version_id, additive_id, detected_from) select @versionId, additive_id, 'INGREDIENTS' from kb.additive where ins_number = @code on conflict do nothing",
                        new { versionId, code }, ct);
                foreach (var (code, presence) in allergens)
                    await u.ExecuteAsync(
                        "insert into kb.product_allergen (version_id, allergen_code, presence, derived_from, verified) values (@versionId, @code, @presence, @from, @verified) on conflict do nothing",
                        new { versionId, code, presence = presence switch { AllergenPresence.Contains => "CONTAINS", AllergenPresence.MayContain => "MAY_CONTAIN", _ => "FREE_FROM" },
                              from = statement.ContainsKey(code) ? c.Verification == "BRAND_VERIFIED" ? "BRAND" : "STATEMENT" : "INGREDIENTS", verified = c.Verification is "BRAND_VERIFIED" or "CURATOR_VERIFIED" }, ct);
                var marks = c.DietaryMarks.ToList();
                if (diet.JainFriendlyInferred) marks.Add("JAIN_FRIENDLY");
                foreach (var mark in marks.Distinct())
                    await u.ExecuteAsync(
                        "insert into kb.product_mark (version_id, mark_code, is_inferred, verified) select @versionId, mark_code, @inferred, @verified from kb.mark where mark_code = @mark on conflict do nothing",
                        new { versionId, mark, inferred = mark == "JAIN_FRIENDLY" || c.MarksInferred, verified = c.Verification == "BRAND_VERIFIED" && mark != "JAIN_FRIENDLY" }, ct);
                foreach (var (field, sel) in selected)
                    await u.ExecuteAsync(
                        """
                        insert into kb.field_provenance (version_id, field_path, source_record_id, value_json, trust_tier, license_code, is_selected, conflict)
                        select @versionId, @field, @src, @value::jsonb, @tier, s.license_code, true, @conflict
                        from kb.source_record r join kb.source s using (source_id) where r.source_record_id = @src
                        on conflict do nothing
                        """, new { versionId, field, src = sel.SourceRecordId, value = sel.Json, tier = (short)sel.Tier, conflict = sel.Conflict }, ct);
                await Outbox.EnqueueAsync(u, null, Topics.KbProductChanges, productId.ToString(), new { productId, versionId, gtin, source = c.SourceCode, created = createdProduct }, ct);
            }
            else versionId = existing!.CurrentVersionId!.Value;

            // Keep every candidate value (also non-selected ones) for audits and dispute resolution.
            foreach (var (field, value) in incoming)
                await u.ExecuteAsync(
                    """
                    insert into kb.field_provenance (version_id, field_path, source_record_id, value_json, trust_tier, license_code, is_selected, conflict)
                    values (@versionId, @field, @src, @value::jsonb, @tier, @license, false, false) on conflict do nothing
                    """, new { versionId, field, src = sourceRecordId, value = value.Json, tier = source.TrustTier, license = source.LicenseCode }, ct);

            foreach (var (kind, priority, rule, payload) in taskList)
                await u.ExecuteAsync("insert into kb.curation_task (kind, priority, product_id, version_id, rule_code, payload) values (@kind, @priority, @productId, @versionId, @rule, @payload::jsonb)",
                    new { kind, priority, productId, versionId, rule, payload = JsonSerializer.Serialize(payload) }, ct);
            return new OneResult(createdProduct, isNewVersion, taskList.Count, messages);
        }, ct);
    }

    private static decimal? Num(string json)
    {
        var node = JsonNode.Parse(json);
        return node is JsonValue v && v.TryGetValue<decimal>(out var d) ? d : null;
    }

    private static Nutrition MergeNutrition(Dictionary<string, (string Json, long, int, bool)> selected, string basis, string carbDefinition, Nutrition fallback)
    {
        decimal? Get(string code) => selected.TryGetValue($"nutrient.{code}.{basis}", out var v) ? Num(v.Json) : null;
        return new Nutrition
        {
            Basis = basis, CarbDefinition = carbDefinition, EnergyKcal = Get("ENERC_KCAL"), ProteinG = Get("PROCNT"), CarbohydrateG = Get("CHOCDF"),
            TotalSugarsG = Get("SUGAR"), AddedSugarsG = Get("SUGAD"), TotalFatG = Get("FAT"), SaturatedFatG = Get("FASAT"), TransFatG = Get("FATRN"),
            CholesterolMg = Get("CHOLE"), SodiumMg = Get("NA"), FibreG = Get("FIBTG"), FvlnPct = fallback.FvlnPct, FvlnEstimated = fallback.FvlnEstimated,
        };
    }

    private async Task<string> WriteBronzeAsync(ProductCandidate c, byte[] payload, byte[] sha, CancellationToken ct)
    {
        var rel = Path.Combine("bronze", "sources", c.SourceCode, clock.UtcNow.ToString("yyyy-MM-dd", CultureInfo.InvariantCulture), Convert.ToHexStringLower(sha)[..16] + ".json");
        if (lake.Enabled)
        {
            var full = Path.Combine(lake.Root, rel);
            Directory.CreateDirectory(Path.GetDirectoryName(full)!);
            if (!File.Exists(full)) await File.WriteAllBytesAsync(full, payload, ct);
        }
        return "lake://" + rel.Replace('\\', '/');
    }
}
