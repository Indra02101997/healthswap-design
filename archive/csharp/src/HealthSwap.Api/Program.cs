using System.Text.Encodings.Web;
using System.Text.Json.Serialization;
using HealthSwap.Api;
using HealthSwap.Platform;
using HealthSwap.Platform.Data;
using Microsoft.AspNetCore.Authentication.JwtBearer;
using Microsoft.Extensions.FileProviders;
using Microsoft.IdentityModel.Tokens;

var builder = WebApplication.CreateBuilder(args);
var repoRoot = builder.Configuration["RepoRoot"] ?? PlatformRegistration.FindRepoRoot(builder.Environment.ContentRootPath);

builder.Services.AddHealthSwapPlatform(builder.Configuration, repoRoot);
builder.Services.AddHealthSwapWorkers(
    includeScheduler: builder.Configuration.GetValue("Scheduler:Enabled", true),
    includeCacheWarmer: builder.Configuration.GetValue("Cache:WarmOnStartup", true));

var auth = new AuthOptions();
builder.Configuration.GetSection("Auth").Bind(auth);
builder.Services.AddSingleton(auth);
var keys = new TokenKeys(auth, repoRoot);
builder.Services.AddSingleton(keys);
builder.Services.AddSingleton(sp => new DevIdentityProvider(sp.GetRequiredService<Db>(), keys, auth, repoRoot));
var rateLimits = new RateLimitOptions();
builder.Configuration.GetSection("RateLimits").Bind(rateLimits);
builder.Services.AddSingleton(rateLimits);
builder.Services.AddSingleton(_ => OpenApiContract.Load());
builder.Services.AddSingleton<BackgroundJobs>();
builder.Services.AddHostedService(sp => sp.GetRequiredService<BackgroundJobs>());
builder.Services.AddHttpContextAccessor();

builder.Services.ConfigureHttpJsonOptions(o =>
{
    o.SerializerOptions.DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull;
    o.SerializerOptions.Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping;
});

builder.Services.AddAuthentication(JwtBearerDefaults.AuthenticationScheme).AddJwtBearer(o =>
{
    o.MapInboundClaims = false;
    if (auth.Authority is { Length: > 0 })
    {
        o.Authority = auth.Authority;
        o.Audience = auth.Audience;
    }
    else
    {
        o.TokenValidationParameters = new TokenValidationParameters
        {
            ValidIssuer = auth.Issuer, ValidAudience = auth.Audience, IssuerSigningKey = keys.Key, ValidAlgorithms = [SecurityAlgorithms.EcdsaSha256],
            ClockSkew = TimeSpan.FromSeconds(30), NameClaimType = "sub",
        };
    }
    o.Events = new JwtBearerEvents
    {
        OnChallenge = async ctx =>
        {
            ctx.HandleResponse();
            if (ctx.Request.Path.StartsWithSegments("/mcp"))
                ctx.Response.Headers.WWWAuthenticate = $"Bearer resource_metadata=\"{Http.BaseUrl(ctx.Request)}/.well-known/oauth-protected-resource\"";
            await ProblemMiddleware.WriteAsync(ctx.HttpContext, 401, "HS-AUTH-INVALID", "Unauthorized", ctx.ErrorDescription ?? "A valid bearer token is required", null);
        },
        OnForbidden = ctx => ProblemMiddleware.WriteAsync(ctx.HttpContext, 403, "HS-AUTH-SCOPE", "Forbidden", "The token lacks the scope required by this operation", null),
    };
});
builder.Services.AddAuthorization(o =>
{
    foreach (var scope in Scopes.All)
        o.AddPolicy("scope:" + scope, p => p.RequireAuthenticatedUser().RequireAssertion(c => c.User.HasScope(scope)));
    o.AddPolicy("admin", p => p.RequireAuthenticatedUser().RequireAssertion(c => c.User.HasScope(Scopes.Admin)));
    o.AddPolicy("mcp", p => p.RequireAuthenticatedUser().RequireAssertion(c => c.User.HasScope("carts.evaluate") || c.User.HasScope("products.read")));
});

builder.Services.AddMcpServer(o => o.ServerInfo = new() { Name = "healthswap", Version = "1.0.0" })
    .WithHttpTransport(o => o.Stateless = true)
    .WithTools<HealthSwapTools>();

builder.Services.AddCors(o => o.AddPolicy("sdk", p => p
    .WithOrigins(builder.Configuration.GetSection("Cors:Origins").Get<string[]>() ?? ["http://localhost:8080"])
    .WithHeaders("Authorization", "Content-Type", "Idempotency-Key", "traceparent").WithMethods("GET", "POST")));

var app = builder.Build();

app.UseMiddleware<ProblemMiddleware>();
app.UseCors("sdk");
app.UseAuthentication();
app.UseMiddleware<RateLimitMiddleware>();
app.UseAuthorization();
app.UseMiddleware<RequestValidationMiddleware>();

app.MapPost("/oauth2/token", async (HttpContext http, DevIdentityProvider idp) =>
{
    if (auth.Authority is { Length: > 0 }) return Results.NotFound();
    if (!http.Request.HasFormContentType)
        return Results.Json(new { error = "invalid_request", error_description = "Use application/x-www-form-urlencoded" }, statusCode: 400);
    var form = await http.Request.ReadFormAsync();
    if (form["grant_type"] != "client_credentials")
        return Results.Json(new { error = "unsupported_grant_type" }, statusCode: 400);
    string? id = form["client_id"], secret = form["client_secret"];
    if (http.Request.Headers.Authorization.FirstOrDefault() is { } basic && basic.StartsWith("Basic ", StringComparison.OrdinalIgnoreCase))
    {
        var decoded = System.Text.Encoding.UTF8.GetString(Convert.FromBase64String(basic[6..])).Split(':', 2);
        if (decoded.Length == 2) (id, secret) = (Uri.UnescapeDataString(decoded[0]), Uri.UnescapeDataString(decoded[1]));
    }
    try
    {
        var token = await idp.IssueAsync(id, secret, form["scope"], http.RequestAborted);
        http.Response.Headers.CacheControl = "no-store";
        return Results.Json(token);
    }
    catch (HsException ex) when (ex.Code is "invalid_client" or "invalid_scope")
    {
        return Results.Json(new { error = ex.Code, error_description = ex.Detail }, statusCode: ex.Status);
    }
}).AllowAnonymous().DisableAntiforgery();

app.MapGet("/.well-known/jwks.json", (TokenKeys k) => Results.Json(k.Jwks())).AllowAnonymous();
app.MapGet("/.well-known/oauth-protected-resource", (HttpContext http) => Results.Json(new
{
    resource = Http.BaseUrl(http.Request) + "/mcp",
    authorization_servers = new[] { auth.Authority ?? Http.BaseUrl(http.Request) + "/oauth2" },
    scopes_supported = new[] { "carts.evaluate", "products.read", "events.write" },
    bearer_methods_supported = new[] { "header" },
})).AllowAnonymous();
app.MapGet("/openapi.yaml", (OpenApiContract c) => Results.Text(c.Yaml, "application/yaml")).AllowAnonymous();
app.MapGet("/healthz", () => Results.Ok(new { status = "ok" })).AllowAnonymous();
app.MapGet("/readyz", async (Db db, CancellationToken ct) =>
{
    await using var c = await db.OpenRawAsync(ct);
    return Results.Ok(new { status = "ready" });
}).AllowAnonymous();

PartnerEndpoints.Map(app);
AdminEndpoints.Map(app);
app.MapMcp("/mcp").RequireAuthorization("mcp");

if (app.Environment.IsDevelopment() || app.Configuration.GetValue("Demo:Enabled", false))
{
    DemoEndpoints.Map(app, repoRoot);
    var demoRoot = Path.Combine(app.Environment.ContentRootPath, "wwwroot", "demo");
    if (Directory.Exists(demoRoot))
        app.UseStaticFiles(new StaticFileOptions { FileProvider = new PhysicalFileProvider(demoRoot), RequestPath = "/demo" });
    var sdkDist = Path.Combine(repoRoot, "sdk", "typescript", "web", "dist");
    if (Directory.Exists(sdkDist))
        app.UseStaticFiles(new StaticFileOptions { FileProvider = new PhysicalFileProvider(sdkDist), RequestPath = "/demo/sdk" });
}

app.Run();

public partial class Program;
