using System.Globalization;
using System.Text.Json;
using System.Text.Json.Serialization;
using HealthSwap.Contracts;
using HealthSwap.Domain.Commerce;
using HealthSwap.Domain.Scoring;

namespace HealthSwap.Platform.Cache;

public sealed record SkuMapEntry(Guid ProductId, decimal Confidence, string State);

/// <summary>Precomputed swap edge (product level, partner-agnostic) stored as swap:{market}:{graphVersion}:{productId}.</summary>
public sealed record SwapEdge(Guid To, decimal HealthDelta, decimal Substitutability, string[] Reasons, long FromVersion, long ToVersion,
    string ToCategory, string[] IntroducedTierC);

public sealed record InventoryItem(bool InStock, decimal Price, decimal? Mrp, DateTime ObservedAt, int? AvailableQuantity);

public sealed record AlternativeSnapshot(int Rank, string PartnerSku, Guid ProductId, long VersionId, decimal? UnitPrice, decimal? NetQuantity,
    string? NetUnit, decimal HealthDelta, bool Sponsored, float[]? Features = null);

/// <summary>Everything attribution (FP-1) and RA-1 need about a served suggestion (mirrors txn.suggestion).</summary>
public sealed record SuggestionSnapshot
{
    public Guid SuggestionId { get; init; }
    public Guid PartnerId { get; init; }
    public Guid? EvaluationId { get; init; }
    public required string CartRef { get; init; }
    public string? UserRef { get; init; }
    public required string Market { get; init; }
    public string? StoreId { get; init; }
    public string Placement { get; init; } = "CART";
    public string? LineId { get; init; }
    public required string FromPartnerSku { get; init; }
    public Guid FromProductId { get; init; }
    public long FromVersionId { get; init; }
    public decimal FromQty { get; init; }
    public decimal? FromUnitPrice { get; init; }
    public decimal? FromNetQuantity { get; init; }
    public string? FromNetUnit { get; init; }
    public decimal? FromTaxRate { get; init; }
    public required string Currency { get; init; }
    public List<AlternativeSnapshot> Alternatives { get; init; } = [];
    public RecoFeeSnapshot? RecoFee { get; init; }
    public required string RulesVersion { get; init; }
    public required string GraphVersion { get; init; }
    public DateTime CreatedAt { get; init; }
    public DateTime ExpiresAt { get; init; }
    public DateTime AttributionExpiresAt { get; init; }
}

public sealed record CachedExplanation(string Short, List<string> Bullets, string Generator, bool AiGenerated, string? ModelRef);

public sealed record ShopperPrefsDoc
{
    public string DietType { get; init; } = "ANY";
    public List<string> Allergens { get; init; } = [];
    public bool StrictMayContain { get; init; } = true;
    public List<string> Goals { get; init; } = [];
    public List<string> AvoidIngredients { get; init; } = [];
    public List<string> ExcludedBrands { get; init; } = [];
    public List<string> ReligiousPreferences { get; init; } = [];
    public string? Locale { get; init; }
    public bool? IsMinor { get; init; }
    public bool HealthDataConsent { get; init; }
    public bool PersonalizationConsent { get; init; }
    public string? ConsentNoticeVersion { get; init; }
    public DateTime UpdatedAt { get; init; }
}

/// <summary>Typed facade over the HLD 10.2 key design. All hot-path reads go through here (no PostgreSQL, no LLM).</summary>
public sealed class HotCache(ICacheStore store)
{
    public static readonly JsonSerializerOptions Json = CreateJson();

    private static JsonSerializerOptions CreateJson()
    {
        var o = HsJson.Create();
        o.Converters.Add(new JsonStringEnumConverter());
        return o;
    }

    public ICacheStore Store => store;

    private static string S(Guid g) => g.ToString("N");
    private static T? De<T>(string? json) => json is null ? default : JsonSerializer.Deserialize<T>(json, Json);
    private static string Ser<T>(T value) => JsonSerializer.Serialize(value, Json);

    // skumap:{partnerId}:{partnerSku}
    public static string SkuMapKey(Guid partner, string sku) => $"skumap:{S(partner)}:{sku}";
    public async Task<SkuMapEntry?[]> GetSkuMapsAsync(Guid partner, IReadOnlyList<string> skus) =>
        (await store.MGetAsync(skus.Select(s => SkuMapKey(partner, s)).ToList())).Select(De<SkuMapEntry>).ToArray();
    public Task SetSkuMapAsync(Guid partner, string sku, SkuMapEntry e) => store.SetAsync(SkuMapKey(partner, sku), Ser(e));
    public Task RemoveSkuMapAsync(Guid partner, string sku) => store.DeleteAsync(SkuMapKey(partner, sku));

    // p2sku:{partnerId}:{productId}
    public static string P2SkuKey(Guid partner, Guid product) => $"p2sku:{S(partner)}:{S(product)}";
    public Task<string[]> GetPartnerSkusAsync(Guid partner, Guid product) => store.SMembersAsync(P2SkuKey(partner, product));
    public Task AddPartnerSkuAsync(Guid partner, Guid product, string sku) => store.SAddAsync(P2SkuKey(partner, product), [sku]);
    public Task RemovePartnerSkuAsync(Guid partner, Guid product, string sku) => store.SRemAsync(P2SkuKey(partner, product), [sku]);

    // gtin:{gtin14} -> productId (GTIN fallback for lines not yet in the synced catalog)
    public async Task<Guid?> GetProductByGtinAsync(string gtin14) => Guid.TryParse(await store.GetAsync("gtin:" + gtin14), out var g) ? g : null;
    public Task SetGtinAsync(string gtin14, Guid product) => store.SetAsync("gtin:" + gtin14, product.ToString());

    // hp:{market}:{productId}
    public static string ProfileKey(string market, Guid product) => $"hp:{market}:{S(product)}";
    public async Task<HealthProfileData?> GetProfileAsync(string market, Guid product) => De<HealthProfileData>(await store.GetAsync(ProfileKey(market, product)));
    public async Task<Dictionary<Guid, HealthProfileData>> GetProfilesAsync(string market, IReadOnlyCollection<Guid> products)
    {
        var ids = products.Distinct().ToList();
        var values = await store.MGetAsync(ids.Select(p => ProfileKey(market, p)).ToList());
        var result = new Dictionary<Guid, HealthProfileData>();
        for (var i = 0; i < ids.Count; i++)
            if (De<HealthProfileData>(values[i]) is { } hp) result[ids[i]] = hp;
        return result;
    }
    public Task SetProfileAsync(HealthProfileData hp) => store.SetAsync(ProfileKey(hp.Market, hp.ProductId), Ser(hp));

    // swap:active:{market} and swap:{market}:{graphVersion}:{productId}
    public Task<string?> GetActiveGraphAsync(string market) => store.GetAsync($"swap:active:{market}");
    public Task SetActiveGraphAsync(string market, string graphVersion) => store.SetAsync($"swap:active:{market}", graphVersion);
    public static string EdgesKey(string market, string graph, Guid product) => $"swap:{market}:{graph}:{S(product)}";
    public async Task<Dictionary<Guid, SwapEdge[]>> GetEdgesAsync(string market, string graph, IReadOnlyCollection<Guid> products)
    {
        var ids = products.Distinct().ToList();
        var values = await store.MGetAsync(ids.Select(p => EdgesKey(market, graph, p)).ToList());
        var result = new Dictionary<Guid, SwapEdge[]>();
        for (var i = 0; i < ids.Count; i++)
            if (De<SwapEdge[]>(values[i]) is { } e) result[ids[i]] = e;
        return result;
    }
    public Task SetEdgesAsync(string market, string graph, Guid product, SwapEdge[] edges) => store.SetAsync(EdgesKey(market, graph, product), Ser(edges));
    public Task<int> DeleteGraphAsync(string market, string graph) => store.DeleteByPrefixAsync($"swap:{market}:{graph}:");

    // inv:{partnerId}:{storeId} -> hash partnerSku -> "inStock|price|mrp|observedUnix|qty"
    public static string InventoryKey(Guid partner, string store) => $"inv:{S(partner)}:{store}";
    public async Task<Dictionary<string, InventoryItem>> GetInventoryAsync(Guid partner, string storeId, IReadOnlyList<string> skus)
    {
        var values = await store.HMGetAsync(InventoryKey(partner, storeId), skus);
        var result = new Dictionary<string, InventoryItem>(StringComparer.Ordinal);
        for (var i = 0; i < skus.Count; i++)
            if (values[i] is { } v && ParseInventory(v) is { } item) result[skus[i]] = item;
        return result;
    }
    public Task SetInventoryAsync(Guid partner, string storeId, IEnumerable<(string Sku, InventoryItem Item)> entries) =>
        store.HSetAsync(InventoryKey(partner, storeId), entries.Select(e => new KeyValuePair<string, string>(e.Sku, FormatInventory(e.Item))).ToList());
    public async Task<string[]> GetInventorySkusAsync(Guid partner, string storeId) => (await store.HGetAllAsync(InventoryKey(partner, storeId))).Keys.ToArray();

    private static string FormatInventory(InventoryItem i) => string.Join('|', i.InStock ? "1" : "0", i.Price.ToString(CultureInfo.InvariantCulture),
        i.Mrp?.ToString(CultureInfo.InvariantCulture) ?? "", new DateTimeOffset(i.ObservedAt).ToUnixTimeSeconds().ToString(CultureInfo.InvariantCulture),
        i.AvailableQuantity?.ToString(CultureInfo.InvariantCulture) ?? "");

    private static InventoryItem? ParseInventory(string v)
    {
        var p = v.Split('|');
        if (p.Length < 4) return null;
        return new InventoryItem(p[0] == "1", decimal.Parse(p[1], CultureInfo.InvariantCulture),
            p[2].Length > 0 ? decimal.Parse(p[2], CultureInfo.InvariantCulture) : null,
            DateTimeOffset.FromUnixTimeSeconds(long.Parse(p[3], CultureInfo.InvariantCulture)).UtcDateTime,
            p.Length > 4 && p[4].Length > 0 ? int.Parse(p[4], CultureInfo.InvariantCulture) : null);
    }

    // expl:{market}:{locale}:{from}:{to}:{factsHash}
    public static string ExplanationKey(string market, string locale, Guid from, Guid to, string factsHash) => $"expl:{market}:{locale}:{S(from)}:{S(to)}:{factsHash}";
    public async Task<CachedExplanation?> GetExplanationAsync(string market, string locale, Guid from, Guid to, string factsHash) =>
        De<CachedExplanation>(await store.GetAsync(ExplanationKey(market, locale, from, to, factsHash)));
    public Task SetExplanationAsync(string market, string locale, Guid from, Guid to, string factsHash, CachedExplanation e) =>
        store.SetAsync(ExplanationKey(market, locale, from, to, factsHash), Ser(e));

    // pref:{partnerId}:{userRef} (TTL 30 days; PostgreSQL is the source of truth)
    public static string PrefsKey(Guid partner, string userRef) => $"pref:{S(partner)}:{userRef}";
    public async Task<ShopperPrefsDoc?> GetPrefsAsync(Guid partner, string userRef) => De<ShopperPrefsDoc>(await store.GetAsync(PrefsKey(partner, userRef)));
    public Task SetPrefsAsync(Guid partner, string userRef, ShopperPrefsDoc prefs) => store.SetAsync(PrefsKey(partner, userRef), Ser(prefs), TimeSpan.FromDays(30));
    public Task DeletePrefsAsync(Guid partner, string userRef) => store.DeleteAsync(PrefsKey(partner, userRef));

    // dismiss:{partnerId}:{userRef}:{fromProductId}:{toProductId} - R8 fatigue cool-down (fixes the missing hot-path store).
    public static string DismissKey(Guid partner, string userRef, Guid from, Guid to) => $"dismiss:{S(partner)}:{userRef}:{S(from)}:{S(to)}";
    public async Task<HashSet<(Guid From, Guid To)>> GetDismissedAsync(Guid partner, string userRef, IReadOnlyList<(Guid From, Guid To)> pairs)
    {
        var values = await store.MGetAsync(pairs.Select(p => DismissKey(partner, userRef, p.From, p.To)).ToList());
        var set = new HashSet<(Guid, Guid)>();
        for (var i = 0; i < pairs.Count; i++) if (values[i] is not null) set.Add(pairs[i]);
        return set;
    }
    public Task SetDismissedAsync(Guid partner, string userRef, Guid from, Guid to, TimeSpan ttl) => store.SetAsync(DismissKey(partner, userRef, from, to), "1", ttl);
    public Task<int> DeleteUserKeysAsync(Guid partner, string userRef) => store.DeleteByPrefixAsync($"dismiss:{S(partner)}:{userRef}:");

    // reco:{partnerId}:{cartRef} -> hash partnerSku -> suggestionId: cart lines holding a recommended product (RA-1 label)
    public static string CartRecoKey(Guid partner, string cartRef) => $"reco:{S(partner)}:{cartRef}";
    public async Task<Dictionary<string, Guid>> GetCartRecoLabelsAsync(Guid partner, string cartRef) =>
        (await store.HGetAllAsync(CartRecoKey(partner, cartRef))).Where(kv => Guid.TryParse(kv.Value, out _)).ToDictionary(kv => kv.Key, kv => Guid.Parse(kv.Value));
    public Task AddCartRecoLabelAsync(Guid partner, string cartRef, string sku, Guid suggestionId, TimeSpan ttl) =>
        store.HSetAsync(CartRecoKey(partner, cartRef), [new(sku, suggestionId.ToString())], ttl);
    public Task RemoveCartRecoLabelAsync(Guid partner, string cartRef, string sku) => store.HDelAsync(CartRecoKey(partner, cartRef), [sku]);

    // sug:{suggestionId} - snapshot available to the event collector before the async database write lands
    public async Task<SuggestionSnapshot?> GetSuggestionAsync(Guid id) => De<SuggestionSnapshot>(await store.GetAsync("sug:" + S(id)));
    public Task SetSuggestionAsync(SuggestionSnapshot s, TimeSpan ttl) => store.SetAsync("sug:" + S(s.SuggestionId), Ser(s), ttl);

    // eval:{partnerId}:{evaluationId} - GET /cart-evaluations/{id} until expiresAt
    public Task<string?> GetEvaluationJsonAsync(Guid partner, Guid evaluationId) => store.GetAsync($"eval:{S(partner)}:{S(evaluationId)}");
    public Task SetEvaluationJsonAsync(Guid partner, Guid evaluationId, string json, TimeSpan ttl) => store.SetAsync($"eval:{S(partner)}:{S(evaluationId)}", json, ttl);

    // cat:{market}:{categoryId} -> HSI p60 (target-line ceiling); catmap:{market}:{fromCategory} -> approved target categories
    public async Task<Dictionary<Guid, decimal>> GetCategoryCeilingsAsync(string market, IReadOnlyCollection<Guid> categories)
    {
        var ids = categories.Distinct().ToList();
        var values = await store.MGetAsync(ids.Select(c => $"cat:{market}:{S(c)}").ToList());
        var result = new Dictionary<Guid, decimal>();
        for (var i = 0; i < ids.Count; i++)
            if (values[i] is { } v && decimal.TryParse(v, NumberStyles.Number, CultureInfo.InvariantCulture, out var d)) result[ids[i]] = d;
        return result;
    }
    public Task SetCategoryCeilingAsync(string market, Guid category, decimal ceiling) =>
        store.SetAsync($"cat:{market}:{S(category)}", ceiling.ToString(CultureInfo.InvariantCulture));
    public Task<string[]> GetCategoryMapAsync(string market, string fromCategory) => store.SMembersAsync($"catmap:{market}:{fromCategory}");
    public Task SetCategoryMapAsync(string market, string fromCategory, IReadOnlyList<string> toCategories) => store.SAddAsync($"catmap:{market}:{fromCategory}", toCategories);
}
