using System.Text.Json;
using static HealthSwap.Api.Tests.Flows;

namespace HealthSwap.Api.Tests;

/// <summary>
/// End-to-end RA-1 (INR 1.00 per add of a HealthSwap-labeled recommendation) and FP-1 (fair premium fee) through settlement,
/// month close, dispute and the signed statement webhook. Mirrors docs/08-manual-test-guide.md.
/// </summary>
public sealed class RecommendationFeeFlowTests(ApiFixture fx)
{
    [Fact]
    public async Task LabeledRecommendationAddsAreChargedSettledBilledAndDisputable()
    {
        fx.SkipIfUnavailable();
        var partner = await fx.ClientAsync();
        var admin = await fx.AdminAsync();
        await SetClockAsync(admin, 0);
        try
        {
            var cart = NewRef("c_flow_");
            var user = NewRef("u_flow_");

            // 1. Evaluation: the alternative carries the label; the disclosure explains the add fee.
            var evaluation = await EvaluateAsync(partner, NoodleCart(cart, user));
            var noodles = Suggestion(evaluation, "1") ?? throw new Xunit.Sdk.XunitException("no suggestion for the noodles line");
            var alt = Alternative(noodles, MilletNoodles);
            var label = alt.GetProperty("recommendation");
            Assert.Equal("HEALTHSWAP_RECOMMENDED", label.GetProperty("code").GetString());
            Assert.True(label.GetProperty("chargeable").GetBoolean());
            Assert.Equal("RA-1", label.GetProperty("feeRule").GetString());
            Assert.Equal("89.00", alt.GetProperty("unitPrice").GetProperty("amount").GetString());
            Assert.Equal("33.00", alt.GetProperty("unitPriceDelta").GetProperty("amount").GetString());
            Assert.Contains("add a recommended item", evaluation.GetProperty("disclosures").GetProperty("feeDisclosure").GetString());
            Assert.NotNull(Suggestion(evaluation, "2")); // approved map: soft drink -> flavoured water
            var sid = noodles.GetProperty("suggestionId").GetString()!;

            // 2. Impression + ACCEPT -> one PENDING INR 1.00 charge; a replayed eventId returns the same charge.
            var acceptId = Guid.NewGuid();
            var accept = new { eventId = acceptId, type = "ACCEPT", suggestionId = sid, alternativePartnerSku = MilletNoodles, quantity = 2, occurredAt = Contract.Now(-3) };
            var ack = await EventsAsync(partner, Impression(sid, 5), accept);
            Assert.Equal(2, ack.GetProperty("accepted").GetInt32());
            var first = ack.GetProperty("charges")[0];
            Assert.Equal("PENDING", first.GetProperty("state").GetString());
            Assert.Equal("1.00", first.GetProperty("amount").GetProperty("amount").GetString());
            Assert.Equal("INR", first.GetProperty("amount").GetProperty("currency").GetString());
            var replay = await EventsAsync(partner, accept);
            Assert.Equal(1, replay.GetProperty("duplicates").GetInt32());
            Assert.Equal(first.GetProperty("chargeId").GetString(), replay.GetProperty("charges")[0].GetProperty("chargeId").GetString());

            // 3. A second add is another charge; an add undone within the grace period is VOID; a non-recommended SKU is rejected.
            var second = await EventsAsync(partner, AddToCart(sid, secondsAgo: 2));
            Assert.Equal("PENDING", second.GetProperty("charges")[0].GetProperty("state").GetString());
            var t = DateTime.UtcNow;
            var undone = await EventsAsync(partner,
                new { eventId = Guid.NewGuid(), type = "ADD_TO_CART", suggestionId = sid, alternativePartnerSku = MilletNoodles, occurredAt = t.AddSeconds(-1).ToString("o") },
                new { eventId = Guid.NewGuid(), type = "UNDO", suggestionId = sid, alternativePartnerSku = MilletNoodles, occurredAt = t.AddSeconds(1).ToString("o") });
            var voidId = undone.GetProperty("charges")[0].GetProperty("chargeId").GetString();
            var voided = await (await partner.GetAsync($"/v1/billing/recommendation-charges/{voidId}")).Expect("getRecommendationCharge", 200);
            Assert.Equal("VOID", voided.GetProperty("state").GetString());
            Assert.Equal("UNDONE_WITHIN_GRACE", voided.GetProperty("rejectionReason").GetString());
            var wrongSku = await EventsAsync(partner, AddToCart(sid, sku: "SKU-CRM-500", secondsAgo: 0));
            Assert.Equal("HS-EVENT-SKU-NOT-RECOMMENDED", wrongSku.GetProperty("rejected")[0].GetProperty("code").GetString());

            // 4. The cart line now holding the recommended product is labeled.
            var again = await EvaluateAsync(partner, new
            {
                cartRef = cart, userRef = user, market = "IN", storeId = Store, currency = "INR",
                lines = new object[] { new { lineId = "1", partnerSku = MilletNoodles, quantity = 3, unitPrice = Inr("89.00") } },
            });
            var lineLabel = again.GetProperty("lineLabels").EnumerateArray().Single(l => l.GetProperty("partnerSku").GetString() == MilletNoodles);
            Assert.Equal("HEALTHSWAP_RECOMMENDED", lineLabel.GetProperty("label").GetProperty("code").GetString());

            // 5. PDP add without an impression: PENDING now, REJECTED (NO_IMPRESSION) at finalization.
            var noodleId = noodles.GetProperty("original").GetProperty("productId").GetString();
            var pdp = await (await partner.GetAsync($"/v1/products/{noodleId}/alternatives?market=IN&storeId={Store}&cartRef={cart}&placement=PDP")).Expect("listProductAlternatives", 200);
            var pdpSid = pdp.GetProperty("suggestionId").GetString()!;
            var pdpAck = await EventsAsync(partner, AddToCart(pdpSid, pdp.GetProperty("alternatives")[0].GetProperty("product").GetProperty("partnerSku").GetString()!, 0));
            var noImpressionId = pdpAck.GetProperty("charges")[0].GetProperty("chargeId").GetString();

            // 6. FP-1 order (IN-1): INR 89 list, INR 8 coupon -> INR 85 net per unit, fee INR 5.80; idempotent create; delivery; return.
            var orderId = NewRef("ORD-IT-");
            var order = new
            {
                orderId, cartRef = cart, userRef = user, storeId = Store, market = "IN", currency = "INR", placedAt = Contract.Now(), fulfillmentType = "QUICK_COMMERCE",
                orderDiscount = Inr("8.00"),
                lines = new object[]
                {
                    new { lineId = "1", partnerSku = MilletNoodles, quantity = 2, unitPrice = Inr("89.00"), allocatedOrderDiscount = Inr("8.00"), netAmount = Inr("170.00"), suggestionId = sid },
                    new { lineId = "2", partnerSku = "SKU-COLA-750", quantity = 1, unitPrice = Inr("40.00"), netAmount = Inr("40.00") },
                },
            };
            var key = Guid.NewGuid().ToString();
            var created = await (await partner.PostWithKeyAsync("/v1/orders", order, key)).Expect("createOrder", 201);
            var attributed = created.GetProperty("attribution").GetProperty("lines").EnumerateArray().Single(l => l.GetProperty("lineId").GetString() == "1");
            Assert.Equal("PENDING", attributed.GetProperty("state").GetString());
            Assert.Equal("5.80", attributed.GetProperty("fee").GetProperty("amount").GetString());
            var replayed = await partner.PostWithKeyAsync("/v1/orders", order, key);
            await replayed.Expect("createOrder", 201);
            Assert.Equal("true", replayed.Headers.GetValues("Idempotent-Replayed").Single());
            await (await partner.PostWithKeyAsync("/v1/orders", order, Guid.NewGuid().ToString())).Expect("createOrder", 409);
            var delivered = await (await partner.PatchAsync($"/v1/orders/{orderId}", new { status = "DELIVERED", occurredAt = Contract.Now() })).Expect("updateOrderStatus", 200);
            Assert.Equal("ACCRUED", delivered.GetProperty("attribution").GetProperty("lines").EnumerateArray().Single(l => l.GetProperty("lineId").GetString() == "1").GetProperty("state").GetString());
            var returned = await (await partner.PostWithKeyAsync($"/v1/orders/{orderId}/adjustments",
                new { adjustments = new object[] { new { adjustmentId = NewRef("ret-"), lineId = "1", type = "RETURN", quantity = 1, occurredAt = Contract.Now() } } }, null))
                .Expect("createOrderAdjustments", 202);
            Assert.Equal("2.90", returned.GetProperty("lines").EnumerateArray().Single(l => l.GetProperty("lineId").GetString() == "1").GetProperty("fee").GetProperty("amount").GetString());
            await (await partner.GetAsync($"/v1/orders/{orderId}")).Expect("getOrder", 200);
            await (await partner.GetAsync($"/v1/orders/{orderId}/attribution")).Expect("getOrderAttribution", 200);

            // 7. Settlement after the return window: finalizers, then month close of the server's current period.
            await SetClockAsync(admin, 3 * 86400);
            await JobAsync(admin, "finalize-reco-charges");
            await JobAsync(admin, "finalize-attributions");
            var charges = await (await partner.GetAsync($"/v1/billing/recommendation-charges?cartRef={cart}&limit=50")).Expect("listRecommendationCharges", 200);
            var items = charges.GetProperty("items").EnumerateArray().ToList();
            var finals = items.Where(i => i.GetProperty("state").GetString() == "FINAL").ToList();
            Assert.Equal(2, finals.Count);
            var rejected = items.Single(i => i.GetProperty("chargeId").GetString() == noImpressionId);
            Assert.Equal("REJECTED", rejected.GetProperty("state").GetString());
            Assert.Equal("NO_IMPRESSION", rejected.GetProperty("rejectionReason").GetString());

            var period = await ServerPeriodAsync(admin);
            var close = await JobAsync(admin, "month-close", $"?period={period}&partnerId={ApiFixture.QuickBasket}");
            Assert.Equal("ISSUED", close[0].GetProperty("outcome").GetString());
            var statements = await (await partner.GetAsync($"/v1/billing/statements?period={period}")).Expect("listStatements", 200);
            var statement = statements.GetProperty("items")[0];
            var statementId = statement.GetProperty("statementId").GetString();
            var totals = statement.GetProperty("totals");
            Assert.Equal("ISSUED", statement.GetProperty("status").GetString());
            Assert.True(decimal.Parse(totals.GetProperty("recommendationFee").GetProperty("amount").GetString()!) >= 2m);
            Assert.True(decimal.Parse(totals.GetProperty("premiumFee").GetProperty("amount").GetString()!) >= 5.80m);
            await (await partner.GetAsync($"/v1/billing/statements/{statementId}")).Expect("getStatement", 200);
            await (await partner.GetAsync($"/v1/billing/statements/{statementId}/lines")).Expect("listStatementLines", 200);
            var billed = await (await partner.GetAsync($"/v1/billing/statements/{statementId}/recommendation-charges")).Expect("listStatementRecommendationCharges", 200);
            Assert.Equal(totals.GetProperty("recommendationChargeCount").GetInt32(), billed.GetProperty("items").GetArrayLength());

            // 8. Dispute one RA-1 charge; upheld -> credit note of INR 1.00 + 18% GST.
            var disputedCharge = finals[0].GetProperty("chargeId").GetString()!;
            var dispute = await (await partner.PostWithKeyAsync("/v1/billing/disputes",
                new { statementId, reason = "NOT_ADDED_TO_CART", recommendationChargeIds = new[] { disputedCharge }, comment = "Shopper did not add the item" }, null))
                .Expect("createDispute", 201);
            Assert.Equal("OPEN", dispute.GetProperty("status").GetString());
            var disputeId = dispute.GetProperty("disputeId").GetString();
            var resolved = JsonDocument.Parse(await (await admin.PostAsync($"/admin/disputes/{disputeId}/resolve",
                Contract.Body(new { decision = "UPHELD", resolution = "Client logs show a double tap" }))).Content.ReadAsStringAsync()).RootElement;
            Assert.Equal("UPHELD", resolved.GetProperty("status").GetString());
            Assert.Equal("1.18", resolved.GetProperty("creditNoteAmount").GetProperty("amount").GetString());
            await (await partner.GetAsync($"/v1/billing/disputes/{disputeId}")).Expect("getDispute", 200);
            var credited = await (await partner.GetAsync($"/v1/billing/recommendation-charges/{disputedCharge}")).Expect("getRecommendationCharge", 200);
            Assert.Equal("CREDITED", credited.GetProperty("state").GetString());
            var trial = JsonDocument.Parse(await admin.GetStringAsync("/admin/ledger/trial-balance")).RootElement;
            Assert.True(trial.GetProperty("balanced").GetBoolean(), trial.GetRawText());

            // 9. The statement webhook is delivered with a valid signature.
            var received = false;
            for (var attempt = 0; attempt < 20 && !received; attempt++)
            {
                await JobAsync(admin, "dispatch-webhooks");
                var hooks = JsonDocument.Parse(await partner.GetStringAsync("/demo/webhooks/quickbasket")).RootElement;
                received = hooks.EnumerateArray().Any(h => h.GetProperty("type").GetString() == "billing.statement.issued" && h.GetProperty("signatureValid").GetBoolean());
                if (!received) await Task.Delay(500);
            }
            Assert.True(received, "billing.statement.issued webhook was not received with a valid signature");
        }
        finally
        {
            await SetClockAsync(admin, 0);
        }
    }

    [Fact]
    public async Task RecommendedAddFeeIsAdditiveAndOnlyForLabeledAlternatives()
    {
        fx.SkipIfUnavailable();
        var partner = await fx.ClientAsync();
        var cart = NewRef("c_add_");
        var evaluation = await EvaluateAsync(partner, NoodleCart(cart));
        foreach (var suggestion in evaluation.GetProperty("suggestions").EnumerateArray())
        foreach (var alternative in suggestion.GetProperty("alternatives").EnumerateArray())
        {
            var label = alternative.GetProperty("recommendation");
            Assert.Equal("HEALTHSWAP_RECOMMENDED", label.GetProperty("code").GetString());
            Assert.False(string.IsNullOrWhiteSpace(label.GetProperty("text").GetString()));
            Assert.DoesNotContain("1.00", label.GetProperty("text").GetString()); // the shopper label never states the partner fee
        }
        // Events for an unknown suggestion are rejected per event; the batch is still accepted.
        var ack = await EventsAsync(partner, AddToCart(Guid.NewGuid().ToString()));
        Assert.Equal(0, ack.GetProperty("accepted").GetInt32());
        Assert.Equal("HS-EVENT-UNKNOWN-SUGGESTION", ack.GetProperty("rejected")[0].GetProperty("code").GetString());
    }

    [Fact]
    public async Task CartEvaluationIsIdempotentAndReadable()
    {
        fx.SkipIfUnavailable();
        var partner = await fx.ClientAsync();
        var request = NoodleCart(NewRef("c_idem_"));
        var key = Guid.NewGuid().ToString();
        var first = await (await partner.PostWithKeyAsync("/v1/cart-evaluations", request, key)).Expect("evaluateCart", 200);
        var second = await partner.PostWithKeyAsync("/v1/cart-evaluations", request, key);
        var replay = await second.Expect("evaluateCart", 200);
        Assert.Equal(first.GetProperty("evaluationId").GetString(), replay.GetProperty("evaluationId").GetString());
        var conflict = await partner.PostWithKeyAsync("/v1/cart-evaluations", NoodleCart(NewRef("c_other_")), key);
        await conflict.Expect("evaluateCart", 409);
        await Task.Delay(200);
        var fetched = await (await partner.GetAsync($"/v1/cart-evaluations/{first.GetProperty("evaluationId").GetString()}")).Expect("getCartEvaluation", 200);
        Assert.Equal(first.GetProperty("cartRef").GetString(), fetched.GetProperty("cartRef").GetString());
    }
}
