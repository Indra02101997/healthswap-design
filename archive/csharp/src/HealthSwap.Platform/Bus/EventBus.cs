using System.Text.Json;
using System.Threading.Channels;
using HealthSwap.Contracts;
using HealthSwap.Platform.Data;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;

namespace HealthSwap.Platform.Bus;

public static class Topics
{
    public const string CartEvaluations = "hs.cart-evaluations.v1";
    public const string SuggestionEvents = "hs.suggestion-events.v1";
    public const string Orders = "hs.orders.v1";
    public const string Attribution = "hs.attribution.v1";
    public const string RecoCharges = "hs.reco-charges.v1";
    public const string CatalogChanges = "hs.catalog-changes.v1";
    public const string InventoryUpdates = "hs.inventory-updates.v1";
    public const string KbProductChanges = "hs.kb-product-changes.v1";
    public const string Billing = "hs.billing.v1";
}

public sealed record BusMessage(long OutboxId, string Topic, string Key, JsonElement Payload, Guid? PartnerId, DateTime CreatedAt);

public interface IBusConsumer
{
    IReadOnlyCollection<string> Topics { get; }
    Task HandleAsync(BusMessage message, CancellationToken ct);
}

/// <summary>Writes domain events in the same transaction as the state change (transactional outbox).</summary>
public static class Outbox
{
    public static Task EnqueueAsync(UnitOfWork uow, Guid? partnerId, string topic, string key, object payload, CancellationToken ct = default) =>
        uow.ExecuteAsync("insert into txn.outbox (partner_id, topic, msg_key, payload) values (@partnerId, @topic, @key, @payload::jsonb)",
            new { partnerId, topic, key, payload = JsonSerializer.Serialize(payload, HsJson.Options) }, ct);
}

/// <summary>
/// Kafka-API stand-in for local development: an in-process bus fed by the outbox relay. Production replaces the relay's
/// publish step with a Kafka/Event Hubs producer; consumers keep the same contract (at-least-once, dedupe by key).
/// </summary>
public sealed class InProcessEventBus(IEnumerable<IBusConsumer> consumers, ILogger<InProcessEventBus> log) : BackgroundService
{
    private readonly Channel<BusMessage> _channel = Channel.CreateBounded<BusMessage>(new BoundedChannelOptions(10_000) { FullMode = BoundedChannelFullMode.Wait });
    private readonly IReadOnlyList<IBusConsumer> _consumers = consumers.ToList();

    public ValueTask PublishAsync(BusMessage message, CancellationToken ct) => _channel.Writer.WriteAsync(message, ct);

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await foreach (var m in _channel.Reader.ReadAllAsync(stoppingToken))
            foreach (var c in _consumers.Where(c => c.Topics.Contains(m.Topic) || c.Topics.Contains("*")))
            {
                try { await c.HandleAsync(m, stoppingToken); }
                catch (Exception ex) when (ex is not OperationCanceledException) { log.LogError(ex, "Consumer {Consumer} failed for {Topic}/{Key}", c.GetType().Name, m.Topic, m.Key); }
            }
    }
}

/// <summary>Relays unpublished outbox rows to the bus (hs_ops role, FOR UPDATE SKIP LOCKED so replicas can run in parallel).</summary>
public sealed class OutboxRelay(Db db, InProcessEventBus bus, ILogger<OutboxRelay> log) : BackgroundService
{
    private sealed class Row
    {
        public long OutboxId { get; set; }
        public Guid? PartnerId { get; set; }
        public string Topic { get; set; } = "";
        public string MsgKey { get; set; } = "";
        public string Payload { get; set; } = "{}";
        public DateTime CreatedAt { get; set; }
    }

    public async Task<int> PumpAsync(CancellationToken ct)
    {
        await using var uow = await db.BeginAsync(DbRole.Ops, null, ct);
        var rows = (await uow.QueryAsync<Row>(
            "select outbox_id, partner_id, topic, msg_key, payload::text as payload, created_at from txn.outbox where published_at is null order by outbox_id limit 500 for update skip locked",
            ct: ct)).ToList();
        foreach (var r in rows)
            await bus.PublishAsync(new BusMessage(r.OutboxId, r.Topic, r.MsgKey, JsonDocument.Parse(r.Payload).RootElement.Clone(), r.PartnerId, r.CreatedAt), ct);
        if (rows.Count > 0)
            await uow.ExecuteAsync("update txn.outbox set published_at = now() where outbox_id = any(@ids)", new { ids = rows.Select(r => r.OutboxId).ToArray() }, ct);
        await uow.CommitAsync(ct);
        return rows.Count;
    }

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        while (!stoppingToken.IsCancellationRequested)
        {
            try
            {
                var n = await PumpAsync(stoppingToken);
                await Task.Delay(n > 0 ? 50 : 500, stoppingToken);
            }
            catch (OperationCanceledException) { break; }
            catch (Exception ex)
            {
                log.LogWarning(ex, "Outbox relay iteration failed");
                await Task.Delay(2000, stoppingToken);
            }
        }
    }
}

/// <summary>Bronze layer of the lakehouse: immutable JSONL files per topic and day (training data source for ml/).</summary>
public sealed class LakeSink(LakeOptions options) : IBusConsumer
{
    private readonly SemaphoreSlim _gate = new(1, 1);
    public IReadOnlyCollection<string> Topics { get; } = ["*"];

    public async Task HandleAsync(BusMessage m, CancellationToken ct)
    {
        if (!options.Enabled) return;
        var dir = Path.Combine(options.Root, "bronze", m.Topic, "dt=" + m.CreatedAt.ToString("yyyy-MM-dd", System.Globalization.CultureInfo.InvariantCulture));
        Directory.CreateDirectory(dir);
        var line = JsonSerializer.Serialize(new { outboxId = m.OutboxId, topic = m.Topic, key = m.Key, partnerId = m.PartnerId, createdAt = m.CreatedAt, payload = m.Payload }, HsJson.Options);
        await _gate.WaitAsync(ct);
        try { await File.AppendAllTextAsync(Path.Combine(dir, "part-0000.jsonl"), line + "\n", ct); }
        finally { _gate.Release(); }
    }
}

public sealed class LakeOptions
{
    public bool Enabled { get; set; } = true;
    public string Root { get; set; } = Path.Combine(AppContext.BaseDirectory, ".data", "lake");
}
