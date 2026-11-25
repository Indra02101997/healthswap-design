namespace HealthSwap.Domain.Knowledge;

public enum QaAction { Review, RejectField, AutoFix, Flag, Quarantine, RejectIdentifier }

public sealed record QaFinding(string Code, QaAction Action, string Field, string Message);

public sealed record QaInput
{
    public required Nutrition Nutrition { get; init; }
    public decimal? PerServingEnergyKcal { get; init; }
    public decimal? ServingSize { get; init; }
    public string? RawGtin { get; init; }
    public IReadOnlyDictionary<string, AllergenPresence> DeclaredAllergens { get; init; } = new Dictionary<string, AllergenPresence>();
    public IReadOnlyCollection<string> IngredientAllergens { get; init; } = [];
    public bool VegMarkContradicted { get; init; }
    public IReadOnlyCollection<string> TierAAdditives { get; init; } = [];
    public DateTime? LastVerifiedAt { get; init; }
    public DateTime Now { get; init; }
    public bool SaltDeclared { get; init; }
    public bool SodiumDeclared { get; init; }
}

/// <summary>Data-quality rules as code (doc 02 section 10). Each rule is deterministic and unit-tested.</summary>
public static class QaRules
{
    public static IReadOnlyList<QaFinding> Run(QaInput input)
    {
        var f = new List<QaFinding>();
        var n = input.Nutrition;

        // QA-01 energy consistency, using available carbohydrate for the jurisdiction's carbohydrate definition.
        if (n.EnergyKcal is { } e && n.ProteinG is { } p && n.AvailableCarbohydrate is { } c && n.TotalFatG is { } fat)
        {
            var computed = 4 * p + 4 * c + 9 * fat + 2 * (n.FibreG ?? 0);
            if (Math.Abs(e - computed) > Math.Max(0.15m * e, 20m))
                f.Add(new("QA-01", QaAction.Review, "nutrition.energyKcal", $"Declared {e} kcal vs {computed:0.#} kcal from macronutrients"));
        }
        // QA-02..QA-04 internal consistency.
        if (n.TotalSugarsG is { } sug && n.CarbohydrateG is { } carb && sug > carb + 0.5m)
            f.Add(new("QA-02", QaAction.RejectField, "nutrition.totalSugarsG", $"Sugars {sug} g exceed carbohydrate {carb} g"));
        if (n.AddedSugarsG is { } add && n.TotalSugarsG is { } tot && add > tot + 0.5m)
            f.Add(new("QA-03", QaAction.RejectField, "nutrition.addedSugarsG", $"Added sugars {add} g exceed total sugars {tot} g"));
        if (n.TotalFatG is { } tf && (n.SaturatedFatG ?? 0) + (n.TransFatG ?? 0) > tf + 0.5m)
            f.Add(new("QA-04", QaAction.RejectField, "nutrition.saturatedFatG", "Saturated + trans fat exceed total fat"));
        // QA-05 mass balance per 100 g. US "total carbohydrate" already includes fibre, so fibre is added only for
        // the AVAILABLE definition (fixes the double count noted in review).
        if (!n.IsLiquid)
        {
            var fibre = n.CarbDefinition == "TOTAL_INCL_FIBRE" ? 0 : n.FibreG ?? 0;
            var mass = (n.ProteinG ?? 0) + (n.TotalFatG ?? 0) + (n.CarbohydrateG ?? 0) + fibre + (n.Salt ?? 0);
            if (mass > 105m) f.Add(new("QA-05", QaAction.Review, "nutrition", $"Macronutrients sum to {mass:0.#} g per 100 g"));
        }
        // QA-06 sodium range (the robust z-score within category runs in the category-level job).
        if (n.Sodium is { } na && na > 40000m)
            f.Add(new("QA-06", QaAction.Review, "nutrition.sodiumMg", $"Sodium {na} mg per 100 g is implausible"));
        // QA-07 per-serving consistency.
        if (input.PerServingEnergyKcal is { } ps && input.ServingSize is { } ss && ss > 0 && n.EnergyKcal is { } e100)
        {
            var expected = e100 * ss / 100m;
            if (expected > 0 && Math.Abs(ps - expected) / expected > 0.05m)
                f.Add(new("QA-07", QaAction.Review, "nutrition.perServing", $"Per-serving energy {ps} vs expected {expected:0.#}"));
        }
        // QA-08 salt = 2.5 x sodium when both are declared.
        if (input.SaltDeclared && input.SodiumDeclared && n.SaltG is { } salt && n.SodiumMg is { } sodium && sodium > 0)
        {
            var expectedSalt = sodium * 2.5m / 1000m;
            if (Math.Abs(salt - expectedSalt) / expectedSalt > 0.10m)
                f.Add(new("QA-08", QaAction.Review, "nutrition.saltG", $"Salt {salt} g vs 2.5 x sodium = {expectedSalt:0.##} g"));
        }
        // QA-10 ingredient-derived allergens must be declared; the stored set is the conservative union.
        var undeclared = input.IngredientAllergens.Where(a => !input.DeclaredAllergens.TryGetValue(a, out var pr) || pr != AllergenPresence.Contains).ToList();
        if (undeclared.Count > 0)
            f.Add(new("QA-10", QaAction.Flag, "allergens", "Ingredients imply undeclared allergens: " + string.Join(", ", undeclared)));
        // QA-11 veg symbol contradicted by ingredients.
        if (input.VegMarkContradicted)
            f.Add(new("QA-11", QaAction.Quarantine, "dietaryMarks", "Vegetarian mark contradicted by ingredients; excluded from veg-constrained swaps"));
        // QA-12 stale verification.
        if (input.LastVerifiedAt is { } lv && input.Now - lv > TimeSpan.FromDays(730))
            f.Add(new("QA-12", QaAction.Review, "verification", "Last verification older than 24 months"));
        // QA-13 GTIN check digit.
        if (!string.IsNullOrWhiteSpace(input.RawGtin) && !Gtin.TryNormalize(input.RawGtin, out _))
            f.Add(new("QA-13", QaAction.RejectIdentifier, "gtin", $"Invalid GTIN {input.RawGtin}"));
        // QA-14 additive prohibited in the market of sale (Tier A).
        if (input.TierAAdditives.Count > 0)
            f.Add(new("QA-14", QaAction.Quarantine, "additives", "Prohibited additive in market: INS " + string.Join(", ", input.TierAAdditives)));
        return f;
    }

    /// <summary>QA-09: sodium declared in grams with a value above 10 is almost certainly milligrams.</summary>
    public static (decimal Value, QaFinding? Finding) FixSodiumUnit(decimal value, string unit)
    {
        if (unit == "g" && value > 10m)
            return (value, new QaFinding("QA-09", QaAction.AutoFix, "nutrition.sodiumMg", $"Sodium {value} g interpreted as {value} mg"));
        return (unit == "g" ? value * 1000m : value, null);
    }
}

/// <summary>SUFFICIENT / PARTIAL / INSUFFICIENT (doc 02 7.5).</summary>
public static class DataSufficiencyRules
{
    public static string Evaluate(Nutrition n, decimal ingredientResolution, bool hasIngredients, decimal allergenConfidence,
        bool allergensVerified, bool hasCategory, bool hasNetQuantity)
    {
        var core = n.EnergyKcal is not null && n.LimitedSugars is not null && n.SaturatedFatG is not null && n.Sodium is not null;
        if (!core) return "INSUFFICIENT";
        var full = n.ProteinG is not null && n.FibreG is not null && hasIngredients && ingredientResolution >= 0.90m &&
                   (allergensVerified || allergenConfidence >= 0.98m) && hasCategory && hasNetQuantity;
        return full ? "SUFFICIENT" : "PARTIAL";
    }
}

/// <summary>Pairwise entity-resolution features (doc 02 7.3). The trained model (ONNX) consumes exactly these features.</summary>
public static class EntityResolution
{
    public static readonly string[] FeatureNames =
        ["name_trigram", "name_embedding_cos", "brand_match", "size_equal", "size_ratio_log", "nutrition_cos", "ingredient_jaccard", "gtin_conflict"];

    public const decimal AutoMergeThreshold = 0.95m;
    public const decimal ReviewThreshold = 0.75m;

    public sealed record Candidate(string Name, string? Brand, NetQuantity? Size, Nutrition? Nutrition, IReadOnlyCollection<string> Ingredients, string? Gtin);

    public static float[] Features(Candidate a, Candidate b)
    {
        var trigram = TextSimilarity.TrigramJaccard(a.Name, b.Name);
        var emb = TextSimilarity.Cosine(HashingEmbedder.Embed(a.Name + " " + a.Brand), HashingEmbedder.Embed(b.Name + " " + b.Brand));
        var brand = BrandMatch(a.Brand, b.Brand);
        var sizeEqual = a.Size is { } sa && b.Size is { } sb && sa.IsComparableTo(sb) && Math.Abs(sa.Value - sb.Value) <= 0.02m * Math.Max(sa.Value, sb.Value) ? 1f : 0f;
        var sizeRatio = a.Size is { } x && b.Size is { } y && x.IsComparableTo(y) && x.Value > 0 && y.Value > 0
            ? (float)Math.Abs(Math.Log((double)(x.Value / y.Value))) : 1f;
        var nut = a.Nutrition is not null && b.Nutrition is not null ? NutritionCosine(a.Nutrition, b.Nutrition) : 0.5f;
        var ing = TextSimilarity.Jaccard(a.Ingredients, b.Ingredients);
        var gtinConflict = a.Gtin is not null && b.Gtin is not null && a.Gtin != b.Gtin ? 1f : 0f;
        return [trigram, emb, brand, sizeEqual, sizeRatio, nut, ing, gtinConflict];
    }

    /// <summary>Transparent fallback used until a trained model is deployed (and as the L2 degradation path).</summary>
    public static decimal HeuristicScore(float[] f)
    {
        if (f[7] > 0) return 0.05m; // never merge two different GTINs (doc 02 7.3 rule 4)
        var z = -6.0 + 3.0 * f[0] + 2.5 * f[1] + 2.0 * f[2] + 1.5 * f[3] - 1.0 * f[4] + 2.0 * f[5] + 1.5 * f[6];
        return (decimal)Math.Round(1.0 / (1.0 + Math.Exp(-z)), 4);
    }

    public static string Decide(decimal score) => score >= AutoMergeThreshold ? "AUTO_MERGE" : score >= ReviewThreshold ? "REVIEW" : "NEW";

    private static float BrandMatch(string? a, string? b)
    {
        if (string.IsNullOrWhiteSpace(a) || string.IsNullOrWhiteSpace(b)) return 0.5f;
        var na = IngredientTaxonomy.Normalize(a);
        var nb = IngredientTaxonomy.Normalize(b);
        return na == nb ? 1f : na.Contains(nb, StringComparison.Ordinal) || nb.Contains(na, StringComparison.Ordinal) ? 0.8f : 0f;
    }

    private static float NutritionCosine(Nutrition a, Nutrition b)
    {
        static double[] V(Nutrition n) =>
        [
            (double)(n.EnergyKcal ?? 0) / 100, (double)(n.ProteinG ?? 0), (double)(n.CarbohydrateG ?? 0), (double)(n.TotalSugarsG ?? 0),
            (double)(n.TotalFatG ?? 0), (double)(n.SaturatedFatG ?? 0), (double)(n.Sodium ?? 0) / 100, (double)(n.FibreG ?? 0),
        ];
        var va = V(a);
        var vb = V(b);
        double dot = 0, na = 0, nb = 0;
        for (var i = 0; i < va.Length; i++) { dot += va[i] * vb[i]; na += va[i] * va[i]; nb += vb[i] * vb[i]; }
        return na == 0 || nb == 0 ? 0f : (float)(dot / Math.Sqrt(na * nb));
    }
}

/// <summary>Field-level survivorship (doc 02 7.4): highest trust tier, then most recent label; conflicts go to curation.</summary>
public static class Survivorship
{
    public sealed record Candidate(string FieldPath, decimal? Number, string? Text, int TrustTier, DateTime ObservedAt, long SourceRecordId, bool QaPassed);

    public sealed record Decision(string FieldPath, Candidate? Selected, bool Conflict, string? Reason);

    public static Decision Select(string fieldPath, IReadOnlyCollection<Candidate> candidates)
    {
        var usable = candidates.Where(c => c.QaPassed && (c.Number is not null || c.Text is not null)).ToList();
        if (usable.Count == 0) return new Decision(fieldPath, null, false, "NO_VALUE");
        var ordered = usable.OrderBy(c => c.TrustTier).ThenByDescending(c => c.ObservedAt).ToList();
        var best = ordered[0];
        // Unverified machine extraction (T5) may only fill gaps; community data (T4) never overrides T1-T3.
        var challenger = ordered.Skip(1).FirstOrDefault(c => c.TrustTier != best.TrustTier && c.TrustTier <= 3);
        if (challenger is not null && Disagree(fieldPath, best, challenger))
            return new Decision(fieldPath, best, true, $"T{best.TrustTier} vs T{challenger.TrustTier} disagree beyond tolerance");
        return new Decision(fieldPath, best, false, null);
    }

    public static bool Disagree(string fieldPath, Candidate a, Candidate b)
    {
        if (a.Number is { } x && b.Number is { } y)
        {
            var tolerance = fieldPath.Contains("SODIUM", StringComparison.OrdinalIgnoreCase) || fieldPath.Contains(".NA.", StringComparison.Ordinal) ? 0.15m : 0.10m;
            var denom = Math.Max(Math.Abs(x), Math.Abs(y));
            return denom > 0 && Math.Abs(x - y) / denom > tolerance;
        }
        if (fieldPath.StartsWith("allergen", StringComparison.OrdinalIgnoreCase))
            return !string.Equals(a.Text, b.Text, StringComparison.OrdinalIgnoreCase);
        return false;
    }
}
