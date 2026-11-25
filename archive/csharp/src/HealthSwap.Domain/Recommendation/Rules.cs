using HealthSwap.Domain.Knowledge;
using HealthSwap.Domain.Scoring;

namespace HealthSwap.Domain.Recommendation;

public sealed record StaticRuleResult(bool Eligible, decimal HealthDelta, IReadOnlyList<string> Failed, IReadOnlyList<string> ReasonCodes,
    IReadOnlyList<string> IntroducedTierC);

/// <summary>
/// Product-level eligibility rules evaluated offline by the swap-graph builder and re-checked at request time when a
/// profile version changed (doc 03 section 3: R1, R2, R3, R4, R7).
/// </summary>
public static class StaticRules
{
    private static readonly (string Fop, string Key, decimal SolidLow, decimal LiquidLow)[] LimitedNutrients =
    [
        ("SUGAD", "LIMITED_SUGARS_G", 5.0m, 2.5m),
        ("NA", "SODIUM_MG", 120m, 60m),
        ("FASAT", "SAT_FAT_G", 1.5m, 0.75m),
        ("FATRN", "TRANS_FAT_G", 0.1m, 0.1m),
    ];

    public static StaticRuleResult Check(HealthProfileData a, HealthProfileData b, decimal substitutability, bool categoryAllowed, MarketPack pack)
    {
        var failed = new List<string>();
        var rules = pack.Rules;
        var delta = (b.Hsi ?? 0) - (a.Hsi ?? 0);

        // R1 substitutable
        if (!categoryAllowed || substitutability < rules.TauSub) failed.Add("R1");

        // R2 meaningfully healthier: delta >= dMin and the primary model strictly better; equal result needs 2 x dMin.
        if (a.Hsi is null || b.Hsi is null) failed.Add("R2");
        else
        {
            var model = pack.Primary;
            var ra = a.Primary is null ? int.MaxValue : PointsModel.GradeRank(model, a.Primary);
            var rb = b.Primary is null ? int.MaxValue : PointsModel.GradeRank(model, b.Primary);
            var strictlyBetter = rb < ra;
            var equal = rb == ra;
            if (!(delta >= rules.DeltaMin && (strictlyBetter || (equal && delta >= 2 * rules.DeltaMin)))) failed.Add("R2");
        }

        // R3 no critical regression in any limited nutrient (per 100 g/ml) and no worse front-of-pack band.
        var liquid = b.Nutrition.IsLiquid;
        foreach (var (fop, key, solidLow, liquidLow) in LimitedNutrients)
        {
            var av = a.Nutrition.Get(key);
            var bv = b.Nutrition.Get(key);
            if (bv is null) { if (av is not null && key != "TRANS_FAT_G") failed.Add("R3"); continue; }
            var low = pack.Fop.FirstOrDefault(t => t.Nutrient == fop && t.Basis != "PER_SERVING_PCT_DV" &&
                                                   (t.PhysicalState == "ANY" || t.PhysicalState == (liquid ? "LIQUID" : "SOLID")))?.LowMax
                      ?? (liquid ? liquidLow : solidLow);
            var limit = Math.Max((1 + rules.LimitedNutrientTolerance) * (av ?? 0), low);
            if (bv > limit) { failed.Add("R3"); break; }
            if (a.FopBands.TryGetValue(fop, out var ba) && b.FopBands.TryGetValue(fop, out var bb) && bb > ba) { failed.Add("R3"); break; }
        }
        if (!failed.Contains("R3"))
            foreach (var fop in new[] { "SUGAR", "SALT" })
                if (a.FopBands.TryGetValue(fop, out var ba) && b.FopBands.TryGetValue(fop, out var bb) && bb > ba) { failed.Add("R3"); break; }

        // R4 no new concerns: no Tier A, no Tier B absent from A, NOVA(B) <= NOVA(A), new Tier C only with 2 x dMin.
        var aCodes = a.Additives.Select(x => x.Code).ToHashSet();
        var introducedC = b.Additives.Where(x => x.Tier == 'C' && !aCodes.Contains(x.Code)).Select(x => x.Code).ToList();
        if (b.HasAdditiveTier('A') || b.Additives.Any(x => x.Tier == 'B' && !aCodes.Contains(x.Code))) failed.Add("R4");
        else if (a.Nova is { } na && (b.Nova is null || b.Nova > na)) failed.Add("R4");
        else if (introducedC.Count > 0 && delta < 2 * rules.DeltaMin) failed.Add("R4");

        // R7 data sufficiency.
        if (b.DataSufficiency != "SUFFICIENT" || b.Confidence < rules.MinAlternativeConfidence || a.DataSufficiency == "INSUFFICIENT") failed.Add("R7");
        if (!b.Eligible) failed.Add("R7");

        return new StaticRuleResult(failed.Count == 0, Math.Round(delta, 2), failed.Distinct().ToList(), ReasonCodes(a, b), introducedC);
    }

    public static List<string> ReasonCodes(HealthProfileData a, HealthProfileData b)
    {
        var codes = new List<string>();
        void Lower(string key, string code, decimal minRel)
        {
            var av = a.Nutrition.Get(key);
            var bv = b.Nutrition.Get(key);
            if (av is > 0 && bv is not null && (av - bv) / av >= minRel) codes.Add(code);
        }
        void Higher(string key, string code)
        {
            var av = a.Nutrition.Get(key) ?? 0;
            var bv = b.Nutrition.Get(key);
            if (bv is not null && bv > av * 1.25m && bv - av >= 1m) codes.Add(code);
        }
        Lower("LIMITED_SUGARS_G", "LOWER_SUGAR", 0.10m);
        Lower("SODIUM_MG", "LOWER_SODIUM", 0.10m);
        Lower("SAT_FAT_G", "LOWER_SAT_FAT", 0.10m);
        Higher("FIBRE_G", "HIGHER_FIBRE");
        Higher("PROTEIN_G", "HIGHER_PROTEIN");
        if (a.Nova is { } na && b.Nova is { } nb && nb < na) codes.Add("LESS_PROCESSED");
        if (a.Additives.Count(x => x.Tier is 'B' or 'C') > b.Additives.Count(x => x.Tier is 'B' or 'C')) codes.Add("FEWER_ADDITIVES_OF_CONCERN");
        if (a.Flags.Contains("ARTIFICIAL_COLOURS") && !b.Flags.Contains("ARTIFICIAL_COLOURS")) codes.Add("NO_ARTIFICIAL_COLOURS");
        return codes;
    }
}

/// <summary>Shopper constraints after consent handling (doc 03 R5, R7, R8).</summary>
public sealed record ShopperConstraints
{
    public string DietType { get; init; } = "ANY";
    public IReadOnlyCollection<string> Allergens { get; init; } = [];
    public bool StrictMayContain { get; init; } = true;
    public IReadOnlyCollection<string> Goals { get; init; } = [];
    public IReadOnlyCollection<string> AvoidIngredients { get; init; } = [];
    public IReadOnlyCollection<string> ExcludedBrands { get; init; } = [];
    public IReadOnlyCollection<string> ReligiousPreferences { get; init; } = [];
    /// <summary>True, or unknown under a STRICT minors policy: no caffeine, no personalization.</summary>
    public bool TreatAsMinor { get; init; }
}

/// <summary>Partner commercial guards and catalog rules (HLD 18, doc 03 R6).</summary>
public sealed record CommercialGuards
{
    public decimal MaxNormalizedPriceRatio { get; init; } = 1.75m;
    public decimal MaxAbsPremiumPerUnit { get; init; } = 150m;
    public decimal PackRatioMin { get; init; } = 0.5m;
    public decimal PackRatioMax { get; init; } = 2.0m;
    public IReadOnlyCollection<string> BlockedBrands { get; init; } = [];
    public IReadOnlyCollection<string> ExcludedCategories { get; init; } = [];
}

public sealed record PriceContext(decimal? OriginalUnitPrice, decimal? AlternativeUnitPrice);

/// <summary>Request-time filters: safety and preferences (R5), data sufficiency for allergic shoppers (R7) and commercial guards (R6).</summary>
public static class RequestRules
{
    public static string? Reject(HealthProfileData original, HealthProfileData alt, ShopperConstraints shopper, CommercialGuards guards,
        PriceContext price, decimal verifiedAllergenConfidence)
    {
        if (!alt.Eligible || alt.Recalled || alt.Quarantined) return "R6_NOT_ELIGIBLE";

        // R5 allergens (CONTAINS always; MAY_CONTAIN when strict) and verified allergen data for declared allergens (R7).
        foreach (var allergen in shopper.Allergens)
        {
            if (alt.Allergens.TryGetValue(allergen, out var presence))
            {
                if (presence == AllergenPresence.Contains) return "R5_ALLERGEN";
                if (presence == AllergenPresence.MayContain && shopper.StrictMayContain) return "R5_ALLERGEN_MAY_CONTAIN";
            }
            if (allergen is "GLUTEN_CEREALS" && alt.Allergens.TryGetValue("WHEAT", out var w) && w == AllergenPresence.Contains) return "R5_ALLERGEN";
        }
        if (shopper.Allergens.Count > 0 && !alt.AllergensVerified && alt.AllergenConfidence < verifiedAllergenConfidence) return "R7_ALLERGEN_UNVERIFIED";

        // R5 diet type (unknown diet data never passes a diet constraint).
        var dietOk = shopper.DietType switch
        {
            "ANY" => true,
            "VEGETARIAN" => alt.DietClass is "VEG" or "VEGAN",
            "EGGETARIAN" => alt.DietClass is "VEG" or "VEGAN" or "EGG",
            "VEGAN" => alt.DietClass == "VEGAN",
            "JAIN" => alt.DietClass is "VEG" or "VEGAN" && alt.JainFriendlyInferred,
            "PESCATARIAN" => alt.DietClass is "VEG" or "VEGAN" or "EGG" ||
                             (alt.DietClass == "NON_VEG" && !alt.IngredientFlags.Contains("MEAT")),
            _ => false,
        };
        if (!dietOk) return "R5_DIET";
        if (shopper.DietType != "ANY" && alt.Flags.Contains("VEG_MARK_CONTRADICTED")) return "R5_DIET";

        foreach (var pref in shopper.ReligiousPreferences)
        {
            var mark = pref == "HALAL" ? "HALAL_CERTIFIED" : pref == "KOSHER" ? "KOSHER_CERTIFIED" : null;
            if (mark is not null && !alt.Marks.Contains(mark)) return "R5_RELIGIOUS_MARK";
        }
        foreach (var avoid in shopper.AvoidIngredients)
        {
            var hit = avoid switch
            {
                "NON_SUGAR_SWEETENERS" => alt.IngredientFlags.Contains("NNS"),
                "ARTIFICIAL_COLOURS" => alt.IngredientFlags.Contains("ARTIFICIAL_COLOUR"),
                "PALM_OIL" => alt.IngredientFlags.Contains("PALM_OIL"),
                "ADDED_MSG" => alt.IngredientFlags.Contains("ADDED_MSG"),
                "CAFFEINE" => alt.IngredientFlags.Contains("CAFFEINE"),
                _ => false,
            };
            if (hit) return "R5_AVOID_" + avoid;
        }
        if (shopper.TreatAsMinor && alt.IngredientFlags.Contains("CAFFEINE")) return "R5_MINOR_CAFFEINE";
        if (alt.Brand is { } brand)
        {
            if (shopper.ExcludedBrands.Any(b => string.Equals(b, brand, StringComparison.OrdinalIgnoreCase))) return "R5_EXCLUDED_BRAND";
            if (guards.BlockedBrands.Any(b => string.Equals(b, brand, StringComparison.OrdinalIgnoreCase))) return "R6_BLOCKED_BRAND";
        }
        if (guards.ExcludedCategories.Any(c => alt.CategoryCode.StartsWith(c, StringComparison.OrdinalIgnoreCase))) return "R5_EXCLUDED_CATEGORY";

        // R6 commercial guards.
        if (original.NetQuantity is { } so && alt.NetQuantity is { } sa && so.IsComparableTo(sa))
        {
            var packRatio = sa.Value / so.Value;
            if (packRatio < guards.PackRatioMin || packRatio > guards.PackRatioMax) return "R6_PACK_RATIO";
        }
        if (price.OriginalUnitPrice is { } po && price.AlternativeUnitPrice is { } pa && po > 0)
        {
            var ratio = NormalizedPriceRatio(po, original.NetQuantity, pa, alt.NetQuantity);
            if (ratio > guards.MaxNormalizedPriceRatio) return "R6_PRICE_RATIO";
            if (pa - po > guards.MaxAbsPremiumPerUnit) return "R6_ABS_PREMIUM";
        }
        return null;
    }

    /// <summary>Price per 100 g/ml (alternative / original); pack-price ratio when sizes are not comparable.</summary>
    public static decimal NormalizedPriceRatio(decimal originalPrice, NetQuantity? originalSize, decimal altPrice, NetQuantity? altSize)
    {
        if (originalPrice <= 0) return decimal.MaxValue;
        if (originalSize is { } so && altSize is { } sa && so.IsComparableTo(sa) && so.Value > 0 && sa.Value > 0)
            return Math.Round(altPrice / sa.Value / (originalPrice / so.Value), 4);
        return Math.Round(altPrice / originalPrice, 4);
    }
}
