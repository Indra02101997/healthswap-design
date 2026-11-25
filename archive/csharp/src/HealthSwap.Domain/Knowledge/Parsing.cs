using System.Globalization;
using System.Text;
using System.Text.RegularExpressions;

namespace HealthSwap.Domain.Knowledge;

public sealed record IngredientEntry(string CanonicalName, IReadOnlyCollection<string> Flags, IReadOnlyCollection<string> Allergens);

public sealed record AdditiveEntry(string Code, string Name, IReadOnlyCollection<string> FunctionalClasses, IReadOnlyCollection<string> Aliases);

/// <summary>Ingredient taxonomy and additive master (kb.ingredient, kb.ingredient_alias, kb.additive, kb.additive_alias).</summary>
public sealed class IngredientTaxonomy
{
    private readonly Dictionary<string, IngredientEntry> _byAlias = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, AdditiveEntry> _additiveByCode = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, AdditiveEntry> _additiveByName = new(StringComparer.OrdinalIgnoreCase);
    private readonly List<string> _aliasesLongestFirst = [];

    private static readonly Dictionary<string, string> ClassTitles = new(StringComparer.OrdinalIgnoreCase)
    {
        ["emulsifier"] = "EMULSIFIER", ["emulsifiers"] = "EMULSIFIER", ["emulsifying agent"] = "EMULSIFIER",
        ["raising agent"] = "RAISING_AGENT", ["raising agents"] = "RAISING_AGENT", ["leavening agent"] = "RAISING_AGENT",
        ["leavening agents"] = "RAISING_AGENT", ["leavening"] = "RAISING_AGENT",
        ["colour"] = "COLOUR", ["colours"] = "COLOUR", ["color"] = "COLOUR", ["colors"] = "COLOUR",
        ["permitted synthetic food colour"] = "COLOUR", ["permitted synthetic food colours"] = "COLOUR",
        ["permitted natural colour"] = "COLOUR", ["acidity regulator"] = "ACIDITY_REGULATOR",
        ["acidity regulators"] = "ACIDITY_REGULATOR", ["acidulant"] = "ACIDITY_REGULATOR", ["preservative"] = "PRESERVATIVE",
        ["preservatives"] = "PRESERVATIVE", ["class ii preservative"] = "PRESERVATIVE", ["antioxidant"] = "ANTIOXIDANT",
        ["antioxidants"] = "ANTIOXIDANT", ["stabiliser"] = "STABILISER", ["stabilisers"] = "STABILISER",
        ["stabilizer"] = "STABILISER", ["stabilizers"] = "STABILISER", ["thickener"] = "THICKENER",
        ["thickeners"] = "THICKENER", ["sweetener"] = "SWEETENER", ["sweeteners"] = "SWEETENER",
        ["artificial sweetener"] = "SWEETENER", ["artificial sweeteners"] = "SWEETENER",
        ["flavour enhancer"] = "FLAVOUR_ENHANCER", ["flavour enhancers"] = "FLAVOUR_ENHANCER",
        ["flavor enhancer"] = "FLAVOUR_ENHANCER", ["humectant"] = "HUMECTANT", ["anticaking agent"] = "ANTICAKING_AGENT",
        ["anti-caking agent"] = "ANTICAKING_AGENT", ["firming agent"] = "FIRMING_AGENT", ["glazing agent"] = "GLAZING_AGENT",
        ["flour treatment agent"] = "FLOUR_TREATMENT_AGENT", ["improver"] = "FLOUR_TREATMENT_AGENT",
    };

    public IngredientTaxonomy(IEnumerable<(string Alias, IngredientEntry Entry)> ingredients, IEnumerable<AdditiveEntry> additives)
    {
        foreach (var (alias, entry) in ingredients)
            _byAlias[Normalize(alias)] = entry;
        _aliasesLongestFirst.AddRange(_byAlias.Keys.OrderByDescending(k => k.Length));
        foreach (var a in additives)
        {
            _additiveByCode[a.Code] = a;
            _additiveByName[Normalize(a.Name)] = a;
            foreach (var alias in a.Aliases) _additiveByName[Normalize(alias)] = a;
        }
    }

    public AdditiveEntry? AdditiveByCode(string code) => _additiveByCode.GetValueOrDefault(code);
    public IEnumerable<AdditiveEntry> Additives => _additiveByCode.Values;

    public static string Normalize(string s)
    {
        var t = s.Trim().ToLowerInvariant().Normalize(NormalizationForm.FormKC);
        t = Regex.Replace(t, @"\s+", " ");
        return t.Trim(' ', '.', '*', ':', '-');
    }

    public static bool TryClassTitle(string head, out string functionalClass)
    {
        var h = Normalize(head);
        return ClassTitles.TryGetValue(h, out functionalClass!) ||
               (h.EndsWith('s') && ClassTitles.TryGetValue(h[..^1], out functionalClass!));
    }

    public IngredientEntry? Resolve(string text)
    {
        var t = Normalize(text);
        if (t.Length == 0) return null;
        if (_byAlias.TryGetValue(t, out var e)) return e;
        if (t.EndsWith('s') && _byAlias.TryGetValue(t[..^1], out e)) return e;
        if (t.EndsWith("es") && _byAlias.TryGetValue(t[..^2], out e)) return e;
        // Longest known alias contained as whole words, e.g. "refined palm oil" -> "palm oil".
        foreach (var alias in _aliasesLongestFirst)
        {
            if (alias.Length < 3 || alias.Length > t.Length) continue;
            var idx = t.IndexOf(alias, StringComparison.Ordinal);
            if (idx < 0) continue;
            var before = idx == 0 || !char.IsLetter(t[idx - 1]);
            var after = idx + alias.Length == t.Length || !char.IsLetter(t[idx + alias.Length]);
            if (before && after) return _byAlias[alias];
        }
        return null;
    }

    public AdditiveEntry? ResolveAdditiveName(string text)
    {
        var t = Normalize(text);
        if (_additiveByName.TryGetValue(t, out var a)) return a;
        foreach (var (name, entry) in _additiveByName)
            if (name.Length >= 4 && Regex.IsMatch(t, $@"(^|[^a-z]){Regex.Escape(name)}([^a-z]|$)")) return entry;
        return null;
    }
}

public sealed record IngredientParseResult(
    IReadOnlyList<ParsedIngredient> Tokens,
    IReadOnlyList<string> Additives,
    IReadOnlySet<string> Flags,
    IReadOnlySet<string> AllergenHints,
    decimal ResolutionRatio,
    decimal? FvlnPctEstimate,
    bool FvlnFromQuid);

/// <summary>
/// Deterministic ingredient tokenizer (doc 02 7.2): nested brackets, percentages (QUID), class titles with INS numbers
/// ("Raising agents (INS 500(ii), 503(ii))"), E-numbers and US additive names ("Red 40"). LLMs are never used here.
/// </summary>
public static partial class IngredientParser
{
    [GeneratedRegex(@"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%")]
    private static partial Regex PercentRx();

    [GeneratedRegex(@"\b(?:INS|E)\s*-?\s*(\d{3,4})\s*([a-z])?(?:\s*\(\s*[ivx]+\s*\))?", RegexOptions.IgnoreCase)]
    private static partial Regex PrefixedCodeRx();

    [GeneratedRegex(@"^\s*(\d{3,4})\s*([a-z])?(?:\s*\(\s*[ivx]+\s*\))?\s*$", RegexOptions.IgnoreCase)]
    private static partial Regex BareCodeRx();

    public static IngredientParseResult Parse(string? text, IngredientTaxonomy taxonomy)
    {
        var tokens = new List<ParsedIngredient>();
        if (string.IsNullOrWhiteSpace(text))
            return new IngredientParseResult(tokens, [], new HashSet<string>(), new HashSet<string>(), 0m, null, false);

        var cleaned = text.Replace('[', '(').Replace(']', ')').Replace('{', '(').Replace('}', ')');
        cleaned = Regex.Replace(cleaned, @"^\s*(ingredients?|सामग्री)\s*[:\-]\s*", "", RegexOptions.IgnoreCase);
        cleaned = cleaned.TrimEnd('.', ' ');
        var position = 0;
        ParseList(cleaned, null, taxonomy, tokens, ref position);

        var additives = tokens.Where(t => t.AdditiveCode is not null).Select(t => t.AdditiveCode!).Distinct().ToList();
        var flags = new HashSet<string>(tokens.SelectMany(t => t.Flags));
        var allergenHints = new HashSet<string>();
        foreach (var t in tokens.Where(t => t.CanonicalName is not null))
        {
            var e = taxonomy.Resolve(t.CanonicalName!);
            if (e is not null) foreach (var a in e.Allergens) allergenHints.Add(a);
        }
        foreach (var code in additives)
        {
            var a = taxonomy.AdditiveByCode(code);
            if (a is null) continue;
            if (a.FunctionalClasses.Contains("SWEETENER") && IsNonSugarSweetener(code)) flags.Add("NNS");
            if (a.FunctionalClasses.Contains("COLOUR") && IsSyntheticColour(code)) flags.Add("ARTIFICIAL_COLOUR");
            if (code is "621" or "627" or "631" or "635") flags.Add("ADDED_MSG");
        }
        // Lecithin (INS 322) implies soy only when the label says so (it may be sunflower lecithin).
        if (tokens.Any(t => t.AdditiveCode == "322" && Regex.IsMatch(t.RawText, @"\bsoy|\bsoya", RegexOptions.IgnoreCase)))
            allergenHints.Add("SOY");

        var countable = tokens.Where(t => !t.RawText.StartsWith("class:", StringComparison.Ordinal)).ToList();
        var resolved = countable.Count(t => t.CanonicalName is not null || t.AdditiveCode is not null);
        var ratio = countable.Count == 0 ? 0m : Math.Round((decimal)resolved / countable.Count, 3);

        // Fruit/vegetable/legume/nut share: QUID when declared, else a deterministic position-based estimate.
        decimal? fvl = null;
        var fromQuid = false;
        var fvlTokens = tokens.Where(t => t.ParentPosition is null && t.Flags.Contains("FRUIT_VEG_LEGUME")).ToList();
        if (fvlTokens.Count > 0)
        {
            if (fvlTokens.All(t => t.Percent is not null))
            {
                fvl = Math.Min(100m, fvlTokens.Sum(t => t.Percent!.Value));
                fromQuid = true;
            }
            else
            {
                var top = tokens.Where(t => t.ParentPosition is null).Select(t => t.Position).ToList();
                decimal sum = 0;
                foreach (var t in fvlTokens)
                {
                    if (t.Percent is { } p) { sum += p; continue; }
                    var rank = top.IndexOf(t.Position);
                    sum += rank switch { 0 => 40m, 1 => 20m, 2 => 10m, _ => 5m };
                }
                fvl = Math.Min(100m, sum);
            }
        }
        return new IngredientParseResult(tokens, additives, flags, allergenHints, ratio, fvl, fromQuid);
    }

    public static bool IsNonSugarSweetener(string code) =>
        code is "950" or "951" or "952" or "954" or "955" or "960" or "961" or "962" or "969";

    public static bool IsSyntheticColour(string code) =>
        code is "102" or "104" or "110" or "122" or "123" or "124" or "127" or "129" or "132" or "133" or "142" or "143" or "151" or "155" or "171";

    private static void ParseList(string text, int? parent, IngredientTaxonomy tax, List<ParsedIngredient> output, ref int position)
    {
        foreach (var part in SplitTopLevel(text))
        {
            foreach (var piece in part.Split(new[] { " and/or ", " & " }, StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries))
                ParseToken(piece, parent, tax, output, ref position);
        }
    }

    private static void ParseToken(string token, int? parent, IngredientTaxonomy tax, List<ParsedIngredient> output, ref int position)
    {
        var (head, groups) = SplitHead(token);
        if (head.Length == 0 && groups.Count == 0) return;
        decimal? percent = null;
        var pm = PercentRx().Match(head);
        if (pm.Success)
        {
            percent = decimal.Parse(pm.Groups[1].Value, CultureInfo.InvariantCulture);
            head = PercentRx().Replace(head, "").Trim();
        }

        // Class title: every member of the bracket group is an additive code or additive name.
        if (TryClassTitleWithInlineCode(head, out var fclass, out var inlineCode) || IngredientTaxonomy.TryClassTitle(head, out fclass))
        {
            var classPos = ++position;
            output.Add(new ParsedIngredient(classPos, parent, "class:" + fclass, null, null, true, null, []));
            if (inlineCode is not null) AddAdditive(inlineCode, head, classPos, tax, output, ref position);
            foreach (var g in groups)
                foreach (var member in SplitTopLevel(g))
                {
                    var code = ExtractCode(member);
                    if (code is not null) AddAdditive(code, member, classPos, tax, output, ref position);
                    else
                    {
                        var byName = tax.ResolveAdditiveName(member);
                        if (byName is not null) AddAdditive(byName.Code, member, classPos, tax, output, ref position);
                        else output.Add(new ParsedIngredient(++position, classPos, member.Trim(), null, null, true, null, []));
                    }
                }
            return;
        }

        // Plain ingredient, possibly with an alias, percentage, additive code or sub-ingredient list in brackets.
        var headCode = ExtractCode(head);
        var additiveByName = headCode is null ? tax.ResolveAdditiveName(head) : null;
        var entry = tax.Resolve(head);
        var myPos = ++position;
        var subLists = new List<string>();
        foreach (var g in groups)
        {
            var gp = PercentRx().Match(g);
            if (gp.Success && PercentRx().Replace(g, "").Trim().Length == 0)
            {
                percent ??= decimal.Parse(gp.Groups[1].Value, CultureInfo.InvariantCulture);
                continue;
            }
            var code = ExtractCode(g);
            if (code is not null && !g.Contains(',')) { headCode ??= code; continue; }
            if (entry is null && !g.Contains(','))
            {
                var aliasEntry = tax.Resolve(g);
                if (aliasEntry is not null) { entry = aliasEntry; continue; }
            }
            subLists.Add(g);
        }

        if (headCode is not null || (additiveByName is not null && entry is null))
        {
            var code = headCode ?? additiveByName!.Code;
            var a = tax.AdditiveByCode(code);
            output.Add(new ParsedIngredient(myPos, parent, head, a?.Name ?? entry?.CanonicalName, percent, true, code, entry?.Flags ?? []));
        }
        else
        {
            output.Add(new ParsedIngredient(myPos, parent, head, entry?.CanonicalName, percent, false, null, entry?.Flags ?? []));
        }
        foreach (var sub in subLists) ParseList(sub, myPos, tax, output, ref position);
    }

    private static void AddAdditive(string code, string raw, int parent, IngredientTaxonomy tax, List<ParsedIngredient> output, ref int position)
    {
        var a = tax.AdditiveByCode(code);
        output.Add(new ParsedIngredient(++position, parent, raw.Trim(), a?.Name, null, true, code, []));
    }

    private static bool TryClassTitleWithInlineCode(string head, out string fclass, out string? code)
    {
        fclass = string.Empty;
        code = null;
        var m = PrefixedCodeRx().Match(head);
        if (!m.Success) return false;
        var title = head[..m.Index].Trim();
        if (!IngredientTaxonomy.TryClassTitle(title, out fclass)) return false;
        code = m.Groups[1].Value + m.Groups[2].Value.ToLowerInvariant();
        return true;
    }

    /// <summary>"INS 322(i)" -> "322", "E150d" -> "150d", "500(ii)" -> "500".</summary>
    public static string? ExtractCode(string text)
    {
        var m = PrefixedCodeRx().Match(text);
        if (m.Success) return m.Groups[1].Value + m.Groups[2].Value.ToLowerInvariant();
        m = BareCodeRx().Match(text);
        return m.Success ? m.Groups[1].Value + m.Groups[2].Value.ToLowerInvariant() : null;
    }

    private static (string Head, List<string> Groups) SplitHead(string token)
    {
        var groups = new List<string>();
        var head = new StringBuilder();
        var depth = 0;
        var current = new StringBuilder();
        foreach (var ch in token)
        {
            if (ch == '(')
            {
                if (depth > 0) current.Append(ch);
                depth++;
            }
            else if (ch == ')')
            {
                depth = Math.Max(0, depth - 1);
                if (depth == 0) { groups.Add(current.ToString().Trim()); current.Clear(); }
                else current.Append(ch);
            }
            else if (depth == 0) head.Append(ch);
            else current.Append(ch);
        }
        if (current.Length > 0) groups.Add(current.ToString().Trim());
        return (Regex.Replace(head.ToString(), @"\s+", " ").Trim(), groups.Where(g => g.Length > 0).ToList());
    }

    private static IEnumerable<string> SplitTopLevel(string text)
    {
        var depth = 0;
        var sb = new StringBuilder();
        foreach (var ch in text)
        {
            if (ch == '(') depth++;
            if (ch == ')') depth = Math.Max(0, depth - 1);
            if ((ch == ',' || ch == ';') && depth == 0)
            {
                if (sb.ToString().Trim().Length > 0) yield return sb.ToString().Trim();
                sb.Clear();
                continue;
            }
            sb.Append(ch);
        }
        if (sb.ToString().Trim().Length > 0) yield return sb.ToString().Trim();
    }
}

/// <summary>Deterministic allergen derivation from statements and ingredients (doc 02 7.2). LLMs never decide allergens.</summary>
public static partial class AllergenDeriver
{
    private static readonly (string Keyword, string[] Codes)[] Keywords =
    [
        ("tree nuts", ["TREE_NUTS"]), ("tree nut", ["TREE_NUTS"]), ("peanuts", ["PEANUTS"]), ("peanut", ["PEANUTS"]),
        ("groundnut", ["PEANUTS"]), ("groundnuts", ["PEANUTS"]), ("nuts", ["TREE_NUTS"]), ("nut", ["TREE_NUTS"]), ("almond", ["TREE_NUTS"]),
        ("almonds", ["TREE_NUTS"]), ("cashew", ["TREE_NUTS"]), ("cashews", ["TREE_NUTS"]), ("walnuts", ["TREE_NUTS"]), ("pistachios", ["TREE_NUTS"]), ("wheat", ["WHEAT", "GLUTEN_CEREALS"]), ("gluten", ["GLUTEN_CEREALS"]),
        ("barley", ["GLUTEN_CEREALS"]), ("rye", ["GLUTEN_CEREALS"]), ("oats", ["GLUTEN_CEREALS"]),
        ("milk", ["MILK"]), ("dairy", ["MILK"]), ("lactose", ["MILK"]), ("egg", ["EGGS"]), ("eggs", ["EGGS"]),
        ("soya", ["SOY"]), ("soy", ["SOY"]), ("soybean", ["SOY"]), ("sesame", ["SESAME"]), ("fish", ["FISH"]),
        ("crustacean", ["CRUSTACEANS"]), ("crustaceans", ["CRUSTACEANS"]), ("shellfish", ["CRUSTACEANS"]),
        ("shrimp", ["CRUSTACEANS"]), ("prawn", ["CRUSTACEANS"]), ("mollusc", ["MOLLUSCS"]), ("molluscs", ["MOLLUSCS"]),
        ("mustard", ["MUSTARD"]), ("celery", ["CELERY"]), ("lupin", ["LUPIN"]), ("sulphite", ["SULPHITES"]),
        ("sulphites", ["SULPHITES"]), ("sulfite", ["SULPHITES"]), ("sulfites", ["SULPHITES"]),
        ("sulphur dioxide", ["SULPHITES"]),
    ];

    [GeneratedRegex(@"may\s+contain(?:\s+traces\s+of)?\s*:?\s*(?<list>[^.;]+)", RegexOptions.IgnoreCase)]
    private static partial Regex MayContainRx();

    [GeneratedRegex(@"(?:processed|manufactured|made|packed|produced)\s+(?:in|on)\s+(?:a\s+)?(?:facility|plant|equipment|line|premises)s?\s+(?:that|which)\s+(?:also\s+)?(?:processes|handles|uses|packs)\s+(?<list>[^.;]+)", RegexOptions.IgnoreCase)]
    private static partial Regex FacilityRx();

    [GeneratedRegex(@"(?<!may\s)contains?\s*:?\s*(?<list>[^.;]+)", RegexOptions.IgnoreCase)]
    private static partial Regex ContainsRx();

    public static Dictionary<string, AllergenPresence> FromStatement(string? statement)
    {
        var result = new Dictionary<string, AllergenPresence>();
        if (string.IsNullOrWhiteSpace(statement)) return result;
        var s = statement.Replace('\n', ' ');
        foreach (Match m in MayContainRx().Matches(s)) Add(result, m.Groups["list"].Value, AllergenPresence.MayContain);
        foreach (Match m in FacilityRx().Matches(s)) Add(result, m.Groups["list"].Value, AllergenPresence.MayContain);
        var withoutMay = MayContainRx().Replace(s, " ");
        withoutMay = FacilityRx().Replace(withoutMay, " ");
        foreach (Match m in ContainsRx().Matches(withoutMay)) Add(result, m.Groups["list"].Value, AllergenPresence.Contains);
        return result;
    }

    /// <summary>Conservative union: CONTAINS from statement or ingredients wins over MAY_CONTAIN.</summary>
    public static Dictionary<string, AllergenPresence> Reconcile(
        IReadOnlyDictionary<string, AllergenPresence> fromStatement, IEnumerable<string> fromIngredients, out IReadOnlyList<string> undeclared)
    {
        var result = new Dictionary<string, AllergenPresence>(fromStatement);
        var missing = new List<string>();
        foreach (var code in fromIngredients.Distinct())
        {
            if (!fromStatement.TryGetValue(code, out var p) || p != AllergenPresence.Contains) missing.Add(code);
            result[code] = AllergenPresence.Contains;
        }
        undeclared = missing;
        return result;
    }

    private static void Add(Dictionary<string, AllergenPresence> result, string list, AllergenPresence presence)
    {
        var lower = " " + list.ToLowerInvariant() + " ";
        foreach (var (keyword, codes) in Keywords)
        {
            if (!Regex.IsMatch(lower, $@"[^a-z]{Regex.Escape(keyword)}[^a-z]")) continue;
            // "nuts" inside "peanuts" or "tree nuts" must not double count.
            if (keyword is "nuts" or "nut" && (lower.Contains("peanut") && !Regex.IsMatch(lower, @"(^|[^a-z])(tree\s)?nuts?([^a-z]|$)"))) continue;
            foreach (var code in codes)
            {
                if (result.TryGetValue(code, out var existing) && existing == AllergenPresence.Contains) continue;
                result[code] = presence;
            }
        }
    }
}

/// <summary>Diet classification from regulatory marks and ingredients; Jain-friendly is inferred only (doc 02 7.2).</summary>
public static class DietClassifier
{
    public sealed record Result(string DietClass, bool JainFriendlyInferred, bool VegMarkContradicted);

    public static Result Classify(IReadOnlyCollection<string> marks, IReadOnlySet<string> flags, IReadOnlyDictionary<string, AllergenPresence> allergens)
    {
        var meat = flags.Contains("MEAT") || flags.Contains("FISH_MEAT") || flags.Contains("GELATIN") ||
                   allergens.ContainsKey("FISH") && allergens["FISH"] == AllergenPresence.Contains ||
                   allergens.ContainsKey("CRUSTACEANS") && allergens["CRUSTACEANS"] == AllergenPresence.Contains;
        var egg = flags.Contains("EGG") || allergens.TryGetValue("EGGS", out var e) && e == AllergenPresence.Contains;
        var animalOther = flags.Contains("DAIRY") || flags.Contains("HONEY") ||
                          allergens.TryGetValue("MILK", out var m) && m == AllergenPresence.Contains;
        var vegMark = marks.Contains("IN_FSSAI_VEG") || marks.Contains("IN_FSSAI_VEGAN");

        string diet;
        if (marks.Contains("IN_FSSAI_NON_VEG") || meat) diet = "NON_VEG";
        else if (egg) diet = "EGG";
        // Vegan only from the regulatory/certification mark: an absence of animal ingredients is not proof.
        else if ((marks.Contains("IN_FSSAI_VEGAN") || marks.Contains("VEGAN_CERTIFIED")) && !animalOther) diet = "VEGAN";
        else if (vegMark || flags.Count > 0) diet = "VEG";
        else diet = "UNKNOWN";

        var jain = diet is "VEG" or "VEGAN" && !flags.Contains("ROOT_VEG") && !flags.Contains("ONION_GARLIC") && !flags.Contains("HONEY");
        return new Result(diet, jain, vegMark && (meat || egg));
    }
}

/// <summary>NOVA group from ingredient markers (doc 03 2.2). Returns null (excluded from HSI) when it cannot be determined.</summary>
public static class NovaClassifier
{
    private static readonly HashSet<string> UltraProcessedClasses =
        ["COLOUR", "SWEETENER", "FLAVOUR_ENHANCER", "EMULSIFIER", "THICKENER", "STABILISER", "GLAZING_AGENT", "HUMECTANT"];

    public static (int? Group, decimal Confidence) Classify(IngredientParseResult parsed, IngredientTaxonomy taxonomy)
    {
        var top = parsed.Tokens.Where(t => t.ParentPosition is null && !t.RawText.StartsWith("class:", StringComparison.Ordinal)).ToList();
        if (top.Count == 0 && parsed.Additives.Count == 0) return (null, 0m);
        var confidence = Math.Round(Math.Min(1m, parsed.ResolutionRatio), 3);
        if (parsed.ResolutionRatio < 0.5m) return (null, confidence);

        var upf = parsed.Flags.Contains("UPF_MARKER") || parsed.Flags.Contains("PHO") || parsed.Flags.Contains("NNS") ||
                  parsed.Additives.Any(code => taxonomy.AdditiveByCode(code)?.FunctionalClasses.Any(UltraProcessedClasses.Contains) == true);
        if (upf) return (4, confidence);

        var culinary = top.Count(t => t.Flags.Contains("CULINARY"));
        var processedMarkers = parsed.Flags.Contains("ADDED_SUGAR") || parsed.Flags.Contains("SALT") || parsed.Flags.Contains("OIL") ||
                               parsed.Additives.Count > 0;
        if (top.Count == 1 && culinary == 1) return (2, confidence);
        if (!processedMarkers && top.Count <= 2) return (1, confidence);
        return (3, confidence);
    }
}
