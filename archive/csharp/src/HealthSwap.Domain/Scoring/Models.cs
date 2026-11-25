using System.Text.Json.Serialization;
using HealthSwap.Domain.Knowledge;

namespace HealthSwap.Domain.Scoring;

/// <summary>One component of a threshold-points model: points = number of thresholds strictly exceeded.</summary>
public sealed record PointsComponent
{
    public required string Nutrient { get; init; }
    public required decimal[] Thresholds { get; init; }
    /// <summary>Optional explicit points per threshold step (for example FVL 40/60/80 % gives 1/2/5).</summary>
    public int[]? Points { get; init; }

    public int Score(decimal value)
    {
        var steps = Thresholds.Count(t => value > t);
        if (steps == 0) return 0;
        return Points is { Length: > 0 } p ? p[Math.Min(steps, p.Length) - 1] : steps;
    }
}

public sealed record ProteinRule
{
    /// <summary>When negative points reach this value protein is not counted ...</summary>
    public int NegativeAtLeast { get; init; } = 11;
    /// <summary>... unless this positive component reaches the given points (UK NPM: FVN = 5). Null = no exception.</summary>
    public string? UnlessComponent { get; init; }
    public int UnlessPointsAtLeast { get; init; }
    public string ProteinComponent { get; init; } = "PROTEIN_G";
}

public sealed record GradeBand(decimal Max, string Grade);

public sealed record ModelVariant
{
    public PointsComponent[] Negative { get; init; } = [];
    public PointsComponent[] Positive { get; init; } = [];
    public ProteinRule? ProteinRule { get; init; }
    public GradeBand[] Grades { get; init; } = [];
    /// <summary>Score bounds used to normalize this variant into the HSI nutrient-profile component (doc 03 2.5).</summary>
    public decimal HsiMin { get; init; }
    public decimal HsiMax { get; init; } = 40;
    /// <summary>Points added when non-sugar sweeteners are present (Nutri-Score 2023 beverages).</summary>
    public int SweetenerPenalty { get; init; }
    /// <summary>Fixed grade for variants such as plain water.</summary>
    public string? FixedGrade { get; init; }
}

/// <summary>Parameters of a threshold-points nutrient-profiling model, stored in rec.scoring_model.params.</summary>
public sealed record PointsModelParams
{
    public required string Code { get; init; }
    public required string Version { get; init; }
    public required Dictionary<string, ModelVariant> Variants { get; init; }
    /// <summary>scoring_category -> variant name (default FOOD).</summary>
    public Dictionary<string, string> CategoryVariants { get; init; } = [];
    /// <summary>"POINTS" (lower is better) or "STARS" (grade expressed as stars, still lower points are better).</summary>
    public string GradeKind { get; init; } = "POINTS";
    public string? Verification { get; init; }
}

public sealed record ModelScore
{
    public required string Code { get; init; }
    public required string Version { get; init; }
    public decimal? Value { get; init; }
    public string? Grade { get; init; }
    public string Variant { get; init; } = "FOOD";
    public Dictionary<string, int> Components { get; init; } = [];
    /// <summary>Normalized 0-100 (higher is better) for the HSI nutrient-profile component.</summary>
    [JsonIgnore] public decimal? Normalized { get; init; }
}

/// <summary>
/// Generic engine for Nutri-Score 2023, UK NPM 2004/05, Health Star Rating and the FSSAI INR draft: all are
/// "negative points minus positive points" models with category variants, a protein rule and grade bands.
/// Only the parameters differ, so a regulatory change is a data change (doc 03 2.1, "pluggable and versioned").
/// </summary>
public static class PointsModel
{
    public static ModelScore Score(PointsModelParams p, ProductData product)
    {
        var variantName = p.CategoryVariants.GetValueOrDefault(product.ScoringCategory) ??
                          (product.Nutrition.IsLiquid && p.Variants.ContainsKey("BEVERAGE") ? "BEVERAGE" : "FOOD");
        if (!p.Variants.TryGetValue(variantName, out var v)) { variantName = "FOOD"; v = p.Variants["FOOD"]; }

        if (v.FixedGrade is not null)
            return new ModelScore { Code = p.Code, Version = p.Version, Value = 0, Grade = v.FixedGrade, Variant = variantName, Normalized = 100 };

        var components = new Dictionary<string, int>();
        var negative = 0;
        foreach (var c in v.Negative)
        {
            var value = product.Nutrition.Get(c.Nutrient);
            if (value is null) return Missing(p, variantName);
            var pts = c.Score(value.Value);
            components[c.Nutrient] = pts;
            negative += pts;
        }
        if (v.SweetenerPenalty > 0 && product.Has("NNS"))
        {
            components["SWEETENERS"] = v.SweetenerPenalty;
            negative += v.SweetenerPenalty;
        }

        var positive = 0;
        var positivePoints = new Dictionary<string, int>();
        foreach (var c in v.Positive)
        {
            var value = product.Nutrition.Get(c.Nutrient) ?? 0m; // missing positive inputs score 0 (conservative)
            var pts = c.Score(value);
            positivePoints[c.Nutrient] = pts;
        }
        foreach (var (nutrient, pts) in positivePoints)
        {
            var counted = pts;
            if (v.ProteinRule is { } rule && nutrient == rule.ProteinComponent && negative >= rule.NegativeAtLeast)
            {
                var exception = rule.UnlessComponent is not null && positivePoints.GetValueOrDefault(rule.UnlessComponent) >= rule.UnlessPointsAtLeast;
                if (!exception && product.ScoringCategory != "CHEESE") counted = 0;
            }
            components[nutrient] = -counted;
            positive += counted;
        }

        var score = negative - positive;
        var grade = v.Grades.FirstOrDefault(g => score <= g.Max)?.Grade ?? v.Grades.LastOrDefault()?.Grade;
        var normalized = v.HsiMax == v.HsiMin ? 50m : Math.Clamp(100m * (v.HsiMax - score) / (v.HsiMax - v.HsiMin), 0m, 100m);
        return new ModelScore
        {
            Code = p.Code, Version = p.Version, Value = score, Grade = grade, Variant = variantName, Components = components,
            Normalized = Math.Round(normalized, 2),
        };
    }

    private static ModelScore Missing(PointsModelParams p, string variant) =>
        new() { Code = p.Code, Version = p.Version, Value = null, Grade = null, Variant = variant };

    /// <summary>Grade order for "strictly better" comparisons (R2). Lower index is better.</summary>
    public static int GradeRank(PointsModelParams p, ModelScore s)
    {
        if (s.Grade is null) return int.MaxValue;
        var v = p.Variants.GetValueOrDefault(s.Variant) ?? p.Variants["FOOD"];
        var idx = Array.FindIndex(v.Grades, g => g.Grade == s.Grade);
        return idx < 0 ? (s.Grade == v.FixedGrade ? -1 : int.MaxValue) : idx;
    }
}

public enum FopBand { Low = 0, Medium = 1, High = 2 }

/// <summary>Front-of-pack thresholds for one nutrient (reg.fop_threshold).</summary>
public sealed record FopThreshold(string Nutrient, string PhysicalState, string Basis, decimal LowMax, decimal HighMin, bool HighInclusive);

public static class FopBands
{
    /// <summary>Band per limited nutrient. PER_SERVING_PCT_DV thresholds need a serving size and daily values.</summary>
    public static Dictionary<string, FopBand> Compute(ProductData p, IReadOnlyList<FopThreshold> thresholds, IReadOnlyDictionary<string, decimal> dailyValues)
    {
        var result = new Dictionary<string, FopBand>();
        var state = p.Nutrition.IsLiquid ? "LIQUID" : "SOLID";
        foreach (var group in thresholds.GroupBy(t => t.Nutrient))
        {
            var t = group.FirstOrDefault(x => x.PhysicalState == state) ?? group.FirstOrDefault(x => x.PhysicalState == "ANY");
            if (t is null) continue;
            var per100 = p.Nutrition.Get(NutrientKey(t.Nutrient));
            if (per100 is null) continue;
            decimal value;
            if (t.Basis == "PER_SERVING_PCT_DV")
            {
                if (p.ServingGrams is not { } serving || !dailyValues.TryGetValue(t.Nutrient, out var dv) || dv <= 0) continue;
                value = per100.Value * serving / 100m / dv * 100m;
            }
            else value = per100.Value;
            var high = t.HighInclusive ? value >= t.HighMin : value > t.HighMin;
            result[t.Nutrient] = high ? FopBand.High : value <= t.LowMax ? FopBand.Low : FopBand.Medium;
        }
        return result;
    }

    public static string NutrientKey(string code) => code switch
    {
        "SUGAR" => "SUGARS_G",
        "SUGAD" => "LIMITED_SUGARS_G",
        "FASAT" => "SAT_FAT_G",
        "FATRN" => "TRANS_FAT_G",
        "NA" => "SODIUM_MG",
        "SALT" => "SALT_G",
        "FAT" => "FAT_G",
        _ => code,
    };
}

/// <summary>Additive concern tiers from regulators and authoritative bodies only (doc 03 2.3).</summary>
public sealed record AdditiveRegulation(string Code, string Name, IReadOnlyCollection<string> FunctionalClasses,
    IReadOnlyDictionary<string, string> StatusByJurisdiction, char? HazardTier);

public static class AdditiveTiers
{
    private static readonly HashSet<string> MajorJurisdictions = ["EU", "US", "IN", "GB", "CA", "AU"];

    public static char Resolve(AdditiveRegulation? a, string market)
    {
        if (a is null) return 'D';
        if (a.StatusByJurisdiction.TryGetValue(market, out var s) && s == "PROHIBITED") return 'A';
        var bElsewhere = a.StatusByJurisdiction.Any(kv => MajorJurisdictions.Contains(kv.Key) && kv.Value is "PROHIBITED" or "PHASE_OUT");
        if (a.HazardTier == 'B' || bElsewhere) return 'B';
        if (a.HazardTier == 'C') return 'C';
        return 'D';
    }
}

public sealed record HsiParams
{
    public decimal WeightNutrientProfile { get; init; } = 0.70m;
    public decimal WeightProcessing { get; init; } = 0.15m;
    public decimal WeightAdditives { get; init; } = 0.15m;
    public Dictionary<int, decimal> NovaPoints { get; init; } = new() { [1] = 100, [2] = 85, [3] = 70, [4] = 40 };
    public decimal TierBPenalty { get; init; } = 30;
    public decimal TierCPenalty { get; init; } = 10;
    public string Version { get; init; } = "1.0";
}

/// <summary>HealthSwap Index (doc 03 2.5): HSI = 0.70 NP + 0.15 PR + 0.15 AD; weights renormalized when NOVA is unknown.</summary>
public static class Hsi
{
    public static decimal? Compute(decimal? np, int? nova, int tierB, int tierC, HsiParams p)
    {
        if (np is null) return null;
        var ad = Math.Max(0m, 100m - p.TierBPenalty * tierB - p.TierCPenalty * tierC);
        decimal value;
        if (nova is { } n && p.NovaPoints.TryGetValue(n, out var pr))
            value = p.WeightNutrientProfile * np.Value + p.WeightProcessing * pr + p.WeightAdditives * ad;
        else
            value = (p.WeightNutrientProfile * np.Value + p.WeightAdditives * ad) / (p.WeightNutrientProfile + p.WeightAdditives);
        return Math.Round(Math.Clamp(value, 0m, 100m), 2);
    }

    public static string? Grade(decimal? hsi) => hsi switch
    {
        null => null,
        >= 80 => "A",
        >= 65 => "B",
        >= 50 => "C",
        >= 35 => "D",
        _ => "E",
    };
}
