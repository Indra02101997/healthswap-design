# 07 — Reference Implementation (Python)

| | |
| --- | --- |
| Document | HealthSwap reference implementation: architecture, configuration and operations |
| Version | 1.0 (draft) |
| Related | [01 HLD](01-hld.md) · [03 Scoring, ranking & LLM](03-health-scoring-recommendation-llm.md) · [06 RA-1](06-recommended-label-and-add-fee.md) · [08 Manual test guide](08-manual-test-guide.md) · [OpenAPI](../api/openapi.yaml) · [Schema](../db/schema.sql) |

---

## 1. Purpose and status

The reference implementation is a runnable version of this design: the Partner API v1 of
[openapi.yaml](../api/openapi.yaml) on the PostgreSQL schema of [schema.sql](../db/schema.sql), the knowledge-base
pipeline, scoring, swap graphs, the cart-evaluation hot path, suggestion events, the RA-1 recommended-add fee
([06](06-recommended-label-and-add-fee.md)), FP-1 attribution, the double-entry ledger with statements and disputes, signed
webhooks, MCP agent tools, a Python server SDK, a browser SDK with a demo shop, and the ML and LLM tooling.

It is meant for development, partner demos, contract testing and as an executable specification. It runs on Windows,
macOS and Linux with Python and Node.js only. It is not production-hardened: section 14 lists what production needs
instead, section 16 the known limitations. The earlier C# implementation in `archive/csharp` is archived and unmaintained.

To run it, follow [08 §3](08-manual-test-guide.md#3-setup-from-a-clean-clone).

## 2. Technology stack

| Concern | Choice | Why |
| --- | --- | --- |
| Language | Python 3.11+ (verified with 3.13) | One cross-platform toolchain for the service, the ML tooling and the tests; no SDK installs beyond Python |
| HTTP | FastAPI on Starlette, served by uvicorn | ASGI middleware pipeline and routing; FastAPI's generated docs are disabled because the contract is `api/openapi.yaml` |
| Database | PostgreSQL 16+ with pgvector, pg_trgm and pgcrypto, through psycopg 3 and psycopg-pool | Row-level security with transaction-local settings, JSONB, trigram indexes for product names, a pgvector column for product embeddings (HNSW search in production; the reference implementation ranks swap candidates in memory), `gen_random_uuid()` |
| Local database | PGlite (PostgreSQL compiled to WebAssembly) behind a Postgres wire-protocol socket, in `tools/devdb` (Node.js 20+) | The same schema and SQL as production without Docker or a local PostgreSQL install |
| Contract validation | jsonschema (Draft 2020-12) with PyYAML | Requests (and, in the tests, responses) are validated against the OpenAPI document itself |
| Security primitives | PyJWT and cryptography | ES256 tokens and JWKS, AES-256-GCM, HKDF, HMAC-SHA256, PBKDF2 |
| Outbound HTTP | httpx | Webhooks (with DNS pinning), Azure OpenAI, the SDK |
| Cache | In-process memory store; Redis optional (`.[redis]` extra) | Hot path reads only the cache; Redis shares it between instances |
| Models | numpy and onnxruntime | In-process inference of the ranker, substitutability and entity-resolution models |
| Agent tools | MCP Python SDK 2.x | Stateless Streamable HTTP MCP server |
| ML training (optional) | scikit-learn, skl2onnx, onnx (`.[ml]` extra) | Train and export the ONNX models |
| Development | pytest, hypothesis, ruff (`.[dev]` extra) | Tests, property tests, lint |
| Browser SDK | One ES module and one CSS file, no dependencies, no build step | Drop-in for partner web shops |

## 3. Repository layout

| Path | Contents |
| --- | --- |
| `api/openapi.yaml` | The Partner API contract; loaded at startup for request validation and served at `/openapi.yaml` |
| `db/schema.sql` | PostgreSQL schema: knowledge base, regulatory packs, scoring, swap graph, tenant data, transactions, ledger, roles, row-level security |
| `config/` | `settings.toml` (all environments) and `settings.development.toml` |
| `data/packs/` | Market packs (`IN.json`, `US.json`), additive tiers, scoring models |
| `data/seed/` | Seed knowledge base (`kb/`) and demo tenants, catalogs, inventory and development client secrets (`demo/`) |
| `data/fixtures/` | Sample Open Food Facts, USDA FDC and openFDA files for the connectors |
| `src/healthswap/domain/` | Pure rules without I/O: primitives, knowledge parsing and quality, scoring models and health profiles, recommendation rules, ranking, explanations, labels, fees (FP-1, RA-1), ledger postings, partner configuration |
| `src/healthswap/platform/` | Everything with I/O: database, caches, event bus and lake, LLM gateway, model registry, knowledge pipeline, services, seeding, settings, dependency container |
| `src/healthswap/api/` | FastAPI application: middleware, authentication, contract validation, problems, rate limits, partner, admin and demo routes, MCP tools |
| `src/healthswap/cli.py` | The `hs` operations CLI |
| `src/healthswap/devdb.py` | Starts and stops the PGlite dev database |
| `tests/` | `domain`, `platform`, `api`, `ml` and `sdk` test suites |
| `sdk/python/` | `healthswap-client`, the server-side Python SDK |
| `sdk/web/` | `healthswap-web.js` and `healthswap-web.css`, the browser SDK |
| `web/demo/` | The QuickBasket demo shop served at `/demo` |
| `ml/` | `train.py` (ONNX models), `finetune/` (Azure OpenAI fine-tuning), `data/` (exported training data, not versioned) |
| `scripts/` | `run_local.py` (local stack) and `smoke_test.py` (end-to-end check) |
| `tools/devdb/` | PGlite server (`server.mjs`) with `package.json` and lock file |
| `archive/csharp/` | The superseded C# implementation (archived, unmaintained) |
| `.data/` | Runtime state, not versioned: dev database, logs, signing and data-protection keys, feed uploads, event lake |

## 4. Package architecture

Three layers with one dependency direction: `api` → `platform` → `domain`. The domain layer is deterministic and has no
I/O, so every business rule (scoring, eligibility, ranking, fair premium, RA-1 decisions, postings) is unit-tested
without a database. `platform/container.py` is the composition root: it builds the services lazily from the settings and
starts the background workers.

| HLD component ([01 §6.1](01-hld.md#61-component-catalog)) | Implementation |
| --- | --- |
| Edge: OAuth 2.0, scopes, quotas, idempotency keys, request ids | `api/http.py` (middleware, route pipeline), `api/auth.py`, `IdempotencyService` in `platform/services/infrastructure.py` |
| SDK edge | SDK session tokens in `api/auth.py` (`POST /v1/sdk/sessions`), session binding in the services |
| MCP / agent gateway | `api/mcp_tools.py` |
| Cart Evaluation Orchestrator, Product Resolution, Candidate & Constraint, Explanation | `platform/services/evaluation.py` with `domain/recommendation/` (`rules.py`, `ranking.py`, `explanations.py`, `labels.py`) and the hot cache `platform/hot_cache.py` |
| Ranker | `domain/recommendation/ranking.py` with ONNX models from `platform/ml/registry.py` |
| Preference & Consent, privacy erasure | `PreferenceService`, `PrivacyService` in `platform/services/catalog.py` |
| Catalog ingestion & matching, inventory mirror | `CatalogService`, `FeedService`, `InventoryService` in `platform/services/catalog.py` |
| Source connectors, normalize, resolve, QA, product master | `platform/knowledge/` (`connectors.py`, `ingestor.py`, `kb_reader.py`, `reference_data.py`) with `domain/knowledge/` |
| Scoring engine, swap-graph builder, explanation pre-generation | `domain/scoring/`, `ScoringJob`, `SwapGraphBuilder` and `ExplanationPregen` in `platform/knowledge/jobs.py` |
| Event collector | `EventService` in `platform/services/events.py` |
| Order & Attribution | `platform/services/orders.py`, `domain/commerce/fees.py` |
| Recommended-add fee (RA-1) | `RecoChargeService` in `platform/services/events.py`, `RecoAddFeeRule` in `domain/commerce/fees.py` |
| Ledger, statements & disputes | `domain/commerce/ledger.py`, `LedgerService` in `infrastructure.py`, `platform/services/billing.py` |
| Webhook dispatcher | `WebhookFanoutConsumer`, `WebhookDispatcher`, `UrlPolicy` in `infrastructure.py` |
| LLM gateway | `platform/llm/gateway.py`, offline mock in `platform/llm/mock.py` |
| Event bus and lakehouse | `platform/bus.py`: transactional outbox, `OutboxRelay`, `InProcessEventBus`, `LakeSink` |
| Experimentation | Holdout assignment in `evaluation.py`; no metric pipeline |
| Control plane | Partner configuration in `tenant.partner.config`, `api/admin_routes.py`, `PartnerDirectory` in `platform/tenancy.py` |
| Curation & brand portals | Not implemented (curation tasks are created and listed at `GET /admin/curation-tasks`) |

## 5. Request pipeline

Every request passes, outermost first:

1. **CORS** for the browser SDK: origins from `cors.origins`, methods GET and POST, headers `Authorization`,
   `Content-Type`, `Idempotency-Key` and `traceparent`.
2. **Problem middleware**: every error becomes an RFC 9457 problem document with `type`, `title`, `status`, `detail`,
   `instance`, an `HS-` `code`, `traceId` (continuing a W3C `traceparent`) and field `errors`.
3. **Authentication**: bearer token validation (ES256 tokens of the local issuer, or tokens of an external authority
   through its OpenID configuration and JWKS). Invalid or missing tokens on protected routes get 401 with
   `WWW-Authenticate`.
4. **Rate limits** on `/v1`: token buckets per partner (or client id, or IP) and route group; cart evaluations 50 per
   second, everything else 20, burst × 2; 429 `HS-RATE-LIMITED` with `RateLimit-*` and `Retry-After` headers.
5. **Route pipeline**: scope check (403 `HS-AUTH-SCOPE`), media type (415), JSON parsing and contract validation of
   body, query and path against `openapi.yaml` (400 `HS-REQ-INVALID`, at most 50 field errors), then the
   `Idempotency-Key` handling for create operations (required for `POST /v1/orders`).
6. **Endpoint**: the synchronous service call runs on a worker thread; the response keeps the contract's shape. Unknown
   routes answer an empty 404 or 405.

Services use short database transactions (section 6). The cart evaluation reads the SKU map, inventory, health
profiles, the active swap graph and the pre-generated explanations from the hot cache, never calls an LLM, and leaves
the database writes to the `EvaluationRecorder` (section 7).

## 6. Database roles and row-level security

`db/schema.sql` creates five `NOLOGIN` roles and enables and forces row-level security on every tenant, transaction and
billing table with one policy: `partner_id = current_setting('app.partner_id')`. A missing setting shows no rows (fail
closed).

| Role | Used for |
| --- | --- |
| `hs_app` | The request path: catalog, inventory, evaluations, events, orders, preferences, disputes |
| `hs_ledger_writer` | The money path: fee accrual and finalization, RA-1 finalization, statements, credit notes, ledger postings |
| `hs_kb_writer` | The knowledge plane: ingestion, scoring, swap-graph builds, LLM audit rows |
| `hs_ops` | Control plane and cross-tenant schedulers (`BYPASSRLS`); never the partner request path |
| `hs_readonly` | Analytics and support, tenant-scoped through RLS |

Every unit of work (`Db.tx` in `platform/db.py`) runs in one transaction:

```sql
BEGIN;
SET LOCAL ROLE hs_app;                                   -- the role of this unit of work
SELECT set_config('app.partner_id', '<partner uuid>', true);  -- transaction-local tenant
-- queries
COMMIT;
```

The login user must be allowed to `SET ROLE` to the `hs_*` roles (the dev database uses the `postgres` superuser; on an
external PostgreSQL grant the roles to the service login; `database.use_roles = false` turns the switching off, which
also turns off RLS for a privileged login). The PGlite dev server multiplexes client connections over one backend and
serializes transactions, so all state is transaction-local (`SET LOCAL`, `set_config(..., true)`), and webhook
deliveries and LLM calls never run inside an open transaction.

## 7. Background workers and events

`Container.start_workers` starts these threads in the API process:

| Worker | Interval | Does |
| --- | --- | --- |
| `InProcessEventBus` | Continuous | Delivers bus messages to the webhook fan-out and the lake sink |
| `OutboxRelay` | 0.05 s while busy, else 0.5 s | Publishes up to 500 `txn.outbox` rows per pass (`FOR UPDATE SKIP LOCKED`) |
| `EvaluationRecorder` | Continuous queue | Writes `txn.cart_evaluation` and `txn.suggestion` and emits `hs.cart-evaluations.v1` (ranker features and positions) off the hot path; capacity 50,000, drops the oldest when full |
| `WebhookDispatcher` | Every second | Signs and delivers due webhooks (batch 50, lease 60 s, backoff 10 s to 4 h, dead after 24 h) |
| `FinalizerWorker` | 30 s (RA-1), 60 s (FP-1) | RA-1 charge finalizer and FP-1 return-window finalizer (`scheduler.enabled`) |
| `CacheWarmer` | At startup, then checks every 15 s | Rebuilds the hot cache from PostgreSQL when the knowledge or tenant data changed |

Business events are written to the transactional outbox in the same transaction as the change, then relayed to the
in-process bus. Topics: `hs.cart-evaluations.v1`, `hs.suggestion-events.v1`, `hs.orders.v1`, `hs.attribution.v1`,
`hs.reco-charges.v1`, `hs.catalog-changes.v1`, `hs.inventory-updates.v1`, `hs.kb-product-changes.v1`, `hs.billing.v1`
and the webhook requests. The lake sink appends every message to
`.data/lake/bronze/<topic>/dt=<yyyy-mm-dd>/part-0000.jsonl`, the training-data source for `ml/`.

## 8. Configuration

Settings are layered, later wins:

1. Code defaults (`src/healthswap/platform/settings.py`).
2. `config/settings.toml`.
3. `config/settings.<environment>.toml`; the environment is `HS_ENV` (default `Production`; `run_local.py` uses
   `Development`).
4. Environment variables `HS_<SECTION>__<KEY>` (case-insensitive; dictionary entries as
   `HS_<SECTION>__<KEY>__<NAME>`, for example `HS_LLM__DEPLOYMENTS__T5`) and `DATABASE_URL`.
5. Overrides from the command line (`hs --database-url`).

Unknown keys are errors. Secrets (`llm.api_key`, `llm.bearer_token`) are accepted from environment variables only and
rejected in TOML files. Relative paths are resolved against the repository root (`HEALTHSWAP_REPO_ROOT` overrides it).

| Setting | Default | Meaning |
| --- | --- | --- |
| `database.url` (`DATABASE_URL`) | `postgresql://postgres:postgres@127.0.0.1:55432/postgres?...` | libpq URL; `run_local.py` passes the dev database URL |
| `database.max_pool_size` | 16 | Connection pool size |
| `cache.redis` | unset | Redis connection string or URL; unset uses the in-process store |
| `llm.provider` | `Mock` | `Mock` or `AzureOpenAI` |
| `llm.endpoint`, `llm.api_key`, `llm.bearer_token` | unset | Azure OpenAI resource and credentials (environment only) |
| `llm.deployments` | T3 `hs-t3-classifier`, T4 `hs-t4-judge`, T5 `hs-t5-explainer`, T7 `hs-embeddings` | Deployment per task |
| `llm.max_calls_per_task_per_run` | 5000 | Budget per task and run |
| `llm.mock_fault_every` | 7 in `settings.toml` | The mock returns an invalid answer every n-th call to exercise the validators |
| `llm.enable_explanation_polish` | true | T5 polish of pre-generated explanations |
| `graph.llm_judge_uncertain`, `uncertain_low`, `uncertain_high` | true, 0.60, 0.80 | T4 judge for swap pairs in the uncertain substitutability band |
| `models.directory` | `models` | ONNX models served by the API |
| `evaluation.internal_budget_ms` | 100 | Compute budget of the hot path; lines beyond it are skipped and the evaluation is `degraded` (`TIMEOUT_BUDGET`) |
| `evaluation.suggestion_ttl_minutes` | 30 | Lifetime of an evaluation and its suggestions |
| `scheduler.enabled` | true | Background finalizers |
| `scheduler.reco_finalizer_seconds`, `attribution_finalizer_seconds` | 30, 60 | Finalizer intervals |
| `webhooks.allow_http_localhost` | false (true in Development) | Allow `http://localhost` webhook URLs |
| `webhooks.max_attempt_hours`, `timeout_seconds` | 24, 10 | Delivery retry horizon and timeout |
| `auth.authority` | unset | External OAuth authority; when set, the development token endpoint answers 404 |
| `auth.issuer`, `auth.audience` | `http://localhost:8080/oauth2`, `healthswap-api` | Local tokens |
| `auth.access_token_minutes`, `sdk_session_max_seconds` | 10, 900 | Token lifetimes |
| `auth.clients_file`, `signing_key_file` | `data/seed/demo/partners.json`, `.data/keys/jwt-es256.pem` | Development identity provider |
| `rate_limits.cart_evaluations_per_second`, `default_per_second`, `burst_multiplier` | 50, 20, 2 | Rate limits |
| `cors.origins` | `http://localhost:8080` | Browser origins |
| `demo.enabled` | false (true in Development) | `/demo` routes and the demo shop |
| `data_protection.keys_directory` | `.data/keys` | Master key for webhook secrets and upload tokens |
| `lake.enabled`, `lake.root` | true, `.data/lake` | Event lake |
| `server.port` | 8080 | Default port of `hs serve` (which listens on `127.0.0.1` unless `--host` is given) |

Partner-specific behavior (markets, currency, fees, caps, windows, placements, kill switch, holdout) lives in each
partner's configuration document in the database ([06 §7](06-recommended-label-and-add-fee.md#7-partner-configuration)),
not in the settings.

## 9. ML models

Three binary classifiers run in-process with ONNX Runtime: the ranker (acceptance propensity), substitutability and
entity resolution. Each is `<models.directory>/<name>.onnx` plus `<name>.meta.json` (name, version, `featureNames`,
input and output names, metrics). The feature order in the metadata is the contract between training and serving: a
model whose `featureNames` differ from the serving code is refused. Missing or refused models fall back to deterministic
heuristics (ranker version `heuristic-L2`, the others `heuristic`), so the API always works without trained models.

Training needs the `[ml]` extra and training data exported from a running database. With the stack running, use the
database URL that `run_local.py` printed:

```shell
python -m pip install -e ".[ml]"
hs --database-url "<database URL from the run_local banner>" ml export
hs --database-url "<database URL from the run_local banner>" ml simulate --sessions 2000
python ml/train.py all
python ml/train.py all --install
```

| Step | Result |
| --- | --- |
| `hs ml export` | Products, pairs, explanation facts and category examples in `ml/data/` (features computed by the serving code) |
| `hs ml simulate` | `ml/data/ranker_events.jsonl`: simulated shopper sessions against the evaluation pipeline, with propensities |
| `python ml/train.py all` | Substitutability and entity resolution: logistic regression, or gradient boosting when its validation log loss is at least 10% lower; ranker: logistic regression with inverse-propensity weights (IPS-weighted AUC reported). Exported to ONNX (opset 17), checked against scikit-learn (max difference 1e-5) and written to `ml/out/models` with metrics and calibration |
| `--install` | Copies the models into `models.directory`; refuses a model with validation AUC below 0.6 unless `--force` |

The running API loads installed models on `POST /admin/models/reload` (admin token) or at the next start; the
evaluation's `modelVersions.ranker` then shows the trained version instead of `heuristic-L2`. Ranker features never
include commercial signals ([06 §12](06-recommended-label-and-add-fee.md#12-fee-neutral-ranking)).

## 10. LLM gateway and fine-tuning

LLMs are used offline only, never on the cart hot path. The gateway (`platform/llm/gateway.py`) serves these tasks:

| Task | Prompt | Used by |
| --- | --- | --- |
| T3 | `category_classifier` (T3-v1) | Ingestion, when the source has no category: an allowed category id with confidence at least 0.8 |
| T4 | `substitutability_judge` (T4-v1) | Swap-graph builder, for pairs whose substitutability score is in the uncertain band (0.60 to 0.80) |
| T5 | `explanation_writer` (T5-v1) | Explanation pre-generation: optional polish of the template text; failures keep the template |
| T7 | Embeddings | Swap-graph builder: product embeddings for candidate retrieval, stored in `rec.product_embedding` |

Every call uses a versioned prompt with strict JSON-schema output and delimited, data-not-instructions input; outputs are
schema-checked (`SCHEMA_INVALID`) and, for explanations, checked again by the deterministic validators against the label
data before use. Results are cached by content hash, each task has a call budget per run (`BUDGET_EXCEEDED` works as a
kill switch), and every call writes an audit row to `rec.llm_audit` (task, prompt version, model, input hash, output,
validation, tokens, latency); `GET /admin/llm-audit` summarizes it. Only product data is sent, never personal data.

Providers:

- `Mock` (default): a deterministic offline provider that also injects invalid answers (`mock_fault_every`).
- `AzureOpenAI`: `POST {endpoint}/openai/v1/chat/completions` with `response_format` JSON schema and
  `/openai/v1/embeddings`, authenticated with `HS_LLM__API_KEY` or a Microsoft Entra ID access token in
  `HS_LLM__BEARER_TOKEN`. The token is used as given: acquiring and refreshing it with a managed identity is not
  implemented in the reference implementation.

`hs llm eval` runs the T5 task over recorded fact sheets through the production validator chain and reports acceptance:

```shell
hs --database-url "<database URL from the run_local banner>" llm eval --max 50
```

Fine-tuning the T5 explainer on Azure OpenAI (`ml/finetune/`):

1. `python ml/finetune/build_datasets.py` turns `ml/data` into chat-format training and validation files in
   `ml/finetune/out/` (`t5_train.jsonl`, `t5_validation.jsonl`, `t5_validation_facts.jsonl`; also T4 and T3 with
   `--tasks t5,t4,t3`), grouped and deterministic (`--seed 7`, `--validation-share 0.2`).
2. `python ml/finetune/submit.py` is a dry run: it checks the files and prints what it would send.
3. `python ml/finetune/submit.py --submit --wait --endpoint https://<resource>.openai.azure.com` uploads the files and
   starts the job (`--model gpt-4.1-mini-2025-04-14` or `gpt-4.1-nano-2025-04-14`, `--training-type` `Standard`,
   `GlobalStandard` or `Developer`). Authentication: `--auth entra` (default; `AZURE_OPENAI_BEARER_TOKEN` or
   `az account get-access-token --resource https://cognitiveservices.azure.com`) or `--auth key`
   (`AZURE_OPENAI_API_KEY`).
4. Deploy the fine-tuned model (for example `az cognitiveservices account deployment create ... --deployment-name
   hs-t5-explainer`), as printed by `submit.py`.
5. Point the platform at it: `HS_LLM__PROVIDER=AzureOpenAI`, `HS_LLM__ENDPOINT`, `HS_LLM__DEPLOYMENTS__T5` and
   `HS_LLM__BEARER_TOKEN` (or `HS_LLM__API_KEY`).
6. Compare with `hs llm eval --input ml/finetune/out/t5_validation_facts.jsonl --max 500`.

Steps 3 to 6 need an Azure OpenAI resource and were not executed for this document.

## 11. SDKs

### 11.1 Python server SDK (`sdk/python`, package `healthswap_client`)

- One method per API operation, named after its `operationId` in snake case (`evaluate_cart`, `create_order`, ...), with
  dicts in the API's JSON shape in and out, plus `report_events`, paging iterators (`iter_recommendation_charges`,
  `iter_statements`, ...) and a low-level `request`. Errors raise `HealthSwapError` with the problem details.
- OAuth 2.0 client credentials with a thread-safe token cache that refreshes in the background before expiry; a 401 is
  answered once with a fresh token. Plain HTTP is refused except to loopback hosts.
- `evaluate_cart` **fails open**: on its deadline (`evaluation_timeout`, default 150 ms, including token and DNS), a
  connection error, 5xx, 429 or an open circuit breaker it returns an evaluation with no suggestions, `degraded: true` and
  `failOpen` instead of raising. The circuit breaker opens after 5 consecutive failures and lets one probe through after
  30 seconds.
- Other calls are retried (default 2 retries, exponential backoff with jitter, 0.5 s base, 8 s cap) when safe: GET, PUT,
  DELETE, POSTs with an Idempotency-Key, and event batches (deduplicated by `eventId`). `Retry-After` is honored.
  Create operations get an Idempotency-Key automatically.
- `events.impression()`, `accept()`, `add_to_cart()`, `undo()`, `dismiss()` build events with fresh `eventId`s.
- `verify_webhook(secret, header, raw_body)` checks `HealthSwap-Signature` in constant time with a 300-second tolerance
  and supports several `v1` values during secret rotation; `sign_webhook` helps testing receivers.

### 11.2 Browser SDK (`sdk/web`)

- `createHealthSwap({ baseUrl, getSessionToken, locale, onEvent })`; the partner backend mints the SDK session
  (`POST /v1/sdk/sessions`), which is the only credential in the browser.
- `evaluateCart` never rejects: it fails open after 1,500 ms (`timeoutMs`).
- `renderSuggestions` renders the fee disclosure above the cards and, on each card, the label, price difference, grade
  and a "Why?" panel with the bullets and the label's disclosure. Add reports ACCEPT and offers a 5-second Undo (UNDO);
  Keep original reports DISMISS. The SDK never changes the cart: the `onAdd`, `onUndo` and `onDismiss` callbacks do.
  Other adds of a recommended product are reported with `addToCart` (ADD_TO_CART).
- `trackImpressions` sends one IMPRESSION per suggestion once at least 50% of its card is visible for 1 second.
- Events are batched (every 2 seconds or at 20 events, up to 500 per request), retried with backoff, and sent with
  `fetch` `keepalive` when the page is hidden or closed.
- API data is rendered with `textContent` only and only http(s) links are used (XSS-safe).

## 12. CLI reference

`hs` prints JSON reports on stdout and logs on stderr; exit codes 0 (success), 1 (failure), 2 (usage).

| Command | Does |
| --- | --- |
| `hs db wait [--timeout 60]` | Waits until PostgreSQL accepts connections and has the schema |
| `hs seed [--demo]` | Reference data, knowledge base, scores, swap graphs and explanations (+ demo partners, catalogs, inventory) |
| `hs kb ingest --source brand\|off\|fdc --file <path>` | Ingests product records from a brand portal, Open Food Facts or USDA FDC file |
| `hs kb recalls --file <openfda.json>` | Matches openFDA enforcement records |
| `hs kb score`, `build-graph`, `explain`, `rebuild` | Knowledge-plane jobs (`rebuild` = all three) |
| `hs jobs finalize` | RA-1 charge finalizer and FP-1 return-window finalizer |
| `hs billing month-close [--period YYYY-MM] [--partner <id>]` | Month close (default: the current UTC month, wall clock) |
| `hs ml export [--out ml/data]` | Training data for `ml/` |
| `hs ml simulate [--partner <id>] [--sessions 2000] [--out ...] [--seed 7]` | Ranker training rows from simulated sessions |
| `hs llm eval [--input ...] [--max 200]` | T5 through the production validator chain |
| `hs serve [--host 127.0.0.1] [--port 8080] [--reload]` | Runs the API with every worker |

Global options: `--env <name>`, `--database-url <url>`, `--verbose`. `hs help` prints the full usage.

## 13. Testing

| Command | Runs |
| --- | --- |
| `python -m pytest tests -q -m "not integration"` | Domain rules, platform services with fakes, contract and authentication tests without a database, SDK, ML tooling (about a minute) |
| `python -m pytest tests -q -m integration` | Each test module gets its own fresh in-memory dev database: seeding, knowledge pipeline, the RA-1 and FP-1 flows, billing and disputes, ingestion, and the HTTP API flows and security tests, whose `/v1` responses are validated against `openapi.yaml` |
| `python scripts/smoke_test.py --base-url http://127.0.0.1:8080` | End to end against a running stack: label, RA-1 events, line labels, PDP, IN-1, settlement, dispute, webhooks, MCP |
| `python -m ruff check .` | Lint |

Integration tests are skipped with the reason when Node.js or `tools/devdb/node_modules` is missing;
`HS_TEST_DATABASE_URL` can point one test module at an external fresh database. The ML training tests are skipped
without the `[ml]` extra. Current counts are in [08 §9](08-manual-test-guide.md#9-f-automated-tests).

## 14. Development-only parts and production requirements

| Development-only in the reference implementation | Production needs instead |
| --- | --- |
| PGlite dev database (one backend, serialized transactions) | Managed PostgreSQL 16+ with pgvector, pg_trgm, pgcrypto, read replicas per geography, backups; service logins granted the `hs_*` roles |
| In-process cache | Redis (`cache.redis`), shared by all instances |
| In-process event bus and local JSONL lake | A Kafka-compatible bus and a lakehouse ([01 §10](01-hld.md#10-data-architecture)) |
| Development identity provider: client secrets from `data/seed/demo/partners.json`, ES256 key in `.data/keys` | A real OAuth 2.0 authority (`auth.authority`, for example Microsoft Entra ID) with client credentials or mTLS-bound tokens (RFC 8705, not implemented); no secrets in the repository |
| `/demo` routes and the demo shop (`demo.enabled`, Development) | Disabled; the partner hosts its own backend for SDK sessions |
| `POST /admin/clock` time travel (Development only) | Not available; real time only |
| Data-protection master key file in `.data/keys` | Azure Key Vault or a KMS for webhook secrets and upload tokens |
| Plain HTTP on localhost | HTTPS everywhere, behind a gateway with WAF ([01 §17](01-hld.md#17-deployment-architecture)) |
| Mock LLM provider | Azure OpenAI with Microsoft Entra ID through a managed identity, deployments per task |
| Admin plane on the same host as the partner API | A private admin plane (network-restricted, separate identity) |
| Single process | Several API instances and separate workers; the finalizers and outbox relay already use `FOR UPDATE SKIP LOCKED` |

## 15. Security notes

| Topic (OWASP) | Implementation |
| --- | --- |
| Broken access control | Scopes per operation from the contract; partner and admin planes separated by scope; SDK sessions limited to three scopes and bound to one cart (and user, store, market) |
| Tenant isolation | Row-level security with a transaction-local `app.partner_id` and `SET LOCAL ROLE`; another tenant's resources answer 404 |
| Identification and authentication | Short-lived ES256 tokens (10 minutes), client secrets stored as PBKDF2-SHA256 hashes (100,000 iterations), JWKS published, 30-second clock skew |
| Injection | Parameterized SQL only (psycopg server-side binding); JSON schema validation of every request against the contract |
| SSRF | Webhook URLs must be https (http only for localhost in Development), without credentials, and must not resolve to private, loopback, link-local or CGNAT addresses (also integer and hex IPv4 forms); the vetted address is pinned for the connection (Host header and SNI kept); no redirects |
| Integrity of outbound data | Webhooks signed with HMAC-SHA256 and a timestamp; per-endpoint secrets encrypted at rest (AES-256-GCM) |
| Replay and duplicates | Idempotency keys (24 hours, request hash, 409 on a different request), event ids, ledger posting keys |
| Sensitive data exposure | Pseudonymous `userRef` only, removed from charges and orders when they are final; LLMs receive product data only; secrets only from environment variables, never logged |
| XSS | The browser SDK and the demo shop write API data with `textContent`; the demo pages send a Content Security Policy |
| Resource exhaustion | Rate limits, array and size limits from the contract (for example at most 500 events per request), LLM call budgets |

## 16. Known limitations

- Production infrastructure from section 14 (managed PostgreSQL, Redis, a Kafka-compatible bus, external OAuth with
  mTLS, Key Vault, managed-identity token acquisition for Azure OpenAI) is not implemented; only the extension points
  exist (`database.url`, `cache.redis`, `auth.authority`, `llm.bearer_token`).
- One region and one database: data residency per geography is a deployment concern not modelled in the code.
- The admin plane is served by the same application as the partner API, protected by the `hs.admin` scope only.
- MCP tools accept a token with either `carts.evaluate` or `products.read`, including `record_swap_decision`, which
  records events; the REST API requires `events.write` for that.
- The holdout applies to cart evaluations only; product-page and search alternatives are served to holdout carts too.
- There is no webhook for RA-1 charge changes; partners poll `GET /v1/billing/recommendation-charges`.
- No curation or brand portal UI, no partner-facing configuration API, no experiment metric pipeline.
- The PGlite dev database serializes transactions and is for development and tests only.
