using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Platform;
using HealthSwap.Platform.Services;

namespace HealthSwap.Api;

/// <summary>RFC 9457 problem responses with stable HealthSwap codes (HLD Appendix A).</summary>
public sealed class ProblemMiddleware(RequestDelegate next, ILogger<ProblemMiddleware> log, IHostEnvironment env)
{
    public async Task InvokeAsync(HttpContext http)
    {
        try { await next(http); }
        catch (HsException ex) { await WriteAsync(http, ex.Status, ex.Code, ex.Title, ex.Detail, ex.Errors); }
        catch (BadHttpRequestException ex) { await WriteAsync(http, ex.StatusCode, "HS-REQ-INVALID", "Request validation failed", ex.InnerException?.Message ?? ex.Message, null); }
        catch (JsonException ex) { await WriteAsync(http, 400, "HS-REQ-INVALID", "Request validation failed", ex.Message, null); }
        catch (OperationCanceledException) when (http.RequestAborted.IsCancellationRequested) { /* the client went away */ }
        catch (Exception ex)
        {
            log.LogError(ex, "Unhandled error for {Method} {Path}", http.Request.Method, http.Request.Path);
            await WriteAsync(http, 500, "HS-INTERNAL", "Internal error", env.IsDevelopment() ? ex.Message : "An unexpected error occurred", null);
        }
    }

    public static Problem Create(HttpContext http, int status, string code, string title, string? detail, List<FieldError>? errors) => new()
    {
        Type = "https://healthswap.example/problems/" + code.ToLowerInvariant(), Title = title, Status = status, Detail = detail, Code = code,
        Instance = http.Request.Path, TraceId = Activity.Current?.TraceId.ToString() ?? http.TraceIdentifier, Errors = errors,
    };

    public static async Task WriteAsync(HttpContext http, int status, string code, string title, string? detail, List<FieldError>? errors)
    {
        if (http.Response.HasStarted) return;
        // Headers set before the failure (rate-limit headers, WWW-Authenticate challenges) are kept on purpose.
        http.Response.StatusCode = status;
        http.Response.ContentType = "application/problem+json";
        await http.Response.WriteAsync(JsonSerializer.Serialize(Create(http, status, code, title, detail, errors), HsJson.Options));
    }
}

public sealed class RateLimitOptions
{
    public bool Enabled { get; set; } = true;
    public int CartEvaluationsPerSecond { get; set; } = 50;
    public int DefaultPerSecond { get; set; } = 20;
    public int BurstMultiplier { get; set; } = 2;
}

/// <summary>Token bucket per partner (or client IP) and route group with IETF RateLimit-* headers and 429 problems.</summary>
public sealed class RateLimitMiddleware(RequestDelegate next, RateLimitOptions options)
{
    private sealed class Bucket
    {
        public double Tokens;
        public long Stamp = Stopwatch.GetTimestamp();
        public readonly object Gate = new();
    }

    private readonly ConcurrentDictionary<string, Bucket> _buckets = new();

    public async Task InvokeAsync(HttpContext http)
    {
        var path = http.Request.Path.Value ?? "";
        if (!options.Enabled || !path.StartsWith("/v1/", StringComparison.Ordinal)) { await next(http); return; }
        var group = path.StartsWith("/v1/cart-evaluations", StringComparison.Ordinal) ? "eval" : "default";
        var rate = group == "eval" ? options.CartEvaluationsPerSecond : options.DefaultPerSecond;
        var capacity = rate * Math.Max(1, options.BurstMultiplier);
        var who = http.User.FindFirst("partner_id")?.Value ?? http.User.FindFirst("client_id")?.Value ?? http.Connection.RemoteIpAddress?.ToString() ?? "anonymous";
        var bucket = _buckets.GetOrAdd($"{who}:{group}", _ => new Bucket { Tokens = capacity });
        bool allowed;
        double remaining;
        lock (bucket.Gate)
        {
            var now = Stopwatch.GetTimestamp();
            bucket.Tokens = Math.Min(capacity, bucket.Tokens + Stopwatch.GetElapsedTime(bucket.Stamp, now).TotalSeconds * rate);
            bucket.Stamp = now;
            allowed = bucket.Tokens >= 1;
            if (allowed) bucket.Tokens -= 1;
            remaining = bucket.Tokens;
        }
        http.Response.Headers["RateLimit-Limit"] = capacity.ToString();
        http.Response.Headers["RateLimit-Remaining"] = ((int)Math.Floor(remaining)).ToString();
        http.Response.Headers["RateLimit-Reset"] = Math.Max(1, (int)Math.Ceiling((capacity - remaining) / rate)).ToString();
        if (!allowed)
        {
            http.Response.Headers.RetryAfter = "1";
            await ProblemMiddleware.WriteAsync(http, 429, "HS-RATE-LIMITED", "Too many requests", $"Limit is {rate} requests per second ({group})", null);
            return;
        }
        await next(http);
    }
}

public static class Http
{
    public static IResult Json(object body, int status = 200) => Results.Json(body, HsJson.Options, statusCode: status);

    /// <summary>
    /// Runs a resource-creating handler under the Idempotency-Key contract (HLD 8.2). The raw body was buffered by the
    /// request validation middleware so replays are compared byte-for-byte.
    /// </summary>
    public static async Task<IResult> Idempotent(HttpContext http, IdempotencyService idempotency, bool required, Func<Task<(int Status, object Body)>> handler)
    {
        var key = http.Request.Headers["Idempotency-Key"].FirstOrDefault();
        if (string.IsNullOrWhiteSpace(key))
        {
            if (required) throw HsException.Invalid("The Idempotency-Key header is required for this operation");
            var (s, b) = await handler();
            return Json(b, s);
        }
        var partner = http.Partner();
        var body = http.Items["raw-body"] as byte[] ?? [];
        var replayed = true;
        var response = await idempotency.ExecuteAsync(partner.PartnerId, key, http.Request.Method, http.Request.Path.Value ?? "", body, async () =>
        {
            replayed = false;
            var (s, b) = await handler();
            return new IdempotentResponse(s, JsonSerializer.Serialize(b, HsJson.Options));
        }, http.RequestAborted);
        if (replayed) http.Response.Headers["Idempotent-Replayed"] = "true";
        return Results.Text(response.Body, response.Status >= 400 ? "application/problem+json" : "application/json", statusCode: response.Status);
    }

    public static string BaseUrl(HttpRequest r) => $"{r.Scheme}://{r.Host}{r.PathBase}";
}
