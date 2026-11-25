using System.Net.Http.Headers;
using System.Text.Json;

namespace HealthSwap.Api.Tests;

/// <summary>Reusable partner flows (QuickBasket, market IN, store BLR-KOR-012) built on the synthetic demo catalog.</summary>
internal static class Flows
{
    public const string Store = "BLR-KOR-012";
    public const string Noodles = "SKU-NDL-280";
    public const string MilletNoodles = "SKU-MLT-256";

    public static object Inr(string amount) => new { amount, currency = "INR" };

    public static string NewRef(string prefix) => prefix + Guid.NewGuid().ToString("N")[..10];

    public static object NoodleCart(string cartRef, string? userRef = null, object? context = null) => new
    {
        cartRef, userRef, market = "IN", storeId = Store, currency = "INR", locale = "en-IN", context,
        lines = new object[]
        {
            new { lineId = "1", partnerSku = Noodles, gtin = "8901234567890", quantity = 2, unitPrice = Inr("56.00") },
            new { lineId = "2", partnerSku = "SKU-COLA-750", quantity = 1, unitPrice = Inr("40.00") },
        },
    };

    public static async Task<JsonElement> EvaluateAsync(HttpClient client, object request) =>
        await (await client.PostAsync("/v1/cart-evaluations", Contract.Body(request))).Expect("evaluateCart", 200);

    public static JsonElement? Suggestion(JsonElement evaluation, string lineId) =>
        evaluation.GetProperty("suggestions").EnumerateArray().Where(s => s.GetProperty("lineId").GetString() == lineId).Select(s => (JsonElement?)s).FirstOrDefault();

    public static JsonElement Alternative(JsonElement suggestion, string partnerSku) =>
        suggestion.GetProperty("alternatives").EnumerateArray().First(a => a.GetProperty("product").GetProperty("partnerSku").GetString() == partnerSku);

    public static async Task<JsonElement> EventsAsync(HttpClient client, params object[] events) =>
        await (await client.PostAsync("/v1/events", Contract.Body(new { events }))).Expect("reportSuggestionEvents", 202);

    public static object Impression(string suggestionId, int secondsAgo = 3) =>
        new { eventId = Guid.NewGuid(), type = "IMPRESSION", suggestionId, occurredAt = Contract.Now(-secondsAgo) };

    public static object AddToCart(string suggestionId, string sku = MilletNoodles, int secondsAgo = 1, Guid? eventId = null, int quantity = 1) =>
        new { eventId = eventId ?? Guid.NewGuid(), type = "ADD_TO_CART", suggestionId, alternativePartnerSku = sku, quantity, occurredAt = Contract.Now(-secondsAgo) };

    /// <summary>Evaluates a fresh noodle cart, reports an impression and one add of the recommended product; returns the PENDING charge.</summary>
    public static async Task<(string CartRef, string SuggestionId, string ChargeId)> RecommendedAddAsync(HttpClient client)
    {
        var cartRef = NewRef("c_it_");
        var evaluation = await EvaluateAsync(client, NoodleCart(cartRef));
        var suggestionId = Suggestion(evaluation, "1")!.Value.GetProperty("suggestionId").GetString()!;
        var ack = await EventsAsync(client, Impression(suggestionId), AddToCart(suggestionId));
        return (cartRef, suggestionId, ack.GetProperty("charges")[0].GetProperty("chargeId").GetString()!);
    }

    public static HttpClient WithBearer(this HttpClient client, string token)
    {
        client.DefaultRequestHeaders.Authorization = new AuthenticationHeaderValue("Bearer", token);
        return client;
    }

    public static async Task<HttpResponseMessage> PostWithKeyAsync(this HttpClient client, string path, object body, string? key)
    {
        var req = new HttpRequestMessage(HttpMethod.Post, path) { Content = Contract.Body(body) };
        if (key is not null) req.Headers.Add("Idempotency-Key", key);
        return await client.SendAsync(req);
    }

    public static async Task<HttpResponseMessage> PatchAsync(this HttpClient client, string path, object body) =>
        await client.SendAsync(new HttpRequestMessage(HttpMethod.Patch, path) { Content = Contract.Body(body) });

    public static async Task<string> ServerPeriodAsync(HttpClient admin)
    {
        var clock = JsonDocument.Parse(await admin.GetStringAsync("/admin/clock")).RootElement;
        return clock.GetProperty("utcNow").GetDateTime().ToUniversalTime().ToString("yyyy-MM");
    }

    public static async Task SetClockAsync(HttpClient admin, double offsetSeconds) =>
        (await admin.PostAsync("/admin/clock", Contract.Body(new { offsetSeconds }))).EnsureSuccessStatusCode();

    public static async Task<JsonElement> JobAsync(HttpClient admin, string job, string query = "")
    {
        var res = await admin.PostAsync($"/admin/jobs/{job}{query}", null);
        var body = await res.Content.ReadAsStringAsync();
        Assert.True(res.IsSuccessStatusCode, $"job {job}: {(int)res.StatusCode} {body}");
        return JsonDocument.Parse(body).RootElement.GetProperty("result").Clone();
    }
}
