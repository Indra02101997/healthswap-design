using System.Globalization;
using System.Reflection;
using System.Text.Json;
using System.Text.Json.Nodes;
using HealthSwap.Contracts;
using HealthSwap.Platform;
using Json.Schema;
using YamlDotNet.Core;
using YamlDotNet.RepresentationModel;

namespace HealthSwap.Api;

/// <summary>
/// The OpenAPI 3.1 contract (api/openapi.yaml, embedded) compiled into JSON Schema 2020-12 validators per operation. Requests
/// are validated before binding; the integration tests validate every response against the same document.
/// </summary>
public sealed class OpenApiContract
{
    private readonly Dictionary<string, JsonSchema> _requests = new(StringComparer.Ordinal);
    private readonly Dictionary<(string Operation, string Status), JsonSchema> _responses = new();
    private static readonly EvaluationOptions Evaluation = new() { OutputFormat = OutputFormat.List, RequireFormatValidation = true, IncludeApplicatorErrors = false };

    public JsonObject Document { get; }
    public string Yaml { get; }
    public IReadOnlyCollection<string> Operations => _operations;
    private readonly List<string> _operations = [];

    private OpenApiContract(string yaml)
    {
        Yaml = yaml;
        var stream = new YamlStream();
        stream.Load(new StringReader(yaml));
        Document = (JsonObject)ToJson(stream.Documents[0].RootNode)!;
        var defs = Rewrite(Document["components"]?["schemas"]?.DeepClone() ?? new JsonObject());
        var responses = Document["components"]?["responses"]?.AsObject();
        foreach (var (path, pathItem) in Document["paths"]!.AsObject())
            foreach (var (method, op) in pathItem!.AsObject())
            {
                if (method is not ("get" or "post" or "put" or "patch" or "delete") || op?["operationId"]?.GetValue<string>() is not { } opId) continue;
                _operations.Add(opId);
                if (op["requestBody"]?["content"]?["application/json"]?["schema"] is { } body) _requests[opId] = Build($"req/{opId}", body, defs);
                foreach (var (status, response) in op["responses"]?.AsObject() ?? [])
                {
                    var r = response?["$ref"]?.GetValue<string>() is { } refName ? responses?[refName.Split('/').Last()] : response;
                    var schema = r?["content"]?["application/json"]?["schema"] ?? r?["content"]?["application/problem+json"]?["schema"];
                    if (schema is not null) _responses[(opId, status)] = Build($"res/{opId}/{status}", schema, defs);
                }
                _ = path;
            }
    }

    private static readonly Lazy<OpenApiContract> Embedded = new(() =>
    {
        using var s = Assembly.GetExecutingAssembly().GetManifestResourceStream("openapi.yaml") ?? throw new InvalidOperationException("openapi.yaml is not embedded");
        using var reader = new StreamReader(s);
        return new OpenApiContract(reader.ReadToEnd());
    });

    public static OpenApiContract Load() => Embedded.Value;

    public JsonSchema? RequestSchema(string operationId) => _requests.GetValueOrDefault(operationId);

    public JsonSchema? ResponseSchema(string operationId, int status) =>
        _responses.GetValueOrDefault((operationId, status.ToString(CultureInfo.InvariantCulture))) ?? _responses.GetValueOrDefault((operationId, "default"));

    public static List<FieldError> Validate(JsonSchema schema, JsonElement instance)
    {
        var result = schema.Evaluate(instance, Evaluation);
        if (result.IsValid) return [];
        var errors = new List<FieldError>();
        void Collect(EvaluationResults r)
        {
            if (r.Errors is { Count: > 0 } e)
                foreach (var (keyword, message) in e)
                    errors.Add(new FieldError(r.InstanceLocation.ToString() is { Length: > 0 } p ? p : "/", message, keyword));
            foreach (var d in r.Details ?? []) Collect(d);
        }
        Collect(result);
        // allOf/anyOf wrappers repeat leaf errors; keep each (field, message) once and cap the list.
        return errors.DistinctBy(x => (x.Field, x.Message)).Take(50).ToList();
    }

    private static JsonSchema Build(string id, JsonNode schema, JsonNode defs)
    {
        var root = new JsonObject
        {
            ["$schema"] = "https://json-schema.org/draft/2020-12/schema",
            ["$id"] = $"https://healthswap.example/contract/{id}.json",
            ["$defs"] = defs.DeepClone(),
            ["allOf"] = new JsonArray(Rewrite(schema.DeepClone())),
        };
        using var doc = JsonDocument.Parse(root.ToJsonString());
        return JsonSchema.Build(doc.RootElement.Clone(), new BuildOptions(), new Uri($"https://healthswap.example/contract/{id}.json"));
    }

    /// <summary>OpenAPI component references become local JSON Schema $defs references.</summary>
    private static JsonNode Rewrite(JsonNode node)
    {
        switch (node)
        {
            case JsonObject o:
                foreach (var key in o.Select(kv => kv.Key).ToList())
                {
                    if (key == "$ref" && o[key] is JsonValue v && v.TryGetValue<string>(out var r) && r.StartsWith("#/components/schemas/", StringComparison.Ordinal))
                        o[key] = "#/$defs/" + r["#/components/schemas/".Length..];
                    else if (o[key] is { } child) Rewrite(child);
                }
                break;
            case JsonArray a:
                foreach (var child in a) if (child is not null) Rewrite(child);
                break;
        }
        return node;
    }

    private static JsonNode? ToJson(YamlNode node) => node switch
    {
        YamlMappingNode m => new JsonObject(m.Children.Select(kv => KeyValuePair.Create(((YamlScalarNode)kv.Key).Value ?? "", ToJson(kv.Value)))),
        YamlSequenceNode s => new JsonArray(s.Children.Select(ToJson).ToArray()),
        YamlScalarNode sc => Scalar(sc),
        _ => null,
    };

    private static JsonNode? Scalar(YamlScalarNode s)
    {
        var v = s.Value ?? "";
        if (s.Style is ScalarStyle.SingleQuoted or ScalarStyle.DoubleQuoted or ScalarStyle.Literal or ScalarStyle.Folded) return JsonValue.Create(v);
        if (v is "null" or "~" or "") return null;
        if (v is "true") return JsonValue.Create(true);
        if (v is "false") return JsonValue.Create(false);
        if (long.TryParse(v, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out var l)) return JsonValue.Create(l);
        if (decimal.TryParse(v, NumberStyles.AllowLeadingSign | NumberStyles.AllowDecimalPoint, CultureInfo.InvariantCulture, out var d)) return JsonValue.Create(d);
        return JsonValue.Create(v);
    }
}

/// <summary>Validates JSON request bodies against the OpenAPI contract before model binding (fails with RFC 9457 problems).</summary>
public sealed class RequestValidationMiddleware(RequestDelegate next, OpenApiContract contract)
{
    public async Task InvokeAsync(HttpContext http)
    {
        var opId = http.GetEndpoint()?.Metadata.GetMetadata<IEndpointNameMetadata>()?.EndpointName;
        if (opId is not null && http.Request.Method is "POST" or "PUT" or "PATCH" && contract.RequestSchema(opId) is { } schema)
        {
            var contentType = http.Request.ContentType ?? "";
            if (!contentType.StartsWith("application/json", StringComparison.OrdinalIgnoreCase))
                throw new HsException(415, "HS-UNSUPPORTED-MEDIA-TYPE", "Unsupported media type", "Send application/json");
            http.Request.EnableBuffering();
            using var ms = new MemoryStream();
            await http.Request.Body.CopyToAsync(ms, http.RequestAborted);
            if (ms.Length > 10 * 1024 * 1024) throw HsException.TooLarge("Request bodies are limited to 10 MB");
            var bytes = ms.ToArray();
            http.Request.Body.Position = 0;
            http.Items["raw-body"] = bytes;
            JsonDocument doc;
            try { doc = JsonDocument.Parse(bytes); }
            catch (JsonException ex) { throw HsException.Invalid("Malformed JSON: " + ex.Message); }
            using (doc)
            {
                var errors = OpenApiContract.Validate(schema, doc.RootElement);
                if (errors.Count > 0) throw HsException.Invalid("The request body does not match the API contract", errors);
            }
        }
        await next(http);
    }
}
