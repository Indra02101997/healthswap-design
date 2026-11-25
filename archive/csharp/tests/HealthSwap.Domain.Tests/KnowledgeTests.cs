using HealthSwap.Domain.Knowledge;

namespace HealthSwap.Domain.Tests;

/// <summary>Deterministic knowledge-plane rules: ingredient parsing, allergens, diet, QA rules, GTIN, entity resolution, survivorship.</summary>
public class KnowledgeTests
{
    private static readonly IngredientTaxonomy Tax = new(
        [
            ("refined wheat flour", new IngredientEntry("refined wheat flour", ["REFINED_FLOUR"], ["WHEAT", "GLUTEN_CEREALS"])),
            ("maida", new IngredientEntry("refined wheat flour", ["REFINED_FLOUR"], ["WHEAT", "GLUTEN_CEREALS"])),
            ("palm oil", new IngredientEntry("palm oil", ["PALM_OIL", "OIL"], [])),
            ("sugar", new IngredientEntry("sugar", ["ADDED_SUGAR"], [])),
            ("salt", new IngredientEntry("salt", ["SALT"], [])),
            ("milk solids", new IngredientEntry("milk solids", ["DAIRY"], ["MILK"])),
            ("lemon juice", new IngredientEntry("lemon juice", ["FRUIT_VEG_LEGUME"], [])),
            ("carbonated water", new IngredientEntry("carbonated water", [], [])),
            ("dehydrated vegetables", new IngredientEntry("dehydrated vegetables", ["FRUIT_VEG_LEGUME"], [])),
        ],
        [
            new AdditiveEntry("322", "Lecithins", ["EMULSIFIER"], ["lecithin", "soy lecithin"]),
            new AdditiveEntry("500", "Sodium carbonates", ["RAISING_AGENT"], ["sodium bicarbonate"]),
            new AdditiveEntry("503", "Ammonium carbonates", ["RAISING_AGENT"], []),
            new AdditiveEntry("129", "Allura Red AC", ["COLOUR"], ["red 40"]),
            new AdditiveEntry("951", "Aspartame", ["SWEETENER"], ["aspartame"]),
            new AdditiveEntry("150d", "Sulphite ammonia caramel", ["COLOUR"], ["caramel colour"]),
        ]);

    [Fact]
    public void Class_titles_with_ins_numbers_become_additives()
    {
        var r = IngredientParser.Parse("Refined wheat flour (maida), Sugar, Raising agents (INS 503(ii), INS 500(ii)), Emulsifier (INS 322), Colour (E129)", Tax);
        Assert.Equal(["503", "500", "322", "129"], r.Additives);
        Assert.Contains("ARTIFICIAL_COLOUR", r.Flags);
        Assert.Contains("ADDED_SUGAR", r.Flags);
        Assert.Contains("WHEAT", r.AllergenHints);
        Assert.DoesNotContain("SOY", r.AllergenHints); // lecithin implies soy only when the label says soy
    }

    [Fact]
    public void Soy_lecithin_implies_soy()
    {
        var r = IngredientParser.Parse("Sugar, Emulsifier (soya lecithin INS 322)", Tax);
        Assert.Contains("SOY", r.AllergenHints);
    }

    [Fact]
    public void Us_additive_names_and_sweeteners_are_detected()
    {
        var r = IngredientParser.Parse("Carbonated water, Red 40, Aspartame", Tax);
        Assert.Contains("129", r.Additives);
        Assert.Contains("951", r.Additives);
        Assert.Contains("NNS", r.Flags);
    }

    [Fact]
    public void Quid_percentages_drive_the_fruit_veg_share()
    {
        var r = IngredientParser.Parse("Carbonated water, Lemon juice (3%)", Tax);
        Assert.Equal(3m, r.FvlnPctEstimate);
        Assert.True(r.FvlnFromQuid);
    }

    [Fact]
    public void Allergen_statements_split_contains_and_may_contain()
    {
        var a = AllergenDeriver.FromStatement("Contains wheat, gluten and groundnut. May contain milk, soy, mustard and nuts.");
        Assert.Equal(AllergenPresence.Contains, a["WHEAT"]);
        Assert.Equal(AllergenPresence.Contains, a["PEANUTS"]);
        Assert.Equal(AllergenPresence.MayContain, a["MILK"]);
        Assert.Equal(AllergenPresence.MayContain, a["TREE_NUTS"]);
    }

    [Fact]
    public void Ingredient_allergens_win_over_statements()
    {
        var merged = AllergenDeriver.Reconcile(new Dictionary<string, AllergenPresence> { ["MILK"] = AllergenPresence.MayContain }, ["MILK"], out var undeclared);
        Assert.Equal(AllergenPresence.Contains, merged["MILK"]);
        Assert.Equal(["MILK"], undeclared);
    }

    [Fact]
    public void Vegan_requires_a_mark()
    {
        Assert.Equal("VEG", DietClassifier.Classify(["IN_FSSAI_VEG"], new HashSet<string>(), new Dictionary<string, AllergenPresence>()).DietClass);
        Assert.Equal("VEGAN", DietClassifier.Classify(["IN_FSSAI_VEGAN"], new HashSet<string>(), new Dictionary<string, AllergenPresence>()).DietClass);
        var contradicted = DietClassifier.Classify(["IN_FSSAI_VEG"], new HashSet<string> { "GELATIN" }, new Dictionary<string, AllergenPresence>());
        Assert.Equal("NON_VEG", contradicted.DietClass);
        Assert.True(contradicted.VegMarkContradicted);
    }

    [Theory]
    [InlineData("8901234567890", true)]
    [InlineData("8901234567891", false)]
    [InlineData("099999000016", true)]
    [InlineData("12345", false)]
    public void Gtin_check_digits(string raw, bool valid) => Assert.Equal(valid, Gtin.TryNormalize(raw, out _));

    [Fact]
    public void Gtins_normalize_to_14_digits()
    {
        Assert.True(Gtin.TryNormalize("099999000016", out var g));
        Assert.Equal("00099999000016", g);
        Assert.True(Gtin.IsRestrictedCirculation("02012345000014"));
    }

    [Fact]
    public void Qa01_flags_energy_inconsistency()
    {
        var f = QaRules.Run(new QaInput { Nutrition = new Nutrition { EnergyKcal = 100, ProteinG = 10, CarbohydrateG = 60, TotalFatG = 20 }, Now = DateTime.UtcNow });
        Assert.Contains(f, x => x.Code == "QA-01");
    }

    [Fact]
    public void Qa05_does_not_double_count_us_fibre()
    {
        // US total carbohydrate includes fibre: 10 + 2 + 85 = 97 g, fibre must not be added again.
        var us = new Nutrition { ProteinG = 10, TotalFatG = 2, CarbohydrateG = 85, FibreG = 10, CarbDefinition = "TOTAL_INCL_FIBRE", EnergyKcal = 398 };
        Assert.DoesNotContain(QaRules.Run(new QaInput { Nutrition = us, Now = DateTime.UtcNow }), x => x.Code == "QA-05");
        var eu = us with { CarbDefinition = "AVAILABLE" };
        Assert.Contains(QaRules.Run(new QaInput { Nutrition = eu, Now = DateTime.UtcNow }), x => x.Code == "QA-05");
    }

    [Fact]
    public void Qa02_rejects_sugars_above_carbohydrate_and_qa14_quarantines_prohibited_additives()
    {
        var f = QaRules.Run(new QaInput { Nutrition = new Nutrition { TotalSugarsG = 20, CarbohydrateG = 10 }, TierAAdditives = ["924a"], Now = DateTime.UtcNow });
        Assert.Contains(f, x => x.Code == "QA-02" && x.Action == QaAction.RejectField);
        Assert.Contains(f, x => x.Code == "QA-14" && x.Action == QaAction.Quarantine);
    }

    [Fact]
    public void Entity_resolution_never_merges_different_gtins()
    {
        var a = new EntityResolution.Candidate("Swaad Masala Noodles 280 g", "Swaad", new NetQuantity(280, "g"), null, [], "08901234567890");
        var b = a with { Gtin = "08909990000019" };
        Assert.Equal(0.05m, EntityResolution.HeuristicScore(EntityResolution.Features(a, b)));
        var promo = new EntityResolution.Candidate("Swaad Masala Noodles 280g Pack of 4", "Swaad", new NetQuantity(280, "g"), null, [], null);
        Assert.True(EntityResolution.HeuristicScore(EntityResolution.Features(a with { Gtin = null }, promo)) >= EntityResolution.ReviewThreshold);
    }

    [Fact]
    public void Survivorship_prefers_trust_tier_and_flags_conflicts()
    {
        var at = DateTime.UtcNow;
        var d = Survivorship.Select("nutrient.NA.PER_100G", [
            new Survivorship.Candidate("nutrient.NA.PER_100G", 340, "340", 2, at, 1, true),
            new Survivorship.Candidate("nutrient.NA.PER_100G", 280, "280", 1, at.AddDays(-30), 2, true),
        ]);
        Assert.Equal(2, d.Selected!.SourceRecordId);
        Assert.True(d.Conflict);
        var off = Survivorship.Select("nutrient.SUGAR.PER_100G", [
            new Survivorship.Candidate("nutrient.SUGAR.PER_100G", 10, "10", 1, at.AddDays(-90), 1, true),
            new Survivorship.Candidate("nutrient.SUGAR.PER_100G", 30, "30", 4, at, 2, true),
        ]);
        Assert.Equal(1, off.Selected!.SourceRecordId); // community data never overrides brand data
        Assert.False(off.Conflict);
    }

    [Fact]
    public void Sodium_is_derived_from_salt()
    {
        var n = new Nutrition { SaltG = 2.5m };
        Assert.Equal(1000m, n.Sodium);
        Assert.Equal(2.5m, new Nutrition { SodiumMg = 1000 }.Salt);
    }
}
