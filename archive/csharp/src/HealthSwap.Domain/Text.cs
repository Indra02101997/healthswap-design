using System.Text;

namespace HealthSwap.Domain;

public static class TextSimilarity
{
    public static HashSet<string> Trigrams(string text)
    {
        var t = "  " + Knowledge.IngredientTaxonomy.Normalize(text) + " ";
        var set = new HashSet<string>(StringComparer.Ordinal);
        for (var i = 0; i + 3 <= t.Length; i++) set.Add(t.Substring(i, 3));
        return set;
    }

    public static float TrigramJaccard(string a, string b) => Jaccard(Trigrams(a), Trigrams(b));

    public static float Jaccard<T>(IEnumerable<T> a, IEnumerable<T> b)
    {
        var sa = a as ISet<T> ?? new HashSet<T>(a);
        var sb = b as ISet<T> ?? new HashSet<T>(b);
        if (sa.Count == 0 && sb.Count == 0) return 0f;
        var inter = sa.Count(sb.Contains);
        return (float)inter / (sa.Count + sb.Count - inter);
    }

    public static float Cosine(float[] a, float[] b)
    {
        if (a.Length != b.Length || a.Length == 0) return 0f;
        double dot = 0, na = 0, nb = 0;
        for (var i = 0; i < a.Length; i++) { dot += a[i] * b[i]; na += a[i] * a[i]; nb += b[i] * b[i]; }
        return na == 0 || nb == 0 ? 0f : (float)(dot / Math.Sqrt(na * nb));
    }
}

/// <summary>
/// Deterministic local stand-in for the multilingual embedding model (task T7): signed feature hashing of word unigrams
/// and character trigrams into 1024 dimensions (the pgvector column size), L2-normalized. Production configures a real
/// embedding deployment through the LLM gateway; the vectors are versioned by model_ref so both can coexist.
/// </summary>
public static class HashingEmbedder
{
    public const int Dimensions = 1024;
    public const string ModelRef = "local/hashing-embedder@1";

    public static float[] Embed(string text)
    {
        var v = new float[Dimensions];
        var normalized = Knowledge.IngredientTaxonomy.Normalize(text);
        foreach (var word in normalized.Split(' ', StringSplitOptions.RemoveEmptyEntries))
            Add(v, "w:" + word, 2f);
        foreach (var tri in TextSimilarity.Trigrams(normalized))
            Add(v, "c:" + tri, 1f);
        var norm = Math.Sqrt(v.Sum(x => (double)x * x));
        if (norm > 0) for (var i = 0; i < v.Length; i++) v[i] = (float)(v[i] / norm);
        return v;
    }

    private static void Add(float[] v, string feature, float weight)
    {
        var h = Fnv1a(feature);
        var idx = (int)(h % Dimensions);
        var sign = (h >> 31 & 1) == 0 ? 1f : -1f;
        v[idx] += sign * weight;
    }

    private static uint Fnv1a(string s)
    {
        var hash = 2166136261u;
        foreach (var b in Encoding.UTF8.GetBytes(s)) { hash ^= b; hash *= 16777619u; }
        return hash;
    }
}
