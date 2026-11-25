using System.Globalization;
using HealthSwap.Contracts;
using HealthSwap.Domain;
using HealthSwap.Domain.Knowledge;
using HealthSwap.Domain.Recommendation;
using HealthSwap.Domain.Scoring;
using Money = HealthSwap.Contracts.Money;
using RecommendationLabel = HealthSwap.Contracts.RecommendationLabel;

namespace HealthSwap.Platform;

/// <summary>Maps internal read models to the public API contract (api/openapi.yaml).</summary>
public static class ApiMapper
{
    public static Money Money(decimal amount, string currency) =>
        new(Currency.Round(amount, currency).ToString("F" + Currency.MinorUnits(currency), CultureInfo.InvariantCulture), currency);

    public static string[] LocalesFor(string market) => market switch
    {
        "IN" => ["en-IN", "hi-IN"],
        "US" => ["en-US", "es-US"],
        _ => ["en-" + market],
    };

    public static string DefaultLocale(string market) => LocalesFor(market)[0];

    /// <summary>Normalizes a requested locale to one with pre-generated explanations for the market.</summary>
    public static string ResolveLocale(string market, string? requested)
    {
        var locales = LocalesFor(market);
        if (requested is null) return locales[0];
        var exact = locales.FirstOrDefault(l => l.Equals(requested, StringComparison.OrdinalIgnoreCase));
        if (exact is not null) return exact;
        var lang = requested.Split('-')[0];
        return locales.FirstOrDefault(l => l.StartsWith(lang + "-", StringComparison.OrdinalIgnoreCase)) ?? locales[0];
    }

    public static HealthScore? Score(ModelScore? s) => s is null ? null : new HealthScore
    {
        Model = s.Code, Version = s.Version, Value = s.Value, Grade = s.Grade,
        Components = s.Components.Count == 0 ? null : s.Components.ToDictionary(kv => kv.Key, kv => (object)kv.Value),
    };

    public static HealthProfileSummary Summary(HealthProfileData hp) => new()
    {
        ProductId = hp.ProductId, Hsi = hp.Hsi, HsiGrade = hp.HsiGrade, PrimaryScore = Score(hp.Primary), Flags = hp.Flags.ToList(),
        DataSufficiency = hp.DataSufficiency,
    };

    public static NetQuantityDto? Net(NetQuantity? n) => n is { } q ? new NetQuantityDto(q.Value, q.Unit) : null;

    public static NutritionPanel Panel(HealthProfileData hp)
    {
        var n = hp.Nutrition;
        return new NutritionPanel
        {
            Basis = n.Basis,
            ServingSize = hp.ServingSize is { } s && s > 0 && hp.ServingUnit is { } u ? new NetQuantityDto(s, u) : null,
            EnergyKcal = n.EnergyKcal, ProteinG = n.ProteinG, CarbohydrateG = n.CarbohydrateG, TotalSugarsG = n.TotalSugarsG, AddedSugarsG = n.AddedSugarsG,
            TotalFatG = n.TotalFatG, SaturatedFatG = n.SaturatedFatG, TransFatG = n.TransFatG, CholesterolMg = n.CholesterolMg, SodiumMg = n.Sodium,
            SaltG = n.Salt, DietaryFibreG = n.FibreG,
            Other = n.FvlnPct is { } f ? [new OtherNutrient("FVLN_PCT", f, "percent")] : null,
        };
    }

    /// <summary>Internal regulatory/certification marks -> public DietaryMark enum.</summary>
    public static List<string> DietaryMarks(IEnumerable<string> marks, string dietClass)
    {
        var result = new SortedSet<string>(StringComparer.Ordinal);
        foreach (var m in marks)
        {
            var mapped = m switch
            {
                "IN_FSSAI_VEG" => "VEG", "IN_FSSAI_NON_VEG" => "NON_VEG", "IN_FSSAI_VEGAN" or "VEGAN_CERTIFIED" => "VEGAN",
                "IN_JAIVIK_BHARAT" or "US_USDA_ORGANIC" => "ORGANIC", "IN_FSSAI_FORTIFIED" => "FORTIFIED", "HALAL_CERTIFIED" => "HALAL",
                "KOSHER_CERTIFIED" => "KOSHER", "GLUTEN_FREE_CLAIM" => "GLUTEN_FREE", _ => null,
            };
            if (mapped is not null) result.Add(mapped);
        }
        if (dietClass == "EGG") result.Add("EGG");
        return result.ToList();
    }

    /// <summary>Public DietaryMark enum -> internal mark codes (partner catalog input).</summary>
    public static List<string> InternalMarks(IEnumerable<string>? marks, string market) =>
        (marks ?? []).Select(m => m switch
        {
            "VEG" => market == "IN" ? "IN_FSSAI_VEG" : null, "NON_VEG" => market == "IN" ? "IN_FSSAI_NON_VEG" : null,
            "VEGAN" => market == "IN" ? "IN_FSSAI_VEGAN" : "VEGAN_CERTIFIED", "ORGANIC" => market == "IN" ? "IN_JAIVIK_BHARAT" : "US_USDA_ORGANIC",
            "FORTIFIED" => "IN_FSSAI_FORTIFIED", "HALAL" => "HALAL_CERTIFIED", "KOSHER" => "KOSHER_CERTIFIED", "GLUTEN_FREE" => "GLUTEN_FREE_CLAIM",
            _ => null,
        }).Where(m => m is not null).Cast<string>().Distinct().ToList();

    public static string Presence(AllergenPresence p) => p switch
    {
        AllergenPresence.Contains => "CONTAINS", AllergenPresence.MayContain => "MAY_CONTAIN", _ => "FREE_FROM",
    };

    public static List<DataAttribution> Attributions(IEnumerable<string> sources)
    {
        var list = new List<DataAttribution>();
        foreach (var s in sources.Distinct())
        {
            if (s == "OFF") list.Add(new DataAttribution("Open Food Facts", "ODbL-1.0", "Contains data from Open Food Facts, available under the Open Database License."));
            if (s.StartsWith("USDA_FDC", StringComparison.Ordinal))
                list.Add(new DataAttribution("USDA FoodData Central", "CC0-1.0", "U.S. Department of Agriculture, Agricultural Research Service. FoodData Central."));
        }
        return list;
    }

    public static HealthProfile Profile(HealthProfileData hp, string? locale = null)
    {
        var loc = ResolveLocale(hp.Market, locale);
        return new HealthProfile
        {
            ProductId = hp.ProductId, Market = hp.Market, Gtin = hp.Gtin, DisplayName = hp.Name, Brand = hp.Brand,
            Category = hp.CategoryId is { } cid ? new CategoryRef(cid.ToString(), hp.CategoryName ?? hp.CategoryCode) : null,
            NetQuantity = Net(hp.NetQuantity), Hsi = hp.Hsi, HsiGrade = hp.HsiGrade, PrimaryScore = Score(hp.Primary),
            Scores = hp.Scores.Select(Score).Cast<HealthScore>().ToList(), NovaGroup = hp.Nova, Flags = hp.Flags.ToList(),
            Badges = hp.Badges.Select(b => new Badge(b.Code, ShopperCopy.BadgeLabel(b.Code, b.Label, loc), b.Tone)).ToList(),
            Nutrition = [Panel(hp)],
            Allergens = hp.Allergens.OrderBy(a => a.Key, StringComparer.Ordinal).Select(a => new AllergenInfo(a.Key, Presence(a.Value))).ToList(),
            DietaryMarks = DietaryMarks(hp.Marks, hp.DietClass),
            Additives = hp.Additives.Select(a => new AdditiveInfo
            {
                InsNumber = a.Code, ENumber = a.ENumber, Name = a.Name, FunctionalClass = a.FunctionalClass, ConcernTier = a.Tier.ToString(),
            }).ToList(),
            UsFdaHealthyEligible = hp.UsFdaHealthy, Recalled = hp.Recalled, DataSufficiency = hp.DataSufficiency,
            DataConfidence = Math.Clamp(hp.Confidence, 0, 1), Attributions = Attributions(hp.Sources), ProfileVersion = hp.ProfileVersion,
            ComputedAt = hp.ComputedAt,
        };
    }

    public static ProductSummary Product(HealthProfileData hp, string partnerSku, string? title = null) => new()
    {
        PartnerSku = partnerSku, ProductId = hp.ProductId, Title = title ?? hp.Name, Brand = hp.Brand, NetQuantity = Net(hp.NetQuantity), Health = Summary(hp),
    };

    public static RecommendationLabel Label(Domain.Recommendation.RecommendationLabel l) => new()
    {
        Code = l.Code, Text = l.Text, ShortText = l.ShortText, Disclosure = l.Disclosure, Chargeable = l.Chargeable, FeeRule = l.FeeRule,
    };

    public static List<NutrientComparison> Details(IEnumerable<Fact> facts) =>
        facts.Where(f => f.Kind == "NUTRIENT").Select(f => new NutrientComparison
        {
            Nutrient = f.Nutrient ?? "", Basis = f.Basis, From = f.From, To = f.To, Unit = f.Unit, RelativeChange = f.RelChange,
            Direction = f.Direction switch { "BETTER" => "BETTER", "WORSE" => "WORSE", _ => "SAME" },
        }).ToList();
}
