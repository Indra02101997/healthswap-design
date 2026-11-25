using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Domain.Knowledge;
using HealthSwap.Domain.Recommendation;
using HealthSwap.Domain.Scoring;
using HealthSwap.Platform.Cache;
using HealthSwap.Platform.Data;
using HealthSwap.Platform.Knowledge;
using HealthSwap.Platform.Llm;
using HealthSwap.Platform.Services;
using Microsoft.Extensions.Logging;

namespace HealthSwap.Platform.Seeding;

/// <summary>
/// Training-data exports for ml/ (substitutability, entity resolution, ranker, LLM fine-tuning) and the evaluation harness for
/// fine-tuned LLM deployments. Features are produced by the same Domain code used at serving time, so there is no train/serve skew.
/// </summary>
public sealed class MlData(Db db, ReferenceDataRepository refs, HotCache cache, EvaluationService evaluations, ExplanationPregen pregen, LlmGateway llm,
    PartnerDirectory partners, ILogger<MlData> log)
{
    private static readonly JsonSerializerOptions Line = new(HotCache.Json) { WriteIndented = false };

    private async Task<List<HealthProfileData>> ProfilesAsync(CancellationToken ct) =>
        (await db.InTxAsync(DbRole.KbWriter, null, u => u.QueryAsync<string>("select profile_doc::text from rec.health_profile order by market, product_id", ct: ct), ct))
        .Select(d => JsonSerializer.Deserialize<HealthProfileData>(d, HotCache.Json)!).ToList();

    public async Task<Dictionary<string, int>> ExportAsync(string outDir, CancellationToken ct)
    {
        Directory.CreateDirectory(outDir);
        var profiles = await ProfilesAsync(ct);
        var counts = new Dictionary<string, int>();
        var maps = (await db.InTxAsync(DbRole.KbWriter, null, u => u.QueryAsync<(string Market, string From, string To)>(
            "select s.market, f.code, t.code from rec.category_substitution s join kb.category f on f.category_id = s.from_category_id join kb.category t on t.category_id = s.to_category_id",
            ct: ct), ct)).ToList();

        // 1. Substitutability pairs (doc 03 section 6). Labels: synthetic "gold" from curated attributes (form, use occasion, diet class,
        //    pack size) - replace with curator-labelled pairs and T4 judgments in production.
        var pairs = 0;
        await using (var w = new StreamWriter(Path.Combine(outDir, "substitutability_pairs.jsonl")))
            foreach (var market in profiles.GroupBy(p => p.Market))
                foreach (var a in market)
                    foreach (var b in market.Where(b => b.ProductId != a.ProductId))
                    {
                        var approved = maps.Any(m => m.Market == market.Key && m.From == a.CategoryCode && m.To == b.CategoryCode);
                        var distance = Substitutability.CategoryDistance(a.CategoryCode, b.CategoryCode, approved);
                        if (distance > 2 && Random.Shared.NextDouble() > 0.15) continue; // keep some far negatives
                        var f = Substitutability.Features(a, b, distance);
                        var label = GoldSubstitutable(a, b, distance) ? 1 : 0;
                        await w.WriteLineAsync(JsonSerializer.Serialize(new { market = market.Key, from = a.ProductId, to = b.ProductId, fromName = a.Name, toName = b.Name, features = f, label, heuristic = Substitutability.Heuristic(f) }, Line));
                        pairs++;
                    }
        counts["substitutability_pairs"] = pairs;

        // 2. Entity-resolution pairs (doc 02 7.3): noisy listing variants of the same product vs different products in the same category.
        var er = 0;
        var rng = new Random(42);
        await using (var w = new StreamWriter(Path.Combine(outDir, "er_pairs.jsonl")))
            foreach (var p in profiles.DistinctBy(p => p.ProductId))
            {
                var original = new EntityResolution.Candidate(p.Name, p.Brand, p.NetQuantity, p.Nutrition, p.IngredientFlags, p.Gtin);
                for (var i = 0; i < 4; i++)
                {
                    var variant = new EntityResolution.Candidate(Noisy(p.Name, rng), rng.NextDouble() < 0.2 ? null : p.Brand, rng.NextDouble() < 0.1 ? null : p.NetQuantity,
                        rng.NextDouble() < 0.5 ? null : p.Nutrition, p.IngredientFlags, null);
                    await w.WriteLineAsync(JsonSerializer.Serialize(new { a = p.Name, b = variant.Name, features = EntityResolution.Features(original, variant), label = 1 }, Line));
                    er++;
                }
                foreach (var other in profiles.Where(o => o.ProductId != p.ProductId && o.CategoryCode == p.CategoryCode).DistinctBy(o => o.ProductId).Take(4))
                {
                    var candidate = new EntityResolution.Candidate(other.Name, other.Brand, other.NetQuantity, other.Nutrition, other.IngredientFlags, rng.NextDouble() < 0.3 ? other.Gtin : null);
                    await w.WriteLineAsync(JsonSerializer.Serialize(new { a = p.Name, b = other.Name, features = EntityResolution.Features(original, candidate with { Gtin = candidate.Gtin is null ? null : p.Gtin }), label = 0 }, Line));
                    er++;
                }
            }
        counts["er_pairs"] = er;

        // 3. LLM fine-tuning sources: explanation fact sheets (T5), substitutability attributes (T4), product titles (T3).
        var expl = 0;
        await using (var w = new StreamWriter(Path.Combine(outDir, "explanation_facts.jsonl")))
            foreach (var market in profiles.GroupBy(p => p.Market))
            {
                var pack = await refs.GetPackAsync(market.Key, ct);
                var byId = market.ToDictionary(p => p.ProductId);
                var edges = await db.InTxAsync(DbRole.KbWriter, null, u => u.QueryAsync<(Guid From, Guid To)>(
                    "select e.from_product_id, e.to_product_id from rec.v_active_swap_edge e join rec.swap_graph_version g using (graph_version) where g.market = @m", new { m = market.Key }, ct), ct);
                foreach (var (from, to) in edges)
                {
                    if (!byId.TryGetValue(from, out var a) || !byId.TryGetValue(to, out var b)) continue;
                    var sheet = FactSheets.Build(a, b, pack);
                    var selected = FactSheets.Select(sheet);
                    if (selected.Count == 0) continue;
                    foreach (var locale in ApiMapper.LocalesFor(market.Key))
                    {
                        var template = ExplanationTemplates.Render(selected, locale);
                        await w.WriteLineAsync(JsonSerializer.Serialize(new { market = market.Key, locale, basis = sheet.Basis, facts = selected, template = new { template.Short, template.Bullets } }, Line));
                        expl++;
                    }
                }
            }
        counts["explanation_facts"] = expl;

        await using (var w = new StreamWriter(Path.Combine(outDir, "category_examples.jsonl")))
            foreach (var p in profiles.DistinctBy(p => p.ProductId))
                await w.WriteLineAsync(JsonSerializer.Serialize(new { name = p.Name, brand = p.Brand, market = p.Market, category = p.CategoryCode }, Line));
        counts["category_examples"] = profiles.DistinctBy(p => p.ProductId).Count();

        await using (var w = new StreamWriter(Path.Combine(outDir, "products.jsonl")))
            foreach (var p in profiles)
                await w.WriteLineAsync(JsonSerializer.Serialize(new
                {
                    p.Market, p.ProductId, p.Name, p.Brand, p.CategoryCode, p.Hsi, p.HsiGrade, p.Nova, p.Flags, p.Form, p.FlavourFamily, p.UseOccasion, p.DietClass,
                    netQuantity = p.NetQuantity?.Value, netUnit = p.NetQuantity?.Unit, p.PhysicalState, p.BrandTier,
                }, Line));
        counts["products"] = profiles.Count;
        log.LogInformation("Exported ML data to {Dir}: {Counts}", outDir, string.Join(", ", counts.Select(kv => $"{kv.Key}={kv.Value}")));
        return counts;
    }

    /// <summary>Synthetic gold label: same form and use occasion, compatible diet class, pack within 0.5-2x, or an approved cross-category map.</summary>
    public static bool GoldSubstitutable(HealthProfileData a, HealthProfileData b, int distance)
    {
        if (distance > 2 || a.PhysicalState != b.PhysicalState) return false;
        var vegA = a.DietClass is "VEG" or "VEGAN";
        var vegB = b.DietClass is "VEG" or "VEGAN";
        if (vegA != vegB) return false;
        if (a.NetQuantity is { } sa && b.NetQuantity is { } sb && sa.IsComparableTo(sb) && sa.Value > 0 && (sb.Value / sa.Value is < 0.5m or > 2m)) return false;
        if (a.UseOccasion is not null && b.UseOccasion is not null && a.UseOccasion != b.UseOccasion) return false;
        if (distance == 2) return true;
        return a.Form is null || b.Form is null || a.Form == b.Form;
    }

    private static string Noisy(string name, Random rng)
    {
        var words = name.Split(' ', StringSplitOptions.RemoveEmptyEntries).ToList();
        switch (rng.Next(5))
        {
            case 0: return name.ToUpperInvariant();
            case 1: return string.Join(' ', words.Select(x => x.Equals("Pack", StringComparison.OrdinalIgnoreCase) ? "Pk" : x.Replace("Whole Wheat", "WW")));
            case 2: if (words.Count > 3) (words[1], words[2]) = (words[2], words[1]); return string.Join(' ', words);
            case 3: return string.Join(' ', words.Where(x => !x.Any(char.IsDigit))) + " " + string.Join("", words.Where(x => x.Any(char.IsDigit)));
            default: return name.Replace(" g", "g").Replace(" ml", "ml") + " (Pack of 1)";
        }
    }

    /// <summary>
    /// Simulates shopper sessions against the real evaluation pipeline and logs ranker training rows with a synthetic acceptance
    /// model (impressions are positional; acceptance depends on substitutability, health gain, price and rank).
    /// </summary>
    public async Task<int> SimulateAsync(Guid partnerId, int sessions, string outFile, int seed, CancellationToken ct)
    {
        var partner = await partners.GetAsync(partnerId, ct);
        var market = partner.Markets[0];
        var currency = partner.Config.Currency;
        var catalog = (await db.InTxAsync(DbRole.App, partnerId, u => u.QueryAsync<(string Sku, string StoreId, decimal Price)>(
            "select i.partner_sku, i.store_id, i.price from tenant.store_inventory i join tenant.catalog_item c on c.partner_id = i.partner_id and c.partner_sku = i.partner_sku where i.in_stock and c.is_listed",
            ct: ct), ct)).ToList();
        if (catalog.Count == 0) throw new InvalidOperationException("Seed the demo partner first (no in-stock catalog items)");
        var rng = new Random(seed);
        var ctx = new PartnerContext { PartnerId = partnerId, ClientId = "simulator", Scopes = new HashSet<string> { "carts.evaluate" } };
        Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(outFile))!);
        await using var w = new StreamWriter(outFile);
        var rows = 0;
        for (var s = 0; s < sessions; s++)
        {
            var store = catalog[rng.Next(catalog.Count)].StoreId;
            var picks = catalog.Where(c => c.StoreId == store).OrderBy(_ => rng.Next()).Take(rng.Next(1, 5)).ToList();
            var placement = new[] { "CART", "CHECKOUT", "CART", "AGENT" }[rng.Next(4)];
            var result = await evaluations.EvaluateAsync(ctx, new CartEvaluationRequest
            {
                CartRef = $"sim-{seed}-{s}", Market = market, Currency = currency, StoreId = store, Context = new CartContext { Placement = placement },
                Lines = picks.Select((p, i) => new CartLine { LineId = (i + 1).ToString(), PartnerSku = p.Sku, Quantity = rng.Next(1, 3), UnitPrice = ApiMapper.Money(p.Price, currency) }).ToList(),
            }, ct);
            foreach (var sug in result.Suggestions)
            {
                var snapshot = await cache.GetSuggestionAsync(sug.SuggestionId);
                foreach (var alt in sug.Alternatives)
                {
                    var features = snapshot?.Alternatives.FirstOrDefault(a => a.Rank == alt.Rank)?.Features;
                    if (features is null) continue;
                    // Hidden "true" acceptance model: substitutability, health gain, price premium, brand loyalty and position.
                    var z = -1.2 + 2.2 * features[1] + 0.035 * features[0] - 1.6 * Math.Max(0, features[2] - 1) + 0.4 * features[15] - 0.55 * (alt.Rank - 1)
                            + (placement == "CHECKOUT" ? -0.2 : 0) + 0.3 * features[12];
                    var p = 1 / (1 + Math.Exp(-z));
                    var accepted = rng.NextDouble() < p ? 1 : 0;
                    await w.WriteLineAsync(JsonSerializer.Serialize(new { suggestionId = sug.SuggestionId, rank = alt.Rank, placement, features, label = accepted, propensity = 1.0 }, Line));
                    rows++;
                }
            }
        }
        log.LogInformation("Simulated {Sessions} sessions, {Rows} ranker rows -> {File}", sessions, rows, outFile);
        return rows;
    }

    public sealed record EvalReport(int Examples, int Accepted, double AcceptanceRate, Dictionary<string, int> FailedChecks, double MeanLatencyMs, int TokensIn, int TokensOut, string ModelRef);

    /// <summary>
    /// Fine-tuned T5 evaluation through the production validator chain (schema, numeric fidelity, banned claims, language,
    /// mandatory disclosures). Input lines come from explanation_facts.jsonl (or a held-out split of the fine-tuning set).
    /// </summary>
    public async Task<EvalReport> EvaluateExplanationsAsync(string inputFile, int max, CancellationToken ct)
    {
        int n = 0, ok = 0, tin = 0, tout = 0;
        long latency = 0;
        var failed = new Dictionary<string, int>();
        var modelRef = "";
        foreach (var line in File.ReadLines(inputFile).Take(max))
        {
            using var doc = JsonDocument.Parse(line);
            var root = doc.RootElement;
            var facts = root.GetProperty("facts").Deserialize<List<Fact>>(HotCache.Json)!;
            var locale = root.GetProperty("locale").GetString()!;
            var basis = root.TryGetProperty("basis", out var b) ? b.GetString()! : "PER_100G";
            var (text, accepted, result) = await pregen.PolishAsync(facts, basis, locale, ct);
            n++;
            tin += result.TokensIn;
            tout += result.TokensOut;
            latency += result.LatencyMs;
            modelRef = result.ModelRef;
            if (accepted) { ok++; continue; }
            if (result.Output is System.Text.Json.Nodes.JsonObject o)
            {
                var report = ExplanationValidator.Validate(o["short"]?.GetValue<string>() ?? "", o["bullets"]?.AsArray().Select(x => x?.GetValue<string>() ?? "").ToList() ?? [], facts, locale);
                foreach (var (check, passed) in report.Checks) if (!passed) failed[check] = failed.GetValueOrDefault(check) + 1;
            }
            else failed["schema"] = failed.GetValueOrDefault("schema") + 1;
            _ = text;
        }
        return new EvalReport(n, ok, n == 0 ? 0 : Math.Round((double)ok / n, 4), failed, n == 0 ? 0 : Math.Round((double)latency / n, 1), tin, tout, modelRef);
    }

    public string Provider => llm.ProviderName;
}
