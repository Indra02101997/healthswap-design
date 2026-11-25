using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Domain.Commerce;
using HealthSwap.Platform.Bus;
using HealthSwap.Platform.Cache;
using HealthSwap.Platform.Data;
using Microsoft.Extensions.Logging;
using Money = HealthSwap.Contracts.Money;

namespace HealthSwap.Platform.Services;

/// <summary>Orders, lifecycle updates and adjustments (HLD 9.4-9.5). Attribution and fees are delegated to <see cref="AttributionEngine"/>.</summary>
public sealed class OrderService(Db db, HotCache cache, PartnerDirectory partners, SuggestionStore suggestions, AttributionEngine attribution, IClock clock,
    ILogger<OrderService> log)
{
    public sealed class HeaderRow
    {
        public string OrderId { get; set; } = "";
        public DateTime PlacedAt { get; set; }
        public string? CartRef { get; set; }
        public string Market { get; set; } = "";
        public string Currency { get; set; } = "";
        public string Status { get; set; } = "";
        public string? FulfillmentType { get; set; }
        public short ReturnWindowDays { get; set; }
        public DateTime? DeliveredAt { get; set; }
    }

    public sealed class LineRow
    {
        public string LineId { get; set; } = "";
        public string PartnerSku { get; set; } = "";
        public Guid? ProductId { get; set; }
        public decimal Quantity { get; set; }
        public decimal UnitPrice { get; set; }
        public decimal LineDiscount { get; set; }
        public decimal AllocatedOrderDiscount { get; set; }
        public decimal? TaxAmount { get; set; }
        public decimal? TaxRate { get; set; }
        public decimal NetAmount { get; set; }
        public Guid? SuggestionId { get; set; }
        public decimal? DeliveredQty { get; set; }
        public decimal? CancelledQty { get; set; }
    }

    public async Task<Order> CreateAsync(PartnerContext ctx, OrderCreateRequest req, CancellationToken ct)
    {
        var partner = await partners.GetAsync(ctx.PartnerId, ct);
        var cfg = partner.Config;
        if (!partner.Markets.Contains(req.Market)) throw HsException.Unprocessable("HS-UNSUPPORTED-MARKET", $"Market {req.Market} is not enabled for this partner");
        if (req.Currency != cfg.Currency) throw HsException.Unprocessable("HS-CURRENCY-MISMATCH", $"Order currency must be {cfg.Currency}");
        var errors = new List<FieldError>();
        var lines = new List<(OrderLine Line, decimal Price, decimal LineDiscount, decimal OrderDiscount, decimal? Tax, decimal Net)>();
        for (var i = 0; i < req.Lines.Count; i++)
        {
            var l = req.Lines[i];
            if (req.Lines.Take(i).Any(x => x.LineId == l.LineId)) errors.Add(new FieldError($"/lines/{i}/lineId", "Duplicate lineId"));
            decimal Amount(Money? m, string field)
            {
                if (m is null) return 0;
                if (m.Currency != req.Currency) errors.Add(new FieldError($"/lines/{i}/{field}/currency", $"Must be {req.Currency}"));
                return Currency.TryParse(m.Amount, out var v) ? v : 0;
            }
            lines.Add((l, Amount(l.UnitPrice, "unitPrice"), Amount(l.LineDiscount, "lineDiscount"), Amount(l.AllocatedOrderDiscount, "allocatedOrderDiscount"),
                l.TaxAmount is null ? null : Amount(l.TaxAmount, "taxAmount"), Amount(l.NetAmount, "netAmount")));
        }
        if (errors.Count > 0) throw HsException.Invalid("Order validation failed", errors);

        var maps = await cache.GetSkuMapsAsync(ctx.PartnerId, req.Lines.Select(l => l.PartnerSku).ToList());
        var snapshots = new Dictionary<Guid, SuggestionSnapshot?>();
        foreach (var sid in req.Lines.Where(l => l.SuggestionId is not null).Select(l => l.SuggestionId!.Value).Distinct())
            snapshots[sid] = await suggestions.GetAsync(ctx.PartnerId, sid, ct);

        var now = clock.UtcNow;
        await db.InTxAsync(DbRole.App, ctx.PartnerId, async u =>
        {
            var inserted = await u.ExecuteAsync("insert into txn.order_key (partner_id, order_id, placed_at) values (@p, @o, @at) on conflict do nothing",
                new { p = ctx.PartnerId, o = req.OrderId, at = req.PlacedAt }, ct);
            if (inserted == 0) throw HsException.Conflict("HS-ORDER-EXISTS", $"Order {req.OrderId} was already reported");
            var orderDiscount = req.OrderDiscount is { } od && Currency.TryParse(od.Amount, out var odv) ? odv : 0m;
            await u.ExecuteAsync(
                """
                insert into txn.order_header (partner_id, order_id, placed_at, cart_ref, user_ref, store_id, market, currency, status, fulfillment_type, experiment_arm,
                    order_discount, return_window_days, updated_at)
                values (@p, @o, @at, @cartRef, @userRef, @storeId, @market, @currency, 'PLACED', @ft, @arm, @discount, @window, @now)
                """,
                new { p = ctx.PartnerId, o = req.OrderId, at = req.PlacedAt, cartRef = req.CartRef, userRef = req.UserRef, storeId = req.StoreId, market = req.Market,
                      currency = req.Currency, ft = req.FulfillmentType, arm = req.ExperimentArm, discount = orderDiscount, window = (short)cfg.ReturnWindowFor(req.FulfillmentType), now }, ct);
            for (var i = 0; i < lines.Count; i++)
            {
                var (l, price, ld, odisc, tax, net) = lines[i];
                await u.ExecuteAsync(
                    """
                    insert into txn.order_line (partner_id, order_id, placed_at, line_id, partner_sku, product_id, quantity, unit_price, line_discount, allocated_order_discount,
                        tax_amount, tax_rate, net_amount, suggestion_id)
                    values (@p, @o, @at, @lineId, @sku, @product, @qty, @price, @ld, @od, @tax, @taxRate, @net, @sid)
                    """,
                    new { p = ctx.PartnerId, o = req.OrderId, at = req.PlacedAt, lineId = l.LineId, sku = l.PartnerSku, product = maps[i]?.ProductId, qty = l.Quantity,
                          price, ld, od = odisc, tax, taxRate = l.TaxRate, net, sid = l.SuggestionId }, ct);
            }
            await attribution.AttributeOrderAsync(u, ctx.PartnerId, cfg, req, lines.Select(x => (x.Line, x.Net)).ToList(), snapshots, ct);
            await Outbox.EnqueueAsync(u, ctx.PartnerId, Topics.Orders, req.OrderId, new
            {
                orderId = req.OrderId, req.Market, req.PlacedAt, req.ExperimentArm, req.CartRef, lines = req.Lines.Count,
                attributedLines = req.Lines.Count(l => l.SuggestionId is not null),
                gross = lines.Sum(x => x.Net),
            }, ct);
        }, ct);
        log.LogInformation("Order {Order} recorded for partner {Partner}", req.OrderId, ctx.PartnerId);
        return await GetAsync(ctx.PartnerId, req.OrderId, ct);
    }

    public async Task<Order> GetAsync(Guid partnerId, string orderId, CancellationToken ct) =>
        await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            var (h, lines) = await LoadAsync(u, orderId, ct);
            var summary = await AttributionEngine.SummaryAsync(u, orderId, h.Currency.Trim(), ct);
            return ToApi(h, lines, summary);
        }, ct);

    public async Task<AttributionSummary> AttributionAsync(Guid partnerId, string orderId, CancellationToken ct) =>
        await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            var (h, _) = await LoadAsync(u, orderId, ct);
            return await AttributionEngine.SummaryAsync(u, orderId, h.Currency.Trim(), ct);
        }, ct);

    public async Task<Order> UpdateStatusAsync(Guid partnerId, string orderId, OrderStatusUpdate update, CancellationToken ct)
    {
        await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            var (h, lines) = await LoadAsync(u, orderId, ct);
            var allowed = h.Status switch
            {
                "PLACED" => new[] { "CONFIRMED", "PARTIALLY_FULFILLED", "DELIVERED", "CANCELLED" },
                "CONFIRMED" => ["PARTIALLY_FULFILLED", "DELIVERED", "CANCELLED"],
                "PARTIALLY_FULFILLED" => ["PARTIALLY_FULFILLED", "DELIVERED", "CANCELLED"],
                _ => [],
            };
            if (!allowed.Contains(update.Status)) throw HsException.Conflict("HS-ORDER-STATE", $"Cannot change order status from {h.Status} to {update.Status}");
            foreach (var ls in update.LineStatuses ?? [])
            {
                var line = lines.FirstOrDefault(l => l.LineId == ls.LineId) ?? throw HsException.Invalid($"Unknown lineId {ls.LineId}");
                if ((ls.DeliveredQuantity ?? 0) + (ls.CancelledQuantity ?? 0) > line.Quantity) throw HsException.Invalid($"Line {ls.LineId}: delivered + cancelled exceeds the ordered quantity");
                await u.ExecuteAsync("update txn.order_line set delivered_qty = coalesce(@d, delivered_qty), cancelled_qty = coalesce(@c, cancelled_qty) where order_id = @o and placed_at = @at and line_id = @l",
                    new { d = ls.DeliveredQuantity, c = ls.CancelledQuantity, o = orderId, at = h.PlacedAt, l = ls.LineId }, ct);
            }
            var delivered = update.Status is "DELIVERED" or "PARTIALLY_FULFILLED";
            await u.ExecuteAsync("update txn.order_header set status = @s, delivered_at = case when @delivered then coalesce(delivered_at, @at) else delivered_at end, updated_at = now() where order_id = @o and placed_at = @placed",
                new { s = update.Status, delivered, at = update.OccurredAt, o = orderId, placed = h.PlacedAt }, ct);
            var (_, updatedLines) = await LoadAsync(u, orderId, ct);
            if (delivered) await attribution.AccrueAsync(u, partnerId, orderId, update.OccurredAt.AddDays(h.ReturnWindowDays), updatedLines, update.Status == "DELIVERED", ct);
            if (update.Status == "CANCELLED") await attribution.CancelAsync(u, partnerId, orderId, "ORDER_CANCELLED", null, ct);
            await Outbox.EnqueueAsync(u, partnerId, Topics.Orders, orderId, new { orderId, status = update.Status, update.OccurredAt }, ct);
        }, ct);
        return await GetAsync(partnerId, orderId, ct);
    }

    public async Task<AttributionSummary> AdjustAsync(Guid partnerId, string orderId, OrderAdjustmentRequest req, CancellationToken ct)
    {
        var partner = await partners.GetAsync(partnerId, ct);
        return await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            var (h, lines) = await LoadAsync(u, orderId, ct);
            foreach (var a in req.Adjustments)
            {
                var line = lines.FirstOrDefault(l => l.LineId == a.LineId) ?? throw HsException.Invalid($"Unknown lineId {a.LineId}");
                if (a.Amount is { } m && m.Currency != h.Currency.Trim()) throw HsException.Invalid($"Adjustment {a.AdjustmentId}: currency must be {h.Currency.Trim()}");
                if (a.Quantity is { } q && q > line.Quantity) throw HsException.Invalid($"Adjustment {a.AdjustmentId}: quantity exceeds the line quantity");
                var amount = a.Amount is { } am && Currency.TryParse(am.Amount, out var av) ? av : (decimal?)null;
                var inserted = await u.ExecuteAsync(
                    """
                    insert into txn.order_adjustment (partner_id, adjustment_id, order_id, line_id, adjustment_type, quantity, amount, substituted_partner_sku, occurred_at)
                    values (@p, @id, @o, @l, @type, @qty, @amount, @sub, @at) on conflict (partner_id, adjustment_id) do nothing
                    """, new { p = partnerId, id = a.AdjustmentId, o = orderId, l = a.LineId, type = a.Type, qty = a.Quantity, amount, sub = a.SubstitutedPartnerSku, at = a.OccurredAt }, ct);
                if (inserted == 0) continue; // replayed adjustment
                await attribution.AdjustAsync(u, partnerId, partner.Config, orderId, line, a, amount, ct);
            }
            await Outbox.EnqueueAsync(u, partnerId, Topics.Orders, orderId, new { orderId, adjustments = req.Adjustments.Count }, ct);
            return await AttributionEngine.SummaryAsync(u, orderId, h.Currency.Trim(), ct);
        }, ct);
    }

    internal static async Task<(HeaderRow Header, List<LineRow> Lines)> LoadAsync(UnitOfWork u, string orderId, CancellationToken ct)
    {
        var placedAt = await u.ScalarAsync<DateTime?>("select placed_at from txn.order_key where order_id = @orderId", new { orderId }, ct)
                       ?? throw HsException.NotFound("Order " + orderId);
        var h = await u.QuerySingleAsync<HeaderRow>(
            "select order_id, placed_at, cart_ref, market, currency, status, fulfillment_type, return_window_days, delivered_at from txn.order_header where order_id = @orderId and placed_at = @placedAt",
            new { orderId, placedAt }, ct);
        var lines = (await u.QueryAsync<LineRow>(
            """
            select line_id, partner_sku, product_id, quantity, unit_price, line_discount, allocated_order_discount, tax_amount, tax_rate, net_amount, suggestion_id,
                   delivered_qty, cancelled_qty
            from txn.order_line where order_id = @orderId and placed_at = @placedAt order by line_id
            """, new { orderId, placedAt }, ct)).ToList();
        return (h, lines);
    }

    private static Order ToApi(HeaderRow h, List<LineRow> lines, AttributionSummary summary)
    {
        var c = h.Currency.Trim();
        return new Order
        {
            OrderId = h.OrderId, Status = h.Status, Market = h.Market, Currency = c, PlacedAt = h.PlacedAt, DeliveredAt = h.DeliveredAt,
            Lines = lines.Select(l => new OrderLine
            {
                LineId = l.LineId, PartnerSku = l.PartnerSku, Quantity = l.Quantity, UnitPrice = ApiMapper.Money(l.UnitPrice, c),
                LineDiscount = l.LineDiscount != 0 ? ApiMapper.Money(l.LineDiscount, c) : null,
                AllocatedOrderDiscount = l.AllocatedOrderDiscount != 0 ? ApiMapper.Money(l.AllocatedOrderDiscount, c) : null,
                TaxAmount = l.TaxAmount is { } t ? ApiMapper.Money(t, c) : null, TaxRate = l.TaxRate, NetAmount = ApiMapper.Money(l.NetAmount, c), SuggestionId = l.SuggestionId,
            }).ToList(),
            Attribution = summary,
        };
    }
}

/// <summary>
/// FP-1 attribution (HLD 9.2-9.5): evidence chain (served, impression, accept, product match, window), fair premium with caps,
/// PENDING at order, ACCRUED at delivery (FEE_ACCRUAL), proportional reversals for returns, FINAL after the return window.
/// </summary>
public sealed class AttributionEngine(Db db, LedgerService ledger, IClock clock, ILogger<AttributionEngine> log)
{
    private sealed class AttrRow
    {
        public Guid AttributionId { get; set; }
        public string OrderId { get; set; } = "";
        public string LineId { get; set; } = "";
        public Guid SuggestionId { get; set; }
        public string State { get; set; } = "";
        public string? RejectionReason { get; set; }
        public string Currency { get; set; } = "";
        public decimal ReplacedQty { get; set; }
        public decimal OrigUnitPriceSnapshot { get; set; }
        public decimal AltNetUnitPrice { get; set; }
        public decimal PackDelta { get; set; }
        public decimal? SizeNormalizedDelta { get; set; }
        public decimal FairPremium { get; set; }
        public decimal FeeRate { get; set; }
        public decimal FeeAmount { get; set; }
        public decimal ReversedAmount { get; set; }
        public string RuleVersion { get; set; } = "";
        public DateTime? FinalizesAt { get; set; }
        public string? OriginalPartnerSku { get; set; }
        public string? AlternativePartnerSku { get; set; }
    }

    private sealed class EventRow { public Guid SuggestionId { get; set; } public string EventType { get; set; } = ""; public string? AlternativePartnerSku { get; set; } public DateTime OccurredAt { get; set; } }

    private const string Columns =
        """
        attribution_id, order_id, line_id, suggestion_id, state, rejection_reason, currency, replaced_qty, orig_unit_price_snapshot, alt_net_unit_price, pack_delta,
        size_normalized_delta, fair_premium, fee_rate, fee_amount, reversed_amount, rule_version, finalizes_at, original_partner_sku, alternative_partner_sku
        """;

    public async Task AttributeOrderAsync(UnitOfWork u, Guid partnerId, PartnerConfig cfg, OrderCreateRequest order, IReadOnlyList<(OrderLine Line, decimal Net)> lines,
        IReadOnlyDictionary<Guid, SuggestionSnapshot?> snapshots, CancellationToken ct)
    {
        var suggestionIds = lines.Where(l => l.Line.SuggestionId is not null).Select(l => l.Line.SuggestionId!.Value).Distinct().ToArray();
        if (suggestionIds.Length == 0) return;
        var events = (await u.QueryAsync<EventRow>(
            "select suggestion_id, event_type, alternative_partner_sku, occurred_at from txn.suggestion_event where suggestion_id = any(@ids) and event_type in ('IMPRESSION', 'ACCEPT', 'UNDO')",
            new { ids = suggestionIds }, ct)).ToList();
        var monthStart = new DateTime(order.PlacedAt.Year, order.PlacedAt.Month, 1, 0, 0, 0, DateTimeKind.Utc);
        var monthFee = await u.ScalarAsync<decimal>(
            "select coalesce(sum(fee_amount - reversed_amount), 0) from txn.attribution where state in ('PENDING', 'ACCRUED', 'FINAL') and computed_at >= @from and computed_at < @to",
            new { from = monthStart, to = monthStart.AddMonths(1) }, ct);
        var orderFee = 0m;
        var claimed = new HashSet<Guid>();
        foreach (var (line, net) in lines.Where(l => l.Line.SuggestionId is not null))
        {
            var sid = line.SuggestionId!.Value;
            var s = snapshots.GetValueOrDefault(sid);
            string state = "PENDING";
            string? reason = null;
            AlternativeSnapshot? alt = null;
            var evs = events.Where(e => e.SuggestionId == sid).ToList();
            var accept = evs.Where(e => e.EventType == "ACCEPT" && (e.AlternativePartnerSku is null || e.AlternativePartnerSku == line.PartnerSku)).OrderBy(e => e.OccurredAt).LastOrDefault();
            if (s is null) (state, reason) = ("REJECTED", "NOT_SERVED_BY_HEALTHSWAP");
            else if ((alt = s.Alternatives.FirstOrDefault(a => a.PartnerSku == line.PartnerSku)) is null) (state, reason) = ("REJECTED", "PRODUCT_MISMATCH");
            else if (accept is null) (state, reason) = ("REJECTED", "NO_ACCEPT");
            else if (!evs.Any(e => e.EventType == "IMPRESSION" && e.OccurredAt <= accept.OccurredAt.AddSeconds(5))) (state, reason) = ("REJECTED", "NO_IMPRESSION");
            else if (order.PlacedAt > s.AttributionExpiresAt || order.PlacedAt < s.CreatedAt.AddMinutes(-5)) (state, reason) = ("REJECTED", "EXPIRED");
            else if (s.FromUnitPrice is null) (state, reason) = ("REJECTED", "NOT_A_SWAP");
            else if (!claimed.Add(sid)) (state, reason) = ("REJECTED", "DUPLICATE");

            FairPremiumResult? fp = null;
            decimal fee = 0, uncapped = 0;
            if (state == "PENDING" && s is not null && alt is not null)
            {
                var originalInOrder = lines.Where(l => l.Line.PartnerSku == s.FromPartnerSku).Sum(l => l.Line.Quantity);
                fp = FairPremiumRule.Compute(new FairPremiumInput
                {
                    OriginalUnitPrice = s.FromUnitPrice!.Value, OriginalQuantity = s.FromQty, OriginalSize = NetQuantity.Normalize(s.FromNetQuantity, s.FromNetUnit),
                    OriginalTaxRate = s.FromTaxRate, AlternativeQuantity = line.Quantity, AlternativeNetAmount = net, AlternativeSize = NetQuantity.Normalize(alt.NetQuantity, alt.NetUnit),
                    AlternativeTaxRate = line.TaxRate, OriginalQuantityInOrder = originalInOrder, Currency = order.Currency, ConvertToExTax = cfg.Fee.FeeBasis == "TAX_EXCLUSIVE",
                }, cfg.Fee.Rate);
                uncapped = fp.Fee;
                fee = FairPremiumRule.ApplyCaps(fp.Fee, cfg.Fee.Caps, orderFee, monthFee);
                orderFee += fee;
                monthFee += fee;
            }
            await u.ExecuteAsync(
                $"""
                insert into txn.attribution (attribution_id, partner_id, order_id, line_id, suggestion_id, state, rejection_reason, currency, replaced_qty,
                    orig_unit_price_snapshot, alt_net_unit_price, pack_delta, size_normalized_delta, fair_premium, fee_rate, fee_amount, rule_version,
                    original_partner_sku, alternative_partner_sku, uncapped_fee_amount, computed_at)
                values (@id, @p, @o, @l, @sid, @state, @reason, @currency, @qrep, @porig, @palt, @dpack, @dsize, @premium, @rate, @fee, @rule, @origSku, @altSku, @uncapped, @now)
                """,
                new
                {
                    id = Ids.NewId(), p = partnerId, o = order.OrderId, l = line.LineId, sid, state, reason, currency = order.Currency, qrep = fp?.ReplacedQuantity ?? 0,
                    porig = fp?.OriginalUnitPrice ?? s?.FromUnitPrice ?? 0, palt = fp?.AlternativeNetUnitPrice ?? (line.Quantity > 0 ? Math.Round(net / line.Quantity, 4) : 0),
                    dpack = fp?.PackDelta ?? 0, dsize = fp?.SizeNormalizedDelta, premium = fp?.FairPremium ?? 0, rate = cfg.Fee.Rate, fee, rule = FairPremiumRule.Version,
                    origSku = s?.FromPartnerSku, altSku = line.PartnerSku, uncapped, now = clock.UtcNow,
                }, ct);
        }
        await Outbox.EnqueueAsync(u, partnerId, Topics.Attribution, order.OrderId, new { orderId = order.OrderId, stage = "PENDING", orderFee }, ct);
    }

    /// <summary>Delivery: PENDING -> ACCRUED with the fee on the delivered share; posts FEE_ACCRUAL (hs_ledger_writer).</summary>
    public async Task AccrueAsync(UnitOfWork u, Guid partnerId, string orderId, DateTime finalizesAt, IReadOnlyList<OrderService.LineRow> lines, bool complete, CancellationToken ct)
    {
        var pending = (await u.QueryAsync<AttrRow>($"select {Columns} from txn.attribution where order_id = @orderId and state = 'PENDING'", new { orderId }, ct)).ToList();
        if (pending.Count == 0) return;
        await u.SetRoleAsync(DbRole.LedgerWriter, ct);
        foreach (var a in pending)
        {
            var line = lines.First(l => l.LineId == a.LineId);
            if (!complete && line.DeliveredQty is null) continue; // partial fulfillment: only lines with a reported delivered quantity
            var delivered = line.DeliveredQty ?? line.Quantity - (line.CancelledQty ?? 0);
            if (delivered <= 0)
            {
                await u.ExecuteAsync("update txn.attribution set state = 'VOID', rejection_reason = 'NOT_DELIVERED', computed_at = now() where attribution_id = @id", new { id = a.AttributionId }, ct);
                continue;
            }
            var feeBearing = Math.Min(a.ReplacedQty, delivered);
            var fee = a.ReplacedQty > 0 && feeBearing < a.ReplacedQty ? Currency.Round(a.FeeAmount * feeBearing / a.ReplacedQty, a.Currency.Trim()) : a.FeeAmount;
            await u.ExecuteAsync(
                "update txn.attribution set state = 'ACCRUED', fee_amount = @fee, replaced_qty = @q, accrued_at = @now, finalizes_at = @fin where attribution_id = @id and state = 'PENDING'",
                new { fee, q = feeBearing, now = clock.UtcNow, fin = finalizesAt, id = a.AttributionId }, ct);
            if (fee > 0) await ledger.PostAsync(u, partnerId, a.Currency.Trim(), Postings.FeeAccrual(a.AttributionId, fee), "attribution-engine", ct);
        }
        await u.SetRoleAsync(DbRole.App, ct);
        await Outbox.EnqueueAsync(u, partnerId, Topics.Attribution, orderId, new { orderId, stage = "ACCRUED", finalizesAt }, ct);
    }

    /// <summary>Order or line cancellation: PENDING -> VOID; ACCRUED -> full reversal (FEE_REVERSAL).</summary>
    public async Task CancelAsync(UnitOfWork u, Guid partnerId, string orderId, string reason, string? lineId, CancellationToken ct)
    {
        var rows = (await u.QueryAsync<AttrRow>($"select {Columns} from txn.attribution where order_id = @orderId and (@lineId::text is null or line_id = @lineId) and state in ('PENDING', 'ACCRUED')",
            new { orderId, lineId }, ct)).ToList();
        if (rows.Count == 0) return;
        await u.SetRoleAsync(DbRole.LedgerWriter, ct);
        foreach (var a in rows)
        {
            if (a.State == "PENDING")
                await u.ExecuteAsync("update txn.attribution set state = 'VOID', rejection_reason = @reason where attribution_id = @id", new { reason, id = a.AttributionId }, ct);
            else await ReverseAsync(u, partnerId, a, a.FeeAmount - a.ReversedAmount, reason + ":" + (lineId ?? "order"), ct);
        }
        await u.SetRoleAsync(DbRole.App, ct);
    }

    public async Task AdjustAsync(UnitOfWork u, Guid partnerId, PartnerConfig cfg, string orderId, OrderService.LineRow line, OrderAdjustment adj, decimal? amount, CancellationToken ct)
    {
        var a = await u.QueryFirstOrDefaultAsync<AttrRow>($"select {Columns} from txn.attribution where order_id = @orderId and line_id = @lineId", new { orderId, lineId = line.LineId }, ct);
        if (a is null || a.State is not ("PENDING" or "ACCRUED")) return; // FINAL fees change only through disputes
        switch (adj.Type)
        {
            case "CANCELLATION" or "PICKER_SUBSTITUTION":
                await CancelAsync(u, partnerId, orderId, adj.Type, line.LineId, ct);
                return;
            case "RETURN" or "REFUND":
            {
                var qty = adj.Quantity ?? (amount is { } am && line.NetAmount > 0 ? Math.Round(line.Quantity * am / line.NetAmount, 3) : line.Quantity);
                if (a.State == "PENDING")
                {
                    var remaining = Math.Max(0, a.ReplacedQty - qty);
                    var fee = a.ReplacedQty > 0 ? Currency.Round(a.FeeAmount * remaining / a.ReplacedQty, a.Currency.Trim()) : 0;
                    await u.ExecuteAsync("update txn.attribution set fee_amount = @fee, replaced_qty = @q where attribution_id = @id", new { fee, q = remaining, id = a.AttributionId }, ct);
                    return;
                }
                var reversal = FairPremiumRule.Reversal(a.FeeAmount, a.ReversedAmount, a.ReplacedQty, qty, a.Currency.Trim());
                await u.SetRoleAsync(DbRole.LedgerWriter, ct);
                await ReverseAsync(u, partnerId, a, reversal, adj.AdjustmentId, ct);
                await u.SetRoleAsync(DbRole.App, ct);
                return;
            }
            case "PRICE_ADJUSTMENT" when amount is { } credit:
            {
                // The shopper paid less: recompute the premium on the reduced net amount and reverse the difference.
                var netUnit = a.ReplacedQty > 0 ? a.AltNetUnitPrice - credit / Math.Max(1, line.Quantity) : a.AltNetUnitPrice;
                var premiumPerUnit = Math.Max(0, Math.Min(netUnit - a.OrigUnitPriceSnapshot, a.SizeNormalizedDelta is null ? decimal.MaxValue : a.SizeNormalizedDelta.Value - (a.AltNetUnitPrice - netUnit)));
                var newFee = Math.Min(a.FeeAmount, Currency.Round(cfg.Fee.Rate * premiumPerUnit * a.ReplacedQty, a.Currency.Trim()));
                if (a.State == "PENDING")
                {
                    await u.ExecuteAsync("update txn.attribution set fee_amount = @fee, alt_net_unit_price = @unit where attribution_id = @id", new { fee = newFee, unit = Math.Round(netUnit, 4), id = a.AttributionId }, ct);
                    return;
                }
                await u.SetRoleAsync(DbRole.LedgerWriter, ct);
                await ReverseAsync(u, partnerId, a, Math.Max(0, a.FeeAmount - a.ReversedAmount - newFee), adj.AdjustmentId, ct);
                await u.SetRoleAsync(DbRole.App, ct);
                return;
            }
        }
    }

    private async Task ReverseAsync(UnitOfWork u, Guid partnerId, AttrRow a, decimal amount, string adjustmentKey, CancellationToken ct)
    {
        amount = Math.Min(amount, a.FeeAmount - a.ReversedAmount);
        if (amount <= 0) return;
        var reversed = a.ReversedAmount + amount;
        await u.ExecuteAsync("update txn.attribution set reversed_amount = @reversed, state = case when @reversed >= fee_amount then 'REVERSED' else state end where attribution_id = @id",
            new { reversed, id = a.AttributionId }, ct);
        await ledger.PostAsync(u, partnerId, a.Currency.Trim(), Postings.FeeReversal(a.AttributionId, adjustmentKey, amount), "attribution-engine", ct);
        a.ReversedAmount = reversed;
    }

    /// <summary>Return window elapsed: ACCRUED -> FINAL, posts FEE_FINALIZE for the net fee and drops the userRef.</summary>
    public async Task<int> FinalizeDueAsync(CancellationToken ct)
    {
        var now = clock.UtcNow;
        var partnerIds = (await db.InTxAsync(DbRole.Ops, null, u => u.QueryAsync<Guid>(
            "select distinct partner_id from txn.attribution where state = 'ACCRUED' and finalizes_at <= @now", new { now }, ct), ct)).ToList();
        var total = 0;
        foreach (var partnerId in partnerIds)
        {
            total += await db.InTxAsync(DbRole.LedgerWriter, partnerId, async u =>
            {
                var due = (await u.QueryAsync<AttrRow>($"select {Columns} from txn.attribution where state = 'ACCRUED' and finalizes_at <= @now for update skip locked", new { now }, ct)).ToList();
                foreach (var a in due)
                {
                    var net = a.FeeAmount - a.ReversedAmount;
                    await u.ExecuteAsync("update txn.attribution set state = 'FINAL', finalized_at = @now where attribution_id = @id and state = 'ACCRUED'", new { now, id = a.AttributionId }, ct);
                    if (net > 0) await ledger.PostAsync(u, partnerId, a.Currency.Trim(), Postings.FeeFinalize(a.AttributionId, net), "attribution-finalizer", ct, now);
                    await Outbox.EnqueueAsync(u, partnerId, Topics.Attribution, a.OrderId, new { orderId = a.OrderId, lineId = a.LineId, stage = "FINAL", fee = net }, ct);
                }
                if (due.Count > 0)
                {
                    await u.SetRoleAsync(DbRole.App, ct);
                    await u.ExecuteAsync("update txn.order_header set user_ref = null where order_id = any(@orders) and user_ref is not null",
                        new { orders = due.Select(d => d.OrderId).Distinct().ToArray() }, ct);
                }
                return due.Count;
            }, ct);
        }
        if (total > 0) log.LogInformation("FP-1 finalizer: {Count} attributions FINAL", total);
        return total;
    }

    public static async Task<AttributionSummary> SummaryAsync(UnitOfWork u, string orderId, string currency, CancellationToken ct)
    {
        var rows = (await u.QueryAsync<AttrRow>($"select {Columns} from txn.attribution where order_id = @orderId order by line_id", new { orderId }, ct)).ToList();
        var billable = rows.Where(r => r.State is "PENDING" or "ACCRUED" or "FINAL" or "CREDITED").ToList();
        return new AttributionSummary(orderId, currency, ApiMapper.Money(billable.Sum(r => r.FairPremium), currency),
            ApiMapper.Money(billable.Sum(r => r.FeeAmount - r.ReversedAmount), currency),
            rows.Select(r => new AttributionLine
            {
                LineId = r.LineId, SuggestionId = r.SuggestionId, OriginalPartnerSku = r.OriginalPartnerSku, AlternativePartnerSku = r.AlternativePartnerSku,
                ReplacedQuantity = r.ReplacedQty, OriginalUnitPriceSnapshot = ApiMapper.Money(r.OrigUnitPriceSnapshot, currency),
                AlternativeNetUnitPrice = ApiMapper.Money(r.AltNetUnitPrice, currency), PackDelta = ApiMapper.Money(r.PackDelta, currency),
                SizeNormalizedDelta = r.SizeNormalizedDelta is { } d ? ApiMapper.Money(d, currency) : null, FairPremium = ApiMapper.Money(r.FairPremium, currency),
                FeeRate = r.FeeRate, Fee = ApiMapper.Money(r.FeeAmount - r.ReversedAmount, currency), State = r.State, RejectionReason = r.RejectionReason,
                FinalizesAt = r.FinalizesAt, RuleVersion = r.RuleVersion,
            }).ToList());
    }
}
