using System.ComponentModel;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Domain.Recommendation;
using HealthSwap.Platform;
using HealthSwap.Platform.Cache;
using HealthSwap.Platform.Knowledge;
using HealthSwap.Platform.Services;
using ModelContextProtocol.Server;
using Money = HealthSwap.Contracts.Money;

namespace HealthSwap.Api;

public sealed record AgentCartLine(
    [property: Description("Partner SKU of the cart line")] string PartnerSku,
    [property: Description("Quantity in the cart")] decimal Quantity,
    [property: Description("Unit price as a decimal string, for example \"56.00\"")] string UnitPrice,
    [property: Description("Optional GTIN when the SKU may not be synced yet")] string? Gtin = null);

/// <summary>
/// MCP tools for shopping agents (HLD 7.6). Every recommendation carries the HealthSwap label; agents must show it and report
/// SHOWN before ACCEPTED / ADDED_TO_CART so RA-1 add fees are only charged for disclosed, labeled recommendations.
/// </summary>
[McpServerToolType]
public sealed class HealthSwapTools(IHttpContextAccessor http, EvaluationService evaluations, ProductService products, EventService events, HotCache cache,
    ReferenceDataRepository refs, IClock clock)
{
    private PartnerContext Partner()
    {
        var ctx = http.HttpContext?.User.ToPartnerContext() ?? throw new HsException(401, "HS-AUTH-INVALID", "Unauthorized", "A partner token is required");
        if (!ctx.Scopes.Contains("carts.evaluate") && !ctx.Scopes.Contains("products.read")) throw HsException.Forbidden("The token needs carts.evaluate or products.read");
        return ctx;
    }

    [McpServerTool(Name = "evaluate_cart", ReadOnly = false, Destructive = false, Idempotent = false, OpenWorld = false, UseStructuredContent = true)]
    [Description("Evaluates a shopping cart and returns healthier, in-stock, constraint-safe swap suggestions. Each alternative is labeled 'Recommended by HealthSwap'; show the label and the disclosure to the shopper.")]
    public async Task<object> EvaluateCart(
        [Description("ISO 3166-1 alpha-2 market, for example IN or US")] string market,
        [Description("ISO 4217 currency, for example INR")] string currency,
        [Description("Cart lines")] List<AgentCartLine> lines,
        [Description("Partner cart reference; reuse it for the whole conversation")] string cartRef,
        [Description("Store id for availability")] string? storeId = null,
        [Description("BCP 47 locale such as en-IN or hi-IN")] string? locale = null,
        CancellationToken ct = default)
    {
        var result = await evaluations.EvaluateAsync(Partner(), new CartEvaluationRequest
        {
            CartRef = cartRef, Market = market, Currency = currency, StoreId = storeId, Locale = locale, Context = new CartContext { Placement = "AGENT", Channel = "AGENT" },
            Lines = lines.Select((l, i) => new CartLine { LineId = (i + 1).ToString(), PartnerSku = l.PartnerSku, Gtin = l.Gtin, Quantity = l.Quantity, UnitPrice = new Money(l.UnitPrice, currency) }).ToList(),
        }, ct);
        return new
        {
            result.EvaluationId, result.Degraded, result.Summary, disclosure = result.Disclosures.FeeDisclosure,
            suggestions = result.Suggestions.Select(s => new
            {
                s.SuggestionId, s.LineId, original = new { s.Original.PartnerSku, s.Original.Title, s.Original.Health.Hsi, s.Original.Health.HsiGrade },
                alternatives = s.Alternatives.Select(a => new
                {
                    a.Rank, a.Product.PartnerSku, a.Product.Title, a.Product.Health.Hsi, a.HealthDelta, a.UnitPrice, label = a.Recommendation.Text,
                    a.Recommendation.Disclosure, explanation = a.Explanation.Short, a.RequiresAvailabilityCheck,
                }),
            }),
            instructions = "Show every alternative with its label. Call record_swap_decision with SHOWN when you present it, then ACCEPTED or ADDED_TO_CART when the shopper adds it.",
        };
    }

    [McpServerTool(Name = "get_product_health", ReadOnly = true, Idempotent = true, OpenWorld = false, UseStructuredContent = true)]
    [Description("Returns the health profile of a product (HSI, jurisdiction score, flags, allergens, additives) by productId, GTIN or partner SKU.")]
    public async Task<object> GetProductHealth(
        [Description("Market code, for example IN")] string market,
        [Description("Canonical product id")] Guid? productId = null,
        [Description("GTIN-8/12/13/14")] string? gtin = null,
        [Description("Partner SKU")] string? partnerSku = null,
        [Description("Locale for labels")] string? locale = null,
        CancellationToken ct = default)
    {
        var ctx = Partner();
        var p = productId is { } id ? await products.GetAsync(ctx, id, market, locale, ct) : await products.LookupAsync(ctx, market, gtin, partnerSku, locale, ct);
        return new
        {
            p.ProductId, p.DisplayName, p.Brand, p.Hsi, p.HsiGrade, primaryScore = p.PrimaryScore, p.NovaGroup, p.Flags, badges = p.Badges.Select(b => b.Label),
            allergens = p.Allergens.Select(a => $"{a.Code}:{a.Presence}"), additives = p.Additives.Select(a => $"INS {a.InsNumber} {a.Name} (tier {a.ConcernTier})"),
            p.DataSufficiency, p.Recalled,
        };
    }

    [McpServerTool(Name = "find_healthier_alternatives", ReadOnly = false, Idempotent = false, OpenWorld = false, UseStructuredContent = true)]
    [Description("Lists healthier alternatives for one product. With a cartRef a suggestionId is returned for record_swap_decision. Alternatives are labeled 'Recommended by HealthSwap'.")]
    public async Task<object> FindHealthierAlternatives(
        [Description("Canonical product id")] Guid productId,
        [Description("Market code")] string market,
        [Description("Cart reference for the conversation (enables add tracking)")] string? cartRef = null,
        [Description("Store id for availability")] string? storeId = null,
        [Description("Maximum alternatives (1-10)")] int limit = 3,
        [Description("Locale")] string? locale = null,
        CancellationToken ct = default)
    {
        var r = await evaluations.AlternativesAsync(Partner(), productId, market, storeId, cartRef, null, "AGENT", null, locale, Math.Clamp(limit, 1, 10), ct);
        return new
        {
            r.SuggestionId, r.ExpiresAt, disclosure = r.Disclosures?.FeeDisclosure,
            alternatives = r.Alternatives.Select(a => new { a.Rank, a.Product.PartnerSku, a.Product.ProductId, a.Product.Title, a.HealthDelta, a.UnitPrice, label = a.Recommendation.Text, explanation = a.Explanation.Short, a.ReasonCodes }),
        };
    }

    [McpServerTool(Name = "explain_swap", ReadOnly = true, Idempotent = true, OpenWorld = false, UseStructuredContent = true)]
    [Description("Explains, with verified facts only, why product B is a healthier alternative to product A (per 100 g/ml comparisons, additives, processing).")]
    public async Task<object> ExplainSwap(
        [Description("Original product id")] Guid fromProductId,
        [Description("Alternative product id")] Guid toProductId,
        [Description("Market code")] string market,
        [Description("Locale")] string? locale = null,
        CancellationToken ct = default)
    {
        Partner();
        var loc = ApiMapper.ResolveLocale(market, locale);
        var a = await cache.GetProfileAsync(market, fromProductId) ?? throw HsException.NotFound("Product " + fromProductId);
        var b = await cache.GetProfileAsync(market, toProductId) ?? throw HsException.NotFound("Product " + toProductId);
        var pack = await refs.GetPackAsync(market, ct);
        var sheet = FactSheets.Build(a, b, pack);
        var selected = FactSheets.Select(sheet);
        var cached = await cache.GetExplanationAsync(market, loc, fromProductId, toProductId, sheet.Hash);
        var text = cached?.Short ?? ExplanationTemplates.Render(selected.Count > 0 ? selected : sheet.Facts.Take(1).ToList(), loc).Short;
        return new
        {
            summary = text, healthDelta = (b.Hsi ?? 0) - (a.Hsi ?? 0), from = new { a.Name, a.Hsi, a.HsiGrade }, to = new { b.Name, b.Hsi, b.HsiGrade },
            facts = ApiMapper.Details(sheet.Facts), disclaimer = ShopperCopy.LabelDisclaimer(loc, b.Nutrition.IsLiquid), methodologyUrl = pack.MethodologyUrl,
        };
    }

    [McpServerTool(Name = "record_swap_decision", ReadOnly = false, Destructive = false, Idempotent = true, OpenWorld = false, UseStructuredContent = true)]
    [Description("Records what happened with a suggestion: SHOWN (label displayed), ACCEPTED (swap accepted and added), ADDED_TO_CART (recommended item added), DISMISSED or UNDONE. ACCEPTED and ADDED_TO_CART may create an RA-1 add fee for the retailer.")]
    public async Task<object> RecordSwapDecision(
        [Description("suggestionId from evaluate_cart or find_healthier_alternatives")] Guid suggestionId,
        [Description("SHOWN, ACCEPTED, ADDED_TO_CART, DISMISSED or UNDONE")] string decision,
        [Description("Partner SKU of the alternative (required for ACCEPTED and ADDED_TO_CART)")] string? alternativePartnerSku = null,
        [Description("Client-generated id for deduplication; reuse it when retrying")] Guid? eventId = null,
        CancellationToken ct = default)
    {
        var type = decision switch
        {
            "SHOWN" => "IMPRESSION", "ACCEPTED" => "ACCEPT", "ADDED_TO_CART" => "ADD_TO_CART", "DISMISSED" => "DISMISS", "UNDONE" => "UNDO",
            _ => throw HsException.Invalid("decision must be SHOWN, ACCEPTED, ADDED_TO_CART, DISMISSED or UNDONE"),
        };
        var result = await events.IngestAsync(Partner(), new EventBatchRequest([new SuggestionEvent
        {
            EventId = eventId ?? Ids.NewId(), Type = type, SuggestionId = suggestionId, AlternativePartnerSku = alternativePartnerSku, OccurredAt = clock.UtcNow,
            Metadata = new Dictionary<string, string> { ["channel"] = "AGENT" },
        }]), ct);
        return new { result.Accepted, result.Duplicates, rejected = result.Rejected, charges = result.Charges?.Select(c => new { c.ChargeId, c.State, c.Amount }) };
    }
}
