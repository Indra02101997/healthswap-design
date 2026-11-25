using System.Globalization;
using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Domain.Commerce;
using HealthSwap.Platform.Data;
using Microsoft.Extensions.Logging;

namespace HealthSwap.Platform.Services;

public sealed record StatementCloseResult(Guid PartnerId, string Period, Guid? StatementId, string? InvoiceNumber, decimal PremiumFee, decimal RecoFee, int RecoChargeCount,
    decimal Reversals, decimal Tax, decimal TotalDue, string Outcome);

public sealed record DisputeResolution(string Decision, List<DisputedLine>? UpheldLines, List<Guid>? UpheldChargeIds, string? Resolution);

public sealed record TrialBalanceRow(string Account, string Currency, decimal Debit, decimal Credit, decimal Balance);

/// <summary>
/// Billing (HLD 9.6-9.8): month close per partner and currency (FINAL FP-1 attributions + FINAL RA-1 charges), invoice numbers,
/// STATEMENT postings, disputes with credit notes, payments and a trial balance for the admin plane.
/// </summary>
public sealed class BillingService(Db db, LedgerService ledger, PartnerDirectory partners, IClock clock, ILogger<BillingService> log)
{
    private sealed class StatementRow
    {
        public Guid StatementId { get; set; }
        public Guid PartnerId { get; set; }
        public DateTime PeriodStart { get; set; }
        public string Currency { get; set; } = "";
        public string Status { get; set; } = "";
        public decimal PremiumFee { get; set; }
        public decimal RecoFee { get; set; }
        public int RecoChargeCount { get; set; }
        public decimal GrossFee { get; set; }
        public decimal Reversals { get; set; }
        public decimal Credits { get; set; }
        public decimal NetFee { get; set; }
        public decimal TaxRate { get; set; }
        public decimal TaxAmount { get; set; }
        public decimal TotalDue { get; set; }
        public decimal PaidAmount { get; set; }
        public int LineCount { get; set; }
        public string? InvoiceNumber { get; set; }
        public DateTime? IssuedAt { get; set; }
        public DateTime? DueAt { get; set; }
    }

    private sealed class AttrBill { public Guid AttributionId { get; set; } public string OrderId { get; set; } = ""; public string LineId { get; set; } = ""; public Guid SuggestionId { get; set; } public decimal FairPremium { get; set; } public decimal FeeAmount { get; set; } public decimal ReversedAmount { get; set; } public DateTime? DeliveredAt { get; set; } }
    private sealed class ChargeBill { public Guid ChargeId { get; set; } public decimal Amount { get; set; } public decimal CreditedAmount { get; set; } }
    private sealed class LineRow { public int LineNo { get; set; } public string LineType { get; set; } = ""; public string OrderId { get; set; } = ""; public string OrderLineId { get; set; } = ""; public Guid SuggestionId { get; set; } public DateTime? DeliveredAt { get; set; } public decimal FairPremium { get; set; } public decimal Amount { get; set; } public Guid AttributionId { get; set; } }
    private sealed class DisputeRow
    {
        public Guid DisputeId { get; set; }
        public Guid PartnerId { get; set; }
        public Guid StatementId { get; set; }
        public string Reason { get; set; } = "";
        public string DisputedLines { get; set; } = "[]";
        public Guid[] DisputedChargeIds { get; set; } = [];
        public string? Comment { get; set; }
        public string Status { get; set; } = "";
        public decimal? CreditNoteAmount { get; set; }
        public decimal? CreditNoteTax { get; set; }
        public DateTime OpenedAt { get; set; }
        public DateTime? ResolvedAt { get; set; }
        public string? Resolution { get; set; }
    }

    private const string StatementColumns =
        """
        statement_id, partner_id, period_start, currency, status, premium_fee, reco_fee, reco_charge_count, gross_fee, reversals, credits, net_fee, tax_rate,
        tax_amount, total_due, paid_amount, line_count, invoice_number, issued_at, due_at
        """;

    private const string DisputeColumns =
        "dispute_id, partner_id, statement_id, reason, disputed_lines::text as disputed_lines, disputed_charge_ids, comment, status, credit_note_amount, credit_note_tax, opened_at, resolved_at, resolution";

    public async Task<List<StatementCloseResult>> CloseMonthAsync(string period, Guid? partnerId, CancellationToken ct)
    {
        if (!Period.TryParse(period, out var start, out var end)) throw HsException.Invalid("period must be YYYY-MM");
        var targets = partnerId is { } p ? [await partners.GetAsync(p, ct)] : (await partners.ListAllAsync(ct)).Where(x => x.Status is "LIVE" or "SHADOW" or "SANDBOX").ToList();
        var results = new List<StatementCloseResult>();
        foreach (var partner in targets) results.Add(await CloseAsync(partner, period, start, end, ct));
        return results;
    }

    private async Task<StatementCloseResult> CloseAsync(PartnerInfo partner, string period, DateTime start, DateTime end, CancellationToken ct)
    {
        var cfg = partner.Config;
        var currency = cfg.Currency;
        var now = clock.UtcNow;
        return await db.InTxAsync(DbRole.LedgerWriter, partner.PartnerId, async u =>
        {
            var existing = await u.QueryFirstOrDefaultAsync<StatementRow>($"select {StatementColumns} from billing.statement where period_start = @start and currency = @currency",
                new { start = start.Date, currency }, ct);
            if (existing is not null)
                return new StatementCloseResult(partner.PartnerId, period, existing.StatementId, existing.InvoiceNumber, existing.PremiumFee, existing.RecoFee, existing.RecoChargeCount,
                    existing.Reversals, existing.TaxAmount, existing.TotalDue, "ALREADY_CLOSED");
            var attributions = (await u.QueryAsync<AttrBill>(
                """
                select a.attribution_id, a.order_id, a.line_id, a.suggestion_id, a.fair_premium, a.fee_amount, a.reversed_amount, h.delivered_at
                from txn.attribution a
                left join txn.order_header h on h.partner_id = a.partner_id and h.order_id = a.order_id
                where a.state = 'FINAL' and a.statement_id is null and a.finalized_at < @end and a.currency = @currency
                order by a.order_id, a.line_id
                """, new { end, currency }, ct)).ToList();
            var recoCharges = (await u.QueryAsync<ChargeBill>(
                "select charge_id, amount, credited_amount from txn.reco_charge where state = 'FINAL' and statement_id is null and finalized_at < @end and currency = @currency",
                new { end, currency }, ct)).ToList();
            if (attributions.Count == 0 && recoCharges.Count == 0)
                return new StatementCloseResult(partner.PartnerId, period, null, null, 0, 0, 0, 0, 0, 0, "NOTHING_TO_BILL");

            var totals = new StatementTotalsData(currency, attributions.Sum(a => a.FeeAmount), recoCharges.Sum(c => c.Amount), recoCharges.Count,
                attributions.Sum(a => a.ReversedAmount), 0, cfg.TaxRate, 0);
            var statementId = Ids.NewId();
            var seq = await u.ScalarAsync<long>("select nextval('billing.invoice_no_seq')", ct: ct);
            var invoice = $"HS-{partner.Geography}-{start:yyyy}-{start:MM}-{seq:D6}";
            var lineNo = 0;
            var lines = new List<(int No, string Type, AttrBill A, decimal Amount)>();
            foreach (var a in attributions)
            {
                lines.Add((++lineNo, "FEE", a, a.FeeAmount));
                if (a.ReversedAmount > 0) lines.Add((++lineNo, "REVERSAL", a, -a.ReversedAmount));
            }
            await u.ExecuteAsync(
                """
                insert into billing.statement (statement_id, partner_id, period_start, period_end, currency, status, premium_fee, reco_fee, reco_charge_count, gross_fee,
                    reversals, credits, net_fee, tax_rate, tax_amount, total_due, paid_amount, line_count, invoice_number, issued_at, due_at)
                values (@statementId, @p, @start, @periodEnd, @currency, 'ISSUED', @premium, @reco, @count, @gross, @reversals, 0, @net, @rate, @tax, @due, 0, @lines,
                    @invoice, @now, @dueAt)
                """,
                new
                {
                    statementId, p = partner.PartnerId, start = start.Date, periodEnd = end.AddDays(-1).Date, currency, premium = totals.PremiumFee, reco = totals.RecoFee,
                    count = totals.RecoChargeCount, gross = totals.GrossFee, reversals = totals.Reversals, net = totals.NetFee, rate = cfg.TaxRate, tax = totals.Tax,
                    due = totals.TotalDue, lines = lines.Count, invoice, now, dueAt = now.AddDays(cfg.PaymentTermsDays),
                }, ct);
            foreach (var (no, type, a, amount) in lines)
                await u.ExecuteAsync(
                    """
                    insert into billing.statement_line (statement_id, line_no, partner_id, attribution_id, line_type, order_id, order_line_id, suggestion_id, delivered_at, fair_premium, amount)
                    values (@statementId, @no, @p, @aid, @type, @o, @l, @sid, @delivered, @premium, @amount)
                    """,
                    new { statementId, no, p = partner.PartnerId, aid = a.AttributionId, type, o = a.OrderId, l = a.LineId, sid = a.SuggestionId, delivered = a.DeliveredAt,
                          premium = a.FairPremium, amount }, ct);
            await u.ExecuteAsync("update txn.attribution set statement_id = @statementId where attribution_id = any(@ids)", new { statementId, ids = attributions.Select(a => a.AttributionId).ToArray() }, ct);
            await u.ExecuteAsync("update txn.reco_charge set statement_id = @statementId where charge_id = any(@ids)", new { statementId, ids = recoCharges.Select(c => c.ChargeId).ToArray() }, ct);
            if (totals.NetFee + totals.Tax > 0)
                await ledger.PostAsync(u, partner.PartnerId, currency, Postings.Statement(statementId, totals.NetFee, totals.Tax), "month-close", ct, now);
            await WebhookFanoutConsumer.RequestAsync(u, partner.PartnerId, WebhookTypes.StatementIssued,
                new { statementId, period, totalDue = ApiMapper.Money(totals.TotalDue, currency) }, ct);
            log.LogInformation("Statement {Invoice} for {Partner} {Period}: FP-1 {Premium} + RA-1 {Reco} ({Count} adds) - reversals {Rev} + tax {Tax} = {Total} {Currency}",
                invoice, partner.DisplayName, period, totals.PremiumFee, totals.RecoFee, totals.RecoChargeCount, totals.Reversals, totals.Tax, totals.TotalDue, currency);
            return new StatementCloseResult(partner.PartnerId, period, statementId, invoice, totals.PremiumFee, totals.RecoFee, totals.RecoChargeCount, totals.Reversals,
                totals.Tax, totals.TotalDue, "ISSUED");
        }, ct);
    }

    public async Task<StatementList> ListAsync(Guid partnerId, string? period, string? status, string? cursor, int limit, CancellationToken ct)
    {
        DateTime? start = null;
        if (period is not null)
        {
            if (!Period.TryParse(period, out var s, out _)) throw HsException.Invalid("period must be YYYY-MM");
            start = s.Date;
        }
        var offset = Cursor.DecodeOffset(cursor);
        limit = Math.Clamp(limit, 1, 200);
        var rows = (await db.InTxAsync(DbRole.App, partnerId, u => u.QueryAsync<StatementRow>(
            $"select {StatementColumns} from billing.statement where (@start::date is null or period_start = @start) and (@status::text is null or status = @status) order by period_start desc, statement_id offset @offset limit @take",
            new { start, status, offset, take = limit + 1 }, ct), ct)).ToList();
        return new StatementList(rows.Take(limit).Select(ToApi).ToList(), rows.Count > limit ? Cursor.EncodeOffset(offset + limit) : null);
    }

    public async Task<Statement> GetAsync(Guid partnerId, Guid statementId, CancellationToken ct)
    {
        var row = await db.InTxAsync(DbRole.App, partnerId, u => u.QueryFirstOrDefaultAsync<StatementRow>($"select {StatementColumns} from billing.statement where statement_id = @statementId",
            new { statementId }, ct), ct) ?? throw HsException.NotFound("Statement " + statementId);
        return ToApi(row);
    }

    public async Task<StatementLinePage> LinesAsync(Guid partnerId, Guid statementId, string? cursor, int limit, CancellationToken ct)
    {
        var offset = Cursor.DecodeOffset(cursor);
        limit = Math.Clamp(limit, 1, 500);
        var (currency, rows) = await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            var c = await u.ScalarAsync<string?>("select currency from billing.statement where statement_id = @statementId", new { statementId }, ct)
                    ?? throw HsException.NotFound("Statement " + statementId);
            var r = (await u.QueryAsync<LineRow>(
                "select line_no, line_type, order_id, order_line_id, suggestion_id, delivered_at, fair_premium, amount, attribution_id from billing.statement_line where statement_id = @statementId order by line_no offset @offset limit @take",
                new { statementId, offset, take = limit + 1 }, ct)).ToList();
            return (c.Trim(), r);
        }, ct);
        return new StatementLinePage(rows.Take(limit).Select(l => new StatementLine
        {
            LineNo = l.LineNo, Type = l.LineType, OrderId = l.OrderId, OrderLineId = l.OrderLineId, SuggestionId = l.SuggestionId, DeliveredAt = l.DeliveredAt,
            FairPremium = ApiMapper.Money(l.FairPremium, currency), Amount = ApiMapper.Money(l.Amount, currency),
        }).ToList(), rows.Count > limit ? Cursor.EncodeOffset(offset + limit) : null);
    }

    public async Task<Dispute> OpenDisputeAsync(Guid partnerId, DisputeRequest req, CancellationToken ct)
    {
        if ((req.Lines is null || req.Lines.Count == 0) && (req.RecommendationChargeIds is null || req.RecommendationChargeIds.Count == 0))
            throw HsException.Invalid("Provide lines, recommendationChargeIds or both");
        var partner = await partners.GetAsync(partnerId, ct);
        var disputeId = Ids.NewId();
        var now = clock.UtcNow;
        await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            var st = await u.QueryFirstOrDefaultAsync<StatementRow>($"select {StatementColumns} from billing.statement where statement_id = @id", new { id = req.StatementId }, ct)
                     ?? throw HsException.NotFound("Statement " + req.StatementId);
            if (st.Status is not ("ISSUED" or "DISPUTED")) throw HsException.Unprocessable("HS-DISPUTE-STATE", $"Statements in status {st.Status} cannot be disputed");
            if (st.IssuedAt is { } issued && now > issued.AddDays(partner.Config.DisputeWindowDays))
                throw HsException.Unprocessable("HS-DISPUTE-WINDOW-CLOSED", $"The dispute window of {partner.Config.DisputeWindowDays} days has closed");
            foreach (var l in req.Lines ?? [])
                if (!await u.ScalarAsync<bool>("select exists (select 1 from billing.statement_line where statement_id = @s and order_id = @o and order_line_id = @l and line_type = 'FEE')",
                        new { s = req.StatementId, o = l.OrderId, l = l.OrderLineId }, ct))
                    throw HsException.Unprocessable("HS-DISPUTE-LINE-UNKNOWN", $"Order line {l.OrderId}/{l.OrderLineId} is not billed on this statement");
            var charges = req.RecommendationChargeIds?.Distinct().ToArray() ?? [];
            if (charges.Length > 0)
            {
                var onStatement = await u.ScalarAsync<int>("select count(*)::int from txn.reco_charge where statement_id = @s and charge_id = any(@ids) and state = 'FINAL'",
                    new { s = req.StatementId, ids = charges }, ct);
                if (onStatement != charges.Length) throw HsException.Unprocessable("HS-DISPUTE-CHARGE-UNKNOWN", "Some recommendation charges are not billed (FINAL) on this statement");
            }
            await u.ExecuteAsync(
                """
                insert into billing.dispute (dispute_id, partner_id, statement_id, reason, disputed_lines, disputed_charge_ids, comment, status, opened_at)
                values (@id, @p, @s, @reason, @lines::jsonb, @charges, @comment, 'OPEN', @now)
                """, new { id = disputeId, p = partnerId, s = req.StatementId, reason = req.Reason, lines = JsonSerializer.Serialize(req.Lines ?? [], HsJson.Options), charges, comment = req.Comment, now }, ct);
            await u.SetRoleAsync(DbRole.LedgerWriter, ct);
            await u.ExecuteAsync("update billing.statement set status = 'DISPUTED' where statement_id = @s and status = 'ISSUED'", new { s = req.StatementId }, ct);
        }, ct);
        return await GetDisputeAsync(partnerId, disputeId, ct);
    }

    public async Task<Dispute> GetDisputeAsync(Guid partnerId, Guid disputeId, CancellationToken ct)
    {
        var (row, currency) = await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            var r = await u.QueryFirstOrDefaultAsync<DisputeRow>($"select {DisputeColumns} from billing.dispute where dispute_id = @disputeId", new { disputeId }, ct)
                    ?? throw HsException.NotFound("Dispute " + disputeId);
            var c = await u.ScalarAsync<string>("select currency from billing.statement where statement_id = @s", new { s = r.StatementId }, ct);
            return (r, c!.Trim());
        }, ct);
        return ToApi(row, currency);
    }

    /// <summary>Admin plane: resolve a dispute. Upheld lines and charges are credited with a credit note (revenue contra + output tax).</summary>
    public async Task<Dispute> ResolveDisputeAsync(Guid disputeId, DisputeResolution decision, string actor, CancellationToken ct)
    {
        if (decision.Decision is not ("UPHELD" or "PARTIALLY_UPHELD" or "REJECTED" or "UNDER_REVIEW")) throw HsException.Invalid("decision must be UPHELD, PARTIALLY_UPHELD, REJECTED or UNDER_REVIEW");
        var partnerId = await db.InTxAsync(DbRole.Ops, null, u => u.ScalarAsync<Guid?>("select partner_id from billing.dispute where dispute_id = @disputeId", new { disputeId }, ct), ct)
                        ?? throw HsException.NotFound("Dispute " + disputeId);
        var now = clock.UtcNow;
        await db.InTxAsync(DbRole.LedgerWriter, partnerId, async u =>
        {
            var d = await u.QuerySingleAsync<DisputeRow>($"select {DisputeColumns} from billing.dispute where dispute_id = @disputeId", new { disputeId }, ct);
            if (d.Status is "UPHELD" or "PARTIALLY_UPHELD" or "REJECTED") throw HsException.Conflict("HS-DISPUTE-RESOLVED", "The dispute is already resolved");
            var st = await u.QuerySingleAsync<StatementRow>($"select {StatementColumns} from billing.statement where statement_id = @s", new { s = d.StatementId }, ct);
            var currency = st.Currency.Trim();
            decimal premiumPart = 0, recoPart = 0, tax = 0;
            if (decision.Decision is "UPHELD" or "PARTIALLY_UPHELD")
            {
                var disputedLines = JsonSerializer.Deserialize<List<DisputedLine>>(d.DisputedLines, HsJson.Options) ?? [];
                var lines = decision.Decision == "UPHELD" ? disputedLines : (decision.UpheldLines ?? []).Where(l => disputedLines.Contains(l)).ToList();
                var chargeIds = decision.Decision == "UPHELD" ? d.DisputedChargeIds : (decision.UpheldChargeIds ?? []).Where(d.DisputedChargeIds.Contains).ToArray();
                foreach (var l in lines)
                {
                    var a = await u.QueryFirstOrDefaultAsync<AttrBill>(
                        "select attribution_id, order_id, line_id, suggestion_id, fair_premium, fee_amount, reversed_amount from txn.attribution where order_id = @o and line_id = @l and statement_id = @s and state = 'FINAL'",
                        new { o = l.OrderId, l = l.OrderLineId, s = d.StatementId }, ct);
                    if (a is null) continue;
                    premiumPart += a.FeeAmount - a.ReversedAmount;
                    await u.ExecuteAsync("update txn.attribution set state = 'CREDITED' where attribution_id = @id", new { id = a.AttributionId }, ct);
                    var no = await u.ScalarAsync<int>("select coalesce(max(line_no), 0) + 1 from billing.statement_line where statement_id = @s", new { s = d.StatementId }, ct);
                    await u.ExecuteAsync(
                        """
                        insert into billing.statement_line (statement_id, line_no, partner_id, attribution_id, line_type, order_id, order_line_id, suggestion_id, fair_premium, amount)
                        values (@s, @no, @p, @aid, 'CREDIT', @o, @l, @sid, @premium, @amount)
                        """, new { s = d.StatementId, no, p = partnerId, aid = a.AttributionId, o = a.OrderId, l = a.LineId, sid = a.SuggestionId, premium = a.FairPremium, amount = -(a.FeeAmount - a.ReversedAmount) }, ct);
                }
                foreach (var c in chargeIds)
                {
                    var charge = await u.QueryFirstOrDefaultAsync<ChargeBill>("select charge_id, amount, credited_amount from txn.reco_charge where charge_id = @c and statement_id = @s and state = 'FINAL'",
                        new { c, s = d.StatementId }, ct);
                    if (charge is null) continue;
                    recoPart += charge.Amount - charge.CreditedAmount;
                    await u.ExecuteAsync("update txn.reco_charge set state = 'CREDITED', credited_amount = amount where charge_id = @c", new { c }, ct);
                }
                tax = Currency.Round((premiumPart + recoPart) * st.TaxRate, currency);
                if (premiumPart + recoPart > 0)
                    await ledger.PostAsync(u, partnerId, currency, Postings.CreditNote(disputeId, premiumPart, recoPart, tax), actor, ct, now);
                var credits = st.Credits + premiumPart + recoPart;
                var taxAmount = st.TaxAmount - tax;
                var net = st.GrossFee - st.Reversals - credits;
                var total = net + taxAmount;
                await u.ExecuteAsync(
                    "update billing.statement set credits = @credits, net_fee = @net, tax_amount = @taxAmount, total_due = @total, line_count = (select count(*) from billing.statement_line where statement_id = @s), status = case when paid_amount >= @total then 'SETTLED' else 'ISSUED' end where statement_id = @s",
                    new { credits, net, taxAmount, total, s = d.StatementId }, ct);
            }
            else if (decision.Decision == "REJECTED")
                await u.ExecuteAsync("update billing.statement set status = case when paid_amount >= total_due then 'SETTLED' else 'ISSUED' end where statement_id = @s and status = 'DISPUTED'", new { s = d.StatementId }, ct);
            var final = decision.Decision != "UNDER_REVIEW";
            await u.ExecuteAsync(
                "update billing.dispute set status = @status, credit_note_amount = @amount, credit_note_tax = @tax, resolution = @resolution, resolved_at = @at where dispute_id = @disputeId",
                new { status = decision.Decision, amount = premiumPart + recoPart > 0 ? premiumPart + recoPart + tax : (decimal?)null, tax = premiumPart + recoPart > 0 ? tax : (decimal?)null,
                      resolution = decision.Resolution, at = final ? now : (DateTime?)null, disputeId }, ct);
            await u.ExecuteAsync("insert into audit.change_log (actor, action, entity_type, entity_id, details) values (@actor, 'DISPUTE_' || @status, 'DISPUTE', @id, @details::jsonb)",
                new { actor, status = decision.Decision, id = disputeId.ToString(), details = JsonSerializer.Serialize(new { premiumPart, recoPart, tax }) }, ct);
            await WebhookFanoutConsumer.RequestAsync(u, partnerId, WebhookTypes.DisputeUpdated, new { disputeId, status = decision.Decision }, ct);
        }, ct);
        return await GetDisputeAsync(partnerId, disputeId, ct);
    }

    public async Task<Statement> RecordPaymentAsync(Guid statementId, decimal amount, string paymentRef, string actor, CancellationToken ct)
    {
        if (amount <= 0) throw HsException.Invalid("amount must be positive");
        var partnerId = await db.InTxAsync(DbRole.Ops, null, u => u.ScalarAsync<Guid?>("select partner_id from billing.statement where statement_id = @statementId", new { statementId }, ct), ct)
                        ?? throw HsException.NotFound("Statement " + statementId);
        await db.InTxAsync(DbRole.LedgerWriter, partnerId, async u =>
        {
            var st = await u.QuerySingleAsync<StatementRow>($"select {StatementColumns} from billing.statement where statement_id = @statementId", new { statementId }, ct);
            var posted = await ledger.PostAsync(u, partnerId, st.Currency.Trim(), Postings.Payment(paymentRef, statementId, amount), actor, ct);
            if (!posted) return;
            await u.ExecuteAsync("update billing.statement set paid_amount = paid_amount + @amount, status = case when paid_amount + @amount >= total_due and status <> 'DISPUTED' then 'SETTLED' else status end where statement_id = @statementId",
                new { amount, statementId }, ct);
        }, ct);
        return await GetAsync(partnerId, statementId, ct);
    }

    /// <summary>Trial balance across all partners (hs_ops): every account must net to zero across the ledger.</summary>
    public async Task<List<TrialBalanceRow>> TrialBalanceAsync(Guid? partnerId, CancellationToken ct) =>
        (await db.InTxAsync(DbRole.Ops, null, u => u.QueryAsync<TrialBalanceRow>(
            """
            select a.code as account, a.currency, sum(l.debit) as debit, sum(l.credit) as credit, sum(l.debit) - sum(l.credit) as balance
            from billing.journal_line l join billing.account a on a.account_id = l.account_id
            where (@partnerId::uuid is null or l.partner_id = @partnerId)
            group by a.code, a.currency order by a.currency, a.code
            """, new { partnerId }, ct), ct)).ToList();

    private static Statement ToApi(StatementRow r)
    {
        var c = r.Currency.Trim();
        return new Statement
        {
            StatementId = r.StatementId, Period = r.PeriodStart.ToString("yyyy-MM", CultureInfo.InvariantCulture), Currency = c, Status = r.Status, LineCount = r.LineCount,
            InvoiceNumber = r.InvoiceNumber, IssuedAt = r.IssuedAt, DueAt = r.DueAt,
            Totals = new StatementTotals
            {
                PremiumFee = ApiMapper.Money(r.PremiumFee, c), RecommendationFee = ApiMapper.Money(r.RecoFee, c), RecommendationChargeCount = r.RecoChargeCount,
                GrossFee = ApiMapper.Money(r.GrossFee, c), Reversals = ApiMapper.Money(r.Reversals, c), Credits = ApiMapper.Money(r.Credits, c),
                NetFee = ApiMapper.Money(r.NetFee, c), Tax = ApiMapper.Money(r.TaxAmount, c), TotalDue = ApiMapper.Money(r.TotalDue, c),
            },
        };
    }

    private static Dispute ToApi(DisputeRow r, string currency) => new()
    {
        DisputeId = r.DisputeId, StatementId = r.StatementId, Status = r.Status, Reason = r.Reason,
        Lines = JsonSerializer.Deserialize<List<DisputedLine>>(r.DisputedLines, HsJson.Options) ?? [],
        RecommendationChargeIds = r.DisputedChargeIds.Length > 0 ? r.DisputedChargeIds.ToList() : null, Comment = r.Comment, Resolution = r.Resolution,
        CreditNoteAmount = r.CreditNoteAmount is { } a ? ApiMapper.Money(a, currency) : null, OpenedAt = r.OpenedAt, ResolvedAt = r.ResolvedAt,
    };
}
