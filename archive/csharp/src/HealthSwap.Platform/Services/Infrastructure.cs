using System.Net;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Domain.Commerce;
using HealthSwap.Platform.Bus;
using HealthSwap.Platform.Data;
using Microsoft.AspNetCore.DataProtection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace HealthSwap.Platform.Services;

public static class WebhookTypes
{
    public const string CatalogReview = "catalog.match.review_required";
    public const string ProfileChanged = "product.health_profile.changed";
    public const string StatementIssued = "billing.statement.issued";
    public const string DisputeUpdated = "billing.dispute.updated";
    public const string ErasureCompleted = "user.erasure.completed";
}

public sealed class WebhookOptions
{
    /// <summary>Development only: allow http://localhost endpoints (production requires https and public hosts).</summary>
    public bool AllowHttpLocalhost { get; set; }
    public int MaxAttemptHours { get; set; } = 24;
    public int TimeoutSeconds { get; set; } = 10;
}

/// <summary>HMAC-SHA256 signatures: <c>HealthSwap-Signature: t=&lt;unix&gt;,v1=&lt;hex&gt;</c> over "t.body" (doc 04 webhook section).</summary>
public static class WebhookSignature
{
    public const string Header = "HealthSwap-Signature";

    public static string Sign(string secret, long timestamp, string body) => $"t={timestamp},v1={Mac(secret, timestamp, body)}";

    private static string Mac(string secret, long timestamp, string body) =>
        Convert.ToHexStringLower(HMACSHA256.HashData(Encoding.UTF8.GetBytes(secret), Encoding.UTF8.GetBytes($"{timestamp}.{body}")));

    public static bool Verify(string secret, string header, string body, DateTimeOffset now, TimeSpan tolerance)
    {
        var parts = header.Split(',').Select(p => p.Split('=', 2)).Where(p => p.Length == 2).ToDictionary(p => p[0].Trim(), p => p[1].Trim());
        if (!parts.TryGetValue("t", out var t) || !long.TryParse(t, out var ts) || !parts.TryGetValue("v1", out var v1)) return false;
        if (Math.Abs(now.ToUnixTimeSeconds() - ts) > tolerance.TotalSeconds) return false;
        return CryptographicOperations.FixedTimeEquals(Encoding.ASCII.GetBytes(Mac(secret, ts, body)), Encoding.ASCII.GetBytes(v1));
    }
}

/// <summary>Encrypts webhook signing secrets at rest (ASP.NET Core Data Protection; key ring in KMS/Key Vault in production).</summary>
public sealed class SecretProtector(IDataProtectionProvider provider)
{
    private readonly IDataProtector _protector = provider.CreateProtector("HealthSwap.WebhookSecrets.v1");
    public string Protect(string secret) => _protector.Protect(secret);
    public string Unprotect(string ciphertext) => _protector.Unprotect(ciphertext);
}

public static class UrlPolicy
{
    /// <summary>Rejects non-https URLs, credentials in URLs and private/loopback IP literals (SSRF guard).</summary>
    public static string? Problem(string url, bool allowHttpLocalhost)
    {
        if (!Uri.TryCreate(url, UriKind.Absolute, out var u)) return "URL is not absolute";
        if (!string.IsNullOrEmpty(u.UserInfo)) return "URL must not contain credentials";
        var local = u.IsLoopback || u.Host.Equals("localhost", StringComparison.OrdinalIgnoreCase);
        if (local) return allowHttpLocalhost ? null : "Loopback hosts are not allowed";
        if (u.Scheme != Uri.UriSchemeHttps) return "Webhook URLs must use https";
        if (IPAddress.TryParse(u.Host, out var ip) && IsPrivate(ip)) return "Private network addresses are not allowed";
        return null;
    }

    private static bool IsPrivate(IPAddress ip)
    {
        if (IPAddress.IsLoopback(ip)) return true;
        var b = ip.MapToIPv4().GetAddressBytes();
        return b[0] == 10 || (b[0] == 172 && b[1] is >= 16 and <= 31) || (b[0] == 192 && b[1] == 168) || (b[0] == 169 && b[1] == 254) || b[0] == 0;
    }
}

/// <summary>Turns domain events into per-endpoint webhook deliveries (hs_ops: needs endpoint and catalog lookups across roles).</summary>
/// <remarks>Webhook delivery is infrastructure: it runs on wall-clock time, not on the (time-travel capable) business clock.</remarks>
public sealed class WebhookFanoutConsumer(Db db, ILogger<WebhookFanoutConsumer> log) : IBusConsumer
{
    public const string Topic = "hs.webhook-requests.v1";
    public IReadOnlyCollection<string> Topics { get; } = [Topic, Bus.Topics.KbProductChanges];

    private sealed class EndpointRow { public Guid EndpointId { get; set; } public Guid PartnerId { get; set; } public string[] EventTypes { get; set; } = []; }
    private sealed class SkuRow { public Guid PartnerId { get; set; } public string PartnerSku { get; set; } = ""; }

    /// <summary>Requests a webhook from inside any unit of work (written to the outbox; delivered asynchronously).</summary>
    public static Task RequestAsync(UnitOfWork u, Guid partnerId, string type, object data, CancellationToken ct) =>
        Outbox.EnqueueAsync(u, partnerId, Topic, partnerId.ToString(), new { type, partnerId, data, id = "evt_" + Guid.CreateVersion7().ToString("N") }, ct);

    public async Task HandleAsync(BusMessage m, CancellationToken ct)
    {
        if (m.Topic == Bus.Topics.KbProductChanges)
        {
            if (!m.Payload.TryGetProperty("kind", out var kind) || kind.GetString() != "PROFILE_CHANGED") return;
            var productId = m.Payload.GetProperty("productId").GetGuid();
            var market = m.Payload.GetProperty("market").GetString()!;
            var changes = m.Payload.GetProperty("changes").EnumerateArray().Select(x => x.GetString()!).ToList();
            if (changes.SequenceEqual(["CREATED"])) return; // initial scoring is not a change for partners
            var skus = await db.InTxAsync(DbRole.Ops, null, u => u.QueryAsync<SkuRow>(
                "select c.partner_id, c.partner_sku from tenant.catalog_item c join tenant.partner p using (partner_id) where c.product_id = @productId and @market = any(p.markets)",
                new { productId, market }, ct), ct);
            foreach (var g in skus.GroupBy(s => s.PartnerId))
                await CreateDeliveriesAsync(g.Key, WebhookTypes.ProfileChanged, "evt_" + Guid.CreateVersion7().ToString("N"), new
                {
                    productId, market, partnerSkus = g.Select(x => x.PartnerSku).Order(StringComparer.Ordinal).ToList(),
                    previousProfileVersion = m.Payload.TryGetProperty("previousProfileVersion", out var p) && p.ValueKind == JsonValueKind.String ? p.GetString() : null,
                    profileVersion = m.Payload.GetProperty("profileVersion").GetString(), changes,
                }, ct);
            return;
        }
        var partnerId = m.Payload.GetProperty("partnerId").GetGuid();
        await CreateDeliveriesAsync(partnerId, m.Payload.GetProperty("type").GetString()!, m.Payload.GetProperty("id").GetString()!, m.Payload.GetProperty("data"), ct);
    }

    private async Task CreateDeliveriesAsync(Guid partnerId, string type, string webhookId, object data, CancellationToken ct)
    {
        var envelope = new WebhookEnvelope
        {
            Id = webhookId, Type = type, CreatedAt = DateTime.UtcNow, ApiVersion = "1.1", PartnerId = partnerId,
            Data = JsonSerializer.SerializeToElement(data, HsJson.Options),
        };
        var payload = JsonSerializer.Serialize(envelope, HsJson.Options);
        var n = await db.InTxAsync(DbRole.Ops, null, async u =>
        {
            var endpoints = (await u.QueryAsync<EndpointRow>(
                "select endpoint_id, partner_id, event_types from tenant.webhook_endpoint where partner_id = @partnerId and status = 'ACTIVE'", new { partnerId }, ct))
                .Where(e => e.EventTypes.Length == 0 || e.EventTypes.Contains(type)).ToList();
            foreach (var e in endpoints)
                await u.ExecuteAsync(
                    "insert into txn.webhook_delivery (webhook_id, partner_id, endpoint_id, event_type, payload, next_attempt_at) values (@webhookId, @partnerId, @endpointId, @type, @payload::jsonb, @now)",
                    new { webhookId, partnerId, endpointId = e.EndpointId, type, payload, now = DateTime.UtcNow }, ct);
            return endpoints.Count;
        }, ct);
        log.LogInformation("Webhook {Type} {Id} queued for {Count} endpoint(s) of partner {Partner}", type, webhookId, n, partnerId);
    }
}

/// <summary>At-least-once signed delivery with exponential backoff for up to 24 hours, then DEAD (HLD 8.4).</summary>
public sealed class WebhookDispatcher(Db db, IHttpClientFactory httpFactory, SecretProtector secrets, WebhookOptions options,
    ILogger<WebhookDispatcher> log) : BackgroundService
{
    private sealed class DueRow
    {
        public Guid DeliveryId { get; set; }
        public string WebhookId { get; set; } = "";
        public Guid PartnerId { get; set; }
        public string Payload { get; set; } = "";
        public int Attempts { get; set; }
        public DateTime CreatedAt { get; set; }
        public string Url { get; set; } = "";
        public string SecretCiphertext { get; set; } = "";
    }

    private static readonly int[] BackoffSeconds = [10, 30, 120, 600, 1800, 3600, 7200, 14400];

    public async Task<int> DispatchDueAsync(CancellationToken ct)
    {
        var now = DateTime.UtcNow;
        // Claim a batch with a short lease, then send outside the transaction (never hold a transaction during HTTP calls).
        var due = await db.InTxAsync(DbRole.Ops, null, async u =>
        {
            var rows = (await u.QueryAsync<DueRow>(
                """
                select d.delivery_id, d.webhook_id, d.partner_id, d.payload::text as payload, d.attempts, d.created_at, e.url, e.secret_ciphertext
                from txn.webhook_delivery d join tenant.webhook_endpoint e on e.endpoint_id = d.endpoint_id
                where d.status = 'PENDING' and d.next_attempt_at <= @now order by d.next_attempt_at limit 50 for update of d skip locked
                """, new { now }, ct)).ToList();
            if (rows.Count > 0)
                await u.ExecuteAsync("update txn.webhook_delivery set next_attempt_at = @lease where delivery_id = any(@ids)",
                    new { lease = now.AddSeconds(60), ids = rows.Select(r => r.DeliveryId).ToArray() }, ct);
            return rows;
        }, ct);

        foreach (var d in due)
        {
            int? status = null;
            string? error = null;
            var problem = UrlPolicy.Problem(d.Url, options.AllowHttpLocalhost);
            if (problem is not null) error = problem;
            else
            {
                try
                {
                    var secret = secrets.Unprotect(d.SecretCiphertext);
                    var ts = DateTimeOffset.UtcNow.ToUnixTimeSeconds();
                    using var req = new HttpRequestMessage(HttpMethod.Post, d.Url) { Content = new StringContent(d.Payload, Encoding.UTF8, "application/json") };
                    req.Headers.Add(WebhookSignature.Header, WebhookSignature.Sign(secret, ts, d.Payload));
                    req.Headers.Add("HealthSwap-Webhook-Id", d.WebhookId);
                    using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
                    cts.CancelAfter(TimeSpan.FromSeconds(options.TimeoutSeconds));
                    using var res = await httpFactory.CreateClient("webhooks").SendAsync(req, cts.Token);
                    status = (int)res.StatusCode;
                    if (!res.IsSuccessStatusCode) error = $"HTTP {status}";
                }
                catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or CryptographicException)
                {
                    error = ex.GetType().Name + ": " + ex.Message;
                }
            }

            var attempts = d.Attempts + 1;
            var delivered = error is null;
            var dead = !delivered && (DateTime.UtcNow - d.CreatedAt).TotalHours >= options.MaxAttemptHours;
            var next = DateTime.UtcNow.AddSeconds(BackoffSeconds[Math.Min(attempts - 1, BackoffSeconds.Length - 1)]);
            await db.InTxAsync(DbRole.Ops, null, u => u.ExecuteAsync(
                """
                update txn.webhook_delivery set attempts = @attempts, last_status_code = @status, last_error = @error,
                    status = case when @delivered then 'DELIVERED' when @dead then 'DEAD' else 'PENDING' end,
                    delivered_at = case when @delivered then @now else null end, next_attempt_at = @next
                where delivery_id = @id
                """, new { attempts, status, error, delivered, dead, now = DateTime.UtcNow, next, id = d.DeliveryId }, ct), ct);
            if (!delivered) log.LogWarning("Webhook {Id} attempt {Attempt} failed: {Error}", d.WebhookId, attempts, error);
        }
        return due.Count;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try { await DispatchDueAsync(stoppingToken); }
            catch (OperationCanceledException) { break; }
            catch (Exception ex) { log.LogWarning(ex, "Webhook dispatch iteration failed"); }
            try { await Task.Delay(1000, stoppingToken); } catch (OperationCanceledException) { break; }
        }
    }
}

public sealed record IdempotentResponse(int Status, string Body);

/// <summary>Idempotency-Key handling (HLD 8.2): same key + same payload replays the stored response; a different payload is 409.</summary>
public sealed class IdempotencyService(Db db, IClock clock)
{
    private sealed class KeyRow { public string Method { get; set; } = ""; public string Path { get; set; } = ""; public byte[] RequestHash { get; set; } = []; public int? StatusCode { get; set; } public string? ResponseBody { get; set; } }

    public async Task<IdempotentResponse> ExecuteAsync(Guid partnerId, string key, string method, string path, byte[] body,
        Func<Task<IdempotentResponse>> handler, CancellationToken ct)
    {
        if (key.Length is 0 or > 255) throw HsException.Invalid("Idempotency-Key must be 1-255 characters");
        var hash = SHA256.HashData(body);
        var now = clock.UtcNow;
        var existing = await db.InTxAsync(DbRole.App, partnerId, async u =>
        {
            await u.ExecuteAsync("delete from txn.idempotency_key where partner_id = @partnerId and key = @key and expires_at < @now", new { partnerId, key, now }, ct);
            var inserted = await u.ExecuteAsync(
                """
                insert into txn.idempotency_key (partner_id, key, method, path, request_hash, expires_at) values (@partnerId, @key, @method, @path, @hash, @exp)
                on conflict (partner_id, key) do nothing
                """, new { partnerId, key, method, path, hash, exp = now.AddHours(24) }, ct);
            return inserted == 1 ? null : await u.QuerySingleAsync<KeyRow>(
                "select method, path, request_hash, status_code, response_body from txn.idempotency_key where partner_id = @partnerId and key = @key", new { partnerId, key }, ct);
        }, ct);

        if (existing is not null)
        {
            if (existing.Method != method || existing.Path != path || !existing.RequestHash.AsSpan().SequenceEqual(hash))
                throw HsException.Conflict("HS-IDEMPOTENCY-CONFLICT", "This Idempotency-Key was used with a different request");
            if (existing.StatusCode is null) throw HsException.Conflict("HS-IDEMPOTENCY-CONFLICT", "A request with this Idempotency-Key is still being processed");
            return new IdempotentResponse(existing.StatusCode.Value, existing.ResponseBody ?? "");
        }

        IdempotentResponse response;
        try { response = await handler(); }
        catch (HsException ex) when (ex.Status < 500)
        {
            var problem = JsonSerializer.Serialize(new Problem { Type = "https://healthswap.example/problems/" + ex.Code.ToLowerInvariant(), Title = ex.Title, Status = ex.Status, Detail = ex.Detail, Code = ex.Code, Errors = ex.Errors }, HsJson.Options);
            await StoreAsync(partnerId, key, ex.Status, problem, ct);
            throw;
        }
        catch
        {
            await db.InTxAsync(DbRole.App, partnerId, u => u.ExecuteAsync("delete from txn.idempotency_key where partner_id = @partnerId and key = @key", new { partnerId, key }, ct), ct);
            throw;
        }
        await StoreAsync(partnerId, key, response.Status, response.Body, ct);
        return response;
    }

    private Task StoreAsync(Guid partnerId, string key, int status, string body, CancellationToken ct) =>
        db.InTxAsync(DbRole.App, partnerId, u => u.ExecuteAsync("update txn.idempotency_key set status_code = @status, response_body = @body where partner_id = @partnerId and key = @key",
            new { partnerId, key, status, body }, ct), ct);
}

/// <summary>Double-entry ledger writer (HLD 9.6). Callers run inside a unit of work with the hs_ledger_writer role.</summary>
public sealed class LedgerService(IClock clock)
{
    public async Task<bool> PostAsync(UnitOfWork u, Guid partnerId, string currency, LedgerPosting posting, string createdBy, CancellationToken ct, DateTime? effectiveAt = null)
    {
        if (u.Role != DbRole.LedgerWriter) throw new InvalidOperationException("Ledger postings require the hs_ledger_writer role");
        var exists = await u.ScalarAsync<bool>("select exists (select 1 from billing.journal_entry where idempotency_key = @key)", new { key = posting.IdempotencyKey }, ct);
        if (exists) return false;
        var entryId = Guid.CreateVersion7();
        await u.ExecuteAsync(
            """
            insert into billing.journal_entry (entry_id, partner_id, entry_kind, reference_type, reference_id, effective_at, created_by, idempotency_key)
            values (@entryId, @partnerId, @kind, @refType, @refId, @at, @by, @key)
            """,
            new { entryId, partnerId, kind = posting.Kind, refType = posting.ReferenceType, refId = posting.ReferenceId, at = effectiveAt ?? clock.UtcNow, by = createdBy, key = posting.IdempotencyKey }, ct);
        short lineNo = 0;
        foreach (var line in posting.Lines)
        {
            var account = await AccountAsync(u, partnerId, line.Account, currency, ct);
            await u.ExecuteAsync("insert into billing.journal_line (entry_id, line_no, partner_id, account_id, debit, credit) values (@entryId, @lineNo, @partnerId, @account, @debit, @credit)",
                new { entryId, lineNo = ++lineNo, partnerId, account, debit = line.Debit, credit = line.Credit }, ct);
        }
        return true;
    }

    private static async Task<Guid> AccountAsync(UnitOfWork u, Guid partnerId, string code, string currency, CancellationToken ct)
    {
        await u.ExecuteAsync("insert into billing.account (partner_id, code, currency) values (@partnerId, @code, @currency) on conflict (partner_id, code, currency) do nothing",
            new { partnerId, code, currency }, ct);
        return await u.QuerySingleAsync<Guid>("select account_id from billing.account where partner_id = @partnerId and code = @code and currency = @currency",
            new { partnerId, code, currency }, ct);
    }
}
