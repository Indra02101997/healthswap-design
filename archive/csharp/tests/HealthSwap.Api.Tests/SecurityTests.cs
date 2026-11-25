using System.Net;
using System.Text.Json;
using static HealthSwap.Api.Tests.Flows;

namespace HealthSwap.Api.Tests;

/// <summary>Authentication, authorization, tenant isolation (RLS), SDK session binding and request validation.</summary>
public sealed class SecurityTests(ApiFixture fx)
{
    [Fact]
    public async Task MissingTokenIsChallengedWithProblemDetails()
    {
        fx.SkipIfUnavailable();
        var res = await fx.Factory.CreateClient().PostAsync("/v1/cart-evaluations", Contract.Body(NoodleCart("c_anon")));
        Assert.Equal(HttpStatusCode.Unauthorized, res.StatusCode);
        Assert.NotEmpty(res.Headers.WwwAuthenticate);
        Assert.Equal("application/problem+json", res.Content.Headers.ContentType?.MediaType);
    }

    [Fact]
    public async Task WrongClientSecretIsRejected()
    {
        fx.SkipIfUnavailable();
        var res = await fx.Factory.CreateClient().PostAsync("/oauth2/token", new FormUrlEncodedContent(new Dictionary<string, string>
        {
            ["grant_type"] = "client_credentials", ["client_id"] = "quickbasket-server", ["client_secret"] = "wrong",
        }));
        Assert.Equal(HttpStatusCode.Unauthorized, res.StatusCode);
        Assert.Equal("invalid_client", JsonDocument.Parse(await res.Content.ReadAsStringAsync()).RootElement.GetProperty("error").GetString());
    }

    [Fact]
    public async Task PartnerTokensCannotUseTheAdminPlane()
    {
        fx.SkipIfUnavailable();
        var partner = await fx.ClientAsync();
        Assert.Equal(HttpStatusCode.Forbidden, (await partner.GetAsync("/admin/partners")).StatusCode);
        Assert.Equal(HttpStatusCode.Forbidden, (await partner.PostAsync("/admin/jobs/month-close", null)).StatusCode);
    }

    [Fact]
    public async Task TenantsCannotSeeEachOthersChargesOrdersOrSuggestions()
    {
        fx.SkipIfUnavailable();
        var quickBasket = await fx.ClientAsync();
        var hometown = await fx.HometownAsync();
        var (_, suggestionId, chargeId) = await RecommendedAddAsync(quickBasket);

        await (await hometown.GetAsync($"/v1/billing/recommendation-charges/{chargeId}")).Expect("getRecommendationCharge", 404);
        var list = await (await hometown.GetAsync("/v1/billing/recommendation-charges?limit=200")).Expect("listRecommendationCharges", 200);
        Assert.DoesNotContain(list.GetProperty("items").EnumerateArray(), i => i.GetProperty("chargeId").GetString() == chargeId);
        var ack = await EventsAsync(hometown, AddToCart(suggestionId));
        Assert.Equal("HS-EVENT-UNKNOWN-SUGGESTION", ack.GetProperty("rejected")[0].GetProperty("code").GetString());
        await (await hometown.GetAsync("/v1/orders/does-not-exist")).Expect("getOrder", 404);
    }

    [Fact]
    public async Task SdkSessionsAreBoundToTheirCartAndScopes()
    {
        fx.SkipIfUnavailable();
        var server = await fx.ClientAsync();
        var cart = NewRef("c_sdk_");
        var session = await (await server.PostWithKeyAsync("/v1/sdk/sessions", new { cartRef = cart, market = "IN", storeId = Store }, null)).Expect("createSdkSession", 201);
        var sdk = fx.Factory.CreateClient().WithBearer(session.GetProperty("token").GetString()!);

        var evaluation = await EvaluateAsync(sdk, NoodleCart(cart));
        await (await sdk.PostAsync("/v1/cart-evaluations", Contract.Body(NoodleCart(NewRef("c_other_"))))).Expect("evaluateCart", 403);
        Assert.Equal(HttpStatusCode.Forbidden, (await sdk.GetAsync("/v1/billing/statements")).StatusCode);
        await (await sdk.PostWithKeyAsync("/v1/sdk/sessions", new { cartRef = cart, market = "IN" }, null)).Expect("createSdkSession", 403);

        // The browser can report RA-1 events for its own cart.
        var sid = Suggestion(evaluation, "1")!.Value.GetProperty("suggestionId").GetString()!;
        var ack = await EventsAsync(sdk, Impression(sid), AddToCart(sid));
        Assert.Equal("PENDING", ack.GetProperty("charges")[0].GetProperty("state").GetString());

        // ...but not for another cart's suggestion.
        var (_, otherSid, _) = await RecommendedAddAsync(server);
        var foreign = await EventsAsync(sdk, AddToCart(otherSid));
        Assert.Equal("HS-AUTH-SESSION-SCOPE", foreign.GetProperty("rejected")[0].GetProperty("code").GetString());
    }

    [Fact]
    public async Task InvalidRequestsReturnFieldErrors()
    {
        fx.SkipIfUnavailable();
        var partner = await fx.ClientAsync();
        var res = await partner.PostAsync("/v1/cart-evaluations", Contract.Body(new { cartRef = "c_bad", market = "IN", currency = "INR" }));
        var problem = await res.Expect("evaluateCart", 400);
        Assert.Equal("HS-REQ-INVALID", problem.GetProperty("code").GetString());
        Assert.NotEqual(0, problem.GetProperty("errors").GetArrayLength());

        var wrongType = new StringContent("{}", System.Text.Encoding.UTF8, "text/plain");
        Assert.Equal(HttpStatusCode.UnsupportedMediaType, (await partner.PostAsync("/v1/cart-evaluations", wrongType)).StatusCode);

        var noKey = await partner.PostWithKeyAsync("/v1/orders", new
        {
            orderId = NewRef("ORD-NOKEY-"), cartRef = "c_x", storeId = Store, market = "IN", currency = "INR", placedAt = Contract.Now(), fulfillmentType = "QUICK_COMMERCE",
            lines = new object[] { new { lineId = "1", partnerSku = Noodles, quantity = 1, unitPrice = Inr("56.00"), netAmount = Inr("56.00") } },
        }, null);
        await noKey.Expect("createOrder", 400);
    }
}
