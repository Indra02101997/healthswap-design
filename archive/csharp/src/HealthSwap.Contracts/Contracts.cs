using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace HealthSwap.Contracts;

public static class HsJson
{
    /// <summary>camelCase, nulls omitted, strict numbers, UTF-8 friendly (Devanagari stays readable).</summary>
    public static readonly JsonSerializerOptions Options = Create();

    public static JsonSerializerOptions Create()
    {
        var o = new JsonSerializerOptions(JsonSerializerDefaults.Web)
        {
            DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
            Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
            NumberHandling = JsonNumberHandling.Strict,
        };
        return o;
    }
}

public sealed record Money(string Amount, string Currency);

public sealed record NetQuantityDto(decimal Value, string Unit);

public sealed record FieldError(string Field, string Message, string? Code = null);

/// <summary>RFC 9457 problem details with a stable HealthSwap error code.</summary>
public sealed record Problem
{
    public string Type { get; init; } = "about:blank";
    public string Title { get; init; } = "";
    public int Status { get; init; }
    public string? Detail { get; init; }
    public string? Instance { get; init; }
    public string Code { get; init; } = "";
    public string? TraceId { get; init; }
    public List<FieldError>? Errors { get; init; }
}

// ---------- Nutrition & health ----------

public sealed record OtherNutrient(string Code, decimal Amount, string Unit);

public sealed record NutritionPanel
{
    public string Basis { get; init; } = "PER_100G";
    public NetQuantityDto? ServingSize { get; init; }
    public decimal? EnergyKcal { get; init; }
    public decimal? ProteinG { get; init; }
    public decimal? CarbohydrateG { get; init; }
    public decimal? TotalSugarsG { get; init; }
    public decimal? AddedSugarsG { get; init; }
    public decimal? TotalFatG { get; init; }
    public decimal? SaturatedFatG { get; init; }
    public decimal? TransFatG { get; init; }
    public decimal? CholesterolMg { get; init; }
    public decimal? SodiumMg { get; init; }
    public decimal? SaltG { get; init; }
    public decimal? DietaryFibreG { get; init; }
    public List<OtherNutrient>? Other { get; init; }
}

public sealed record HealthScore
{
    public required string Model { get; init; }
    public required string Version { get; init; }
    public decimal? Value { get; init; }
    public string? Grade { get; init; }
    public Dictionary<string, object>? Components { get; init; }
}

public sealed record Badge(string Code, string Label, string Tone);

public sealed record AllergenInfo(string Code, string Presence);

public sealed record AdditiveInfo
{
    public string? InsNumber { get; init; }
    public string? ENumber { get; init; }
    public required string Name { get; init; }
    public string? FunctionalClass { get; init; }
    public required string ConcernTier { get; init; }
}

public sealed record DataAttribution(string Source, string License, string AttributionText);

public sealed record HealthProfileSummary
{
    public Guid ProductId { get; init; }
    public decimal? Hsi { get; init; }
    public string? HsiGrade { get; init; }
    public HealthScore? PrimaryScore { get; init; }
    public List<string> Flags { get; init; } = [];
    public string DataSufficiency { get; init; } = "INSUFFICIENT";
}

public sealed record CategoryRef(string Id, string Name);

public sealed record HealthProfile
{
    public Guid ProductId { get; init; }
    public required string Market { get; init; }
    public string? Gtin { get; init; }
    public string? DisplayName { get; init; }
    public string? Brand { get; init; }
    public CategoryRef? Category { get; init; }
    public NetQuantityDto? NetQuantity { get; init; }
    public decimal? Hsi { get; init; }
    public string? HsiGrade { get; init; }
    public HealthScore? PrimaryScore { get; init; }
    public List<HealthScore> Scores { get; init; } = [];
    public int? NovaGroup { get; init; }
    public List<string> Flags { get; init; } = [];
    public List<Badge> Badges { get; init; } = [];
    public List<NutritionPanel> Nutrition { get; init; } = [];
    public List<AllergenInfo> Allergens { get; init; } = [];
    public List<string> DietaryMarks { get; init; } = [];
    public List<AdditiveInfo> Additives { get; init; } = [];
    public bool? UsFdaHealthyEligible { get; init; }
    public bool Recalled { get; init; }
    public string DataSufficiency { get; init; } = "INSUFFICIENT";
    public decimal DataConfidence { get; init; }
    public List<DataAttribution> Attributions { get; init; } = [];
    public required string ProfileVersion { get; init; }
    public DateTime ComputedAt { get; init; }
}

// ---------- Catalog ----------

public sealed record CatalogImage(string Url, string Kind);

public sealed record CatalogItem
{
    public required string PartnerSku { get; init; }
    public string? Gtin { get; init; }
    public required string Title { get; init; }
    public string? Brand { get; init; }
    public List<string>? CategoryPath { get; init; }
    public NetQuantityDto? NetQuantity { get; init; }
    public int? PackCount { get; init; }
    public string? PhysicalState { get; init; }
    public List<string>? Markets { get; init; }
    public List<CatalogImage>? Images { get; init; }
    public string? IngredientsText { get; init; }
    public string? AllergenStatement { get; init; }
    public List<NutritionPanel>? Nutrition { get; init; }
    public List<string>? DietaryMarks { get; init; }
    public List<string>? FssaiLicenseNumbers { get; init; }
    public bool? IsPrivateLabel { get; init; }
    public bool? IsListed { get; init; }
    public Dictionary<string, string>? Attributes { get; init; }
}

public sealed record CatalogBatchRequest(List<CatalogItem> Items);

public sealed record MatchResult
{
    public required string State { get; init; }
    public required string Method { get; init; }
    public decimal Confidence { get; init; }
    public Guid? ProductId { get; init; }
    public string DataSufficiency { get; init; } = "INSUFFICIENT";
}

public sealed record CatalogItemResult
{
    public required string PartnerSku { get; init; }
    public required string Status { get; init; }
    public MatchResult? Match { get; init; }
    public List<FieldError>? Errors { get; init; }
}

public sealed record CatalogBatchResult(Guid BatchId, int Received, int Accepted, int Rejected, List<CatalogItemResult> Results);

public sealed record CatalogItemStatus
{
    public required string PartnerSku { get; init; }
    public bool IsListed { get; init; }
    public required MatchResult Match { get; init; }
    public HealthProfileSummary? Health { get; init; }
    public DateTime UpdatedAt { get; init; }
}

public sealed record MatchOverrideRequest(string Action, Guid? ProductId = null, string? Comment = null);

public sealed record FeedCreateRequest(string Format, string Mode, string? Compression = "GZIP", int? ExpectedItems = null);

public sealed record FeedStats
{
    public int Received { get; init; }
    public int Accepted { get; init; }
    public int Rejected { get; init; }
    public int AutoMatched { get; init; }
    public int NeedsReview { get; init; }
    public int Unmatched { get; init; }
    public int PendingEnrichment { get; init; }
}

public sealed record Feed
{
    public Guid FeedId { get; init; }
    public required string Status { get; init; }
    public string? UploadUrl { get; init; }
    public DateTime? UploadUrlExpiresAt { get; init; }
    public FeedStats? Stats { get; init; }
    public string? ErrorReportUrl { get; init; }
    public DateTime CreatedAt { get; init; }
    public DateTime? CompletedAt { get; init; }
}

// ---------- Inventory ----------

public sealed record InventoryEntry
{
    public required string PartnerSku { get; init; }
    public bool InStock { get; init; }
    public int? AvailableQuantity { get; init; }
    public required Money Price { get; init; }
    public Money? Mrp { get; init; }
    public int? MaxOrderQuantity { get; init; }
}

public sealed record InventoryBatchRequest(DateTime SnapshotAt, string Mode, List<InventoryEntry> Entries);

public sealed record InventoryError(string PartnerSku, string Code, string Message);

public sealed record InventoryBatchAck(int Accepted, int Rejected, List<InventoryError>? Errors = null);

// ---------- Shoppers ----------

public sealed record UserPreferences
{
    public string? DietType { get; init; }
    public List<string>? Allergens { get; init; }
    public bool? StrictMayContain { get; init; }
    public List<string>? Goals { get; init; }
    public List<string>? AvoidIngredients { get; init; }
    public List<string>? ExcludedBrands { get; init; }
    public List<string>? ReligiousPreferences { get; init; }
    public string? Locale { get; init; }
    public bool? IsMinor { get; init; }
    public bool? HealthDataConsent { get; init; }
    public bool? PersonalizationConsent { get; init; }
    public string? ConsentNoticeVersion { get; init; }
    public DateTime? UpdatedAt { get; init; }
}

public sealed record ErasureRequest(Guid RequestId, string Status, DateTime ReceivedAt);

// ---------- SDK sessions ----------

public sealed record SdkSessionRequest
{
    public string? UserRef { get; init; }
    public required string CartRef { get; init; }
    public string? StoreId { get; init; }
    public required string Market { get; init; }
    public List<string>? Scopes { get; init; }
    public int? TtlSeconds { get; init; }
}

public sealed record SdkSession(string Token, string TokenType, DateTime ExpiresAt, List<string> Scopes);

// ---------- Cart evaluation ----------

public sealed record CartLine
{
    public required string LineId { get; init; }
    public required string PartnerSku { get; init; }
    public string? Gtin { get; init; }
    public string? Title { get; init; }
    public decimal Quantity { get; init; }
    public required Money UnitPrice { get; init; }
    public Money? LineDiscount { get; init; }
    public decimal? TaxRate { get; init; }
    public bool? IsSubstitutable { get; init; }
}

public sealed record CartContext
{
    public string? Placement { get; init; }
    public string? Channel { get; init; }
    public string? AppVersion { get; init; }
    public string? ExperimentArm { get; init; }
    public bool? IsMinor { get; init; }
    public int? MaxSuggestions { get; init; }
}

public sealed record CartEvaluationRequest
{
    public required string CartRef { get; init; }
    public string? UserRef { get; init; }
    public required string Market { get; init; }
    public string? StoreId { get; init; }
    public required string Currency { get; init; }
    public string? Locale { get; init; }
    public string? AvailabilityMode { get; init; }
    public CartContext? Context { get; init; }
    public UserPreferences? Preferences { get; init; }
    public required List<CartLine> Lines { get; init; }
}

public sealed record ProductSummary
{
    public required string PartnerSku { get; init; }
    public Guid ProductId { get; init; }
    public required string Title { get; init; }
    public string? Brand { get; init; }
    public NetQuantityDto? NetQuantity { get; init; }
    public string? ImageUrl { get; init; }
    public required HealthProfileSummary Health { get; init; }
}

public sealed record NutrientComparison
{
    public required string Nutrient { get; init; }
    public required string Basis { get; init; }
    public decimal From { get; init; }
    public decimal To { get; init; }
    public required string Unit { get; init; }
    public decimal? RelativeChange { get; init; }
    public required string Direction { get; init; }
}

public sealed record Explanation
{
    public required string Short { get; init; }
    public List<string>? Bullets { get; init; }
    public List<NutrientComparison> Details { get; init; } = [];
    public required string Generator { get; init; }
    public bool AiGenerated { get; init; }
    public required string Locale { get; init; }
    public required string MethodologyUrl { get; init; }
    public string? Disclaimer { get; init; }
}

/// <summary>RA-1 label that the partner must render on every HealthSwap-recommended product.</summary>
public sealed record RecommendationLabel
{
    public required string Code { get; init; }
    public required string Text { get; init; }
    public required string ShortText { get; init; }
    public required string Disclosure { get; init; }
    public bool Chargeable { get; init; }
    public string? FeeRule { get; init; }
}

public sealed record Alternative
{
    public int Rank { get; init; }
    public required ProductSummary Product { get; init; }
    public required RecommendationLabel Recommendation { get; init; }
    public Money? UnitPrice { get; init; }
    public Money? UnitPriceDelta { get; init; }
    public decimal? NormalizedPriceRatio { get; init; }
    public decimal HealthDelta { get; init; }
    public List<string>? ReasonCodes { get; init; }
    public required Explanation Explanation { get; init; }
    public bool Sponsored { get; init; }
    public bool RequiresAvailabilityCheck { get; init; }
    public decimal? SuggestedQuantity { get; init; }
}

public sealed record LineSuggestion
{
    public Guid SuggestionId { get; init; }
    public required string LineId { get; init; }
    public required ProductSummary Original { get; init; }
    public required List<Alternative> Alternatives { get; init; }
    public DateTime? ExpiresAt { get; init; }
}

public sealed record LineLabel(string LineId, string PartnerSku, Guid SuggestionId, RecommendationLabel Label);

public sealed record CartHealthSummary
{
    public int LinesTotal { get; init; }
    public int LinesResolved { get; init; }
    public int LinesScored { get; init; }
    public int LinesWithSuggestions { get; init; }
    public decimal? CartHsi { get; init; }
    public Dictionary<string, int>? FlagCounts { get; init; }
}

public sealed record Disclosures
{
    public required string FeeDisclosure { get; init; }
    public required string LabelDisclaimer { get; init; }
    public required string MethodologyUrl { get; init; }
    public List<DataAttribution>? Attributions { get; init; }
}

public sealed record ModelVersions(string Scoring, string Rules, string SwapGraph, string? Ranker = null);

public sealed record CartEvaluation
{
    public Guid EvaluationId { get; init; }
    public required string CartRef { get; init; }
    public required string Market { get; init; }
    public string? StoreId { get; init; }
    public DateTime CreatedAt { get; init; }
    public DateTime ExpiresAt { get; init; }
    public bool Degraded { get; init; }
    public List<string>? DegradationReasons { get; init; }
    public CartHealthSummary? Summary { get; init; }
    public List<LineSuggestion> Suggestions { get; init; } = [];
    public List<LineLabel>? LineLabels { get; init; }
    public required Disclosures Disclosures { get; init; }
    public required ModelVersions ModelVersions { get; init; }
}

public sealed record AlternativesResponse
{
    public Guid ProductId { get; init; }
    public required string Market { get; init; }
    public string? StoreId { get; init; }
    public Guid? SuggestionId { get; init; }
    public DateTime? ExpiresAt { get; init; }
    public List<Alternative> Alternatives { get; init; } = [];
    public Disclosures? Disclosures { get; init; }
}

// ---------- Events & RA-1 charges ----------

public sealed record SuggestionEvent
{
    public Guid EventId { get; init; }
    public required string Type { get; init; }
    public Guid SuggestionId { get; init; }
    public string? AlternativePartnerSku { get; init; }
    public decimal? Quantity { get; init; }
    public DateTime OccurredAt { get; init; }
    public Dictionary<string, string>? Metadata { get; init; }
}

public sealed record EventBatchRequest(List<SuggestionEvent> Events);

public sealed record RejectedEvent(string EventId, string Code, string Message);

public sealed record RecommendationChargeAck(Guid EventId, Guid ChargeId, string State, Money Amount, DateTime? FinalizesAt = null);

public sealed record EventBatchResult
{
    public int Accepted { get; init; }
    public int Duplicates { get; init; }
    public List<RejectedEvent>? Rejected { get; init; }
    public List<RecommendationChargeAck>? Charges { get; init; }
}

public sealed record RecommendationCharge
{
    public Guid ChargeId { get; init; }
    public Guid EventId { get; init; }
    public required string EventType { get; init; }
    public Guid SuggestionId { get; init; }
    public required string CartRef { get; init; }
    public string? Placement { get; init; }
    public required string AlternativePartnerSku { get; init; }
    public decimal Quantity { get; init; }
    public string? Basis { get; init; }
    public required Money Amount { get; init; }
    public required string State { get; init; }
    public string? RejectionReason { get; init; }
    public required string RuleVersion { get; init; }
    public DateTime OccurredAt { get; init; }
    public DateTime? FinalizesAt { get; init; }
    public DateTime? FinalizedAt { get; init; }
    public Guid? StatementId { get; init; }
    public Money? CreditedAmount { get; init; }
}

public sealed record RecommendationChargePage(List<RecommendationCharge> Items, string? NextCursor);

// ---------- Orders & attribution ----------

public sealed record OrderLine
{
    public required string LineId { get; init; }
    public required string PartnerSku { get; init; }
    public string? Gtin { get; init; }
    public decimal Quantity { get; init; }
    public required Money UnitPrice { get; init; }
    public Money? LineDiscount { get; init; }
    public Money? AllocatedOrderDiscount { get; init; }
    public Money? TaxAmount { get; init; }
    public decimal? TaxRate { get; init; }
    public required Money NetAmount { get; init; }
    public Guid? SuggestionId { get; init; }
}

public sealed record OrderCreateRequest
{
    public required string OrderId { get; init; }
    public string? CartRef { get; init; }
    public string? UserRef { get; init; }
    public string? StoreId { get; init; }
    public required string Market { get; init; }
    public required string Currency { get; init; }
    public DateTime PlacedAt { get; init; }
    public string? FulfillmentType { get; init; }
    public string? ExperimentArm { get; init; }
    public Money? OrderDiscount { get; init; }
    public required List<OrderLine> Lines { get; init; }
}

public sealed record AttributionLine
{
    public required string LineId { get; init; }
    public Guid SuggestionId { get; init; }
    public string? OriginalPartnerSku { get; init; }
    public string? AlternativePartnerSku { get; init; }
    public decimal? ReplacedQuantity { get; init; }
    public Money? OriginalUnitPriceSnapshot { get; init; }
    public Money? AlternativeNetUnitPrice { get; init; }
    public Money? PackDelta { get; init; }
    public Money? SizeNormalizedDelta { get; init; }
    public Money? FairPremium { get; init; }
    public decimal? FeeRate { get; init; }
    public Money? Fee { get; init; }
    public required string State { get; init; }
    public string? RejectionReason { get; init; }
    public DateTime? FinalizesAt { get; init; }
    public required string RuleVersion { get; init; }
}

public sealed record AttributionSummary(string OrderId, string Currency, Money TotalFairPremium, Money TotalFee, List<AttributionLine> Lines);

public sealed record Order
{
    public required string OrderId { get; init; }
    public required string Status { get; init; }
    public required string Market { get; init; }
    public required string Currency { get; init; }
    public DateTime PlacedAt { get; init; }
    public DateTime? DeliveredAt { get; init; }
    public List<OrderLine> Lines { get; init; } = [];
    public AttributionSummary? Attribution { get; init; }
}

public sealed record LineStatus(string LineId, decimal? DeliveredQuantity = null, decimal? CancelledQuantity = null);

public sealed record OrderStatusUpdate(string Status, DateTime OccurredAt, List<LineStatus>? LineStatuses = null);

public sealed record OrderAdjustment
{
    public required string AdjustmentId { get; init; }
    public required string LineId { get; init; }
    public required string Type { get; init; }
    public decimal? Quantity { get; init; }
    public Money? Amount { get; init; }
    public string? SubstitutedPartnerSku { get; init; }
    public DateTime OccurredAt { get; init; }
}

public sealed record OrderAdjustmentRequest(List<OrderAdjustment> Adjustments);

// ---------- Billing ----------

public sealed record StatementTotals
{
    public Money? PremiumFee { get; init; }
    public Money? RecommendationFee { get; init; }
    public int? RecommendationChargeCount { get; init; }
    public required Money GrossFee { get; init; }
    public required Money Reversals { get; init; }
    public required Money Credits { get; init; }
    public required Money NetFee { get; init; }
    public required Money Tax { get; init; }
    public required Money TotalDue { get; init; }
}

public sealed record Statement
{
    public Guid StatementId { get; init; }
    public required string Period { get; init; }
    public required string Currency { get; init; }
    public required string Status { get; init; }
    public required StatementTotals Totals { get; init; }
    public int LineCount { get; init; }
    public string? InvoiceNumber { get; init; }
    public string? InvoiceUrl { get; init; }
    public DateTime? IssuedAt { get; init; }
    public DateTime? DueAt { get; init; }
}

public sealed record StatementList(List<Statement> Items, string? NextCursor);

public sealed record StatementLine
{
    public int LineNo { get; init; }
    public required string Type { get; init; }
    public required string OrderId { get; init; }
    public required string OrderLineId { get; init; }
    public Guid SuggestionId { get; init; }
    public DateTime? DeliveredAt { get; init; }
    public required Money FairPremium { get; init; }
    public required Money Amount { get; init; }
}

public sealed record StatementLinePage(List<StatementLine> Items, string? NextCursor);

public sealed record DisputedLine(string OrderId, string OrderLineId);

public sealed record DisputeRequest
{
    public Guid StatementId { get; init; }
    public required string Reason { get; init; }
    public List<DisputedLine>? Lines { get; init; }
    public List<Guid>? RecommendationChargeIds { get; init; }
    public string? Comment { get; init; }
}

public sealed record Dispute
{
    public Guid DisputeId { get; init; }
    public Guid StatementId { get; init; }
    public required string Status { get; init; }
    public required string Reason { get; init; }
    public List<DisputedLine> Lines { get; init; } = [];
    public List<Guid>? RecommendationChargeIds { get; init; }
    public string? Comment { get; init; }
    public string? Resolution { get; init; }
    public Money? CreditNoteAmount { get; init; }
    public DateTime OpenedAt { get; init; }
    public DateTime? ResolvedAt { get; init; }
}

// ---------- Transparency ----------

public sealed record ScoringModel
{
    public required string Code { get; init; }
    public required string Version { get; init; }
    public required string Market { get; init; }
    public required string Status { get; init; }
    public string? Description { get; init; }
    public string? DocumentationUrl { get; init; }
    public string? EffectiveFrom { get; init; }
}

public sealed record ScoringModelList(List<ScoringModel> Items);

public sealed record RegionStatus(string Region, string Status);

public sealed record ServiceStatus(string Status, List<RegionStatus>? Regions, DateTime UpdatedAt);

// ---------- Webhooks ----------

public sealed record WebhookEnvelope
{
    public required string Id { get; init; }
    public required string Type { get; init; }
    public DateTime CreatedAt { get; init; }
    public string ApiVersion { get; init; } = "1.1";
    public Guid? PartnerId { get; init; }
    public required JsonElement Data { get; init; }
}

public sealed record TokenResponse(
    [property: JsonPropertyName("access_token")] string AccessToken,
    [property: JsonPropertyName("token_type")] string TokenType,
    [property: JsonPropertyName("expires_in")] int ExpiresIn,
    [property: JsonPropertyName("scope")] string Scope);
