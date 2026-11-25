using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using HealthSwap.Domain;
using HealthSwap.Domain.Recommendation;

namespace HealthSwap.Platform.Llm;

/// <summary>
/// Deterministic offline provider used for local development, tests and CI. It honours the same prompts and schemas as
/// the Azure OpenAI provider so the whole pipeline (gateway, validators, audit, caching, fallbacks) runs without a model.
/// </summary>
public sealed partial class MockLlmProvider(LlmOptions options) : ILlmProvider
{
    private int _t5Calls;
    public string Name => "mock";

    [GeneratedRegex(@"<(?<tag>product|products|facts)>(?<body>.*)</\k<tag>>", RegexOptions.Singleline)]
    private static partial Regex Delimited();

    private static JsonObject Payload(string user) =>
        JsonNode.Parse(Delimited().Match(user) is { Success: true } m ? m.Groups["body"].Value : user)!.AsObject();

    public Task<ProviderResponse> CompleteJsonAsync(PromptSpec spec, string userContent, string deployment, CancellationToken ct)
    {
        var p = Payload(userContent);
        var json = spec.Task switch
        {
            LlmTask.T3 => Classify(p),
            LlmTask.T4 => Judge(p),
            LlmTask.T5 => Explain(p),
            _ => throw new InvalidOperationException($"Mock provider does not implement {spec.Task}"),
        };
        var tokensIn = userContent.Length / 4 + spec.System.Length / 4;
        return Task.FromResult(new ProviderResponse(json.ToJsonString(), tokensIn, json.ToJsonString().Length / 4, $"mock/{deployment}"));
    }

    public Task<float[][]> EmbedAsync(IReadOnlyList<string> texts, string deployment, int dimensions, CancellationToken ct) =>
        Task.FromResult(texts.Select(HashingEmbedder.Embed).ToArray());

    private static readonly (string Keyword, string Category)[] CategoryKeywords =
    [
        ("noodle", "noodles.instant"), ("oats", "breakfast.cereals"), ("cereal", "breakfast.cereals"), ("flakes", "breakfast.cereals"),
        ("muesli", "breakfast.cereals"), ("granola", "breakfast.cereals"), ("cola", "beverages.carbonated.soft-drinks"), ("soda", "beverages.carbonated.soft-drinks"),
        ("sparkling", "beverages.carbonated.flavoured-water"), ("biscuit", "biscuits.sweet"), ("cookie", "biscuits.sweet"), ("rusk", "biscuits.sweet"),
        ("chips", "snacks.chips"), ("crisps", "snacks.chips"), ("bhujia", "snacks.namkeen"), ("namkeen", "snacks.namkeen"), ("chana", "snacks.namkeen"),
        ("yogurt", "dairy.yogurt"), ("dahi", "dairy.yogurt"), ("curd", "dairy.yogurt"), ("bread", "bread.sliced"), ("chocolate milk", "beverages.flavoured-milk"),
        ("chocolate", "confectionery.chocolate"), ("ketchup", "sauces.ketchup"),
    ];

    private static JsonObject Classify(JsonObject p)
    {
        var text = ((p["name"]?.GetValue<string>() ?? "") + " " + (p["brand"]?.GetValue<string>() ?? "")).ToLowerInvariant();
        var allowed = p["allowed"]?.AsArray().Select(x => x!.GetValue<string>()).ToHashSet() ?? [];
        var hit = CategoryKeywords.FirstOrDefault(k => text.Contains(k.Keyword) && allowed.Contains(k.Category));
        return new JsonObject
        {
            ["categoryCode"] = hit.Category ?? allowed.FirstOrDefault() ?? "",
            ["confidence"] = hit.Category is null ? 0.2 : 0.86,
            ["rationale"] = hit.Category is null ? "No keyword matched" : $"Title mentions '{hit.Keyword}'",
        };
    }

    private static JsonObject Judge(JsonObject p)
    {
        var a = p["A"]!.AsObject();
        var b = p["B"]!.AsObject();
        static string? S(JsonObject o, string k) => o[k]?.GetValue<string>();
        var issues = new JsonArray();
        var score = S(a, "category") == S(b, "category") ? 0.82 : 0.62;
        var formMatch = S(a, "form") == S(b, "form");
        var useMatch = S(a, "useOccasion") == S(b, "useOccasion");
        if (!formMatch) { score -= 0.30; issues.Add("DIFFERENT_FORM"); }
        if (!useMatch) { score -= 0.25; issues.Add("DIFFERENT_USE"); }
        if (S(a, "flavourFamily") != S(b, "flavourFamily")) { score -= 0.12; issues.Add("DIFFERENT_FLAVOUR_FAMILY"); }
        if (S(a, "dietClass") != S(b, "dietClass") && !(S(a, "dietClass") is "VEG" or "VEGAN" && S(b, "dietClass") is "VEG" or "VEGAN"))
        { score -= 0.30; issues.Add("DIETARY_CLASS_MISMATCH"); }
        if (a["netQuantity"] is { } qa && b["netQuantity"] is { } qb && qa.GetValue<double>() > 0)
        {
            var ratio = qb.GetValue<double>() / qa.GetValue<double>();
            if (ratio is < 0.5 or > 2.0) { score -= 0.10; issues.Add("PACK_SIZE_MISMATCH"); }
        }
        if (issues.Count == 0) issues.Add("NONE");
        score = Math.Round(Math.Clamp(score + 0.1, 0, 1), 2);
        return new JsonObject
        {
            ["score"] = score, ["sameUseOccasion"] = useMatch, ["formMatch"] = formMatch, ["issues"] = issues,
            ["rationale"] = formMatch && useMatch ? "Same form and use occasion" : "Form or use occasion differs",
        };
    }

    private JsonObject Explain(JsonObject p)
    {
        var locale = p["locale"]?.GetValue<string>() ?? "en-IN";
        var facts = JsonSerializer.Deserialize<List<Fact>>(p["facts"]!.ToJsonString(), HealthSwap.Platform.Cache.HotCache.Json) ?? [];
        var lang = ExplanationTemplates.Lang(locale);
        var sentences = facts.Select(f => ExplanationTemplates.Sentence(f, lang, locale)).ToList();
        string shortText;
        if (sentences.Count >= 2)
        {
            var joiner = lang switch { "hi" => " और ", "es" => " y ", _ => ", plus " };
            var second = lang == "en" && sentences[1].Length > 0 ? char.ToLowerInvariant(sentences[1][0]) + sentences[1][1..] : sentences[1];
            shortText = sentences[0] + joiner + second;
            if (shortText.Length > 90) shortText = sentences[0];
        }
        else shortText = sentences.FirstOrDefault() ?? "";

        var n = Interlocked.Increment(ref _t5Calls);
        if (options.MockFaultEvery > 0 && n % options.MockFaultEvery == 0)
            shortText = lang == "en" ? "Guilt-free choice with 99% less sugar" : shortText + " 99%";

        return new JsonObject
        {
            ["short"] = shortText,
            ["bullets"] = new JsonArray(sentences.Take(3).Select(s => (JsonNode)s).ToArray()),
            ["factIdsUsed"] = new JsonArray(facts.Select(f => (JsonNode)f.Id).ToArray()),
        };
    }
}
