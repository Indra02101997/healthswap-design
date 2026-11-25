using System.Diagnostics;
using System.Net;
using System.Net.Http.Headers;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using HealthSwap.Api;
using HealthSwap.Platform;
using HealthSwap.Platform.Seeding;
using HealthSwap.Platform.Services;
using Microsoft.AspNetCore.Hosting;
using Microsoft.AspNetCore.Hosting.Server;
using Microsoft.AspNetCore.Mvc.Testing;
using Microsoft.AspNetCore.TestHost;
using Microsoft.Extensions.DependencyInjection;

[assembly: AssemblyFixture(typeof(HealthSwap.Api.Tests.ApiFixture))]
[assembly: CollectionBehavior(DisableTestParallelization = true)]

namespace HealthSwap.Api.Tests;

/// <summary>
/// Starts the PGlite development database (tools/devdb, in memory, random port), hosts the API in-process, seeds the synthetic
/// knowledge base and demo tenants once per test run, and warms the hot cache. Requires Node.js 20+ with `npm ci` in tools/devdb.
/// </summary>
public sealed class ApiFixture : IAsyncLifetime
{
    public const string QuickBasket = "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01";
    public const string Hometown = "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e02";
    private Process? _devdb;
    private string _temp = "";
    public string? SkipReason { get; private set; }
    public WebApplicationFactory<Program> Factory { get; private set; } = null!;
    public string RepoRoot { get; } = PlatformRegistration.FindRepoRoot(AppContext.BaseDirectory);

    public async ValueTask InitializeAsync()
    {
        var devdb = Path.Combine(RepoRoot, "tools", "devdb");
        if (!Directory.Exists(Path.Combine(devdb, "node_modules"))) { SkipReason = "Run `npm ci` in tools/devdb first (PGlite dev database)."; return; }
        var port = FreePort();
        _temp = Path.Combine(Path.GetTempPath(), "healthswap-tests-" + Guid.NewGuid().ToString("N")[..8]);
        Directory.CreateDirectory(_temp);
        var psi = new ProcessStartInfo("node", $"server.mjs --memory --port {port}") { WorkingDirectory = devdb, RedirectStandardOutput = true, RedirectStandardError = true, UseShellExecute = false };
        try { _devdb = Process.Start(psi)!; }
        catch (System.ComponentModel.Win32Exception) { SkipReason = "Node.js is not installed"; return; }
        var ready = new TaskCompletionSource();
        _devdb.OutputDataReceived += (_, e) => { if (e.Data?.StartsWith("DEVDB READY", StringComparison.Ordinal) == true) ready.TrySetResult(); };
        _devdb.ErrorDataReceived += (_, e) => { if (e.Data is { Length: > 0 }) Console.Error.WriteLine("devdb: " + e.Data); };
        _devdb.BeginOutputReadLine();
        _devdb.BeginErrorReadLine();
        if (await Task.WhenAny(ready.Task, Task.Delay(TimeSpan.FromSeconds(90))) != ready.Task) throw new TimeoutException("devdb did not start");

        var settings = new Dictionary<string, string>
        {
            ["ConnectionStrings:HealthSwap"] = $"Host=127.0.0.1;Port={port};Username=postgres;Password=postgres;Database=postgres;SSL Mode=Disable;Maximum Pool Size=16;Timeout=30;Command Timeout=120",
            ["RepoRoot"] = RepoRoot,
            ["Scheduler:Enabled"] = "false",
            ["Cache:WarmOnStartup"] = "false",
            ["RateLimits:Enabled"] = "false",
            ["Lake:Enabled"] = "false",
            ["Llm:MockFaultEvery"] = "5",
            ["Webhooks:AllowHttpLocalhost"] = "true",
            ["DataProtection:KeysDirectory"] = Path.Combine(_temp, "keys"),
            ["Auth:SigningKeyFile"] = Path.Combine(_temp, "jwt.pem"),
            ["Feeds:StorageRoot"] = Path.Combine(_temp, "feeds"),
            ["Lake:Root"] = Path.Combine(_temp, "lake"),
        };
        // Program.cs reads some values before Build(); environment variables are visible to WebApplication.CreateBuilder.
        foreach (var (k, v) in settings) Environment.SetEnvironmentVariable(k.Replace(":", "__"), v);
        Factory = new WebApplicationFactory<Program>().WithWebHostBuilder(b =>
        {
            b.UseEnvironment("Development");
            foreach (var (k, v) in settings) b.UseSetting(k, v);
            // Deliver webhooks into the in-process server so the demo receiver (/demo/webhooks/{name}) verifies the signatures.
            b.ConfigureServices(s => s.AddHttpClient("webhooks").ConfigurePrimaryHttpMessageHandler(sp => ((TestServer)sp.GetRequiredService<IServer>()).CreateHandler()));
        });
        _ = Factory.CreateClient(); // builds the host
        await Factory.Services.GetRequiredService<DemoSeeder>().SeedAsync(RepoRoot, true, CancellationToken.None);
        await Factory.Services.GetRequiredService<CacheWarmer>().WarmAllAsync(CancellationToken.None);
    }

    public async ValueTask DisposeAsync()
    {
        if (Factory is not null) await Factory.DisposeAsync();
        if (_devdb is { HasExited: false }) { _devdb.Kill(entireProcessTree: true); await _devdb.WaitForExitAsync(); }
        try { if (_temp.Length > 0) Directory.Delete(_temp, true); } catch (IOException) { }
    }

    public void SkipIfUnavailable() { if (SkipReason is not null) Assert.Skip(SkipReason); }

    public async Task<HttpClient> ClientAsync(string clientId = "quickbasket-server", string secret = "dev-only-quickbasket-secret-change-me", string? scope = null)
    {
        var anonymous = Factory.CreateClient();
        var form = new Dictionary<string, string> { ["grant_type"] = "client_credentials", ["client_id"] = clientId, ["client_secret"] = secret };
        if (scope is not null) form["scope"] = scope;
        var res = await anonymous.PostAsync("/oauth2/token", new FormUrlEncodedContent(form));
        res.EnsureSuccessStatusCode();
        var token = JsonDocument.Parse(await res.Content.ReadAsStringAsync()).RootElement.GetProperty("access_token").GetString();
        var client = Factory.CreateClient();
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        return client;
    }

    public Task<HttpClient> AdminAsync() => ClientAsync("hs-admin", "dev-only-admin-secret-change-me");
    public Task<HttpClient> HometownAsync() => ClientAsync("hometown-server", "dev-only-hometown-secret-change-me");

    private static int FreePort()
    {
        var l = new TcpListener(IPAddress.Loopback, 0);
        l.Start();
        var port = ((IPEndPoint)l.LocalEndpoint).Port;
        l.Stop();
        return port;
    }
}

public static class Contract
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web) { DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull };

    public static StringContent Body(object o) => new(JsonSerializer.Serialize(o, Json), Encoding.UTF8, "application/json");

    /// <summary>Asserts the status code and validates the body against the OpenAPI response schema of the operation.</summary>
    public static async Task<JsonElement> Expect(this HttpResponseMessage res, string operationId, int status)
    {
        var body = await res.Content.ReadAsStringAsync();
        Assert.True((int)res.StatusCode == status, $"{operationId}: expected {status}, got {(int)res.StatusCode}: {body}");
        if (body.Length == 0) return default;
        var json = JsonDocument.Parse(body).RootElement.Clone();
        if (OpenApiContract.Load().ResponseSchema(operationId, status) is { } schema)
        {
            var errors = OpenApiContract.Validate(schema, json);
            Assert.True(errors.Count == 0, $"{operationId} {status} violates the contract: {string.Join("; ", errors.Select(e => e.Field + ": " + e.Message))}\n{body}");
        }
        return json;
    }

    public static string Now(int seconds = 0) => DateTime.UtcNow.AddSeconds(seconds).ToString("o");
}
