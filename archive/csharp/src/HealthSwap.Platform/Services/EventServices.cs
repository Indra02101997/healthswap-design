using System.Globalization;
using System.Text;
using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Domain.Commerce;
using HealthSwap.Platform.Bus;
using HealthSwap.Platform.Cache;
using HealthSwap.Platform.Data;
using HealthSwap.Platform.Knowledge;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace HealthSwap.Platform.Services;

/// <summary>Suggestion snapshots: hot cache first (written at serve time), PostgreSQL second (txn.suggestion, written asynchronously).</summary>
public sealed class SuggestionStore(Db db, HotCache cache)
{
    private sealed class Row
    {
        public Guid SuggestionId { get; set; }
        public DateTime CreatedAt { get; set; }
        public Guid? EvaluationId { get; set; }
        public Guid PartnerId { get; set; }
        public string CartRef { get; set; } = "";
        public string? UserRef { get; set; }
        public string Market { get; set; } = "";
        public string? StoreId { get; set; }
        public string Placement { get; set; } = "";
        public string? LineId { get; set; }
        public string FromPartnerSku { get; set; } = "";
        public Guid FromProductId { get; set; }
        public long FromVersionId { get; set; }
        public decimal FromQty { get; set; }
        public decimal? FromUnitPrice { get; set; }
        public decimal? FromNetQuantity { get; set; }
        public string? FromNetUnit { get; set; }
        public decimal? FromTaxRate { get; set; }
        public string Currency { get; set; } = "";
        public string Alternatives { get; set; } = "[]";
        public string? RecoFee { get; set; }
        public string RulesVersion { get; set; } = "";
        public string GraphVersion { get; set; } = "";
        public DateTime ExpiresAt { get; set; }
        public DateTime AttributionExpiresAt { get; set; }
    }

    public const string SelectSql =
        """
        select suggestion_id, created_at, evaluation_id, partner_id, cart_ref, user_ref, market, store_id, placement, line_id, from_partner_sku, from_product_id,
               from_version_id, from_qty, from_unit_price, from_net_quantity, from_net_unit, from_tax_rate, currency, alternatives::text as alternatives,
               reco_fee::text as reco_fee, rules_version, graph_version, expires_at, attribution_expires_at
        from txn.suggestion
        """;

    public async Task<SuggestionSnapshot?> GetAsync(Guid partnerId, Guid suggestionId, CancellationToken ct)
    {
        var cached = await cache.GetSuggestionAsync(suggestionId);
        if (cached is not null) return cached.PartnerId == partnerId ? cached : null;
        var row = await db.InTxAsync(DbRole.App, partnerId, u => u.QueryFirstOrDefaultAsync<Row>(SelectSql + " where suggestion_id = @suggestionId", new { suggestionId }, ct), ct);
        return row is null ? null : Map(row);
    }

    public static async Task<SuggestionSnapshot?> LoadInTxAsync(UnitOfWork u, Guid suggestionId, CancellationToken ct)
    {
        var row = await u.QueryFirstOrDefaultAsync<Row>(SelectSql + " where suggestion_id = @suggestionId", new { suggestionId }, ct);
        return row is null ? null : Map(row);
    }

    private static SuggestionSnapshot Map(Row r) => new()
    {
        SuggestionId = r.SuggestionId, CreatedAt = r.CreatedAt, EvaluationId = r.EvaluationId, PartnerId = r.PartnerId, CartRef = r.CartRef, UserRef = r.UserRef,
        Market = r.Market, StoreId = r.StoreId, Placement = r.Placement, LineId = r.LineId, FromPartnerSku = r.FromPartnerSku, FromProductId = r.FromProductId,
        FromVersionId = r.FromVersionId, FromQty = r.FromQty, FromUnitPrice = r.FromUnitPrice, FromNetQuantity = r.FromNetQuantity, FromNetUnit = r.FromNetUnit,
        FromTaxRate = r.FromTaxRate, Currency = r.Currency.Trim(), Alternatives = JsonSerializer.Deserialize<List<AlternativeSnapshot>>(r.Alternatives, HotCache.Json) ?? [],
        RecoFee = r.RecoFee is null ? null : JsonSerializer.Deserialize<RecoFeeSnapshot>(r.RecoFee, HotCache.Json), RulesVersion = r.RulesVersion,
        GraphVersion = r.GraphVersion, ExpiresAt = r.ExpiresAt, AttributionExpiresAt = r.AttributionExpiresAt,
    };
}

/// <summary>
/// Suggestion event collector (HLD 7.2): dedupe on eventId, evidence for attribution, RA-1 charges for ACCEPT / ADD_TO_CART of a
/// HealthSwap-recommended alternative, UNDO voids within the grace period, DISMISS feeds the R8 fatigue store, line labels.
/// </summary>
public sealed class EventService(Db db, HotCache cache, SuggestionStore suggestions, RecoChargeService charges, PartnerDirectory partners, ReferenceDataRepository refs,
    IClock clock)
{
    public async Task<EventBatchResult> IngestAsync(PartnerContext ctx, EventBatchRequest req, CancellationToken ct)
    {
        var now = clock.UtcNow;
        var partner = await partners.GetAsync(ctx.PartnerId, ct);
        var snapshots = new Dictionary<Guid, SuggestionSnapshot?>();
        foreach (var id in req.Events.Select(e => e.SuggestionId).Distinct())
            snapshots[id] = await suggestions.GetAsync(ctx.PartnerId, id, ct);

        var rejected = new List<RejectedEvent>();
        var acks = new List<RecommendationChargeAck>();
        var labelAdds = new List<(string CartRef, string Sku, Guid SuggestionId, DateTime Until)>();
        var dismissals = new List<(string Key, Guid From, Guid To)>();
        int accepted = 0, duplicates = 0;
        var cooldownDays = 7;
        if (partner.Markets.Count > 0) cooldownDays = (await refs.GetPackAsync(partner.Markets[0], ct)).Rules.DismissCooldownDays;

        await db.InTxAsync(DbRole.App, ctx.PartnerId, async u =>
        {
            foreach (var (e, _) in req.Events.Select((e, i) => (e, i)).OrderBy(x => x.e.OccurredAt).ThenBy(x => x.i))
            {
                var s = snapshots[e.SuggestionId];
                string? problem = null, code = null;
                if (s is null) (code, problem) = ("HS-EVENT-UNKNOWN-SUGGESTION", "Unknown or expired suggestionId");
                else if (ctx.IsSdkSession && s.CartRef != ctx.SdkCartRef) (code, problem) = ("HS-AUTH-SESSION-SCOPE", "The suggestion belongs to another cart session");
                else if (e.OccurredAt > now.AddMinutes(5)) (code, problem) = ("HS-EVENT-TIME", "occurredAt is in the future");
                else if (e.Type is "ACCEPT" or "ADD_TO_CART" && (e.AlternativePartnerSku is null || s.Alternatives.All(a => a.PartnerSku != e.AlternativePartnerSku)))
                    (code, problem) = ("HS-EVENT-SKU-NOT-RECOMMENDED", "alternativePartnerSku must be one of the suggestion's recommended alternatives");
                if (problem is not null) { rejected.Add(new RejectedEvent(e.EventId.ToString(), code!, problem)); continue; }

                var inserted = await u.ExecuteAsync("insert into txn.event_key (partner_id, event_id, occurred_at) values (@p, @id, @at) on conflict do nothing",
                    new { p = ctx.PartnerId, id = e.EventId, at = e.OccurredAt }, ct);
                if (inserted == 0)
                {
                    duplicates++;
                    if (ctx.IsServer && await charges.AckForEventAsync(u, e.EventId, ct) is { } existing) acks.Add(existing);
                    continue;
                }
                await u.ExecuteAsync(
                    """
                    insert into txn.suggestion_event (event_id, occurred_at, received_at, partner_id, suggestion_id, event_type, alternative_partner_sku, quantity, metadata)
                    values (@id, @at, @now, @p, @sid, @type, @sku, @qty, @meta::jsonb)
                    """,
                    new { id = e.EventId, at = e.OccurredAt, now, p = ctx.PartnerId, sid = e.SuggestionId, type = e.Type, sku = e.AlternativePartnerSku, qty = e.Quantity,
                          meta = JsonSerializer.Serialize(e.Metadata ?? []) }, ct);
                accepted++;

                switch (e.Type)
                {
                    case "ACCEPT" or "ADD_TO_CART":
                        labelAdds.Add((s!.CartRef, e.AlternativePartnerSku!, s.SuggestionId, s.AttributionExpiresAt));
                        if (s.RecoFee is { } fee && RecoAddFeeRule.IsChargeableEvent(e.Type, fee))
                        {
                            var ack = await charges.CreatePendingAsync(u, s, e, now, ct);
                            if (ctx.IsServer) acks.Add(ack);
                        }
                        break;
                    case "UNDO":
                        // Labels stay on the cart line: earlier adds may still be in the cart, and lineLabels only lists lines present in the cart.
                        await charges.VoidOnUndoAsync(u, s!, e, now, ct);
                        break;
                    case "DISMISS":
                        var key = s!.UserRef ?? "cart:" + s.CartRef;
                        foreach (var a in s.Alternatives) dismissals.Add((key, s.FromProductId, a.ProductId));
                        break;
                }
                await Outbox.EnqueueAsync(u, ctx.PartnerId, Topics.SuggestionEvents, e.SuggestionId.ToString(), new
                {
                    eventId = e.EventId, type = e.Type, suggestionId = e.SuggestionId, e.AlternativePartnerSku, e.Quantity, e.OccurredAt, receivedAt = now,
                    placement = s!.Placement, market = s.Market, fromProductId = s.FromProductId,
                    rank = s.Alternatives.FirstOrDefault(a => a.PartnerSku == e.AlternativePartnerSku)?.Rank,
                }, ct);
            }
        }, ct);

        foreach (var (cartRef, sku, sid, until) in labelAdds)
            await cache.AddCartRecoLabelAsync(ctx.PartnerId, cartRef, sku, sid, until - now > TimeSpan.Zero ? until - now : TimeSpan.FromHours(1));
        foreach (var (key, from, to) in dismissals) await cache.SetDismissedAsync(ctx.PartnerId, key, from, to, TimeSpan.FromDays(cooldownDays));

        return new EventBatchResult
        {
            Accepted = accepted, Duplicates = duplicates, Rejected = rejected.Count > 0 ? rejected : null, Charges = acks.Count > 0 ? acks : null,
        };
    }
}

/// <summary>RA-1 charge lifecycle: PENDING at the add, VOID on a timely UNDO, FINAL (ledger RECO_FEE) or REJECTED by the finalizer.</summary>
public sealed class RecoChargeService(Db db, LedgerService ledger, IClock clock, ILogger<RecoChargeService> log)
{
    private sealed class ChargeRow
    {
        public Guid ChargeId { get; set; }
        public Guid PartnerId { get; set; }
        public Guid EventId { get; set; }
        public string EventType { get; set; } = "";
        public Guid SuggestionId { get; set; }
        public string CartRef { get; set; } = "";
        public string Placement { get; set; } = "";
        public string AlternativePartnerSku { get; set; } = "";
        public decimal Quantity { get; set; }
        public string Basis { get; set; } = "";
        public decimal UnitAmount { get; set; }
        public decimal Amount { get; set; }
        public string Currency { get; set; } = "";
        public string RuleVersion { get; set; } = "";
        public string State { get; set; } = "";
        public string? RejectionReason { get; set; }
        public DateTime OccurredAt { get; set; }
        public DateTime ReceivedAt { get; set; }
        public DateTime FinalizesAt { get; set; }
        public DateTime? FinalizedAt { get; set; }
        public Guid? StatementId { get; set; }
        public decimal CreditedAmount { get; set; }
    }

    private const string Columns =
        """
        charge_id, partner_id, event_id, event_type, suggestion_id, cart_ref, placement, alternative_partner_sku, quantity, basis, unit_amount, amount, currency,
        rule_version, state, rejection_reason, occurred_at, received_at, finalizes_at, finalized_at, statement_id, credited_amount
        """;

    public async Task<RecommendationChargeAck> CreatePendingAsync(UnitOfWork u, SuggestionSnapshot s, SuggestionEvent e, DateTime receivedAt, CancellationToken ct)
    {
        var fee = s.RecoFee!;
        var amount = RecoAddFeeRule.Amount(fee, e.Quantity);
        var finalizesAt = RecoAddFeeRule.FinalizesAt(e.OccurredAt, receivedAt, fee);
        var chargeId = Ids.NewId();
        var alt = s.Alternatives.First(a => a.PartnerSku == e.AlternativePartnerSku);
        await u.ExecuteAsync(
            $"""
            insert into txn.reco_charge (charge_id, partner_id, event_id, event_type, suggestion_id, cart_ref, user_ref, market, placement, alternative_partner_sku,
                alternative_product_id, quantity, basis, unit_amount, amount, currency, rule_version, state, occurred_at, received_at, finalizes_at)
            values (@chargeId, @partnerId, @eventId, @type, @sid, @cartRef, @userRef, @market, @placement, @sku, @product, @qty, @basis, @unit, @amount, @currency,
                @rule, 'PENDING', @occurred, @received, @finalizesAt)
            on conflict (partner_id, event_id) do nothing
            """,
            new
            {
                chargeId, partnerId = s.PartnerId, eventId = e.EventId, type = e.Type, sid = s.SuggestionId, cartRef = s.CartRef, userRef = s.UserRef, market = s.Market,
                placement = s.Placement, sku = alt.PartnerSku, product = alt.ProductId, qty = Math.Max(0.001m, e.Quantity ?? 1m), basis = fee.Basis, unit = fee.Amount,
                amount, currency = fee.Currency, rule = fee.Rule, occurred = e.OccurredAt, received = receivedAt, finalizesAt,
            }, ct);
        await Outbox.EnqueueAsync(u, s.PartnerId, Topics.RecoCharges, chargeId.ToString(), new { chargeId, state = "PENDING", amount, fee.Currency, suggestionId = s.SuggestionId, eventType = e.Type }, ct);
        return new RecommendationChargeAck(e.EventId, chargeId, "PENDING", ApiMapper.Money(amount, fee.Currency), finalizesAt);
    }

    public async Task<RecommendationChargeAck?> AckForEventAsync(UnitOfWork u, Guid eventId, CancellationToken ct)
    {
        var r = await u.QueryFirstOrDefaultAsync<ChargeRow>($"select {Columns} from txn.reco_charge where event_id = @eventId", new { eventId }, ct);
        return r is null ? null : new RecommendationChargeAck(r.EventId, r.ChargeId, r.State, ApiMapper.Money(r.Amount, r.Currency.Trim()), r.State == "PENDING" ? r.FinalizesAt : null);
    }

    /// <summary>Voids the latest PENDING add of the suggestion when the UNDO is within the grace period. Returns the affected SKU.</summary>
    public async Task<string?> VoidOnUndoAsync(UnitOfWork u, SuggestionSnapshot s, SuggestionEvent undo, DateTime now, CancellationToken ct)
    {
        if (s.RecoFee is not { } fee) return null;
        var latest = await u.QueryFirstOrDefaultAsync<ChargeRow>(
            $"select {Columns} from txn.reco_charge where suggestion_id = @sid and state = 'PENDING' and (@sku::text is null or alternative_partner_sku = @sku) and occurred_at <= @at order by occurred_at desc limit 1",
            new { sid = s.SuggestionId, sku = undo.AlternativePartnerSku, at = undo.OccurredAt }, ct);
        if (latest is null) return null;
        if (!RecoAddFeeRule.UndoVoids(latest.OccurredAt, undo.OccurredAt, fee)) return latest.AlternativePartnerSku;
        await u.ExecuteAsync("update txn.reco_charge set state = 'VOID', rejection_reason = @reason, finalized_at = @now, user_ref = null where charge_id = @id and state = 'PENDING'",
            new { reason = RecoChargeReasons.UndoneWithinGrace, now, id = latest.ChargeId }, ct);
        await Outbox.EnqueueAsync(u, s.PartnerId, Topics.RecoCharges, latest.ChargeId.ToString(), new { chargeId = latest.ChargeId, state = "VOID", reason = RecoChargeReasons.UndoneWithinGrace }, ct);
        return latest.AlternativePartnerSku;
    }

    /// <summary>Finalizer: applies the RA-1 evidence chain to due PENDING charges and posts RECO_FEE for FINAL ones.</summary>
    public async Task<(int Final, int Rejected)> FinalizeDueAsync(CancellationToken ct)
    {
        var now = clock.UtcNow;
        var partnerIds = (await db.InTxAsync(DbRole.Ops, null, u => u.QueryAsync<Guid>(
            "select distinct partner_id from txn.reco_charge where state = 'PENDING' and finalizes_at <= @now", new { now }, ct), ct)).ToList();
        int final = 0, rejected = 0;
        foreach (var partnerId in partnerIds)
        {
            var (f, r) = await db.InTxAsync(DbRole.LedgerWriter, partnerId, async u =>
            {
                int fc = 0, rc = 0;
                var due = (await u.QueryAsync<ChargeRow>(
                    $"select {Columns} from txn.reco_charge where state = 'PENDING' and finalizes_at <= @now order by occurred_at limit 500 for update skip locked",
                    new { now }, ct)).ToList();
                foreach (var c in due)
                {
                    var s = await SuggestionStore.LoadInTxAsync(u, c.SuggestionId, ct);
                    if (s is null && now - c.ReceivedAt < TimeSpan.FromHours(1)) continue; // the asynchronous suggestion write may still be in flight
                    var fee = s?.RecoFee ?? new RecoFeeSnapshot { Currency = c.Currency.Trim(), Amount = c.UnitAmount, Basis = c.Basis };
                    var firstImpression = await u.ScalarAsync<DateTime?>(
                        "select min(occurred_at) from txn.suggestion_event where suggestion_id = @sid and event_type = 'IMPRESSION'", new { sid = c.SuggestionId }, ct);
                    var finalForSuggestion = await u.ScalarAsync<int>(
                        "select count(*)::int from txn.reco_charge where suggestion_id = @sid and state in ('FINAL', 'CREDITED')", new { sid = c.SuggestionId }, ct);
                    var monthStart = new DateTime(c.OccurredAt.Year, c.OccurredAt.Month, 1, 0, 0, 0, DateTimeKind.Utc);
                    var monthToDate = await u.ScalarAsync<decimal>(
                        "select coalesce(sum(amount), 0) from txn.reco_charge where state in ('FINAL', 'CREDITED') and occurred_at >= @from and occurred_at < @to",
                        new { from = monthStart, to = monthStart.AddMonths(1) }, ct);
                    var evidence = new RecoChargeEvidence
                    {
                        SuggestionServed = s is not null, SkuRecommended = s?.Alternatives.Any(a => a.PartnerSku == c.AlternativePartnerSku) ?? false,
                        FirstImpressionAt = firstImpression, SuggestionCreatedAt = s?.CreatedAt ?? c.OccurredAt, AttributionExpiresAt = s?.AttributionExpiresAt ?? c.OccurredAt,
                        FinalChargesForSuggestion = finalForSuggestion, MonthToDateAmount = monthToDate,
                    };
                    var (state, reason) = RecoAddFeeRule.Decide(c.OccurredAt, c.Amount, fee, evidence);
                    await u.ExecuteAsync(
                        "update txn.reco_charge set state = @state, rejection_reason = @reason, finalized_at = @now, user_ref = null where charge_id = @id and state = 'PENDING'",
                        new { state, reason, now, id = c.ChargeId }, ct);
                    if (state == "FINAL")
                    {
                        await ledger.PostAsync(u, partnerId, c.Currency.Trim(), Postings.RecoFee(c.ChargeId, c.Amount), "reco-charge-finalizer", ct, now);
                        fc++;
                    }
                    else rc++;
                    await Outbox.EnqueueAsync(u, partnerId, Topics.RecoCharges, c.ChargeId.ToString(), new { chargeId = c.ChargeId, state, reason, amount = c.Amount }, ct);
                }
                return (fc, rc);
            }, ct);
            final += f;
            rejected += r;
        }
        if (final + rejected > 0) log.LogInformation("RA-1 finalizer: {Final} FINAL, {Rejected} REJECTED", final, rejected);
        return (final, rejected);
    }

    public async Task<RecommendationChargePage> ListAsync(Guid partnerId, string? period, string? state, string? cartRef, Guid? suggestionId, Guid? statementId,
        string? cursor, int limit, CancellationToken ct)
    {
        DateTime? from = null, to = null;
        if (period is not null)
        {
            if (!Period.TryParse(period, out var start, out var end)) throw HsException.Invalid("period must be YYYY-MM");
            (from, to) = (start, end);
        }
        var (cAt, cId) = Cursor.Decode(cursor);
        limit = Math.Clamp(limit, 1, 200);
        var rows = await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            if (statementId is not null && !await u.ScalarAsync<bool>("select exists (select 1 from billing.statement where statement_id = @statementId)", new { statementId }, ct))
                throw HsException.NotFound("Statement " + statementId);
            return (await u.QueryAsync<ChargeRow>(
                $"""
                select {Columns} from txn.reco_charge
                where (@from::timestamptz is null or occurred_at >= @from) and (@to::timestamptz is null or occurred_at < @to)
                  and (@state::text is null or state = @state) and (@cartRef::text is null or cart_ref = @cartRef)
                  and (@sid::uuid is null or suggestion_id = @sid) and (@statementId::uuid is null or statement_id = @statementId)
                  and (@cAt::timestamptz is null or (occurred_at, charge_id) < (@cAt::timestamptz, @cId::uuid))
                order by occurred_at desc, charge_id desc limit @take
                """,
                new { from, to, state, cartRef, sid = suggestionId, statementId, cAt, cId, take = limit + 1 }, ct)).ToList();
        }, ct);
        var next = rows.Count > limit ? Cursor.Encode(rows[limit - 1].OccurredAt, rows[limit - 1].ChargeId) : null;
        return new RecommendationChargePage(rows.Take(limit).Select(ToApi).ToList(), next);
    }

    public async Task<RecommendationCharge> GetAsync(Guid partnerId, Guid chargeId, CancellationToken ct)
    {
        var row = await db.InTxAsync(DbRole.App, partnerId, u => u.QueryFirstOrDefaultAsync<ChargeRow>($"select {Columns} from txn.reco_charge where charge_id = @chargeId",
            new { chargeId }, ct), ct) ?? throw HsException.NotFound("Recommendation charge " + chargeId);
        return ToApi(row);
    }

    private static RecommendationCharge ToApi(ChargeRow r)
    {
        var currency = r.Currency.Trim();
        return new RecommendationCharge
        {
            ChargeId = r.ChargeId, EventId = r.EventId, EventType = r.EventType, SuggestionId = r.SuggestionId, CartRef = r.CartRef, Placement = r.Placement,
            AlternativePartnerSku = r.AlternativePartnerSku, Quantity = r.Quantity, Basis = r.Basis, Amount = ApiMapper.Money(r.Amount, currency), State = r.State,
            RejectionReason = r.RejectionReason, RuleVersion = r.RuleVersion, OccurredAt = r.OccurredAt, FinalizesAt = r.State == "PENDING" ? r.FinalizesAt : null,
            FinalizedAt = r.FinalizedAt, StatementId = r.StatementId, CreditedAmount = r.CreditedAmount > 0 ? ApiMapper.Money(r.CreditedAmount, currency) : null,
        };
    }
}

/// <summary>Opaque keyset cursors: base64url("ticks|id").</summary>
public static class Cursor
{
    public static string Encode(DateTime at, Guid id) =>
        Convert.ToBase64String(Encoding.UTF8.GetBytes($"{at.Ticks}|{id}")).TrimEnd('=').Replace('+', '-').Replace('/', '_');

    public static (DateTime? At, Guid? Id) Decode(string? cursor)
    {
        if (string.IsNullOrEmpty(cursor)) return (null, null);
        try
        {
            var b64 = cursor.Replace('-', '+').Replace('_', '/');
            b64 = b64.PadRight(b64.Length + (4 - b64.Length % 4) % 4, '=');
            var parts = Encoding.UTF8.GetString(Convert.FromBase64String(b64)).Split('|');
            return (new DateTime(long.Parse(parts[0], CultureInfo.InvariantCulture), DateTimeKind.Utc), Guid.Parse(parts[1]));
        }
        catch (Exception ex) when (ex is FormatException or IndexOutOfRangeException or ArgumentOutOfRangeException)
        {
            throw HsException.Invalid("Invalid cursor");
        }
    }

    public static string EncodeOffset(int offset) => Convert.ToBase64String(Encoding.UTF8.GetBytes("o|" + offset)).TrimEnd('=');

    public static int DecodeOffset(string? cursor)
    {
        if (string.IsNullOrEmpty(cursor)) return 0;
        try
        {
            var b64 = cursor.PadRight(cursor.Length + (4 - cursor.Length % 4) % 4, '=');
            var s = Encoding.UTF8.GetString(Convert.FromBase64String(b64));
            return s.StartsWith("o|", StringComparison.Ordinal) ? int.Parse(s[2..], CultureInfo.InvariantCulture) : throw new FormatException();
        }
        catch (FormatException) { throw HsException.Invalid("Invalid cursor"); }
    }
}

public sealed class SchedulerOptions
{
    public bool Enabled { get; set; } = true;
    public int RecoFinalizerSeconds { get; set; } = 30;
    public int AttributionFinalizerSeconds { get; set; } = 60;
}

/// <summary>Background schedulers: RA-1 charge finalization and FP-1 return-window finalization.</summary>
public sealed class FinalizerWorker(RecoChargeService reco, AttributionEngine attribution, SchedulerOptions options, ILogger<FinalizerWorker> log) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        if (!options.Enabled) return;
        var lastAttribution = DateTime.MinValue;
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                await reco.FinalizeDueAsync(stoppingToken);
                if (DateTime.UtcNow - lastAttribution > TimeSpan.FromSeconds(options.AttributionFinalizerSeconds))
                {
                    await attribution.FinalizeDueAsync(stoppingToken);
                    lastAttribution = DateTime.UtcNow;
                }
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex) { log.LogWarning(ex, "Finalizer iteration failed"); }
            try { await Task.Delay(TimeSpan.FromSeconds(options.RecoFinalizerSeconds), stoppingToken); } catch (OperationCanceledException) { break; }
        }
    }
}
