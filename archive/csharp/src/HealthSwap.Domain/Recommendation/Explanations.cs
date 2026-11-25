using System.Globalization;
using System.Text.RegularExpressions;
using HealthSwap.Domain.Scoring;

namespace HealthSwap.Domain.Recommendation;

/// <summary>One deterministic, verifiable comparison fact (doc 03 section 8).</summary>
public sealed record Fact
{
    public required string Id { get; init; }
    /// <summary>NUTRIENT, ADDITIVE_REMOVED, ADDITIVE_INTRODUCED or PROCESSING.</summary>
    public required string Kind { get; init; }
    public string? Nutrient { get; init; }
    public decimal From { get; init; }
    public decimal To { get; init; }
    public string Unit { get; init; } = "g";
    public string Basis { get; init; } = "PER_100G";
    public decimal? RelChange { get; init; }
    public string Direction { get; init; } = "SAME";
    public decimal Impact { get; init; }
    public bool ClaimEligible { get; init; }
    public bool MustDisclose { get; init; }
    public IReadOnlyList<string> Codes { get; init; } = [];
}

public sealed record FactSheet(string Basis, IReadOnlyList<Fact> Facts, string Hash);

public static class FactSheets
{
    private static readonly (string Key, string Api, string Unit, string Role, decimal Weight)[] Nutrients =
    [
        ("LIMITED_SUGARS_G", "totalSugarsG", "g", "LIMIT", 1.0m),
        ("SODIUM_MG", "sodiumMg", "mg", "LIMIT", 0.9m),
        ("SAT_FAT_G", "saturatedFatG", "g", "LIMIT", 0.9m),
        ("TRANS_FAT_G", "transFatG", "g", "LIMIT", 1.0m),
        ("FIBRE_G", "dietaryFibreG", "g", "ENCOURAGE", 0.6m),
        ("PROTEIN_G", "proteinG", "g", "ENCOURAGE", 0.4m),
    ];

    public static FactSheet Build(HealthProfileData a, HealthProfileData b, MarketPack pack)
    {
        var basis = b.Nutrition.IsLiquid ? "PER_100ML" : "PER_100G";
        var facts = new List<Fact>();
        var i = 0;
        var useAdded = a.Nutrition.AddedSugarsG is not null && b.Nutrition.AddedSugarsG is not null;
        foreach (var (key, api, unit, role, weight) in Nutrients)
        {
            var k = key == "LIMITED_SUGARS_G" && !useAdded ? "SUGARS_G" : key;
            var apiName = key == "LIMITED_SUGARS_G" && useAdded ? "addedSugarsG" : api;
            var av = a.Nutrition.Get(k);
            var bv = b.Nutrition.Get(k);
            if (av is null || bv is null) continue;
            av = Round(av.Value, unit);
            bv = Round(bv.Value, unit);
            if (av == bv) continue;
            var rel = av == 0 ? (decimal?)null : Math.Round((bv.Value - av.Value) / av.Value, 4);
            var better = role == "LIMIT" ? bv < av : bv > av;
            var threshold = pack.ComparativeThreshold(NutrientCode(key));
            // Differences below a meaningful absolute amount never become claims or headline facts (no "100% less trans fat" for 0.1 g).
            var material = Math.Abs(bv.Value - av.Value) >= MinimumDifference(key);
            var claimEligible = material && rel is { } r && Math.Abs(r) >= threshold && better;
            var impact = better && material ? weight * Math.Min(3m, Math.Abs(rel ?? 1m)) : 0m;
            facts.Add(new Fact
            {
                Id = "f" + ++i, Kind = "NUTRIENT", Nutrient = apiName, From = av.Value, To = bv.Value, Unit = unit, Basis = basis,
                RelChange = rel, Direction = better ? "BETTER" : "WORSE", Impact = Math.Round(impact, 4), ClaimEligible = claimEligible,
            });
        }

        var aColours = a.Additives.Where(x => IsColour(x)).Select(x => x.Code).ToHashSet();
        var bColours = b.Additives.Where(x => IsColour(x)).Select(x => x.Code).ToHashSet();
        var removedColours = aColours.Except(bColours).Order(StringComparer.Ordinal).ToList();
        if (removedColours.Count > 0 && bColours.Count == 0)
            facts.Add(new Fact { Id = "f" + ++i, Kind = "ADDITIVE_REMOVED", Nutrient = "artificialColours", Codes = removedColours, Direction = "BETTER", Impact = 0.5m, Basis = basis });

        var aCodes = a.Additives.Select(x => x.Code).ToHashSet();
        var introduced = b.Additives.Where(x => x.Tier is 'B' or 'C' && !aCodes.Contains(x.Code)).Select(x => x.Code).Order(StringComparer.Ordinal).ToList();
        if (introduced.Count > 0)
        {
            var sweetener = b.Additives.Any(x => introduced.Contains(x.Code) && x.FunctionalClass == "SWEETENER");
            facts.Add(new Fact
            {
                Id = "f" + ++i, Kind = "ADDITIVE_INTRODUCED", Nutrient = sweetener ? "sweeteners" : "additives", Codes = introduced,
                Direction = "WORSE", MustDisclose = true, Impact = 0, Basis = basis,
            });
        }
        if (a.Nova is { } na && b.Nova is { } nb && nb < na)
            facts.Add(new Fact { Id = "f" + ++i, Kind = "PROCESSING", Nutrient = "nova", From = na, To = nb, Unit = "", Direction = "BETTER", Impact = 0.3m * (na - nb), Basis = basis });

        var hash = Hash(basis, facts);
        return new FactSheet(basis, facts, hash);
    }

    /// <summary>Top facts by health impact plus every must-disclose fact (doc 03 8.2).</summary>
    public static IReadOnlyList<Fact> Select(FactSheet sheet, int max = 2) =>
        sheet.Facts.Where(f => f.Direction == "BETTER" && f.Impact > 0).OrderByDescending(f => f.Impact).ThenBy(f => f.Id, StringComparer.Ordinal).Take(max)
            .Concat(sheet.Facts.Where(f => f.MustDisclose)).ToList();

    private static bool IsColour(AdditiveDetail a) => a.FunctionalClass == "COLOUR" && a.Tier is 'B' or 'C';

    /// <summary>Smallest per-100 g/ml difference worth mentioning (below typical label rounding and analytical tolerance).</summary>
    public static decimal MinimumDifference(string key) => key switch
    {
        "LIMITED_SUGARS_G" => 1.0m,
        "SODIUM_MG" => 40m,
        "SAT_FAT_G" => 0.5m,
        "TRANS_FAT_G" => 0.2m,
        "FIBRE_G" => 1.0m,
        "PROTEIN_G" => 2.0m,
        _ => 0m,
    };

    private static string NutrientCode(string key) => key switch
    {
        "LIMITED_SUGARS_G" => "SUGAR",
        "SODIUM_MG" => "NA",
        "SAT_FAT_G" => "FASAT",
        "TRANS_FAT_G" => "FATRN",
        "FIBRE_G" => "FIBTG",
        "PROTEIN_G" => "PROCNT",
        _ => key,
    };

    public static decimal Round(decimal v, string unit) => unit == "mg" ? Math.Round(v, 0) : Math.Round(v, 1);

    private static string Hash(string basis, IEnumerable<Fact> facts)
    {
        var canonical = basis + "|" + string.Join("|", facts.Select(f =>
            $"{f.Kind}:{f.Nutrient}:{f.From.ToString(CultureInfo.InvariantCulture)}:{f.To.ToString(CultureInfo.InvariantCulture)}:{string.Join(",", f.Codes)}"));
        return Convert.ToHexStringLower(System.Security.Cryptography.SHA256.HashData(System.Text.Encoding.UTF8.GetBytes(canonical)))[..32];
    }
}

public sealed record RenderedExplanation(string Short, IReadOnlyList<string> Bullets, IReadOnlyList<string> FactIdsUsed, string Generator, bool AiGenerated, string Locale);

/// <summary>Deterministic, claim-safe templates (en-IN, en-US, hi-IN, es-US). Claim wording only above thresholds.</summary>
public static class ExplanationTemplates
{
    private static readonly Dictionary<string, Dictionary<string, string>> NutrientNames = new()
    {
        ["en"] = new() { ["totalSugarsG"] = "sugar", ["addedSugarsG"] = "added sugar", ["sodiumMg"] = "sodium", ["saturatedFatG"] = "saturated fat",
            ["transFatG"] = "trans fat", ["dietaryFibreG"] = "fibre", ["proteinG"] = "protein" },
        ["hi"] = new() { ["totalSugarsG"] = "चीनी", ["addedSugarsG"] = "अतिरिक्त चीनी", ["sodiumMg"] = "सोडियम", ["saturatedFatG"] = "संतृप्त वसा",
            ["transFatG"] = "ट्रांस वसा", ["dietaryFibreG"] = "फाइबर", ["proteinG"] = "प्रोटीन" },
        ["es"] = new() { ["totalSugarsG"] = "azúcar", ["addedSugarsG"] = "azúcar añadido", ["sodiumMg"] = "sodio", ["saturatedFatG"] = "grasa saturada",
            ["transFatG"] = "grasa trans", ["dietaryFibreG"] = "fibra", ["proteinG"] = "proteína" },
    };

    public static string Lang(string locale) => locale.Split('-')[0] switch { "hi" => "hi", "es" => "es", _ => "en" };

    public static RenderedExplanation Render(IReadOnlyList<Fact> selected, string locale)
    {
        var lang = Lang(locale);
        var sentences = new List<(string Id, string Text)>();
        foreach (var f in selected) sentences.Add((f.Id, Sentence(f, lang, locale)));
        var shortText = string.Join(" · ", sentences.Select(s => s.Text));
        if (shortText.Length > 90) shortText = sentences[0].Text;
        if (shortText.Length > 90) shortText = shortText[..87] + "...";
        return new RenderedExplanation(shortText, sentences.Take(3).Select(s => s.Text.Length > 80 ? s.Text[..77] + "..." : s.Text).ToList(),
            sentences.Select(s => s.Id).ToList(), "TEMPLATE", false, locale);
    }

    public static string Sentence(Fact f, string lang, string locale)
    {
        var per = f.Basis == "PER_100ML" ? "100 ml" : "100 g";
        var perHi = f.Basis == "PER_100ML" ? "100 मिली" : "100 ग्राम";
        switch (f.Kind)
        {
            case "ADDITIVE_REMOVED":
                return lang switch
                {
                    "hi" => $"कोई कृत्रिम रंग नहीं (INS {string.Join(", ", f.Codes)} हटाए गए)",
                    "es" => $"Sin colorantes artificiales (INS {string.Join(", ", f.Codes)})",
                    _ => $"No artificial colours (INS {string.Join(", ", f.Codes)})",
                };
            case "ADDITIVE_INTRODUCED":
                var sweet = f.Nutrient == "sweeteners";
                return lang switch
                {
                    "hi" => sweet ? $"स्वीटनर शामिल है (INS {string.Join(", ", f.Codes)})" : $"योजक शामिल है (INS {string.Join(", ", f.Codes)})",
                    "es" => sweet ? $"Contiene edulcorante (INS {string.Join(", ", f.Codes)})" : $"Contiene aditivo (INS {string.Join(", ", f.Codes)})",
                    _ => sweet ? $"Contains sweetener (INS {string.Join(", ", f.Codes)})" : $"Contains additive (INS {string.Join(", ", f.Codes)})",
                };
            case "PROCESSING":
                return lang switch
                {
                    "hi" => $"कम प्रोसेस्ड (NOVA {N(f.To)} बनाम {N(f.From)})",
                    "es" => $"Menos procesado (NOVA {N(f.To)} vs {N(f.From)})",
                    _ => $"Less processed (NOVA {N(f.To)} vs {N(f.From)})",
                };
        }

        var name = NutrientNames[lang].GetValueOrDefault(f.Nutrient ?? "", f.Nutrient ?? "");
        var unitHi = f.Unit == "mg" ? "मि.ग्रा." : "ग्राम";
        var from = V(f.From, f.Unit);
        var to = V(f.To, f.Unit);
        if (f.ClaimEligible && f.RelChange is { } rel)
        {
            var pct = Pct(rel);
            if (rel < 0)
                return lang switch
                {
                    "hi" => $"प्रति {perHi} {pct}% कम {name}",
                    "es" => $"{pct}% menos {name} por {per}",
                    _ => $"{pct}% less {name} per {per}",
                };
            var mult = Multiplier(f.From, f.To);
            if (mult is not null)
                return lang switch
                {
                    "hi" => $"प्रति {perHi} {mult} गुना {name} ({to} बनाम {from} {unitHi})",
                    "es" => $"{mult} veces la {name} por {per} ({to} g vs {from} g)",
                    _ => $"{mult}x the {name} per {per} ({to} {f.Unit} vs {from} {f.Unit})",
                };
            return lang switch
            {
                "hi" => $"प्रति {perHi} {pct}% अधिक {name}",
                "es" => $"{pct}% más {name} por {per}",
                _ => $"{pct}% more {name} per {per}",
            };
        }
        return lang switch
        {
            "hi" => $"प्रति {perHi} {name}: {to} बनाम {from} {unitHi}",
            "es" => $"{to} {f.Unit} vs {from} {f.Unit} de {name} por {per}",
            _ => $"{to} {f.Unit} vs {from} {f.Unit} {name} per {per}",
        };
    }

    /// <summary>Percent is floored so the copy never overstates the difference.</summary>
    public static int Pct(decimal rel) => (int)Math.Floor(Math.Abs(rel) * 100m);

    public static string? Multiplier(decimal from, decimal to)
    {
        if (from <= 0 || to / from < 2m) return null;
        var m = Math.Floor(to / from * 10m) / 10m;
        return m == Math.Floor(m) ? ((int)m).ToString(CultureInfo.InvariantCulture) : m.ToString("0.0", CultureInfo.InvariantCulture);
    }

    public static string V(decimal v, string unit) => unit == "mg" ? Math.Round(v, 0).ToString("0", CultureInfo.InvariantCulture) : Math.Round(v, 1).ToString("0.0", CultureInfo.InvariantCulture);

    private static string N(decimal v) => ((int)v).ToString(CultureInfo.InvariantCulture);
}

public sealed record ValidationReport(bool Passed, IReadOnlyDictionary<string, bool> Checks, IReadOnlyList<string> Problems);

/// <summary>
/// Validator chain for LLM-polished explanations (doc 03 9.3): schema/length, numeric fidelity (every number must come
/// from the facts), banned claims per locale, language ID, and mandatory disclosures. Any failure falls back to the template.
/// </summary>
public static partial class ExplanationValidator
{
    public static readonly IReadOnlyDictionary<string, string[]> BannedTerms = new Dictionary<string, string[]>
    {
        ["en"] = ["cure", "cures", "prevent", "prevents", "treat", "treats", "treatment", "diabetic-safe", "diabetic", "doctor recommended",
                  "detox", "immunity booster", "boosts immunity", "chemical-free", "toxic", "poison", "guilt-free", "healthiest",
                  "100% healthy", "clinically proven", "superfood", "miracle"],
        ["hi"] = ["इलाज", "रोकथाम", "डिटॉक्स", "रोग प्रतिरोधक", "ज़हर", "जहर", "विषैला", "सबसे स्वस्थ", "मधुमेह", "डॉक्टर द्वारा", "चमत्कार", "केमिकल-मुक्त"],
        ["es"] = ["cura", "curar", "previene", "prevenir", "tratamiento", "desintoxica", "detox", "inmunidad", "tóxico", "veneno",
                  "sin culpa", "el más saludable", "clínicamente probado", "milagro"],
    };

    [GeneratedRegex(@"\d+(?:[.,]\d+)?")]
    private static partial Regex NumberRx();

    public static ValidationReport Validate(string shortText, IReadOnlyList<string> bullets, IReadOnlyList<Fact> facts, string locale, bool regulatedHealthyAllowed = false)
    {
        var checks = new Dictionary<string, bool>();
        var problems = new List<string>();
        var lang = ExplanationTemplates.Lang(locale);
        var all = string.Join(" ", new[] { shortText }.Concat(bullets));

        checks["schema"] = shortText.Length is > 0 and <= 90 && bullets.Count <= 3 && bullets.All(b => b.Length <= 80);
        if (!checks["schema"]) problems.Add("Length or bullet limits exceeded");

        var allowed = AllowedNumbers(facts);
        var normalizedDigits = ToAsciiDigits(all);
        var unexpected = NumberRx().Matches(normalizedDigits).Select(m => m.Value.Replace(',', '.')).Where(n => !allowed.Contains(Canon(n))).Distinct().ToList();
        checks["numericFidelity"] = unexpected.Count == 0;
        if (unexpected.Count > 0) problems.Add("Numbers not present in facts: " + string.Join(", ", unexpected));

        var lower = all.ToLowerInvariant();
        var banned = BannedTerms[lang].Where(t => Regex.IsMatch(lower, $@"(^|[^\p{{L}}]){Regex.Escape(t.ToLowerInvariant())}([^\p{{L}}]|$)")).ToList();
        if (!regulatedHealthyAllowed && lang == "en" && Regex.IsMatch(lower, @"\bhealthy\b")) banned.Add("healthy");
        if (!regulatedHealthyAllowed && lang == "es" && Regex.IsMatch(lower, @"\bsaludable\b")) banned.Add("saludable");
        checks["claims"] = banned.Count == 0;
        if (banned.Count > 0) problems.Add("Banned terms: " + string.Join(", ", banned));

        var letters = all.Where(char.IsLetter).ToList();
        var devanagari = letters.Count(c => c is >= '\u0900' and <= '\u097F');
        checks["language"] = lang switch
        {
            "hi" => letters.Count > 0 && devanagari >= letters.Count * 0.3,
            "es" => devanagari == 0 && Regex.IsMatch(lower, @"\b(por|menos|más|sin|contiene|veces|de|procesado)\b"),
            _ => devanagari == 0,
        };
        if (!checks["language"]) problems.Add("Language does not match " + locale);

        var missing = facts.Where(f => f.MustDisclose && !f.Codes.All(c => all.Contains(c, StringComparison.OrdinalIgnoreCase))).Select(f => f.Id).ToList();
        checks["mustDisclose"] = missing.Count == 0;
        if (missing.Count > 0) problems.Add("Mandatory disclosures missing: " + string.Join(", ", missing));

        return new ValidationReport(checks.Values.All(v => v), checks, problems);
    }

    public static HashSet<string> AllowedNumbers(IEnumerable<Fact> facts)
    {
        var set = new HashSet<string> { "100" };
        foreach (var f in facts)
        {
            foreach (var v in new[] { f.From, f.To })
            {
                set.Add(Canon(v.ToString(CultureInfo.InvariantCulture)));
                set.Add(Canon(Math.Round(v, 1).ToString("0.0", CultureInfo.InvariantCulture)));
                set.Add(Canon(Math.Round(v, 0).ToString("0", CultureInfo.InvariantCulture)));
            }
            if (f.RelChange is { } rel)
            {
                set.Add(ExplanationTemplates.Pct(rel).ToString(CultureInfo.InvariantCulture));
                set.Add(((int)Math.Round(Math.Abs(rel) * 100m, MidpointRounding.AwayFromZero)).ToString(CultureInfo.InvariantCulture));
            }
            if (ExplanationTemplates.Multiplier(f.From, f.To) is { } m)
            {
                set.Add(Canon(m));
                set.Add(Canon(Math.Round(f.To / f.From, 1).ToString("0.0", CultureInfo.InvariantCulture)));
            }
            foreach (var code in f.Codes)
                foreach (Match d in NumberRx().Matches(code)) set.Add(Canon(d.Value));
        }
        return set;
    }

    private static string Canon(string n)
    {
        if (!decimal.TryParse(n, NumberStyles.Number, CultureInfo.InvariantCulture, out var d)) return n;
        return d.ToString("0.####", CultureInfo.InvariantCulture);
    }

    public static string ToAsciiDigits(string s)
    {
        var chars = s.ToCharArray();
        for (var i = 0; i < chars.Length; i++)
            if (chars[i] is >= '\u0966' and <= '\u096F') chars[i] = (char)('0' + (chars[i] - '\u0966'));
        return new string(chars);
    }
}
