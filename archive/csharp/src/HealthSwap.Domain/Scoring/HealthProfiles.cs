using HealthSwap.Domain.Knowledge;

namespace HealthSwap.Domain.Scoring;

/// <summary>Versioned eligibility-rule parameters per market (doc 03 section 3 defaults).</summary>
public sealed record RulesParams
{
    public string Version { get; init; } = "R-1.3";
    public decimal TauSub { get; init; } = 0.70m;
    public decimal DeltaMin { get; init; } = 8m;
    public decimal LimitedNutrientTolerance { get; init; } = 0.10m;
    public int DismissCooldownDays { get; init; } = 7;
    public decimal MinAlternativeConfidence { get; init; } = 0.80m;
    public decimal VerifiedAllergenConfidence { get; init; } = 0.98m;
    public int TargetCeilingPercentile { get; init; } = 60;
    public int MaxEdgesPerProduct { get; init; } = 50;
    public int RetrievalCandidates { get; init; } = 200;
}

public sealed record ClaimThreshold(string ClaimCode, string Nutrient, string PhysicalState, string Basis, decimal? MaxValue, decimal? MinValue, decimal? MinRelativeDifference);

public sealed record UsHealthyGroup(string Group, decimal AddedSugarsMaxG, decimal SodiumMaxMg, decimal SatFatMaxG, string? RequiredFlag);

/// <summary>In-memory jurisdiction pack (reg.* + rec.scoring_model params) for one market.</summary>
public sealed record MarketPack
{
    public required string Market { get; init; }
    public required string PrimaryModel { get; init; }
    public required IReadOnlyDictionary<string, PointsModelParams> PointsModels { get; init; }
    public HsiParams Hsi { get; init; } = new();
    public IReadOnlyList<FopThreshold> Fop { get; init; } = [];
    public IReadOnlyDictionary<string, decimal> DailyValues { get; init; } = new Dictionary<string, decimal>();
    public IReadOnlyList<ClaimThreshold> Claims { get; init; } = [];
    public IReadOnlyDictionary<string, AdditiveRegulation> Additives { get; init; } = new Dictionary<string, AdditiveRegulation>();
    public RulesParams Rules { get; init; } = new();
    public IReadOnlyDictionary<string, UsHealthyGroup> UsHealthyByCategoryPrefix { get; init; } = new Dictionary<string, UsHealthyGroup>();
    public string MethodologyUrl { get; init; } = "https://healthswap.example/methodology";
    public string PackVersion { get; init; } = "1.0";

    public PointsModelParams Primary => PointsModels[PrimaryModel];

    /// <summary>Minimum relative difference for comparative wording ("less", "reduced", "more"); default Codex 25%.</summary>
    public decimal ComparativeThreshold(string nutrient) =>
        Claims.FirstOrDefault(c => c.ClaimCode is "REDUCED" or "INCREASED" && c.Basis == "RELATIVE" && (c.Nutrient == nutrient || c.Nutrient == "ANY"))?.MinRelativeDifference ?? 0.25m;

    public decimal? AbsoluteClaim(string claim, string nutrient, bool liquid) =>
        Claims.FirstOrDefault(c => c.ClaimCode == claim && c.Nutrient == nutrient && (c.PhysicalState == "ANY" || c.PhysicalState == (liquid ? "LIQUID" : "SOLID")))
              is { } c ? c.MaxValue ?? c.MinValue : null;
}

public sealed record AdditiveDetail(string Code, string? ENumber, string Name, string? FunctionalClass, char Tier);

public sealed record BadgeData(string Code, string Label, string Tone);

/// <summary>Denormalized health profile per product and market: the hot-cache document hp:{market}:{productId}.</summary>
public sealed record HealthProfileData
{
    public Guid ProductId { get; init; }
    public required string Market { get; init; }
    public long VersionId { get; init; }
    public string? Gtin { get; init; }
    public required string Name { get; init; }
    public string? Brand { get; init; }
    public string BrandTier { get; init; } = "MAINSTREAM";
    public required string CategoryCode { get; init; }
    public Guid? CategoryId { get; init; }
    public string? CategoryName { get; init; }
    public string ScoringCategory { get; init; } = "GENERAL";
    public string PhysicalState { get; init; } = "SOLID";
    public NetQuantity? NetQuantity { get; init; }
    public decimal? Hsi { get; init; }
    public string? HsiGrade { get; init; }
    public ModelScore? Primary { get; init; }
    public List<ModelScore> Scores { get; init; } = [];
    public int? Nova { get; init; }
    public decimal NovaConfidence { get; init; }
    public List<string> Flags { get; init; } = [];
    public List<BadgeData> Badges { get; init; } = [];
    public Dictionary<string, int> AdditiveTierCounts { get; init; } = [];
    public List<AdditiveDetail> Additives { get; init; } = [];
    public bool? UsFdaHealthy { get; init; }
    public string DataSufficiency { get; init; } = "INSUFFICIENT";
    public decimal Confidence { get; init; }
    public bool Eligible { get; init; }
    public bool Recalled { get; init; }
    public bool Quarantined { get; init; }
    public Nutrition Nutrition { get; init; } = new();
    public decimal? ServingSize { get; init; }
    public string? ServingUnit { get; init; }
    public Dictionary<string, FopBand> FopBands { get; init; } = [];
    public Dictionary<string, AllergenPresence> Allergens { get; init; } = [];
    public bool AllergensVerified { get; init; }
    public decimal AllergenConfidence { get; init; }
    public string DietClass { get; init; } = "UNKNOWN";
    public bool JainFriendlyInferred { get; init; }
    public List<string> Marks { get; init; } = [];
    public List<string> IngredientFlags { get; init; } = [];
    public string? Form { get; init; }
    public string? FlavourFamily { get; init; }
    public string? UseOccasion { get; init; }
    public bool IsPrivateLabel { get; init; }
    public List<string> Sources { get; init; } = [];
    public required string ProfileVersion { get; init; }
    public DateTime ComputedAt { get; init; }

    public int TierCount(char tier) => AdditiveTierCounts.GetValueOrDefault(tier.ToString());
    public bool HasAdditiveTier(char tier) => TierCount(tier) > 0;
}

public static class HealthProfileBuilder
{
    public static HealthProfileData Build(ProductData p, MarketPack pack, DateTime now, string? categoryName = null, IReadOnlyList<string>? sources = null)
    {
        var scores = pack.PointsModels.Values.Select(m => PointsModel.Score(m, p)).ToList();
        var primary = scores.First(s => s.Code == pack.PrimaryModel);

        var additiveDetails = new List<AdditiveDetail>();
        var tierCounts = new Dictionary<string, int> { ["A"] = 0, ["B"] = 0, ["C"] = 0, ["D"] = 0 };
        foreach (var code in p.Additives.Distinct())
        {
            var reg = pack.Additives.GetValueOrDefault(code);
            var tier = AdditiveTiers.Resolve(reg, pack.Market);
            tierCounts[tier.ToString()]++;
            additiveDetails.Add(new AdditiveDetail(code, reg is null ? null : "E" + code.ToUpperInvariant(), reg?.Name ?? "INS " + code,
                reg?.FunctionalClasses.FirstOrDefault(), tier));
        }

        var tierA = tierCounts["A"] > 0;
        var hsi = tierA ? null : Hsi.Compute(primary.Normalized, p.Nova, tierCounts["B"], tierCounts["C"], pack.Hsi);
        var bands = FopBands.Compute(p, pack.Fop, pack.DailyValues);
        var flags = ComputeFlags(p, pack, bands);
        if (tierA) flags.Add("PROHIBITED_ADDITIVE");

        bool? usHealthy = null;
        if (pack.Market == "US")
        {
            var group = pack.UsHealthyByCategoryPrefix.FirstOrDefault(kv => p.CategoryCode.StartsWith(kv.Key, StringComparison.Ordinal)).Value;
            if (group is not null && p.ServingGrams is { } racc)
            {
                var f = racc / 100m;
                usHealthy = (p.Nutrition.LimitedSugars ?? 99) * f <= group.AddedSugarsMaxG &&
                            (p.Nutrition.Sodium ?? 9999) * f <= group.SodiumMaxMg &&
                            (p.Nutrition.SaturatedFatG ?? 99) * f <= group.SatFatMaxG &&
                            (group.RequiredFlag is null || p.Has(group.RequiredFlag));
            }
        }

        return new HealthProfileData
        {
            ProductId = p.ProductId, Market = pack.Market, VersionId = p.VersionId, Gtin = p.Gtin, Name = p.Name, Brand = p.Brand,
            BrandTier = p.BrandTier, CategoryCode = p.CategoryCode, CategoryId = p.CategoryId, CategoryName = categoryName,
            ScoringCategory = p.ScoringCategory, PhysicalState = p.PhysicalState, NetQuantity = p.NetQuantity,
            Hsi = hsi, HsiGrade = Hsi.Grade(hsi), Primary = primary, Scores = scores, Nova = p.Nova, NovaConfidence = p.NovaConfidence,
            Flags = flags.ToList(), Badges = Badges(flags), AdditiveTierCounts = tierCounts, Additives = additiveDetails,
            UsFdaHealthy = usHealthy, DataSufficiency = p.DataSufficiency, Confidence = p.Confidence,
            Eligible = !tierA && !p.Recalled && !p.Quarantined && p.DataSufficiency != "INSUFFICIENT" && hsi is not null,
            Recalled = p.Recalled, Quarantined = p.Quarantined, Nutrition = p.Nutrition, ServingSize = p.ServingSize, ServingUnit = p.ServingUnit,
            FopBands = bands, Allergens = new Dictionary<string, AllergenPresence>(p.Allergens), AllergensVerified = p.AllergensVerified,
            AllergenConfidence = p.AllergenConfidence, DietClass = p.DietClass, JainFriendlyInferred = p.JainFriendlyInferred,
            Marks = p.Marks.ToList(),
            IngredientFlags = p.IngredientFlags.Where(f => f is "NNS" or "ARTIFICIAL_COLOUR" or "PALM_OIL" or "ADDED_MSG" or "CAFFEINE" or "WHOLE_GRAIN" or "REFINED_FLOUR" or "PHO" or "ADDED_SUGAR").ToList(),
            Form = p.Form, FlavourFamily = p.FlavourFamily, UseOccasion = p.UseOccasion, IsPrivateLabel = p.IsPrivateLabel,
            Sources = sources?.ToList() ?? [],
            ProfileVersion = $"{pack.Market}:{primary.Code}@{primary.Version}+HSI@{pack.Hsi.Version}#v{p.VersionId}",
            ComputedAt = now,
        };
    }

    public static SortedSet<string> ComputeFlags(ProductData p, MarketPack pack, IReadOnlyDictionary<string, FopBand> bands)
    {
        var flags = new SortedSet<string>(StringComparer.Ordinal);
        if (bands.GetValueOrDefault("SUGAR") == FopBand.High || bands.GetValueOrDefault("SUGAD") == FopBand.High) flags.Add("HIGH_SUGAR");
        if (bands.GetValueOrDefault("NA") == FopBand.High || bands.GetValueOrDefault("SALT") == FopBand.High) flags.Add("HIGH_SODIUM");
        if (bands.GetValueOrDefault("FASAT") == FopBand.High) flags.Add("HIGH_SAT_FAT");
        if ((p.Nutrition.TransFatG ?? 0) > 0.2m || p.Has("PHO")) flags.Add("CONTAINS_TRANS_FAT");
        if (p.Nova == 4) flags.Add("ULTRA_PROCESSED");
        if (p.Has("ARTIFICIAL_COLOUR")) flags.Add("ARTIFICIAL_COLOURS");
        if (p.Has("NNS")) flags.Add("NON_SUGAR_SWEETENERS");
        if (p.Ingredients.FirstOrDefault(i => i.ParentPosition is null)?.Flags.Contains("WHOLE_GRAIN") == true) flags.Add("WHOLE_GRAIN");
        var highFibre = pack.AbsoluteClaim("HIGH_FIBRE", "FIBTG", p.Nutrition.IsLiquid) ?? 6m;
        if ((p.Nutrition.FibreG ?? 0) >= highFibre) flags.Add("HIGH_FIBRE");
        if (p.Nutrition.ProteinG is { } prot && p.Nutrition.EnergyKcal is { } kcal && kcal > 0 && prot * 4 / kcal >= 0.20m) flags.Add("HIGH_PROTEIN");
        if (!p.Has("ADDED_SUGAR") && !p.Has("NNS") && p.IngredientResolution >= 0.9m && (p.Nutrition.AddedSugarsG ?? 0) == 0 && p.Ingredients.Count > 0)
            flags.Add("NO_ADDED_SUGAR");
        if (p.Recalled) flags.Add("RECALLED");
        return flags;
    }

    private static List<BadgeData> Badges(IEnumerable<string> flags)
    {
        var list = new List<BadgeData>();
        foreach (var f in flags)
        {
            var b = f switch
            {
                "HIGH_SUGAR" => new BadgeData(f, "High in sugar", "CAUTION"),
                "HIGH_SODIUM" => new BadgeData(f, "High in sodium", "CAUTION"),
                "HIGH_SAT_FAT" => new BadgeData(f, "High in saturated fat", "CAUTION"),
                "CONTAINS_TRANS_FAT" => new BadgeData(f, "Contains trans fat", "CAUTION"),
                "ULTRA_PROCESSED" => new BadgeData(f, "Ultra-processed", "NEUTRAL"),
                "ARTIFICIAL_COLOURS" => new BadgeData(f, "Artificial colours", "NEUTRAL"),
                "NON_SUGAR_SWEETENERS" => new BadgeData(f, "Contains sweeteners", "NEUTRAL"),
                "WHOLE_GRAIN" => new BadgeData(f, "Whole grain", "POSITIVE"),
                "HIGH_FIBRE" => new BadgeData(f, "High in fibre", "POSITIVE"),
                "HIGH_PROTEIN" => new BadgeData(f, "High in protein", "POSITIVE"),
                "NO_ADDED_SUGAR" => new BadgeData(f, "No added sugar", "POSITIVE"),
                "RECALLED" => new BadgeData(f, "Recalled", "CAUTION"),
                _ => null,
            };
            if (b is not null) list.Add(b);
        }
        return list;
    }
}
