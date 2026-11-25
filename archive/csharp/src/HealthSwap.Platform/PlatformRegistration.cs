using HealthSwap.Domain;
using HealthSwap.Platform.Bus;
using HealthSwap.Platform.Cache;
using HealthSwap.Platform.Data;
using HealthSwap.Platform.Knowledge;
using HealthSwap.Platform.Llm;
using HealthSwap.Platform.Ml;
using HealthSwap.Platform.Services;
using Microsoft.AspNetCore.DataProtection;
using Microsoft.Extensions.Configuration;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Logging;
using StackExchange.Redis;

namespace HealthSwap.Platform;

/// <summary>Shared composition root for the API, the CLI and the integration tests.</summary>
public static class PlatformRegistration
{
    public static string FindRepoRoot(string? start = null)
    {
        var dir = new DirectoryInfo(start ?? AppContext.BaseDirectory);
        while (dir is not null && !File.Exists(Path.Combine(dir.FullName, "db", "schema.sql"))) dir = dir.Parent;
        return dir?.FullName ?? Directory.GetCurrentDirectory();
    }

    public static IServiceCollection AddHealthSwapPlatform(this IServiceCollection services, IConfiguration config, string? repoRoot = null)
    {
        var root = config["RepoRoot"] ?? repoRoot ?? FindRepoRoot();
        T Bind<T>(string section) where T : new()
        {
            var o = new T();
            config.GetSection(section).Bind(o);
            services.AddSingleton(typeof(T), o!);
            return o;
        }

        var dbOptions = Bind<DbOptions>("Database");
        if (config.GetConnectionString("HealthSwap") is { Length: > 0 } cs) dbOptions.ConnectionString = cs;
        var llm = Bind<LlmOptions>("Llm");
        var modelOptions = Bind<ModelOptions>("Models");
        if (config["Models:Directory"] is null) modelOptions.Directory = Path.Combine(root, "models");
        var lake = Bind<LakeOptions>("Lake");
        if (config["Lake:Root"] is null) lake.Root = Path.Combine(root, ".data", "lake");
        Bind<GraphOptions>("Graph");
        Bind<EvaluationOptions>("Evaluation");
        Bind<WebhookOptions>("Webhooks");
        var feeds = Bind<FeedOptions>("Feeds");
        if (config["Feeds:StorageRoot"] is null) feeds.StorageRoot = Path.Combine(root, ".data", "feeds");
        Bind<SchedulerOptions>("Scheduler");

        services.AddDataProtection().SetApplicationName("HealthSwap")
            .PersistKeysToFileSystem(new DirectoryInfo(config["DataProtection:KeysDirectory"] ?? Path.Combine(root, ".data", "keys")));

        var clock = new OffsetClock();
        services.AddSingleton(clock);
        services.AddSingleton<IClock>(clock);
        services.AddSingleton<Db>();

        if (config["Cache:Redis"] is { Length: > 0 } redis)
        {
            services.AddSingleton<IConnectionMultiplexer>(_ => ConnectionMultiplexer.Connect(redis));
            services.AddSingleton<ICacheStore, RedisCacheStore>();
        }
        else services.AddSingleton<ICacheStore, MemoryCacheStore>();
        services.AddSingleton<HotCache>();

        services.AddSingleton<PartnerDirectory>();
        services.AddSingleton<ReferenceDataRepository>();
        services.AddSingleton<ReferenceDataLoader>();
        services.AddSingleton<KbReader>();
        services.AddSingleton<RecallIngestor>();
        services.AddSingleton<ProductIngestor>();
        services.AddSingleton<ScoringJob>();
        services.AddSingleton<SwapGraphBuilder>();
        services.AddSingleton<ExplanationPregen>();
        services.AddSingleton<ModelRegistry>();

        services.AddHttpClient("azure-openai", c => c.Timeout = TimeSpan.FromSeconds(60));
        services.AddHttpClient("webhooks", c => c.Timeout = TimeSpan.FromSeconds(15))
            .ConfigurePrimaryHttpMessageHandler(() => new HttpClientHandler { AllowAutoRedirect = false });
        services.AddSingleton<ILlmProvider>(sp => llm.Provider.Equals("AzureOpenAI", StringComparison.OrdinalIgnoreCase)
            ? new AzureOpenAiProvider(sp.GetRequiredService<IHttpClientFactory>().CreateClient("azure-openai"), llm)
            : new MockLlmProvider(llm));
        services.AddSingleton<LlmGateway>();

        services.AddSingleton<SecretProtector>();
        services.AddSingleton<LedgerService>();
        services.AddSingleton<IdempotencyService>();
        services.AddSingleton<CatalogService>();
        services.AddSingleton<CatalogEnrichment>();
        services.AddSingleton<FeedService>();
        services.AddSingleton<InventoryService>();
        services.AddSingleton<PreferenceService>();
        services.AddSingleton<PrivacyService>();
        services.AddSingleton<EvaluationRecorder>();
        services.AddSingleton<EvaluationService>();
        services.AddSingleton<SuggestionStore>();
        services.AddSingleton<RecoChargeService>();
        services.AddSingleton<EventService>();
        services.AddSingleton<AttributionEngine>();
        services.AddSingleton<OrderService>();
        services.AddSingleton<BillingService>();
        services.AddSingleton<ProductService>();
        services.AddSingleton<TransparencyService>();
        services.AddSingleton<CacheWarmer>();
        services.AddSingleton<Seeding.DemoSeeder>();
        services.AddSingleton<Seeding.MlData>();

        services.AddSingleton<WebhookFanoutConsumer>();
        services.AddSingleton<IBusConsumer>(sp => sp.GetRequiredService<WebhookFanoutConsumer>());
        services.AddSingleton<IBusConsumer, LakeSink>();
        services.AddSingleton<InProcessEventBus>();
        services.AddSingleton<OutboxRelay>();
        services.AddSingleton<WebhookDispatcher>();
        services.AddSingleton<FinalizerWorker>();
        services.AddLogging(b => b.AddSimpleConsole(o => { o.SingleLine = true; o.TimestampFormat = "HH:mm:ss "; }));
        return services;
    }

    /// <summary>Background workers: outbox relay + in-process bus, evaluation recorder, webhook dispatcher, finalizers, cache warmer.</summary>
    public static IServiceCollection AddHealthSwapWorkers(this IServiceCollection services, bool includeScheduler = true, bool includeCacheWarmer = true, bool includeWebhooks = true)
    {
        services.AddHostedService(sp => sp.GetRequiredService<InProcessEventBus>());
        services.AddHostedService(sp => sp.GetRequiredService<OutboxRelay>());
        services.AddHostedService(sp => sp.GetRequiredService<EvaluationRecorder>());
        if (includeWebhooks) services.AddHostedService(sp => sp.GetRequiredService<WebhookDispatcher>());
        if (includeScheduler) services.AddHostedService(sp => sp.GetRequiredService<FinalizerWorker>());
        if (includeCacheWarmer) services.AddHostedService(sp => sp.GetRequiredService<CacheWarmer>());
        return services;
    }
}
