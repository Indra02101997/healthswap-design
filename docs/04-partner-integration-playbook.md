# 04 — Partner Integration Playbook: shipping HealthSwap as a 3rd-party plugin

## 1. Integration modes

| Mode | Who renders the UI | Partnership needed? | Can collect "10% of fair premium"? | Effort | Reach | Recommendation |
| --- | --- | --- | --- | --- | --- | --- |
| **A. Server-to-server API** | Partner (native UI) | Yes (B2B contract) | Yes, by contract | Medium (partner builds UI) | App + web | **Primary** for large platforms |
| **B. Server + UI SDK** (Android, iOS, Web, React Native, Flutter) | HealthSwap components themed by the partner | Yes | Yes | Low–medium | App + web | Mid-size partners, faster launch |
| **C. Agent tools (MCP)** | AI assistant | Yes (agent owner or merchant) | Yes, by contract | Low | Agentic channels | Ship early; growing channel |
| **D. Commerce-platform app** (Shopify, BigCommerce, Adobe Commerce, SFCC, commercetools, SAP Commerce, VTEX, WooCommerce) | HealthSwap app block / extension | No (merchant installs) | **Yes**, through platform app billing (for example Shopify usage charges) | Medium (once per platform) | Long-tail grocers and D2C food brands | Self-serve growth channel |
| **E. ONDC (India open network)** | Buyer app (partner's or HealthSwap's) | ONDC onboarding | Yes (technology-provider contract), or the buyer-app finder fee if HealthSwap runs its own buyer app | Medium–high | Indian open network | Strategic India channel |
| **F. Own consumer app + retailer developer platforms / affiliates** (Instacart Developer Platform, Kroger APIs, Walmart Affiliate API) | HealthSwap app | Program sign-up only | Not as 10% of premium; earns program commissions instead | Medium | Mostly US | Proof of value without partnerships |
| **G. Browser extension** (desktop web) | Overlay on retailer website | No | Only through disclosed, user-initiated affiliate links, where allowed | Low | Desktop only | Marketing/research only; fragile |

**Not acceptable** (legal, policy or trust risk):

- Mobile screen overlays or Android Accessibility API hacks on third-party apps (Google Play policy prohibits
  accessibility misuse; iOS sandboxing makes it impossible anyway).
- Scraping partner apps or websites, or reverse-engineering private APIs.

## 2. What large platforms will ask for (readiness checklist)

| Area | Have ready |
| --- | --- |
| Security | SOC 2 Type II report (Type I during pilot), ISO 27001/27701 roadmap, pen-test summary, VAPT for Indian enterprises, security questionnaire answers (SIG / CAIQ) |
| Privacy | DPA (GDPR Art. 28 / DPDP processor terms), sub-processor list, data-flow diagram, residency guarantee, DPIA support, deletion SLAs |
| Performance | Latency SLOs with public status page, fail-open guidance, load-test evidence at 2× their peak |
| Control | Per-placement kill switch, config UI, category allow/deny lists, brand blocklists, frequency caps, experiment controls |
| UX ownership | Design tokens/theming, white-label mode, copy review rights, localization coverage |
| Proof of value | Shadow-mode report on their catalog: coverage, example swaps reviewed by nutritionists, projected IHGMV, latency |
| Commercial | Fee model, caps, statements, reconciliation API, dispute SLA, tax-ready invoices (GST for India) |
| Liability | Methodology warranties, allergen disclaimer language, insurance (cyber, professional liability / E&O) |

## 3. Partner integration runbook (mode A/B)

1. **Contract and sandbox**: NDA → pilot agreement + DPA. HealthSwap issues sandbox OAuth clients and a webhook secret.
2. **Catalog**: the partner uploads a full catalog feed (`POST /v1/catalog/feeds`), then sends deltas
   (`POST /v1/catalog/item-batches`). HealthSwap returns a match report: matched, needs review, unmatched, data
   insufficient.
3. **Enrichment**: the partner grants access to back-of-pack images for unmatched or insufficient SKUs. HealthSwap
   runs OCR + curation and prioritizes by the partner's sales velocity.
4. **Availability**:
   - `MIRROR` mode: the partner streams inventory and price per store (`POST /v1/stores/{storeId}/inventory-batches`).
   - `PARTNER_FILTER` mode: the partner filters returned candidates itself.
5. **Shadow mode**: the partner calls `POST /v1/cart-evaluations` on real carts but doesn't render the results.
   Review coverage, latency and sample swaps jointly with the partner's category managers.
6. **UI**: native implementation (mode A) or the UI SDK (mode B) in the agreed placements (cart line chip, checkout
   sheet, product-page badge).
7. **Events and orders**: send `POST /v1/events` (impressions, accepts, dismissals), `POST /v1/orders`, status
   updates and adjustments.
8. **Reconciliation dry run**: generate a statement on pilot data and reconcile it against the partner's order
   system.
9. **Experiment**: employee dogfood → A/B (for example 5% treatment vs 5% holdout) → staged ramp with guardrails.
10. **General availability**: production credentials, SLAs active, monthly business reviews.

## 4. SDK design

### 4.1 Server SDKs (TypeScript, Java/Kotlin, .NET, Python, Go)

- OAuth token caching and refresh, typed models generated from [openapi.yaml](../api/openapi.yaml), and
  idempotency-key helpers.
- Fail-open defaults:
  - 150 ms timeout.
  - Retries with jitter only for idempotent calls.
  - Circuit breaker: open at ≥ 20% errors over 30 s.
- Webhook signature verification helper and event batching with a backpressure-safe buffer.
- OpenTelemetry instrumentation that propagates `traceparent`.

```ts
import { HealthSwapClient } from "@healthswap/server";

const hs = new HealthSwapClient({
  region: "in",
  clientId: process.env.HS_CLIENT_ID!,
  clientSecret: process.env.HS_CLIENT_SECRET!, // from a secret store, never committed
  timeoutMs: 150,
});

const evaluation = await hs.cartEvaluations.create({
  cartRef, userRef, market: "IN", storeId, currency: "INR", locale: "hi-IN",
  context: { placement: "CHECKOUT", channel: "ANDROID" },
  lines: cart.lines.map(l => ({ lineId: l.id, partnerSku: l.sku, quantity: l.qty,
                                unitPrice: { amount: l.price, currency: "INR" } })),
}); // on timeout or error the SDK resolves to { suggestions: [], degraded: true }
```

### 4.2 UI SDKs (Android, iOS, Web, React Native, Flutter)

| Component | Purpose |
| --- | --- |
| `SwapChip` | Inline "Healthier option" chip under a cart line |
| `SwapSheet` | Checkout bottom sheet with up to 3 swaps, one-tap swap + undo |
| `HealthBadge` | Grade / flags on product and listing pages |
| `WhySheet` | Nutrient comparison table, sources, methodology, disclosures |

- **Partner stays in control**: the SDK emits `onSwapAccepted(lineId, alternativeSku, qty)`. The partner app replaces
  the cart line with its own cart API, so the SDK never owns pricing or the cart.
- **Theming**: partner design tokens (color, typography, radius, spacing), dark mode, RTL readiness.
- **Footprint and privacy**:
  - Small binary (target < 300 KB); no third-party trackers.
  - No device identifiers; no cookies on web.
  - CSP-friendly web components.
- **Accessibility**: WCAG 2.2 AA, screen-reader labels, dynamic type, 44 pt touch targets.
- **Events**: impressions are counted only when ≥ 50% visible for ≥ 1 s. Events are batched and sent with the session
  token.

```kotlin
// Android (Jetpack Compose)
val hs = HealthSwap.init(
    context,
    HealthSwapConfig(
        sessionTokenProvider = { partnerApi.fetchHealthSwapToken(cartId) }, // minted by partner backend
        locale = "hi-IN",
        theme = HealthSwapTheme.fromTokens(myDesignTokens),
    )
)
val evaluation = hs.evaluateCart(cart.toHealthSwapCart(storeId = store.id))
SwapSheet(
    evaluation = evaluation,
    onSwapAccepted = { s -> cartViewModel.replaceLine(s.lineId, s.alternativeSku, s.quantity) },
    onDismiss = { /* SDK records DISMISS */ },
)
```

```swift
// iOS (SwiftUI)
let hs = HealthSwap(configuration: .init(
    sessionTokenProvider: { try await partnerAPI.healthSwapToken(cartID: cart.id) },
    locale: "en-US", theme: .partner(myTokens)))
let evaluation = try await hs.evaluateCart(cart.asHealthSwapCart(storeID: store.id))
SwapSheet(evaluation: evaluation) { swap in
    cartStore.replaceLine(swap.lineID, with: swap.alternativeSKU, quantity: swap.quantity)
}
```

```html
<!-- Web component -->
<healthswap-swap-sheet store-id="BLR-KOR-012" locale="en-IN"></healthswap-swap-sheet>
<script type="module">
  import { HealthSwap } from "https://cdn.healthswap.example/web/v1/healthswap.js";
  HealthSwap.init({ tokenProvider: () => fetch("/api/healthswap-token").then(r => r.json()).then(j => j.token) });
  document.querySelector("healthswap-swap-sheet")
    .addEventListener("swap-accepted", e => myCart.replaceLine(e.detail.lineId, e.detail.alternativeSku, e.detail.quantity));
</script>
```

### 4.3 UX guidelines

| Do | Don't |
| --- | --- |
| Swap in one tap, with a 5-second undo | Auto-replace items or pre-select swaps (basket sneaking) |
| Show both prices and the difference per unit | Hide or minimize the price difference |
| Give a short factual reason with its basis ("45% less sugar per 100 g") | Use guilt copy ("No, I don't care about my health") |
| Use a neutral decline label ("Keep original") | Show swaps on every cart view (nagging). Max 1 sheet per session and 3 suggestions per cart. |
| Label sponsored items clearly | Use false urgency or scarcity |
| Offer "Why am I seeing this?" with a methodology link and the fee disclosure | Call products "healthy" unless they meet the jurisdiction's definition |
| Let shoppers turn off nudges in settings | Block or delay checkout |

**Example localized copy:**

- en-IN: "45% less sugar per 100 g · ₹29 more"
- hi-IN: "प्रति 100 ग्राम 45% कम चीनी · ₹29 अधिक"
- en-US: "Better nutrition score · 11 g less added sugar per serving · +$1.20"

**Fee disclosure (in `WhySheet`)**: "HealthSwap may receive a fee from {Partner} when you choose a suggested item.
Suggestions are ranked on nutrition, not on fees."

## 5. Agent mode (MCP)

### 5.1 Tools

The HealthSwap MCP server uses Streamable HTTP transport and OAuth 2.1-based authorization per the MCP specification.
It exposes these tools:

```json
{
  "tools": [
    {
      "name": "evaluate_cart",
      "description": "Suggest healthier, in-stock, allergen-safe alternatives for cart items at a store. Returns suggestionIds that must be passed to record_swap_decision and to checkout metadata.",
      "inputSchema": {
        "type": "object",
        "required": ["market", "storeId", "lines"],
        "properties": {
          "market": { "type": "string", "pattern": "^[A-Z]{2}$" },
          "storeId": { "type": "string" },
          "locale": { "type": "string" },
          "lines": {
            "type": "array", "maxItems": 200,
            "items": {
              "type": "object", "required": ["lineId", "partnerSku", "quantity"],
              "properties": {
                "lineId": { "type": "string" }, "partnerSku": { "type": "string" },
                "gtin": { "type": "string" }, "quantity": { "type": "number", "exclusiveMinimum": 0 },
                "unitPrice": { "type": "string", "description": "Decimal string in store currency" }
              }
            }
          },
          "preferences": {
            "type": "object",
            "properties": {
              "dietType": { "enum": ["ANY", "VEGETARIAN", "EGGETARIAN", "VEGAN", "JAIN", "PESCATARIAN"] },
              "allergens": { "type": "array", "items": { "type": "string" } },
              "goals": { "type": "array", "items": { "type": "string" } }
            }
          }
        }
      },
      "annotations": { "readOnlyHint": true, "openWorldHint": false }
    },
    {
      "name": "get_product_health",
      "description": "Health profile (scores, flags, nutrient table, allergens) for a product by GTIN or partner SKU.",
      "inputSchema": { "type": "object", "required": ["market"], "properties": {
        "market": { "type": "string" }, "gtin": { "type": "string" }, "partnerSku": { "type": "string" } } },
      "annotations": { "readOnlyHint": true }
    },
    {
      "name": "find_healthier_alternatives",
      "description": "Healthier substitutes for one product, optionally limited to what a store has in stock.",
      "inputSchema": { "type": "object", "required": ["market"], "properties": {
        "market": { "type": "string" }, "gtin": { "type": "string" }, "partnerSku": { "type": "string" },
        "storeId": { "type": "string" }, "limit": { "type": "integer", "minimum": 1, "maximum": 10 } } },
      "annotations": { "readOnlyHint": true }
    },
    {
      "name": "explain_swap",
      "description": "Factual, localized comparison between two products (per 100 g/ml) with sources.",
      "inputSchema": { "type": "object", "required": ["market", "fromGtin", "toGtin"], "properties": {
        "market": { "type": "string" }, "fromGtin": { "type": "string" }, "toGtin": { "type": "string" },
        "locale": { "type": "string" } } },
      "annotations": { "readOnlyHint": true }
    },
    {
      "name": "record_swap_decision",
      "description": "Record that the shopper accepted or dismissed a suggestion. Required for attribution.",
      "inputSchema": { "type": "object", "required": ["suggestionId", "decision"], "properties": {
        "suggestionId": { "type": "string" }, "decision": { "enum": ["ACCEPTED", "DISMISSED"] },
        "alternativePartnerSku": { "type": "string" }, "quantity": { "type": "number" } } },
      "annotations": { "readOnlyHint": false, "destructiveHint": false, "idempotentHint": true }
    }
  ]
}
```

### 5.2 Agent safety

- HealthSwap tools never mutate carts or place orders. The merchant's own tools do that, with user confirmation.
- Tool outputs include `facts` and `disclosures` so the agent doesn't need to invent claims. The agent must not
  restate health claims beyond those facts; this is part of the partner agent policy.
- Tokens are tenant-scoped, rate-limited and audience-bound. User consent for preference use is passed explicitly.

### 5.3 Positioning with agentic-commerce protocols

- **Agentic Commerce Protocol** (OpenAI + Stripe, Apache-2.0, REST or MCP): the merchant stays merchant of record.
  HealthSwap enriches the merchant's product feed with health attributes and runs before checkout. `suggestionId`
  values travel in checkout or order metadata, where the merchant's implementation supports it.
- **Agent-to-agent**: publish an agent card (for example A2A) so retailer agents can delegate "make this basket
  healthier" sub-tasks to the HealthSwap agent.

## 6. Platform-by-platform strategy

> Public developer surfaces were checked in November 2026. Partner programs change, so re-verify before outreach.

### 6.1 India

| Platform | Operator | Public plugin / affiliate API? | Route | Notes |
| --- | --- | --- | --- | --- |
| Blinkit | Eternal Ltd | No | Mode A (BD partnership) | Dark-store inventory; back-of-pack images on product pages |
| Zepto | Zepto | No | Mode A/B | Strong fit for UI SDK speed |
| Swiggy Instamart | Swiggy | No | Mode A | |
| BigBasket / BB Now | Tata Digital | No | Mode A | Large private-label range: healthier private label is a win-win |
| Flipkart Minutes | Flipkart | Limited (marketplace programs) | Mode A | |
| Amazon (Fresh / quick commerce) | Amazon | Associates program (grocery coverage limited) | Mode A, or F for consumer app | |
| JioMart, DMart Ready | Reliance Retail, Avenue Supermarts | No | Mode A | |
| ONDC buyer apps | Various | Open protocol | **Mode E** | See §8 |
| D2C food brands and regional grocers | — | Shopify / WooCommerce / other app stores | **Mode D** | Self-serve |

**India pitch angle**:

- Regulatory tailwind: FSSAI's push for front-of-pack nutrition labeling.
- Health-conscious urban cohorts.
- Private-label upsell.
- Catalog enrichment: structured nutrition data for search filters ("high protein", "no added sugar") and compliance
  checks on their listings.

### 6.2 United States

| Platform | Public developer surface | Route |
| --- | --- | --- |
| Instacart | **Instacart Developer Platform**: shopping-list and recipe pages, nearby retailers, brand and health filters, AI-agent connectivity. **Instacart Connect** is for retailers. | Mode F now (consumer app hands healthier lists to Instacart); Mode A/C enterprise later |
| Kroger | **Public APIs**: Products, Locations, Identity, Cart (add an item to an authenticated customer's cart via OAuth). Partner APIs include Catalog V2 and Partner Cart. | Mode F now (consumer app pushes healthier items to the shopper's Kroger cart with consent); partner APIs later |
| Walmart | **Walmart Affiliate Marketing API**: product lookup, search, taxonomy, recommendations; referral fees | Mode F now; Mode A/C enterprise later |
| Amazon (Fresh, Whole Foods) | Associates program | Mode F |
| Target, Albertsons, DoorDash, Uber Eats, Gopuff | Mostly partner-only | Mode A via BD |
| Independent grocers | Grocery e-commerce SaaS (for example Instacart Storefront, Mercatus) and Shopify | Mode D, or a SaaS-vendor partnership |

**US pitch angle**:

- FDA "healthy" rule and FOP proposal momentum.
- Retailer health programs and private label.
- Agentic shopping readiness through MCP.

### 6.3 Rest of world (phase 2+)

- UK and EU: Tesco, Sainsbury's, Ocado, Carrefour, Rewe and others (Mode A), Shopify grocers (Mode D). Nutri-Score
  where adopted; UK HFSS rules.
- Canada (Loblaw, Sobeys), Australia (Woolworths, Coles): Mode A. The "High in" symbol (CA) and Health Star Rating
  (AU/NZ) work as consumer-facing grades.

## 7. Commerce-platform plugins (mode D)

### 7.1 Shopify app (reference design)

| Concern | Implementation |
| --- | --- |
| Storefront UI | Theme app extension: an app block on the cart page and an app embed for cart drawers (Online Store 2.0). Swaps use the Ajax Cart API (`/cart/change.js`, `/cart/add.js`). |
| Checkout UI (optional) | Checkout UI extensions for in-checkout nudges on plans that support them; thank-you page education |
| Catalog and nutrition | Admin GraphQL API: products and variants. Merchant-entered or OCR-captured nutrition is stored in product metafields and synced to HealthSwap. |
| Orders and attribution | Webhooks: `orders/create`, `orders/fulfilled`, `refunds/create`, `app/uninstalled`. Line-item properties carry `suggestionId`. |
| Billing (10% of fair premium) | Shopify Billing API usage-based subscription with a capped amount. HealthSwap posts usage records daily for finalized fees. |
| Privacy | Mandatory compliance webhooks (`customers/data_request`, `customers/redact`, `shop/redact`). No customer PII stored. |
| Performance | Async script loading; Lighthouse impact within app-store limits |

### 7.2 Other platforms

| Platform | Packaging |
| --- | --- |
| BigCommerce | Marketplace app + Script Manager widget + Storefront Cart API |
| Adobe Commerce / Magento | Marketplace extension (module + cart block) |
| Salesforce Commerce Cloud | Cartridge (SFRA) on AppExchange |
| commercetools | Connect application |
| SAP Commerce Cloud | Extension |
| VTEX | VTEX IO app |
| WooCommerce | WordPress plugin using cart hooks |

Billing on these platforms uses platform billing where available, or HealthSwap invoicing (Stripe / Razorpay).

## 8. ONDC (India open network)

ONDC retail is built on the Beckn protocol. Buyer apps (BAPs) and seller apps (BPPs) interoperate through standard
APIs: `search` / `on_search` → `select` / `on_select` (quote) → `init` / `on_init` → `confirm` / `on_confirm` →
`status`, `update`, `cancel` and their `on_*` callbacks.

| Hook | HealthSwap action |
| --- | --- |
| `on_search` (catalog received) | Match items, compute health badges for listing and product pages |
| Before `select` (cart built) | `POST /v1/cart-evaluations`. Alternatives must come from the **same provider and location**, because ONDC orders are per seller. |
| `on_confirm` | `POST /v1/orders` with `suggestionId`s |
| `on_status` delivered, `on_update` returns or cancellations | Order status updates and adjustments for attribution |

**Two business options:**

1. **Technology provider to existing buyer apps**: contract with the buyer app (Mode A economics).
2. **HealthSwap's own health-first buyer app**: register as a network participant, pass ONDC pre-production
   certification, and implement issue and grievance management and reconciliation and settlement. Revenue comes from
   the seller-declared buyer-app finder fee (retail contracts carry fields such as
   `@ondc/org/buyer_app_finder_fee_type` / `_amount`; verify against the current API contract version) plus
   HealthSwap's own value-added services.

ONDC retail catalogs carry statutory pre-packaged food information (nutrition and additive text, FSSAI licence
numbers). This is a useful data source for the knowledge base.

## 9. Own consumer app (mode F) — proof of value without partnerships

- **India**:
  - Barcode scanning in stores (EAN-13 with the 890 prefix plus imports).
  - Cart import from a shared screenshot (OCR) or from manual list entry.
  - Suggestions use the knowledge base; availability is not guaranteed.
  - Deep links to partner search pages only where their terms allow.
- **US**: build a healthier list, then hand off to the Instacart Developer Platform (shopping-list page) or the
  Kroger Cart API (add to the shopper's cart after OAuth consent), or link through the Walmart affiliate program.
- Monetization is program commissions or subscription, not the 10% premium fee. The app proves consumer demand,
  builds the brand and generates acceptance data for B2B pitches.
- In this mode HealthSwap is the controller / Data Fiduciary. That means a full privacy program, consent UX, and
  stronger care for children's data.

## 10. Browser extension (mode G)

- Desktop web only. Content scripts read the visible cart and product pages. Product pages often expose GTINs in
  JSON-LD. The extension calls `GET /v1/products/lookup` and shows an overlay with healthier options.
- **Constraints**:
  - Site terms may prohibit automated access.
  - DOM changes break it.
  - Chrome Web Store policies require single purpose, and require affiliate links to be disclosed and inserted only
    on user action with direct user benefit.
- Use it for research, SEO and marketing. Never make it a core revenue channel, and never manipulate carts.

## 11. Commercial models and contract checklist

| Model | Who pays | How the 10% applies | When to use |
| --- | --- | --- | --- |
| **M1 Platform-paid performance fee** (default) | Partner | 10% of fair premium on delivered, non-returned swaps; monthly statement | Enterprise partners (modes A–C, E-TSP) |
| M2 Customer-paid disclosed fee | Shopper, via partner as merchant of record | Line item equal to the computed fee, shown before payment, refundable | Only if the partner insists; higher regulatory and UX risk |
| M3 SaaS + performance | Partner | Platform fee (catalog enrichment, dashboards) + lower performance rate | Partners wanting predictable cost |
| M4 App-store usage billing | Merchant | Exact 10% of fair premium through platform usage charges, capped | Mode D |
| M5 Affiliate / program commissions | Retailer program | Program-defined; 10% of premium tracked as an internal KPI only | Mode F/G |
| Sponsored "healthy spotlight" (optional) | Brands | Fixed CPM / CPA; labeled; eligible items only | After neutrality governance is mature |

**Contract checklist:**

- SLAs: availability, latency, support tiers, status page, credits.
- Fee terms: rate, fair-premium rule version, caps and minimums, return windows, statement cadence, dispute window,
  payment terms, currency, taxes (GST invoices and TDS in India).
- DPA: processor terms, residency, sub-processors (LLM providers receive non-personal data only), breach notification
  timelines, deletion on termination, audit rights.
- Catalog enrichment licence (doc 02 §5). No cross-partner use of partner-identifiable data. Aggregated, anonymized
  benchmarks only with explicit consent.
- IP: the partner owns its data and UX; HealthSwap owns the knowledge base, models and methodology. Logo and trademark
  usage rules.
- Liability: methodology adherence warranties, partner warranty on its catalog and price data, allergen disclaimer
  language, indemnities, caps, insurance.
- Neutrality commitments (fee-neutral ranking), sponsored-content policy, dark-pattern compliance, accessibility.
- Exit: data return, transition assistance, kill switch.

## 12. Legal and policy checklist by channel

| Channel | India | US | EU/UK |
| --- | --- | --- | --- |
| All | Consumer Protection (E-Commerce) Rules 2020; CCPA Guidelines on Dark Patterns 2023; FSSAI Advertising & Claims Regs 2018; DPDP Act 2023 + Rules 2025 | FTC Act §5 (unfair or deceptive practices); FTC Endorsement Guides (material connection disclosure); state privacy and consumer-health-data laws; FDA labeling definitions (for example "healthy") | GDPR; Unfair Commercial Practices Directive; Reg. 1924/2006 claims; EU AI Act transparency for AI-generated text; UK DMCC Act consumer rules |
| UI SDK in partner apps | Partner app policies; children's data (DPDP §9) | COPPA if any child-directed context | Children's codes (for example the UK Age Appropriate Design Code) |
| Commerce-platform apps | Platform app-store rules | Same | Same |
| Own consumer app | Data Fiduciary duties, consent manager readiness | CCPA/CPRA, Washington My Health My Data Act | GDPR controller duties |
| Browser extension | Site terms | Chrome Web Store policies, site terms | Same |

## 13. Pilot playbook and success criteria (hypotheses to validate)

| Stage | Gate to pass |
| --- | --- |
| Sandbox | Contract tests green; webhook signatures verified |
| Shadow | Coverage ≥ 85% of cart lines in pilot categories; p99 ≤ 150 ms; nutritionist review of 500 sampled swaps ≥ 95% agreement; zero allergen or diet violations |
| Dogfood | No P1 bugs; UX review passes the dark-pattern checklist |
| A/B (treatment vs holdout) | Positive AOV and IHGMV; checkout conversion not significantly below control (guardrail: −0.2 pp); undo rate < 10%; complaints < 0.05% of impressions |
| Ramp | SLOs met for 4 consecutive weeks; statement reconciled at 100% |

Report alongside commercials:

- Health impact: sugar (g) and sodium (mg) avoided per 1,000 orders, mean ΔHSI per accepted swap.
- Neutrality metrics.

## 14. Value proposition for partners (one-liner per stakeholder)

| Stakeholder | Value |
| --- | --- |
| CEO / strategy | Health-first differentiation, regulatory readiness, measurable public-health impact |
| Category / merchandising | Incremental GMV (the partner keeps ~90% of the premium plus margin), private-label upsell, structured nutrition for filters |
| Product / design | Drop-in, themeable, fail-open components, or a clean API for native UI |
| Engineering | One API, < 150 ms p99, no ML or regulatory upkeep |
| Legal / compliance | Published methodology, claim-safe copy, dark-pattern-safe UX, DPA-ready |
| Finance | Pay only on delivered, non-returned swaps; transparent statements; caps |
