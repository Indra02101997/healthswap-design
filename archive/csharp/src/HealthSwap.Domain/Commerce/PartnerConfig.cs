using System.Text.Json.Serialization;

namespace HealthSwap.Domain.Commerce;

/// <summary>Versioned per-partner configuration document (HLD section 18 + RA-1). Stored in tenant.partner.config.</summary>
public sealed record PartnerConfig
{
    public List<string> Markets { get; init; } = ["IN"];
    public string Currency { get; init; } = "INR";
    public FeeConfig Fee { get; init; } = new();
    /// <summary>Output tax on the service invoice (for example 18% GST in India; 0 for most US B2B service fees).</summary>
    public decimal TaxRate { get; init; } = 0.18m;
    public int PaymentTermsDays { get; init; } = 30;
    public int DisputeWindowDays { get; init; } = 15;
    public Dictionary<string, int> ReturnWindowDays { get; init; } = new() { ["QUICK_COMMERCE"] = 2, ["SCHEDULED_DELIVERY"] = 7, ["PICKUP"] = 2 };
    public int AttributionWindowHours { get; init; } = 24;
    public List<string> Placements { get; init; } = ["CART", "CHECKOUT", "PDP", "SEARCH", "AGENT"];
    public int MaxSuggestionsPerCart { get; init; } = 3;
    public int MaxAlternativesPerLine { get; init; } = 2;
    public PriceGuards PriceGuards { get; init; } = new();
    /// <summary>TAX_INCLUSIVE (India MRP basis) or TAX_EXCLUSIVE; applies to cart, inventory and order prices.</summary>
    public string PriceBasis { get; init; } = "TAX_INCLUSIVE";
    public PackSizeRatio PackSizeRatio { get; init; } = new();
    public string AvailabilityMode { get; init; } = "MIRROR";
    public int InventoryStalenessSeconds { get; init; } = 600;
    public string ScoringProfile { get; init; } = "IN_DEFAULT_V1";
    public SponsoredConfig Sponsored { get; init; } = new();
    public List<string> BlockedBrands { get; init; } = [];
    public List<string> ExcludedCategories { get; init; } = ["infant-foods", "alcohol", "supplements"];
    public string MinorsPolicy { get; init; } = "STRICT";
    public List<string> Locales { get; init; } = ["en-IN", "hi-IN"];
    public bool KillSwitch { get; init; }
    public List<string> DisabledPlacements { get; init; } = [];
    public ExperimentConfig Experiment { get; init; } = new();
    public decimal ExplorationRate { get; init; } = 0.05m;
    public int MaxLines { get; init; } = 200;

    public int ReturnWindowFor(string? fulfillmentType) =>
        fulfillmentType is not null && ReturnWindowDays.TryGetValue(fulfillmentType, out var d) ? d : ReturnWindowDays.Values.DefaultIfEmpty(7).Max();

    public IReadOnlyList<string> Validate()
    {
        var errors = new List<string>();
        if (Markets.Count == 0) errors.Add("markets must not be empty");
        if (Currency.Length != 3) errors.Add("currency must be ISO 4217");
        if (Fee.Rate is < 0 or > 1) errors.Add("fee.rate must be between 0 and 1");
        if (Fee.RecommendationAdd.Amount < 0) errors.Add("fee.recommendationAdd.amount must be >= 0");
        if (Fee.RecommendationAdd.Basis is not ("PER_ADD_EVENT" or "PER_UNIT")) errors.Add("fee.recommendationAdd.basis must be PER_ADD_EVENT or PER_UNIT");
        if (Fee.RecommendationAdd.Currency.Length != 3) errors.Add("fee.recommendationAdd.currency must be ISO 4217");
        if (Fee.RecommendationAdd.UndoGraceSeconds is < 0 or > 3600) errors.Add("fee.recommendationAdd.undoGraceSeconds must be 0-3600");
        if (Fee.RecommendationAdd.SettleAfterSeconds < Fee.RecommendationAdd.UndoGraceSeconds) errors.Add("fee.recommendationAdd.settleAfterSeconds must be >= undoGraceSeconds");
        if (TaxRate is < 0 or > 1) errors.Add("taxRate must be between 0 and 1");
        if (MaxSuggestionsPerCart is < 0 or > 10) errors.Add("maxSuggestionsPerCart must be 0-10");
        if (MaxAlternativesPerLine is < 1 or > 5) errors.Add("maxAlternativesPerLine must be 1-5");
        if (PriceGuards.MaxNormalizedPriceRatio <= 0) errors.Add("priceGuards.maxNormalizedPriceRatio must be > 0");
        if (AvailabilityMode is not ("MIRROR" or "PARTNER_FILTER")) errors.Add("availabilityMode must be MIRROR or PARTNER_FILTER");
        if (MinorsPolicy is not ("STRICT" or "STANDARD")) errors.Add("minorsPolicy must be STRICT or STANDARD");
        if (Experiment.HoldoutPercent is < 0 or > 100) errors.Add("experiment.holdoutPercent must be 0-100");
        return errors;
    }
}

public sealed record FeeConfig
{
    /// <summary>FP-1 rate: share of the fair premium (0 disables the premium fee).</summary>
    public decimal Rate { get; init; } = 0.10m;
    public string Rule { get; init; } = "FP-1";
    public FeeCaps Caps { get; init; } = new();
    /// <summary>SAME_AS_PRICE or TAX_EXCLUSIVE (convert tax-inclusive prices with the line taxRate before computing FP-1).</summary>
    public string FeeBasis { get; init; } = "SAME_AS_PRICE";
    public RecommendationAddFeeConfig RecommendationAdd { get; init; } = new();
}

public sealed record FeeCaps
{
    [JsonNumberHandling(JsonNumberHandling.AllowReadingFromString)] public decimal? PerLine { get; init; } = 50m;
    [JsonNumberHandling(JsonNumberHandling.AllowReadingFromString)] public decimal? PerOrder { get; init; } = 120m;
    [JsonNumberHandling(JsonNumberHandling.AllowReadingFromString)] public decimal? PerMonth { get; init; }
    public string Currency { get; init; } = "INR";
}

/// <summary>RA-1: fixed fee every time a shopper adds a HealthSwap-recommended (labeled) product to the cart.</summary>
public sealed record RecommendationAddFeeConfig
{
    public bool Enabled { get; init; } = true;
    public string Rule { get; init; } = "RA-1";
    [JsonNumberHandling(JsonNumberHandling.AllowReadingFromString)] public decimal Amount { get; init; } = 1.00m;
    public string Currency { get; init; } = "INR";
    /// <summary>PER_ADD_EVENT (default: one fee per add action) or PER_UNIT (fee x units added by the action).</summary>
    public string Basis { get; init; } = "PER_ADD_EVENT";
    /// <summary>An UNDO within this many seconds of the add voids the charge (matches the 5-second undo in the UX guidelines).</summary>
    public int UndoGraceSeconds { get; init; } = 5;
    /// <summary>Charges become FINAL after this window so that late, batched events (impressions, undos) are considered.</summary>
    public int SettleAfterSeconds { get; init; } = 600;
    /// <summary>The labeled recommendation must have been shown (IMPRESSION) for an add to be billable.</summary>
    public bool RequireImpression { get; init; } = true;
    /// <summary>Clock-skew tolerance between client-side IMPRESSION and add timestamps.</summary>
    public int ImpressionToleranceSeconds { get; init; } = 5;
    public bool ChargeAccept { get; init; } = true;
    public int? MaxChargeableAddsPerSuggestion { get; init; }
    [JsonNumberHandling(JsonNumberHandling.AllowReadingFromString)] public decimal? MonthlyCap { get; init; }
}

public sealed record PriceGuards
{
    public decimal MaxNormalizedPriceRatio { get; init; } = 1.75m;
    [JsonNumberHandling(JsonNumberHandling.AllowReadingFromString)] public decimal MaxAbsPremiumPerUnit { get; init; } = 150m;
}

public sealed record PackSizeRatio
{
    public decimal Min { get; init; } = 0.5m;
    public decimal Max { get; init; } = 2.0m;
}

public sealed record SponsoredConfig
{
    public bool Allowed { get; init; }
}

public sealed record ExperimentConfig
{
    public string Name { get; init; } = "default";
    /// <summary>Share of carts (by stable hash of userRef or cartRef) that get no suggestions (holdout arm).</summary>
    public int HoldoutPercent { get; init; }
}
