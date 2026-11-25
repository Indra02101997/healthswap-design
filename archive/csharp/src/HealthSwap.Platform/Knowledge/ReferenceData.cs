using System.Collections.Concurrent;
using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain.Knowledge;
using HealthSwap.Domain.Scoring;
using HealthSwap.Platform.Data;

namespace HealthSwap.Platform.Knowledge;

// ---------- Reference-data file formats (data/packs, data/seed/kb) ----------

public sealed record PackDocument(string Citation, string Authority, string Title, string? Url, string? EffectiveFrom, string? Jurisdiction = null);

public sealed record PackFile
{
    public required string Market { get; init; }
    public required string PackVersion { get; init; }
    public string Status { get; init; } = "PREVIEW";
    public string? Verification { get; init; }
    public required string PrimaryModel { get; init; }
    public string MethodologyUrl { get; init; } = "";
    public List<PackDocument> Documents { get; init; } = [];
    public List<string> AllergenRules { get; init; } = [];
    public HsiParams Hsi { get; init; } = new();
    public RulesParams Rules { get; init; } = new();
    public List<FopThreshold> Fop { get; init; } = [];
    public Dictionary<string, decimal> DailyValues { get; init; } = [];
    public List<ClaimThreshold> Claims { get; init; } = [];
    public List<UsHealthyFile> UsHealthy { get; init; } = [];
    public List<string> Models { get; init; } = [];
}

public sealed record UsHealthyFile(string CategoryPrefix, string Group, decimal AddedSugarsMaxG, decimal SodiumMaxMg, decimal SatFatMaxG, string? RequiredFlag);

public sealed record AdditiveStatusFile(string Jurisdiction, string Status, string EffectiveFrom, string? EffectiveTo, string? Citation);

public sealed record AdditiveHazardFile(string Authority, string Finding, string Tier, string AsOf, string? Citation);

public sealed record AdditiveFile
{
    public required string Code { get; init; }
    public required string Name { get; init; }
    public List<string> Classes { get; init; } = [];
    public List<string> Aliases { get; init; } = [];
    public List<AdditiveStatusFile> Statuses { get; init; } = [];
    public List<AdditiveHazardFile> Hazards { get; init; } = [];
}

public sealed record AdditivesFile(string Version, List<PackDocument> Documents, List<AdditiveFile> Additives);

public sealed record CategoryFile(string Code, string Name, string PhysicalState, string ScoringCategory, string? GpcBrick = null);

public sealed record SubstitutionMapFile(List<string> Markets, string From, string To, string ApprovedBy, string? Notes);

public sealed record CategoriesFile(string Version, List<CategoryFile> Categories, List<SubstitutionMapFile> SubstitutionMaps);

public sealed record IngredientFile(string Name, List<string> Flags, List<string> Allergens, Dictionary<string, List<string>> Aliases);

public sealed record IngredientsFile(string Version, List<IngredientFile> Ingredients);

public static class RefJson
{
    public static readonly JsonSerializerOptions Options = HsJson.Create();
    public static T Read<T>(string path) => JsonSerializer.Deserialize<T>(File.ReadAllText(path), Options) ?? throw new InvalidDataException("Empty file " + path);
}

/// <summary>
/// Applies versioned reference data (jurisdiction packs, additive master, taxonomy, categories) to reg.*, kb.* and
/// rec.scoring_model. This is the "pack PR" path in doc 02 section 8: files reviewed in source control, loaded idempotently.
/// </summary>
public sealed class ReferenceDataLoader(Db db)
{
    public async Task ApplyAllAsync(string repoRoot, CancellationToken ct = default)
    {
        var packsDir = Path.Combine(repoRoot, "data", "packs");
        await ApplyAdditivesAsync(RefJson.Read<AdditivesFile>(Path.Combine(packsDir, "additives.json")), ct);
        foreach (var modelPath in Directory.GetFiles(Path.Combine(packsDir, "models"), "*.json"))
            await ApplyModelAsync(RefJson.Read<PointsModelParams>(modelPath), File.ReadAllText(modelPath), ct);
        foreach (var packPath in Directory.GetFiles(packsDir, "??.json"))
            await ApplyPackAsync(RefJson.Read<PackFile>(packPath), ct);
        await ApplyTaxonomyAsync(RefJson.Read<IngredientsFile>(Path.Combine(repoRoot, "data", "seed", "kb", "ingredients.json")), ct);
        await ApplyCategoriesAsync(RefJson.Read<CategoriesFile>(Path.Combine(repoRoot, "data", "seed", "kb", "categories.json")), ct);
    }

    private static async Task<Guid?> UpsertDocumentAsync(UnitOfWork u, PackDocument d, string jurisdiction, CancellationToken ct) =>
        await u.QuerySingleAsync<Guid>(
            """
            insert into reg.document (jurisdiction, authority, title, citation, url, effective_from)
            values (@jurisdiction, @authority, @title, @citation, @url, @effectiveFrom::date)
            on conflict (citation) do update set title = excluded.title, url = coalesce(excluded.url, reg.document.url)
            returning document_id
            """,
            new { jurisdiction = d.Jurisdiction ?? jurisdiction, authority = d.Authority, title = d.Title, citation = d.Citation, url = d.Url, effectiveFrom = d.EffectiveFrom }, ct);

    public Task ApplyAdditivesAsync(AdditivesFile file, CancellationToken ct = default) => db.InTxAsync(DbRole.KbWriter, null, async u =>
    {
        var docs = new Dictionary<string, Guid?>();
        foreach (var d in file.Documents) docs[d.Citation] = await UpsertDocumentAsync(u, d, d.Jurisdiction ?? "EU", ct);
        foreach (var a in file.Additives)
        {
            var usNames = a.Aliases.Where(x => x.StartsWith("FD&C", StringComparison.Ordinal) || char.IsUpper(x[0])).ToArray();
            var id = await u.QuerySingleAsync<Guid>(
                """
                insert into kb.additive (ins_number, e_number, canonical_name, us_names, functional_classes)
                values (@code, @eNumber, @name, @usNames, @classes)
                on conflict (ins_number) do update set canonical_name = excluded.canonical_name, us_names = excluded.us_names,
                    functional_classes = excluded.functional_classes
                returning additive_id
                """,
                new { code = a.Code, eNumber = a.Code == "924a" ? null : "E" + a.Code, name = a.Name, usNames, classes = a.Classes.ToArray() }, ct);
            foreach (var alias in a.Aliases.Append(a.Name).Distinct(StringComparer.OrdinalIgnoreCase))
                await u.ExecuteAsync("insert into kb.additive_alias (alias, lang, additive_id) values (@alias, 'en', @id) on conflict (alias, lang) do update set additive_id = excluded.additive_id",
                    new { alias = IngredientTaxonomy.Normalize(alias), id }, ct);
            foreach (var s in a.Statuses)
                await u.ExecuteAsync(
                    """
                    insert into reg.additive_status (additive_id, jurisdiction, status, effective_from, effective_to, document_id)
                    values (@id, @j, @status, @from::date, @to::date, @doc)
                    on conflict (additive_id, jurisdiction, effective_from) do update set status = excluded.status, effective_to = excluded.effective_to
                    """,
                    new { id, j = s.Jurisdiction, status = s.Status, from = s.EffectiveFrom, to = s.EffectiveTo, doc = s.Citation is null ? null : docs.GetValueOrDefault(s.Citation) }, ct);
            foreach (var h in a.Hazards)
                await u.ExecuteAsync(
                    """
                    insert into reg.additive_hazard (additive_id, authority, finding, concern_tier, as_of, document_id)
                    values (@id, @authority, @finding, @tier, @asOf::date, @doc)
                    on conflict (additive_id, authority, as_of) do update set finding = excluded.finding, concern_tier = excluded.concern_tier
                    """,
                    new { id, authority = h.Authority, finding = h.Finding, tier = h.Tier, asOf = h.AsOf, doc = h.Citation is null ? null : docs.GetValueOrDefault(h.Citation) }, ct);
        }
    }, ct);

    public Task ApplyModelAsync(PointsModelParams model, string rawJson, CancellationToken ct = default) => db.InTxAsync(DbRole.KbWriter, null, async u =>
    {
        var jurisdiction = model.Code switch { "IN_INR_DRAFT" => "IN", "NUTRISCORE_2023" => "EU", "UK_NPM_2004" => "GB", "HSR" => "AU", _ => null };
        await u.ExecuteAsync("update rec.scoring_model set status = 'DEPRECATED' where code = @code and version <> @version and status <> 'DEPRECATED'",
            new { code = model.Code, version = model.Version }, ct);
        await u.ExecuteAsync(
            """
            insert into rec.scoring_model (code, version, jurisdiction, params, status, published_at, methodology_url)
            values (@code, @version, @jurisdiction, @params::jsonb, 'ACTIVE', now(), @url)
            on conflict (code, version) do update set params = excluded.params, status = 'ACTIVE'
            """,
            new { code = model.Code, version = model.Version, jurisdiction, @params = rawJson, url = $"https://healthswap.example/methodology/models/{model.Code}/{model.Version}" }, ct);
    }, ct);

    public Task ApplyPackAsync(PackFile pack, CancellationToken ct = default) => db.InTxAsync(DbRole.KbWriter, null, async u =>
    {
        var m = pack.Market;
        foreach (var d in pack.Documents) await UpsertDocumentAsync(u, d, m, ct);
        foreach (var a in pack.AllergenRules)
            await u.ExecuteAsync("insert into reg.allergen_rule (jurisdiction, allergen_code, mandatory_declaration) values (@m, @a, true) on conflict do nothing", new { m, a }, ct);
        foreach (var t in pack.Fop)
            await u.ExecuteAsync(
                """
                insert into reg.fop_threshold (jurisdiction, scheme, nutrient_code, physical_state, basis, low_max, high_min, high_inclusive)
                values (@m, @scheme, @n, @state, @basis, @low, @high, @incl)
                on conflict (jurisdiction, scheme, nutrient_code, physical_state) do update set basis = excluded.basis, low_max = excluded.low_max,
                    high_min = excluded.high_min, high_inclusive = excluded.high_inclusive
                """,
                new { m, scheme = m == "US" ? "US_FDA_FOP_PROPOSED" : $"{m}_FOP_ILLUSTRATIVE", n = t.Nutrient, state = t.PhysicalState, basis = t.Basis, low = t.LowMax, high = t.HighMin, incl = t.HighInclusive }, ct);
        foreach (var (n, amount) in pack.DailyValues)
            await u.ExecuteAsync("insert into reg.daily_value (jurisdiction, population, nutrient_code, amount) values (@m, 'ADULT', @n, @amount) on conflict (jurisdiction, population, nutrient_code) do update set amount = excluded.amount",
                new { m, n, amount }, ct);
        foreach (var c in pack.Claims)
            await u.ExecuteAsync(
                """
                insert into reg.claim_threshold (jurisdiction, claim_code, nutrient_code, physical_state, basis, max_value, min_value, min_relative_difference)
                values (@m, @claim, @n, @state, @basis, @max, @min, @rel)
                on conflict (jurisdiction, claim_code, nutrient_code, physical_state) do update set basis = excluded.basis, max_value = excluded.max_value,
                    min_value = excluded.min_value, min_relative_difference = excluded.min_relative_difference
                """,
                new { m, claim = c.ClaimCode, n = c.Nutrient, state = c.PhysicalState, basis = c.Basis, max = c.MaxValue, min = c.MinValue, rel = c.MinRelativeDifference }, ct);

        var meta = new { pack.PrimaryModel, pack.Models, pack.Hsi, pack.Rules, pack.UsHealthy, pack.MethodologyUrl, pack.PackVersion, pack.Verification };
        await u.ExecuteAsync("update rec.scoring_model set status = 'DEPRECATED' where code = 'MARKET_PACK' and jurisdiction = @m and version <> @v", new { m, v = pack.PackVersion }, ct);
        await u.ExecuteAsync(
            """
            insert into rec.scoring_model (code, version, jurisdiction, params, status, published_at, methodology_url)
            values ('MARKET_PACK', @v, @m, @params::jsonb, 'ACTIVE', now(), @url)
            on conflict (code, version) do update set params = excluded.params, status = 'ACTIVE'
            """,
            new { m, v = pack.PackVersion, @params = JsonSerializer.Serialize(meta, RefJson.Options), url = pack.MethodologyUrl }, ct);
        foreach (var (code, version) in new[] { ("HSI", pack.Hsi.Version), ("NOVA", "1.0"), ("ADDITIVE_TIERS", "1.0") })
            await u.ExecuteAsync(
                """
                insert into rec.scoring_model (code, version, jurisdiction, params, status, published_at)
                values (@code, @version, null, '{}'::jsonb, 'ACTIVE', now()) on conflict (code, version) do update set status = 'ACTIVE'
                """, new { code, version }, ct);
        await u.ExecuteAsync("update rec.scoring_model set status = 'DEPRECATED' where version = '0.1' and status = 'PREVIEW'", ct: ct);
    }, ct);

    public Task ApplyTaxonomyAsync(IngredientsFile file, CancellationToken ct = default) => db.InTxAsync(DbRole.KbWriter, null, async u =>
    {
        foreach (var i in file.Ingredients)
        {
            var id = await u.QuerySingleAsync<Guid>(
                """
                insert into kb.ingredient (canonical_name, flags, allergens) values (@name, @flags, @allergens)
                on conflict (canonical_name) do update set flags = excluded.flags, allergens = excluded.allergens
                returning ingredient_id
                """, new { name = i.Name, flags = i.Flags.ToArray(), allergens = i.Allergens.ToArray() }, ct);
            foreach (var (lang, aliases) in i.Aliases)
                foreach (var alias in aliases.Append(i.Name).Distinct(StringComparer.OrdinalIgnoreCase))
                    await u.ExecuteAsync("insert into kb.ingredient_alias (alias, lang, ingredient_id) values (@alias, @lang, @id) on conflict (alias, lang) do update set ingredient_id = excluded.ingredient_id",
                        new { alias = IngredientTaxonomy.Normalize(alias), lang, id }, ct);
        }
    }, ct);

    public Task ApplyCategoriesAsync(CategoriesFile file, CancellationToken ct = default) => db.InTxAsync(DbRole.KbWriter, null, async u =>
    {
        foreach (var c in file.Categories.OrderBy(c => c.Code.Count(ch => ch == '.')))
        {
            var parent = c.Code.Contains('.') ? c.Code[..c.Code.LastIndexOf('.')] : null;
            await u.ExecuteAsync(
                """
                insert into kb.category (parent_id, code, name, gpc_brick_code, physical_state, scoring_category)
                values ((select category_id from kb.category where code = @parent), @code, @name, @gpc, @state, @scoring)
                on conflict (code) do update set name = excluded.name, parent_id = excluded.parent_id, physical_state = excluded.physical_state,
                    scoring_category = excluded.scoring_category, gpc_brick_code = excluded.gpc_brick_code
                """, new { parent, code = c.Code, name = c.Name, gpc = c.GpcBrick, state = c.PhysicalState, scoring = c.ScoringCategory }, ct);
        }
        foreach (var map in file.SubstitutionMaps)
            foreach (var market in map.Markets)
                await u.ExecuteAsync(
                    """
                    insert into rec.category_substitution (market, from_category_id, to_category_id, approved_by, notes)
                    select @market, f.category_id, t.category_id, @by, @notes from kb.category f, kb.category t where f.code = @from and t.code = @to
                    on conflict do nothing
                    """, new { market, from = map.From, to = map.To, by = map.ApprovedBy, notes = map.Notes }, ct);
    }, ct);
}

/// <summary>Loads jurisdiction packs and the taxonomy from the database (the source of truth at runtime).</summary>
public sealed class ReferenceDataRepository(Db db)
{
    private readonly ConcurrentDictionary<string, (MarketPack Pack, DateTime At)> _packs = new();
    private (IngredientTaxonomy Tax, DateTime At)? _taxonomy;
    private static readonly TimeSpan Ttl = TimeSpan.FromMinutes(5);

    private sealed class ModelRow { public string Code { get; set; } = ""; public string Version { get; set; } = ""; public string Params { get; set; } = "{}"; public string? Jurisdiction { get; set; } }
    private sealed class FopRow { public string NutrientCode { get; set; } = ""; public string PhysicalState { get; set; } = ""; public string Basis { get; set; } = ""; public decimal LowMax { get; set; } public decimal HighMin { get; set; } public bool HighInclusive { get; set; } }
    private sealed class DvRow { public string NutrientCode { get; set; } = ""; public decimal Amount { get; set; } }
    private sealed class ClaimRow { public string ClaimCode { get; set; } = ""; public string NutrientCode { get; set; } = ""; public string PhysicalState { get; set; } = ""; public string Basis { get; set; } = ""; public decimal? MaxValue { get; set; } public decimal? MinValue { get; set; } public decimal? MinRelativeDifference { get; set; } }
    private sealed class AdditiveRow { public string InsNumber { get; set; } = ""; public string CanonicalName { get; set; } = ""; public string[] FunctionalClasses { get; set; } = []; public string? Jurisdiction { get; set; } public string? Status { get; set; } public string? ConcernTier { get; set; } }
    private sealed class IngredientRow { public Guid IngredientId { get; set; } public string CanonicalName { get; set; } = ""; public string[] Flags { get; set; } = []; public string[] Allergens { get; set; } = []; }
    private sealed class AliasRow { public string Alias { get; set; } = ""; public Guid IngredientId { get; set; } }
    private sealed class AdditiveAliasRow { public string InsNumber { get; set; } = ""; public string Alias { get; set; } = ""; }

    public sealed record PackMeta(string PrimaryModel, List<string> Models, HsiParams Hsi, RulesParams Rules, List<UsHealthyFile> UsHealthy, string MethodologyUrl, string PackVersion);

    public async Task<IReadOnlyList<string>> MarketsAsync(CancellationToken ct = default) =>
        (await db.InTxAsync(DbRole.ReadOnly, null, u => u.QueryAsync<string>("select jurisdiction from rec.scoring_model where code = 'MARKET_PACK' and status = 'ACTIVE' order by 1", ct: ct), ct)).ToList();

    public async Task<MarketPack> GetPackAsync(string market, CancellationToken ct = default)
    {
        if (_packs.TryGetValue(market, out var c) && DateTime.UtcNow - c.At < Ttl) return c.Pack;
        var pack = await db.InTxAsync(DbRole.ReadOnly, null, async u =>
        {
            var metaRow = await u.QueryFirstOrDefaultAsync<ModelRow>(
                "select code, version, params::text as params, jurisdiction from rec.scoring_model where code = 'MARKET_PACK' and jurisdiction = @market and status = 'ACTIVE' order by published_at desc limit 1",
                new { market }, ct) ?? throw HsException.Unprocessable("HS-UNKNOWN-MARKET", $"No active jurisdiction pack for market {market}");
            var meta = JsonSerializer.Deserialize<PackMeta>(metaRow.Params, RefJson.Options)!;
            var models = new Dictionary<string, PointsModelParams>();
            foreach (var r in await u.QueryAsync<ModelRow>("select code, version, params::text as params, jurisdiction from rec.scoring_model where code || '@' || version = any(@keys)",
                         new { keys = meta.Models.ToArray() }, ct))
                models[r.Code] = JsonSerializer.Deserialize<PointsModelParams>(r.Params, RefJson.Options)!;
            var fop = (await u.QueryAsync<FopRow>("select nutrient_code, physical_state, basis, low_max, high_min, high_inclusive from reg.fop_threshold where jurisdiction = @market", new { market }, ct))
                .Select(r => new FopThreshold(r.NutrientCode, r.PhysicalState, r.Basis, r.LowMax, r.HighMin, r.HighInclusive)).ToList();
            var dvs = (await u.QueryAsync<DvRow>("select nutrient_code, amount from reg.daily_value where jurisdiction = @market and population = 'ADULT'", new { market }, ct))
                .ToDictionary(r => r.NutrientCode, r => r.Amount);
            var claims = (await u.QueryAsync<ClaimRow>("select claim_code, nutrient_code, physical_state, basis, max_value, min_value, min_relative_difference from reg.claim_threshold where jurisdiction = @market", new { market }, ct))
                .Select(r => new ClaimThreshold(r.ClaimCode, r.NutrientCode, r.PhysicalState, r.Basis, r.MaxValue, r.MinValue, r.MinRelativeDifference)).ToList();
            var additiveRows = await u.QueryAsync<AdditiveRow>(
                """
                select a.ins_number, a.canonical_name, a.functional_classes, s.jurisdiction, s.status, h.concern_tier
                from kb.additive a
                left join reg.additive_status s on s.additive_id = a.additive_id and s.effective_from <= current_date
                     and (s.effective_to is null or s.effective_to > current_date)
                left join lateral (select min(concern_tier) as concern_tier from reg.additive_hazard h where h.additive_id = a.additive_id) h on true
                where a.ins_number is not null
                """, ct: ct);
            var additives = additiveRows.GroupBy(r => r.InsNumber).ToDictionary(g => g.Key, g =>
            {
                var first = g.First();
                var statuses = g.Where(r => r.Jurisdiction is not null).GroupBy(r => r.Jurisdiction!).ToDictionary(x => x.Key, x => x.First().Status!);
                return new AdditiveRegulation(first.InsNumber, first.CanonicalName, first.FunctionalClasses, statuses, first.ConcernTier is { Length: > 0 } t ? t[0] : null);
            });
            return new MarketPack
            {
                Market = market, PrimaryModel = meta.PrimaryModel, PointsModels = models, Hsi = meta.Hsi, Fop = fop, DailyValues = dvs, Claims = claims,
                Additives = additives, Rules = meta.Rules, MethodologyUrl = meta.MethodologyUrl, PackVersion = meta.PackVersion,
                UsHealthyByCategoryPrefix = meta.UsHealthy.ToDictionary(h => h.CategoryPrefix, h => new UsHealthyGroup(h.Group, h.AddedSugarsMaxG, h.SodiumMaxMg, h.SatFatMaxG, h.RequiredFlag)),
            };
        }, ct);
        _packs[market] = (pack, DateTime.UtcNow);
        return pack;
    }

    public async Task<IngredientTaxonomy> GetTaxonomyAsync(CancellationToken ct = default)
    {
        if (_taxonomy is { } t && DateTime.UtcNow - t.At < Ttl) return t.Tax;
        var tax = await db.InTxAsync(DbRole.ReadOnly, null, async u =>
        {
            var ingredients = (await u.QueryAsync<IngredientRow>("select ingredient_id, canonical_name, flags, allergens from kb.ingredient", ct: ct)).ToDictionary(r => r.IngredientId);
            var aliases = await u.QueryAsync<AliasRow>("select alias, ingredient_id from kb.ingredient_alias", ct: ct);
            var entries = new List<(string, IngredientEntry)>();
            foreach (var i in ingredients.Values) entries.Add((i.CanonicalName, new IngredientEntry(i.CanonicalName, i.Flags, i.Allergens)));
            foreach (var a in aliases)
                if (ingredients.TryGetValue(a.IngredientId, out var i)) entries.Add((a.Alias, new IngredientEntry(i.CanonicalName, i.Flags, i.Allergens)));
            var additiveAliases = (await u.QueryAsync<AdditiveAliasRow>("select a.ins_number, al.alias from kb.additive_alias al join kb.additive a using (additive_id) where a.ins_number is not null", ct: ct))
                .GroupBy(r => r.InsNumber).ToDictionary(g => g.Key, g => g.Select(x => x.Alias).ToList());
            var additives = (await u.QueryAsync<AdditiveRow>("select ins_number, canonical_name, functional_classes from kb.additive where ins_number is not null", ct: ct))
                .Select(r => new AdditiveEntry(r.InsNumber, r.CanonicalName, r.FunctionalClasses, additiveAliases.GetValueOrDefault(r.InsNumber) ?? []));
            return new IngredientTaxonomy(entries, additives);
        }, ct);
        _taxonomy = (tax, DateTime.UtcNow);
        return tax;
    }

    public void Invalidate()
    {
        _packs.Clear();
        _taxonomy = null;
    }
}
