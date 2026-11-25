using System.Collections.Concurrent;
using System.Security.Claims;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Platform;
using HealthSwap.Platform.Data;
using Microsoft.IdentityModel.JsonWebTokens;
using Microsoft.IdentityModel.Tokens;

namespace HealthSwap.Api;

public sealed class AuthOptions
{
    /// <summary>External OAuth authority (for example Microsoft Entra ID). When set, the built-in development token endpoint is disabled.</summary>
    public string? Authority { get; set; }
    public string Issuer { get; set; } = "http://localhost:8080/oauth2";
    public string Audience { get; set; } = "healthswap-api";
    public int AccessTokenMinutes { get; set; } = 10;
    public int SdkSessionMaxSeconds { get; set; } = 900;
    /// <summary>Development identity provider: client secrets are loaded from this file and kept only as PBKDF2 hashes in memory.</summary>
    public string? ClientsFile { get; set; }
    public string? SigningKeyFile { get; set; }
}

public static class Scopes
{
    public const string Admin = "hs.admin";
    public static readonly string[] SdkAllowed = ["carts.evaluate", "events.write", "products.read"];
    public static readonly string[] All =
    [
        "catalog.write", "catalog.read", "inventory.write", "products.read", "carts.evaluate", "events.write", "orders.write", "orders.read",
        "users.write", "users.read", "billing.read", "billing.write", "sdk.sessions.write", Admin,
    ];
}

/// <summary>ES256 signing key for locally issued tokens (persisted so tokens survive restarts during manual testing).</summary>
public sealed class TokenKeys
{
    public ECDsaSecurityKey Key { get; }
    public SigningCredentials Credentials { get; }

    public TokenKeys(AuthOptions options, string repoRoot)
    {
        var path = options.SigningKeyFile ?? Path.Combine(repoRoot, ".data", "keys", "jwt-es256.pem");
        var ecdsa = ECDsa.Create(ECCurve.NamedCurves.nistP256);
        if (File.Exists(path)) ecdsa.ImportFromPem(File.ReadAllText(path));
        else
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, ecdsa.ExportECPrivateKeyPem());
        }
        var kid = Convert.ToHexStringLower(SHA256.HashData(ecdsa.ExportSubjectPublicKeyInfo()))[..16];
        Key = new ECDsaSecurityKey(ecdsa) { KeyId = kid };
        Credentials = new SigningCredentials(Key, SecurityAlgorithms.EcdsaSha256);
    }

    public object Jwks()
    {
        var p = Key.ECDsa.ExportParameters(false);
        return new { keys = new[] { new { kty = "EC", crv = "P-256", use = "sig", alg = "ES256", kid = Key.KeyId, x = Base64UrlEncoder.Encode(p.Q.X!), y = Base64UrlEncoder.Encode(p.Q.Y!) } } };
    }
}

/// <summary>
/// Development OAuth 2.0 client-credentials server (RFC 6749 section 4.4). Production deployments use an external authority
/// (Auth:Authority) and mTLS-bound tokens (RFC 8705); the API only validates tokens.
/// </summary>
public sealed class DevIdentityProvider
{
    private sealed record ClientSecret(byte[] Salt, byte[] Hash);
    private sealed record FileClient(string ClientId, string Secret, List<string> Scopes);
    private sealed record FilePartner(Guid PartnerId, List<FileClient> Clients);
    private sealed record FileRoot(List<FilePartner> Partners, FileClient? Admin);
    private sealed class ClientRow { public string ClientId { get; set; } = ""; public Guid PartnerId { get; set; } public string[] Scopes { get; set; } = []; public string Status { get; set; } = ""; public string Geography { get; set; } = ""; }

    private readonly ConcurrentDictionary<string, ClientSecret> _secrets = new();
    private readonly HashSet<string> _adminClients = [];
    private readonly Db _db;
    private readonly TokenKeys _keys;
    private readonly AuthOptions _options;

    public DevIdentityProvider(Db db, TokenKeys keys, AuthOptions options, string repoRoot)
    {
        _db = db;
        _keys = keys;
        _options = options;
        var file = options.ClientsFile ?? Path.Combine(repoRoot, "data", "seed", "demo", "partners.json");
        if (!File.Exists(file)) return;
        var root = JsonSerializer.Deserialize<FileRoot>(File.ReadAllText(file), HsJson.Options)!;
        foreach (var c in root.Partners.SelectMany(p => p.Clients)) _secrets[c.ClientId] = Hash(c.Secret);
        if (root.Admin is { } admin) { _secrets[admin.ClientId] = Hash(admin.Secret); _adminClients.Add(admin.ClientId); }
    }

    private static ClientSecret Hash(string secret)
    {
        var salt = RandomNumberGenerator.GetBytes(16);
        return new ClientSecret(salt, Rfc2898DeriveBytes.Pbkdf2(Encoding.UTF8.GetBytes(secret), salt, 100_000, HashAlgorithmName.SHA256, 32));
    }

    private bool Verify(string clientId, string secret) =>
        _secrets.TryGetValue(clientId, out var s) &&
        CryptographicOperations.FixedTimeEquals(s.Hash, Rfc2898DeriveBytes.Pbkdf2(Encoding.UTF8.GetBytes(secret), s.Salt, 100_000, HashAlgorithmName.SHA256, 32));

    public async Task<TokenResponse> IssueAsync(string? clientId, string? clientSecret, string? requestedScope, CancellationToken ct)
    {
        if (clientId is null || clientSecret is null || !Verify(clientId, clientSecret))
            throw new HsException(401, "invalid_client", "Invalid client", "Client authentication failed");
        string[] granted;
        var claims = new Dictionary<string, object> { ["client_id"] = clientId, ["token_use"] = "partner" };
        if (_adminClients.Contains(clientId))
        {
            granted = [Scopes.Admin];
            claims["token_use"] = "admin";
        }
        else
        {
            var row = await _db.InTxAsync(DbRole.Ops, null, u => u.QueryFirstOrDefaultAsync<ClientRow>(
                "select c.client_id, c.partner_id, c.scopes, c.status, p.geography from tenant.api_client c join tenant.partner p using (partner_id) where c.client_id = @clientId",
                new { clientId }, ct), ct);
            if (row is null || row.Status == "REVOKED") throw new HsException(401, "invalid_client", "Invalid client", "Client is not registered or revoked");
            granted = row.Scopes;
            claims["partner_id"] = row.PartnerId.ToString();
            claims["geo"] = row.Geography;
        }
        if (!string.IsNullOrWhiteSpace(requestedScope))
        {
            var requested = requestedScope.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            if (requested.Any(s => !granted.Contains(s))) throw new HsException(400, "invalid_scope", "Invalid scope", "Requested scope exceeds the client's grant");
            granted = requested;
        }
        claims["scope"] = string.Join(' ', granted);
        var lifetime = TimeSpan.FromMinutes(_options.AccessTokenMinutes);
        return new TokenResponse(Create(clientId, claims, lifetime), "Bearer", (int)lifetime.TotalSeconds, claims["scope"].ToString()!);
    }

    /// <summary>SDK session token bound to partner, cart, user, store and market (POST /sdk/sessions).</summary>
    public SdkSession IssueSdkSession(PartnerContext partner, SdkSessionRequest req)
    {
        var scopes = (req.Scopes is { Count: > 0 } s ? s : Scopes.SdkAllowed.ToList()).Distinct().ToList();
        if (scopes.Any(x => !Scopes.SdkAllowed.Contains(x))) throw HsException.Invalid("SDK sessions may only carry carts.evaluate, events.write and products.read");
        var ttl = Math.Clamp(req.TtlSeconds ?? _options.SdkSessionMaxSeconds, 60, _options.SdkSessionMaxSeconds);
        var claims = new Dictionary<string, object>
        {
            ["client_id"] = partner.ClientId, ["partner_id"] = partner.PartnerId.ToString(), ["token_use"] = "sdk", ["scope"] = string.Join(' ', scopes),
            ["cart_ref"] = req.CartRef, ["market"] = req.Market,
        };
        if (req.UserRef is not null) claims["user_ref"] = req.UserRef;
        if (req.StoreId is not null) claims["store_id"] = req.StoreId;
        var expires = DateTime.UtcNow.AddSeconds(ttl);
        return new SdkSession(Create("sdk:" + partner.ClientId, claims, TimeSpan.FromSeconds(ttl)), "Bearer", expires, scopes);
    }

    private string Create(string subject, Dictionary<string, object> claims, TimeSpan lifetime)
    {
        var now = DateTime.UtcNow;
        claims["sub"] = subject;
        claims["jti"] = Guid.NewGuid().ToString("N");
        return new JsonWebTokenHandler().CreateToken(new SecurityTokenDescriptor
        {
            Issuer = _options.Issuer, Audience = _options.Audience, IssuedAt = now, NotBefore = now.AddSeconds(-5), Expires = now + lifetime,
            Claims = claims, SigningCredentials = _keys.Credentials,
        });
    }
}

public static class PrincipalExtensions
{
    public static bool HasScope(this ClaimsPrincipal user, string scope) =>
        user.FindAll("scope").SelectMany(c => c.Value.Split(' ', StringSplitOptions.RemoveEmptyEntries)).Contains(scope);

    public static PartnerContext? ToPartnerContext(this ClaimsPrincipal user)
    {
        if (!Guid.TryParse(user.FindFirst("partner_id")?.Value, out var partnerId)) return null;
        var sdk = user.FindFirst("token_use")?.Value == "sdk";
        return new PartnerContext
        {
            PartnerId = partnerId, ClientId = user.FindFirst("client_id")?.Value ?? "",
            Scopes = user.FindAll("scope").SelectMany(c => c.Value.Split(' ', StringSplitOptions.RemoveEmptyEntries)).ToHashSet(),
            IsSdkSession = sdk, SdkCartRef = sdk ? user.FindFirst("cart_ref")?.Value : null, SdkUserRef = sdk ? user.FindFirst("user_ref")?.Value : null,
            SdkStoreId = sdk ? user.FindFirst("store_id")?.Value : null, SdkMarket = sdk ? user.FindFirst("market")?.Value : null,
        };
    }

    public static PartnerContext Partner(this HttpContext http) =>
        http.User.ToPartnerContext() ?? throw new HsException(403, "HS-AUTH-SCOPE", "Forbidden", "This endpoint requires a partner token");
}
