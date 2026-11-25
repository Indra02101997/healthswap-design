namespace HealthSwap.Domain.Commerce;

public static class Accounts
{
    public const string UnbilledAr = "UNBILLED_AR";
    public const string BilledAr = "BILLED_AR";
    public const string DeferredRevenue = "DEFERRED_REVENUE";
    public const string FeeRevenue = "FEE_REVENUE";
    public const string RecoFeeRevenue = "RECO_FEE_REVENUE";
    public const string OutputTax = "OUTPUT_TAX";
    public const string Cash = "CASH";
}

public sealed record PostingLine(string Account, decimal Debit, decimal Credit);

/// <summary>A balanced double-entry posting (HLD 9.6). Corrections are always new reversing entries, never updates.</summary>
public sealed record LedgerPosting(string Kind, string ReferenceType, string ReferenceId, string IdempotencyKey, IReadOnlyList<PostingLine> Lines)
{
    public decimal TotalDebit => Lines.Sum(l => l.Debit);
    public decimal TotalCredit => Lines.Sum(l => l.Credit);
}

public static class Postings
{
    public static LedgerPosting FeeAccrual(Guid attributionId, decimal fee) =>
        Make("FEE_ACCRUAL", "ATTRIBUTION", attributionId.ToString(), $"FEE_ACCRUAL:{attributionId}",
            (Accounts.UnbilledAr, fee, 0), (Accounts.DeferredRevenue, 0, fee));

    public static LedgerPosting FeeReversal(Guid attributionId, string adjustmentId, decimal amount) =>
        Make("FEE_REVERSAL", "ATTRIBUTION", attributionId.ToString(), $"FEE_REVERSAL:{attributionId}:{adjustmentId}",
            (Accounts.DeferredRevenue, amount, 0), (Accounts.UnbilledAr, 0, amount));

    public static LedgerPosting FeeFinalize(Guid attributionId, decimal amount) =>
        Make("FEE_FINALIZE", "ATTRIBUTION", attributionId.ToString(), $"FEE_FINALIZE:{attributionId}",
            (Accounts.DeferredRevenue, amount, 0), (Accounts.FeeRevenue, 0, amount));

    /// <summary>RA-1: recognized when the charge becomes FINAL (no return contingency); billed at month close.</summary>
    public static LedgerPosting RecoFee(Guid chargeId, decimal amount) =>
        Make("RECO_FEE", "RECO_CHARGE", chargeId.ToString(), $"RECO_FEE:{chargeId}",
            (Accounts.UnbilledAr, amount, 0), (Accounts.RecoFeeRevenue, 0, amount));

    public static LedgerPosting Statement(Guid statementId, decimal netFee, decimal tax) =>
        Make("STATEMENT", "STATEMENT", statementId.ToString(), $"STATEMENT:{statementId}",
            (Accounts.BilledAr, netFee + tax, 0), (Accounts.UnbilledAr, 0, netFee), (Accounts.OutputTax, 0, tax));

    public static LedgerPosting Payment(string paymentRef, Guid statementId, decimal amount) =>
        Make("PAYMENT", "PAYMENT", paymentRef, $"PAYMENT:{statementId}:{paymentRef}",
            (Accounts.Cash, amount, 0), (Accounts.BilledAr, 0, amount));

    /// <summary>Credit note after a dispute is upheld on an issued statement: revenue contra + output tax, against billed AR.</summary>
    public static LedgerPosting CreditNote(Guid disputeId, decimal premiumPart, decimal recoPart, decimal tax) =>
        Make("CREDIT_NOTE", "DISPUTE", disputeId.ToString(), $"CREDIT_NOTE:{disputeId}",
            (Accounts.FeeRevenue, premiumPart, 0), (Accounts.RecoFeeRevenue, recoPart, 0), (Accounts.OutputTax, tax, 0),
            (Accounts.BilledAr, 0, premiumPart + recoPart + tax));

    private static LedgerPosting Make(string kind, string refType, string refId, string key, params (string Account, decimal Debit, decimal Credit)[] lines)
    {
        var posting = new LedgerPosting(kind, refType, refId, key,
            lines.Where(l => l.Debit != 0 || l.Credit != 0).Select(l => new PostingLine(l.Account, l.Debit, l.Credit)).ToList());
        if (posting.Lines.Any(l => l.Debit < 0 || l.Credit < 0)) throw new InvalidOperationException("Negative posting amounts are not allowed");
        if (posting.TotalDebit != posting.TotalCredit) throw new InvalidOperationException($"Unbalanced posting {kind}");
        return posting;
    }
}

/// <summary>Statement totals (HLD 9.7, OpenAPI StatementTotals): gross = FP-1 + RA-1; net = gross - reversals - credits.</summary>
public sealed record StatementTotalsData(string Currency, decimal PremiumFee, decimal RecoFee, int RecoChargeCount, decimal Reversals, decimal Credits, decimal TaxRate, decimal CreditTax)
{
    public decimal GrossFee => PremiumFee + RecoFee;
    public decimal NetFee => GrossFee - Reversals - Credits;
    /// <summary>Tax on the originally billed net fee minus the tax already credited (keeps the ledger exact).</summary>
    public decimal Tax => Currency is null ? 0 : HealthSwap.Domain.Currency.Round((GrossFee - Reversals) * TaxRate, Currency) - CreditTax;
    public decimal TotalDue => NetFee + Tax;
}
