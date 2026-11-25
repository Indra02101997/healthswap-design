# 08 — Manual Test Guide: Python reference implementation

| | |
| --- | --- |
| Document | HealthSwap reference implementation: manual test guide |
| Version | 1.0 (draft) |
| Related | [06 Recommended label and add fee (RA-1)](06-recommended-label-and-add-fee.md) · [07 Reference implementation](07-reference-implementation.md) · [OpenAPI](../api/openapi.yaml) · [Smoke test](../scripts/smoke_test.py) |

---

## 1. Purpose

This guide lets a reviewer check the Python reference implementation by hand on Windows, macOS or Linux:

- **A.** The demo shop: the "Recommended by HealthSwap" label, the fee disclosure and the RA-1 charges that adds create.
- **B.** The automated end-to-end smoke test.
- **C.** An API walkthrough of RA-1 together with the FP-1 fair-premium fee, from cart evaluation to the monthly
  statement, a dispute and the ledger.
- **D.** Negative tests: authentication, authorization, validation, idempotency, tenant isolation, SDK sessions and the
  kill switch.
- **E.** The MCP agent tools.
- **F.** The automated test suites.
- **G.** Troubleshooting.

Section 10 is the expected-results checklist. Everything runs locally on synthetic data; the client secrets used below are
development-only values from [data/seed/demo/partners.json](../data/seed/demo/partners.json).

The API steps (C to E) are Python snippets rather than curl or PowerShell commands: they run unchanged on every operating
system, need nothing beyond the project's virtual environment and use the Python server SDK in [sdk/python](../sdk/python)
where it has a method for the call.

## 2. Prerequisites

| Requirement | Version | Check |
| --- | --- | --- |
| Python | 3.11 or later | `python --version` (Windows: `py -3 --version`; macOS and Linux: `python3 --version`) |
| Node.js with npm | Node.js 20 or later | `node --version` |
| Browser | Current Chrome, Edge, Firefox or Safari | Needed for section A only |
| Free TCP port | 8080 for the API | The dev database picks a free port by itself |
| Package index access | PyPI and the npm registry (or your mirrors) | Needed once, for the installation |

Docker and a local PostgreSQL are not needed: the development database is PGlite (PostgreSQL compiled to WebAssembly with
pgvector, pg_trgm and pgcrypto) started by `scripts/run_local.py` through Node.js. The guide was verified on Windows 11
with Python 3.13 and Node.js 22.

## 3. Setup from a clean clone

### 3.1 Create and activate a virtual environment

Windows (PowerShell):

```powershell
cd healthswap-design
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS and Linux (bash or zsh):

```bash
cd healthswap-design
python3 -m venv .venv
source .venv/bin/activate
```

If PowerShell refuses to run `Activate.ps1`, allow scripts for the current window only with
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` and activate again. On Debian and Ubuntu, `python3 -m venv`
needs the `python3-venv` package (`sudo apt install python3-venv`).

### 3.2 Install the packages

Run these commands in the activated environment. They are the same in PowerShell, bash and zsh:

```shell
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
python -m pip install -e sdk/python
npm ci --prefix tools/devdb
```

| Command | Installs |
| --- | --- |
| `pip install -e ".[dev]"` | The `healthswap` package (API, workers, `hs` CLI) with pytest, hypothesis and ruff |
| `pip install -e sdk/python` | The Python server SDK (`healthswap_client`) used in sections C to E |
| `npm ci --prefix tools/devdb` | PGlite and its socket server for the development database (exact versions from the lock file) |
| `pip install -e ".[ml]"` (optional) | scikit-learn, skl2onnx and onnx, only to train models ([07 §9](07-reference-implementation.md#9-ml-models)) |

### 3.3 Start the stack

```shell
python scripts/run_local.py
```

The first start creates `.data/devdb`, applies [db/schema.sql](../db/schema.sql), seeds the reference data, knowledge
base and demo tenants, and starts the API. It takes about 20 seconds (database about 5 s, seeding about 12 s). When the
API is ready the script prints:

```text
HealthSwap API (Development) is running on http://127.0.0.1:8080
  Demo shop     http://127.0.0.1:8080/demo
  Contract      http://127.0.0.1:8080/openapi.yaml  (partner API under http://127.0.0.1:8080/v1)
  MCP           http://127.0.0.1:8080/mcp  (Streamable HTTP, bearer token)
  Tokens        POST http://127.0.0.1:8080/oauth2/token  (client credentials)
  Health        http://127.0.0.1:8080/healthz  http://127.0.0.1:8080/readyz
  Demo clients  quickbasket-server (QuickBasket, IN), hometown-server (Hometown Grocer, US), hs-admin (admin plane /admin)
                development-only secrets: data/seed/demo/partners.json
  Database      postgresql://postgres:postgres@127.0.0.1:<port>/postgres?sslmode=disable&connect_timeout=30
  Smoke test    python scripts/smoke_test.py --base-url http://127.0.0.1:8080
Press Ctrl+C to stop.
```

Leave this terminal running. Open a second terminal, change to the repository and activate the virtual environment
there too (section 3.1) for sections B to F. Ctrl+C in the first terminal stops the API and then the dev database.

| `run_local.py` option | Effect |
| --- | --- |
| *(none)* | Dev database in `.data/devdb`, seeded on first use and kept between runs |
| `--reset` | Wipe `.data/devdb` and seed again (a fresh database) |
| `--memory` | In-memory dev database, seeded on every start and gone after Ctrl+C |
| `--data DIR` | Keep the dev database in `DIR` instead of `.data/devdb` |
| `--port N`, `--host H` | API address (default `127.0.0.1:8080`) |
| `--no-seed` | Do not seed an empty database |
| `--database-url URL` | Use an existing PostgreSQL 16+ with pgvector, pg_trgm, pgcrypto and `db/schema.sql` applied instead of the dev database; cannot be combined with `--memory`, `--data` or `--reset` |

### 3.4 Test identities

| Client id | Secret (development only) | Partner | What it is for |
| --- | --- | --- | --- |
| `quickbasket-server` | `dev-only-quickbasket-secret-change-me` | QuickBasket: market IN, INR, store `BLR-KOR-012`, RA-1 INR 1.00 per add, GST 18% | The partner backend in every section |
| `hometown-server` | `dev-only-hometown-secret-change-me` | Hometown Grocer: market US, USD, RA-1 disabled | Tenant isolation (section D) |
| `hs-admin` | `dev-only-admin-secret-change-me` | None (scope `hs.admin`) | Admin plane `/admin`: clock, jobs, disputes, ledger, kill switch |

The QuickBasket ids used below: partner `6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01`, noodles `SKU-NDL-280` (GTIN
8901234567890, INR 56.00, 280 g) with the recommended alternative `SKU-MLT-256` (INR 89.00, 256 g), cola
`SKU-COLA-750` (INR 40.00) with the recommended alternative `SKU-LEMW-500` (INR 35.00).

## 4. A. Demo shop

Open <http://127.0.0.1:8080/demo>. The page is a QuickBasket cart that uses the browser SDK
([sdk/web](../sdk/web/healthswap-web.js)) with a short-lived SDK session; the "Partner view (demo only)" panel shows the
RA-1 charges of this cart, read server-side by the demo backend.

| Step | Action | Expected result |
| --- | --- | --- |
| A1 | Open the page | "Your cart": Swaad Masala Instant Noodles 4-pack 280 g, ₹56.00 × 2 = ₹112.00; Fizzo Cola 750 ml, ₹40.00 × 1 = ₹40.00; cart total ₹152.00. Partner view: "No charges yet: add a recommended swap." |
| A2 | Click **Find healthier swaps** | Status "2 healthier swaps found." The fee disclosure is shown above the suggestions: "HealthSwap may receive a fee from QuickBasket when you add a recommended item to your cart or buy a suggested swap. Recommendations are ranked on nutrition, not fees." |
| A3 | Look at the first card | "MilletMagic Millet Masala Noodles 4-pack 256 g" with the label **Recommended by HealthSwap**, grade C (was D), ₹89.00, "₹33.00 more per unit" and the reason "3x the fibre per 100 g (6.1 g vs 2.0 g), plus 52% less saturated fat per 100 g". The second card: "PureSip Sparkling Lemon Water 500 ml", grade A (was D), ₹35.00, "₹5.00 less per unit", "100% less added sugar per 100 ml, plus less processed (NOVA 1 vs 4)". No card mentions the fee amount. Footer: "Based on pack label data. Always check the label, especially for allergies." and the link "How HealthSwap compares products" |
| A4 | Watch the event log | "SDK session token acquired", "Cart evaluated in … ms: 2 suggestion(s)", then "IMPRESSION queued (suggestion …)" and "Sent 1 event(s): 1 accepted, 0 duplicates" once a card has been at least 50% visible for 1 second (a card below the fold is reported only after you scroll to it) |
| A5 | Click **Why?** on the MilletMagic card | The panel lists "3x the fibre per 100 g (6.1 g vs 2.0 g)" and "52% less saturated fat per 100 g", the fee disclosure, "Based on pack label data per 100 g. Always check the label, especially for allergies.", "This wording was written with AI assistance and checked against the label data." and the methodology link |
| A6 | Click **Add** on the MilletMagic card | Status "Swapped to MilletMagic Millet Masala Noodles 4-pack 256 g." with an **Undo** button and a 5 s countdown. The cart line becomes MilletMagic with the label, ₹89.00 × 2 = ₹178.00, total ₹218.00. Partner view: ACCEPT, SKU-MLT-256 x2, INR 1.00, PENDING (one fee for the swap, not one per unit) |
| A7 | Click **Add** on the PureSip card, then **Undo** within 5 seconds | The cart shows Fizzo Cola again and the PureSip card is back. Partner view: ACCEPT, SKU-LEMW-500 x1, INR 1.00, VOID with reason UNDONE_WITHIN_GRACE |
| A8 | Change the MilletMagic quantity from 2 to 3 (type 3 and press Tab, or use the arrows) | Line total ₹267.00, cart total ₹307.00. Partner view: ADD_TO_CART, SKU-MLT-256 x1, INR 1.00, PENDING. Summary: "3 charge(s): PENDING ₹2.00, VOID ₹1.00. PENDING charges become FINAL after the settlement window (10 minutes) when the evidence holds." |
| A9 | Click **Keep original** on the PureSip card | The card disappears, status "Kept Fizzo Cola 750 ml.", the event log shows "DISMISS queued"; no new charge |
| A10 | Click **Find healthier swaps** again | "No healthier swaps for this cart." The noodles line already holds a HealthSwap pick and the dismissed lemon water is in its 7-day cool-down |

Lowering a quantity sends no event, and nothing in the page states the fee amount to the shopper. The charges become
FINAL (or REJECTED) after the 10-minute settlement window; section C shows that step with time travel.

## 5. B. Automated smoke test

In the second terminal:

```shell
python scripts/smoke_test.py --base-url http://127.0.0.1:8080
```

The script runs nine sections against the running stack and prints one `PASS` line per check:

```text
== 1. Cart evaluation: labeled recommendation ==
PASS  ...
== 2. RA-1 events: impression, accept, add, undo, duplicates ==
== 3. Line labels on the cart after the swap ==
== 4. Product page (PDP) recommendation without an impression ==
== 5. FP-1 order attribution (IN-1: fee INR 5.80) ==
== 6. Settlement: time travel, finalizers, month close ==
      statement: FP-1 5.80 - reversals 2.90 + RA-1 2.00 (2 adds) = net 4.90; tax 0.88; total 5.78
== 7. Dispute an RA-1 charge and resolve it ==
== 8. Webhooks ==
== 9. MCP tools (stateless streamable HTTP) ==

ALL CHECKS PASSED
```

The exit code is 0 on success. The statement line above is what a fresh database prints. If you did section A first,
the FINAL adds of your demo cart are billed in the same statement, for example
`RA-1 4.00 (4 adds) = net 6.90; tax 1.24; total 8.14` after the steps in section 4.

The smoke test moves the business clock forward, closes the current month and resets the clock at the end. A second run
on the same database finds the statement already issued, prints two `NOTE` lines and ends with
`ALL EXECUTED CHECKS PASSED (billing checks skipped)`. Restart with `python scripts/run_local.py --reset` (or `--memory`)
to replay the billing checks.

## 6. C. API walkthrough

The walkthrough reproduces the RA-1 and FP-1 example IN-1 of [06 §10](06-recommended-label-and-add-fee.md#10-interaction-with-fp-1-worked-example-in-1):
FP-1 5.80 − reversal 2.90 + RA-1 2 × 1.00 = net 4.90, GST 18% 0.88, total 5.78, then an upheld RA-1 dispute with a credit
note of 1.18. It needs a statement-free month, so start from a fresh database:

1. Stop the server with Ctrl+C in the first terminal.
2. Start it again with `python scripts/run_local.py --reset` and wait for the banner.
3. In the second terminal (virtual environment active) start Python with `python`.
4. Paste the blocks below one at a time at the `>>>` prompt. After a block that ends with a `for` loop or a function,
   press Enter once more so that Python runs it.

The expected output follows each block. Ids, timestamps and dates differ on your machine; the amounts, states and codes
must match. If the API runs on another port, change `BASE` in C1.

### C1. Set up the session

The partner calls go through the Python SDK (`HealthSwapClient`). Its cart evaluation fails open after 150 ms by default,
which is too short for a cold local stack, so the walkthrough allows 5 seconds. Calls without an SDK method and the admin
plane use `httpx` with a small client-credentials helper.

```python
import json, uuid
import httpx
from healthswap_client import HealthSwapClient, events

BASE = "http://127.0.0.1:8080"
QB = "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01"
STORE = "BLR-KOR-012"

def show(value):
    print(json.dumps(value, indent=2, ensure_ascii=False))

def inr(amount):
    return {"amount": amount, "currency": "INR"}

class Token(httpx.Auth):
    """Client-credentials token from the dev identity provider; fetched again after a 401 (tokens live 10 minutes)."""
    def __init__(self, client_id, secret):
        self.credentials, self.value = (client_id, secret), None
    def auth_flow(self, request):
        for attempt in range(2):
            if self.value is None or attempt == 1:
                reply = httpx.post(BASE + "/oauth2/token", data={"grant_type": "client_credentials"}, auth=self.credentials)
                self.value = reply.json()["access_token"]
            request.headers["Authorization"] = "Bearer " + self.value
            response = yield request
            if response.status_code != 401:
                return

partner = HealthSwapClient(BASE, "quickbasket-server", "dev-only-quickbasket-secret-change-me", evaluation_timeout=5)
qb = httpx.Client(base_url=BASE, auth=Token("quickbasket-server", "dev-only-quickbasket-secret-change-me"), timeout=60)
admin = httpx.Client(base_url=BASE, auth=Token("hs-admin", "dev-only-admin-secret-change-me"), timeout=60)
admin.post("/admin/clock", json={"offsetSeconds": 0}).json()
```

Expected output (the business clock without an offset):

```text
{'utcNow': '2026-11-24T14:56:23.869978Z', 'offsetSeconds': 0}
```

### C2. Evaluate the noodle cart and read the label

```python
cart, user = "c_manual_" + uuid.uuid4().hex[:8], "u_manual_" + uuid.uuid4().hex[:8]
evaluation = partner.evaluate_cart({
    "cartRef": cart, "userRef": user, "market": "IN", "storeId": STORE, "currency": "INR", "locale": "en-IN",
    "context": {"placement": "CART", "channel": "ANDROID"},
    "lines": [
        {"lineId": "1", "partnerSku": "SKU-NDL-280", "gtin": "8901234567890", "quantity": 2, "unitPrice": inr("56.00")},
        {"lineId": "2", "partnerSku": "SKU-COLA-750", "quantity": 1, "unitPrice": inr("40.00")},
    ],
})
noodles = next(s for s in evaluation["suggestions"] if s["lineId"] == "1")
alt, sid = noodles["alternatives"][0], noodles["suggestionId"]
print(evaluation["degraded"], len(evaluation["suggestions"]), alt["product"]["partnerSku"], alt["unitPrice"]["amount"], alt["unitPriceDelta"]["amount"], alt["normalizedPriceRatio"])
show(alt["recommendation"])
print(evaluation["disclosures"]["feeDisclosure"])
```

Expected output: two suggestions, the MilletMagic alternative at INR 89.00 (INR 33.00 more per unit, 1.74 times the
price per 100 g) and its chargeable RA-1 label. Neither the label nor the disclosure states the amount.

```text
False 2 SKU-MLT-256 89.00 33.00 1.74
{
  "code": "HEALTHSWAP_RECOMMENDED",
  "text": "Recommended by HealthSwap",
  "shortText": "HealthSwap pick",
  "disclosure": "HealthSwap may receive a fee from QuickBasket when you add a recommended item to your cart or buy a suggested swap. Recommendations are ranked on nutrition, not fees.",
  "chargeable": true,
  "feeRule": "RA-1"
}
HealthSwap may receive a fee from QuickBasket when you add a recommended item to your cart or buy a suggested swap. Recommendations are ranked on nutrition, not fees.
```

### C3. Report IMPRESSION and ACCEPT

```python
impression = events.impression(sid)
accept = events.accept(sid, "SKU-MLT-256", quantity=2)
ack = partner.report_events([impression, accept])
show(ack)
```

Expected output: both events accepted and one PENDING charge of INR 1.00 for the ACCEPT (quantity 2 still counts as one
add). `finalizesAt` is the add time plus the 600-second settlement window.

```text
{
  "accepted": 2,
  "duplicates": 0,
  "charges": [
    {
      "eventId": "0a23709f-84c9-4dac-801e-1497cf99f457",
      "chargeId": "01a20e0e-d9c1-77cd-befb-11a3f4d4c48f",
      "state": "PENDING",
      "amount": {
        "amount": "1.00",
        "currency": "INR"
      },
      "finalizesAt": "2026-11-24T15:06:23.983000Z"
    }
  ]
}
```

Send the same ACCEPT again (same `eventId`):

```python
replay = partner.report_events([accept])
print(replay["duplicates"], replay["charges"][0]["chargeId"] == ack["charges"][0]["chargeId"])
```

Expected output: counted as a duplicate; the acknowledgement points at the existing charge, no second charge exists.

```text
1 True
```

### C4. Another add, an undone add and a SKU that was not recommended

```python
second = partner.report_events([events.add_to_cart(sid, "SKU-MLT-256")])
undone = partner.report_events([events.add_to_cart(sid, "SKU-MLT-256"), events.undo(sid, "SKU-MLT-256")])
voided = partner.get_recommendation_charge(undone["charges"][0]["chargeId"])
print(second["charges"][0]["state"], voided["state"], voided["rejectionReason"])
show(partner.report_events([events.add_to_cart(sid, "SKU-CRM-500")]))
```

Expected output: the second add is PENDING, the add followed by an UNDO within 5 seconds is VOID, and an add of a SKU
that the suggestion did not recommend is rejected at ingestion (the batch itself is still accepted with HTTP 202).

```text
PENDING VOID UNDONE_WITHIN_GRACE
{
  "accepted": 0,
  "duplicates": 0,
  "rejected": [
    {
      "eventId": "84796b72-c9d3-4503-a396-ff81f95d92ea",
      "code": "HS-EVENT-SKU-NOT-RECOMMENDED",
      "message": "alternativePartnerSku must be one of the suggestion's recommended alternatives"
    }
  ]
}
```

### C5. List the charges of the cart

```python
for c in partner.list_recommendation_charges(cart_ref=cart)["items"]:
    print(c["eventType"], c["quantity"], c["amount"]["amount"], c["state"], c.get("rejectionReason") or "-")

```

Expected output (newest first):

```text
ADD_TO_CART 1.0 1.00 VOID UNDONE_WITHIN_GRACE
ADD_TO_CART 1.0 1.00 PENDING -
ACCEPT 2.0 1.00 PENDING -
```

### C6. The cart line now holding the recommended product is labeled

```python
again = partner.evaluate_cart({"cartRef": cart, "userRef": user, "market": "IN", "storeId": STORE, "currency": "INR",
                               "lines": [{"lineId": "1", "partnerSku": "SKU-MLT-256", "quantity": 3, "unitPrice": inr("89.00")}]})
show(again["lineLabels"])
print(again["suggestions"])
```

Expected output: the line with the added recommended product carries the label (render it on the cart line); a line that
already holds a HealthSwap pick gets no new suggestion.

```text
[
  {
    "lineId": "1",
    "partnerSku": "SKU-MLT-256",
    "suggestionId": "01a20e0e-d9aa-74e1-8de9-43fd666454a9",
    "label": {
      "code": "HEALTHSWAP_RECOMMENDED",
      "text": "Recommended by HealthSwap",
      "shortText": "HealthSwap pick",
      "disclosure": "HealthSwap may receive a fee from QuickBasket when you add a recommended item to your cart or buy a suggested swap. Recommendations are ranked on nutrition, not fees.",
      "chargeable": true,
      "feeRule": "RA-1"
    }
  }
]
[]
```

### C7. Product page (PDP) alternatives and an add without an impression

```python
pdp = partner.list_product_alternatives(noodles["original"]["productId"], "IN", store_id=STORE, cart_ref=cart, placement="PDP")
pdp_add = partner.report_events([events.add_to_cart(pdp["suggestionId"], pdp["alternatives"][0]["product"]["partnerSku"])])
pdp_charge = pdp_add["charges"][0]["chargeId"]
print(pdp["suggestionId"] != sid, pdp["alternatives"][0]["recommendation"]["chargeable"], pdp_add["charges"][0]["state"])
```

Expected output: the product page gets its own suggestion (because a cart is known) with a chargeable label, and the add
is PENDING. No IMPRESSION was sent for it, so the finalizer rejects it in C9.

```text
True True PENDING
```

### C8. FP-1 order IN-1, delivery and a return

```python
order_id = "ORD-MANUAL-" + uuid.uuid4().hex[:8]
order = partner.create_order({
    "orderId": order_id, "cartRef": cart, "userRef": user, "storeId": STORE, "market": "IN", "currency": "INR",
    "placedAt": events.timestamp(), "fulfillmentType": "QUICK_COMMERCE", "orderDiscount": inr("8.00"),
    "lines": [
        {"lineId": "1", "partnerSku": "SKU-MLT-256", "quantity": 2, "unitPrice": inr("89.00"), "allocatedOrderDiscount": inr("8.00"),
         "netAmount": inr("170.00"), "suggestionId": sid},
        {"lineId": "2", "partnerSku": "SKU-COLA-750", "quantity": 1, "unitPrice": inr("40.00"), "netAmount": inr("40.00")},
    ],
})
a = order["attribution"]["lines"][0]
print(a["state"], a["originalUnitPriceSnapshot"]["amount"], a["alternativeNetUnitPrice"]["amount"], a["packDelta"]["amount"], a["sizeNormalizedDelta"]["amount"], a["fairPremium"]["amount"], a["fee"]["amount"])
```

Expected output: net unit price 170.00 / 2 = 85.00; pack delta 85.00 − 56.00 = 29.00; size-normalized delta
85.00 − 56.00 × 256 / 280 = 33.80; fair premium min(29.00, 33.80) × 2 = 58.00; FP-1 fee 10% = 5.80, PENDING until delivery.

```text
PENDING 56.00 85.00 29.00 33.80 58.00 5.80
```

Deliver the order and return one of the two noodle packs:

```python
delivered = partner.update_order_status(order_id, {"status": "DELIVERED", "occurredAt": events.timestamp()})
returned = partner.create_order_adjustments(order_id, {"adjustments": [
    {"adjustmentId": "ret-" + uuid.uuid4().hex[:8], "lineId": "1", "type": "RETURN", "quantity": 1, "occurredAt": events.timestamp()}]})
line = next(x for x in returned["lines"] if x["lineId"] == "1")
print(delivered["attribution"]["lines"][0]["state"], line["state"], line["fee"]["amount"])
```

Expected output: the fee accrues at delivery; the return reverses half of it (2.90), the line stays ACCRUED.

```text
ACCRUED ACCRUED 2.90
```

### C9. Time travel and the finalizers

The admin clock shifts the business clock of the whole API (development only). Three days pass the RA-1 settlement window
(10 minutes) and the two-day return window of a quick-commerce order.

```python
show(admin.post("/admin/clock", json={"advanceSeconds": 3 * 86400}).json())
show(admin.post("/admin/jobs/finalize-reco-charges").json())
show(admin.post("/admin/jobs/finalize-attributions").json())
```

Expected output:

```text
{
  "utcNow": "2026-11-27T14:56:24.261909Z",
  "offsetSeconds": 259200
}
{
  "job": "finalize-reco-charges",
  "result": {
    "final": 2,
    "rejected": 1
  }
}
{
  "job": "finalize-attributions",
  "result": {
    "final": 1
  }
}
```

The background finalizers run every 30 seconds (RA-1) and every 60 seconds (FP-1). If one of them ran between the clock
change and your job call, the job reports 0; the charge states below are what matters.

```python
for c in partner.list_recommendation_charges(cart_ref=cart)["items"]:
    print(c["eventType"], c["state"], c.get("rejectionReason") or "-", c.get("finalizedAt"))

print(partner.get_order_attribution(order_id)["lines"][0]["state"])
```

Expected output: the ACCEPT and the second add are FINAL; the product-page add is REJECTED for lack of an impression;
the undone add stays VOID; the FP-1 attribution is FINAL.

```text
ADD_TO_CART REJECTED NO_IMPRESSION 2026-11-27T14:56:24.265428Z
ADD_TO_CART VOID UNDONE_WITHIN_GRACE 2026-11-24T14:56:24.038297Z
ADD_TO_CART FINAL - 2026-11-27T14:56:24.265428Z
ACCEPT FINAL - 2026-11-27T14:56:24.265428Z
FINAL
```

### C10. Month close and the statement

```python
period = admin.get("/admin/clock").json()["utcNow"][:7]
show(admin.post("/admin/jobs/month-close", params={"period": period, "partnerId": QB}).json())
```

Expected output: one statement with FP-1 5.80, RA-1 2.00 for 2 adds, reversals 2.90, GST 0.88 and 5.78 due.

```text
{
  "job": "month-close",
  "result": [
    {
      "partnerId": "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01",
      "period": "2026-11",
      "statementId": "01a20e0e-db42-7163-925c-2272691b0e71",
      "invoiceNumber": "HS-IN-2026-11-000001",
      "premiumFee": 5.8,
      "recoFee": 2.0,
      "recoChargeCount": 2,
      "reversals": 2.9,
      "tax": 0.88,
      "totalDue": 5.78,
      "outcome": "ISSUED"
    }
  ]
}
```

If `outcome` is `ALREADY_CLOSED`, this database already has a statement for the month: restart with `--reset` and run C
again. Now read the statement as the partner:

```python
statement = partner.list_statements(period=period)["items"][0]
print(statement["status"], statement["invoiceNumber"], statement["period"])
show(statement["totals"])
for x in partner.list_statement_lines(statement["statementId"])["items"]:
    print(x["lineNo"], x["type"], x["orderId"], x["amount"]["amount"])

billed = partner.list_statement_recommendation_charges(statement["statementId"])["items"]
print([(c["eventType"], c["amount"]["amount"], c["state"]) for c in billed])
```

Expected output: the statement lines are the FP-1 fee and its reversal; the RA-1 charges are listed separately and are
counted in the totals.

```text
ISSUED HS-IN-2026-11-000001 2026-11
{
  "premiumFee": {
    "amount": "5.80",
    "currency": "INR"
  },
  "recommendationFee": {
    "amount": "2.00",
    "currency": "INR"
  },
  "recommendationChargeCount": 2,
  "grossFee": {
    "amount": "7.80",
    "currency": "INR"
  },
  "reversals": {
    "amount": "2.90",
    "currency": "INR"
  },
  "credits": {
    "amount": "0.00",
    "currency": "INR"
  },
  "netFee": {
    "amount": "4.90",
    "currency": "INR"
  },
  "tax": {
    "amount": "0.88",
    "currency": "INR"
  },
  "totalDue": {
    "amount": "5.78",
    "currency": "INR"
  }
}
1 FEE ORD-MANUAL-fa78680d 5.80
2 REVERSAL ORD-MANUAL-fa78680d -2.90
[('ADD_TO_CART', '1.00', 'FINAL'), ('ACCEPT', '1.00', 'FINAL')]
```

### C11. Dispute one RA-1 charge

The partner disputes the billed ADD_TO_CART charge and the HealthSwap operator (admin plane) upholds the dispute.

```python
disputed = billed[0]["chargeId"]
dispute = partner.create_dispute({"statementId": statement["statementId"], "reason": "NOT_ADDED_TO_CART",
                                  "recommendationChargeIds": [disputed], "comment": "Shopper did not add the item"})
print(dispute["status"], partner.get_statement(statement["statementId"])["status"])
resolved = admin.post(f"/admin/disputes/{dispute['disputeId']}/resolve", json={"decision": "UPHELD", "resolution": "Client logs show a double tap"}).json()
print(resolved["status"], resolved["creditNoteAmount"], partner.get_recommendation_charge(disputed)["state"])
show(partner.get_statement(statement["statementId"])["totals"])
```

Expected output: the statement is DISPUTED while the dispute is open; the upheld dispute issues a credit note of 1.18
(INR 1.00 plus 18% GST), the charge becomes CREDITED and the totals drop to net 3.90, tax 0.70, total due 4.60.

```text
OPEN DISPUTED
UPHELD {'amount': '1.18', 'currency': 'INR'} CREDITED
{
  "premiumFee": {
    "amount": "5.80",
    "currency": "INR"
  },
  "recommendationFee": {
    "amount": "2.00",
    "currency": "INR"
  },
  "recommendationChargeCount": 2,
  "grossFee": {
    "amount": "7.80",
    "currency": "INR"
  },
  "reversals": {
    "amount": "2.90",
    "currency": "INR"
  },
  "credits": {
    "amount": "1.00",
    "currency": "INR"
  },
  "netFee": {
    "amount": "3.90",
    "currency": "INR"
  },
  "tax": {
    "amount": "0.70",
    "currency": "INR"
  },
  "totalDue": {
    "amount": "4.60",
    "currency": "INR"
  }
}
```

### C12. Ledger trial balance

```python
balance = admin.get("/admin/ledger/trial-balance", params={"partnerId": QB}).json()
for row in balance["rows"]:
    print(row)

print("balanced:", balance["balanced"])
```

Expected output: RA-1 revenue has its own account (RECO_FEE_REVENUE, 2.00 recognized, 1.00 credited); billed receivables
are 5.78 − 1.18 = 4.60; every posting balances.

```text
{'account': 'BILLED_AR', 'currency': 'INR', 'debit': 5.78, 'credit': 1.18, 'balance': 4.6}
{'account': 'DEFERRED_REVENUE', 'currency': 'INR', 'debit': 5.8, 'credit': 5.8, 'balance': 0.0}
{'account': 'FEE_REVENUE', 'currency': 'INR', 'debit': 0.0, 'credit': 2.9, 'balance': -2.9}
{'account': 'OUTPUT_TAX', 'currency': 'INR', 'debit': 0.18, 'credit': 0.88, 'balance': -0.7}
{'account': 'RECO_FEE_REVENUE', 'currency': 'INR', 'debit': 1.0, 'credit': 2.0, 'balance': -1.0}
{'account': 'UNBILLED_AR', 'currency': 'INR', 'debit': 7.8, 'credit': 7.8, 'balance': 0.0}
balanced: True
```

### C13. Signed webhooks

The statement and the dispute decision each produce a webhook to QuickBasket's demo receiver, signed with
`HealthSwap-Signature`. The background dispatcher delivers within seconds; the job call below only speeds it up.

```python
import time
for _ in range(2):
    r = admin.post("/admin/jobs/dispatch-webhooks")
    time.sleep(2)

hooks = httpx.get(BASE + "/demo/webhooks/quickbasket").json()
for hook in hooks:
    print(hook["type"], hook["signatureValid"])

show(next(h["body"] for h in hooks if h["type"] == "billing.statement.issued"))
```

Expected output (newest first; `catalog.match.review_required` was sent when the demo catalog was seeded):

```text
billing.dispute.updated True
billing.statement.issued True
catalog.match.review_required True
{
  "id": "evt_01a20e0edb5a717a8b676ad9b29ac8f8",
  "data": {
    "period": "2026-11",
    "totalDue": {
      "amount": "5.78",
      "currency": "INR"
    },
    "statementId": "01a20e0e-db42-7163-925c-2272691b0e71"
  },
  "type": "billing.statement.issued",
  "createdAt": "2026-11-24T14:56:24.419285Z",
  "partnerId": "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01",
  "apiVersion": "1.1"
}
```

### C14. Reset the clock

```python
show(admin.post("/admin/clock", json={"offsetSeconds": 0}).json())
```

Expected output:

```text
{
  "utcNow": "2026-11-24T14:56:24.593375Z",
  "offsetSeconds": 0
}
```

Keep the Python session open for sections D and E: they reuse its variables.

## 7. D. Negative tests

### D1. Authentication, authorization, routing and validation

```python
anonymous = httpx.Client(base_url=BASE, timeout=60)
hometown = httpx.Client(base_url=BASE, auth=Token("hometown-server", "dev-only-hometown-secret-change-me"), timeout=60)
small_cart = {"cartRef": "c_neg_" + uuid.uuid4().hex[:8], "market": "IN", "storeId": STORE, "currency": "INR",
              "lines": [{"lineId": "1", "partnerSku": "SKU-NDL-280", "quantity": 1, "unitPrice": inr("56.00")}]}
r = anonymous.post("/v1/cart-evaluations", json=small_cart)
print(r.status_code, r.headers["WWW-Authenticate"], r.json()["code"])
r = qb.get("/admin/partners")
print(r.status_code)
r = admin.post("/v1/cart-evaluations", json=small_cart)
print(r.status_code, r.json()["code"], r.json()["detail"])
r = qb.get(f"/v1/billing/recommendation-charges/{uuid.uuid4()}")
print(r.status_code, r.json()["code"])
r = qb.post("/v1/cart-evaluations", content="{}", headers={"Content-Type": "text/plain"})
print(r.status_code, r.json()["code"])
r = qb.post("/v1/cart-evaluations", json={"cartRef": "c_bad", "market": "IN", "currency": "INR"})
print(r.status_code, r.json()["code"], r.json()["errors"])
```

Expected output: 401 without a token, 403 for a partner token on the admin plane and for an admin token on the partner
API, 404 for an unknown charge, 415 for a non-JSON body and 400 with field errors for a body that violates the contract.
Errors are RFC 9457 problem documents with an `HS-` code.

```text
401 Bearer HS-AUTH-INVALID
403
403 HS-AUTH-SCOPE The token lacks the scope required by this operation
404 HS-NOT-FOUND
415 HS-UNSUPPORTED-MEDIA-TYPE
400 HS-REQ-INVALID [{'field': '/', 'message': "'lines' is a required property", 'code': 'required'}]
```

### D2. Idempotency-Key

```python
neg_order = {"orderId": "ORD-NEG-" + uuid.uuid4().hex[:8], "cartRef": "c_neg", "storeId": STORE, "market": "IN", "currency": "INR",
             "placedAt": events.timestamp(), "fulfillmentType": "QUICK_COMMERCE",
             "lines": [{"lineId": "1", "partnerSku": "SKU-NDL-280", "quantity": 1, "unitPrice": inr("56.00"), "netAmount": inr("56.00")}]}
r = qb.post("/v1/orders", json=neg_order)
print(r.status_code, r.json()["detail"])
key = str(uuid.uuid4())
first = qb.post("/v1/orders", json=neg_order, headers={"Idempotency-Key": key})
again = qb.post("/v1/orders", json=neg_order, headers={"Idempotency-Key": key})
print(first.status_code, again.status_code, again.headers.get("Idempotent-Replayed"), again.json() == first.json())
r = qb.post("/v1/orders", json={**neg_order, "cartRef": "c_other"}, headers={"Idempotency-Key": key})
print(r.status_code, r.json()["code"])
r = qb.post("/v1/orders", json=neg_order, headers={"Idempotency-Key": str(uuid.uuid4())})
print(r.status_code, r.json()["code"])
```

Expected output: creating an order without the header is rejected; the same key with the same body replays the stored
201 response (`Idempotent-Replayed: true`); the same key with a different body is a conflict; a new key for an existing
order id is rejected by the order service.

```text
400 The Idempotency-Key header is required for this operation
201 201 true True
409 HS-IDEMPOTENCY-CONFLICT
409 HS-ORDER-EXISTS
```

### D3. Tenant isolation

Hometown Grocer tries to read QuickBasket's data with its own valid token:

```python
r = hometown.get(f"/v1/billing/recommendation-charges/{disputed}")
print(r.status_code, r.json()["code"])
r = hometown.post("/v1/events", json={"events": [events.add_to_cart(sid, "SKU-MLT-256")]})
print(r.status_code, r.json())
r = hometown.get(f"/v1/cart-evaluations/{evaluation['evaluationId']}")
print(r.status_code)
print(len(hometown.get("/v1/billing/recommendation-charges").json()["items"]))
```

Expected output: another tenant's charge is "not found" (not "forbidden"), its suggestion is unknown, its evaluation
does not exist and Hometown's own charge list is empty.

```text
404 HS-NOT-FOUND
202 {'accepted': 0, 'duplicates': 0, 'rejected': [{'eventId': 'f45b73b0-29f0-4457-9ff6-27baf975ae06', 'code': 'HS-EVENT-UNKNOWN-SUGGESTION', 'message': 'Unknown or expired suggestionId'}]}
404
0
```

### D4. SDK session: bound to one cart, no billing data

```python
sdk_cart = "c_sdk_" + uuid.uuid4().hex[:8]
session = partner.create_sdk_session({"cartRef": sdk_cart, "market": "IN", "storeId": STORE})
print(session["tokenType"], sorted(session["scopes"]))
browser = httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + session["token"]}, timeout=60)
own = browser.post("/v1/cart-evaluations", json={**small_cart, "cartRef": sdk_cart})
other = browser.post("/v1/cart-evaluations", json={**small_cart, "cartRef": "c_someone_else"})
print(own.status_code, other.status_code, other.json()["code"], other.json()["detail"])
r = browser.get("/v1/billing/statements")
print(r.status_code, r.json()["code"])
sdk_sid = own.json()["suggestions"][0]["suggestionId"]
sdk_sku = own.json()["suggestions"][0]["alternatives"][0]["product"]["partnerSku"]
print(browser.post("/v1/events", json={"events": [events.impression(sdk_sid), events.add_to_cart(sdk_sid, sdk_sku)]}).json())
print(browser.post("/v1/events", json={"events": [events.add_to_cart(sid, "SKU-MLT-256")]}).json())
print([(c["cartRef"], c["state"]) for c in partner.list_recommendation_charges(cart_ref=sdk_cart)["items"]])
```

Expected output: the session token carries three scopes and works only for its own cart; billing endpoints are out of
scope; the add from the browser creates a charge, but the response to the browser carries no `charges` member; events for
another cart's suggestion are rejected; the partner backend sees the charge.

```text
Bearer ['carts.evaluate', 'events.write', 'products.read']
200 403 HS-AUTH-SCOPE The SDK session is bound to another cart
403 HS-AUTH-SCOPE
{'accepted': 2, 'duplicates': 0}
{'accepted': 0, 'duplicates': 0, 'rejected': [{'eventId': 'b6e33ca9-57c9-4cac-abd5-c6139f4d9d55', 'code': 'HS-AUTH-SESSION-SCOPE', 'message': 'The suggestion belongs to another cart session'}]}
[('c_sdk_9137ae03', 'PENDING')]
```

### D5. Kill switch

```python
show(admin.post(f"/admin/partners/{QB}/kill-switch", json={"enabled": True}).json())
off = partner.evaluate_cart({**small_cart, "cartRef": "c_kill_" + uuid.uuid4().hex[:8]})
print(off["degraded"], off["degradationReasons"], off["suggestions"])
show(admin.post(f"/admin/partners/{QB}/kill-switch", json={"enabled": False}).json())
print(len(partner.evaluate_cart({**small_cart, "cartRef": "c_back_" + uuid.uuid4().hex[:8]})["suggestions"]))
```

Expected output: with the kill switch on, evaluations return no suggestions and the degradation reason `KILL_SWITCH`
(the cart is never blocked); switched off, suggestions are back. The admin call takes effect immediately in this process
(other API instances pick it up within 15 seconds).

```text
{
  "partnerId": "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01",
  "killSwitch": true,
  "effectiveWithinSeconds": 15
}
True ['KILL_SWITCH'] []
{
  "partnerId": "6f1d0c2a-5b7e-4c1a-9d3e-1a2b3c4d5e01",
  "killSwitch": false,
  "effectiveWithinSeconds": 15
}
1
```

## 8. E. MCP check

The MCP endpoint `/mcp` is a stateless Streamable HTTP server for AI shopping agents. Without a token it answers 401 and
points to the OAuth protected-resource metadata:

```python
r = httpx.post(BASE + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers={"Accept": "application/json, text/event-stream"})
print(r.status_code, r.headers["WWW-Authenticate"])
```

Expected output:

```text
401 Bearer resource_metadata="http://127.0.0.1:8080/.well-known/oauth-protected-resource"
```

With the QuickBasket token, list the tools and ask for alternatives as an agent would:

```python
def mcp(method, params):
    reply = qb.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                    headers={"Accept": "application/json, text/event-stream"})
    data = [line[5:] for line in reply.text.splitlines() if line.startswith("data:")]
    return json.loads(data[0]) if data else reply.json()

print(mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "manual-test", "version": "1"}})["result"]["serverInfo"])
print(sorted(tool["name"] for tool in mcp("tools/list", {})["result"]["tools"]))
agent_cart = "c_agent_" + uuid.uuid4().hex[:8]
found = mcp("tools/call", {"name": "find_healthier_alternatives", "arguments": {
    "productId": noodles["original"]["productId"], "market": "IN", "storeId": STORE, "cartRef": agent_cart}})["result"]["structuredContent"]["result"]
show(found)
```

Expected output: five tools, and an alternative that carries the label and the disclosure for the agent to show:

```text
{'name': 'healthswap', 'version': '1.0.0'}
['evaluate_cart', 'explain_swap', 'find_healthier_alternatives', 'get_product_health', 'record_swap_decision']
{
  "suggestionId": "01a20e0e-dde7-7391-881e-28f2d4492d8d",
  "expiresAt": "2026-11-24T15:26:25.063207Z",
  "disclosure": "HealthSwap may receive a fee from QuickBasket when you add a recommended item to your cart or buy a suggested swap. Recommendations are ranked on nutrition, not fees.",
  "alternatives": [
    {
      "rank": 1,
      "partnerSku": "SKU-MLT-256",
      "productId": "01a20e09-515b-7277-9e98-56af69207b3b",
      "title": "MilletMagic Millet Masala Noodles 4-pack 256 g",
      "healthDelta": 14.73,
      "unitPrice": {
        "amount": "89.00",
        "currency": "INR"
      },
      "label": "Recommended by HealthSwap",
      "explanation": "3x the fibre per 100 g (6.1 g vs 2.0 g), plus 52% less saturated fat per 100 g",
      "reasonCodes": [
        "LOWER_SUGAR",
        "LOWER_SODIUM",
        "LOWER_SAT_FAT",
        "HIGHER_FIBRE"
      ]
    }
  ]
}
```

Record that the agent showed the alternative and that the shopper added it:

```python
for decision in ("SHOWN", "ADDED_TO_CART"):
    print(mcp("tools/call", {"name": "record_swap_decision", "arguments": {
        "suggestionId": found["suggestionId"], "decision": decision, "alternativePartnerSku": found["alternatives"][0]["partnerSku"]}})["result"]["structuredContent"]["result"])

print([(c["placement"], c["eventType"], c["state"], c["amount"]["amount"]) for c in partner.list_recommendation_charges(cart_ref=agent_cart)["items"]])
```

Expected output: SHOWN is recorded as an IMPRESSION, ADDED_TO_CART creates a PENDING RA-1 charge with placement AGENT.

```text
{'accepted': 1, 'duplicates': 0}
{'accepted': 1, 'duplicates': 0, 'charges': [{'chargeId': '01a20e0e-de10-737f-9f80-12c16fbc9e04', 'state': 'PENDING', 'amount': {'amount': '1.00', 'currency': 'INR'}}]}
[('AGENT', 'ADD_TO_CART', 'PENDING', '1.00')]
```

Leave Python with `exit()`.

## 9. F. Automated tests

The tests do not use the running server: the integration tests start their own in-memory dev database per test module on
a free port. Run them in the second terminal with the virtual environment active:

```shell
python -m pytest tests -q -m "not integration"
python -m pytest tests -q -m integration
```

| Command | What it runs | Expected |
| --- | --- | --- |
| `-m "not integration"` | Domain rules, services with fakes, the API with an in-memory test harness, SDK, ML and fine-tuning tooling | `626 passed` in about a minute |
| `-m integration` | Seeding, the RA-1 and FP-1 flows, billing, disputes, webhooks and ingestion against a real dev database | `55 passed` in a few minutes |

Without Node.js or `tools/devdb/node_modules` the integration tests are skipped with the reason; the pytest header line
`integration database: ...` shows which case applies. `python -m pytest tests -q` runs both groups.

## 10. Expected-results checklist

| ID | Area | Check | Expected |
| --- | --- | --- | --- |
| 1 | Setup | `run_local.py` banner | "HealthSwap API (Development) is running on <http://127.0.0.1:8080>" |
| 2 | A | Suggestions on the demo cart | 2 cards, each labeled "Recommended by HealthSwap", fee disclosure above them, no amount shown |
| 3 | A | Add (swap accepted) | ACCEPT, INR 1.00, PENDING; one charge for quantity 2 |
| 4 | A | Undo within 5 s | VOID, UNDONE_WITHIN_GRACE |
| 5 | A | Quantity + on a labeled line | ADD_TO_CART, INR 1.00, PENDING |
| 6 | A | Keep original | DISMISS, no charge |
| 7 | B | Smoke test | `ALL CHECKS PASSED` (fresh database: total 5.78) |
| 8 | C2 | Label | `HEALTHSWAP_RECOMMENDED`, `chargeable: true`, `feeRule: RA-1`, disclosure without an amount |
| 9 | C3 | ACCEPT | One PENDING charge of INR 1.00, `finalizesAt` = add + 600 s |
| 10 | C3 | Duplicate eventId | `duplicates: 1`, same chargeId |
| 11 | C4 | Undo, wrong SKU | VOID UNDONE_WITHIN_GRACE; HS-EVENT-SKU-NOT-RECOMMENDED |
| 12 | C6 | Line labels | Label on the line holding SKU-MLT-256, no new suggestion |
| 13 | C7 | PDP | Own suggestionId, chargeable label |
| 14 | C8 | FP-1 IN-1 | Fair premium 58.00, fee 5.80; after the return 2.90 reversed |
| 15 | C9 | Finalization | 2 FINAL, 1 REJECTED NO_IMPRESSION, VOID unchanged; attribution FINAL |
| 16 | C10 | Statement | FP-1 5.80 − 2.90 + RA-1 2.00 (2 adds) = net 4.90; GST 0.88; total 5.78; invoice `HS-IN-<yyyy>-<mm>-<nnnnnn>` |
| 17 | C11 | Dispute upheld | Credit note 1.18; charge CREDITED; total due 4.60 |
| 18 | C12 | Ledger | `balanced: True`; RECO_FEE_REVENUE balance −1.00 |
| 19 | C13 | Webhooks | billing.statement.issued and billing.dispute.updated with valid signatures |
| 20 | C14 | Clock | `offsetSeconds: 0` |
| 21 | D1 | Auth and validation | 401, 403, 403, 404, 415, 400 with the codes shown |
| 22 | D2 | Idempotency | 400 without key; replay 201 `Idempotent-Replayed: true`; 409 conflict; 409 HS-ORDER-EXISTS |
| 23 | D3 | Tenant isolation | 404, unknown suggestion, 404, empty list |
| 24 | D4 | SDK session | Own cart only; billing 403; no `charges` in the browser response; charge visible to the partner |
| 25 | D5 | Kill switch | `KILL_SWITCH`, no suggestions; suggestions back after switching off |
| 26 | E | MCP | 401 with resource metadata; 5 tools; labeled alternative; AGENT charge PENDING |
| 27 | F | pytest | Both groups pass |

## 11. G. Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| `error while attempting to bind on address ('127.0.0.1', 8080)` | Port 8080 is taken. Start with `--port 8081` and use the same port in the smoke test (`--base-url`) and in `BASE` (C1). Find the owner with `Get-NetTCPConnection -LocalPort 8080` (Windows) or `lsof -i :8080` (macOS, Linux) |
| `run_local: tools/devdb/node_modules is missing: run npm ci in tools/devdb (PGlite dev database)` | Run `npm ci --prefix tools/devdb` |
| `run_local: Node.js is not installed (the PGlite dev database in tools/devdb needs Node.js 20+ on PATH)` | Install Node.js 20 or later and open a new terminal, or use an existing PostgreSQL 16+ with `--database-url` |
| `run_local: No module named 'healthswap'` | The virtual environment is not active or the package is not installed: activate it (3.1) and run the installation (3.2) |
| `ModuleNotFoundError: No module named 'healthswap_client'` | Run `python -m pip install -e sdk/python` |
| Month close returns `ALREADY_CLOSED`, or the smoke test prints `NOTE` and skips billing checks | The database already has a statement for this month. Restart with `python scripts/run_local.py --reset` (or `--memory`) |
| A finalizer job returns `0` | The background finalizer ran first. List the charges: their states are what matters |
| A webhook is missing from `/demo/webhooks/quickbasket` | Delivery is asynchronous (fan-out, then the dispatcher every second). Wait a few seconds or call `/admin/jobs/dispatch-webhooks` again |
| `failOpen` with reason `TIMEOUT` in an evaluation from the Python SDK | The SDK's default deadline is 150 ms. The first evaluations after a start can be slower; pass `evaluation_timeout=5` as in C1 |
| No suggestions for a cart that had them before | A DISMISS suppresses that swap for the same user or cart for 7 days, and a line that already holds a HealthSwap pick gets none. Use a new `cartRef` and `userRef` (the snippets generate them) or `--reset` |
| Stale suggestions or labels after `--reset` while `HS_CACHE__REDIS` is set | The hot cache lives in Redis and survived the database reset. Flush that Redis database or unset the variable (the default cache is in process memory) |
| The demo page looks outdated after an update | Reload without the browser cache (Ctrl+F5, or Cmd+Shift+R on macOS) |
| `python` opens the Microsoft Store (Windows) | Use `py -3` to create the environment; inside the activated environment `python` is the venv's interpreter |
| `Activate.ps1 cannot be loaded because running scripts is disabled` | `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`, then activate again |
| `ensurepip is not available` (Debian, Ubuntu) | `sudo apt install python3-venv` and create the environment again |
| Port numbers in logs differ between runs | Expected: the dev database takes a free port on every start; the API port stays 8080 |

## 12. Clean up

1. Leave Python with `exit()` if it is still open.
2. Press Ctrl+C in the server terminal; the script stops the API and then the dev database ("Dev database stopped.").
3. Run `deactivate` in both terminals.
4. Optional: delete the local state (dev database, logs, signing and data-protection keys, event lake) by removing the
   `.data` folder: `Remove-Item -Recurse -Force .data` (PowerShell) or `rm -rf .data` (bash, zsh).
