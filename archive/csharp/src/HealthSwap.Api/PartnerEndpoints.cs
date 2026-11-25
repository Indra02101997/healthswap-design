using HealthSwap.Contracts;
using HealthSwap.Platform;
using HealthSwap.Platform.Services;
using Microsoft.AspNetCore.Mvc;

namespace HealthSwap.Api;

/// <summary>Background work queue for asynchronous partner work (catalog enrichment, feed processing).</summary>
public sealed class BackgroundJobs(ILogger<BackgroundJobs> log) : BackgroundService
{
    private readonly System.Threading.Channels.Channel<(string Name, Func<CancellationToken, Task> Work)> _queue =
        System.Threading.Channels.Channel.CreateUnbounded<(string, Func<CancellationToken, Task>)>();
    private int _pending;

    public int Pending => Volatile.Read(ref _pending);

    public void Enqueue(string name, Func<CancellationToken, Task> work)
    {
        Interlocked.Increment(ref _pending);
        _queue.Writer.TryWrite((name, work));
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await foreach (var (name, work) in _queue.Reader.ReadAllAsync(stoppingToken))
        {
            try { await work(stoppingToken); }
            catch (Exception ex) when (ex is not OperationCanceledException) { log.LogError(ex, "Background job {Job} failed", name); }
            finally { Interlocked.Decrement(ref _pending); }
        }
    }
}

/// <summary>Partner API (api/openapi.yaml). Every route carries its operationId so the contract validator can find the schema.</summary>
public static class PartnerEndpoints
{
    private static int Limit(int? limit, int max = 500) => Math.Clamp(limit ?? 100, 1, max);

    public static void Map(WebApplication app)
    {
        var v1 = app.MapGroup("/v1");

        v1.MapPost("/sdk/sessions", (HttpContext http, [FromBody] SdkSessionRequest req, DevIdentityProvider idp, PartnerDirectory partners, IdempotencyService idem) =>
            Http.Idempotent(http, idem, false, async () =>
            {
                var ctx = http.Partner();
                if (ctx.IsSdkSession) throw HsException.Forbidden("SDK sessions cannot mint other sessions");
                if (req.UserRef is not null) PreferenceService.ValidateUserRef(req.UserRef);
                var partner = await partners.GetAsync(ctx.PartnerId, http.RequestAborted);
                if (!partner.Markets.Contains(req.Market)) throw HsException.Unprocessable("HS-UNSUPPORTED-MARKET", $"Market {req.Market} is not enabled for this partner");
                return (201, idp.IssueSdkSession(ctx, req));
            })).WithName("createSdkSession").RequireAuthorization("scope:sdk.sessions.write");

        // ---------- Catalog ----------
        v1.MapPost("/catalog/item-batches", (HttpContext http, [FromBody] CatalogBatchRequest req, CatalogService catalog, CatalogEnrichment enrichment,
                BackgroundJobs jobs, IdempotencyService idem) =>
            Http.Idempotent(http, idem, false, async () =>
            {
                var ctx = http.Partner();
                var outcome = await catalog.UpsertAsync(ctx.PartnerId, req.Items, null, http.RequestAborted);
                if (outcome.NeedsEnrichment.Count > 0)
                    jobs.Enqueue("catalog-enrichment", ct => enrichment.EnrichAsync(ctx.PartnerId, outcome.NeedsEnrichment, ct));
                return (200, outcome.Result);
            })).WithName("upsertCatalogItems").RequireAuthorization("scope:catalog.write");

        v1.MapPost("/catalog/feeds", (HttpContext http, [FromBody] FeedCreateRequest req, FeedService feeds, IdempotencyService idem) =>
            Http.Idempotent(http, idem, false, async () =>
            {
                var ctx = http.Partner();
                var baseUrl = Http.BaseUrl(http.Request);
                var feed = await feeds.CreateAsync(ctx.PartnerId, req, (id, token) => $"{baseUrl}/v1/catalog/feeds/{id}/upload?token={Uri.EscapeDataString(token)}", http.RequestAborted);
                return (201, feed);
            })).WithName("createCatalogFeed").RequireAuthorization("scope:catalog.write");

        // Pre-signed upload target (stands in for object storage). Authorized by the signed token, not by OAuth.
        v1.MapPut("/catalog/feeds/{feedId:guid}/upload", async (HttpContext http, Guid feedId, [FromQuery] string token, FeedService feeds, BackgroundJobs jobs) =>
        {
            var (partnerId, id) = await feeds.AcceptUploadAsync(feedId, token, http.Request.Body, http.RequestAborted);
            jobs.Enqueue("feed-processing", ct => feeds.ProcessAsync(partnerId, id, ct));
            return Results.Accepted();
        }).WithName("uploadCatalogFeed").AllowAnonymous().DisableAntiforgery();

        v1.MapGet("/catalog/feeds/{feedId:guid}", async (HttpContext http, Guid feedId, FeedService feeds) =>
        {
            var baseUrl = Http.BaseUrl(http.Request);
            return Http.Json(await feeds.GetAsync(http.Partner().PartnerId, feedId, id => $"{baseUrl}/v1/catalog/feeds/{id}/errors", http.RequestAborted));
        }).WithName("getCatalogFeed").RequireAuthorization("scope:catalog.read");

        v1.MapGet("/catalog/feeds/{feedId:guid}/errors", async (HttpContext http, Guid feedId, FeedService feeds) =>
        {
            await feeds.GetAsync(http.Partner().PartnerId, feedId, null, http.RequestAborted); // tenant check
            return feeds.ErrorReportPath(feedId) is { } path ? Results.File(path, "text/plain") : Results.NotFound();
        }).WithName("getCatalogFeedErrors").RequireAuthorization("scope:catalog.read");

        v1.MapGet("/catalog/items/{partnerSku}", async (HttpContext http, string partnerSku, CatalogService catalog) =>
            Http.Json(await catalog.GetAsync(http.Partner().PartnerId, partnerSku, http.RequestAborted)))
            .WithName("getCatalogItem").RequireAuthorization("scope:catalog.read");

        v1.MapDelete("/catalog/items/{partnerSku}", async (HttpContext http, string partnerSku, CatalogService catalog) =>
        {
            await catalog.DelistAsync(http.Partner().PartnerId, partnerSku, http.RequestAborted);
            return Results.NoContent();
        }).WithName("delistCatalogItem").RequireAuthorization("scope:catalog.write");

        v1.MapPut("/catalog/items/{partnerSku}/match", async (HttpContext http, string partnerSku, [FromBody] MatchOverrideRequest req, CatalogService catalog) =>
            Http.Json(await catalog.OverrideMatchAsync(http.Partner().PartnerId, partnerSku, req, http.RequestAborted)))
            .WithName("overrideCatalogMatch").RequireAuthorization("scope:catalog.write");

        // ---------- Inventory ----------
        v1.MapPost("/stores/{storeId}/inventory-batches", (HttpContext http, string storeId, [FromBody] InventoryBatchRequest req, InventoryService inventory, IdempotencyService idem) =>
            Http.Idempotent(http, idem, false, async () => (202, (object)await inventory.UpsertAsync(http.Partner().PartnerId, storeId, req, http.RequestAborted))))
            .WithName("upsertStoreInventory").RequireAuthorization("scope:inventory.write");

        // ---------- Products ----------
        v1.MapGet("/products/lookup", async (HttpContext http, [FromQuery] string market, [FromQuery] string? gtin, [FromQuery] string? partnerSku, [FromQuery] string? locale,
                ProductService products) =>
            Http.Json(await products.LookupAsync(http.Partner(), market, gtin, partnerSku, locale, http.RequestAborted)))
            .WithName("lookupProduct").RequireAuthorization("scope:products.read");

        v1.MapGet("/products/{productId:guid}", async (HttpContext http, Guid productId, [FromQuery] string market, [FromQuery] string? locale, ProductService products) =>
            Http.Json(await products.GetAsync(http.Partner(), productId, market, locale, http.RequestAborted)))
            .WithName("getProductHealthProfile").RequireAuthorization("scope:products.read");

        v1.MapGet("/products/{productId:guid}/alternatives", async (HttpContext http, Guid productId, [FromQuery] string market, [FromQuery] string? storeId,
                [FromQuery] string? cartRef, [FromQuery] string? userRef, [FromQuery] string? placement, [FromQuery] string? currency, [FromQuery] string? locale,
                [FromQuery] int? limit, EvaluationService evaluations) =>
        {
            if (placement is not (null or "PDP" or "SEARCH")) throw HsException.Invalid("placement must be PDP or SEARCH");
            if (userRef is not null) PreferenceService.ValidateUserRef(userRef);
            return Http.Json(await evaluations.AlternativesAsync(http.Partner(), productId, market, storeId, cartRef, userRef, placement, currency, locale, Math.Clamp(limit ?? 5, 1, 10), http.RequestAborted));
        }).WithName("listProductAlternatives").RequireAuthorization("scope:products.read");

        // ---------- Cart evaluations ----------
        v1.MapPost("/cart-evaluations", (HttpContext http, [FromBody] CartEvaluationRequest req, EvaluationService evaluations, IdempotencyService idem) =>
            Http.Idempotent(http, idem, false, async () =>
            {
                if (req.UserRef is not null) PreferenceService.ValidateUserRef(req.UserRef);
                return (200, (object)await evaluations.EvaluateAsync(http.Partner(), req, http.RequestAborted));
            })).WithName("evaluateCart").RequireAuthorization("scope:carts.evaluate");

        v1.MapGet("/cart-evaluations/{evaluationId:guid}", async (HttpContext http, Guid evaluationId, EvaluationService evaluations) =>
        {
            var ctx = http.Partner();
            var evaluation = await evaluations.GetAsync(ctx.PartnerId, evaluationId);
            if (ctx.IsSdkSession && evaluation.CartRef != ctx.SdkCartRef) throw HsException.NotFound("Cart evaluation " + evaluationId);
            return Http.Json(evaluation);
        }).WithName("getCartEvaluation").RequireAuthorization("scope:carts.evaluate");

        // ---------- Events ----------
        v1.MapPost("/events", async (HttpContext http, [FromBody] EventBatchRequest req, EventService events) =>
            Http.Json(await events.IngestAsync(http.Partner(), req, http.RequestAborted), 202))
            .WithName("reportSuggestionEvents").RequireAuthorization("scope:events.write");

        // ---------- Orders ----------
        v1.MapPost("/orders", (HttpContext http, [FromBody] OrderCreateRequest req, OrderService orders, IdempotencyService idem) =>
            Http.Idempotent(http, idem, true, async () =>
            {
                if (req.UserRef is not null) PreferenceService.ValidateUserRef(req.UserRef);
                return (201, (object)await orders.CreateAsync(http.Partner(), req, http.RequestAborted));
            })).WithName("createOrder").RequireAuthorization("scope:orders.write");

        v1.MapGet("/orders/{orderId}", async (HttpContext http, string orderId, OrderService orders) =>
            Http.Json(await orders.GetAsync(http.Partner().PartnerId, orderId, http.RequestAborted)))
            .WithName("getOrder").RequireAuthorization("scope:orders.read");

        v1.MapPatch("/orders/{orderId}", async (HttpContext http, string orderId, [FromBody] OrderStatusUpdate req, OrderService orders) =>
            Http.Json(await orders.UpdateStatusAsync(http.Partner().PartnerId, orderId, req, http.RequestAborted)))
            .WithName("updateOrderStatus").RequireAuthorization("scope:orders.write");

        v1.MapPost("/orders/{orderId}/adjustments", (HttpContext http, string orderId, [FromBody] OrderAdjustmentRequest req, OrderService orders, IdempotencyService idem) =>
            Http.Idempotent(http, idem, false, async () => (202, (object)await orders.AdjustAsync(http.Partner().PartnerId, orderId, req, http.RequestAborted))))
            .WithName("createOrderAdjustments").RequireAuthorization("scope:orders.write");

        v1.MapGet("/orders/{orderId}/attribution", async (HttpContext http, string orderId, OrderService orders) =>
            Http.Json(await orders.AttributionAsync(http.Partner().PartnerId, orderId, http.RequestAborted)))
            .WithName("getOrderAttribution").RequireAuthorization("scope:orders.read");

        // ---------- Shoppers ----------
        v1.MapGet("/users/{userRef}/preferences", async (HttpContext http, string userRef, PreferenceService prefs) =>
            Http.Json(await prefs.GetAsync(http.Partner().PartnerId, userRef, http.RequestAborted)))
            .WithName("getShopperPreferences").RequireAuthorization("scope:users.read");

        v1.MapPut("/users/{userRef}/preferences", async (HttpContext http, string userRef, [FromBody] UserPreferences req, PreferenceService prefs) =>
            Http.Json(await prefs.PutAsync(http.Partner().PartnerId, userRef, req, http.RequestAborted)))
            .WithName("putShopperPreferences").RequireAuthorization("scope:users.write");

        v1.MapDelete("/users/{userRef}", async (HttpContext http, string userRef, PrivacyService privacy) =>
            Http.Json(await privacy.EraseAsync(http.Partner().PartnerId, userRef, http.RequestAborted), 202))
            .WithName("eraseShopper").RequireAuthorization("scope:users.write");

        // ---------- Billing ----------
        v1.MapGet("/billing/statements", async (HttpContext http, [FromQuery] string? period, [FromQuery] string? status, [FromQuery] string? cursor, [FromQuery] int? limit,
                BillingService billing) =>
            Http.Json(await billing.ListAsync(http.Partner().PartnerId, period, status, cursor, Limit(limit), http.RequestAborted)))
            .WithName("listStatements").RequireAuthorization("scope:billing.read");

        v1.MapGet("/billing/statements/{statementId:guid}", async (HttpContext http, Guid statementId, BillingService billing) =>
            Http.Json(await billing.GetAsync(http.Partner().PartnerId, statementId, http.RequestAborted)))
            .WithName("getStatement").RequireAuthorization("scope:billing.read");

        v1.MapGet("/billing/statements/{statementId:guid}/lines", async (HttpContext http, Guid statementId, [FromQuery] string? cursor, [FromQuery] int? limit, BillingService billing) =>
            Http.Json(await billing.LinesAsync(http.Partner().PartnerId, statementId, cursor, Limit(limit), http.RequestAborted)))
            .WithName("listStatementLines").RequireAuthorization("scope:billing.read");

        v1.MapGet("/billing/statements/{statementId:guid}/recommendation-charges", async (HttpContext http, Guid statementId, [FromQuery] string? cursor, [FromQuery] int? limit,
                RecoChargeService charges) =>
            Http.Json(await charges.ListAsync(http.Partner().PartnerId, null, null, null, null, statementId, cursor, Limit(limit, 200), http.RequestAborted)))
            .WithName("listStatementRecommendationCharges").RequireAuthorization("scope:billing.read");

        v1.MapGet("/billing/recommendation-charges", async (HttpContext http, [FromQuery] string? period, [FromQuery] string? state, [FromQuery] string? cartRef,
                [FromQuery] Guid? suggestionId, [FromQuery] string? cursor, [FromQuery] int? limit, RecoChargeService charges) =>
        {
            if (state is not (null or "PENDING" or "FINAL" or "VOID" or "REJECTED" or "CREDITED")) throw HsException.Invalid("Unknown state " + state);
            return Http.Json(await charges.ListAsync(http.Partner().PartnerId, period, state, cartRef, suggestionId, null, cursor, Limit(limit, 200), http.RequestAborted));
        }).WithName("listRecommendationCharges").RequireAuthorization("scope:billing.read");

        v1.MapGet("/billing/recommendation-charges/{chargeId:guid}", async (HttpContext http, Guid chargeId, RecoChargeService charges) =>
            Http.Json(await charges.GetAsync(http.Partner().PartnerId, chargeId, http.RequestAborted)))
            .WithName("getRecommendationCharge").RequireAuthorization("scope:billing.read");

        v1.MapPost("/billing/disputes", (HttpContext http, [FromBody] DisputeRequest req, BillingService billing, IdempotencyService idem) =>
            Http.Idempotent(http, idem, false, async () => (201, (object)await billing.OpenDisputeAsync(http.Partner().PartnerId, req, http.RequestAborted))))
            .WithName("createDispute").RequireAuthorization("scope:billing.write");

        v1.MapGet("/billing/disputes/{disputeId:guid}", async (HttpContext http, Guid disputeId, BillingService billing) =>
            Http.Json(await billing.GetDisputeAsync(http.Partner().PartnerId, disputeId, http.RequestAborted)))
            .WithName("getDispute").RequireAuthorization("scope:billing.read");

        // ---------- Transparency (public) ----------
        v1.MapGet("/scoring-models", async (HttpContext http, [FromQuery] string? market, TransparencyService transparency) =>
        {
            if (market is not null && (market.Length != 2 || !market.All(char.IsAsciiLetterUpper))) throw HsException.Invalid("market must be an ISO 3166-1 alpha-2 code");
            return Http.Json(await transparency.ModelsAsync(market, http.RequestAborted));
        }).WithName("listScoringModels").AllowAnonymous();

        v1.MapGet("/status", async (HttpContext http, TransparencyService transparency) => Http.Json(await transparency.StatusAsync(http.RequestAborted)))
            .WithName("getServiceStatus").AllowAnonymous();
    }
}
