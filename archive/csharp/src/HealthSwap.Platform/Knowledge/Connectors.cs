using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;
using HealthSwap.Domain;
using HealthSwap.Domain.Knowledge;

namespace HealthSwap.Platform.Knowledge;

/// <summary>A product record from one source, normalized to the canonical model before survivorship (doc 02 sections 6-7).</summary>
public sealed record ProductCandidate
{
    public required string SourceCode { get; init; }
    public required string ExternalId { get; init; }
    public string? RawGtin { get; init; }
    public required string Name { get; init; }
    public string? Brand { get; init; }
    public string? BrandOwner { get; init; }
    public string BrandTier { get; init; } = "MAINSTREAM";
    public string? CategoryCode { get; init; }
    public List<string> Markets { get; init; } = [];
    public decimal? NetValue { get; init; }
    public string? NetUnit { get; init; }
    public int PackCount { get; init; } = 1;
    public string PhysicalState { get; init; } = "SOLID";
    public decimal? ServingValue { get; init; }
    public string? ServingUnit { get; init; }
    public Nutrition? Nutrition { get; init; }
    public bool SaltDeclared { get; init; }
    public bool SodiumDeclared { get; init; }
    public decimal? PerServingEnergyKcal { get; init; }
    public string? IngredientsText { get; init; }
    public string? AllergenStatement { get; init; }
    /// <summary>Structured allergen tags from the source (OFF allergens_tags / traces_tags) as code -> presence.</summary>
    public Dictionary<string, AllergenPresence> AllergenTags { get; init; } = [];
    public List<string> DietaryMarks { get; init; } = [];
    public bool MarksInferred { get; init; }
    public Dictionary<string, string> Attributes { get; init; } = [];
    public bool IsPrivateLabel { get; init; }
    public string Verification { get; init; } = "UNVERIFIED";
    public DateTime? LastVerifiedAt { get; init; }
    public DateTime ObservedAt { get; init; }
    public required string RawJson { get; init; }
    public Guid? PartnerId { get; init; }
}

/// <summary>Source connectors: each maps a source-specific layout to <see cref="ProductCandidate"/>. They never call remote APIs at request time.</summary>
public static class Connectors
{
    private static readonly JsonSerializerOptions Json = RefJson.Options;

    private sealed record BrandProductFile(string Version, string Source, List<JsonElement> Products);

    /// <summary>Brand portal submissions (trust tier 1, brand-verified), as in data/seed/kb/products.json.</summary>
    public static IEnumerable<ProductCandidate> BrandPortal(string path, DateTime now)
    {
        var file = RefJson.Read<BrandProductFile>(path);
        foreach (var p in file.Products)
        {
            var o = JsonNode.Parse(p.GetRawText())!.AsObject();
            var n = o["nutrition"]?.AsObject();
            var basis = n?["basis"]?.GetValue<string>() ?? "PER_100G";
            var carbDef = o["carbDefinition"]?.GetValue<string>() ?? "AVAILABLE";
            var attrs = o["attributes"]?.AsObject().ToDictionary(kv => kv.Key, kv => kv.Value!.GetValue<string>()) ?? [];
            attrs["brandTier"] = o["brandTier"]?.GetValue<string>() ?? "MAINSTREAM";
            yield return new ProductCandidate
            {
                SourceCode = "BRAND_PORTAL", ExternalId = o["gtin"]!.GetValue<string>(), RawGtin = o["gtin"]!.GetValue<string>(),
                Name = o["name"]!.GetValue<string>(), Brand = o["brand"]?.GetValue<string>(), BrandOwner = o["brandOwner"]?.GetValue<string>(),
                BrandTier = attrs["brandTier"], CategoryCode = o["category"]?.GetValue<string>(),
                Markets = o["markets"]?.AsArray().Select(x => x!.GetValue<string>()).ToList() ?? [],
                NetValue = o["netQuantity"]?["value"]?.GetValue<decimal>(), NetUnit = o["netQuantity"]?["unit"]?.GetValue<string>(),
                PackCount = o["packCount"]?.GetValue<int>() ?? 1, PhysicalState = o["physicalState"]?.GetValue<string>() ?? "SOLID",
                ServingValue = o["servingSize"]?["value"]?.GetValue<decimal>(), ServingUnit = o["servingSize"]?["unit"]?.GetValue<string>(),
                Nutrition = n is null ? null : new Nutrition
                {
                    Basis = basis, CarbDefinition = carbDef,
                    EnergyKcal = D(n, "energyKcal"), ProteinG = D(n, "proteinG"), CarbohydrateG = D(n, "carbohydrateG"), TotalSugarsG = D(n, "totalSugarsG"),
                    AddedSugarsG = D(n, "addedSugarsG"), TotalFatG = D(n, "totalFatG"), SaturatedFatG = D(n, "saturatedFatG"), TransFatG = D(n, "transFatG"),
                    CholesterolMg = D(n, "cholesterolMg"), SodiumMg = D(n, "sodiumMg"), SaltG = D(n, "saltG"), FibreG = D(n, "dietaryFibreG"),
                },
                SodiumDeclared = n?["sodiumMg"] is not null, SaltDeclared = n?["saltG"] is not null,
                IngredientsText = o["ingredientsText"]?.GetValue<string>(), AllergenStatement = o["allergenStatement"]?.GetValue<string>(),
                DietaryMarks = o["dietaryMarks"]?.AsArray().Select(x => x!.GetValue<string>()).ToList() ?? [],
                Attributes = attrs, IsPrivateLabel = o["isPrivateLabel"]?.GetValue<bool>() ?? false,
                Verification = "BRAND_VERIFIED", LastVerifiedAt = now.AddDays(-30), ObservedAt = now, RawJson = p.GetRawText(),
            };
        }
    }

    private static readonly Dictionary<string, string> OffCategoryMap = new()
    {
        ["en:instant-noodles"] = "noodles.instant", ["en:breakfast-cereals"] = "breakfast.cereals", ["en:cookies"] = "biscuits.sweet",
        ["en:biscuits"] = "biscuits.sweet", ["en:sodas"] = "beverages.carbonated.soft-drinks", ["en:colas"] = "beverages.carbonated.soft-drinks",
        ["en:chips"] = "snacks.chips", ["en:crisps"] = "snacks.chips", ["en:yogurts"] = "dairy.yogurt", ["en:breads"] = "bread.sliced",
        ["en:chocolates"] = "confectionery.chocolate", ["en:ketchup"] = "sauces.ketchup",
    };

    private static readonly Dictionary<string, string> OffAllergens = new()
    {
        ["en:gluten"] = "GLUTEN_CEREALS", ["en:peanuts"] = "PEANUTS", ["en:nuts"] = "TREE_NUTS", ["en:milk"] = "MILK", ["en:soybeans"] = "SOY",
        ["en:mustard"] = "MUSTARD", ["en:sesame-seeds"] = "SESAME", ["en:eggs"] = "EGGS", ["en:fish"] = "FISH", ["en:crustaceans"] = "CRUSTACEANS",
        ["en:celery"] = "CELERY", ["en:lupin"] = "LUPIN", ["en:sulphur-dioxide-and-sulphites"] = "SULPHITES", ["en:molluscs"] = "MOLLUSCS",
    };

    /// <summary>
    /// Open Food Facts JSONL export lines (ODbL, trust tier 4). Process nightly deltas in timestamp order and reconcile weekly
    /// against the full dump (deltas carry no deletions). Never called at request time (15 reads/min/IP API limit).
    /// </summary>
    public static IEnumerable<ProductCandidate> OpenFoodFacts(string path, DateTime now)
    {
        foreach (var line in File.ReadLines(path))
        {
            if (string.IsNullOrWhiteSpace(line)) continue;
            var o = JsonNode.Parse(line)!.AsObject();
            var countries = o["countries_tags"]?.AsArray().Select(x => x!.GetValue<string>()).ToList() ?? [];
            var markets = countries.Select(c => c switch { "en:india" => "IN", "en:united-states" => "US", _ => null }).Where(m => m is not null).Cast<string>().ToList();
            if (markets.Count == 0) continue; // filter by countries_tags for target markets
            var nut = o["nutriments"]?.AsObject();
            var allergens = new Dictionary<string, AllergenPresence>();
            foreach (var tag in o["traces_tags"]?.AsArray() ?? []) if (OffAllergens.TryGetValue(tag!.GetValue<string>(), out var c)) allergens[c] = AllergenPresence.MayContain;
            foreach (var tag in o["allergens_tags"]?.AsArray() ?? []) if (OffAllergens.TryGetValue(tag!.GetValue<string>(), out var c)) allergens[c] = AllergenPresence.Contains;
            var category = o["categories_tags"]?.AsArray().Select(x => OffCategoryMap.GetValueOrDefault(x!.GetValue<string>())).FirstOrDefault(x => x is not null);
            var (qty, unit) = ParseQuantity(o["quantity"]?.GetValue<string>());
            var labels = o["labels_tags"]?.AsArray().Select(x => x!.GetValue<string>()).ToList() ?? [];
            var sodiumG = Dn(nut, "sodium_100g");
            yield return new ProductCandidate
            {
                SourceCode = "OFF", ExternalId = o["code"]!.GetValue<string>(), RawGtin = o["code"]!.GetValue<string>(),
                Name = o["product_name"]?.GetValue<string>() ?? "Unnamed product", Brand = o["brands"]?.GetValue<string>()?.Split(',')[0].Trim(),
                CategoryCode = category, Markets = markets, NetValue = qty, NetUnit = unit,
                PhysicalState = unit is "ml" or "l" ? "LIQUID" : "SOLID",
                Nutrition = nut is null ? null : new Nutrition
                {
                    Basis = unit is "ml" or "l" ? Basis.Per100Ml : Basis.Per100G,
                    EnergyKcal = Dn(nut, "energy-kcal_100g"), ProteinG = Dn(nut, "proteins_100g"), CarbohydrateG = Dn(nut, "carbohydrates_100g"),
                    TotalSugarsG = Dn(nut, "sugars_100g"), AddedSugarsG = Dn(nut, "added-sugars_100g"), TotalFatG = Dn(nut, "fat_100g"),
                    SaturatedFatG = Dn(nut, "saturated-fat_100g"), TransFatG = Dn(nut, "trans-fat_100g"), FibreG = Dn(nut, "fiber_100g"),
                    SaltG = Dn(nut, "salt_100g"), SodiumMg = sodiumG is { } s ? Math.Round(s * 1000m, 1) : null,
                    CarbDefinition = markets.Contains("US") ? "TOTAL_INCL_FIBRE" : "AVAILABLE",
                },
                SaltDeclared = nut?["salt_100g"] is not null, SodiumDeclared = false,
                IngredientsText = o["ingredients_text"]?.GetValue<string>(), AllergenTags = allergens,
                DietaryMarks = labels.Contains("en:vegetarian") && markets.Contains("IN") ? ["IN_FSSAI_VEG"] : [], MarksInferred = true,
                Verification = "UNVERIFIED", ObservedAt = o["last_modified_t"] is { } t ? DateTimeOffset.FromUnixTimeSeconds(t.GetValue<long>()).UtcDateTime : now,
                RawJson = line,
            };
        }
    }

    private sealed record FdcNutrient(FdcNutrientRef Nutrient, decimal Amount);
    private sealed record FdcNutrientRef(string Number, string Name, string UnitName);
    private sealed record FdcFood(long FdcId, string Description, string? GtinUpc, string? BrandOwner, string? BrandName, string? BrandedFoodCategory,
        string? MarketCountry, string? Ingredients, decimal? ServingSize, string? ServingSizeUnit, string? ModifiedDate, List<FdcNutrient> FoodNutrients);
    private sealed record FdcFile(List<FdcFood> BrandedFoods);

    /// <summary>USDA FoodData Central Branded Foods (CC0, trust tier 2). Nutrients per 100 g/ml, keyed by nutrient number.</summary>
    public static IEnumerable<ProductCandidate> UsdaFdcBranded(string path, DateTime now)
    {
        var file = RefJson.Read<FdcFile>(path);
        foreach (var f in file.BrandedFoods)
        {
            decimal? N(string number) => f.FoodNutrients.FirstOrDefault(x => x.Nutrient.Number == number)?.Amount;
            var liquid = f.ServingSizeUnit is "ml" or "MLT";
            var category = f.BrandedFoodCategory switch
            {
                "Cereal" => "breakfast.cereals", "Chips, Pretzels & Snacks" => "snacks.chips", "Cookies & Biscuits" => "biscuits.sweet",
                "Soda" => "beverages.carbonated.soft-drinks", "Yogurt" => "dairy.yogurt", "Breads & Buns" => "bread.sliced", _ => null,
            };
            yield return new ProductCandidate
            {
                SourceCode = "USDA_FDC_BRANDED", ExternalId = f.FdcId.ToString(CultureInfo.InvariantCulture), RawGtin = f.GtinUpc,
                Name = CultureInfo.InvariantCulture.TextInfo.ToTitleCase(f.Description.ToLowerInvariant()),
                Brand = f.BrandName is null ? null : CultureInfo.InvariantCulture.TextInfo.ToTitleCase(f.BrandName.ToLowerInvariant()), BrandOwner = f.BrandOwner,
                CategoryCode = category, Markets = f.MarketCountry == "United States" ? ["US"] : [],
                PhysicalState = liquid ? "LIQUID" : "SOLID", ServingValue = f.ServingSize, ServingUnit = f.ServingSizeUnit?.ToLowerInvariant() is "g" or "grm" ? "g" : liquid ? "ml" : "g",
                Nutrition = new Nutrition
                {
                    Basis = liquid ? Basis.Per100Ml : Basis.Per100G, CarbDefinition = "TOTAL_INCL_FIBRE",
                    EnergyKcal = N("208"), ProteinG = N("203"), TotalFatG = N("204"), CarbohydrateG = N("205"), TotalSugarsG = N("269"),
                    AddedSugarsG = N("539"), FibreG = N("291"), SodiumMg = N("307"), SaturatedFatG = N("606"), TransFatG = N("605"), CholesterolMg = N("601"),
                },
                SodiumDeclared = true, IngredientsText = f.Ingredients is null ? null : CultureInfo.InvariantCulture.TextInfo.ToTitleCase(f.Ingredients.ToLowerInvariant()),
                Verification = "UNVERIFIED", ObservedAt = DateTime.TryParse(f.ModifiedDate, CultureInfo.InvariantCulture, DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out var md) ? md : now,
                RawJson = JsonSerializer.Serialize(f, Json),
            };
        }
    }

    public sealed record RecallRecord(string Authority, string Reference, string? Gtin, string Description, string? Classification, DateTime PublishedAt, string Jurisdiction);

    /// <summary>openFDA food enforcement reports (weekly). UPCs in code_info / product_description are matched to GTINs.</summary>
    public static IEnumerable<RecallRecord> OpenFdaEnforcement(string path)
    {
        var o = JsonNode.Parse(File.ReadAllText(path))!.AsObject();
        foreach (var r in o["results"]?.AsArray() ?? [])
        {
            var text = (r!["code_info"]?.GetValue<string>() ?? "") + " " + (r["product_description"]?.GetValue<string>() ?? "");
            var upcs = System.Text.RegularExpressions.Regex.Matches(text.Replace(" ", ""), @"\d{12,14}").Select(m => m.Value).Distinct();
            var published = DateTime.ParseExact(r["report_date"]!.GetValue<string>(), "yyyyMMdd", CultureInfo.InvariantCulture, DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal);
            foreach (var upc in upcs.DefaultIfEmpty(null))
                yield return new RecallRecord("FDA", r["recall_number"]!.GetValue<string>(), upc is not null && Gtin.TryNormalize(upc, out var g) ? g : null,
                    r["product_description"]?.GetValue<string>() + " - " + r["reason_for_recall"]?.GetValue<string>(), r["classification"]?.GetValue<string>(),
                    published, "US");
        }
    }

    private static decimal? D(JsonObject o, string name) => o[name] is { } v && v.GetValueKind() == JsonValueKind.Number ? v.GetValue<decimal>() : null;

    private static decimal? Dn(JsonObject? o, string name) => o?[name] is { } v && v.GetValueKind() == JsonValueKind.Number ? Math.Round(v.GetValue<decimal>(), 4) : null;

    public static (decimal?, string?) ParseQuantity(string? text)
    {
        if (string.IsNullOrWhiteSpace(text)) return (null, null);
        var m = System.Text.RegularExpressions.Regex.Match(text.ToLowerInvariant(), @"(\d+(?:\.\d+)?)\s*(kg|g|ml|l|oz|fl oz)\b");
        return m.Success ? (decimal.Parse(m.Groups[1].Value, CultureInfo.InvariantCulture), m.Groups[2].Value) : (null, null);
    }
}
