using HealthSwap.Domain.Scoring;

namespace HealthSwap.Domain.Recommendation;

/// <summary>
/// Substitutability: "would a typical shopper who intended to buy A accept B for the same use?" Health and price are
/// deliberately excluded (doc 03 section 6). The calibrated classifier (ONNX, trained in ml/substitutability) consumes
/// exactly these features; the heuristic is the transparent fallback.
/// </summary>
public static class Substitutability
{
    public static readonly string[] FeatureNames =
    [
        "category_distance", "embedding_cos", "form_match", "flavour_match", "use_occasion_match", "diet_class_match",
        "pack_ratio_log", "brand_tier_same", "same_brand", "state_match",
    ];

    /// <summary>0 = same leaf category, 1 = sibling leaf under the same parent, 2 = approved cross-category map (curated, takes precedence).</summary>
    public static int CategoryDistance(string a, string b, bool approvedMap)
    {
        if (a == b) return 0;
        if (approvedMap) return 2;
        var pa = a.LastIndexOf('.');
        var pb = b.LastIndexOf('.');
        if (pa > 0 && pb > 0 && a[..pa] == b[..pb]) return 1;
        return 3;
    }

    public static float[] Features(HealthProfileData a, HealthProfileData b, int categoryDistance)
    {
        static float Match(string? x, string? y) => x is null || y is null ? 0.5f : string.Equals(x, y, StringComparison.OrdinalIgnoreCase) ? 1f : 0f;
        var emb = TextSimilarity.Cosine(HashingEmbedder.Embed(Describe(a)), HashingEmbedder.Embed(Describe(b)));
        var packRatio = a.NetQuantity is { } sa && b.NetQuantity is { } sb && sa.IsComparableTo(sb) && sa.Value > 0 && sb.Value > 0
            ? (float)Math.Abs(Math.Log((double)(sb.Value / sa.Value))) : 0.7f;
        var dietMatch = a.DietClass == b.DietClass ? 1f : (a.DietClass is "VEG" or "VEGAN") == (b.DietClass is "VEG" or "VEGAN") ? 0.7f : 0f;
        return
        [
            categoryDistance, emb, Match(a.Form, b.Form), Match(a.FlavourFamily, b.FlavourFamily), Match(a.UseOccasion, b.UseOccasion),
            dietMatch, packRatio, a.BrandTier == b.BrandTier ? 1f : 0f,
            a.Brand is not null && string.Equals(a.Brand, b.Brand, StringComparison.OrdinalIgnoreCase) ? 1f : 0f,
            a.PhysicalState == b.PhysicalState ? 1f : 0f,
        ];
    }

    public static string Describe(HealthProfileData p) => $"{p.Name} {p.Brand} {p.CategoryName ?? p.CategoryCode} {p.FlavourFamily} {p.Form}";

    public static decimal Heuristic(float[] f)
    {
        var score = f[0] switch { 0 => 0.80, 1 => 0.62, 2 => 0.74, _ => 0.20 };
        score += (f[3] - 0.5) * 0.24;        // flavour family
        score += (f[2] - 0.5) * 0.30;        // product form
        score += (f[4] - 0.5) * 0.20;        // use occasion
        score -= (1 - f[5]) * 0.30;          // dietary class
        score -= Math.Max(0, f[6] - 0.69) * 0.20; // pack ratio beyond 2x
        score += (f[1] - 0.3) * 0.15;        // text similarity
        if (f[9] < 1) score -= 0.25;         // solid vs liquid
        return (decimal)Math.Round(Math.Clamp(score, 0, 1), 3);
    }
}

/// <summary>Runtime ranker features (doc 03 section 7). Fee, margin, commission and sponsor payments are forbidden.</summary>
public static class RankerFeatures
{
    public static readonly string[] FeatureNames =
    [
        "delta_hsi", "substitutability", "price_ratio", "is_cheaper", "brand_switch", "pack_ratio_log",
        "placement_cart", "placement_checkout", "placement_pdp", "placement_agent", "daypart_morning", "daypart_evening",
        "goal_match", "category_beverage", "nova_improvement", "same_brand_family",
    ];

    public static readonly string[] Forbidden = ["fee", "margin", "commission", "sponsor", "reco_fee", "premium", "revenue"];

    public sealed record Input(decimal DeltaHsi, decimal Substitutability, decimal? PriceRatio, bool BrandSwitch, decimal? PackRatio,
        string Placement, int LocalHour, decimal GoalMatch, bool Beverage, int NovaImprovement, bool SameBrandFamily);

    public static float[] Vector(Input i) =>
    [
        (float)i.DeltaHsi, (float)i.Substitutability, (float)(i.PriceRatio ?? 1m), i.PriceRatio < 1m ? 1f : 0f, i.BrandSwitch ? 1f : 0f,
        i.PackRatio is { } pr && pr > 0 ? (float)Math.Abs(Math.Log((double)pr)) : 0f,
        i.Placement == "CART" ? 1f : 0f, i.Placement == "CHECKOUT" ? 1f : 0f, i.Placement is "PDP" or "SEARCH" ? 1f : 0f,
        i.Placement == "AGENT" ? 1f : 0f, i.LocalHour is >= 5 and < 12 ? 1f : 0f, i.LocalHour is >= 17 and < 23 ? 1f : 0f,
        (float)i.GoalMatch, i.Beverage ? 1f : 0f, i.NovaImprovement, i.SameBrandFamily ? 1f : 0f,
    ];
}

public sealed record RankWeights
{
    public decimal Health { get; init; } = 0.45m;
    public decimal Substitutability { get; init; } = 0.25m;
    public decimal Acceptance { get; init; } = 0.30m;
    public decimal PricePenalty { get; init; } = 0.15m;
    public decimal Fatigue { get; init; } = 0.20m;
    public decimal Goal { get; init; } = 0.10m;
}

public static class Ranking
{
    /// <summary>score = wh dHSI^ + ws Sub + wa P(accept) - wp PricePenalty - wf Fatigue + wg GoalMatch (doc 03 section 7).</summary>
    public static decimal Score(RankWeights w, decimal deltaHsi, decimal sub, decimal? pAccept, decimal? priceRatio, decimal fatigue, decimal goalMatch)
    {
        var normalizedDelta = Math.Clamp(deltaHsi / 50m, 0m, 1m);
        var pricePenalty = priceRatio is { } r ? Math.Max(0m, r - 1m) : 0m;
        if (pAccept is null) return Math.Round(normalizedDelta * sub, 6); // L2 degradation: health delta x substitutability
        return Math.Round(w.Health * normalizedDelta + w.Substitutability * sub + w.Acceptance * pAccept.Value
                          - w.PricePenalty * pricePenalty - w.Fatigue * fatigue + w.Goal * goalMatch, 6);
    }

    /// <summary>Goal overlays change ranking weights only, never eligibility (doc 03 2.7).</summary>
    public static decimal GoalMatch(IReadOnlyCollection<string> goals, IReadOnlyCollection<string> reasonCodes)
    {
        if (goals.Count == 0) return 0m;
        var map = new Dictionary<string, string>
        {
            ["LOWER_SUGAR"] = "LOWER_SUGAR", ["LOWER_SODIUM"] = "LOWER_SODIUM", ["LOWER_SAT_FAT"] = "LOWER_SAT_FAT",
            ["HIGHER_PROTEIN"] = "HIGHER_PROTEIN", ["HIGHER_FIBRE"] = "HIGHER_FIBRE", ["LESS_PROCESSED"] = "LESS_PROCESSED",
            ["FEWER_ADDITIVES"] = "FEWER_ADDITIVES_OF_CONCERN",
        };
        var hits = goals.Count(g => map.TryGetValue(g, out var rc) && reasonCodes.Contains(rc));
        return Math.Round((decimal)hits / goals.Count, 3);
    }

    /// <summary>Deterministic exploration bucket: about 5% of evaluations shuffle the top 3 (propensities are logged).</summary>
    public static bool IsExploration(Guid evaluationId, decimal rate)
    {
        var bytes = evaluationId.ToByteArray();
        var bucket = (bytes[10] * 256 + bytes[11]) % 10000;
        return bucket < rate * 10000;
    }
}

public sealed record CartCandidate(string LineId, string CategoryCode, decimal DeltaHsi, decimal Quantity, decimal CategoryWeight, decimal? PriceRatio);

public static class CartSelection
{
    /// <summary>Up to M lines maximizing sum(dHSI x qty x weight), at most one per category, ties to the lower price ratio.</summary>
    public static IReadOnlyList<string> Select(IEnumerable<CartCandidate> candidates, int maxSuggestions)
    {
        return candidates
            .OrderByDescending(c => c.DeltaHsi * c.Quantity * c.CategoryWeight)
            .ThenBy(c => c.PriceRatio ?? 1m)
            .ThenBy(c => c.LineId, StringComparer.Ordinal)
            .GroupBy(c => c.CategoryCode)
            .Select(g => g.First())
            .OrderByDescending(c => c.DeltaHsi * c.Quantity * c.CategoryWeight)
            .ThenBy(c => c.PriceRatio ?? 1m)
            .Take(maxSuggestions)
            .Select(c => c.LineId)
            .ToList();
    }
}
