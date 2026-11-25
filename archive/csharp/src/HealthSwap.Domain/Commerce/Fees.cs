namespace HealthSwap.Domain.Commerce;

public sealed record FairPremiumInput
{
    /// <summary>p_orig: unit price of the original item at suggestion time (snapshot).</summary>
    public required decimal OriginalUnitPrice { get; init; }
    /// <summary>q_orig: original line quantity when the suggestion was served (snapshot).</summary>
    public required decimal OriginalQuantity { get; init; }
    public NetQuantity? OriginalSize { get; init; }
    public decimal? OriginalTaxRate { get; init; }
    /// <summary>q_alt: quantity of the alternative on the order line.</summary>
    public required decimal AlternativeQuantity { get; init; }
    /// <summary>Net amount actually paid for the alternative line (after line and pro-rated order discounts).</summary>
    public required decimal AlternativeNetAmount { get; init; }
    public NetQuantity? AlternativeSize { get; init; }
    public decimal? AlternativeTaxRate { get; init; }
    /// <summary>Quantity of the original product that is still present in the same order.</summary>
    public decimal OriginalQuantityInOrder { get; init; }
    public required string Currency { get; init; }
    public bool ConvertToExTax { get; init; }
}

public sealed record FairPremiumResult(
    decimal ReplacedQuantity,
    decimal OriginalUnitPrice,
    decimal AlternativeNetUnitPrice,
    decimal PackDelta,
    decimal? SizeNormalizedDelta,
    decimal PremiumPerUnit,
    decimal FairPremium,
    decimal Fee);

/// <summary>
/// FP-1 fair-premium rule (HLD 9.2): premium = max(0, min(d_pack, d_size)), fee = round_half_even(r x premium x q_rep).
/// HealthSwap earns only on the health premium, never on extra volume, and never more than the shopper paid extra.
/// </summary>
public static class FairPremiumRule
{
    public const string Version = "FP-1";

    public static FairPremiumResult Compute(FairPremiumInput i, decimal rate)
    {
        var qRep = Math.Max(0m, Math.Min(i.AlternativeQuantity, i.OriginalQuantity - i.OriginalQuantityInOrder));
        var pOrig = i.OriginalUnitPrice;
        var pAlt = i.AlternativeQuantity > 0 ? i.AlternativeNetAmount / i.AlternativeQuantity : 0m;
        if (i.ConvertToExTax)
        {
            pOrig /= 1m + (i.OriginalTaxRate ?? i.AlternativeTaxRate ?? 0m);
            pAlt /= 1m + (i.AlternativeTaxRate ?? i.OriginalTaxRate ?? 0m);
        }
        var dPack = pAlt - pOrig;
        decimal? dSize = null;
        if (i.OriginalSize is { } so && i.AlternativeSize is { } sa && so.IsComparableTo(sa) && so.Value > 0)
            dSize = pAlt - pOrig * (sa.Value / so.Value);
        var premiumPerUnit = Math.Max(0m, dSize is { } ds ? Math.Min(dPack, ds) : dPack);
        var fairPremium = premiumPerUnit * qRep;
        var fee = Currency.Round(rate * fairPremium, i.Currency);
        return new FairPremiumResult(qRep, Math.Round(pOrig, 4), Math.Round(pAlt, 4), Math.Round(dPack, 4),
            dSize is { } d ? Math.Round(d, 4) : null, Math.Round(premiumPerUnit, 4), Math.Round(fairPremium, 4), fee);
    }

    /// <summary>Applies per-line, per-order and per-month caps in that order (HLD 9.2: caps are applied afterwards).</summary>
    public static decimal ApplyCaps(decimal fee, FeeCaps caps, decimal orderFeeSoFar, decimal monthFeeSoFar)
    {
        var capped = fee;
        if (caps.PerLine is { } line) capped = Math.Min(capped, line);
        if (caps.PerOrder is { } order) capped = Math.Min(capped, Math.Max(0m, order - orderFeeSoFar));
        if (caps.PerMonth is { } month) capped = Math.Min(capped, Math.Max(0m, month - monthFeeSoFar));
        return Math.Max(0m, capped);
    }

    /// <summary>Proportional reversal for returns (IN-1: returning 1 of 2 units reverses INR 2.90 of INR 5.80).</summary>
    public static decimal Reversal(decimal fee, decimal alreadyReversed, decimal feeBearingQuantity, decimal returnedQuantity, string currency)
    {
        if (feeBearingQuantity <= 0) return 0m;
        var share = Math.Min(1m, returnedQuantity / feeBearingQuantity);
        var amount = Currency.Round(fee * share, currency);
        return Math.Min(amount, fee - alreadyReversed);
    }
}

/// <summary>RA-1 terms snapshotted on the suggestion when it is served, so later contract changes never re-price it.</summary>
public sealed record RecoFeeSnapshot
{
    public string Rule { get; init; } = RecoAddFeeRule.Version;
    public decimal Amount { get; init; }
    public required string Currency { get; init; }
    public string Basis { get; init; } = "PER_ADD_EVENT";
    public int UndoGraceSeconds { get; init; } = 5;
    public int SettleAfterSeconds { get; init; } = 600;
    public bool RequireImpression { get; init; } = true;
    public int ImpressionToleranceSeconds { get; init; } = 5;
    public bool ChargeAccept { get; init; } = true;
    public int? MaxChargeableAddsPerSuggestion { get; init; }
    public decimal? MonthlyCap { get; init; }
}

public sealed record RecoChargeEvidence
{
    public bool SuggestionServed { get; init; }
    public bool SkuRecommended { get; init; }
    public DateTime? FirstImpressionAt { get; init; }
    public DateTime SuggestionCreatedAt { get; init; }
    public DateTime AttributionExpiresAt { get; init; }
    /// <summary>FINAL charges already recorded for the same suggestion (for the per-suggestion cap).</summary>
    public int FinalChargesForSuggestion { get; init; }
    /// <summary>FINAL RA-1 amount already recorded for this partner in the calendar month (for the monthly cap).</summary>
    public decimal MonthToDateAmount { get; init; }
}

public static class RecoChargeReasons
{
    public const string NotServed = "NOT_SERVED_BY_HEALTHSWAP";
    public const string NoImpression = "NO_IMPRESSION";
    public const string SkuNotRecommended = "SKU_NOT_RECOMMENDED";
    public const string OutsideWindow = "OUTSIDE_ATTRIBUTION_WINDOW";
    public const string UndoneWithinGrace = "UNDONE_WITHIN_GRACE";
    public const string CapReached = "CAP_REACHED";
    public const string NotContracted = "FEE_NOT_CONTRACTED";
}

/// <summary>
/// RA-1 recommendation-add fee: a fixed fee (default INR 1.00) every time the shopper adds a HealthSwap-recommended,
/// labeled product to the cart. Evidence chain: suggestion served by HealthSwap, the label was shown (impression), the
/// added SKU is one of the recommended alternatives, and the add happened inside the attribution window.
/// </summary>
public static class RecoAddFeeRule
{
    public const string Version = "RA-1";

    public static RecoFeeSnapshot? Snapshot(RecommendationAddFeeConfig cfg, string currency)
    {
        if (!cfg.Enabled || cfg.Amount <= 0 || !string.Equals(cfg.Currency, currency, StringComparison.OrdinalIgnoreCase)) return null;
        return new RecoFeeSnapshot
        {
            Rule = cfg.Rule, Amount = cfg.Amount, Currency = cfg.Currency.ToUpperInvariant(), Basis = cfg.Basis,
            UndoGraceSeconds = cfg.UndoGraceSeconds, SettleAfterSeconds = cfg.SettleAfterSeconds, RequireImpression = cfg.RequireImpression,
            ImpressionToleranceSeconds = cfg.ImpressionToleranceSeconds, ChargeAccept = cfg.ChargeAccept,
            MaxChargeableAddsPerSuggestion = cfg.MaxChargeableAddsPerSuggestion, MonthlyCap = cfg.MonthlyCap,
        };
    }

    public static bool IsChargeableEvent(string eventType, RecoFeeSnapshot s) =>
        eventType == "ADD_TO_CART" || (eventType == "ACCEPT" && s.ChargeAccept);

    public static decimal Amount(RecoFeeSnapshot s, decimal? quantity) =>
        s.Basis == "PER_UNIT" ? Currency.Round(s.Amount * Math.Max(1m, quantity ?? 1m), s.Currency) : Currency.Round(s.Amount, s.Currency);

    public static DateTime FinalizesAt(DateTime occurredAt, DateTime receivedAt, RecoFeeSnapshot s)
    {
        var byOccurrence = occurredAt.AddSeconds(s.SettleAfterSeconds);
        var minimum = receivedAt.AddSeconds(Math.Max(s.UndoGraceSeconds, 1));
        return byOccurrence > minimum ? byOccurrence : minimum;
    }

    /// <summary>An UNDO voids the latest add of the same suggestion when it happens within the grace period.</summary>
    public static bool UndoVoids(DateTime addOccurredAt, DateTime undoOccurredAt, RecoFeeSnapshot s) =>
        undoOccurredAt >= addOccurredAt && (undoOccurredAt - addOccurredAt).TotalSeconds <= s.UndoGraceSeconds;

    public static (string State, string? Reason) Decide(DateTime occurredAt, decimal amount, RecoFeeSnapshot s, RecoChargeEvidence e)
    {
        if (!e.SuggestionServed) return ("REJECTED", RecoChargeReasons.NotServed);
        if (!e.SkuRecommended) return ("REJECTED", RecoChargeReasons.SkuNotRecommended);
        var tolerance = TimeSpan.FromSeconds(s.ImpressionToleranceSeconds);
        if (occurredAt < e.SuggestionCreatedAt - tolerance || occurredAt > e.AttributionExpiresAt)
            return ("REJECTED", RecoChargeReasons.OutsideWindow);
        if (s.RequireImpression && (e.FirstImpressionAt is not { } imp || imp > occurredAt + tolerance))
            return ("REJECTED", RecoChargeReasons.NoImpression);
        if (s.MaxChargeableAddsPerSuggestion is { } max && e.FinalChargesForSuggestion >= max)
            return ("REJECTED", RecoChargeReasons.CapReached);
        if (s.MonthlyCap is { } cap && e.MonthToDateAmount + amount > cap)
            return ("REJECTED", RecoChargeReasons.CapReached);
        return ("FINAL", null);
    }
}

public static class AttributionStates
{
    private static readonly Dictionary<string, string[]> Allowed = new()
    {
        ["PENDING"] = ["ACCRUED", "VOID", "REJECTED"],
        ["ACCRUED"] = ["ACCRUED", "REVERSED", "FINAL"],
        ["FINAL"] = ["CREDITED"],
        ["REVERSED"] = [],
        ["VOID"] = [],
        ["REJECTED"] = [],
        ["CREDITED"] = [],
    };

    public static bool CanTransition(string from, string to) => Allowed.TryGetValue(from, out var next) && next.Contains(to);
}
