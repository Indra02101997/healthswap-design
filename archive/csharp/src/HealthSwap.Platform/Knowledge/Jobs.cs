using System.Globalization;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using HealthSwap.Domain;
using HealthSwap.Domain.Recommendation;
using HealthSwap.Domain.Scoring;
using HealthSwap.Platform.Bus;
using HealthSwap.Platform.Cache;
using HealthSwap.Platform.Data;
using HealthSwap.Platform.Llm;
using HealthSwap.Platform.Ml;
using Microsoft.Extensions.Logging;

namespace HealthSwap.Platform.Knowledge;

public sealed record ScoringReport(string Market, int Products, int Eligible, int Changed, int Categories);

/// <summary>
/// Scoring service (HLD 6.3): jurisdiction models, NOVA, additive tiers and HSI per product and market; writes
/// rec.product_score, rec.health_profile (read model) and rec.category_stats (p60 ceilings), then refreshes the hot cache.
/// </summary>
public sealed class ScoringJob(Db db, KbReader reader, ReferenceDataRepository refs, HotCache cache, IClock clock, ILogger<ScoringJob> log)
{
    private sealed class PrevRow { public Guid ProductId { get; set; } public string ProfileVersion { get; set; } = ""; public string ProfileDoc { get; set; } = "{}"; }
    private sealed class ModelIdRow { public Guid ModelId { get; set; } public string Code { get; set; } = ""; public string Version { get; set; } = ""; }

    public async Task<List<ScoringReport>> RunAllAsync(CancellationToken ct = default)
    {
        var reports = new List<ScoringReport>();
        foreach (var market in await refs.MarketsAsync(ct)) reports.Add(await RunAsync(market, ct));
        return reports;
    }

    public async Task<ScoringReport> RunAsync(string market, CancellationToken ct = default)
    {
        refs.Invalidate();
        var pack = await refs.GetPackAsync(market, ct);
        var products = await reader.LoadAsync(market, null, ct);
        var now = clock.UtcNow;
        var profiles = products.Select(p => HealthProfileBuilder.Build(p.Data, pack, now, p.CategoryName, p.Sources)).ToList();

        var changed = 0;
        var categories = 0;
        await db.InTxAsync(DbRole.KbWriter, null, async u =>
        {
            var previous = (await u.QueryAsync<PrevRow>("select product_id, profile_version, profile_doc::text as profile_doc from rec.health_profile where market = @market",
                new { market }, ct)).ToDictionary(r => r.ProductId);
            var modelIds = (await u.QueryAsync<ModelIdRow>("select model_id, code, version from rec.scoring_model where status = 'ACTIVE'", ct: ct))
                .ToDictionary(r => r.Code + "@" + r.Version, r => r.ModelId);
            foreach (var hp in profiles)
            {
                foreach (var s in hp.Scores)
                    if (modelIds.TryGetValue(s.Code + "@" + s.Version, out var modelId))
                        await u.ExecuteAsync(
                            """
                            insert into rec.product_score (version_id, model_id, market, score, grade, components, inputs_hash, computed_at)
                            values (@v, @modelId, @market, @score, @grade, @components::jsonb, @hash, @now)
                            on conflict (version_id, model_id, market) do update set score = excluded.score, grade = excluded.grade,
                                components = excluded.components, inputs_hash = excluded.inputs_hash, computed_at = excluded.computed_at
                            """,
                            new { v = hp.VersionId, modelId, market, score = s.Value, grade = s.Grade, components = JsonSerializer.Serialize(s.Components),
                                  hash = InputsHash(hp, s), now }, ct);

                var doc = JsonSerializer.Serialize(hp, HotCache.Json);
                await u.ExecuteAsync(
                    """
                    insert into rec.health_profile (product_id, market, version_id, hsi, hsi_grade, primary_model_code, primary_score, primary_grade, nova_group,
                        flags, additive_tiers, us_fda_healthy, data_sufficient, eligible, profile_version, computed_at, category_id, profile_doc)
                    values (@ProductId, @market, @VersionId, @Hsi, @HsiGrade, @model, @score, @grade, @nova, @flags, @tiers::jsonb, @UsFdaHealthy,
                        @sufficient, @Eligible, @ProfileVersion, @now, @CategoryId, @doc::jsonb)
                    on conflict (product_id, market) do update set version_id = excluded.version_id, hsi = excluded.hsi, hsi_grade = excluded.hsi_grade,
                        primary_model_code = excluded.primary_model_code, primary_score = excluded.primary_score, primary_grade = excluded.primary_grade,
                        nova_group = excluded.nova_group, flags = excluded.flags, additive_tiers = excluded.additive_tiers, us_fda_healthy = excluded.us_fda_healthy,
                        data_sufficient = excluded.data_sufficient, eligible = excluded.eligible, profile_version = excluded.profile_version,
                        computed_at = excluded.computed_at, category_id = excluded.category_id, profile_doc = excluded.profile_doc
                    """,
                    new
                    {
                        hp.ProductId, market, hp.VersionId, hp.Hsi, hp.HsiGrade, model = hp.Primary?.Code, score = hp.Primary?.Value, grade = hp.Primary?.Grade,
                        nova = (short?)hp.Nova, flags = hp.Flags.ToArray(), tiers = JsonSerializer.Serialize(hp.AdditiveTierCounts), hp.UsFdaHealthy,
                        sufficient = hp.DataSufficiency == "SUFFICIENT", hp.Eligible, hp.ProfileVersion, now, hp.CategoryId, doc,
                    }, ct);

                var prevDoc = previous.TryGetValue(hp.ProductId, out var prev) ? JsonSerializer.Deserialize<HealthProfileData>(prev.ProfileDoc, HotCache.Json) : null;
                var changes = Changes(prevDoc, hp);
                if (changes.Count > 0)
                {
                    changed++;
                    await Outbox.EnqueueAsync(u, null, Topics.KbProductChanges, hp.ProductId.ToString(), new
                    {
                        kind = "PROFILE_CHANGED", productId = hp.ProductId, market, previousProfileVersion = prev?.ProfileVersion, profileVersion = hp.ProfileVersion, changes,
                    }, ct);
                }
            }

            foreach (var g in profiles.Where(p => p.CategoryId is not null).GroupBy(p => p.CategoryId!.Value))
            {
                var hsis = g.Where(p => p.Hsi is not null).Select(p => p.Hsi!.Value).Order().ToList();
                categories++;
                await u.ExecuteAsync(
                    """
                    insert into rec.category_stats (market, category_id, product_count, hsi_p60, hsi_mean, computed_at) values (@market, @cat, @n, @p60, @mean, @now)
                    on conflict (market, category_id) do update set product_count = excluded.product_count, hsi_p60 = excluded.hsi_p60, hsi_mean = excluded.hsi_mean,
                        computed_at = excluded.computed_at
                    """,
                    new { market, cat = g.Key, n = g.Count(), p60 = Percentile(hsis, pack.Rules.TargetCeilingPercentile / 100m), mean = hsis.Count == 0 ? (decimal?)null : Math.Round(hsis.Average(), 2), now }, ct);
            }
        }, ct);

        foreach (var hp in profiles)
        {
            await cache.SetProfileAsync(hp);
            if (hp.Gtin is not null) await cache.SetGtinAsync(hp.Gtin, hp.ProductId);
        }
        foreach (var g in profiles.Where(p => p.CategoryId is not null).GroupBy(p => p.CategoryId!.Value))
            if (Percentile(g.Where(p => p.Hsi is not null).Select(p => p.Hsi!.Value).Order().ToList(), pack.Rules.TargetCeilingPercentile / 100m) is { } p60)
                await cache.SetCategoryCeilingAsync(market, g.Key, p60);

        var report = new ScoringReport(market, profiles.Count, profiles.Count(p => p.Eligible), changed, categories);
        log.LogInformation("Scored {Market}: {Products} products, {Eligible} eligible, {Changed} changed", market, report.Products, report.Eligible, report.Changed);
        return report;
    }

    /// <summary>Linear-interpolated percentile (PERCENTILE_CONT); null for empty input.</summary>
    public static decimal? Percentile(IReadOnlyList<decimal> sorted, decimal q)
    {
        if (sorted.Count == 0) return null;
        if (sorted.Count == 1) return sorted[0];
        var pos = q * (sorted.Count - 1);
        var lo = (int)Math.Floor(pos);
        var hi = (int)Math.Ceiling(pos);
        return Math.Round(sorted[lo] + (sorted[hi] - sorted[lo]) * (pos - lo), 2);
    }

    public static List<string> Changes(HealthProfileData? prev, HealthProfileData now)
    {
        if (prev is null) return ["CREATED"];
        var changes = new List<string>();
        if (prev.VersionId != now.VersionId) changes.Add("NUTRITION_UPDATED");
        if (!prev.Allergens.OrderBy(k => k.Key).SequenceEqual(now.Allergens.OrderBy(k => k.Key))) changes.Add("ALLERGENS_UPDATED");
        if (prev.ProfileVersion != now.ProfileVersion && prev.VersionId == now.VersionId) changes.Add("SCORE_MODEL_UPDATED");
        if (!prev.Recalled && now.Recalled) changes.Add("RECALL_FLAGGED");
        if (prev.Hsi != now.Hsi || prev.HsiGrade != now.HsiGrade || !prev.Flags.SequenceEqual(now.Flags)) changes.Add("SCORE_CHANGED");
        if (prev.Eligible != now.Eligible) changes.Add("ELIGIBILITY_CHANGED");
        return changes;
    }

    private static byte[] InputsHash(HealthProfileData hp, ModelScore s) =>
        SHA256.HashData(Encoding.UTF8.GetBytes($"{s.Code}@{s.Version}|{hp.VersionId}|{JsonSerializer.Serialize(hp.Nutrition, HotCache.Json)}|{string.Join(",", hp.Additives.Select(a => a.Code))}|{hp.Nova}"));
}

public sealed class GraphOptions
{
    /// <summary>Ask the T4 substitutability judge (fine-tuned LLM, offline) for pairs in the uncertain band.</summary>
    public bool LlmJudgeUncertain { get; set; } = true;
    public decimal UncertainLow { get; set; } = 0.60m;
    public decimal UncertainHigh { get; set; } = 0.80m;
}

public sealed record GraphBuildReport(string Market, string GraphVersion, string Status, int Products, int Edges, Dictionary<string, object> Validation);

/// <summary>
/// Swap-graph builder (HLD 6.4, doc 03 sections 3-6): blocking by category and approved maps, embedding retrieval, static
/// rules R1-R4/R7, substitutability model, top-K with brand-tier diversity, validation gates and blue/green activation.
/// </summary>
public sealed class SwapGraphBuilder(Db db, ReferenceDataRepository refs, ModelRegistry models, LlmGateway llm, HotCache cache, GraphOptions options,
    IClock clock, ILogger<SwapGraphBuilder> log)
{
    private sealed class MapRow { public string FromCode { get; set; } = ""; public string ToCode { get; set; } = ""; }

    public async Task<List<GraphBuildReport>> BuildAllAsync(CancellationToken ct = default)
    {
        var reports = new List<GraphBuildReport>();
        foreach (var market in await refs.MarketsAsync(ct)) reports.Add(await BuildAsync(market, ct));
        return reports;
    }

    public async Task<GraphBuildReport> BuildAsync(string market, CancellationToken ct = default)
    {
        var pack = await refs.GetPackAsync(market, ct);
        var rules = pack.Rules;
        var (docs, maps) = await db.InTxAsync(DbRole.KbWriter, null, async u =>
        {
            var d = (await u.QueryAsync<string>("select profile_doc::text from rec.health_profile where market = @market", new { market }, ct)).ToList();
            var m = (await u.QueryAsync<MapRow>(
                """
                select f.code as from_code, t.code as to_code from rec.category_substitution s
                join kb.category f on f.category_id = s.from_category_id join kb.category t on t.category_id = s.to_category_id where s.market = @market
                """, new { market }, ct)).ToList();
            return (d, m);
        }, ct);
        var profiles = docs.Select(d => JsonSerializer.Deserialize<HealthProfileData>(d, HotCache.Json)!).ToList();
        var approved = maps.GroupBy(m => m.FromCode).ToDictionary(g => g.Key, g => g.Select(x => x.ToCode).ToHashSet());

        // Embeddings for retrieval (T7). Stored in rec.product_embedding for pgvector HNSW search in production.
        var texts = profiles.Select(Substitutability.Describe).ToList();
        var vectors = texts.Count == 0 ? [] : await llm.EmbedAsync(texts, ct);
        var embedding = profiles.Select((p, i) => (p.ProductId, Vector: vectors[i])).ToDictionary(x => x.ProductId, x => x.Vector);

        var now = clock.UtcNow;
        var graphVersion = $"{market}-{now.ToString("yyyy-MM-ddTHH:mm:ss", CultureInfo.InvariantCulture)}Z";
        if (await db.InTxAsync(DbRole.KbWriter, null, u => u.ScalarAsync<bool>("select exists (select 1 from rec.swap_graph_version where graph_version = @graphVersion)", new { graphVersion }, ct), ct))
            graphVersion += "." + now.ToString("fff", CultureInfo.InvariantCulture);
        var edges = new Dictionary<Guid, List<(SwapEdge Edge, string BrandTier, float[] Features)>>();
        var judged = 0;
        foreach (var a in profiles.Where(p => p.Hsi is not null))
        {
            var targets = approved.GetValueOrDefault(a.CategoryCode) ?? [];
            var list = new List<(SwapEdge, string, float[])>();
            foreach (var b in Candidates(a, profiles, targets, embedding, rules.RetrievalCandidates))
            {
                var (edge, diagnosis, features) = await EvaluatePairAsync(a, b, targets, pack, market, ct);
                if (diagnosis.JudgeScore is not null) judged++;
                if (edge is not null) list.Add((edge, b.BrandTier, features));
            }
            if (list.Count > 0) edges[a.ProductId] = Diversify(list, rules.MaxEdgesPerProduct);
        }

        // Validation gates (doc 03 section 6): never an edge into an ineligible product, every edge above the minimum delta.
        var byId = profiles.ToDictionary(p => p.ProductId);
        var all = edges.SelectMany(kv => kv.Value.Select(e => (From: kv.Key, e.Edge))).ToList();
        var intoIneligible = all.Count(e => !byId[e.Edge.To].Eligible);
        var belowDelta = all.Count(e => e.Edge.HealthDelta < rules.DeltaMin);
        var targetsWithEdges = edges.Count;
        var validation = new Dictionary<string, object>
        {
            ["edges"] = all.Count, ["productsWithEdges"] = targetsWithEdges, ["edgesIntoIneligible"] = intoIneligible, ["edgesBelowDeltaMin"] = belowDelta,
            ["llmJudged"] = judged, ["substitutabilityModel"] = models.SubstitutabilityVersion, ["rulesVersion"] = rules.Version,
        };
        var passed = intoIneligible == 0 && belowDelta == 0 && (all.Count > 0 || profiles.Count(p => p.Eligible) < 2);
        var status = passed ? "ACTIVE" : "FAILED";

        await db.InTxAsync(DbRole.KbWriter, null, async u =>
        {
            await u.ExecuteAsync(
                "insert into rec.swap_graph_version (graph_version, market, rules_version, hsi_model_version, status, edge_count, validation_report) values (@graphVersion, @market, @rv, @hv, 'BUILDING', 0, '{}'::jsonb)",
                new { graphVersion, market, rv = rules.Version, hv = pack.Hsi.Version }, ct);
            foreach (var (from, e) in all)
                await u.ExecuteAsync(
                    """
                    insert into rec.swap_edge (graph_version, from_product_id, to_product_id, health_delta, substitutability, reason_codes, features)
                    values (@graphVersion, @from, @to, @delta, @sub, @reasons, @features::jsonb)
                    """,
                    new { graphVersion, from, to = e.To, delta = e.HealthDelta, sub = e.Substitutability, reasons = e.Reasons,
                          features = JsonSerializer.Serialize(new { e.FromVersion, e.ToVersion, e.ToCategory, e.IntroducedTierC }) }, ct);
            await u.ExecuteAsync("update rec.swap_graph_version set status = 'VALIDATED', edge_count = @n, validation_report = @report::jsonb where graph_version = @graphVersion",
                new { graphVersion, n = all.Count, report = JsonSerializer.Serialize(validation) }, ct);
            if (passed)
            {
                await u.ExecuteAsync("update rec.swap_graph_version set status = 'RETIRED' where market = @market and status = 'ACTIVE'", new { market }, ct);
                await u.ExecuteAsync("update rec.swap_graph_version set status = 'ACTIVE', activated_at = @now where graph_version = @graphVersion", new { graphVersion, now }, ct);
            }
            else await u.ExecuteAsync("update rec.swap_graph_version set status = 'FAILED' where graph_version = @graphVersion", new { graphVersion }, ct);
            foreach (var (id, vector) in embedding)
                await u.ExecuteAsync(
                    """
                    insert into rec.product_embedding (product_id, model_ref, embedding) values (@id, @model, @v::vector)
                    on conflict (product_id, model_ref) do update set embedding = excluded.embedding, created_at = now()
                    """, new { id, model = llm.ProviderName == "mock" ? HashingEmbedder.ModelRef : $"{llm.ProviderName}/{llm.Options.Deployments.GetValueOrDefault("T7")}",
                              v = "[" + string.Join(",", vector.Select(x => x.ToString("G6", CultureInfo.InvariantCulture))) + "]" }, ct);
        }, ct);

        if (passed)
        {
            // Blue/green: write the new version's keys first, flip the pointer, then delete the old version's keys.
            var old = await cache.GetActiveGraphAsync(market);
            foreach (var (from, list) in edges) await cache.SetEdgesAsync(market, graphVersion, from, list.Select(x => x.Edge).ToArray());
            await cache.SetActiveGraphAsync(market, graphVersion);
            if (old is not null && old != graphVersion) await cache.DeleteGraphAsync(market, old);
        }
        log.LogInformation("Swap graph {Graph}: {Status}, {Edges} edges for {Products} products", graphVersion, status, all.Count, targetsWithEdges);
        return new GraphBuildReport(market, graphVersion, status, targetsWithEdges, all.Count, validation);
    }

    public sealed record SwapDiagnosis(Guid To, string Name, string Category, decimal? Hsi, decimal HealthDelta, decimal ModelSubstitutability, decimal? JudgeScore,
        decimal Substitutability, bool CategoryAllowed, bool Eligible, IReadOnlyList<string> FailedRules, IReadOnlyList<string> ReasonCodes, bool InActiveGraph);

    /// <summary>Blocking + retrieval: same category or an approved target category, eligible, healthier; nearest by embedding.</summary>
    private static List<HealthProfileData> Candidates(HealthProfileData a, List<HealthProfileData> profiles, HashSet<string> targets,
        Dictionary<Guid, float[]> embedding, int max) =>
        profiles.Where(b => b.ProductId != a.ProductId && b.Eligible && b.Hsi > a.Hsi && (b.CategoryCode == a.CategoryCode || targets.Contains(b.CategoryCode)))
            .OrderByDescending(b => TextSimilarity.Cosine(embedding[a.ProductId], embedding[b.ProductId])).Take(max).ToList();

    /// <summary>One candidate pair: substitutability model, T4 judge for the uncertain band, static rules R1-R4/R7.</summary>
    private async Task<(SwapEdge? Edge, SwapDiagnosis Diagnosis, float[] Features)> EvaluatePairAsync(HealthProfileData a, HealthProfileData b, HashSet<string> targets,
        MarketPack pack, string market, CancellationToken ct)
    {
        var allowed = b.CategoryCode == a.CategoryCode || targets.Contains(b.CategoryCode);
        var features = Substitutability.Features(a, b, Substitutability.CategoryDistance(a.CategoryCode, b.CategoryCode, targets.Contains(b.CategoryCode)));
        var model = models.ScoreSubstitutability([features])[0];
        var sub = model;
        decimal? judge = null;
        if (options.LlmJudgeUncertain && sub >= options.UncertainLow && sub < options.UncertainHigh)
        {
            judge = await JudgeAsync(a, b, market, ct);
            if (judge is { } s) sub = Math.Round((sub + s) / 2m, 3);
        }
        var check = StaticRules.Check(a, b, sub, allowed, pack);
        var diagnosis = new SwapDiagnosis(b.ProductId, b.Name, b.CategoryCode, b.Hsi, check.HealthDelta, model, judge, sub, allowed, check.Eligible, check.Failed, check.ReasonCodes, false);
        if (!check.Eligible) return (null, diagnosis, features);
        var introducedC = b.Additives.Where(x => x.Tier == 'C' && a.Additives.All(y => y.Code != x.Code)).Select(x => x.Code).ToArray();
        return (new SwapEdge(b.ProductId, Math.Round(check.HealthDelta, 2), Math.Round(sub, 3), check.ReasonCodes.ToArray(), a.VersionId, b.VersionId, b.CategoryCode, introducedC),
            diagnosis, features);
    }

    /// <summary>Admin diagnostics: why a product does or does not get alternatives (every candidate with its rule results).</summary>
    public async Task<object> DiagnoseAsync(string market, Guid fromProductId, CancellationToken ct)
    {
        var pack = await refs.GetPackAsync(market, ct);
        var (docs, maps, active) = await db.InTxAsync(DbRole.KbWriter, null, async u =>
        {
            var d = (await u.QueryAsync<string>("select profile_doc::text from rec.health_profile where market = @market", new { market }, ct)).ToList();
            var m = (await u.QueryAsync<MapRow>(
                "select f.code as from_code, t.code as to_code from rec.category_substitution s join kb.category f on f.category_id = s.from_category_id join kb.category t on t.category_id = s.to_category_id where s.market = @market",
                new { market }, ct)).ToList();
            var e = (await u.QueryAsync<Guid>("select e.to_product_id from rec.v_active_swap_edge e join rec.swap_graph_version g using (graph_version) where g.market = @market and e.from_product_id = @fromProductId",
                new { market, fromProductId }, ct)).ToHashSet();
            return (d, m, e);
        }, ct);
        var profiles = docs.Select(d => JsonSerializer.Deserialize<HealthProfileData>(d, HotCache.Json)!).ToList();
        var a = profiles.FirstOrDefault(p => p.ProductId == fromProductId) ?? throw HsException.NotFound("Product " + fromProductId);
        var targets = maps.Where(m => m.FromCode == a.CategoryCode).Select(m => m.ToCode).ToHashSet();
        var pool = profiles.Where(b => b.ProductId != a.ProductId && (b.CategoryCode == a.CategoryCode || targets.Contains(b.CategoryCode))).ToList();
        var results = new List<SwapDiagnosis>();
        foreach (var b in pool)
        {
            var (_, d, _) = await EvaluatePairAsync(a, b, targets, pack, market, ct);
            results.Add(d with { InActiveGraph = active.Contains(b.ProductId) });
        }
        return new
        {
            from = new { a.ProductId, a.Name, a.CategoryCode, a.Hsi, a.HsiGrade, a.Eligible, a.DataSufficiency, approvedTargetCategories = targets },
            rules = new { pack.Rules.Version, pack.Rules.TauSub, pack.Rules.DeltaMin },
            candidates = results.OrderByDescending(r => r.Eligible).ThenByDescending(r => r.HealthDelta),
        };
    }

    /// <summary>Keeps the best edges by health gain x substitutability while ensuring other brand tiers stay represented.</summary>
    private static List<(SwapEdge Edge, string BrandTier, float[] Features)> Diversify(List<(SwapEdge Edge, string BrandTier, float[] Features)> list, int max)
    {
        var ordered = list.OrderByDescending(e => e.Edge.HealthDelta * e.Edge.Substitutability).ThenBy(e => e.Edge.To).ToList();
        var top = ordered.Take(max).ToList();
        foreach (var tier in ordered.Select(e => e.BrandTier).Distinct().ToList())
        {
            if (top.Any(e => e.BrandTier == tier)) continue;
            var best = ordered.First(e => e.BrandTier == tier);
            if (top.Count >= max) top.RemoveAt(top.Count - 1);
            top.Add(best);
        }
        return top.OrderByDescending(e => e.Edge.HealthDelta * e.Edge.Substitutability).ThenBy(e => e.Edge.To).ToList();
    }

    private async Task<decimal?> JudgeAsync(HealthProfileData a, HealthProfileData b, string market, CancellationToken ct)
    {
        static object Attrs(HealthProfileData p) => new
        {
            name = p.Name, category = p.CategoryCode, form = p.Form, flavourFamily = p.FlavourFamily, useOccasion = p.UseOccasion, dietClass = p.DietClass,
            netQuantity = p.NetQuantity?.Value,
        };
        var payload = JsonSerializer.Serialize(new { A = Attrs(a), B = Attrs(b) });
        var r = await llm.RunAsync(PromptRegistry.SubstitutabilityJudge(market), $"<products>{payload}</products>", ct);
        var score = r.Output?["score"] is JsonValue v && v.TryGetValue<double>(out var d) ? (decimal?)Math.Round((decimal)d, 3) : null;
        var valid = r.Ok && score is >= 0 and <= 1;
        if (!r.Cached) await llm.AuditAsync(LlmTask.T4, r, payload, new Dictionary<string, bool> { ["schema"] = r.Ok, ["range"] = score is >= 0 and <= 1 }, valid, ct);
        return valid ? score : null;
    }
}

public sealed record ExplanationReport(string Market, int Pairs, int Rendered, int LlmAccepted, int LlmRejected);

/// <summary>
/// Explanation pre-generation (doc 03 sections 8-9): deterministic fact sheet, template rendering, optional LLM polish (T5)
/// through the validator chain; failures fall back to the template. Results are stored in rec.explanation and the hot cache.
/// </summary>
public sealed class ExplanationPregen(Db db, ReferenceDataRepository refs, LlmGateway llm, HotCache cache, IClock clock, ILogger<ExplanationPregen> log)
{
    private sealed class EdgeRow { public Guid FromProductId { get; set; } public Guid ToProductId { get; set; } }

    public async Task<List<ExplanationReport>> RunAllAsync(CancellationToken ct = default)
    {
        var reports = new List<ExplanationReport>();
        foreach (var market in await refs.MarketsAsync(ct)) reports.Add(await RunAsync(market, ct));
        return reports;
    }

    public async Task<ExplanationReport> RunAsync(string market, CancellationToken ct = default)
    {
        var pack = await refs.GetPackAsync(market, ct);
        var (edges, docs) = await db.InTxAsync(DbRole.KbWriter, null, async u =>
        {
            var e = (await u.QueryAsync<EdgeRow>(
                "select from_product_id, to_product_id from rec.v_active_swap_edge e join rec.swap_graph_version g using (graph_version) where g.market = @market",
                new { market }, ct)).ToList();
            var d = (await u.QueryAsync<string>("select profile_doc::text from rec.health_profile where market = @market", new { market }, ct)).ToList();
            return (e, d);
        }, ct);
        var profiles = docs.Select(d => JsonSerializer.Deserialize<HealthProfileData>(d, HotCache.Json)!).ToDictionary(p => p.ProductId);
        int rendered = 0, accepted = 0, rejected = 0;
        var rows = new List<(Guid From, Guid To, string Locale, FactSheet Sheet, RenderedExplanation Text, string? ModelRef, string? Prompt)>();
        foreach (var e in edges)
        {
            if (!profiles.TryGetValue(e.FromProductId, out var a) || !profiles.TryGetValue(e.ToProductId, out var b)) continue;
            var sheet = FactSheets.Build(a, b, pack);
            var selected = FactSheets.Select(sheet);
            if (selected.Count == 0) continue;
            foreach (var locale in ApiMapper.LocalesFor(market))
            {
                var text = ExplanationTemplates.Render(selected, locale);
                string? modelRef = null, prompt = null;
                if (llm.Options.EnableExplanationPolish)
                {
                    var (polished, ok, r) = await PolishAsync(selected, sheet.Basis, locale, ct);
                    if (ok) { text = polished!; modelRef = r.ModelRef; prompt = r.PromptVersion; accepted++; }
                    else rejected++;
                }
                rows.Add((a.ProductId, b.ProductId, locale, sheet, text, modelRef, prompt));
                rendered++;
            }
        }

        var now = clock.UtcNow;
        await db.InTxAsync(DbRole.KbWriter, null, async u =>
        {
            foreach (var r in rows)
                await u.ExecuteAsync(
                    """
                    insert into rec.explanation (market, locale, from_product_id, to_product_id, facts_hash, short_text, details, generator, model_ref, prompt_version, validated_at)
                    values (@market, @locale, @from, @to, decode(@hash, 'hex'), @short, @details::jsonb, @generator, @modelRef, @prompt, @now)
                    on conflict (market, locale, from_product_id, to_product_id, facts_hash) do update set short_text = excluded.short_text, details = excluded.details,
                        generator = excluded.generator, model_ref = excluded.model_ref, prompt_version = excluded.prompt_version, validated_at = excluded.validated_at
                    """,
                    new
                    {
                        market, locale = r.Locale, from = r.From, to = r.To, hash = r.Sheet.Hash, @short = r.Text.Short,
                        details = JsonSerializer.Serialize(new { bullets = r.Text.Bullets, factIdsUsed = r.Text.FactIdsUsed, facts = r.Sheet.Facts }, HotCache.Json),
                        generator = r.Text.Generator, modelRef = r.ModelRef, prompt = r.Prompt, now,
                    }, ct);
        }, ct);
        foreach (var r in rows)
            await cache.SetExplanationAsync(market, r.Locale, r.From, r.To, r.Sheet.Hash,
                new CachedExplanation(r.Text.Short, r.Text.Bullets.ToList(), r.Text.Generator, r.Text.AiGenerated, r.ModelRef));
        log.LogInformation("Explanations {Market}: {Rendered} rendered, LLM accepted {Accepted}, rejected {Rejected}", market, rendered, accepted, rejected);
        return new ExplanationReport(market, edges.Count, rendered, accepted, rejected);
    }

    /// <summary>T5 polish + validator chain. Exposed for the admin plane and the fine-tuning evaluation harness.</summary>
    public async Task<(RenderedExplanation? Text, bool Accepted, LlmResult Result)> PolishAsync(IReadOnlyList<Fact> facts, string basis, string locale, CancellationToken ct)
    {
        var payload = JsonSerializer.Serialize(new { basis, locale, facts }, HotCache.Json);
        var r = await llm.RunAsync(PromptRegistry.ExplanationWriter(locale), $"<facts>{payload}</facts>", ct);
        if (!r.Ok || r.Output is not JsonObject o)
        {
            if (!r.Cached) await llm.AuditAsync(LlmTask.T5, r, payload, new Dictionary<string, bool> { ["schema"] = false }, false, ct);
            return (null, false, r);
        }
        var shortText = o["short"]?.GetValue<string>() ?? "";
        var bullets = o["bullets"]?.AsArray().Select(x => x?.GetValue<string>() ?? "").ToList() ?? [];
        var report = ExplanationValidator.Validate(shortText, bullets, facts, locale);
        if (!r.Cached) await llm.AuditAsync(LlmTask.T5, r, payload, report.Checks, report.Passed, ct);
        if (!report.Passed) return (null, false, r);
        return (new RenderedExplanation(shortText, bullets, facts.Select(f => f.Id).ToList(), "LLM_VALIDATED", true, locale), true, r);
    }
}
