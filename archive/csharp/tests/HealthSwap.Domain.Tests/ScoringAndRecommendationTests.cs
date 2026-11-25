using System.Text.Json;
using FsCheck;
using FsCheck.Fluent;
using FsCheck.Xunit;
using HealthSwap.Domain.Knowledge;
using HealthSwap.Domain.Recommendation;
using HealthSwap.Domain.Scoring;

namespace HealthSwap.Domain.Tests;

internal static class TestData
{
    private static readonly JsonSerializerOptions Json = new(JsonSerializerDefaults.Web);

    public static string RepoRoot()
    {
        var dir = new DirectoryInfo(AppContext.BaseDirectory);
        while (dir is not null && !File.Exists(Path.Combine(dir.FullName, "db", "schema.sql"))) dir = dir.Parent;
        return dir?.FullName ?? throw new DirectoryNotFoundException("Repository root not found");
    }

    public static PointsModelParams Model(string code) =>
        JsonSerializer.Deserialize<PointsModelParams>(File.ReadAllText(Path.Combine(RepoRoot(), "data", "packs", "models", code + ".json")), Json)!;

    public static MarketPack Pack(string primary = "NUTRISCORE_2023", Dictionary<string, AdditiveRegulation>? additives = null) => new()
    {
        Market = "IN", PrimaryModel = primary, PointsModels = new Dictionary<string, PointsModelParams> { [primary] = Model(primary) },
        Additives = additives ?? new Dictionary<string, AdditiveRegulation>(),
    };

    public static ProductData Product(string name, string category, decimal kcal, decimal sugar, decimal satFat, decimal sodium, decimal fibre, decimal protein,
        int? nova = 3, string[]? additives = null, string state = "SOLID", decimal net = 280, string? brand = "Brand") => new()
    {
        ProductId = Guid.NewGuid(), VersionId = Random.Shared.Next(1, 1_000_000), Name = name, Brand = brand, CategoryCode = category, PhysicalState = state,
        NetQuantity = new NetQuantity(net, state == "LIQUID" ? "ml" : "g"),
        Nutrition = new Nutrition
        {
            Basis = state == "LIQUID" ? Basis.Per100Ml : Basis.Per100G, EnergyKcal = kcal, TotalSugarsG = sugar, AddedSugarsG = sugar, SaturatedFatG = satFat,
            TotalFatG = satFat * 2, TransFatG = 0, SodiumMg = sodium, FibreG = fibre, ProteinG = protein, CarbohydrateG = 60,
        },
        Nova = nova, NovaConfidence = 0.9m, Additives = additives ?? [], DataSufficiency = "SUFFICIENT", Confidence = 0.95m, DietClass = "VEG",
        AllergensVerified = true, AllergenConfidence = 1, Form = "NOODLE", FlavourFamily = "MASALA", UseOccasion = "MEAL",
    };
}

public class ScoringTests
{
    [Fact]
    public void More_sugar_never_scores_better()
    {
        var model = TestData.Model("NUTRISCORE_2023");
        var low = PointsModel.Score(model, TestData.Product("a", "biscuits.sweet", 450, 5, 5, 300, 3, 6));
        var high = PointsModel.Score(model, TestData.Product("b", "biscuits.sweet", 450, 35, 5, 300, 3, 6));
        Assert.True(high.Value > low.Value);
        Assert.True(high.Normalized < low.Normalized);
    }

    [Fact]
    public void Hsi_weights_and_renormalization()
    {
        var p = new HsiParams();
        Assert.Equal(51.5m, Hsi.Compute(50, 4, 1, 0, p)); // 0.70x50 + 0.15x40 + 0.15x70
        Assert.Equal(Math.Round((0.70m * 50 + 0.15m * 70) / 0.85m, 2), Hsi.Compute(50, null, 1, 0, p)); // NOVA unknown
        Assert.Null(Hsi.Compute(null, 1, 0, 0, p));
        Assert.Equal("A", Hsi.Grade(80));
        Assert.Equal("E", Hsi.Grade(34.99m));
    }

    [Fact]
    public void Prohibited_additive_makes_a_product_ineligible()
    {
        var additives = new Dictionary<string, AdditiveRegulation>
        {
            ["924a"] = new("924a", "Potassium bromate", ["FLOUR_TREATMENT_AGENT"], new Dictionary<string, string> { ["IN"] = "PROHIBITED" }, null),
        };
        var hp = HealthProfileBuilder.Build(TestData.Product("bread", "bread.sliced", 250, 4, 1, 400, 3, 8, additives: ["924a"]), TestData.Pack(additives: additives), DateTime.UtcNow);
        Assert.Null(hp.Hsi);
        Assert.False(hp.Eligible);
        Assert.Contains("PROHIBITED_ADDITIVE", hp.Flags);
    }

    [Fact]
    public void Additive_tiers_come_from_regulators()
    {
        Assert.Equal('A', AdditiveTiers.Resolve(new("171", "TiO2", ["COLOUR"], new Dictionary<string, string> { ["EU"] = "PROHIBITED" }, 'B'), "EU"));
        Assert.Equal('B', AdditiveTiers.Resolve(new("171", "TiO2", ["COLOUR"], new Dictionary<string, string> { ["EU"] = "PROHIBITED" }, 'B'), "IN"));
        Assert.Equal('C', AdditiveTiers.Resolve(new("951", "Aspartame", ["SWEETENER"], new Dictionary<string, string>(), 'C'), "IN"));
        Assert.Equal('D', AdditiveTiers.Resolve(null, "IN"));
    }
}

public class RecommendationTests
{
    private static (HealthProfileData A, HealthProfileData B, MarketPack Pack) NoodlePair()
    {
        var pack = TestData.Pack();
        var a = HealthProfileBuilder.Build(TestData.Product("Masala noodles", "noodles.instant", 440, 2.5m, 8.5m, 1200, 2, 8.5m, nova: 4), pack, DateTime.UtcNow);
        var b = HealthProfileBuilder.Build(TestData.Product("Millet noodles", "noodles.instant", 380, 2, 4, 1050, 6.1m, 10, nova: 3, net: 256), pack, DateTime.UtcNow);
        return (a, b, pack);
    }

    [Fact]
    public void Healthier_substitutable_product_passes_static_rules()
    {
        var (a, b, pack) = NoodlePair();
        var r = StaticRules.Check(a, b, 0.9m, true, pack);
        Assert.True(r.Eligible, string.Join(",", r.Failed));
        Assert.Contains("HIGHER_FIBRE", r.ReasonCodes);
    }

    [Fact]
    public void Low_substitutability_fails_r1_and_a_worse_product_fails_r2()
    {
        var (a, b, pack) = NoodlePair();
        Assert.Contains("R1", StaticRules.Check(a, b, 0.5m, true, pack).Failed);
        Assert.Contains("R2", StaticRules.Check(b, a, 0.9m, true, pack).Failed);
    }

    [Fact]
    public void Shopper_allergens_and_price_guards_filter_alternatives()
    {
        var (a, b, _) = NoodlePair();
        var withMilk = b with { Allergens = new Dictionary<string, AllergenPresence> { ["MILK"] = AllergenPresence.MayContain } };
        Assert.Equal("R5_ALLERGEN_MAY_CONTAIN", RequestRules.Reject(a, withMilk, new ShopperConstraints { Allergens = ["MILK"] }, new CommercialGuards(), new PriceContext(56, 89), 0.98m));
        Assert.Null(RequestRules.Reject(a, withMilk, new ShopperConstraints { Allergens = ["MILK"], StrictMayContain = false }, new CommercialGuards(), new PriceContext(56, 89), 0.98m));
        Assert.Equal("R6_PRICE_RATIO", RequestRules.Reject(a, b, new ShopperConstraints(), new CommercialGuards(), new PriceContext(56, 120), 0.98m));
        Assert.Equal(1.74m, Math.Round(RequestRules.NormalizedPriceRatio(56, a.NetQuantity, 89, b.NetQuantity), 2));
    }

    [Fact]
    public void Minors_never_get_caffeine()
    {
        var (a, b, _) = NoodlePair();
        var caffeinated = b with { IngredientFlags = ["CAFFEINE"] };
        Assert.Equal("R5_MINOR_CAFFEINE", RequestRules.Reject(a, caffeinated, new ShopperConstraints { TreatAsMinor = true }, new CommercialGuards(), new PriceContext(null, null), 0.98m));
    }

    [Fact]
    public void Ranker_features_exclude_commercial_signals()
    {
        foreach (var f in RankerFeatures.FeatureNames)
            Assert.DoesNotContain(RankerFeatures.Forbidden, bad => f.Contains(bad, StringComparison.OrdinalIgnoreCase));
        var v = RankerFeatures.Vector(new RankerFeatures.Input(16.5m, 0.9m, 1.74m, true, 0.91m, "CART", 10, 0, false, 1, false));
        Assert.Equal(RankerFeatures.FeatureNames.Length, v.Length);
        var (a, b, _) = NoodlePair();
        Assert.Equal(Substitutability.FeatureNames.Length, Substitutability.Features(a, b, 0).Length);
    }

    [Fact]
    public void Approved_category_maps_take_precedence_over_sibling_distance()
    {
        Assert.Equal(0, Substitutability.CategoryDistance("a.b", "a.b", false));
        Assert.Equal(1, Substitutability.CategoryDistance("beverages.carbonated.soft-drinks", "beverages.carbonated.flavoured-water", false));
        Assert.Equal(2, Substitutability.CategoryDistance("beverages.carbonated.soft-drinks", "beverages.carbonated.flavoured-water", true));
        Assert.Equal(3, Substitutability.CategoryDistance("snacks.chips", "dairy.yogurt", false));
    }

    [Fact]
    public void Cart_selection_takes_one_line_per_category()
    {
        var picked = CartSelection.Select([
            new CartCandidate("1", "noodles", 15, 2, 1, 1.7m), new CartCandidate("2", "noodles", 10, 1, 1, 1.1m), new CartCandidate("3", "cola", 50, 1, 1, 1.3m),
            new CartCandidate("4", "chips", 9, 1, 1, null),
        ], 2);
        Assert.Equal(["3", "1"], picked);
    }

    [Fact]
    public void Trivial_differences_are_not_claims()
    {
        var (a, b, pack) = NoodlePair();
        var withTrans = a with { Nutrition = a.Nutrition with { TransFatG = 0.1m } };
        var sheet = FactSheets.Build(withTrans, b, pack);
        var trans = sheet.Facts.Single(f => f.Nutrient == "transFatG");
        Assert.False(trans.ClaimEligible);
        Assert.Equal(0, trans.Impact);
        Assert.DoesNotContain(FactSheets.Select(sheet), f => f.Nutrient == "transFatG");
    }
}

public class ExplanationTests
{
    private static Fact Nutrient(string nutrient, decimal from, decimal to, string unit = "g", bool claim = true) => new()
    {
        Id = "f1", Kind = "NUTRIENT", Nutrient = nutrient, From = from, To = to, Unit = unit, RelChange = from == 0 ? null : Math.Round((to - from) / from, 4),
        Direction = "BETTER", Impact = 1, ClaimEligible = claim,
    };

    [Theory]
    [InlineData("en-IN")]
    [InlineData("hi-IN")]
    [InlineData("en-US")]
    [InlineData("es-US")]
    public void Templates_always_pass_the_validator(string locale)
    {
        var facts = new List<Fact>
        {
            Nutrient("totalSugarsG", 10.6m, 0.2m),
            Nutrient("dietaryFibreG", 2.0m, 6.1m) with { Id = "f2" },
            new() { Id = "f3", Kind = "ADDITIVE_INTRODUCED", Nutrient = "sweeteners", Codes = ["951"], Direction = "WORSE", MustDisclose = true },
        };
        var r = ExplanationTemplates.Render(facts, locale);
        var report = ExplanationValidator.Validate(r.Short, r.Bullets, facts, locale);
        Assert.True(report.Passed, string.Join("; ", report.Problems) + " :: " + r.Short);
    }

    [Property(MaxTest = 200)]
    public Property Template_numbers_always_come_from_the_facts(PositiveInt fromTenths, PositiveInt toTenths)
    {
        var from = fromTenths.Get % 1000 / 10m + 0.1m;
        var to = toTenths.Get % 1000 / 10m;
        var fact = Nutrient("totalSugarsG", from, to, claim: to < from * 0.75m);
        var r = ExplanationTemplates.Render([fact], "en-IN");
        return ExplanationValidator.Validate(r.Short, r.Bullets, [fact], "en-IN").Checks["numericFidelity"].ToProperty();
    }

    [Fact]
    public void Validator_rejects_hallucinated_numbers_banned_claims_wrong_language_and_missing_disclosures()
    {
        var facts = new List<Fact> { Nutrient("totalSugarsG", 10.6m, 0.2m) };
        Assert.False(ExplanationValidator.Validate("Guilt-free choice", [], facts, "en-IN").Checks["claims"]);
        Assert.False(ExplanationValidator.Validate("99% less sugar per 100 ml", [], facts, "en-IN").Checks["numericFidelity"]);
        Assert.False(ExplanationValidator.Validate("98% less sugar per 100 ml", [], facts, "hi-IN").Checks["language"]);
        Assert.False(ExplanationValidator.Validate("Cures diabetes", [], facts, "en-IN").Checks["claims"]);
        var disclose = new List<Fact> { new() { Id = "f9", Kind = "ADDITIVE_INTRODUCED", Codes = ["951"], MustDisclose = true } };
        Assert.False(ExplanationValidator.Validate("Less sugar", [], disclose, "en-IN").Checks["mustDisclose"]);
        Assert.True(ExplanationValidator.Validate("98% less sugar per 100 ml", [], facts, "en-IN").Passed);
    }

    [Fact]
    public void Percentages_are_floored_never_overstated()
    {
        Assert.Equal(98, ExplanationTemplates.Pct(-0.9811m));
        Assert.Equal("3", ExplanationTemplates.Multiplier(2.0m, 6.1m));
        Assert.Null(ExplanationTemplates.Multiplier(2.0m, 3.9m));
    }
}
