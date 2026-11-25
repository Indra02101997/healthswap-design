using HealthSwap.Domain.Commerce;
using HealthSwap.Domain.Recommendation;

namespace HealthSwap.Domain.Tests;

/// <summary>RA-1: INR 1.00 per add of a HealthSwap-recommended (labeled) product, with an evidence chain and undo grace.</summary>
public class RecoAddFeeTests
{
    private static readonly DateTime T0 = new(2026, 11, 4, 10, 15, 30, DateTimeKind.Utc);
    private static RecoFeeSnapshot Fee(Action<RecommendationAddFeeConfig>? _ = null) => RecoAddFeeRule.Snapshot(new RecommendationAddFeeConfig(), "INR")!;

    private static RecoChargeEvidence Evidence(DateTime? impression = null, bool served = true, bool recommended = true, int finals = 0, decimal mtd = 0) => new()
    {
        SuggestionServed = served, SkuRecommended = recommended, FirstImpressionAt = impression ?? T0.AddSeconds(2), SuggestionCreatedAt = T0,
        AttributionExpiresAt = T0.AddHours(24), FinalChargesForSuggestion = finals, MonthToDateAmount = mtd,
    };

    [Fact]
    public void Default_terms_are_one_rupee_per_add_event()
    {
        var s = Fee();
        Assert.Equal("RA-1", s.Rule);
        Assert.Equal(1.00m, s.Amount);
        Assert.Equal("PER_ADD_EVENT", s.Basis);
        Assert.Equal(1.00m, RecoAddFeeRule.Amount(s, 3)); // quantity does not matter per add event
        Assert.Equal(3.00m, RecoAddFeeRule.Amount(s with { Basis = "PER_UNIT" }, 3));
    }

    [Fact]
    public void No_snapshot_when_disabled_or_currency_differs()
    {
        Assert.Null(RecoAddFeeRule.Snapshot(new RecommendationAddFeeConfig { Enabled = false }, "INR"));
        Assert.Null(RecoAddFeeRule.Snapshot(new RecommendationAddFeeConfig(), "USD"));
        Assert.Null(RecoAddFeeRule.Snapshot(new RecommendationAddFeeConfig { Amount = 0 }, "INR"));
    }

    [Theory]
    [InlineData("ADD_TO_CART", true, true)]
    [InlineData("ACCEPT", true, true)]
    [InlineData("ACCEPT", false, false)]
    [InlineData("IMPRESSION", true, false)]
    [InlineData("UNDO", true, false)]
    public void Chargeable_events(string type, bool chargeAccept, bool expected) =>
        Assert.Equal(expected, RecoAddFeeRule.IsChargeableEvent(type, Fee() with { ChargeAccept = chargeAccept }));

    [Fact]
    public void Undo_within_grace_voids_after_grace_does_not()
    {
        var s = Fee();
        Assert.True(RecoAddFeeRule.UndoVoids(T0, T0.AddSeconds(5), s));
        Assert.False(RecoAddFeeRule.UndoVoids(T0, T0.AddSeconds(6), s));
        Assert.False(RecoAddFeeRule.UndoVoids(T0, T0.AddSeconds(-1), s));
    }

    [Fact]
    public void Charges_settle_after_the_window_even_for_late_events()
    {
        var s = Fee();
        Assert.Equal(T0.AddSeconds(600), RecoAddFeeRule.FinalizesAt(T0, T0.AddSeconds(1), s));
        // An add reported an hour late still gets the undo grace from receipt.
        Assert.Equal(T0.AddHours(1).AddSeconds(5), RecoAddFeeRule.FinalizesAt(T0, T0.AddHours(1), s));
    }

    [Fact]
    public void Full_evidence_chain_is_final()
    {
        Assert.Equal(("FINAL", (string?)null), RecoAddFeeRule.Decide(T0.AddSeconds(10), 1m, Fee(), Evidence()));
    }

    [Fact]
    public void Each_missing_link_is_rejected_with_a_reason()
    {
        var s = Fee();
        Assert.Equal(RecoChargeReasons.NotServed, RecoAddFeeRule.Decide(T0.AddSeconds(10), 1m, s, Evidence(served: false)).Reason);
        Assert.Equal(RecoChargeReasons.SkuNotRecommended, RecoAddFeeRule.Decide(T0.AddSeconds(10), 1m, s, Evidence(recommended: false)).Reason);
        Assert.Equal(RecoChargeReasons.NoImpression, RecoAddFeeRule.Decide(T0.AddSeconds(10), 1m, s, Evidence(impression: T0.AddMinutes(5))).Reason);
        Assert.Equal(RecoChargeReasons.NoImpression, RecoAddFeeRule.Decide(T0.AddSeconds(10), 1m, s, Evidence() with { FirstImpressionAt = null }).Reason);
        Assert.Equal(RecoChargeReasons.OutsideWindow, RecoAddFeeRule.Decide(T0.AddHours(25), 1m, s, Evidence()).Reason);
        Assert.Equal(RecoChargeReasons.CapReached, RecoAddFeeRule.Decide(T0.AddSeconds(10), 1m, s with { MaxChargeableAddsPerSuggestion = 2 }, Evidence(finals: 2)).Reason);
        Assert.Equal(RecoChargeReasons.CapReached, RecoAddFeeRule.Decide(T0.AddSeconds(10), 1m, s with { MonthlyCap = 500 }, Evidence(mtd: 500)).Reason);
    }

    [Fact]
    public void Impression_clock_skew_is_tolerated()
    {
        Assert.Equal("FINAL", RecoAddFeeRule.Decide(T0.AddSeconds(10), 1m, Fee(), Evidence(impression: T0.AddSeconds(14))).State);
    }

    [Theory]
    [InlineData("en-IN")]
    [InlineData("hi-IN")]
    [InlineData("en-US")]
    [InlineData("es-US")]
    public void Label_and_disclosure_never_state_the_amount(string locale)
    {
        var label = ShopperCopy.Label(locale, "QuickBasket", addFeeEnabled: true);
        Assert.Equal(ShopperCopy.RecommendedCode, label.Code);
        Assert.True(label.Chargeable);
        Assert.Equal("RA-1", label.FeeRule);
        Assert.DoesNotContain("₹", label.Disclosure);
        Assert.DoesNotContain("$", label.Disclosure);
        Assert.DoesNotMatch(@"\d", label.Disclosure);
        Assert.Contains("QuickBasket", label.Disclosure);
    }

    [Fact]
    public void Disclosure_mentions_add_fee_only_when_enabled()
    {
        Assert.Contains("add a recommended item", ShopperCopy.FeeDisclosure("en-IN", "QuickBasket", true));
        Assert.DoesNotContain("add a recommended item", ShopperCopy.FeeDisclosure("en-IN", "QuickBasket", false));
    }

    [Fact]
    public void Partner_config_validation_covers_ra1()
    {
        var cfg = new PartnerConfig { Fee = new FeeConfig { RecommendationAdd = new RecommendationAddFeeConfig { Basis = "PER_DAY", SettleAfterSeconds = 1, UndoGraceSeconds = 5 } } };
        var errors = cfg.Validate();
        Assert.Contains(errors, e => e.Contains("basis"));
        Assert.Contains(errors, e => e.Contains("settleAfterSeconds"));
        Assert.Empty(new PartnerConfig().Validate());
    }
}

public class LedgerTests
{
    [Fact]
    public void All_postings_balance()
    {
        var id = Guid.NewGuid();
        foreach (var p in new[]
                 {
                     Postings.FeeAccrual(id, 5.80m), Postings.FeeReversal(id, "r1", 2.90m), Postings.FeeFinalize(id, 2.90m), Postings.RecoFee(id, 1.00m),
                     Postings.Statement(id, 4.90m, 0.88m), Postings.Payment("p1", id, 5.78m), Postings.CreditNote(id, 0m, 1.00m, 0.18m),
                 })
        {
            Assert.Equal(p.TotalDebit, p.TotalCredit);
            Assert.All(p.Lines, l => Assert.True((l.Debit == 0) != (l.Credit == 0)));
        }
    }

    [Fact]
    public void Ra1_is_recognized_as_separate_revenue()
    {
        var p = Postings.RecoFee(Guid.NewGuid(), 1.00m);
        Assert.Contains(p.Lines, l => l.Account == Accounts.RecoFeeRevenue && l.Credit == 1.00m);
        Assert.Contains(p.Lines, l => l.Account == Accounts.UnbilledAr && l.Debit == 1.00m);
    }

    [Fact]
    public void Statement_totals_combine_fp1_and_ra1()
    {
        // Smoke-test scenario: FP-1 5.80 with a 2.90 return reversal, plus two RA-1 adds, 18% GST.
        var t = new StatementTotalsData("INR", 5.80m, 2.00m, 2, 2.90m, 0, 0.18m, 0);
        Assert.Equal(7.80m, t.GrossFee);
        Assert.Equal(4.90m, t.NetFee);
        Assert.Equal(0.88m, t.Tax);
        Assert.Equal(5.78m, t.TotalDue);
    }

    [Fact]
    public void Unbalanced_or_negative_postings_are_impossible()
    {
        Assert.Throws<InvalidOperationException>(() => Postings.FeeAccrual(Guid.NewGuid(), -1m));
    }
}
