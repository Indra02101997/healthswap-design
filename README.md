# HealthSwap — Healthier-Swap Intelligence for Grocery & Quick-Commerce Apps

> Working name · Design package v1.0 · November 2026 · Status: **Draft for review**

HealthSwap is a B2B decisioning service (Partner API + SDKs + AI-agent tools) that plugs into grocery and
quick-commerce platforms such as Blinkit, Zepto, Swiggy Instamart, BigBasket, Walmart, Instacart, Kroger,
Shopify-based grocers, ONDC buyer apps and AI shopping assistants. When a shopper builds a cart, HealthSwap
checks the nutrition and ingredient composition of every item. It then suggests **healthier, in-stock,
allergen-safe alternatives from the same store**. If the shopper accepts a swap to a more expensive option,
HealthSwap earns **10% of the extra amount paid** (the *fair premium*, defined in
[docs/01-hld.md §9](docs/01-hld.md#9-attribution--fee-model)). Optionally, every recommendation is labeled
**"Recommended by HealthSwap"** and the partner pays a small fixed fee each time a shopper adds a labeled recommended
product to the cart (the *recommended-add fee*, rule RA-1, specified in
[docs/06](docs/06-recommended-label-and-add-fee.md)).

## Package contents

| # | Artifact | What it covers |
| --- | --- | --- |
| 1 | [docs/01-hld.md](docs/01-hld.md) | Requirements, architecture, components, end-to-end sequence flows, attribution & fee model, data architecture, security, privacy, reliability, capacity, deployment |
| 2 | [docs/02-food-knowledge-base.md](docs/02-food-knowledge-base.md) | How to build the multi-market food composition DB: sources (FSSAI, FDA, USDA, GS1, Open Food Facts, ...), licensing, ingestion, normalization, entity resolution, QA, label OCR |
| 3 | [docs/03-health-scoring-recommendation-llm.md](docs/03-health-scoring-recommendation-llm.md) | Health scoring per jurisdiction, "healthier" eligibility rules, candidate generation, substitutability, ranking, where and how LLMs are used, prompts, guardrails, evaluation |
| 4 | [docs/04-partner-integration-playbook.md](docs/04-partner-integration-playbook.md) | How to ship as a 3rd-party plugin: integration modes, SDKs, MCP agent mode, platform-by-platform strategy (India / US / global), ONDC, Shopify, affiliate routes, UX & legal guardrails |
| 5 | [docs/05-implementation-roadmap.md](docs/05-implementation-roadmap.md) | Step-by-step build plan, phases and exit criteria, team, unit economics, KPIs, risk register |
| 6 | [api/openapi.yaml](api/openapi.yaml) | OpenAPI 3.1 contract for the Partner API and webhooks |
| 7 | [db/schema.sql](db/schema.sql) | PostgreSQL reference schema: knowledge base, regulatory packs, scoring, swap graph, tenant data, attribution, double-entry ledger |
| 8 | [docs/06-recommended-label-and-add-fee.md](docs/06-recommended-label-and-add-fee.md) | RA-1 specification: the "Recommended by HealthSwap" label and disclosures, chargeable adds, evidence checks, charge lifecycle, configuration, API, ledger, disputes, worked example IN-1, test coverage |
| 9 | [docs/07-reference-implementation.md](docs/07-reference-implementation.md) | Python reference implementation: stack, layout, architecture mapped to the HLD, request pipeline, row-level security, workers, configuration, ML and LLM tooling, SDKs, CLI, tests, production gaps, security notes, limitations |
| 10 | [docs/08-manual-test-guide.md](docs/08-manual-test-guide.md) | Manual test guide: setup, demo shop, smoke test, API walkthrough, negative tests, MCP, pytest, troubleshooting, expected-results checklist |
| 11 | [docs/09-client-app-integration.md](docs/09-client-app-integration.md) | Client app integration guide: integration patterns (web SDK, native apps, server-side), session minting, rendering and event rules, orders, webhooks, reconciliation, local testing, go-live checklist; demo recording in [docs/media](docs/media) |
| 12 | [docs/10-codebase-guide.md](docs/10-codebase-guide.md) | Codebase guide: which folder holds which code, the code flow of each component and how the components work together; narrated system-design video in [docs/media](docs/media) |
| 13 | [src/healthswap](src/healthswap) | Reference implementation (Python 3.11+): domain rules, platform services, FastAPI application, MCP server, `hs` CLI |
| 14 | [tests](tests) | pytest suites: unit tests and integration tests on an in-memory PostgreSQL (PGlite) |
| 15 | [sdk/python](sdk/python) | Python server SDK `healthswap-client`: fail-open cart evaluation, retries, circuit breaker, webhook verification |
| 16 | [sdk/web](sdk/web) | Browser SDK: one ES module and a stylesheet that render labeled suggestions and report events |
| 17 | [web/demo](web/demo) | QuickBasket demo shop, served at `/demo` by the local stack |
| 18 | [ml](ml) | ONNX model training (`train.py`) and Azure OpenAI fine-tuning of the explanation writer (`finetune/`) |
| 19 | [scripts](scripts) | `run_local.py` (local stack) and `smoke_test.py` (end-to-end check) |
| 20 | [config](config) | Settings files (`settings.toml`, `settings.development.toml`) |
| 21 | [data](data) | Market packs, seed knowledge base, demo partners and connector fixtures |
| 22 | [tools/devdb](tools/devdb) | PGlite dev database: PostgreSQL compiled to WebAssembly, run by Node.js, for local runs and tests |
| 23 | [tools/demo-recorder](tools/demo-recorder) | Playwright scripts that record the client integration demo and render the narrated system-design video into `docs/media` |
| 24 | [archive/csharp](archive/csharp) | The earlier C# implementation: archived and unmaintained, superseded by the Python reference implementation |

Suggested reading order: this README → 01 → 03 → 02 → 04 → 05 → 06. Engineers should treat 6 and 7 as the contract.
To run the reference implementation, read 07 and follow 08. To integrate a client app, read 09; for a map of the code,
read 10.

## Quick start (reference implementation)

Prerequisites: Python 3.11+ and Node.js 20+ (for the PGlite dev database), or Python 3.11+ and an existing
PostgreSQL 16+ with pgvector, pg_trgm and pgcrypto instead of Node.js.

1. Create and activate a virtual environment in the repository root. Windows (PowerShell): `py -3 -m venv .venv` and
   `.venv\Scripts\Activate.ps1`. macOS and Linux: `python3 -m venv .venv` and `source .venv/bin/activate`.
2. Install the packages and the dev database, then start the stack:

   ```shell
   python -m pip install -e ".[dev]"
   python -m pip install -e sdk/python
   npm ci --prefix tools/devdb
   python scripts/run_local.py
   ```

3. Open <http://127.0.0.1:8080/demo> for the demo shop. In a second terminal (with the virtual environment activated),
   run `python scripts/smoke_test.py --base-url http://127.0.0.1:8080`; on a fresh database it ends with
   `ALL CHECKS PASSED`.
4. Stop the stack with Ctrl+C. `python scripts/run_local.py --reset` starts again from a fresh database.

With an existing PostgreSQL, apply [db/schema.sql](db/schema.sql) to an empty database and start with
`python scripts/run_local.py --database-url "postgresql://user:password@host:5432/healthswap"`; Node.js is then not
needed. Architecture and configuration: [docs/07](docs/07-reference-implementation.md). Step-by-step tests,
expected results and troubleshooting: [docs/08](docs/08-manual-test-guide.md).

## Executive summary

1. **Core loop**: cart → resolve every line to a canonical product → look up a precomputed *swap graph* → filter by
   store availability, allergens, diet, price and pack size → rank → return up to 3 suggestions with factual,
   localized explanations. Target server latency is p99 < 150 ms. If HealthSwap is slow or down, the partner
   shows the cart without suggestions (fail-open).
2. **"Healthier" is deterministic**. It comes from published, versioned, jurisdiction-specific nutrient-profiling
   models (Nutri-Score 2023, Health Star Rating / FSSAI INR draft, FDA FOP and "healthy" criteria, UK NPM, NOVA)
   plus an evidence-based additive tiering. LLMs help with matching, label extraction, substitutability judgments
   and explanations. **They never decide healthiness, allergen safety or fees.**
3. **The knowledge base is built, not downloaded.** Regulators (FSSAI, FDA) define what goes on labels but publish
   no SKU-level composition database. The DB is assembled from Open Food Facts (ODbL), USDA FoodData Central
   (CC0), licensed GS1 data pools (GS1 India DataKart, GDSN), partner catalogs, brand submissions and label OCR.
   Every field keeps its provenance and licence.
4. **Monetization**: by default the platform pays 10% of the fair premium, accrued on delivery, finalized after the
   return window, and invoiced monthly from a double-entry ledger. A customer-paid fee is possible but not
   recommended. Partners can also contract the recommended-add fee RA-1: a fixed amount (INR 1.00 in the demo) per
   add of a labeled recommendation, voided by an undo, finalized after a 10-minute settlement window only with
   evidence (served suggestion, recommended SKU, impression), additive to FP-1 and billed on the same statement.
5. **Neutrality**: neither fee is a ranking feature. Cheaper healthier swaps are always eligible, and the fees are
   disclosed (the RA-1 label and disclosure never state the amount). This protects trust and keeps the design within
   dark-pattern and endorsement rules (India CCPA 2023 guidelines, FTC).
6. **Plugin reality**: Indian quick-commerce apps have no public plugin or affiliate APIs, so in-app presence needs
   a B2B partnership. ONDC is the open-network alternative. In the US you can start without partnerships through
   the Instacart Developer Platform, Kroger public APIs and the Walmart Affiliate API. Shopify and other commerce
   platforms give a self-serve plugin channel with native usage billing. MCP tools make HealthSwap callable by AI
   shopping agents.
7. **Privacy by design**: partners send pseudonymous user references only. Personal and tenant data stays in its
   geography (India, US, EU). The knowledge base is global and non-personal.

## End-to-end flow

```mermaid
flowchart LR
    A["Shopper adds items<br/>to cart"] --> B["Partner backend calls<br/>POST /v1/cart-evaluations"]
    B --> C["Resolve lines to canonical<br/>products + health profiles"]
    C --> D["Swap graph + store availability<br/>+ allergen / diet / price rules"]
    D --> E["Ranked suggestions with<br/>factual explanations"]
    E --> F{"Shopper accepts?"}
    F -- "No" --> G["Dismiss event<br/>no fee, cool-down"]
    F -- "Yes" --> H["Partner swaps the cart line<br/>and sends ACCEPT event"]
    H --> I["Order placed with suggestionId"]
    I --> J["Delivered: fee accrues<br/>10% of fair premium"]
    J --> K["Return window passes: FINAL"]
    K --> L["Monthly statement: partner pays"]
```

## Key design decisions

| ID | Decision | Rationale | Rejected alternative |
| --- | --- | --- | --- |
| ADR-01 | Healthiness comes from deterministic, published, versioned rules. LLMs only assist. | Auditability, regulatory defensibility, no hallucinated nutrition | LLM-as-judge at request time |
| ADR-02 | Precompute a per-market swap graph offline. At request time, only filter and rank. | p99 < 150 ms at thousands of RPS | Online vector search + LLM per request |
| ADR-03 | Fail-open: never block cart or checkout | Protects partner conversion | Synchronous gating |
| ADR-04 | Default monetization is platform-paid, 10% of fair premium | Conversion, avoids drip-pricing and dark-pattern risk | Customer surcharge by default |
| ADR-05 | Fair premium = min(pack-price delta, size-normalized delta) | Earn only on the health premium, never on extra volume | Raw price delta |
| ADR-06 | Fee-neutral ranking. Cheaper healthier options stay eligible. | Trust, consumer-protection scrutiny | Revenue-optimized ranking |
| ADR-07 | Server-first integration, optional UI SDKs, MCP tools for agents | Large apps own their UX. Agents are a growing channel. | SDK-only |
| ADR-08 | Global non-personal knowledge base; tenant and personal data pinned to its geography | DPDP / GDPR residency, latency | Single global database |
| ADR-09 | Field-level provenance and licence tags | ODbL share-alike compliance, audits, dispute resolution | Merged blobs without lineage |
| ADR-10 | PostgreSQL + Redis + Kafka-compatible bus + lakehouse, cloud-agnostic, with an Azure reference mapping | Mature, portable, broad skills market | Single-vendor proprietary stack |
| ADR-11 | Optional recommended-add fee RA-1: a fixed, contracted amount per add of a labeled HealthSwap recommendation, additive to FP-1, settled after an undo grace and evidence checks | Pays for the recommendation itself, including cheaper swaps that earn no FP-1; per event and reconcilable by `eventId`; disclosed by a label that never states the amount | Earning only through FP-1 |
| ADR-12 | Python reference implementation (FastAPI, psycopg 3, PGlite dev database) | Cross-platform development on Windows, macOS and Linux with only Python and Node.js; same language as the ML tooling | The C# implementation, now archived in `archive/csharp` |

## What the research changed

1. **No regulator publishes a product-level composition database.** FSSAI (Labelling & Display Regulations 2020,
   Food Products Standards & Food Additives Regulations 2011, Advertising & Claims Regulations 2018) and FDA
   (21 CFR 101, additive inventories, the "healthy" claim rule) define rules and permitted substances. SKU data
   has to come from labels.
2. **Open Food Facts** is ODbL (share-alike). Its API is rate-limited to 15 product reads/min/IP and 10
   searches/min/IP, so use the nightly dumps and 14-day daily deltas. Never call it at request time.
3. **USDA FoodData Central** is public domain (CC0). The API allows 1,000 requests/hour/IP. The latest Branded
   Foods bulk release is October 2026 (about 3 GB of JSON).
4. **US rules are changing.** The FDA "healthy" final rule (December 2024) sets food-group requirements plus limits
   on added sugars, sodium and saturated fat per reference amount. The FDA front-of-package "Nutrition Info box"
   was proposed in January 2025 (comment period ended July 2025). The scoring engine therefore has to be
   pluggable and versioned.
5. **India front-of-pack labelling**: the FSSAI "Indian Nutrition Rating" was released as a draft. Treat it as a
   configurable model and track FSSAI notifications.
6. **Indian quick-commerce apps** expose no public plugin or affiliate APIs, and a third party can't legitimately
   overlay mobile apps. In-app presence needs a partnership.
7. **US open developer surfaces exist**:
   - Instacart Developer Platform: shopping-list and recipe pages, nearby retailers, brand and health filters, AI-agent connectivity.
   - Kroger public APIs: Products, Locations, Identity, and Cart add-item for an authenticated customer.
   - Walmart Affiliate Marketing API: product lookup and referral fees.
8. **Agentic commerce** is standardizing: the Agentic Commerce Protocol (OpenAI + Stripe, Apache-2.0, REST or MCP)
   and MCP itself. HealthSwap should ship MCP tools.
9. **Conflict of interest**: earning more when shoppers pay more requires a neutrality policy, disclosure and
   cheaper-swap visibility.

## Assumptions

- Launch markets are India and the US. EU/UK, Canada and Australia/NZ follow.
- Partners are the merchant of record. HealthSwap never touches payment instruments.
- Partners send pseudonymous `userRef` values only: no names, phone numbers, emails or addresses.
- Scope starts with packaged foods and beverages. Fresh produce uses generic composition tables.
- **Excluded:** alcohol, tobacco, supplements and nutraceuticals, food for special medical purposes, and infant foods.
  India's IMS Act and the WHO Code restrict the promotion of infant foods.
- Fee rate, caps and return windows are configurable per contract. The default is 10% of fair premium.
- RA-1 is optional per contract and paid by the partner, never by the shopper. Its amount, basis (per add or per unit),
  caps and windows are set per partner in the partner's currency.
- Partners (or the HealthSwap SDKs) report impressions, accepts, adds and undos truthfully; RA-1 evidence depends on
  these events, and disputes correct errors.

## Open questions for stakeholders

1. Who pays the 10%: the platform (recommended) or the shopper as a disclosed line item?
2. Does the fee apply to fresh and loose items, or only to packaged goods?
3. Are brand-sponsored placements allowed (always labeled, and only among eligible healthier options)?
4. Which pilot partners and markets come first? Is a consumer app in scope?
5. What budget is available for licensed data (GS1 data pools, national composition tables)?
6. Do partners require in-country processing for LLM calls? Only non-personal product data is ever sent.
7. Is the label wording ("Recommended by HealthSwap") and the fee disclosure, in every language, acceptable per
   market (India CCPA guidelines, FTC Endorsement Guides, EU rules)? Legal review is needed before launch.
8. What is the GST and sales-tax treatment of RA-1 (service classification, place of supply), and may it be invoiced
   on the same statement as FP-1, as the reference implementation does?
9. May partners pass RA-1 on to shoppers, for example in a service fee? The design assumes they may not.
10. Which RA-1 amount, basis and caps per market, and should ACCEPT and ADD_TO_CART both be chargeable for every
    partner?

## References (verified November 2026)

- Open Food Facts API docs: <https://openfoodfacts.github.io/openfoodfacts-server/api/> · Data & licence: <https://world.openfoodfacts.org/data>
- USDA FoodData Central API guide: <https://fdc.nal.usda.gov/api-guide> · Downloads: <https://fdc.nal.usda.gov/download-datasets>
- FDA "healthy" claim: <https://www.fda.gov/food/nutrition-food-labeling-and-critical-foods/use-term-healthy-food-labeling>
- FDA front-of-package labeling: <https://www.fda.gov/food/nutrition-food-labeling-and-critical-foods/front-package-nutrition-labeling>
- FDA Substances Added to Food inventory: <https://www.fda.gov/food/food-additives-petitions/substances-added-food-formerly-eafus>
- openFDA food enforcement (recalls): <https://open.fda.gov/apis/food/enforcement/>
- FSSAI regulations index: <https://www.fssai.gov.in/cms/food-safety-and-standards-regulations.php>
- GS1 India DataKart: <https://www.gs1india.org/services/datakart>
- Instacart Developer Platform: <https://docs.instacart.com/developer_platform_api/>
- Kroger developer API catalog: <https://developer.kroger.com/api-products>
- Walmart Affiliate Marketing API: <https://walmart.io/docs/affiliates/v1/introduction>
- Agentic Commerce Protocol: <https://www.agenticcommerce.dev/>
- ONDC retail specifications: <https://github.com/ONDC-Official/ONDC-RET-Specifications>
- Model Context Protocol: <https://modelcontextprotocol.io>

> **Disclaimer**: this is engineering design guidance, not legal, medical, nutritional or tax advice. Food
> regulations change frequently. Verify every regulatory threshold and status against the current official text
> with qualified counsel and nutrition scientists before production use.
