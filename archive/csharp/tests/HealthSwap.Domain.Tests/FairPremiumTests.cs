using FsCheck;
using FsCheck.Fluent;
using FsCheck.Xunit;
using HealthSwap.Domain;
using HealthSwap.Domain.Commerce;

namespace HealthSwap.Domain.Tests;

/// <summary>FP-1 worked examples from docs/01-hld.md section 9.3 and properties of the fair-premium rule.</summary>
public class FairPremiumTests
{
    private static FairPremiumResult Compute(decimal pOrig, decimal qOrig, NetQuantity? sOrig, decimal qAlt, decimal netAlt, NetQuantity? sAlt, string currency = "INR",
        decimal originalInOrder = 0) =>
        FairPremiumRule.Compute(new FairPremiumInput
        {
            OriginalUnitPrice = pOrig, OriginalQuantity = qOrig, OriginalSize = sOrig, AlternativeQuantity = qAlt, AlternativeNetAmount = netAlt, AlternativeSize = sAlt,
            Currency = currency, OriginalQuantityInOrder = originalInOrder,
        }, 0.10m);

    [Fact]
    public void IN1_millet_noodles_fee_is_5_80()
    {
        // ₹56 x 2 noodles (280 g) -> millet noodles 256 g, list ₹89, ₹8 coupon pro-rated -> net ₹170 for 2 (₹85 each).
        var r = Compute(56m, 2, new NetQuantity(280, "g"), 2, 170m, new NetQuantity(256, "g"));
        Assert.Equal(29.00m, r.PackDelta);
        Assert.Equal(33.80m, r.SizeNormalizedDelta);
        Assert.Equal(29.00m, r.PremiumPerUnit);
        Assert.Equal(2, r.ReplacedQuantity);
        Assert.Equal(5.80m, r.Fee);
    }

    [Fact]
    public void IN2_bigger_pack_is_charged_on_size_normalized_premium_only()
    {
        var r = Compute(100m, 1, new NetQuantity(500, "g"), 1, 240m, new NetQuantity(1000, "g"));
        Assert.Equal(140.00m, r.PackDelta);
        Assert.Equal(40.00m, r.SizeNormalizedDelta);
        Assert.Equal(4.00m, r.Fee); // not ₹14: HealthSwap never earns on extra volume
    }

    [Fact]
    public void IN3_cheaper_swap_earns_nothing()
    {
        var r = Compute(60m, 1, new NetQuantity(400, "g"), 1, 45m, new NetQuantity(400, "g"));
        Assert.Equal(-15.00m, r.PackDelta);
        Assert.Equal(0m, r.Fee);
    }

    [Fact]
    public void US1_cereal_fee_is_12_cents()
    {
        var r = Compute(4.29m, 1, NetQuantity.Normalize(13.5m, "oz"), 1, 5.49m, NetQuantity.Normalize(12m, "oz"), "USD");
        Assert.Equal(1.20m, r.PackDelta);
        Assert.Equal(0.12m, r.Fee);
    }

    [Fact]
    public void Original_still_in_the_order_reduces_the_replaced_quantity()
    {
        var r = Compute(56m, 2, new NetQuantity(280, "g"), 2, 170m, new NetQuantity(256, "g"), originalInOrder: 1);
        Assert.Equal(1, r.ReplacedQuantity);
        Assert.Equal(2.90m, r.Fee);
    }

    [Fact]
    public void Returning_one_of_two_units_reverses_half()
    {
        Assert.Equal(2.90m, FairPremiumRule.Reversal(5.80m, 0, 2, 1, "INR"));
        Assert.Equal(2.90m, FairPremiumRule.Reversal(5.80m, 2.90m, 2, 2, "INR")); // never more than what is left
    }

    [Fact]
    public void Caps_apply_per_line_then_order_then_month()
    {
        var caps = new FeeCaps { PerLine = 50, PerOrder = 120, PerMonth = 1000 };
        Assert.Equal(50m, FairPremiumRule.ApplyCaps(80m, caps, 0, 0));
        Assert.Equal(20m, FairPremiumRule.ApplyCaps(40m, caps, 100m, 0));
        Assert.Equal(0m, FairPremiumRule.ApplyCaps(40m, caps, 0, 1000m));
    }

    [Fact]
    public void Rounding_is_half_even_in_minor_units()
    {
        Assert.Equal(0.12m, Currency.Round(0.125m, "USD"));
        Assert.Equal(0.14m, Currency.Round(0.135m, "USD"));
        Assert.Equal(100m, Currency.Round(100.4m, "JPY"));
    }

    [Property(MaxTest = 500)]
    public Property Fee_never_exceeds_rate_times_extra_paid(PositiveInt origCents, PositiveInt altCents, PositiveInt qty)
    {
        var pOrig = origCents.Get % 100_000 / 100m + 1;
        var pAlt = altCents.Get % 100_000 / 100m + 1;
        var q = qty.Get % 10 + 1;
        var r = Compute(pOrig, q, null, q, pAlt * q, null);
        var extraPaid = Math.Max(0, pAlt - pOrig) * q;
        return (r.Fee >= 0 && r.Fee <= Currency.Round(0.10m * extraPaid, "INR") && (pAlt > pOrig || r.Fee == 0)).ToProperty();
    }

    [Property(MaxTest = 300)]
    public Property Bigger_packs_never_increase_the_fee_beyond_the_pack_premium(PositiveInt origCents, PositiveInt sizeFactor)
    {
        var pOrig = origCents.Get % 50_000 / 100m + 1;
        var factor = sizeFactor.Get % 4 + 1;
        var pAlt = pOrig * factor + 5m; // a bigger pack that costs more in absolute terms
        var r = Compute(pOrig, 1, new NetQuantity(100, "g"), 1, pAlt, new NetQuantity(100 * factor, "g"));
        return (r.PremiumPerUnit <= 5.0001m).ToProperty();
    }
}
