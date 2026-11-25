using System.Collections.Concurrent;
using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Domain.Commerce;
using HealthSwap.Platform;
using HealthSwap.Platform.Bus;
using HealthSwap.Platform.Data;
using HealthSwap.Platform.Knowledge;
using HealthSwap.Platform.Ml;
using HealthSwap.Platform.Services;
using Microsoft.AspNetCore.Mvc;

namespace HealthSwap.Api;

public sealed record KillSwitchRequest(bool Enabled, List<string>? DisabledPlacements = null);
public sealed record ClockRequest(double? OffsetSeconds, double? AdvanceSeconds);
public sealed record PaymentRequest(string Amount, string PaymentRef);

/// <summary>Private admin plane (scope hs.admin): tenant config, kill switches, jobs, time travel for testing, ledger and curation views.</summary>
public static class AdminEndpoints
{
    public static void Map(WebApplication app)
    {
        var admin = app.MapGroup("/admin").RequireAuthorization("admin");
        string Actor(HttpContext http) => "admin:" + (http.User.FindFirst("client_id")?.Value ?? "unknown");

        admin.MapGet("/partners", async (PartnerDirectory partners, CancellationToken ct) =>
            Http.Json((await partners.ListAllAsync(ct)).Select(p => new { p.PartnerId, p.DisplayName, p.Geography, p.Markets, p.Status, config = JsonDocument.Parse(p.ConfigJson).RootElement })));

        admin.MapPut("/partners/{partnerId:guid}/config", async (HttpContext http, Guid partnerId, [FromBody] JsonElement body, Db db, PartnerDirectory partners, CancellationToken ct) =>
        {
            var cfg = body.Deserialize<PartnerConfig>(PartnerDirectory.ConfigJson) ?? throw HsException.Invalid("Empty config");
            var problems = cfg.Validate();
            if (problems.Count > 0) throw HsException.Invalid("Invalid partner configuration", problems.Select(p => new FieldError("/", p)).ToList());
            await UpdateConfigAsync(db, partnerId, body.GetRawText(), Actor(http), "CONFIG_REPLACED", ct);
            partners.Invalidate(partnerId);
            return Http.Json(await partners.GetAsync(partnerId, ct) is var p ? new { p.PartnerId, config = JsonDocument.Parse(p.ConfigJson).RootElement } : null!);
        });

        admin.MapPost("/partners/{partnerId:guid}/kill-switch", async (HttpContext http, Guid partnerId, [FromBody] KillSwitchRequest req, Db db, PartnerDirectory partners, CancellationToken ct) =>
        {
            var current = await partners.GetAsync(partnerId, ct);
            var node = System.Text.Json.Nodes.JsonNode.Parse(current.ConfigJson)!.AsObject();
            node["killSwitch"] = req.Enabled;
            if (req.DisabledPlacements is not null) node["disabledPlacements"] = new System.Text.Json.Nodes.JsonArray(req.DisabledPlacements.Select(p => (System.Text.Json.Nodes.JsonNode)p).ToArray());
            await UpdateConfigAsync(db, partnerId, node.ToJsonString(), Actor(http), req.Enabled ? "KILL_SWITCH_ON" : "KILL_SWITCH_OFF", ct);
            partners.Invalidate(partnerId);
            return Http.Json(new { partnerId, killSwitch = req.Enabled, req.DisabledPlacements, effectiveWithinSeconds = PartnerDirectory.Ttl.TotalSeconds });
        });

        admin.MapPost("/jobs/{job}", async (string job, [FromQuery] string? period, [FromQuery] Guid? partnerId, RecoChargeService reco, AttributionEngine attribution,
            BillingService billing, WebhookDispatcher webhooks, ScoringJob scoring, SwapGraphBuilder graph, ExplanationPregen explanations, CacheWarmer warmer,
            EvaluationRecorder recorder, OutboxRelay relay, OffsetClock clock, CancellationToken ct) =>
        {
            await recorder.FlushAsync(TimeSpan.FromSeconds(10));
            object result = job switch
            {
                "finalize-reco-charges" => await reco.FinalizeDueAsync(ct) is var (f, r) ? new { final = f, rejected = r } : null!,
                "finalize-attributions" => new { final = await attribution.FinalizeDueAsync(ct) },
                "month-close" => await billing.CloseMonthAsync(period ?? Period.Of(clock.UtcNow), partnerId, ct),
                "dispatch-webhooks" => new { relayed = await relay.PumpAsync(ct), dispatched = await webhooks.DispatchDueAsync(ct) },
                "kb-rebuild" => new { scoring = await scoring.RunAllAsync(ct), graphs = await graph.BuildAllAsync(ct), explanations = await explanations.RunAllAsync(ct), cache = await warmer.WarmAllAsync(ct) },
                "warm-cache" => await warmer.WarmAllAsync(ct),
                _ => throw HsException.NotFound("Job " + job),
            };
            return Http.Json(new { job, result });
        });

        admin.MapGet("/clock", (OffsetClock clock) => Http.Json(new { utcNow = clock.UtcNow, offsetSeconds = clock.Offset.TotalSeconds }));
        admin.MapPost("/clock", (HttpContext http, [FromBody] ClockRequest req, OffsetClock clock, IHostEnvironment env, ILoggerFactory logs) =>
        {
            if (!env.IsDevelopment()) throw HsException.Forbidden("Time travel is available in Development only");
            if (req.OffsetSeconds is { } o) clock.Offset = TimeSpan.FromSeconds(o);
            if (req.AdvanceSeconds is { } a) clock.Offset += TimeSpan.FromSeconds(a);
            logs.CreateLogger("Admin").LogWarning("{Actor} set the clock offset to {Offset}", Actor(http), clock.Offset);
            return Http.Json(new { utcNow = clock.UtcNow, offsetSeconds = clock.Offset.TotalSeconds });
        });

        admin.MapGet("/ledger/trial-balance", async ([FromQuery] Guid? partnerId, BillingService billing, CancellationToken ct) =>
        {
            var rows = await billing.TrialBalanceAsync(partnerId, ct);
            return Http.Json(new { rows, balanced = rows.GroupBy(r => r.Currency).All(g => g.Sum(r => r.Balance) == 0) });
        });

        admin.MapGet("/curation-tasks", async ([FromQuery] string? status, Db db, CancellationToken ct) =>
            Http.Json(await db.InTxAsync(DbRole.Ops, null, u => u.QueryAsync<CurationTask>(
                "select task_id, kind, priority, status, product_id, rule_code, payload::text as payload, created_at from kb.curation_task where (@status::text is null or status = @status) order by priority, created_at limit 500",
                new { status }, ct), ct)));

        admin.MapPost("/disputes/{disputeId:guid}/resolve", async (HttpContext http, Guid disputeId, [FromBody] DisputeResolution req, BillingService billing, CancellationToken ct) =>
            Http.Json(await billing.ResolveDisputeAsync(disputeId, req, Actor(http), ct)));

        admin.MapPost("/statements/{statementId:guid}/payments", async (HttpContext http, Guid statementId, [FromBody] PaymentRequest req, BillingService billing, CancellationToken ct) =>
        {
            if (!Currency.TryParse(req.Amount, out var amount)) throw HsException.Invalid("amount must be a decimal string");
            return Http.Json(await billing.RecordPaymentAsync(statementId, amount, req.PaymentRef, Actor(http), ct));
        });

        admin.MapGet("/webhook-deliveries", async ([FromQuery] Guid? partnerId, Db db, CancellationToken ct) =>
            Http.Json(await db.InTxAsync(DbRole.Ops, null, u => u.QueryAsync<WebhookDeliveryRow>(
                "select delivery_id, webhook_id, partner_id, event_type, status, attempts, last_status_code, last_error, created_at, delivered_at from txn.webhook_delivery where (@partnerId::uuid is null or partner_id = @partnerId) order by created_at desc limit 200",
                new { partnerId }, ct), ct)));

        admin.MapGet("/llm-audit", async ([FromQuery] string? task, [FromQuery] int? limit, Db db, CancellationToken ct) =>
            Http.Json(await db.InTxAsync(DbRole.Ops, null, async u => new
            {
                summary = await u.QueryAsync<LlmAuditSummary>(
                    "select task, prompt_version, model_ref, count(*)::int as calls, count(*) filter (where accepted)::int as accepted, coalesce(sum(tokens_in), 0)::int as tokens_in, coalesce(sum(tokens_out), 0)::int as tokens_out from rec.llm_audit group by 1, 2, 3 order by 1",
                    ct: ct),
                recent = await u.QueryAsync<LlmAuditRow>(
                    "select task, prompt_version, model_ref, accepted, validation::text as validation, output::text as output, latency_ms, created_at from rec.llm_audit where (@task::text is null or task = @task) order by created_at desc limit @limit",
                    new { task, limit = Math.Clamp(limit ?? 20, 1, 200) }, ct),
            }, ct)));

        admin.MapPost("/models/reload", (ModelRegistry models) =>
        {
            models.Reload();
            return Http.Json(new { ranker = models.RankerVersion, substitutability = models.SubstitutabilityVersion, entityResolution = models.EntityResolutionVersion });
        });

        admin.MapGet("/diagnostics/swaps", async ([FromQuery] string market, [FromQuery] Guid productId, SwapGraphBuilder graph, CancellationToken ct) =>
            Http.Json(await graph.DiagnoseAsync(market, productId, ct)));
    }

    private static Task UpdateConfigAsync(Db db, Guid partnerId, string json, string actor, string action, CancellationToken ct) =>
        db.InTxAsync(DbRole.Ops, null, async u =>
        {
            var updated = await u.ExecuteAsync("update tenant.partner set config = @json::jsonb where partner_id = @partnerId", new { json, partnerId }, ct);
            if (updated == 0) throw HsException.NotFound("Partner " + partnerId);
            await u.ExecuteAsync("insert into audit.change_log (actor, action, entity_type, entity_id, details) values (@actor, @action, 'PARTNER_CONFIG', @id, @details::jsonb)",
                new { actor, action, id = partnerId.ToString(), details = json }, ct);
        }, ct);

    private sealed class CurationTask { public Guid TaskId { get; set; } public string Kind { get; set; } = ""; public string Priority { get; set; } = ""; public string Status { get; set; } = ""; public Guid? ProductId { get; set; } public string? RuleCode { get; set; } public string Payload { get; set; } = "{}"; public DateTime CreatedAt { get; set; } }
    private sealed class WebhookDeliveryRow { public Guid DeliveryId { get; set; } public string WebhookId { get; set; } = ""; public Guid PartnerId { get; set; } public string EventType { get; set; } = ""; public string Status { get; set; } = ""; public int Attempts { get; set; } public int? LastStatusCode { get; set; } public string? LastError { get; set; } public DateTime CreatedAt { get; set; } public DateTime? DeliveredAt { get; set; } }
    private sealed class LlmAuditSummary { public string Task { get; set; } = ""; public string PromptVersion { get; set; } = ""; public string ModelRef { get; set; } = ""; public int Calls { get; set; } public int Accepted { get; set; } public int TokensIn { get; set; } public int TokensOut { get; set; } }
    private sealed class LlmAuditRow { public string Task { get; set; } = ""; public string PromptVersion { get; set; } = ""; public string ModelRef { get; set; } = ""; public bool Accepted { get; set; } public string Validation { get; set; } = "{}"; public string? Output { get; set; } public int? LatencyMs { get; set; } public DateTime CreatedAt { get; set; } }
}

/// <summary>Local demo (Development only): a shopper UI built on the web SDK, an SDK-session backend and a webhook receiver.</summary>
public static class DemoEndpoints
{
    private sealed record DemoSessionRequest(string CartRef, string? UserRef, string? StoreId, string Market);
    private sealed record WebhookSecretFile(List<WebhookPartner> Partners);
    private sealed record WebhookPartner(Guid PartnerId, string DisplayName, WebhookConfig? Webhook);
    private sealed record WebhookConfig(string Url, string Secret);
    public sealed record ReceivedWebhook(DateTime ReceivedAt, bool SignatureValid, string? Type, JsonElement Body);

    private static readonly ConcurrentDictionary<string, ConcurrentQueue<ReceivedWebhook>> Received = new();

    public static void Map(WebApplication app, string repoRoot)
    {
        var file = Path.Combine(repoRoot, "data", "seed", "demo", "partners.json");
        var demo = File.Exists(file) ? JsonSerializer.Deserialize<WebhookSecretFile>(File.ReadAllText(file), HsJson.Options)! : new WebhookSecretFile([]);
        string? Secret(string name) => demo.Partners.FirstOrDefault(p => p.Webhook?.Url.EndsWith("/" + name, StringComparison.OrdinalIgnoreCase) == true)?.Webhook?.Secret;
        var quickBasket = demo.Partners.FirstOrDefault();

        app.MapGet("/demo", () => Results.Redirect("/demo/index.html"));

        // Stands in for the partner backend: mints an SDK session for the first demo partner (never expose this in production).
        app.MapPost("/demo/api/session", ([FromBody] DemoSessionRequest req, DevIdentityProvider idp) =>
        {
            if (quickBasket is null) throw HsException.NotFound("Demo partner");
            var ctx = new PartnerContext { PartnerId = quickBasket.PartnerId, ClientId = "demo-backend", Scopes = new HashSet<string>(Scopes.SdkAllowed) };
            return Http.Json(idp.IssueSdkSession(ctx, new SdkSessionRequest { CartRef = req.CartRef, UserRef = req.UserRef, StoreId = req.StoreId, Market = req.Market }), 201);
        });

        app.MapPost("/demo/webhooks/{name}", async (HttpContext http, string name) =>
        {
            using var reader = new StreamReader(http.Request.Body);
            var body = await reader.ReadToEndAsync();
            var secret = Secret(name);
            var signature = http.Request.Headers[WebhookSignature.Header].FirstOrDefault();
            var valid = secret is not null && signature is not null && WebhookSignature.Verify(secret, signature, body, DateTimeOffset.UtcNow, TimeSpan.FromMinutes(5));
            JsonElement json;
            try { json = JsonDocument.Parse(body).RootElement.Clone(); } catch (JsonException) { json = JsonDocument.Parse("{}").RootElement.Clone(); }
            var queue = Received.GetOrAdd(name, _ => new ConcurrentQueue<ReceivedWebhook>());
            queue.Enqueue(new ReceivedWebhook(DateTime.UtcNow, valid, json.TryGetProperty("type", out var t) ? t.GetString() : null, json));
            while (queue.Count > 200) queue.TryDequeue(out _);
            return valid ? Results.NoContent() : Results.Unauthorized();
        });

        app.MapGet("/demo/webhooks/{name}", (string name) => Http.Json(Received.TryGetValue(name, out var q) ? q.Reverse().ToList() : []));
    }
}
