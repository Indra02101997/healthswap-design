using System.Text.Json;
using HealthSwap.Contracts;
using HealthSwap.Domain.Knowledge;
using HealthSwap.Domain.Recommendation;
using Microsoft.Extensions.Logging;
using Microsoft.ML.OnnxRuntime;
using Microsoft.ML.OnnxRuntime.Tensors;

namespace HealthSwap.Platform.Ml;

/// <summary>Metadata written next to every ONNX artifact by ml/ (feature order is the contract between training and serving).</summary>
public sealed record ModelMetadata
{
    public required string Name { get; init; }
    public required string Version { get; init; }
    public required string[] FeatureNames { get; init; }
    public string InputName { get; init; } = "input";
    public string OutputName { get; init; } = "probabilities";
    public int PositiveClassIndex { get; init; } = 1;
    public Dictionary<string, double> Metrics { get; init; } = [];
    public string? TrainedAt { get; init; }
}

/// <summary>Binary classifier evaluated in-process with ONNX Runtime (HLD: "Ranker - in-process model").</summary>
public sealed class OnnxBinaryModel : IDisposable
{
    private readonly InferenceSession _session;
    public ModelMetadata Metadata { get; }

    private OnnxBinaryModel(InferenceSession session, ModelMetadata metadata)
    {
        _session = session;
        Metadata = metadata;
    }

    public static OnnxBinaryModel? TryLoad(string directory, string name, string[] expectedFeatures, ILogger log)
    {
        var onnx = Path.Combine(directory, name + ".onnx");
        var meta = Path.Combine(directory, name + ".meta.json");
        if (!File.Exists(onnx) || !File.Exists(meta)) return null;
        var metadata = JsonSerializer.Deserialize<ModelMetadata>(File.ReadAllText(meta), HsJson.Options)!;
        if (!metadata.FeatureNames.SequenceEqual(expectedFeatures))
        {
            log.LogError("Refusing to load {Model} {Version}: feature order differs from the serving code (train/serve skew)", name, metadata.Version);
            return null;
        }
        var options = new SessionOptions { IntraOpNumThreads = 1, InterOpNumThreads = 1, GraphOptimizationLevel = GraphOptimizationLevel.ORT_ENABLE_ALL };
        return new OnnxBinaryModel(new InferenceSession(onnx, options), metadata);
    }

    public float[] Predict(IReadOnlyList<float[]> rows)
    {
        if (rows.Count == 0) return [];
        var width = Metadata.FeatureNames.Length;
        var tensor = new DenseTensor<float>([rows.Count, width]);
        for (var r = 0; r < rows.Count; r++)
            for (var c = 0; c < width; c++)
                tensor[r, c] = rows[r][c];
        using var results = _session.Run([NamedOnnxValue.CreateFromTensor(Metadata.InputName, tensor)]);
        var output = results.First(x => x.Name == Metadata.OutputName).AsTensor<float>();
        var probs = new float[rows.Count];
        for (var r = 0; r < rows.Count; r++) probs[r] = output[r, Metadata.PositiveClassIndex];
        return probs;
    }

    public void Dispose() => _session.Dispose();
}

public sealed class ModelOptions
{
    public string Directory { get; set; } = Path.Combine(AppContext.BaseDirectory, "models");
}

/// <summary>Trained models with transparent fallbacks: absent or skewed models degrade to deterministic heuristics (L2).</summary>
public sealed class ModelRegistry : IDisposable
{
    private readonly ModelOptions _options;
    private readonly ILogger<ModelRegistry> _log;

    public ModelRegistry(ModelOptions options, ILogger<ModelRegistry> log)
    {
        _options = options;
        _log = log;
        Reload();
    }

    public OnnxBinaryModel? Ranker { get; private set; }
    public OnnxBinaryModel? Substitutability { get; private set; }
    public OnnxBinaryModel? EntityResolution { get; private set; }

    public string RankerVersion => Ranker?.Metadata.Version ?? "heuristic-L2";
    public string SubstitutabilityVersion => Substitutability?.Metadata.Version ?? "heuristic";
    public string EntityResolutionVersion => EntityResolution?.Metadata.Version ?? "heuristic";

    public void Reload()
    {
        Dispose();
        try
        {
            Ranker = OnnxBinaryModel.TryLoad(_options.Directory, "ranker", RankerFeatures.FeatureNames, _log);
            Substitutability = OnnxBinaryModel.TryLoad(_options.Directory, "substitutability", Domain.Recommendation.Substitutability.FeatureNames, _log);
            EntityResolution = OnnxBinaryModel.TryLoad(_options.Directory, "entity_resolution", Domain.Knowledge.EntityResolution.FeatureNames, _log);
        }
        catch (OnnxRuntimeException ex)
        {
            _log.LogError(ex, "Failed to load ONNX models from {Dir}; using heuristics", _options.Directory);
        }
        _log.LogInformation("Models: ranker={Ranker} substitutability={Sub} er={Er}", RankerVersion, SubstitutabilityVersion, EntityResolutionVersion);
    }

    public decimal[] ScoreSubstitutability(IReadOnlyList<float[]> features)
    {
        if (Substitutability is { } m) return m.Predict(features).Select(p => (decimal)Math.Round(p, 3)).ToArray();
        return features.Select(Domain.Recommendation.Substitutability.Heuristic).ToArray();
    }

    public decimal ScoreEntityMatch(float[] features)
    {
        if (features[7] > 0) return 0.05m; // different GTINs never merge, whatever the model says
        return EntityResolution is { } m ? (decimal)Math.Round(m.Predict([features])[0], 4) : Domain.Knowledge.EntityResolution.HeuristicScore(features);
    }

    public decimal[]? PredictAcceptance(IReadOnlyList<float[]> features) =>
        Ranker is { } m ? m.Predict(features).Select(p => (decimal)Math.Round(p, 4)).ToArray() : null;

    public void Dispose()
    {
        Ranker?.Dispose();
        Substitutability?.Dispose();
        EntityResolution?.Dispose();
        Ranker = Substitutability = EntityResolution = null;
    }
}
