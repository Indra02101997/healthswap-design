namespace HealthSwap.Domain.Knowledge;

public static class Basis
{
    public const string Per100G = "PER_100G";
    public const string Per100Ml = "PER_100ML";
    public const string PerServing = "PER_SERVING";
}

public enum AllergenPresence { Contains, MayContain, FreeFrom }

/// <summary>Nutrition per 100 g (solids) or per 100 ml (liquids) - the primary basis for every model (doc 02 7.1).</summary>
public sealed record Nutrition
{
    public string Basis { get; init; } = Knowledge.Basis.Per100G;
    public decimal? EnergyKcal { get; init; }
    public decimal? ProteinG { get; init; }
    public decimal? CarbohydrateG { get; init; }
    /// <summary>TOTAL_INCL_FIBRE (US "total carbohydrate") or AVAILABLE (EU/IN). </summary>
    public string CarbDefinition { get; init; } = "AVAILABLE";
    public decimal? TotalSugarsG { get; init; }
    public decimal? AddedSugarsG { get; init; }
    public decimal? TotalFatG { get; init; }
    public decimal? SaturatedFatG { get; init; }
    public decimal? TransFatG { get; init; }
    public decimal? CholesterolMg { get; init; }
    public decimal? SodiumMg { get; init; }
    public decimal? SaltG { get; init; }
    public decimal? FibreG { get; init; }
    /// <summary>Fruit, vegetables, legumes and nuts share (%), from QUID or estimated from ingredients.</summary>
    public decimal? FvlnPct { get; init; }
    public bool FvlnEstimated { get; init; }

    public bool IsLiquid => Basis == Knowledge.Basis.Per100Ml;

    /// <summary>Sodium in mg; derived from salt (salt = sodium x 2.5) when only salt is declared.</summary>
    public decimal? Sodium => SodiumMg ?? (SaltG is { } s ? Math.Round(s * 400m, 1) : null);
    public decimal? Salt => SaltG ?? (SodiumMg is { } na ? Math.Round(na * 2.5m / 1000m, 3) : null);
    public decimal? EnergyKj => EnergyKcal is { } k ? Math.Round(k * 4.184m, 1) : null;

    /// <summary>Available carbohydrate: US total carbohydrate includes dietary fibre.</summary>
    public decimal? AvailableCarbohydrate =>
        CarbohydrateG is not { } c ? null : CarbDefinition == "TOTAL_INCL_FIBRE" ? Math.Max(0, c - (FibreG ?? 0)) : c;

    /// <summary>Added sugars when declared, otherwise total sugars (doc 02 7.1: models fall back to total sugars).</summary>
    public decimal? LimitedSugars => AddedSugarsG ?? TotalSugarsG;

    public decimal? Get(string nutrient) => nutrient switch
    {
        "ENERGY_KCAL" => EnergyKcal,
        "ENERGY_KJ" => EnergyKj,
        "PROTEIN_G" => ProteinG,
        "CARB_G" => CarbohydrateG,
        "SUGARS_G" => TotalSugarsG,
        "ADDED_SUGARS_G" => AddedSugarsG,
        "LIMITED_SUGARS_G" => LimitedSugars,
        "FAT_G" => TotalFatG,
        "SAT_FAT_G" => SaturatedFatG,
        "TRANS_FAT_G" => TransFatG,
        "SODIUM_MG" => Sodium,
        "SALT_G" => Salt,
        "FIBRE_G" => FibreG,
        "FVL_PCT" => FvlnPct,
        _ => null,
    };
}

/// <summary>A parsed ingredient token (doc 02 7.2).</summary>
public sealed record ParsedIngredient(
    int Position,
    int? ParentPosition,
    string RawText,
    string? CanonicalName,
    decimal? Percent,
    bool IsAdditive,
    string? AdditiveCode,
    IReadOnlyCollection<string> Flags);

/// <summary>Canonical product record used by scoring, eligibility and explanations (one product version).</summary>
public sealed record ProductData
{
    public Guid ProductId { get; init; }
    public long VersionId { get; init; }
    public string? Gtin { get; init; }
    public required string Name { get; init; }
    public string? Brand { get; init; }
    /// <summary>PREMIUM, MAINSTREAM or PRIVATE_LABEL (used for swap-graph diversity).</summary>
    public string BrandTier { get; init; } = "MAINSTREAM";
    public required string CategoryCode { get; init; }
    public Guid? CategoryId { get; init; }
    /// <summary>Model routing: GENERAL, BEVERAGE, WATER, FAT_OIL_NUT, CHEESE, RED_MEAT.</summary>
    public string ScoringCategory { get; init; } = "GENERAL";
    public string PhysicalState { get; init; } = "SOLID";
    public NetQuantity? NetQuantity { get; init; }
    public int PackCount { get; init; } = 1;
    public IReadOnlyList<string> Markets { get; init; } = [];
    public bool IsPrivateLabel { get; init; }
    public Nutrition Nutrition { get; init; } = new();
    public decimal? ServingSize { get; init; }
    public string? ServingUnit { get; init; }
    public string? IngredientsText { get; init; }
    public IReadOnlyList<ParsedIngredient> Ingredients { get; init; } = [];
    public decimal IngredientResolution { get; init; }
    /// <summary>Additive codes (INS numbers, for example 322, 150d, 951).</summary>
    public IReadOnlyList<string> Additives { get; init; } = [];
    public IReadOnlySet<string> IngredientFlags { get; init; } = new HashSet<string>();
    public IReadOnlyDictionary<string, AllergenPresence> Allergens { get; init; } = new Dictionary<string, AllergenPresence>();
    /// <summary>True when allergen data is curator- or brand-verified (R7 requirement for shoppers with declared allergens).</summary>
    public bool AllergensVerified { get; init; }
    public decimal AllergenConfidence { get; init; }
    public IReadOnlyList<string> Marks { get; init; } = [];
    /// <summary>VEGAN, VEG, EGG, NON_VEG or UNKNOWN.</summary>
    public string DietClass { get; init; } = "UNKNOWN";
    public bool JainFriendlyInferred { get; init; }
    public int? Nova { get; init; }
    public decimal NovaConfidence { get; init; }
    public string DataSufficiency { get; init; } = "INSUFFICIENT";
    public decimal Confidence { get; init; }
    public string Verification { get; init; } = "UNVERIFIED";
    /// <summary>Substitutability attributes: form (BAR, NOODLE, BISCUIT, ...), flavour family, use occasion, cuisine.</summary>
    public string? Form { get; init; }
    public string? FlavourFamily { get; init; }
    public string? UseOccasion { get; init; }
    public string? Cuisine { get; init; }
    public bool Recalled { get; init; }
    public bool Quarantined { get; init; }

    public bool Has(string ingredientFlag) => IngredientFlags.Contains(ingredientFlag);
    public bool ContainsCaffeine => IngredientFlags.Contains("CAFFEINE");
    public decimal? ServingGrams => ServingSize is { } s && ServingUnit is "g" or "ml" ? s : null;
}
