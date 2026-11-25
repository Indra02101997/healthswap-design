using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Platform.Data;
using HealthSwap.Platform.Knowledge;
using HealthSwap.Platform.Services;
using Microsoft.Extensions.Logging;
using Money = HealthSwap.Contracts.Money;

namespace HealthSwap.Platform.Seeding;

public sealed record SeedReport(Dictionary<string, object> Steps);

/// <summary>
/// Builds a complete local environment from versioned files: reference data, the synthetic knowledge base (brand portal,
/// Open Food Facts and USDA FDC fixtures, an openFDA recall), demo partners, catalogs, inventory, scores, swap graphs and
/// explanations. Idempotent: safe to re-run.
/// </summary>
public sealed class DemoSeeder(Db db, ReferenceDataLoader references, ReferenceDataRepository refs, ProductIngestor ingestor, RecallIngestor recalls, ScoringJob scoring,
    SwapGraphBuilder graphs, ExplanationPregen explanations, CatalogService catalog, CatalogEnrichment enrichment, InventoryService inventory, SecretProtector secrets,
    PartnerDirectory partners, IClock clock, ILogger<DemoSeeder> log)
{
    private sealed record ClientFile(string ClientId, string Secret, List<string> Scopes);
    private sealed record WebhookFile(string Url, string Secret, List<string>? EventTypes);
    private sealed record StoreFile(string StoreId, string Market, string? PostalCode, string Timezone);
    private sealed record PartnerFile(Guid PartnerId, string DisplayName, string LegalName, string Geography, List<string> Markets, string Status, List<ClientFile> Clients,
        WebhookFile? Webhook, JsonElement Config, List<StoreFile> Stores, string? CatalogFile, string? InventoryFile);
    private sealed record PartnersFile(List<PartnerFile> Partners);
    private sealed record CatalogFile(List<CatalogItem> Items);
    private sealed record InventoryRow(string PartnerSku, bool InStock, string Price, string? Mrp, int? AvailableQuantity);
    private sealed record InventoryFile(Dictionary<string, List<InventoryRow>> Stores);

    public async Task<SeedReport> SeedAsync(string repoRoot, bool demoTenants, CancellationToken ct)
    {
        var steps = new Dictionary<string, object>();
        var now = clock.UtcNow;
        await references.ApplyAllAsync(repoRoot, ct);
        refs.Invalidate();
        steps["referenceData"] = "applied";

        var kb = Path.Combine(repoRoot, "data", "seed", "kb", "products.json");
        steps["brandPortal"] = Summ(await ingestor.IngestAsync(Connectors.BrandPortal(kb, now).ToList(), ct));
        var fixtures = Path.Combine(repoRoot, "data", "fixtures");
        if (File.Exists(Path.Combine(fixtures, "fdc-branded-sample.json")))
            steps["usdaFdc"] = Summ(await ingestor.IngestAsync(Connectors.UsdaFdcBranded(Path.Combine(fixtures, "fdc-branded-sample.json"), now).ToList(), ct));
        if (File.Exists(Path.Combine(fixtures, "off-sample.jsonl")))
            steps["openFoodFacts"] = Summ(await ingestor.IngestAsync(Connectors.OpenFoodFacts(Path.Combine(fixtures, "off-sample.jsonl"), now).ToList(), ct));
        if (File.Exists(Path.Combine(fixtures, "openfda-enforcement-sample.json")))
            steps["recalls"] = await recalls.IngestAsync(Connectors.OpenFdaEnforcement(Path.Combine(fixtures, "openfda-enforcement-sample.json")).ToList(), ct);

        steps["scoring"] = await scoring.RunAllAsync(ct);

        if (demoTenants)
        {
            var file = JsonSerializer.Deserialize<PartnersFile>(await File.ReadAllTextAsync(Path.Combine(repoRoot, "data", "seed", "demo", "partners.json"), ct), HsJson.Options)!;
            foreach (var p in file.Partners)
            {
                await UpsertPartnerAsync(p, ct);
                partners.Invalidate(p.PartnerId);
                if (p.CatalogFile is { } cf)
                {
                    var items = JsonSerializer.Deserialize<CatalogFile>(await File.ReadAllTextAsync(Path.Combine(repoRoot, "data", "seed", "demo", cf), ct), HsJson.Options)!.Items;
                    var outcome = await catalog.UpsertAsync(p.PartnerId, items, null, ct);
                    await enrichment.EnrichAsync(p.PartnerId, outcome.NeedsEnrichment, ct);
                    steps[$"catalog:{p.DisplayName}"] = new
                    {
                        outcome.Result.Accepted, outcome.Result.Rejected,
                        states = outcome.Result.Results.GroupBy(r => r.Match?.State ?? r.Status).ToDictionary(g => g.Key, g => g.Count()),
                    };
                }
                if (p.InventoryFile is { } inf)
                {
                    var inv = JsonSerializer.Deserialize<InventoryFile>(await File.ReadAllTextAsync(Path.Combine(repoRoot, "data", "seed", "demo", inf), ct), HsJson.Options)!;
                    var currency = p.Config.GetProperty("currency").GetString()!;
                    foreach (var (storeId, rows) in inv.Stores)
                    {
                        var ack = await inventory.UpsertAsync(p.PartnerId, storeId, new InventoryBatchRequest(now, "FULL_SNAPSHOT", rows.Select(r => new InventoryEntry
                        {
                            PartnerSku = r.PartnerSku, InStock = r.InStock, AvailableQuantity = r.AvailableQuantity, Price = new Money(r.Price, currency),
                            Mrp = r.Mrp is null ? null : new Money(r.Mrp, currency),
                        }).ToList()), ct);
                        steps[$"inventory:{p.DisplayName}:{storeId}"] = new { ack.Accepted, ack.Rejected };
                    }
                }
            }
        }

        steps["swapGraphs"] = await graphs.BuildAllAsync(ct);
        steps["explanations"] = await explanations.RunAllAsync(ct);
        log.LogInformation("Seed complete");
        return new SeedReport(steps);
    }

    private static object Summ(IngestReport r) => new { r.Received, r.Created, r.NewVersions, r.Unchanged, r.Rejected, r.CurationTasks };

    private Task UpsertPartnerAsync(PartnerFile p, CancellationToken ct) => db.InTxAsync(DbRole.Ops, null, async u =>
    {
        await u.ExecuteAsync(
            """
            insert into tenant.partner (partner_id, display_name, legal_name, geography, markets, status, config)
            values (@PartnerId, @DisplayName, @LegalName, @Geography, @markets, @Status, @config::jsonb)
            on conflict (partner_id) do update set display_name = excluded.display_name, legal_name = excluded.legal_name, geography = excluded.geography,
                markets = excluded.markets, status = excluded.status, config = excluded.config
            """, new { p.PartnerId, p.DisplayName, p.LegalName, p.Geography, markets = p.Markets.ToArray(), p.Status, config = p.Config.GetRawText() }, ct);
        foreach (var c in p.Clients)
            await u.ExecuteAsync(
                """
                insert into tenant.api_client (client_id, partner_id, scopes, status) values (@ClientId, @partnerId, @scopes, 'ACTIVE')
                on conflict (client_id) do update set partner_id = excluded.partner_id, scopes = excluded.scopes, status = 'ACTIVE'
                """, new { c.ClientId, partnerId = p.PartnerId, scopes = c.Scopes.ToArray() }, ct);
        foreach (var s in p.Stores)
            await u.ExecuteAsync(
                """
                insert into tenant.store (partner_id, store_id, market, postal_code, timezone) values (@partnerId, @StoreId, @Market, @PostalCode, @Timezone)
                on conflict (partner_id, store_id) do update set market = excluded.market, postal_code = excluded.postal_code, timezone = excluded.timezone, is_active = true
                """, new { partnerId = p.PartnerId, s.StoreId, s.Market, s.PostalCode, s.Timezone }, ct);
        if (p.Webhook is { } w)
        {
            await u.ExecuteAsync("delete from tenant.webhook_endpoint where partner_id = @partnerId and url = @url", new { partnerId = p.PartnerId, url = w.Url }, ct);
            await u.ExecuteAsync(
                "insert into tenant.webhook_endpoint (partner_id, url, secret_ciphertext, event_types) values (@partnerId, @url, @secret, @types)",
                new { partnerId = p.PartnerId, url = w.Url, secret = secrets.Protect(w.Secret), types = (w.EventTypes ?? []).ToArray() }, ct);
        }
        await u.ExecuteAsync("insert into audit.change_log (actor, action, entity_type, entity_id, details) values ('seed', 'PARTNER_UPSERT', 'PARTNER', @id, '{}'::jsonb)",
            new { id = p.PartnerId.ToString() }, ct);
    }, ct);
}
