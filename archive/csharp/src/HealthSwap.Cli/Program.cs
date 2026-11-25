using System.Diagnostics;
using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Platform;
using HealthSwap.Platform.Bus;
using HealthSwap.Platform.Data;
using HealthSwap.Platform.Knowledge;
using HealthSwap.Platform.Seeding;
using HealthSwap.Platform.Services;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

// hs - HealthSwap operations CLI. Run "hs help" for commands.
var argv = args.ToList();
string? Opt(string name, string? fallback = null)
{
    var i = argv.IndexOf("--" + name);
    return i >= 0 && i + 1 < argv.Count ? argv[i + 1] : fallback;
}
bool Flag(string name) => argv.Contains("--" + name);

if (argv.Count == 0 || argv[0] is "help" or "--help" or "-h")
{
    Console.WriteLine("""
        hs - HealthSwap operations CLI

          hs db wait [--timeout 60]                   wait until PostgreSQL (or the PGlite dev server) accepts connections
          hs seed [--demo]                            reference data + knowledge base (+ demo partners, catalogs, inventory) + scores + graphs + explanations
          hs kb ingest --source brand|off|fdc --file <path>
          hs kb recalls --file <openfda.json>
          hs kb score | build-graph | explain | rebuild
          hs jobs finalize                            RA-1 charge finalizer + FP-1 return-window finalizer
          hs billing month-close [--period YYYY-MM] [--partner <id>]
          hs ml export [--out ml/data]                training data for ml/ (features from the serving code)
          hs ml simulate [--partner <id>] [--sessions 2000] [--out ml/data/ranker_events.jsonl] [--seed 7]
          hs llm eval [--input ml/data/explanation_facts.jsonl] [--max 200]   run T5 through the production validator chain

        Configuration: appsettings.json next to the binary, environment variables (ConnectionStrings__HealthSwap, Llm__Provider,
        Llm__Endpoint, Llm__ApiKey, Llm__Deployments__T5, ...).
        """);
    return 0;
}

var builder = Host.CreateApplicationBuilder(new HostApplicationBuilderSettings { Args = [], ContentRootPath = AppContext.BaseDirectory });
builder.Configuration.AddJsonFile(Path.Combine(AppContext.BaseDirectory, "appsettings.json"), optional: true).AddEnvironmentVariables();
var repoRoot = builder.Configuration["RepoRoot"] ?? PlatformRegistration.FindRepoRoot(Directory.GetCurrentDirectory());
builder.Services.AddHealthSwapPlatform(builder.Configuration, repoRoot);
builder.Services.AddHealthSwapWorkers(includeScheduler: false, includeCacheWarmer: false, includeWebhooks: false);
builder.Logging.SetMinimumLevel(Flag("verbose") ? LogLevel.Debug : LogLevel.Information);
builder.Logging.AddFilter("Microsoft", LogLevel.Warning).AddFilter("System.Net.Http", LogLevel.Warning);
using var host = builder.Build();
var sp = host.Services;
var ct = CancellationToken.None;
var json = new JsonSerializerOptions(HsJson.Options) { WriteIndented = true };
void Print(object o) => Console.WriteLine(JsonSerializer.Serialize(o, json));

var command = string.Join(' ', argv.TakeWhile(a => !a.StartsWith("--", StringComparison.Ordinal)));
if (command == "db wait")
{
    var timeout = TimeSpan.FromSeconds(int.Parse(Opt("timeout", "60")!));
    var sw = Stopwatch.StartNew();
    while (true)
    {
        try
        {
            await using var c = await sp.GetRequiredService<Db>().OpenRawAsync(ct);
            await using var cmd = c.CreateCommand();
            cmd.CommandText = "select count(*) from information_schema.tables where table_schema = 'kb' and table_name = 'product'";
            if ((long)(await cmd.ExecuteScalarAsync(ct))! > 0) { Console.WriteLine($"Database ready ({sw.ElapsedMilliseconds} ms)"); return 0; }
        }
        catch (Exception ex) when (ex is Npgsql.NpgsqlException or System.Net.Sockets.SocketException or TimeoutException) { }
        if (sw.Elapsed > timeout) { Console.Error.WriteLine("Database not reachable or schema missing"); return 1; }
        await Task.Delay(1000);
    }
}

await host.StartAsync(ct);
try
{
    switch (command)
    {
        case "seed":
            Print((await sp.GetRequiredService<DemoSeeder>().SeedAsync(repoRoot, Flag("demo"), ct)).Steps);
            break;
        case "kb ingest":
        {
            var file = Opt("file") ?? throw new ArgumentException("--file is required");
            var now = sp.GetRequiredService<IClock>().UtcNow;
            var candidates = Opt("source", "brand") switch
            {
                "brand" => Connectors.BrandPortal(file, now).ToList(),
                "off" => Connectors.OpenFoodFacts(file, now).ToList(),
                "fdc" => Connectors.UsdaFdcBranded(file, now).ToList(),
                var s => throw new ArgumentException("Unknown source " + s),
            };
            Print(await sp.GetRequiredService<ProductIngestor>().IngestAsync(candidates, ct));
            break;
        }
        case "kb recalls":
            Print(new { matched = await sp.GetRequiredService<RecallIngestor>().IngestAsync(Connectors.OpenFdaEnforcement(Opt("file") ?? throw new ArgumentException("--file is required")).ToList(), ct) });
            break;
        case "kb score": Print(await sp.GetRequiredService<ScoringJob>().RunAllAsync(ct)); break;
        case "kb build-graph": Print(await sp.GetRequiredService<SwapGraphBuilder>().BuildAllAsync(ct)); break;
        case "kb explain": Print(await sp.GetRequiredService<ExplanationPregen>().RunAllAsync(ct)); break;
        case "kb rebuild":
            Print(new
            {
                scoring = await sp.GetRequiredService<ScoringJob>().RunAllAsync(ct),
                graphs = await sp.GetRequiredService<SwapGraphBuilder>().BuildAllAsync(ct),
                explanations = await sp.GetRequiredService<ExplanationPregen>().RunAllAsync(ct),
            });
            break;
        case "jobs finalize":
        {
            var (final, rejected) = await sp.GetRequiredService<RecoChargeService>().FinalizeDueAsync(ct);
            Print(new { recoCharges = new { final, rejected }, attributionsFinal = await sp.GetRequiredService<AttributionEngine>().FinalizeDueAsync(ct) });
            break;
        }
        case "billing month-close":
            Print(await sp.GetRequiredService<BillingService>().CloseMonthAsync(Opt("period") ?? Period.Of(DateTime.UtcNow), Opt("partner") is { } p ? Guid.Parse(p) : null, ct));
            break;
        case "ml export":
            Print(await sp.GetRequiredService<MlData>().ExportAsync(Opt("out") ?? Path.Combine(repoRoot, "ml", "data"), ct));
            break;
        case "ml simulate":
        {
            // The simulator reads the hot cache, so warm it from PostgreSQL first.
            await sp.GetRequiredService<CacheWarmer>().WarmAllAsync(ct);
            var partner = Guid.Parse(Opt("partner", "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01")!);
            var rows = await sp.GetRequiredService<MlData>().SimulateAsync(partner, int.Parse(Opt("sessions", "2000")!),
                Opt("out") ?? Path.Combine(repoRoot, "ml", "data", "ranker_events.jsonl"), int.Parse(Opt("seed", "7")!), ct);
            await sp.GetRequiredService<EvaluationRecorder>().FlushAsync(TimeSpan.FromSeconds(60));
            Print(new { rows });
            break;
        }
        case "llm eval":
        {
            var ml = sp.GetRequiredService<MlData>();
            var report = await ml.EvaluateExplanationsAsync(Opt("input") ?? Path.Combine(repoRoot, "ml", "data", "explanation_facts.jsonl"), int.Parse(Opt("max", "200")!), ct);
            Print(new { provider = ml.Provider, report });
            break;
        }
        default:
            Console.Error.WriteLine($"Unknown command '{command}'. Run 'hs help'.");
            return 2;
    }
    // Let the outbox relay publish what the command produced (lake files for ml/), then stop.
    await sp.GetRequiredService<OutboxRelay>().PumpAsync(ct);
    await Task.Delay(300);
}
finally
{
    await host.StopAsync(ct);
}
return 0;
