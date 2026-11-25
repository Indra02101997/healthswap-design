using System.Collections.Concurrent;
using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain.Commerce;
using HealthSwap.Platform.Data;

namespace HealthSwap.Platform;

/// <summary>Error with a stable RFC 9457 code (HLD Appendix A). Mapped to application/problem+json by the API.</summary>
public sealed class HsException(int status, string code, string title, string? detail = null, List<FieldError>? errors = null) : Exception(detail ?? title)
{
    public int Status { get; } = status;
    public string Code { get; } = code;
    public string Title { get; } = title;
    public string? Detail { get; } = detail;
    public List<FieldError>? Errors { get; } = errors;

    public static HsException NotFound(string what) => new(404, "HS-NOT-FOUND", "Resource not found", $"{what} not found or not visible to this partner");
    public static HsException Invalid(string detail, List<FieldError>? errors = null) => new(400, "HS-REQ-INVALID", "Request validation failed", detail, errors);
    public static HsException TooLarge(string detail) => new(400, "HS-REQ-TOO-LARGE", "Request too large", detail);
    public static HsException Conflict(string code, string detail) => new(409, code, "Conflict", detail);
    public static HsException Unprocessable(string code, string detail) => new(422, code, "Unprocessable request", detail);
    public static HsException Forbidden(string detail) => new(403, "HS-AUTH-SCOPE", "Forbidden", detail);
}

/// <summary>Authenticated caller: partner, OAuth client or SDK session (bound to userRef, cartRef and storeId).</summary>
public sealed record PartnerContext
{
    public Guid PartnerId { get; init; }
    public string ClientId { get; init; } = "";
    public IReadOnlySet<string> Scopes { get; init; } = new HashSet<string>();
    public bool IsSdkSession { get; init; }
    public string? SdkUserRef { get; init; }
    public string? SdkCartRef { get; init; }
    public string? SdkStoreId { get; init; }
    public string? SdkMarket { get; init; }
    public bool IsServer => !IsSdkSession;
}

public sealed record PartnerInfo(Guid PartnerId, string DisplayName, string Geography, IReadOnlyList<string> Markets, string Status, PartnerConfig Config, string ConfigJson);

/// <summary>Tenant configuration directory: in-process cache with explicit invalidation (HLD 18: kill switch effective in seconds).</summary>
public sealed class PartnerDirectory(Db db)
{
    private sealed class Row
    {
        public Guid PartnerId { get; set; }
        public string DisplayName { get; set; } = "";
        public string Geography { get; set; } = "";
        public string[] Markets { get; set; } = [];
        public string Status { get; set; } = "";
        public string Config { get; set; } = "{}";
    }

    private readonly ConcurrentDictionary<Guid, (PartnerInfo Info, DateTime LoadedAt)> _cache = new();
    public static readonly TimeSpan Ttl = TimeSpan.FromSeconds(15);

    public static JsonSerializerOptions ConfigJson { get; } = HsJson.Create();

    public async Task<PartnerInfo> GetAsync(Guid partnerId, CancellationToken ct = default)
    {
        if (_cache.TryGetValue(partnerId, out var c) && DateTime.UtcNow - c.LoadedAt < Ttl) return c.Info;
        var row = await db.InTxAsync(DbRole.App, partnerId, u => u.QueryFirstOrDefaultAsync<Row>(
            "select partner_id, display_name, geography, markets, status, config::text as config from tenant.partner where partner_id = @partnerId",
            new { partnerId }, ct), ct) ?? throw new HsException(401, "HS-AUTH-INVALID", "Unknown partner", "The token's partner is not registered");
        var info = ToInfo(row);
        _cache[partnerId] = (info, DateTime.UtcNow);
        return info;
    }

    public async Task<IReadOnlyList<PartnerInfo>> ListAllAsync(CancellationToken ct = default)
    {
        var rows = await db.InTxAsync(DbRole.Ops, null, u => u.QueryAsync<Row>(
            "select partner_id, display_name, geography, markets, status, config::text as config from tenant.partner order by display_name", ct: ct), ct);
        return rows.Select(ToInfo).ToList();
    }

    public void Invalidate(Guid partnerId) => _cache.TryRemove(partnerId, out _);

    private static PartnerInfo ToInfo(Row r)
    {
        var cfg = JsonSerializer.Deserialize<PartnerConfig>(r.Config, ConfigJson) ?? new PartnerConfig();
        return new PartnerInfo(r.PartnerId, r.DisplayName, r.Geography, r.Markets, r.Status, cfg, r.Config);
    }
}
