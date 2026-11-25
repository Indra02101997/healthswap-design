// Content of the HealthSwap system-design walkthrough video: one entry per chapter with its diagram (nodes, groups,
// edges, cards, lists, texts on a 1920 x 1080 stage; the diagram area is y 120-865, the caption sits below) and its
// beats. A beat is one caption sentence group, spoken by the narrator: `text` is shown (`code` and **bold** allowed),
// `say` overrides what is spoken, `show` reveals elements, `focus` dims everything else, `flow` moves packets along edge
// chains ("-id" runs an edge backwards), `on` highlights list items, `lines` highlights card lines.

const N = (id, x, y, w, h, kind, title, sub, body, extra = {}) => ({ id, x, y, w, h, kind, title, sub, body, ...extra });
const E = (id, from, to, extra = {}) => ({ id, from, to, ...extra });
const range = (prefix, n, from = 1) => Array.from({ length: n }, (_, i) => `${prefix}${i + from}`);

export const meta = {
  brand: "HealthSwap",
  subtitle: "System design walkthrough",
};

export const scenes = [
  {
    id: "intro",
    chapter: null,
    section: "HealthSwap system design",
    texts: [
      { id: "title", x: 120, y: 190, w: 1680, text: "HealthSwap", size: 124, weight: 800, spacing: "-1px" },
      { id: "subtitle", x: 126, y: 345, w: 1680, text: "System design and code walkthrough", size: 54, weight: 600, color: "#2dd4bf" },
      {
        id: "stack", x: 128, y: 435, w: 1680, size: 27, color: "#93a4bd",
        text: "Python reference implementation · `src/healthswap` · FastAPI · PostgreSQL · ONNX · offline LLMs",
      },
    ],
    nodes: [
      N("n1", 120, 560, 400, 150, "api", "Partner API", "/v1 · /admin · /mcp", "cart evaluations in milliseconds"),
      N("n2", 560, 560, 400, 150, "worker", "Knowledge plane", "offline jobs", "scores · swap graph · explanations"),
      N("n3", 1000, 560, 400, 150, "service", "Events, orders, billing", "RA-1 · FP-1 · ledger", "transparent, evidence-based fees"),
      N("n4", 1440, 560, 400, 150, "client", "SDKs and agents", "sdk/python · sdk/web · MCP", "partner apps and AI assistants"),
    ],
    beats: [
      {
        text: "This video walks through the HealthSwap system: what each component does, how requests and data flow through the code, and how the parts work together.",
      },
      {
        text: "HealthSwap is a B2B service for grocery and quick-commerce apps. When a shopper builds a cart, it suggests healthier, in-stock and allergen-safe alternatives from the same store.",
        say: "HealthSwap is a business-to-business service for grocery and quick-commerce apps. When a shopper builds a cart, it suggests healthier, in-stock and allergen-safe alternatives from the same store.",
        show: ["n1", "n2", "n3", "n4"],
      },
    ],
  },

  {
    id: "product",
    chapter: "01",
    section: "What HealthSwap does",
    nodes: [
      N("cart", 80, 150, 520, 220, "client", "Shopper's cart", "partner app", [
        "Swaad Masala Noodles 280 g · ₹56 × 2 · grade D",
        "Fizzo Cola 750 ml · ₹40 · grade D",
      ]),
      N("eval", 700, 195, 420, 130, "api", "HealthSwap API", "POST /v1/cart-evaluations"),
      N("sugg", 1220, 150, 620, 220, "ml", "MilletMagic Millet Noodles 256 g", "Recommended by HealthSwap", [
        "grade C (was D) · ₹33.00 more per unit",
        "3x the fibre, 52% less saturated fat per 100 g",
      ]),
      N("fp1", 80, 430, 850, 160, "data", "FP-1 · fair-premium fee", null, [
        "10% of the extra amount paid for a pricier swap,",
        "once the order is delivered and not returned",
      ]),
      N("ra1", 990, 430, 850, 160, "data", "RA-1 · recommended-add fee", null, [
        "₹1.00 each time a shopper adds a recommended,",
        "labeled product to the cart (paid by the partner)",
      ]),
    ],
    texts: [
      {
        id: "principles", x: 80, y: 660, w: 1760, size: 26, align: "center", pill: true, color: "#cbd5e1",
        text: "Rules decide · LLMs assist offline only · fee-neutral ranking · fail open: the cart never waits",
      },
    ],
    edges: [E("e1", "cart", "eval"), E("e2", "eval", "sugg")],
    beats: [
      {
        text: "A partner's app sends the shopper's cart to HealthSwap, which answers with healthier alternatives from the same store.",
        flow: [["e1", "e2"]], packet: "cart",
      },
      {
        text: "Every alternative carries the label \"Recommended by HealthSwap\", a factual reason and the price difference. The shopper decides; HealthSwap never changes the cart.",
        focus: ["sugg"],
      },
      {
        text: "Two fee rules can apply, with terms set per partner. FP-1 earns a share of the fair premium, ten percent in the demo, when a pricier swap is delivered and kept. RA-1 charges a small fixed fee, one rupee in the demo, each time a shopper adds a recommended product.",
        show: ["fp1", "ra1"], focus: ["fp1", "ra1"],
      },
      {
        text: "Rules, not language models, decide what is healthier and what is billed. Ranking never looks at fees, and every call fails open, so the cart never waits.",
        show: ["principles"],
      },
    ],
  },

  {
    id: "context",
    chapter: "02",
    section: "System context",
    groups: [{ id: "gApi", x: 460, y: 150, w: 420, h: 610, label: "src/healthswap/api" }],
    nodes: [
      N("apps", 60, 150, 330, 120, "client", "Shopper apps", "web · Android · iOS", "web SDK or own UI"),
      N("backend", 60, 320, 330, 120, "client", "Partner backend", "sdk/python", "catalog · orders · sessions"),
      N("agents", 60, 490, 330, 110, "client", "AI shopping agents", "MCP tools"),
      N("ops", 60, 650, 330, 110, "neutral", "Operators", "admin token"),
      N("v1", 490, 190, 360, 150, "api", "Partner API", "/v1 · partner_routes.py", "evaluations · events · orders"),
      N("mcp", 490, 400, 360, 120, "api", "MCP server", "/mcp · mcp_tools.py"),
      N("admin", 490, 590, 360, 130, "api", "Admin plane", "/admin · admin_routes.py", "jobs · config · disputes"),
      N("services", 960, 190, 380, 150, "service", "Platform services", "platform/services/", "evaluation · events · billing"),
      N("hot", 960, 420, 380, 110, "store", "Hot cache", "memory or Redis", null, { shape: "db" }),
      N("pg", 960, 600, 380, 120, "store", "PostgreSQL", "db/schema.sql", "RLS · ledger · outbox", { shape: "db" }),
      N("sources", 1440, 150, 420, 140, "external", "Food data sources", null, ["brand portals · Open Food Facts", "USDA FDC · openFDA recalls"]),
      N("kp", 1440, 380, 420, 150, "worker", "Knowledge plane", "platform/knowledge/ · offline", "ingest · score · swap graph · explain"),
      N("llm", 1560, 640, 300, 120, "external", "LLM provider", "Azure OpenAI or mock", "offline tasks only"),
    ],
    edges: [
      E("eApps", "apps", "v1", { toOffset: -30 }),
      E("eBackend", "backend", "v1", { toOffset: 30 }),
      E("eAgents", "agents", "mcp"),
      E("eOps", "ops", "admin"),
      E("eV1", "v1", "services", { toOffset: -40 }),
      E("eMcp", "mcp", "services"),
      E("eAdmin", "admin", "services", { fromSide: "right", toSide: "left", toOffset: 40 }),
      E("eHot", "services", "hot", { label: "read" }),
      E("ePg", "services", "pg", { points: [[1340, 300], [1395, 300], [1395, 640], [1340, 640]], label: "write" }),
      E("eSrc", "sources", "kp"),
      E("eKpPg", "kp", "pg", { points: [[1480, 530], [1480, 690], [1340, 690]], label: "scores · graph", labelAt: 0.3 }),
      E("eWarm", "pg", "hot", { label: "cache warmer", labelDx: -95 }),
      E("eLlm", "kp", "llm"),
    ],
    beats: [
      {
        text: "At the edge, partner apps and the partner's backend call the Partner API. AI shopping agents use the MCP server, and operators use the admin plane.",
        flow: [["eApps"], ["eBackend"], ["eAgents"], ["eOps"]],
        focus: ["apps", "backend", "agents", "ops", "v1", "mcp", "admin", "gApi"],
      },
      {
        text: "All three planes call the same platform services. On the request path the services read the hot cache, and they write PostgreSQL in short transactions.",
        flow: [["eV1", "eHot"], ["eMcp", "ePg"]],
        focus: ["v1", "mcp", "admin", "gApi", "services", "hot", "pg", "eAdmin"],
      },
      {
        text: "Offline, the knowledge plane turns food data sources into scores, a swap graph and explanations in PostgreSQL, and a cache warmer copies them into the hot cache.",
        flow: [["eSrc", "eKpPg", "eWarm"]], packet: "data",
        focus: ["sources", "kp", "pg", "hot"],
      },
      {
        text: "Language models are used only by these offline jobs, never while a shopper waits.",
        flow: [["eLlm"]], focus: ["kp", "llm"],
      },
    ],
  },

  {
    id: "layers",
    chapter: "03",
    section: "Three layers, one direction",
    groups: [
      { id: "gApi", x: 60, y: 140, w: 1800, h: 190, label: "src/healthswap/api — HTTP" },
      { id: "gPlat", x: 60, y: 380, w: 1800, h: 190, label: "src/healthswap/platform — everything with I/O" },
      { id: "gDom", x: 60, y: 620, w: 1800, h: 180, label: "src/healthswap/domain — pure rules, no I/O" },
    ],
    nodes: [
      ...[
        ["a1", "app.py", "create_app, middleware order, workers"],
        ["a2", "http.py", "middleware and route pipeline"],
        ["a3", "auth.py", "ES256 tokens, scopes, SDK sessions"],
        ["a4", "contract.py", "validates against openapi.yaml"],
        ["a5", "partner_routes.py", "/v1 partner API"],
        ["a6", "admin_routes.py", "/admin and /demo"],
        ["a7", "mcp_tools.py", "/mcp agent tools"],
      ].map(([id, title, body], i) => N(id, 86 + i * 252, 172, 236, 134, "api", title, null, body, { small: true })),
      ...[
        ["p1", "container.py", "composition root: builds services, starts workers"],
        ["p2", "services/", "evaluation, events, orders, billing, catalog"],
        ["p3", "knowledge/", "connectors, ingestor, scoring, graph, texts"],
        ["p4", "db.py", "one role and tenant per transaction (RLS)"],
        ["p5", "hot_cache.py", "typed hot-path keys, memory or Redis"],
        ["p6", "bus.py", "outbox, relay, in-process bus, lake sink"],
        ["p7", "llm/ · ml/", "LLM gateway; ONNX model registry"],
      ].map(([id, title, body], i) => N(id, 86 + i * 252, 412, 236, 134, "service", title, null, body, { small: true })),
      ...[
        ["d1", "scoring/", "HSI, nutrient points models, front-of-pack bands"],
        ["d2", "recommendation/", "rules R1–R7, ranking, explanations, labels"],
        ["d3", "commerce/", "FP-1, RA-1, ledger postings, partner config"],
        ["d4", "knowledge/", "parsing, allergens, QA, entity resolution"],
        ["d5", "primitives.py", "money, GTIN, clocks, UUIDv7 ids"],
      ].map(([id, title, body], i) => N(id, 84 + i * 354, 652, 336, 124, "domain", title, null, body, { small: true })),
    ],
    edges: [
      E("g1", "gApi", "gPlat", { label: "calls", labelDx: 60 }),
      E("g2", "gPlat", "gDom", { label: "uses", labelDx: 56 }),
    ],
    beats: [
      {
        text: "The code has three layers with a single dependency direction: api calls platform, and platform calls domain. Nothing calls upward.",
        flow: [["g1", "g2"]],
      },
      {
        text: "The api package handles HTTP: the application factory, the middleware chain, authentication, contract validation, and the route sets for partners, operators and agents.",
        focus: ["gApi", ...range("a", 7)],
      },
      {
        text: "The platform package does everything with input and output: services, the knowledge jobs, database transactions, the hot cache, the event bus, and the LLM and model clients.",
        focus: ["gPlat", ...range("p", 7)],
      },
      {
        text: "The domain package holds the business rules as pure functions: scores, eligibility rules, ranking, explanations, fees and ledger postings. They are unit-tested without a database.",
        focus: ["gDom", ...range("d", 5)],
      },
      {
        text: "`platform/container.py` is the composition root. It builds every service lazily from the settings and starts the background workers.",
        say: "The platform container module is the composition root. It builds every service lazily from the settings and starts the background workers.",
        focus: ["gPlat", "p1"],
      },
    ],
  },

  {
    id: "repo",
    chapter: "04",
    section: "Repository map",
    nodes: [
      ["r1", "api", "api/", "openapi.yaml", "the Partner API contract; validates every request"],
      ["r2", "store", "db/", "schema.sql", "7 schemas, 5 roles, row-level security, ledger triggers"],
      ["r3", "neutral", "config/", "settings.toml", "layered settings; secrets only from the environment"],
      ["r4", "data", "data/", "packs · seed · fixtures", "market packs, seed products, demo partners"],
      ["r5", "service", "src/healthswap/", "api · platform · domain", "the service and the hs CLI"],
      ["r6", "client", "sdk/python/", "healthswap_client", "server SDK: fail-open evaluation, retries, webhooks"],
      ["r7", "client", "sdk/web/", "healthswap-web.js", "browser SDK: labeled cards and events"],
      ["r8", "client", "web/demo/", "served at /demo", "QuickBasket demo shop"],
      ["r9", "ml", "ml/", "train.py · finetune/", "ONNX models; Azure OpenAI fine-tuning"],
      ["r10", "neutral", "scripts/", "run_local.py · smoke_test.py", "local stack and end-to-end check"],
      ["r11", "neutral", "tools/", "devdb · demo-recorder", "PostgreSQL in WebAssembly; video recorder"],
      ["r12", "neutral", "tests/ · docs/", "domain · platform · api · sdk · ml", "test suites; design docs and guides"],
    ].map(([id, kind, title, sub, body], i) => N(id, 60 + (i % 4) * 450, 140 + Math.floor(i / 4) * 235, 420, 190, kind, title, sub, body)),
    beats: [
      {
        text: "The contract lives in `api`, the database schema in `db`, settings in `config`, and market packs, seed products and demo partners in `data`.",
        say: "The contract lives in the api folder, the database schema in db, settings in config, and market packs, seed products and demo partners in data.",
        focus: range("r", 4),
      },
      {
        text: "`src/healthswap` is the service itself. `sdk/python` and `sdk/web` are the partner SDKs, and `web/demo` is a demo shop built with the web SDK.",
        say: "The source folder holds the service itself. The Python and web SDKs are for partners, and the demo shop is built with the web SDK.",
        focus: range("r", 4, 5),
      },
      {
        text: "`ml` trains the models, `scripts` start the local stack and run the smoke test, `tools` holds the WebAssembly database and the video recorder, and `tests` mirror the layers.",
        say: "The ml folder trains the models, scripts start the local stack and run the smoke test, tools holds the WebAssembly database and the video recorder, and the tests mirror the layers.",
        focus: range("r", 4, 9),
      },
    ],
  },

  {
    id: "pipeline",
    chapter: "05",
    section: "How a request flows",
    nodes: [
      N("caller", 60, 150, 230, 130, "client", "Caller", "app · backend", "or an AI agent"),
      N("cors", 340, 150, 220, 130, "api", "CORS", "app.py", "browser origins"),
      N("problem", 610, 150, 270, 130, "api", "Problem handler", "http.py", "errors → RFC 9457"),
      N("auth", 930, 150, 250, 130, "api", "Authentication", "auth.py", "bearer token"),
      N("rate", 1230, 150, 220, 130, "api", "Rate limits", "http.py", "per partner"),
      N("pipe", 1500, 140, 360, 190, "api", "Route pipeline", "http.py · pipeline()", ["scope check", "validate vs openapi.yaml", "Idempotency-Key"]),
      N("handler", 1500, 450, 360, 140, "api", "Route handler", "partner_routes.py", "on a worker thread"),
      N("service", 1000, 450, 400, 140, "service", "Service", "platform/services/*.py", "orchestrates the use case"),
      N("dbtx", 500, 450, 400, 140, "store", "Db.tx(role, partner)", "platform/db.py", "one transaction per unit of work"),
      N("pg", 60, 450, 340, 140, "store", "PostgreSQL", "RLS on tenant tables", "business rows + txn.outbox", { shape: "db" }),
      N("domain", 1000, 680, 400, 120, "domain", "Domain rules", "domain/*", "pure functions"),
    ],
    cards: [
      {
        id: "tx", x: 60, y: 615, w: 860, title: "platform/db.py — every unit of work",
        lines: [
          "BEGIN;",
          "SET LOCAL ROLE hs_app;                       -- one role",
          "SELECT set_config('app.partner_id', :p, true);  -- tenant",
          "INSERT ... business rows ...;",
          "INSERT INTO txn.outbox ...;                  -- same tx",
          "COMMIT;",
        ],
      },
    ],
    edges: [
      E("c1", "caller", "cors"),
      E("c2", "cors", "problem"),
      E("c3", "problem", "auth"),
      E("c4", "auth", "rate"),
      E("c5", "rate", "pipe", { toOffset: -20 }),
      E("c6", "pipe", "handler"),
      E("c7", "handler", "service"),
      E("c8", "service", "domain"),
      E("c9", "service", "dbtx"),
      E("c10", "dbtx", "pg"),
    ],
    beats: [
      {
        text: "Every request passes the same chain. CORS comes first, then the problem handler, which turns any error into an RFC 9457 problem document with a stable error code.",
        flow: [["c1", "c2", "c3"]], packet: "request", focus: ["caller", "cors", "problem"],
      },
      {
        text: "Authentication validates the bearer token: an ES256 token from the OAuth endpoint, or a short-lived SDK session token. Then per-partner rate limits apply.",
        flow: [["c3", "c4", "c5"]], packet: "request", focus: ["problem", "auth", "rate"],
      },
      {
        text: "The route pipeline checks the operation's scope, validates body, query and path against `openapi.yaml` itself, and records Idempotency-Keys for create operations.",
        say: "The route pipeline checks the operation's scope, validates body, query and path against the OpenAPI contract itself, and records idempotency keys for create operations.",
        flow: [["c5", "c6"]], packet: "request", focus: ["rate", "pipe", "handler"],
      },
      {
        text: "The handler runs on a worker thread and calls a service, which takes every decision with pure domain rules.",
        flow: [["c6", "c7", "c8"]], focus: ["pipe", "handler", "service", "domain"],
      },
      {
        text: "Each unit of work is one transaction with one database role and the partner id set locally, so row-level security isolates every tenant.",
        flow: [["c9", "c10"]], focus: ["service", "dbtx", "pg", "tx"], lines: { tx: [0, 1, 2, 5] },
      },
      {
        text: "Business events are inserted into an outbox table in the same transaction, so a crash can never lose them.",
        focus: ["pg", "dbtx", "tx"], lines: { tx: [3, 4] },
      },
    ],
  },

  {
    id: "knowledge",
    chapter: "06",
    section: "Knowledge plane (offline)",
    nodes: [
      N("sources", 60, 150, 330, 180, "external", "Sources", null, ["brand portal files", "Open Food Facts · USDA FDC", "openFDA recalls"]),
      N("conn", 450, 170, 290, 140, "worker", "Connectors", "connectors.py", "→ product candidates"),
      N("ing", 800, 150, 400, 190, "worker", "ProductIngestor", "ingestor.py", ["parse ingredients, allergens", "QA · entity resolution · merge", "LLM T3 category if missing"]),
      N("kb", 1260, 175, 250, 140, "store", "kb.*", "product versions", "provenance per field", { shape: "db" }),
      N("score", 1570, 150, 290, 190, "worker", "ScoringJob", "jobs.py", ["HSI per market", "grade A to E", "rules from data/packs"]),
      N("prof", 1570, 440, 290, 140, "store", "rec.health_profile", "per market", null, { shape: "db" }),
      N("graph", 1150, 420, 360, 180, "worker", "SwapGraphBuilder", "jobs.py", ["candidates · rules R1–R7", "top-K edges · blue/green"]),
      N("expl", 700, 420, 390, 180, "worker", "ExplanationPregen", "jobs.py", ["fact sheets · templates", "LLM T5 polish + validator"]),
      N("warm", 380, 440, 260, 140, "worker", "CacheWarmer", "services/read.py"),
      N("hot", 60, 440, 260, 140, "store", "Hot cache", "read by the hot path", null, { shape: "db" }),
    ],
    texts: [
      {
        id: "entry", x: 60, y: 680, w: 1800, size: 25, align: "center", pill: true, color: "#cbd5e1",
        text: "Run by `hs seed --demo` · `hs kb rebuild` · `POST /admin/jobs/kb-rebuild` — never on the request path",
      },
    ],
    edges: [
      E("k1", "sources", "conn"),
      E("k2", "conn", "ing"),
      E("k3", "ing", "kb"),
      E("k4", "kb", "score"),
      E("k5", "score", "prof"),
      E("k6", "prof", "graph"),
      E("k7", "graph", "expl"),
      E("k8", "expl", "warm"),
      E("k9", "warm", "hot"),
    ],
    beats: [
      {
        text: "The knowledge plane runs offline. `hs seed` builds a fresh environment, and the `hs kb` commands or the admin jobs endpoint rebuild it.",
        say: "The knowledge plane runs offline. H S seed builds a fresh environment, and the H S K B commands or the admin jobs endpoint rebuild it.",
        show: ["entry"], focus: ["entry"],
      },
      {
        text: "Connectors read each source format into product candidates. The ingestor parses ingredients, additives and allergens, applies quality rules, merges duplicates across sources, and asks a language model for a category only when a source has none.",
        flow: [["k1", "k2", "k3"]], packet: "product", focus: ["sources", "conn", "ing", "kb"],
      },
      {
        text: "The scoring job computes the HealthSwap Index per market: 70% nutrient profile, 15% processing level and 15% additives, graded A to E.",
        say: "The scoring job computes the HealthSwap Index per market: seventy percent nutrient profile, fifteen percent processing level and fifteen percent additives, graded A to E.",
        flow: [["k4", "k5"]], focus: ["kb", "score", "prof"],
      },
      {
        text: "The swap-graph builder finds healthier substitutes, the explanation job pre-writes why each swap is better, and the cache warmer copies everything the hot path needs into the hot cache.",
        flow: [["k6", "k7", "k8", "k9"]],
      },
    ],
  },

  {
    id: "graph",
    chapter: "07",
    section: "Inside the swap-graph builder",
    lists: [
      {
        id: "steps", x: 60, y: 135, w: 900, size: 21,
        items: [
          "**Blocking:** same or approved category, eligible, higher HSI",
          "**Retrieval:** nearest neighbours by embedding (LLM T7)",
          "**Substitutability:** ONNX model, or a heuristic",
          "**Uncertain 0.60–0.80:** LLM judge T4, averaged in",
          "**Static rules:** R1, R2, R3, R4 and R7",
          "**Top-K:** health gain × substitutability, brand diversity",
          "**Validation gates**, then **blue/green** activation",
        ],
      },
      {
        id: "rules", x: 1000, y: 135, w: 860, size: 21, marks: ["R1", "R2", "R3", "R4", "R7"],
        items: [
          "substitutable: substitutability ≥ 0.70",
          "meaningfully healthier: ΔHSI ≥ 8 and a better grade (same grade: ≥ 16)",
          "no worse limited nutrients or front-of-pack bands",
          "no new additive concerns, no more processing (NOVA)",
          "enough data and confidence; the alternative is eligible",
        ],
      },
    ],
    nodes: [
      N("builder", 60, 520, 880, 110, "worker", "SwapGraphBuilder.build(market)", "platform/knowledge/jobs.py", "per market; LLM calls never run inside a transaction"),
      N("edges", 1000, 520, 400, 120, "store", "rec.swap_edge", "graph version → ACTIVE", null, { shape: "db" }),
      N("cache", 1460, 520, 400, 120, "store", "Hot cache", "edges:{market}:{version}", null, { shape: "db" }),
      N("diag", 1000, 700, 860, 120, "api", "GET /admin/diagnostics/swaps", null, "every candidate of one product with its rule results"),
    ],
    edges: [
      E("w1", "builder", "edges"),
      E("w2", "edges", "cache"),
    ],
    beats: [
      {
        text: "Let's zoom into the swap-graph builder. For each product it first blocks candidates: the same or an approved category, eligible, and a higher index. Then it ranks them by embedding similarity.",
        on: { steps: [0, 1] }, focus: ["steps", "builder"],
      },
      {
        text: "Each pair gets a substitutability score from an ONNX model, or from a heuristic when no model is installed. Only uncertain pairs, between 0.6 and 0.8, go to the language-model judge, and its score is averaged in.",
        on: { steps: [2, 3] }, focus: ["steps", "builder"],
      },
      {
        text: "Static rules then decide eligibility: substitutable, meaningfully healthier, no worse limited nutrients, no new additive concerns or extra processing, and enough data.",
        on: { steps: [4], rules: [0, 1, 2, 3, 4] }, focus: ["steps", "rules"],
      },
      {
        text: "The best edges by health gain times substitutability are kept, with brand diversity. Validation gates must pass; then the new graph is activated blue/green: new keys first, then the pointer flips.",
        on: { steps: [5, 6] }, flow: [["w1", "w2"]], packet: "graph", focus: ["steps", "builder", "edges", "cache"],
      },
      {
        text: "When a product gets no alternatives, the admin diagnostics endpoint shows every candidate with its rule results.",
        show: ["diag"], focus: ["diag", "rules"],
      },
    ],
  },

  {
    id: "catalog",
    chapter: "08",
    section: "Catalog and inventory sync",
    nodes: [
      N("backend", 60, 330, 300, 140, "client", "Partner backend", "OAuth token", "full sync, then deltas"),
      N("catApi", 440, 160, 380, 140, "api", "Catalog batches", "POST /v1/catalog/item-batches"),
      N("invApi", 440, 520, 380, 140, "api", "Inventory batches", "POST /v1/stores/{id}/…", "inventory-batches, per store"),
      N("catSvc", 900, 140, 440, 170, "service", "CatalogService.upsert", "services/catalog.py", ["GTIN match, then entity resolution", "auto-match ≥ 0.95 · review 0.75–0.95"]),
      N("skumap", 1420, 140, 440, 120, "store", "SKU map", "tenant.catalog_item + hot cache", null, { shape: "db" }),
      N("review", 1420, 300, 440, 110, "external", "Review webhook", "catalog.match.review_required"),
      N("enrich", 900, 360, 440, 130, "worker", "CatalogEnrichment", "background job", "own label data → ingestor → rescore"),
      N("invSvc", 900, 540, 440, 120, "service", "InventoryService.upsert", "services/catalog.py"),
      N("stock", 1420, 540, 440, 120, "store", "Stock and prices", "tenant.store_inventory + hot cache", null, { shape: "db" }),
    ],
    texts: [
      {
        id: "feeds", x: 440, y: 720, w: 1420, size: 24, align: "center", pill: true, color: "#cbd5e1",
        text: "Bulk catalogs: `POST /v1/catalog/feeds` → pre-signed upload → `FeedService.process` (JSONL or CSV)",
      },
    ],
    edges: [
      E("b1", "backend", "catApi"),
      E("b2", "backend", "invApi"),
      E("s1", "catApi", "catSvc"),
      E("s2", "catSvc", "skumap"),
      E("s3", "catSvc", "review"),
      E("s4", "catSvc", "enrich"),
      E("s5", "invApi", "invSvc"),
      E("s6", "invSvc", "stock"),
    ],
    beats: [
      {
        text: "Before carts can be evaluated, partners sync their catalog. CatalogService matches each SKU to a canonical product, by GTIN first and then by entity resolution, which either auto-matches or asks for review.",
        flow: [["b1", "s1", "s2"]], packet: "items", focus: ["backend", "catApi", "catSvc", "skumap"],
      },
      {
        text: "Matches that need review trigger a signed webhook. Items that carry their own label data but match nothing are ingested as new products and scored by a background job.",
        flow: [["s3"], ["s4"]], focus: ["catSvc", "review", "enrich"],
      },
      {
        text: "Inventory batches mirror stock and prices per store, so every suggestion is in stock at the shopper's store. Large catalogs use feed files with a pre-signed upload.",
        flow: [["b2", "s5", "s6"]], packet: "stock", show: ["feeds"], focus: ["backend", "invApi", "invSvc", "stock", "feeds"],
      },
    ],
  },

  {
    id: "hotpath",
    chapter: "09",
    section: "Cart evaluation: the hot path",
    lists: [
      {
        id: "steps", x: 60, y: 130, w: 900, size: 21,
        items: [
          "Resolve lines to products: SKU map, then GTIN",
          "Kill switch, placement policy, holdout arm",
          "Shopper constraints with consent: diet, allergens, goals",
          "Target lines: scored, below the category's 60th percentile",
          "Swap edges → R5–R7, stock, price guards, cool-down → rank",
          "Select at most 3 per cart, one per category",
          "Explanations, RA-1 label, suggestion snapshot",
        ],
      },
    ],
    cards: [
      {
        id: "rank", x: 60, y: 585, w: 900, title: "domain/recommendation/ranking.py",
        lines: [
          "score = 0.45·(ΔHSI/50) + 0.25·substitutability + 0.30·P(accept)",
          "      − 0.15·price penalty − 0.20·fatigue + 0.10·goal match",
          "no ranker model → (ΔHSI/50) × substitutability",
          "fee, margin, commission: never ranking features",
        ],
      },
    ],
    groups: [{ id: "cacheG", x: 1040, y: 290, w: 820, h: 290, label: "hot cache · platform/hot_cache.py" }],
    nodes: [
      N("svc", 1040, 130, 820, 110, "service", "EvaluationService.evaluate", "platform/services/evaluation.py", "no PostgreSQL · no LLM · 100 ms compute budget"),
      ...["SKU maps", "Profiles", "Swap edges", "Explanations", "Inventory", "Cool-downs", "Line labels", "Suggestions"].map((title, i) =>
        N(`k${i + 1}`, 1060 + (i % 4) * 200, 335 + Math.floor(i / 4) * 120, 180, 90, "store", title, null, null, { small: true, center: true })),
      N("recorder", 1040, 640, 390, 160, "worker", "EvaluationRecorder", "background thread", ["txn.cart_evaluation", "txn.suggestion"]),
      N("response", 1470, 640, 390, 160, "client", "Response", null, ["suggestions · lineLabels", "disclosures · degraded flag"]),
    ],
    edges: [
      E("r1", "svc", "cacheG", { label: "read only", fromOffset: 250, toOffset: 250 }),
      E("r2", "svc", "response", { points: [[1860, 185], [1890, 185], [1890, 720], [1860, 720]] }),
      E("r3", "svc", "recorder", { points: [[1040, 185], [1012, 185], [1012, 720], [1040, 720]], dash: true }),
    ],
    beats: [
      {
        text: "Now the hot path: the cart evaluations endpoint. EvaluationService reads only the hot cache. There is no database query and no language-model call while the shopper waits.",
        flow: [["r1"]], packet: "cart", focus: ["svc", "cacheG", ...range("k", 8)],
      },
      {
        text: "Step one resolves each cart line to a product. Step two applies the kill switch, the placement policy and the experiment holdout. Step three loads the shopper's preferences, honoring consent.",
        on: { steps: [0, 1, 2] }, focus: ["steps", "svc", "cacheG", "k1", "k2"],
      },
      {
        text: "Step four picks the target lines: scored products below their category's 60th percentile that are not already a HealthSwap recommendation. Step five reads their precomputed swap edges and filters them: allergens and diet, stock, price and pack-size guards, and the seven-day dismiss cool-down.",
        on: { steps: [3, 4] }, focus: ["steps", "svc", "cacheG", "k3", "k5", "k6"],
      },
      {
        text: "The survivors are ranked by health gain, substitutability and the acceptance predicted by the ONNX ranker. Fees are never a ranking feature.",
        on: { steps: [4] }, show: ["rank"], focus: ["steps", "rank"], lines: { rank: [0, 1, 3] },
      },
      {
        text: "Step six keeps at most three suggestions per cart, one per category. Step seven attaches explanations and the RA-1 label, and stores a suggestion snapshot for the events that follow.",
        on: { steps: [5, 6] }, focus: ["steps", "svc", "cacheG", "k4", "k7", "k8"],
      },
      {
        text: "The response returns at once. A background recorder writes the evaluation and its suggestions to PostgreSQL afterwards, and when the compute budget runs out the response is marked degraded instead of waiting.",
        flow: [["r2"], ["r3"]], packets: ["response", "record"], focus: ["svc", "response", "recorder"],
      },
    ],
  },

  {
    id: "client",
    chapter: "10",
    section: "Client integration",
    nodes: [
      N("lApp", 70, 130, 360, 90, "client", "Shopper app", "web SDK or own UI", null, { center: true }),
      N("lBackend", 560, 130, 360, 90, "client", "Partner backend", "holds the OAuth secret", null, { center: true }),
      N("lHs", 1050, 130, 360, 90, "api", "HealthSwap API", "/v1", null, { center: true }),
    ],
    edges: [
      E("life1", [250, 222], [250, 850], { noArrow: true, dash: true }),
      E("life2", [740, 222], [740, 850], { noArrow: true, dash: true }),
      E("life3", [1230, 222], [1230, 850], { noArrow: true, dash: true }),
      E("m1", [250, 290], [740, 290], { label: "POST /api/healthswap/session", labelDy: -20 }),
      E("m2", [740, 360], [1230, 360], { label: "POST /v1/sdk/sessions (OAuth)", labelDy: -20 }),
      E("m3", [1230, 430], [740, 430], { label: "token: one cart, ≤ 15 min", labelDy: -20 }),
      E("m4", [740, 500], [250, 500], { label: "token", labelDy: -20 }),
      E("m5", [250, 580], [1230, 580], { label: "POST /v1/cart-evaluations", labelDy: -20 }),
      E("m6", [1230, 650], [250, 650], { label: "labeled suggestions + disclosures", labelDy: -20 }),
      E("m7", [250, 730], [1230, 730], { label: "POST /v1/events (batched)", labelDy: -20 }),
    ],
    cards: [
      {
        id: "sdk", x: 1480, y: 130, w: 380, title: "sdk/web/healthswap-web.js",
        lines: [
          "createHealthSwap({",
          "  getSessionToken })",
          "evaluateCart(cart)",
          "renderSuggestions(el,",
          "  evaluation, handlers)",
          "trackImpressions(el)",
          "accept · undo · dismiss",
          "addToCart(id, sku, { quantity })",
        ],
      },
    ],
    lists: [
      {
        id: "events", x: 1480, y: 440, w: 380, size: 18, marks: ["I", "A", "+", "U", "D"],
        items: [
          "IMPRESSION: ≥ 50% visible for 1 s",
          "ACCEPT: the shopper tapped Add",
          "ADD_TO_CART: more units, re-adds",
          "UNDO within 5 s voids the charge",
          "DISMISS: 7-day cool-down",
        ],
      },
    ],
    texts: [
      { id: "callout", x: 1480, y: 735, w: 380, size: 21, color: "#fde68a", text: "The SDK never changes the cart: the partner's callbacks do." },
    ],
    beats: [
      {
        text: "On the client side, the partner backend mints a short-lived SDK session bound to one cart, using its own OAuth credentials. The app only ever holds that session token.",
        flow: [["m1", "m2", "m3", "m4"]], packet: "session",
        focus: ["lApp", "lBackend", "lHs", "life1", "life2", "life3", "m1", "m2", "m3", "m4"],
      },
      {
        text: "With it, the app evaluates the cart directly and renders the labeled suggestions and the fee disclosure. In a web shop, the web SDK does this in a few calls.",
        flow: [["m5", "m6"]], packet: "cart", show: ["sdk"], lines: { sdk: [0, 1, 2, 3, 4] },
        focus: ["lApp", "lHs", "life1", "life3", "m5", "m6", "sdk"],
      },
      {
        text: "Then it reports what the shopper did, in batches: an impression after a second of visibility, accept, add, undo or dismiss. The SDK never changes the cart; the partner's callbacks do.",
        flow: [["m7"]], packet: "events", show: ["events", "callout"], lines: { sdk: [5, 6, 7] }, on: { events: [0, 1, 2, 3, 4] },
        focus: ["lApp", "lHs", "life1", "life3", "m7", "sdk", "events", "callout"],
      },
    ],
  },

  {
    id: "events",
    chapter: "11",
    section: "Events and RA-1 charges",
    nodes: [
      N("app", 60, 165, 300, 120, "client", "App or backend", "POST /v1/events"),
      N("ingest", 430, 150, 470, 170, "service", "EventService.ingest", "services/events.py", ["dedupe on eventId (txn.event_key)", "store event + outbox, one transaction"]),
      N("charges", 970, 150, 440, 170, "service", "RecoChargeService", "services/events.py", ["ACCEPT / ADD_TO_CART → PENDING", "UNDO within 5 s → VOID"]),
      N("final", 1480, 150, 380, 170, "worker", "FinalizerWorker", "every 30 s", ["settles after 10 minutes", "RecoAddFeeRule.decide"]),
      N("cache", 430, 390, 470, 120, "store", "Hot cache", null, "line labels · 7-day dismiss cool-down", { shape: "db" }),
      N("ledger", 1480, 390, 380, 120, "store", "Ledger", "RECO_FEE posting", "balanced, append-only", { shape: "db" }),
      N("sP", 60, 690, 220, 56, "worker", "PENDING", null, null, { shape: "pill", small: true }),
      N("sV", 380, 600, 340, 52, "neutral", "VOID · undo within 5 s", null, null, { shape: "pill", small: true }),
      N("sF", 380, 692, 340, 52, "service", "FINAL · evidence holds", null, null, { shape: "pill", small: true }),
      N("sR", 380, 784, 340, 52, "danger", "REJECTED · with a reason", null, null, { shape: "pill", small: true }),
      N("sC", 800, 692, 350, 52, "data", "CREDITED · dispute upheld", null, null, { shape: "pill", small: true }),
    ],
    texts: [{ id: "evTitle", x: 1200, y: 538, w: 660, size: 20, color: "#93a4bd", text: "Finalizer evidence checks (domain/commerce/fees.py)" }],
    lists: [
      {
        id: "evidence", x: 1190, y: 572, w: 670, size: 19,
        items: [
          "served by HealthSwap (a suggestion snapshot exists)",
          "the added SKU was one of the recommended alternatives",
          "inside the attribution window (24 hours)",
          "an IMPRESSION no later than 5 s after the add",
          "per-suggestion and monthly caps not reached",
        ],
      },
    ],
    edges: [
      E("a1", "app", "ingest"),
      E("a2", "ingest", "charges"),
      E("a3", "ingest", "cache"),
      E("a4", "charges", "final"),
      E("a5", "final", "ledger"),
      E("t1", "sP", "sV"),
      E("t2", "sP", "sF"),
      E("t3", "sP", "sR"),
      E("t4", "sF", "sC"),
    ],
    beats: [
      {
        text: "Events arrive in batches. EventService deduplicates on the client's event id, then stores each event and its outbox message in one transaction.",
        flow: [["a1"]], packet: "events", focus: ["app", "ingest"],
      },
      {
        text: "An accept or an add of a recommended product creates a pending RA-1 charge, and an undo within five seconds voids it. Adds also label the cart line, and a dismiss starts the seven-day cool-down.",
        flow: [["a2"], ["a3"]], show: ["sP", "sV"], focus: ["ingest", "charges", "cache", "sP", "sV", "t1"],
      },
      {
        text: "Every thirty seconds the finalizer settles pending charges once the ten-minute window has passed. It checks the evidence: served by HealthSwap, a recommended SKU, inside the window, an impression no later than five seconds after the add, and the caps.",
        flow: [["a4"]], packet: "charge", show: ["evidence", "evTitle"], on: { evidence: [0, 1, 2, 3, 4] },
        focus: ["charges", "final", "evidence", "evTitle"],
      },
      {
        text: "If the evidence holds, the charge becomes final and the fee is posted to the ledger; otherwise it is rejected with a reason. A final charge can later be credited by an upheld dispute.",
        flow: [["a5"], ["t2", "t4"], ["t3"]], show: ["sF", "sR", "sC"],
        focus: ["final", "ledger", "sP", "sV", "sF", "sR", "sC"],
      },
    ],
  },

  {
    id: "orders",
    chapter: "12",
    section: "Orders and FP-1 attribution",
    nodes: [
      N("partner", 60, 165, 300, 120, "client", "Partner backend", "POST /v1/orders"),
      N("osvc", 430, 150, 440, 150, "service", "OrderService.create", "services/orders.py", "Idempotency-Key required"),
      N("attr", 940, 150, 460, 150, "service", "AttributionEngine", "services/orders.py", "served · accepted · shown · same SKU · in time"),
      N("fp", 1470, 150, 390, 150, "domain", "FairPremiumRule", "domain/commerce/fees.py", "max(0, min(Δpack, Δsize)) × q_rep"),
      N("lP", 430, 400, 260, 110, "worker", "PENDING", null, "until delivery"),
      N("lA", 780, 400, 260, 110, "worker", "ACCRUED", null, "on DELIVERED"),
      N("lF", 1130, 400, 260, 110, "service", "FINAL", null, "after the return window"),
      N("lR", 780, 570, 260, 100, "danger", "Returns", null, "reverse part of the fee"),
      N("ledger", 1470, 400, 390, 110, "store", "Ledger", null, "accrual · reversal · finalize", { shape: "db" }),
    ],
    texts: [{ id: "note", x: 60, y: 330, w: 300, size: 20, color: "#93a4bd", text: "Lines that came from a swap carry the `suggestionId`." }],
    cards: [
      {
        id: "example", x: 1100, y: 580, w: 760, title: "Worked example IN-1 (docs/08 §C8)",
        lines: [
          "₹56 noodles 280 g → net ₹85 millet 256 g, × 2",
          "Δpack = 85.00 − 56.00 = 29.00",
          "Δsize = 85.00 − 56.00 × 256 / 280 = 33.80",
          "premium = min(29.00, 33.80) × 2 = 58.00",
          "FP-1 fee = 10% = ₹5.80",
        ],
      },
    ],
    edges: [
      E("o1", "partner", "osvc"),
      E("o2", "osvc", "attr"),
      E("o3", "attr", "fp"),
      E("o4", "attr", "lP", { fromSide: "bottom", toSide: "top", curve: 0.12 }),
      E("o5", "lP", "lA"),
      E("o6", "lA", "lF"),
      E("o7", "lA", "lR"),
      E("o8", "lF", "ledger"),
    ],
    beats: [
      {
        text: "At checkout the partner reports the order, with the suggestion id on each line that came from a swap. The attribution engine checks the evidence: served, accepted, shown, the same product, and inside the window.",
        flow: [["o1", "o2"]], packet: "order", focus: ["partner", "osvc", "attr", "note"],
      },
      {
        text: "The fair premium is the smaller of the pack-price difference and the size-normalized difference, times the replaced quantity. FP-1 is ten percent of it, within the contract's caps.",
        flow: [["o3"]], focus: ["attr", "fp"],
      },
      {
        text: "For example, millet noodles at a net ₹85 replace ₹56 noodles: ₹29 more per pack, so for two packs the fee is ₹5.80.",
        say: "For example, millet noodles at a net eighty-five rupees replace fifty-six-rupee noodles: twenty-nine rupees more per pack, so for two packs the fee is five rupees eighty.",
        show: ["example"], focus: ["example", "fp"], lines: { example: [0, 1, 2, 3, 4] },
      },
      {
        text: "The fee is pending until delivery, accrues when the order is delivered, is partly reversed by returns, and becomes final after the return window. Each step is a balanced ledger posting.",
        flow: [["o4", "o5", "o6", "o8"], ["o7"]], packets: ["fee", "return"],
        focus: ["attr", "lP", "lA", "lF", "lR", "ledger"],
      },
    ],
  },

  {
    id: "billing",
    chapter: "13",
    section: "Billing, ledger and disputes",
    nodes: [
      N("close", 60, 160, 360, 140, "worker", "Month close", "hs billing month-close", "or the admin job"),
      N("bsvc", 490, 150, 460, 160, "service", "BillingService.close_month", "services/billing.py", "FINAL FP-1 fees + FINAL RA-1 charges"),
      N("stmt", 1020, 150, 400, 160, "data", "Statement + invoice", null, ["premium fee · recommendation fee", "reversals · credits · tax · total due"]),
      N("wh", 1490, 170, 370, 120, "external", "Webhook", "billing.statement.issued"),
      N("ledger", 490, 410, 460, 150, "store", "Double-entry ledger", "billing.journal_entry / _line", "append-only · balanced by a DB trigger", { shape: "db" }),
      N("tb", 60, 425, 360, 120, "api", "Trial balance", "GET /admin/ledger/trial-balance"),
      N("dispute", 1020, 410, 400, 150, "client", "Dispute", "POST /v1/billing/disputes", "lines or charges, within 15 days"),
      N("resolve", 1490, 410, 370, 150, "api", "Resolve", "POST /admin/disputes/{id}/resolve", "credit note · CREDITED"),
    ],
    cards: [
      {
        id: "postings", x: 60, y: 630, w: 890, title: "domain/commerce/ledger.py — Postings",
        lines: [
          "reco_fee · fee_accrual · fee_reversal · fee_finalize",
          "statement · credit_note · payment",
          "every entry: sum(debits) = sum(credits), or rejected",
        ],
      },
    ],
    edges: [
      E("b1", "close", "bsvc"),
      E("b2", "bsvc", "stmt"),
      E("b3", "stmt", "wh"),
      E("b4", "bsvc", "ledger"),
      E("b5", "ledger", "tb"),
      E("b6", "dispute", "resolve"),
      E("b7", "resolve", "ledger", { points: [[1675, 560], [1675, 598], [720, 598], [720, 560]] }),
    ],
    beats: [
      {
        text: "At month close, BillingService gathers each partner's final FP-1 fees and RA-1 charges into a statement, adds tax and an invoice number, and sends the statement webhook.",
        flow: [["b1", "b2", "b3"]], packet: "statement", focus: ["close", "bsvc", "stmt", "wh"],
      },
      {
        text: "Every money movement is a posting in an append-only, double-entry ledger, and a database trigger rejects any entry that does not balance. The trial balance proves the books add up.",
        flow: [["b4", "b5"]], show: ["postings"], focus: ["bsvc", "ledger", "tb", "postings"],
      },
      {
        text: "Partners can dispute statement lines or single charges within the dispute window. An upheld dispute issues a credit note, the charges become credited, and the partner gets a webhook.",
        flow: [["b6", "b7"]], packet: "credit note", focus: ["dispute", "resolve", "ledger"],
      },
    ],
  },

  {
    id: "bus",
    chapter: "14",
    section: "Outbox, event bus and webhooks",
    nodes: [
      N("tx", 60, 330, 330, 150, "service", "Service transaction", null, ["state change", "+ outbox row"]),
      N("outbox", 460, 345, 270, 120, "store", "txn.outbox", "same transaction", null, { shape: "db" }),
      N("relay", 800, 335, 320, 140, "worker", "OutboxRelay", "platform/bus.py", "≤ 500 rows per pass"),
      N("bus", 1190, 335, 320, 140, "worker", "InProcessEventBus", "platform/bus.py", "Kafka stand-in"),
      N("lake", 1580, 150, 280, 150, "data", "LakeSink", null, [".data/lake/bronze/", "<topic>/dt=<day>"]),
      N("fan", 1580, 520, 280, 150, "worker", "Webhook fan-out", null, "one delivery per endpoint"),
      N("disp", 1190, 640, 320, 180, "worker", "WebhookDispatcher", "every second", ["HMAC-SHA256 signature", "SSRF policy, DNS pinning", "retries up to 24 h"]),
      N("partner", 800, 670, 320, 130, "client", "Partner endpoint", null, "verify_webhook()"),
    ],
    cards: [
      {
        id: "topics", x: 60, y: 560, w: 640, title: "Topics (platform/bus.py)",
        lines: [
          "hs.cart-evaluations.v1   hs.suggestion-events.v1",
          "hs.reco-charges.v1       hs.orders.v1",
          "hs.attribution.v1        hs.billing.v1",
          "hs.catalog-changes.v1    hs.kb-product-changes.v1",
        ],
      },
    ],
    edges: [
      E("x1", "tx", "outbox"),
      E("x2", "outbox", "relay"),
      E("x3", "relay", "bus"),
      E("x4", "bus", "lake"),
      E("x5", "bus", "fan"),
      E("x6", "fan", "disp"),
      E("x7", "disp", "partner"),
    ],
    beats: [
      {
        text: "Services never call consumers directly. They write an outbox row inside their own transaction; the outbox relay publishes committed rows to the in-process bus, which stands in for Kafka.",
        flow: [["x1", "x2", "x3"]], packet: "event", focus: ["tx", "outbox", "relay", "bus"],
      },
      {
        text: "The lake sink appends every message to JSON-lines files, partitioned by topic and day. These files are the training data for the machine-learning models.",
        flow: [["x4"]], packet: "event", show: ["topics"], focus: ["bus", "lake", "topics"],
      },
      {
        text: "The webhook fan-out creates one delivery per subscribed endpoint. The dispatcher signs each body with HMAC-SHA256, checks the URL against SSRF rules, and retries with backoff for up to 24 hours.",
        say: "The webhook fan-out creates one delivery per subscribed endpoint. The dispatcher signs each body with H-mac SHA 256, checks the URL against S S R F rules, and retries with backoff for up to twenty-four hours.",
        flow: [["x5", "x6", "x7"]], packet: "webhook", focus: ["bus", "fan", "disp", "partner"],
      },
    ],
  },

  {
    id: "ml",
    chapter: "15",
    section: "ML models and the LLM gateway",
    nodes: [
      N("lake", 60, 150, 260, 150, "data", "Event lake", ".data/lake"),
      N("export", 380, 150, 320, 150, "worker", "hs ml export · simulate", "seeding/ml_data.py"),
      N("mldata", 760, 150, 260, 150, "data", "ml/data/*.jsonl", null, "features from serving code"),
      N("train", 1080, 150, 330, 150, "ml", "ml/train.py", null, ["scikit-learn → ONNX", "checked · AUC gate"]),
      N("registry", 1470, 150, 390, 150, "ml", "ModelRegistry", "platform/ml/registry.py", "ranker · substitutability · ER"),
      N("gateway", 60, 420, 620, 210, "ml", "LLM gateway", "platform/llm/gateway.py", [
        "T3 category · T4 judge · T5 polish · T7 embeddings",
        "versioned prompts · strict JSON · validators",
        "budgets · cache · an audit row per call",
      ]),
      N("providers", 760, 440, 320, 150, "external", "Mock or Azure OpenAI", null, "product data only, never personal data"),
      N("finetune", 1150, 420, 360, 170, "ml", "ml/finetune", null, ["build_datasets.py", "submit.py → deployment"]),
      N("eval", 1570, 440, 290, 150, "ml", "hs llm eval", null, "acceptance through validators"),
    ],
    texts: [
      {
        id: "fallback", x: 60, y: 690, w: 1800, size: 24, align: "center", pill: true, color: "#cbd5e1",
        text: "No model installed → deterministic heuristics (ranker `heuristic-L2`): the API always works",
      },
    ],
    edges: [
      E("l1", "lake", "export"),
      E("l2", "export", "mldata"),
      E("l3", "mldata", "train"),
      E("l4", "train", "registry"),
      E("g1", "gateway", "providers"),
      E("f1", "mldata", "finetune", { fromSide: "bottom", toSide: "top", curve: 0.12 }),
      E("f2", "finetune", "providers"),
      E("f3", "finetune", "eval"),
    ],
    beats: [
      {
        text: "Three ONNX models run inside the API process: the ranker, substitutability and entity resolution. Their training data comes from the event lake, exported by the `hs ml` commands.",
        say: "Three ONNX models run inside the API process: the ranker, substitutability and entity resolution. Their training data comes from the event lake, exported by the H S M L commands.",
        flow: [["l1", "l2"]], packet: "rows", focus: ["lake", "export", "mldata", "registry"],
      },
      {
        text: "`ml/train.py` fits scikit-learn models, exports them to ONNX, checks them against scikit-learn and installs them; the registry loads them on reload. Without a model, deterministic heuristics take over, so the API always works.",
        say: "The training script fits scikit-learn models, exports them to ONNX, checks them against scikit-learn and installs them; the registry loads them on reload. Without a model, deterministic heuristics take over, so the API always works.",
        flow: [["l3", "l4"]], packet: "model", show: ["fallback"], focus: ["mldata", "train", "registry", "fallback"],
      },
      {
        text: "The LLM gateway serves four offline tasks: category classification, the substitutability judge, explanation polish and embeddings. Every call uses a versioned prompt, strict JSON output, validators, a budget and an audit row.",
        flow: [["g1"]], packet: "prompt", focus: ["gateway", "providers"],
      },
      {
        text: "`ml/finetune` builds chat-format datasets from the same data and submits fine-tuning jobs to Azure OpenAI; `hs llm eval` then compares the new deployment through the production validators.",
        say: "The fine-tuning tools build chat-format datasets from the same data and submit fine-tuning jobs to Azure OpenAI; H S L L M eval then compares the new deployment through the production validators.",
        flow: [["f1", "f2"], ["f3"]], focus: ["mldata", "finetune", "providers", "eval"],
      },
    ],
  },

  {
    id: "security",
    chapter: "16",
    section: "Security and multi-tenancy",
    nodes: [
      ...[
        ["c1", "api", "Identity", ["OAuth 2.0 client credentials", "ES256 JWTs that live 10 minutes", "client secrets stored as PBKDF2 hashes"]],
        ["c2", "api", "Authorization", ["a scope per operation from the contract", "SDK sessions: one cart, ≤ 3 scopes, ≤ 15 min", "admin plane needs the hs.admin scope"]],
        ["c3", "store", "Tenant isolation", ["row-level security on every tenant table", "SET LOCAL ROLE + app.partner_id per transaction", "other tenants' data answers 404"]],
        ["c4", "api", "Request safety", ["validation against openapi.yaml", "rate limits · Idempotency-Key (24 h)", "RFC 9457 problems with stable codes"]],
        ["c5", "worker", "Outbound safety", ["HMAC-signed webhooks with timestamps", "SSRF policy with DNS pinning, no redirects", "LLMs receive product data only"]],
        ["c6", "client", "Client and secrets", ["SDKs render API data as text (XSS-safe)", "strict Content-Security-Policy on /demo", "LLM keys from the environment only", "webhook secrets encrypted at rest"]],
      ].map(([id, kind, title, body], i) => N(id, 60 + (i % 3) * 615, 140 + Math.floor(i / 3) * 220, 570, 190, kind, title, null, body)),
      ...[
        ["q1", "hs_app", "request path"],
        ["q2", "hs_ledger_writer", "money path"],
        ["q3", "hs_kb_writer", "knowledge plane"],
        ["q4", "hs_ops", "control plane, BYPASSRLS"],
        ["q5", "hs_readonly", "analytics, tenant-scoped"],
      ].map(([id, title, body], i) => N(id, 60 + i * 362, 660, 340, 110, "store", title, null, body, { small: true })),
    ],
    texts: [{ id: "rolesTitle", x: 60, y: 620, w: 1000, size: 20, color: "#93a4bd", text: "Five database roles (db/schema.sql); only hs_ops bypasses row-level security" }],
    beats: [
      {
        text: "Security is layered. Partners authenticate with OAuth client credentials and receive ES256 tokens that live ten minutes. Every operation needs its scope, and SDK sessions are limited to one cart and at most three scopes.",
        focus: ["c1", "c2"],
      },
      {
        text: "In PostgreSQL, row-level security isolates tenants. Every transaction sets one of five roles and the partner id, so the request path, the money path and the knowledge plane can only touch what they own.",
        show: ["rolesTitle", ...range("q", 5)], focus: ["c3", "rolesTitle", ...range("q", 5)],
      },
      {
        text: "Requests are validated against the contract, rate-limited and idempotent. Webhooks are signed and SSRF-checked, the SDKs render API data only as text, and LLM keys never live in config files.",
        say: "Requests are validated against the contract, rate-limited and idempotent. Webhooks are signed and S S R F checked, the SDKs render API data only as text, and L L M keys never live in config files.",
        focus: ["c4", "c5", "c6"],
      },
    ],
  },

  {
    id: "local",
    chapter: "17",
    section: "Run it and test it",
    groups: [{ id: "tests", x: 60, y: 420, w: 1260, h: 260, label: "tests/ — pytest" }],
    nodes: [
      N("runlocal", 60, 150, 520, 200, "neutral", "scripts/run_local.py", null, ["start PGlite (tools/devdb)", "hs seed --demo on an empty database", "hs serve: /v1 · /admin · /mcp · /demo"]),
      N("devdb", 660, 165, 360, 140, "store", "PGlite dev database", "tools/devdb/server.mjs", "PostgreSQL in WebAssembly", { shape: "db" }),
      N("api", 1100, 165, 360, 140, "api", "API + workers", "hs serve · uvicorn", "one process"),
      N("demo", 1540, 165, 320, 140, "client", "Demo shop", "http://127.0.0.1:8080/demo"),
      ...[
        ["t1", "domain", "domain/", "rules, scores, fees; no database"],
        ["t2", "service", "platform/", "services and flows on fresh PGlite"],
        ["t3", "api", "api/", "HTTP flows, security, contract"],
        ["t4", "client", "sdk/", "fail-open, retries, webhooks"],
        ["t5", "ml", "ml/", "training and fine-tuning tools"],
      ].map(([id, kind, title, body], i) => N(id, 85 + i * 245, 470, 228, 180, kind, title, null, body, { small: true })),
      N("smoke", 1400, 420, 460, 160, "worker", "scripts/smoke_test.py", null, "label → RA-1 → FP-1 → statement → dispute → webhooks → MCP"),
      N("recorder", 1400, 620, 460, 120, "neutral", "tools/demo-recorder", null, "the demo video and this walkthrough"),
    ],
    texts: [
      {
        id: "cmd", x: 60, y: 730, w: 1260, size: 20, color: "#cbd5e1",
        text: "`python -m pytest tests -q -m \"not integration\"` · `-m integration`: a fresh in-memory database per module",
      },
    ],
    edges: [E("w1", "runlocal", "devdb"), E("w2", "devdb", "api"), E("w3", "api", "demo")],
    beats: [
      {
        text: "Locally, `scripts/run_local.py` starts PostgreSQL in WebAssembly, seeds the knowledge base and the demo partners, and serves the API, the admin plane, MCP and the demo shop from one process.",
        say: "Locally, the run-local script starts PostgreSQL in WebAssembly, seeds the knowledge base and the demo partners, and serves the API, the admin plane, MCP and the demo shop from one process.",
        flow: [["w1", "w2", "w3"]], focus: ["runlocal", "devdb", "api", "demo"],
      },
      {
        text: "Tests mirror the layers: pure domain tests, platform and API flows on a fresh in-memory database per module, the SDK, and the ML tooling.",
        show: ["cmd"], focus: ["tests", ...range("t", 5), "cmd"],
      },
      {
        text: "The smoke test drives a running stack end to end, from the label to the statement, and the demo recorder produced both the demo video and this walkthrough.",
        focus: ["smoke", "recorder"],
      },
    ],
  },

  {
    id: "recap",
    chapter: null,
    section: "Recap",
    lists: [
      {
        id: "recap", x: 120, y: 250, w: 1680, size: 30,
        items: [
          "An **offline knowledge plane** precomputes scores, a swap graph and explanations",
          "A **cache-only hot path** answers carts in milliseconds and fails open",
          "**Events, orders and a double-entry ledger** turn accepted swaps into transparent fees",
          "**Strict layers** keep the rules pure: api → platform → domain; LLMs assist offline only",
        ],
      },
    ],
    texts: [
      {
        id: "readnext", x: 120, y: 600, w: 1680, size: 26, color: "#cbd5e1",
        text: "Read next: `docs/07-reference-implementation.md` · `docs/09-client-app-integration.md` · `docs/10-codebase-guide.md`",
      },
    ],
    beats: [
      {
        text: "To recap: the knowledge plane precomputes scores, a swap graph and explanations offline. The hot path reads only the cache, answers in milliseconds and fails open.",
        on: { recap: [0, 1] },
      },
      {
        text: "Events, orders and a double-entry ledger turn accepted swaps into transparent fees, and strict layers keep the business rules pure and tested.",
        on: { recap: [2, 3] },
      },
      {
        text: "For the details, read documents 07, 09 and 10 in the repository. Thanks for watching.",
        say: "For the details, read documents seven, nine and ten in the repository. Thanks for watching.",
        show: ["readnext"], on: { recap: [0, 1, 2, 3] },
      },
    ],
  },
];
