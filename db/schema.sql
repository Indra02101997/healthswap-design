-- =====================================================================================================================
-- HealthSwap reference schema (PostgreSQL 16+)
--
-- Planes (schemas):
--   kb      Global food knowledge base (non-personal): products, versions, nutrition, ingredients, additives, allergens
--   reg     Jurisdiction packs: regulatory reference data with citations
--   rec     Scoring models, product scores, health profiles, swap graph, explanations, embeddings
--   tenant  Partner-scoped data: partners, stores, catalog mapping, preferences, consent, DSAR   (row-level security)
--   txn     Partner-scoped transactions: evaluations, suggestions, events, orders, attribution, recommendation-add
--           charges (RA-1), idempotency keys, outbox, webhook deliveries                          (row-level security)
--   billing Double-entry ledger, statements, disputes                                            (append-only journal)
--   audit   Change log
--
-- Fee rules (contracted per partner, both can be enabled at the same time):
--   FP-1  10% of the fair premium on delivered, non-returned accepted swaps   (txn.attribution, HLD section 9.1-9.9)
--   RA-1  Fixed fee (default INR 1.00) every time a shopper adds a HealthSwap-recommended (labeled) product to the
--         cart                                                               (txn.reco_charge, HLD section 9.10)
--
-- Database roles used by the reference implementation (src/): every unit of work runs
--   BEGIN; SET LOCAL ROLE <role>; SELECT set_config('app.partner_id', '<uuid>', true); ...; COMMIT;
-- so that row-level security is enforced even when the login user is privileged.
--
-- Deployment note: for a single-database dev setup this script runs as-is. In production, split kb/reg/rec (global,
-- replicated) from tenant/txn/billing/audit (one database per geography) and drop the cross-database foreign keys
-- marked "XDB".
--
-- Requires extensions: pgcrypto (gen_random_uuid), pg_trgm (fuzzy matching), vector (pgvector >= 0.5 for HNSW).
-- Seed rows marked ILLUSTRATIVE must be verified against the cited official documents before production use.
-- =====================================================================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

CREATE SCHEMA IF NOT EXISTS kb;
CREATE SCHEMA IF NOT EXISTS reg;
CREATE SCHEMA IF NOT EXISTS rec;
CREATE SCHEMA IF NOT EXISTS tenant;
CREATE SCHEMA IF NOT EXISTS txn;
CREATE SCHEMA IF NOT EXISTS billing;
CREATE SCHEMA IF NOT EXISTS audit;

-- =====================================================================================================================
-- kb: knowledge base
-- =====================================================================================================================

CREATE TABLE kb.source (
    source_id        smallint PRIMARY KEY,
    code             text     NOT NULL UNIQUE,          -- OFF, USDA_FDC_BRANDED, GS1_DATAKART, PARTNER_FEED, LABEL_OCR, ...
    name             text     NOT NULL,
    license_code     text     NOT NULL,                 -- ODbL-1.0, CC0-1.0, CONTRACT, PARTNER-OWNED, INTERNAL, ...
    trust_tier       smallint NOT NULL CHECK (trust_tier BETWEEN 1 AND 5),
    share_alike      boolean  NOT NULL DEFAULT false,
    attribution_text text,
    homepage_url     text
);

CREATE TABLE kb.source_record (
    source_record_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source_id        smallint    NOT NULL REFERENCES kb.source (source_id),
    external_id      text        NOT NULL,              -- e.g. OFF code, FDC id, partner SKU, image hash
    partner_id       uuid,                              -- set when the record is partner-owned data
    fetched_at       timestamptz NOT NULL,
    payload_uri      text        NOT NULL,              -- pointer to the raw object in the lake (bronze)
    payload_sha256   bytea       NOT NULL,
    UNIQUE (source_id, external_id, payload_sha256)
);

CREATE TABLE kb.brand (
    brand_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    name               text NOT NULL,
    normalized_name    text NOT NULL,
    owner_company      text,
    gs1_company_prefix text,
    UNIQUE NULLS NOT DISTINCT (normalized_name, owner_company)
);

CREATE TABLE kb.category (
    category_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    parent_id        uuid REFERENCES kb.category (category_id),
    code             text NOT NULL UNIQUE,              -- e.g. 'snacks.savoury.namkeen'
    name             text NOT NULL,
    gpc_brick_code   text,                              -- GS1 GPC brick for interoperability
    physical_state   text CHECK (physical_state IN ('SOLID', 'LIQUID', 'MIXED')),
    scoring_category text NOT NULL DEFAULT 'GENERAL'    -- model routing: GENERAL, BEVERAGE, FAT_OIL_NUT, CHEESE, RED_MEAT, ...
);

CREATE TABLE kb.product_family (
    family_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    brand_id  uuid REFERENCES kb.brand (brand_id),
    name      text NOT NULL,
    notes     text
);

CREATE TABLE kb.product (
    product_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    primary_gtin       char(14) UNIQUE,                 -- normalized GTIN-14; NULL for non-GTIN items
    brand_id           uuid REFERENCES kb.brand (brand_id),
    family_id          uuid REFERENCES kb.product_family (family_id),
    category_id        uuid REFERENCES kb.category (category_id),
    name               text    NOT NULL,
    generic_name       text,
    net_quantity       numeric(12, 3),
    net_quantity_unit  text CHECK (net_quantity_unit IN ('g', 'ml', 'pcs')),
    pack_count         integer NOT NULL DEFAULT 1 CHECK (pack_count > 0),
    physical_state     text CHECK (physical_state IN ('SOLID', 'LIQUID', 'MIXED')),
    markets            text[]  NOT NULL DEFAULT '{}',   -- ISO 3166-1 alpha-2
    country_of_origin  char(2),
    is_private_label   boolean NOT NULL DEFAULT false,
    is_generic         boolean NOT NULL DEFAULT false,  -- loose produce / staples mapped to composition tables
    status             text    NOT NULL DEFAULT 'ACTIVE'
                       CHECK (status IN ('ACTIVE', 'DISCONTINUED', 'MERGED', 'QUARANTINED')),
    merged_into        uuid REFERENCES kb.product (product_id),
    current_version_id bigint,                          -- FK added after kb.product_version
    attributes         jsonb   NOT NULL DEFAULT '{}',   -- substitutability attributes: form, flavourFamily, useOccasion, cuisine, brandTier
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX product_name_trgm_idx ON kb.product USING gin (name gin_trgm_ops);
CREATE INDEX product_category_idx  ON kb.product (category_id);
CREATE INDEX product_markets_idx   ON kb.product USING gin (markets);
CREATE INDEX product_family_idx    ON kb.product (family_id);

CREATE TABLE kb.product_identifier (
    id_type    text NOT NULL CHECK (id_type IN ('GTIN', 'PLU', 'FDC_ID', 'OFF_CODE', 'ONDC_ITEM', 'INTERNAL')),
    id_value   text NOT NULL,
    product_id uuid NOT NULL REFERENCES kb.product (product_id),
    market     char(2),
    PRIMARY KEY (id_type, id_value)
);
CREATE INDEX product_identifier_product_idx ON kb.product_identifier (product_id);

-- One row per label version (SCD-2). Reformulations create new versions.
CREATE TABLE kb.product_version (
    version_id         bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    product_id         uuid        NOT NULL REFERENCES kb.product (product_id),
    valid_from         timestamptz NOT NULL,
    valid_to           timestamptz,                     -- NULL = current
    label_hash         bytea       NOT NULL,            -- hash of normalized label content
    serving_size       numeric(10, 3),
    serving_unit       text CHECK (serving_unit IN ('g', 'ml')),
    servings_per_pack  numeric(10, 2),
    carb_definition    text CHECK (carb_definition IN ('TOTAL_INCL_FIBRE', 'AVAILABLE')),
    ingredients_text   text,
    ingredients_lang   text,
    allergen_statement text,
    nova_group         smallint CHECK (nova_group BETWEEN 1 AND 4),
    nova_confidence    numeric(4, 3),
    completeness       numeric(4, 3) NOT NULL DEFAULT 0 CHECK (completeness BETWEEN 0 AND 1),
    confidence         numeric(4, 3) NOT NULL DEFAULT 0 CHECK (confidence BETWEEN 0 AND 1),
    data_sufficiency   text NOT NULL DEFAULT 'INSUFFICIENT'
                       CHECK (data_sufficiency IN ('SUFFICIENT', 'PARTIAL', 'INSUFFICIENT')),
    verification       text NOT NULL DEFAULT 'UNVERIFIED'
                       CHECK (verification IN ('UNVERIFIED', 'MACHINE_VALIDATED', 'CURATOR_VERIFIED', 'BRAND_VERIFIED')),
    last_verified_at   timestamptz,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE UNIQUE INDEX product_version_current_uq ON kb.product_version (product_id) WHERE valid_to IS NULL;

ALTER TABLE kb.product
    ADD CONSTRAINT product_current_version_fk
    FOREIGN KEY (current_version_id) REFERENCES kb.product_version (version_id);

CREATE TABLE kb.nutrient (
    nutrient_code text PRIMARY KEY,                     -- INFOODS tagname (ENERC_KCAL, PROCNT, ...) or custom (SALT, FVLN_PCT)
    name          text NOT NULL,
    unit          text NOT NULL,                        -- canonical unit: kcal, g, mg, ug, %
    role          text NOT NULL CHECK (role IN ('LIMIT', 'ENCOURAGE', 'NEUTRAL'))
);

CREATE TABLE kb.product_nutrient (
    version_id       bigint NOT NULL REFERENCES kb.product_version (version_id) ON DELETE CASCADE,
    nutrient_code    text   NOT NULL REFERENCES kb.nutrient (nutrient_code),
    basis            text   NOT NULL CHECK (basis IN ('PER_100G', 'PER_100ML', 'PER_SERVING')),
    amount           numeric(12, 4) NOT NULL CHECK (amount >= 0),
    is_declared      boolean NOT NULL DEFAULT true,     -- false = derived (e.g. salt from sodium)
    is_estimated     boolean NOT NULL DEFAULT false,    -- e.g. fruit/veg/legume % estimated from ingredients
    source_record_id bigint REFERENCES kb.source_record (source_record_id),
    confidence       numeric(4, 3) NOT NULL DEFAULT 1 CHECK (confidence BETWEEN 0 AND 1),
    PRIMARY KEY (version_id, nutrient_code, basis)
);

CREATE TABLE kb.ingredient (
    ingredient_id  uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_name text NOT NULL UNIQUE,
    parent_id      uuid REFERENCES kb.ingredient (ingredient_id),
    is_additive    boolean NOT NULL DEFAULT false,
    flags          text[]  NOT NULL DEFAULT '{}',       -- ADDED_SUGAR, REFINED_FLOUR, WHOLE_GRAIN, PHO, NNS, FRUIT_VEG_LEGUME, ...
    allergens      text[]  NOT NULL DEFAULT '{}'        -- allergens implied by the ingredient (whey -> MILK, semolina -> WHEAT)
);

CREATE TABLE kb.ingredient_alias (
    alias         text NOT NULL,
    lang          text NOT NULL,                        -- BCP 47, e.g. en, hi
    ingredient_id uuid NOT NULL REFERENCES kb.ingredient (ingredient_id),
    PRIMARY KEY (alias, lang)
);

CREATE TABLE kb.product_ingredient (
    version_id      bigint   NOT NULL REFERENCES kb.product_version (version_id) ON DELETE CASCADE,
    position        smallint NOT NULL,
    parent_position smallint,                           -- sub-ingredient of another position
    ingredient_id   uuid REFERENCES kb.ingredient (ingredient_id),   -- NULL when unresolved
    raw_text        text     NOT NULL,
    percent         numeric(5, 2) CHECK (percent IS NULL OR percent BETWEEN 0 AND 100),
    PRIMARY KEY (version_id, position)
);

CREATE TABLE kb.additive (
    additive_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ins_number         text UNIQUE,                     -- Codex INS, e.g. '129', '322', '924a'
    e_number           text UNIQUE,                     -- EU, e.g. 'E129'
    canonical_name     text   NOT NULL,
    us_names           text[] NOT NULL DEFAULT '{}',    -- e.g. {'FD&C Red No. 40','Red 40'}
    cas_number         text,
    functional_classes text[] NOT NULL DEFAULT '{}'     -- Codex functional classes
);

CREATE TABLE kb.additive_alias (
    alias       text NOT NULL,
    lang        text NOT NULL,
    additive_id uuid NOT NULL REFERENCES kb.additive (additive_id),
    PRIMARY KEY (alias, lang)
);

CREATE TABLE kb.product_additive (
    version_id    bigint NOT NULL REFERENCES kb.product_version (version_id) ON DELETE CASCADE,
    additive_id   uuid   NOT NULL REFERENCES kb.additive (additive_id),
    detected_from text   NOT NULL CHECK (detected_from IN ('INGREDIENTS', 'DECLARED', 'OCR', 'BRAND')),
    PRIMARY KEY (version_id, additive_id)
);

CREATE TABLE kb.allergen (
    allergen_code text PRIMARY KEY,
    name          text NOT NULL
);

CREATE TABLE kb.product_allergen (
    version_id    bigint NOT NULL REFERENCES kb.product_version (version_id) ON DELETE CASCADE,
    allergen_code text   NOT NULL REFERENCES kb.allergen (allergen_code),
    presence      text   NOT NULL CHECK (presence IN ('CONTAINS', 'MAY_CONTAIN', 'FREE_FROM')),
    derived_from  text   NOT NULL CHECK (derived_from IN ('STATEMENT', 'INGREDIENTS', 'BRAND', 'CURATOR')),
    verified      boolean NOT NULL DEFAULT false,
    PRIMARY KEY (version_id, allergen_code)
);

CREATE TABLE kb.mark (
    mark_code    text PRIMARY KEY,
    jurisdiction text,
    kind         text NOT NULL CHECK (kind IN ('DIETARY', 'CERTIFICATION', 'CLAIM', 'REGULATORY_SYMBOL')),
    description  text
);

CREATE TABLE kb.product_mark (
    version_id bigint NOT NULL REFERENCES kb.product_version (version_id) ON DELETE CASCADE,
    mark_code  text   NOT NULL REFERENCES kb.mark (mark_code),
    is_inferred boolean NOT NULL DEFAULT false,         -- e.g. Jain-friendly inferred from ingredients
    verified   boolean NOT NULL DEFAULT false,
    PRIMARY KEY (version_id, mark_code)
);

CREATE TABLE kb.product_media (
    media_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    product_id   uuid     NOT NULL REFERENCES kb.product (product_id),
    kind         text     NOT NULL CHECK (kind IN ('FRONT', 'BACK', 'NUTRITION', 'INGREDIENTS', 'OTHER')),
    storage_uri  text     NOT NULL,
    license_code text     NOT NULL,
    source_id    smallint NOT NULL REFERENCES kb.source (source_id),
    phash        bigint,                                -- perceptual hash for dedupe / matching
    captured_at  timestamptz
);
CREATE INDEX product_media_product_idx ON kb.product_media (product_id);

-- Field-level provenance: every candidate value with its source, tier and licence; one is selected.
CREATE TABLE kb.field_provenance (
    version_id       bigint   NOT NULL REFERENCES kb.product_version (version_id) ON DELETE CASCADE,
    field_path       text     NOT NULL,                 -- e.g. 'nutrient.SUGAR.PER_100G', 'ingredients_text'
    source_record_id bigint   NOT NULL REFERENCES kb.source_record (source_record_id),
    value_json       jsonb    NOT NULL,
    trust_tier       smallint NOT NULL CHECK (trust_tier BETWEEN 1 AND 5),
    license_code     text     NOT NULL,
    is_selected      boolean  NOT NULL,
    conflict         boolean  NOT NULL DEFAULT false,
    PRIMARY KEY (version_id, field_path, source_record_id)
);

CREATE TABLE kb.recall (
    recall_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    jurisdiction   text        NOT NULL,
    authority      text        NOT NULL,                -- FDA, FSSAI, FSA, RASFF
    reference      text        NOT NULL,
    product_id     uuid REFERENCES kb.product (product_id),
    gtin           char(14),
    description    text        NOT NULL,
    classification text,
    published_at   timestamptz NOT NULL,
    suppress_until timestamptz,
    UNIQUE NULLS NOT DISTINCT (authority, reference, gtin)
);

-- Curation queue for nutrition scientists and data curators (FR-13). Partner match reviews stay in tenant.catalog_item.
CREATE TABLE kb.curation_task (
    task_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    kind        text NOT NULL CHECK (kind IN ('FIELD_CONFLICT', 'QA_REVIEW', 'ER_REVIEW', 'OCR_REVIEW', 'RECALL_CONFIRM',
                                              'COMPLIANCE')),
    priority    text NOT NULL DEFAULT 'P3' CHECK (priority IN ('P1', 'P2', 'P3')),
    status      text NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'IN_PROGRESS', 'RESOLVED', 'DISMISSED')),
    product_id  uuid REFERENCES kb.product (product_id),
    version_id  bigint REFERENCES kb.product_version (version_id),
    rule_code   text,                                   -- e.g. QA-01, QA-14, ER-0.82
    payload     jsonb NOT NULL DEFAULT '{}',
    created_at  timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolved_by text,
    resolution  jsonb
);
CREATE INDEX curation_task_open_idx ON kb.curation_task (status, priority, created_at) WHERE status IN ('OPEN', 'IN_PROGRESS');

CREATE VIEW kb.v_current_nutrition AS
SELECT p.product_id, p.primary_gtin, v.version_id, pn.nutrient_code, pn.basis, pn.amount, n.unit, pn.is_estimated
FROM kb.product p
JOIN kb.product_version v   ON v.version_id = p.current_version_id
JOIN kb.product_nutrient pn ON pn.version_id = v.version_id
JOIN kb.nutrient n          ON n.nutrient_code = pn.nutrient_code;

-- =====================================================================================================================
-- reg: jurisdiction packs
-- =====================================================================================================================

CREATE TABLE reg.jurisdiction (
    code  text PRIMARY KEY,                             -- IN, US, EU, GB, CA, AU, NZ, SG, ...
    name  text NOT NULL,
    notes text
);

CREATE TABLE reg.document (
    document_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    jurisdiction   text NOT NULL REFERENCES reg.jurisdiction (code),
    authority      text NOT NULL,
    title          text NOT NULL,
    citation       text NOT NULL UNIQUE,
    version_label  text,
    url            text,
    effective_from date,
    retrieved_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reg.additive_status (
    additive_id    uuid NOT NULL REFERENCES kb.additive (additive_id),
    jurisdiction   text NOT NULL REFERENCES reg.jurisdiction (code),
    status         text NOT NULL CHECK (status IN ('PERMITTED', 'RESTRICTED', 'PHASE_OUT', 'PROHIBITED', 'NOT_LISTED')),
    conditions     text,
    effective_from date NOT NULL,
    effective_to   date,
    document_id    uuid REFERENCES reg.document (document_id),
    PRIMARY KEY (additive_id, jurisdiction, effective_from)
);

CREATE TABLE reg.additive_hazard (
    additive_id  uuid    NOT NULL REFERENCES kb.additive (additive_id),
    authority    text    NOT NULL,                      -- IARC, JECFA, EFSA, WHO, FDA, FSSAI, EU
    finding      text    NOT NULL,
    concern_tier char(1) NOT NULL CHECK (concern_tier IN ('A', 'B', 'C', 'D')),
    as_of        date    NOT NULL,
    document_id  uuid REFERENCES reg.document (document_id),
    PRIMARY KEY (additive_id, authority, as_of)
);

CREATE TABLE reg.allergen_rule (
    jurisdiction          text    NOT NULL REFERENCES reg.jurisdiction (code),
    allergen_code         text    NOT NULL REFERENCES kb.allergen (allergen_code),
    mandatory_declaration boolean NOT NULL,
    threshold_note        text,
    document_id           uuid REFERENCES reg.document (document_id),
    PRIMARY KEY (jurisdiction, allergen_code)
);

CREATE TABLE reg.claim_threshold (
    jurisdiction            text NOT NULL REFERENCES reg.jurisdiction (code),
    claim_code              text NOT NULL,              -- LOW_SUGAR, SUGAR_FREE, LOW_SODIUM, HIGH_FIBRE, REDUCED, ...
    nutrient_code           text NOT NULL REFERENCES kb.nutrient (nutrient_code),
    physical_state          text NOT NULL CHECK (physical_state IN ('SOLID', 'LIQUID', 'ANY')),
    basis                   text NOT NULL CHECK (basis IN ('PER_100G', 'PER_100ML', 'PER_100KCAL', 'PER_SERVING', 'RELATIVE')),
    max_value               numeric(12, 4),
    min_value               numeric(12, 4),
    min_relative_difference numeric(5, 4),              -- e.g. 0.25 for comparative claims
    document_id             uuid REFERENCES reg.document (document_id),
    PRIMARY KEY (jurisdiction, claim_code, nutrient_code, physical_state)
);

CREATE TABLE reg.daily_value (
    jurisdiction  text NOT NULL REFERENCES reg.jurisdiction (code),
    population    text NOT NULL DEFAULT 'ADULT',
    nutrient_code text NOT NULL REFERENCES kb.nutrient (nutrient_code),
    amount        numeric(12, 4) NOT NULL,
    document_id   uuid REFERENCES reg.document (document_id),
    PRIMARY KEY (jurisdiction, population, nutrient_code)
);

CREATE TABLE reg.fop_threshold (
    jurisdiction   text NOT NULL REFERENCES reg.jurisdiction (code),
    scheme         text NOT NULL,                       -- UK_TRAFFIC_LIGHT, US_FDA_FOP_PROPOSED, CA_HIGH_IN, MX_NOM051, ...
    nutrient_code  text NOT NULL REFERENCES kb.nutrient (nutrient_code),
    physical_state text NOT NULL CHECK (physical_state IN ('SOLID', 'LIQUID', 'ANY')),
    basis          text NOT NULL CHECK (basis IN ('PER_100G', 'PER_100ML', 'PER_SERVING', 'PER_SERVING_PCT_DV')),
    low_max        numeric(12, 4),                      -- value <= low_max is "Low"
    high_min       numeric(12, 4),                      -- value above (or at, if high_inclusive) high_min is "High"
    high_inclusive boolean NOT NULL DEFAULT false,
    document_id    uuid REFERENCES reg.document (document_id),
    PRIMARY KEY (jurisdiction, scheme, nutrient_code, physical_state)
);

-- =====================================================================================================================
-- rec: scoring, health profiles, swap graph, explanations, embeddings
-- =====================================================================================================================

CREATE TABLE rec.scoring_model (
    model_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    code            text  NOT NULL,                     -- NUTRISCORE_2023, HSR, IN_INR_DRAFT, US_FDA_FOP, US_FDA_HEALTHY, UK_NPM_2004, NOVA, ADDITIVE_TIERS, HSI
    version         text  NOT NULL,
    jurisdiction    text REFERENCES reg.jurisdiction (code),
    params          jsonb NOT NULL DEFAULT '{}',
    status          text  NOT NULL CHECK (status IN ('PREVIEW', 'ACTIVE', 'DEPRECATED')),
    published_at    timestamptz,
    methodology_url text,
    UNIQUE (code, version)
);

CREATE TABLE rec.product_score (
    version_id  bigint NOT NULL REFERENCES kb.product_version (version_id) ON DELETE CASCADE,
    model_id    uuid   NOT NULL REFERENCES rec.scoring_model (model_id),
    market      text   NOT NULL,
    score       numeric(8, 3),
    grade       text,
    components  jsonb  NOT NULL DEFAULT '{}',
    inputs_hash bytea  NOT NULL,
    computed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (version_id, model_id, market)
);

-- Denormalized read model (one row per product and market); mirrored to the hot cache as hp:{market}:{productId}.
CREATE TABLE rec.health_profile (
    product_id         uuid   NOT NULL REFERENCES kb.product (product_id),
    market             text   NOT NULL,
    version_id         bigint NOT NULL REFERENCES kb.product_version (version_id),
    hsi                numeric(5, 2) CHECK (hsi BETWEEN 0 AND 100),
    hsi_grade          char(1) CHECK (hsi_grade IN ('A', 'B', 'C', 'D', 'E')),
    primary_model_code text,
    primary_score      numeric(8, 3),
    primary_grade      text,
    nova_group         smallint CHECK (nova_group BETWEEN 1 AND 4),
    flags              text[]  NOT NULL DEFAULT '{}',
    additive_tiers     jsonb   NOT NULL DEFAULT '{}',   -- {"A":0,"B":1,"C":0,"D":3}
    us_fda_healthy     boolean,
    data_sufficient    boolean NOT NULL,
    eligible           boolean NOT NULL DEFAULT true,   -- false when Tier A, recalled or quarantined
    profile_version    text    NOT NULL,
    computed_at        timestamptz NOT NULL DEFAULT now(),
    category_id        uuid REFERENCES kb.category (category_id),
    profile_doc        jsonb   NOT NULL DEFAULT '{}',   -- full API representation (HealthProfile schema) for cache rebuilds
    PRIMARY KEY (product_id, market)
);

-- Per-market category statistics; hsi_p60 is the target-line threshold (categoryCeiling, doc 03 section 3).
CREATE TABLE rec.category_stats (
    market        text NOT NULL,
    category_id   uuid NOT NULL REFERENCES kb.category (category_id),
    product_count integer NOT NULL,
    hsi_p60       numeric(5, 2),
    hsi_mean      numeric(5, 2),
    computed_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (market, category_id)
);

-- Audit trail for every LLM call made through the LLM gateway (doc 03 section 9.3). Never contains personal data.
CREATE TABLE rec.llm_audit (
    audit_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    task            text NOT NULL CHECK (task IN ('T1', 'T2', 'T3', 'T4', 'T5', 'T7', 'T8')),
    prompt_version  text NOT NULL,
    model_ref       text NOT NULL,                      -- provider/deployment, e.g. azure-openai/hs-t5-explainer-ft
    input_hash      bytea NOT NULL,
    output          jsonb,
    validation      jsonb NOT NULL DEFAULT '{}',        -- per-validator pass/fail
    accepted        boolean NOT NULL,
    tokens_in       integer,
    tokens_out      integer,
    latency_ms      integer,
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX llm_audit_task_idx ON rec.llm_audit (task, created_at);

CREATE TABLE rec.category_substitution (
    market           text NOT NULL,
    from_category_id uuid NOT NULL REFERENCES kb.category (category_id),
    to_category_id   uuid NOT NULL REFERENCES kb.category (category_id),
    approved_by      text NOT NULL,
    approved_at      timestamptz NOT NULL DEFAULT now(),
    notes            text,
    PRIMARY KEY (market, from_category_id, to_category_id)
);

CREATE TABLE rec.swap_graph_version (
    graph_version     text PRIMARY KEY,                 -- e.g. 'IN-2026-11-04T02:00Z'
    market            text NOT NULL,
    rules_version     text NOT NULL,
    hsi_model_version text NOT NULL,
    status            text NOT NULL CHECK (status IN ('BUILDING', 'VALIDATED', 'ACTIVE', 'RETIRED', 'FAILED')),
    edge_count        bigint,
    validation_report jsonb,
    created_at        timestamptz NOT NULL DEFAULT now(),
    activated_at      timestamptz
);
CREATE UNIQUE INDEX swap_graph_one_active_per_market ON rec.swap_graph_version (market) WHERE status = 'ACTIVE';

-- Partitioned by graph version so whole versions can be attached/detached/dropped cheaply (blue/green).
CREATE TABLE rec.swap_edge (
    graph_version    text   NOT NULL REFERENCES rec.swap_graph_version (graph_version),
    from_product_id  uuid   NOT NULL,
    to_product_id    uuid   NOT NULL,
    health_delta     numeric(6, 2) NOT NULL CHECK (health_delta > 0),
    substitutability numeric(4, 3) NOT NULL CHECK (substitutability BETWEEN 0 AND 1),
    reason_codes     text[] NOT NULL,
    features         jsonb  NOT NULL DEFAULT '{}',
    PRIMARY KEY (graph_version, from_product_id, to_product_id)
) PARTITION BY LIST (graph_version);
CREATE TABLE rec.swap_edge_default PARTITION OF rec.swap_edge DEFAULT;

CREATE VIEW rec.v_active_swap_edge AS
SELECT e.*
FROM rec.swap_edge e
JOIN rec.swap_graph_version g ON g.graph_version = e.graph_version
WHERE g.status = 'ACTIVE';

CREATE TABLE rec.explanation (
    market          text  NOT NULL,
    locale          text  NOT NULL,
    from_product_id uuid  NOT NULL,
    to_product_id   uuid  NOT NULL,
    facts_hash      bytea NOT NULL,
    short_text      text  NOT NULL,
    details         jsonb NOT NULL,
    generator       text  NOT NULL CHECK (generator IN ('TEMPLATE', 'LLM_VALIDATED')),
    model_ref       text,
    prompt_version  text,
    validated_at    timestamptz NOT NULL,
    PRIMARY KEY (market, locale, from_product_id, to_product_id, facts_hash)
);

-- Dimension must match the chosen embedding model; create one table/column per model if dimensions differ.
CREATE TABLE rec.product_embedding (
    product_id uuid NOT NULL REFERENCES kb.product (product_id),
    model_ref  text NOT NULL,
    embedding  vector(1024) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (product_id, model_ref)
);
CREATE INDEX product_embedding_hnsw_idx ON rec.product_embedding USING hnsw (embedding vector_cosine_ops);

-- =====================================================================================================================
-- tenant: partner-scoped data (row-level security enabled at the end of this script)
-- =====================================================================================================================

CREATE TABLE tenant.partner (
    partner_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    display_name text   NOT NULL,
    legal_name   text   NOT NULL,
    geography    text   NOT NULL CHECK (geography IN ('IN', 'US', 'EU', 'GB', 'CA', 'AU')),
    markets      text[] NOT NULL,
    status       text   NOT NULL DEFAULT 'SANDBOX'
                 CHECK (status IN ('SANDBOX', 'SHADOW', 'LIVE', 'SUSPENDED', 'OFFBOARDED')),
    config       jsonb  NOT NULL DEFAULT '{}',          -- versioned tenant config (see HLD section 18)
    created_at   timestamptz NOT NULL DEFAULT now()
);

-- OAuth client metadata only; secrets live in the identity provider / secret store, never here.
CREATE TABLE tenant.api_client (
    client_id            text PRIMARY KEY,
    partner_id           uuid   NOT NULL REFERENCES tenant.partner (partner_id),
    scopes               text[] NOT NULL,
    status               text   NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'ROTATING', 'REVOKED')),
    mtls_cert_thumbprint text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    last_rotated_at      timestamptz
);

CREATE TABLE tenant.store (
    partner_id  uuid NOT NULL REFERENCES tenant.partner (partner_id),
    store_id    text NOT NULL,
    market      text NOT NULL,
    postal_code text,
    timezone    text NOT NULL,
    is_active   boolean NOT NULL DEFAULT true,
    PRIMARY KEY (partner_id, store_id)
);

CREATE TABLE tenant.catalog_item (
    partner_id        uuid NOT NULL REFERENCES tenant.partner (partner_id),
    partner_sku       text NOT NULL,
    gtin              char(14),
    title             text NOT NULL,
    brand_text        text,
    category_path     text[],
    net_quantity      numeric(12, 3),
    net_quantity_unit text,
    product_id        uuid REFERENCES kb.product (product_id),   -- XDB: logical FK in production
    match_method      text CHECK (match_method IN ('GTIN_EXACT', 'GTIN_NORMALIZED', 'FUZZY', 'MANUAL', 'NONE')),
    match_confidence  numeric(4, 3) CHECK (match_confidence BETWEEN 0 AND 1),
    match_state       text NOT NULL DEFAULT 'PENDING'
                      CHECK (match_state IN ('PENDING', 'AUTO_MATCHED', 'NEEDS_REVIEW', 'CONFIRMED', 'REJECTED',
                                             'UNMATCHED', 'PENDING_ENRICHMENT')),
    raw_payload_uri   text,
    is_listed         boolean NOT NULL DEFAULT true,
    updated_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (partner_id, partner_sku)
);
CREATE INDEX catalog_item_product_idx ON tenant.catalog_item (partner_id, product_id);
CREATE INDEX catalog_item_gtin_idx    ON tenant.catalog_item (gtin) WHERE gtin IS NOT NULL;
CREATE INDEX catalog_item_review_idx  ON tenant.catalog_item (partner_id, match_state)
    WHERE match_state IN ('NEEDS_REVIEW', 'UNMATCHED', 'PENDING_ENRICHMENT');

-- Optional durable mirror (the hot copy lives in Redis as inv:{partnerId}:{storeId}).
CREATE TABLE tenant.store_inventory (
    partner_id    uuid    NOT NULL,
    store_id      text    NOT NULL,
    partner_sku   text    NOT NULL,
    in_stock      boolean NOT NULL,
    available_qty integer CHECK (available_qty IS NULL OR available_qty >= 0),
    price         numeric(12, 2) NOT NULL CHECK (price >= 0),
    mrp           numeric(12, 2) CHECK (mrp IS NULL OR mrp >= 0),
    currency      char(3) NOT NULL,
    observed_at   timestamptz NOT NULL,
    PRIMARY KEY (partner_id, store_id, partner_sku),
    FOREIGN KEY (partner_id, store_id) REFERENCES tenant.store (partner_id, store_id)
);

CREATE TABLE tenant.user_preference (
    partner_id              uuid   NOT NULL REFERENCES tenant.partner (partner_id),
    user_ref                text   NOT NULL,           -- pseudonymous partner-side keyed hash; never an email/phone/name
    diet_type               text   NOT NULL DEFAULT 'ANY'
                            CHECK (diet_type IN ('ANY', 'VEGETARIAN', 'EGGETARIAN', 'VEGAN', 'JAIN', 'PESCATARIAN')),
    allergens               text[] NOT NULL DEFAULT '{}',
    strict_may_contain      boolean NOT NULL DEFAULT true,
    goals                   text[] NOT NULL DEFAULT '{}',
    avoid_ingredients       text[] NOT NULL DEFAULT '{}',
    excluded_brands         text[] NOT NULL DEFAULT '{}',
    religious_prefs         text[] NOT NULL DEFAULT '{}',
    locale                  text,
    is_minor                boolean,
    health_data_consent     boolean NOT NULL DEFAULT false,
    personalization_consent boolean NOT NULL DEFAULT false,
    consent_notice_version  text,
    updated_at              timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (partner_id, user_ref),
    -- health-related fields may only be stored with consent
    CHECK (health_data_consent OR (cardinality(allergens) = 0 AND cardinality(goals) = 0))
);

CREATE TABLE tenant.consent_record (
    consent_id     uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id     uuid NOT NULL REFERENCES tenant.partner (partner_id),
    user_ref       text,                                -- nulled on erasure
    purpose        text NOT NULL CHECK (purpose IN ('HEALTH_PREFERENCES', 'PERSONALIZATION', 'ANALYTICS')),
    granted        boolean NOT NULL,
    notice_version text NOT NULL,
    captured_at    timestamptz NOT NULL,
    evidence       jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX consent_record_user_idx ON tenant.consent_record (partner_id, user_ref);

CREATE TABLE tenant.dsar_request (
    request_id    uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id    uuid  NOT NULL REFERENCES tenant.partner (partner_id),
    user_ref_hash bytea NOT NULL,                       -- only a hash is kept for audit after erasure
    request_type  text  NOT NULL CHECK (request_type IN ('ERASURE', 'ACCESS', 'CORRECTION')),
    status        text  NOT NULL CHECK (status IN ('RECEIVED', 'IN_PROGRESS', 'COMPLETED', 'REJECTED')),
    received_at   timestamptz NOT NULL DEFAULT now(),
    completed_at  timestamptz
);

-- Webhook subscriptions. The signing secret is stored encrypted by the application (AES-256-GCM with keys held outside
-- the database, for example a local key file in development or Key Vault / KMS in production); only the ciphertext is
-- kept here.
CREATE TABLE tenant.webhook_endpoint (
    endpoint_id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id        uuid   NOT NULL REFERENCES tenant.partner (partner_id),
    url               text   NOT NULL CHECK (url ~ '^https?://'),
    secret_ciphertext text   NOT NULL,
    event_types       text[] NOT NULL DEFAULT '{}',     -- empty = all event types
    status            text   NOT NULL DEFAULT 'ACTIVE' CHECK (status IN ('ACTIVE', 'DISABLED')),
    created_at        timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX webhook_endpoint_partner_idx ON tenant.webhook_endpoint (partner_id);

-- Bulk catalog feed jobs (POST /catalog/feeds).
CREATE TABLE tenant.catalog_feed (
    feed_id              uuid PRIMARY KEY,
    partner_id           uuid NOT NULL REFERENCES tenant.partner (partner_id),
    format               text NOT NULL CHECK (format IN ('JSONL', 'CSV', 'PARQUET')),
    compression          text NOT NULL CHECK (compression IN ('NONE', 'GZIP')),
    mode                 text NOT NULL CHECK (mode IN ('FULL', 'DELTA')),
    status               text NOT NULL CHECK (status IN ('AWAITING_UPLOAD', 'PROCESSING', 'COMPLETED', 'PARTIAL', 'FAILED', 'EXPIRED')),
    expected_items       integer,
    stats                jsonb NOT NULL DEFAULT '{}',
    upload_expires_at    timestamptz NOT NULL,
    storage_uri          text,
    error_report_uri     text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    completed_at         timestamptz
);
CREATE INDEX catalog_feed_partner_idx ON tenant.catalog_feed (partner_id, created_at);

-- =====================================================================================================================
-- txn: evaluations, suggestions, events, orders, attribution (time-partitioned; manage partitions with pg_partman)
-- IDs are UUIDv7 generated by the application, so creation time is recoverable from the id.
-- =====================================================================================================================

CREATE TABLE txn.cart_evaluation (
    evaluation_id    uuid        NOT NULL,
    created_at       timestamptz NOT NULL,
    partner_id       uuid        NOT NULL,
    cart_ref         text        NOT NULL,
    user_ref         text,
    store_id         text,
    market           text        NOT NULL,
    placement        text        NOT NULL,
    line_count       smallint    NOT NULL,
    suggestion_count smallint    NOT NULL,
    degraded         boolean     NOT NULL DEFAULT false,
    latency_ms       integer,
    experiment_arm   text,
    model_versions   jsonb       NOT NULL,
    PRIMARY KEY (created_at, evaluation_id)
) PARTITION BY RANGE (created_at);

CREATE TABLE txn.suggestion (
    suggestion_id     uuid        NOT NULL,
    created_at        timestamptz NOT NULL,
    evaluation_id     uuid,                             -- NULL for product-page (PDP) recommendations
    partner_id        uuid        NOT NULL,
    cart_ref          text        NOT NULL,
    user_ref          text,                             -- nulled on erasure
    market            text        NOT NULL,
    store_id          text,
    placement         text        NOT NULL DEFAULT 'CART' CHECK (placement IN ('CART', 'CHECKOUT', 'PDP', 'SEARCH', 'AGENT')),
    line_id           text,                             -- NULL for PDP recommendations
    from_partner_sku  text        NOT NULL,
    from_product_id   uuid        NOT NULL,
    from_version_id   bigint      NOT NULL,             -- product version used (replayability)
    from_qty          numeric(10, 3) NOT NULL,
    from_unit_price   numeric(12, 4),                   -- snapshot used by the fair-premium rule; NULL when unknown (PDP)
    from_net_quantity numeric(12, 3),
    from_net_unit     text,
    from_tax_rate     numeric(6, 5),
    currency          char(3)     NOT NULL,
    alternatives      jsonb       NOT NULL,             -- [{rank, partnerSku, productId, versionId, unitPrice, netQuantity, healthDelta, sponsored, recommended}]
    reco_fee          jsonb,                            -- RA-1 terms in effect when served: {rule, amount, currency, basis, ...}; NULL = no add fee
    rules_version     text        NOT NULL,
    graph_version     text        NOT NULL,
    expires_at        timestamptz NOT NULL,
    attribution_expires_at timestamptz NOT NULL,        -- end of the attribution window (cart session + 24 h by default)
    PRIMARY KEY (created_at, suggestion_id)
) PARTITION BY RANGE (created_at);
CREATE INDEX suggestion_id_idx ON txn.suggestion (suggestion_id);
CREATE INDEX suggestion_cart_idx ON txn.suggestion (partner_id, cart_ref);

CREATE TABLE txn.suggestion_event (
    event_id                uuid        NOT NULL,
    occurred_at             timestamptz NOT NULL,
    received_at             timestamptz NOT NULL DEFAULT now(),
    partner_id              uuid        NOT NULL,
    suggestion_id           uuid        NOT NULL,
    event_type              text        NOT NULL
                            CHECK (event_type IN ('IMPRESSION', 'ACCEPT', 'ADD_TO_CART', 'DISMISS', 'UNDO', 'DETAILS_VIEWED')),
    alternative_partner_sku text,
    quantity                numeric(10, 3),
    metadata                jsonb       NOT NULL DEFAULT '{}',
    PRIMARY KEY (occurred_at, event_id)                 -- cross-partition dedupe uses txn.event_key
) PARTITION BY RANGE (occurred_at);
CREATE INDEX suggestion_event_suggestion_idx ON txn.suggestion_event (suggestion_id, event_type);

-- Small, non-partitioned key table that makes event ingestion idempotent across partitions (dedupe on eventId).
CREATE TABLE txn.event_key (
    partner_id  uuid        NOT NULL,
    event_id    uuid        NOT NULL,
    occurred_at timestamptz NOT NULL,
    PRIMARY KEY (partner_id, event_id)
);

-- Small, non-partitioned key table that makes order ingestion idempotent across partitions.
CREATE TABLE txn.order_key (
    partner_id uuid        NOT NULL,
    order_id   text        NOT NULL,
    placed_at  timestamptz NOT NULL,
    PRIMARY KEY (partner_id, order_id)
);

CREATE TABLE txn.order_header (
    partner_id         uuid        NOT NULL,
    order_id           text        NOT NULL,
    placed_at          timestamptz NOT NULL,
    cart_ref           text,
    user_ref           text,                            -- nulled at attribution FINAL and on erasure
    store_id           text,
    market             text        NOT NULL,
    currency           char(3)     NOT NULL,
    status             text        NOT NULL
                       CHECK (status IN ('PLACED', 'CONFIRMED', 'PARTIALLY_FULFILLED', 'DELIVERED', 'CANCELLED')),
    fulfillment_type   text CHECK (fulfillment_type IN ('QUICK_COMMERCE', 'SCHEDULED_DELIVERY', 'PICKUP')),
    experiment_arm     text,
    order_discount     numeric(12, 2) NOT NULL DEFAULT 0,
    return_window_days smallint    NOT NULL,
    delivered_at       timestamptz,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (partner_id, order_id, placed_at)
) PARTITION BY RANGE (placed_at);

CREATE TABLE txn.order_line (
    partner_id               uuid        NOT NULL,
    order_id                 text        NOT NULL,
    placed_at                timestamptz NOT NULL,
    line_id                  text        NOT NULL,
    partner_sku              text        NOT NULL,
    product_id               uuid,
    quantity                 numeric(10, 3) NOT NULL CHECK (quantity > 0),
    unit_price               numeric(12, 4) NOT NULL,
    line_discount            numeric(12, 2) NOT NULL DEFAULT 0,
    allocated_order_discount numeric(12, 2) NOT NULL DEFAULT 0,
    tax_amount               numeric(12, 2),
    tax_rate                 numeric(6, 5),
    net_amount               numeric(12, 2) NOT NULL,   -- on the contracted price basis, after all discounts
    suggestion_id            uuid,
    delivered_qty            numeric(10, 3),            -- from lineStatuses; NULL = whole line delivered with the order
    cancelled_qty            numeric(10, 3),
    PRIMARY KEY (partner_id, order_id, placed_at, line_id),
    FOREIGN KEY (partner_id, order_id, placed_at) REFERENCES txn.order_header (partner_id, order_id, placed_at)
) PARTITION BY RANGE (placed_at);
CREATE INDEX order_line_suggestion_idx ON txn.order_line (suggestion_id) WHERE suggestion_id IS NOT NULL;

CREATE TABLE txn.order_adjustment (
    partner_id              uuid        NOT NULL,
    adjustment_id           text        NOT NULL,
    order_id                text        NOT NULL,
    line_id                 text        NOT NULL,
    adjustment_type         text        NOT NULL
                            CHECK (adjustment_type IN ('RETURN', 'REFUND', 'PRICE_ADJUSTMENT', 'PICKER_SUBSTITUTION', 'CANCELLATION')),
    quantity                numeric(10, 3),
    amount                  numeric(12, 2),
    substituted_partner_sku text,
    occurred_at             timestamptz NOT NULL,
    received_at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (partner_id, adjustment_id)
);
CREATE INDEX order_adjustment_order_idx ON txn.order_adjustment (partner_id, order_id);

CREATE TABLE txn.attribution (
    attribution_id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id               uuid    NOT NULL,
    order_id                 text    NOT NULL,
    line_id                  text    NOT NULL,
    suggestion_id            uuid    NOT NULL,
    state                    text    NOT NULL
                             CHECK (state IN ('PENDING', 'ACCRUED', 'FINAL', 'REVERSED', 'VOID', 'CREDITED', 'REJECTED')),
    rejection_reason         text,
    currency                 char(3) NOT NULL,
    replaced_qty             numeric(10, 3) NOT NULL CHECK (replaced_qty >= 0),
    orig_unit_price_snapshot numeric(12, 4) NOT NULL,
    alt_net_unit_price       numeric(12, 4) NOT NULL,
    pack_delta               numeric(12, 4) NOT NULL,
    size_normalized_delta    numeric(12, 4),
    fair_premium             numeric(12, 4) NOT NULL CHECK (fair_premium >= 0),
    fee_rate                 numeric(6, 5)  NOT NULL CHECK (fee_rate >= 0 AND fee_rate <= 1),
    fee_amount               numeric(12, 2) NOT NULL CHECK (fee_amount >= 0),
    reversed_amount          numeric(12, 2) NOT NULL DEFAULT 0 CHECK (reversed_amount >= 0),
    rule_version             text    NOT NULL,          -- e.g. FP-1
    computed_at              timestamptz NOT NULL DEFAULT now(),
    finalizes_at             timestamptz,
    original_partner_sku     text,
    alternative_partner_sku  text,
    uncapped_fee_amount      numeric(12, 2),            -- before per-line / per-order / per-month caps (audit)
    accrued_at               timestamptz,
    finalized_at             timestamptz,
    statement_id             uuid,                      -- set when billed on a statement
    UNIQUE (partner_id, order_id, line_id),
    CHECK (reversed_amount <= fee_amount)
);
CREATE INDEX attribution_state_idx ON txn.attribution (partner_id, state, finalizes_at);

-- RA-1 recommendation-add charges: one row per chargeable ACCEPT / ADD_TO_CART event for a HealthSwap-recommended
-- (labeled) alternative. Lifecycle: PENDING -> FINAL | VOID | REJECTED; FINAL -> CREDITED (dispute upheld).
CREATE TABLE txn.reco_charge (
    charge_id               uuid PRIMARY KEY,
    partner_id              uuid        NOT NULL,
    event_id                uuid        NOT NULL,
    event_type              text        NOT NULL CHECK (event_type IN ('ACCEPT', 'ADD_TO_CART')),
    suggestion_id           uuid        NOT NULL,
    cart_ref                text        NOT NULL,
    user_ref                text,                       -- nulled at FINAL and on erasure
    market                  text        NOT NULL,
    placement               text        NOT NULL,
    alternative_partner_sku text        NOT NULL,
    alternative_product_id  uuid,
    quantity                numeric(10, 3) NOT NULL DEFAULT 1 CHECK (quantity > 0),
    basis                   text        NOT NULL CHECK (basis IN ('PER_ADD_EVENT', 'PER_UNIT')),
    unit_amount             numeric(12, 4) NOT NULL CHECK (unit_amount >= 0),
    amount                  numeric(12, 2) NOT NULL CHECK (amount >= 0),
    currency                char(3)     NOT NULL,
    rule_version            text        NOT NULL,       -- RA-1
    state                   text        NOT NULL CHECK (state IN ('PENDING', 'FINAL', 'VOID', 'REJECTED', 'CREDITED')),
    rejection_reason        text,
    occurred_at             timestamptz NOT NULL,
    received_at             timestamptz NOT NULL DEFAULT now(),
    finalizes_at            timestamptz NOT NULL,
    finalized_at            timestamptz,
    statement_id            uuid,
    credited_amount         numeric(12, 2) NOT NULL DEFAULT 0 CHECK (credited_amount >= 0),
    UNIQUE (partner_id, event_id),
    CHECK (credited_amount <= amount)
);
CREATE INDEX reco_charge_pending_idx ON txn.reco_charge (finalizes_at) WHERE state = 'PENDING';
CREATE INDEX reco_charge_suggestion_idx ON txn.reco_charge (suggestion_id, occurred_at);
CREATE INDEX reco_charge_billing_idx ON txn.reco_charge (partner_id, state, finalized_at) WHERE statement_id IS NULL;
CREATE INDEX reco_charge_statement_idx ON txn.reco_charge (statement_id) WHERE statement_id IS NOT NULL;
CREATE INDEX reco_charge_cart_idx ON txn.reco_charge (partner_id, cart_ref);

-- Idempotency-Key store (24 h). Responses are replayed for the same key and payload; a different payload is a 409.
CREATE TABLE txn.idempotency_key (
    partner_id    uuid        NOT NULL,
    key           text        NOT NULL,
    method        text        NOT NULL,
    path          text        NOT NULL,
    request_hash  bytea       NOT NULL,
    status_code   integer,                              -- NULL while the first request is still in flight
    response_body text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    expires_at    timestamptz NOT NULL,
    PRIMARY KEY (partner_id, key)
);

-- Transactional outbox: domain events are written in the same transaction as the state change and relayed to the
-- event bus (Kafka API in production, in-process in local development). partner_id is NULL for global KB events.
CREATE TABLE txn.outbox (
    outbox_id    bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    partner_id   uuid,
    topic        text        NOT NULL,
    msg_key      text        NOT NULL,
    payload      jsonb       NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz
);
CREATE INDEX outbox_unpublished_idx ON txn.outbox (outbox_id) WHERE published_at IS NULL;

-- Signed webhook deliveries with retry state (at-least-once; dedupe on webhook_id).
CREATE TABLE txn.webhook_delivery (
    delivery_id      uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    webhook_id       text        NOT NULL,              -- envelope id, identical across retries
    partner_id       uuid        NOT NULL,
    endpoint_id      uuid        NOT NULL,
    event_type       text        NOT NULL,
    payload          jsonb       NOT NULL,
    status           text        NOT NULL DEFAULT 'PENDING' CHECK (status IN ('PENDING', 'DELIVERED', 'DEAD')),
    attempts         integer     NOT NULL DEFAULT 0,
    next_attempt_at  timestamptz NOT NULL DEFAULT now(),
    last_status_code integer,
    last_error       text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    delivered_at     timestamptz
);
CREATE INDEX webhook_delivery_due_idx ON txn.webhook_delivery (next_attempt_at) WHERE status = 'PENDING';

-- Initial partitions (create future ones ahead of time with pg_partman or a scheduled job).
CREATE TABLE txn.cart_evaluation_2026_11 PARTITION OF txn.cart_evaluation
    FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');
CREATE TABLE txn.cart_evaluation_default PARTITION OF txn.cart_evaluation DEFAULT;
CREATE TABLE txn.suggestion_2026_11 PARTITION OF txn.suggestion
    FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');
CREATE TABLE txn.suggestion_default PARTITION OF txn.suggestion DEFAULT;
CREATE TABLE txn.suggestion_event_2026_11 PARTITION OF txn.suggestion_event
    FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');
CREATE TABLE txn.suggestion_event_default PARTITION OF txn.suggestion_event DEFAULT;
CREATE TABLE txn.order_header_2026_11 PARTITION OF txn.order_header
    FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');
CREATE TABLE txn.order_header_default PARTITION OF txn.order_header DEFAULT;
CREATE TABLE txn.order_line_2026_11 PARTITION OF txn.order_line
    FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');
CREATE TABLE txn.order_line_default PARTITION OF txn.order_line DEFAULT;

-- =====================================================================================================================
-- billing: append-only double-entry ledger, statements, disputes
-- =====================================================================================================================

CREATE TABLE billing.account (
    account_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id uuid,                                    -- NULL for house accounts (e.g. FEE_REVENUE, CASH)
    code       text    NOT NULL CHECK (code IN ('UNBILLED_AR', 'BILLED_AR', 'DEFERRED_REVENUE', 'FEE_REVENUE',
                                                'RECO_FEE_REVENUE', 'OUTPUT_TAX', 'CASH', 'SUSPENSE')),
    currency   char(3) NOT NULL,
    UNIQUE NULLS NOT DISTINCT (partner_id, code, currency)
);

CREATE TABLE billing.journal_entry (
    entry_id        uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id      uuid        NOT NULL,
    entry_kind      text        NOT NULL
                    CHECK (entry_kind IN ('FEE_ACCRUAL', 'FEE_FINALIZE', 'FEE_REVERSAL', 'RECO_FEE', 'STATEMENT', 'PAYMENT',
                                          'CREDIT_NOTE', 'MANUAL_ADJUSTMENT')),
    reference_type  text        NOT NULL CHECK (reference_type IN ('ATTRIBUTION', 'RECO_CHARGE', 'STATEMENT', 'DISPUTE',
                                                                   'PAYMENT', 'MANUAL')),
    reference_id    text        NOT NULL,
    effective_at    timestamptz NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    created_by      text        NOT NULL,
    idempotency_key text        NOT NULL UNIQUE
);
CREATE INDEX journal_entry_reference_idx ON billing.journal_entry (reference_type, reference_id);

CREATE TABLE billing.journal_line (
    entry_id   uuid     NOT NULL REFERENCES billing.journal_entry (entry_id),
    line_no    smallint NOT NULL,
    partner_id uuid     NOT NULL,                       -- copy of journal_entry.partner_id (row-level security)
    account_id uuid     NOT NULL REFERENCES billing.account (account_id),
    debit      numeric(14, 2) NOT NULL DEFAULT 0 CHECK (debit >= 0),
    credit     numeric(14, 2) NOT NULL DEFAULT 0 CHECK (credit >= 0),
    CHECK ((debit = 0) <> (credit = 0)),                -- exactly one side is non-zero
    PRIMARY KEY (entry_id, line_no)
);
CREATE INDEX journal_line_account_idx ON billing.journal_line (account_id);

-- Every journal entry must balance at commit time. SECURITY DEFINER so the check sees every line of the entry
-- regardless of the caller's role and row-level security.
CREATE OR REPLACE FUNCTION billing.assert_entry_balanced() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, billing AS $$
DECLARE
    v_diff numeric;
BEGIN
    SELECT COALESCE(SUM(debit), 0) - COALESCE(SUM(credit), 0)
      INTO v_diff
      FROM billing.journal_line
     WHERE entry_id = NEW.entry_id;
    IF v_diff <> 0 THEN
        RAISE EXCEPTION 'Journal entry % is not balanced (debits - credits = %)', NEW.entry_id, v_diff;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM billing.journal_entry e WHERE e.entry_id = NEW.entry_id AND e.partner_id = NEW.partner_id) THEN
        RAISE EXCEPTION 'Journal line partner does not match entry %', NEW.entry_id;
    END IF;
    RETURN NULL;
END;
$$;

CREATE CONSTRAINT TRIGGER journal_line_balanced
    AFTER INSERT ON billing.journal_line
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION billing.assert_entry_balanced();

-- The journal is append-only: corrections are reversing entries.
CREATE OR REPLACE FUNCTION billing.block_mutation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'Ledger rows are append-only; post a reversing entry instead';
END;
$$;

CREATE TRIGGER journal_entry_immutable BEFORE UPDATE OR DELETE ON billing.journal_entry
    FOR EACH ROW EXECUTE FUNCTION billing.block_mutation();
CREATE TRIGGER journal_line_immutable BEFORE UPDATE OR DELETE ON billing.journal_line
    FOR EACH ROW EXECUTE FUNCTION billing.block_mutation();
CREATE TRIGGER journal_entry_no_truncate BEFORE TRUNCATE ON billing.journal_entry
    FOR EACH STATEMENT EXECUTE FUNCTION billing.block_mutation();
CREATE TRIGGER journal_line_no_truncate BEFORE TRUNCATE ON billing.journal_line
    FOR EACH STATEMENT EXECUTE FUNCTION billing.block_mutation();

CREATE TABLE billing.statement (
    statement_id   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id     uuid    NOT NULL,
    period_start   date    NOT NULL,
    period_end     date    NOT NULL,
    currency       char(3) NOT NULL,
    status         text    NOT NULL CHECK (status IN ('DRAFT', 'ISSUED', 'DISPUTED', 'SETTLED', 'VOID')),
    premium_fee    numeric(14, 2) NOT NULL DEFAULT 0,   -- FP-1 fees (before reversals)
    reco_fee       numeric(14, 2) NOT NULL DEFAULT 0,   -- RA-1 recommendation-add fees
    reco_charge_count integer NOT NULL DEFAULT 0,
    gross_fee      numeric(14, 2) NOT NULL DEFAULT 0,   -- premium_fee + reco_fee
    reversals      numeric(14, 2) NOT NULL DEFAULT 0,
    credits        numeric(14, 2) NOT NULL DEFAULT 0,
    net_fee        numeric(14, 2) NOT NULL DEFAULT 0,   -- gross_fee - reversals - credits
    tax_rate       numeric(6, 5)  NOT NULL DEFAULT 0,
    tax_amount     numeric(14, 2) NOT NULL DEFAULT 0,
    total_due      numeric(14, 2) NOT NULL DEFAULT 0,   -- net_fee + tax_amount
    paid_amount    numeric(14, 2) NOT NULL DEFAULT 0,
    line_count     integer NOT NULL DEFAULT 0,
    invoice_number text UNIQUE,
    issued_at      timestamptz,
    due_at         timestamptz,
    UNIQUE (partner_id, period_start, currency),
    CHECK (period_end >= period_start),
    CHECK (gross_fee = premium_fee + reco_fee)
);
CREATE SEQUENCE billing.invoice_no_seq;

CREATE TABLE billing.statement_line (
    statement_id   uuid    NOT NULL REFERENCES billing.statement (statement_id),
    line_no        integer NOT NULL,
    partner_id     uuid    NOT NULL,                    -- row-level security
    attribution_id uuid    NOT NULL,                    -- XDB-safe logical FK to txn.attribution
    line_type      text    NOT NULL CHECK (line_type IN ('FEE', 'REVERSAL', 'CREDIT')),
    order_id       text    NOT NULL,
    order_line_id  text    NOT NULL,
    suggestion_id  uuid    NOT NULL,
    delivered_at   timestamptz,
    fair_premium   numeric(12, 4) NOT NULL,
    amount         numeric(12, 2) NOT NULL,
    PRIMARY KEY (statement_id, line_no)
);
CREATE INDEX statement_line_order_idx ON billing.statement_line (order_id, order_line_id);
-- RA-1 charges are not copied into statement_line (one row per add would duplicate txn.reco_charge); they are linked
-- through txn.reco_charge.statement_id and listed by GET /billing/statements/{id}/recommendation-charges.

CREATE TABLE billing.dispute (
    dispute_id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    partner_id         uuid  NOT NULL,
    statement_id       uuid  NOT NULL REFERENCES billing.statement (statement_id),
    reason             text  NOT NULL
                       CHECK (reason IN ('ORDER_NOT_DELIVERED', 'PRICE_MISMATCH', 'NOT_ATTRIBUTABLE', 'DUPLICATE',
                                         'NOT_ADDED_TO_CART', 'OTHER')),
    disputed_lines     jsonb NOT NULL,                  -- [{orderId, orderLineId}]
    disputed_charge_ids uuid[] NOT NULL DEFAULT '{}',   -- RA-1 recommendation charges
    comment            text,
    status             text  NOT NULL DEFAULT 'OPEN'
                       CHECK (status IN ('OPEN', 'UNDER_REVIEW', 'UPHELD', 'PARTIALLY_UPHELD', 'REJECTED')),
    credit_note_amount numeric(12, 2),
    credit_note_tax    numeric(12, 2),
    opened_at          timestamptz NOT NULL DEFAULT now(),
    resolved_at        timestamptz,
    resolution         text
);

-- =====================================================================================================================
-- audit
-- =====================================================================================================================

CREATE TABLE audit.change_log (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT now(),
    actor       text  NOT NULL,                         -- workload identity or human principal
    action      text  NOT NULL,
    entity_type text  NOT NULL,
    entity_id   text  NOT NULL,
    before_hash bytea,
    after_hash  bytea,
    details     jsonb NOT NULL DEFAULT '{}'
);

-- =====================================================================================================================
-- Row-level security (defense in depth; the application also filters by partner_id).
-- The application sets the tenant per transaction:  SELECT set_config('app.partner_id', '<partner uuid>', true);
-- If the setting is missing, the policy evaluates to NULL and no rows are visible (fail closed).
-- Control-plane jobs that must see all tenants use the hs_ops role (BYPASSRLS), never the request path.
-- Rows with a NULL partner_id (house accounts, global outbox events) are visible to hs_ops only.
-- =====================================================================================================================

DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'tenant.partner', 'tenant.api_client', 'tenant.store', 'tenant.catalog_item', 'tenant.store_inventory',
        'tenant.user_preference', 'tenant.consent_record', 'tenant.dsar_request', 'tenant.webhook_endpoint',
        'tenant.catalog_feed',
        'txn.cart_evaluation', 'txn.suggestion', 'txn.suggestion_event', 'txn.event_key', 'txn.order_key',
        'txn.order_header', 'txn.order_line', 'txn.order_adjustment', 'txn.attribution', 'txn.reco_charge',
        'txn.idempotency_key', 'txn.outbox', 'txn.webhook_delivery',
        'billing.account', 'billing.journal_entry', 'billing.journal_line', 'billing.statement',
        'billing.statement_line', 'billing.dispute'
    ]
    LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'CREATE POLICY tenant_isolation ON %s '
            'USING (partner_id = NULLIF(current_setting(''app.partner_id'', true), '''')::uuid) '
            'WITH CHECK (partner_id = NULLIF(current_setting(''app.partner_id'', true), '''')::uuid)',
            t);
    END LOOP;
END;
$$;

-- =====================================================================================================================
-- Roles (least privilege). Login users are created per service, granted the roles they need (NOINHERIT is
-- recommended) and switch with SET LOCAL ROLE per transaction:
--   hs_app            request path (catalog, inventory, evaluations, events, orders, preferences, disputes)
--   hs_ledger_writer  money path (fee accrual/finalization, RA-1 charge finalization, statements, credit notes)
--   hs_kb_writer      knowledge plane (ingestion, scoring, swap-graph builds)
--   hs_ops            control plane and cross-tenant schedulers (BYPASSRLS; private admin plane only)
--   hs_readonly       analytics / support (tenant-scoped through RLS)
-- Partner configuration (tenant.partner, tenant.api_client, tenant.webhook_endpoint) is writable by hs_ops only.
-- =====================================================================================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hs_app') THEN
        CREATE ROLE hs_app NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hs_ledger_writer') THEN
        CREATE ROLE hs_ledger_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hs_kb_writer') THEN
        CREATE ROLE hs_kb_writer NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hs_readonly') THEN
        CREATE ROLE hs_readonly NOLOGIN;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hs_ops') THEN
        CREATE ROLE hs_ops NOLOGIN BYPASSRLS;
    END IF;
END;
$$;

-- Knowledge-plane change events are global (no tenant); the KB writer may append only those to the outbox.
CREATE POLICY kb_global_events ON txn.outbox FOR INSERT TO hs_kb_writer WITH CHECK (partner_id IS NULL);

-- hs_app: read the knowledge plane and its own partner config; full DML on its own tenant and transaction rows.
GRANT USAGE ON SCHEMA kb, reg, rec, tenant, txn, billing, audit TO hs_app, hs_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA kb, reg, rec TO hs_app, hs_readonly;
GRANT SELECT ON tenant.partner, tenant.api_client, tenant.webhook_endpoint TO hs_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON tenant.store, tenant.catalog_item, tenant.store_inventory, tenant.user_preference,
    tenant.consent_record, tenant.dsar_request, tenant.catalog_feed TO hs_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA txn TO hs_app;
GRANT SELECT ON ALL TABLES IN SCHEMA tenant, txn, billing TO hs_readonly;
GRANT SELECT ON ALL TABLES IN SCHEMA billing TO hs_app;
GRANT INSERT ON billing.dispute TO hs_app;
GRANT INSERT ON audit.change_log TO hs_app;
GRANT INSERT ON kb.curation_task TO hs_app;

-- hs_kb_writer: knowledge plane only (no tenant or billing data).
GRANT USAGE ON SCHEMA kb, reg, rec, audit, txn TO hs_kb_writer;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA kb, reg, rec TO hs_kb_writer;
GRANT INSERT ON txn.outbox TO hs_kb_writer;
GRANT INSERT ON audit.change_log TO hs_kb_writer;

-- hs_ledger_writer: append-only journal, statements, disputes, and the fee state on attributions and RA-1 charges.
GRANT USAGE ON SCHEMA billing, txn, tenant, audit TO hs_ledger_writer;
GRANT SELECT ON tenant.partner TO hs_ledger_writer;
GRANT SELECT, INSERT ON billing.journal_entry, billing.journal_line TO hs_ledger_writer;
GRANT SELECT, INSERT, UPDATE ON billing.account, billing.statement, billing.statement_line, billing.dispute TO hs_ledger_writer;
GRANT SELECT ON txn.suggestion, txn.suggestion_event, txn.order_header, txn.order_line, txn.order_adjustment TO hs_ledger_writer;
GRANT SELECT, UPDATE ON txn.attribution, txn.reco_charge TO hs_ledger_writer;
GRANT INSERT ON txn.outbox, txn.webhook_delivery TO hs_ledger_writer;
GRANT INSERT ON audit.change_log TO hs_ledger_writer;

-- hs_ops: control plane (partner onboarding, config, kill switches, schedulers, outbox relay, webhook dispatch).
GRANT USAGE ON SCHEMA kb, reg, rec, tenant, txn, billing, audit TO hs_ops;
GRANT SELECT ON ALL TABLES IN SCHEMA kb, reg, rec, billing TO hs_ops;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA tenant, txn TO hs_ops;
GRANT INSERT, UPDATE ON kb.curation_task TO hs_ops;
GRANT INSERT ON audit.change_log TO hs_ops;

GRANT USAGE ON ALL SEQUENCES IN SCHEMA kb, rec, txn, audit TO hs_kb_writer, hs_app, hs_ledger_writer, hs_ops;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA billing TO hs_ledger_writer;

-- =====================================================================================================================
-- Reference data seeds
-- =====================================================================================================================

INSERT INTO kb.source (source_id, code, name, license_code, trust_tier, share_alike, attribution_text, homepage_url) VALUES
    (1,  'BRAND_PORTAL',         'HealthSwap brand portal (brand-verified)',  'CONTRACT',      1, false, NULL, NULL),
    (2,  'GDSN_POOL',            'GS1 GDSN data pool (licensed)',             'CONTRACT',      2, false, NULL, NULL),
    (3,  'GS1_DATAKART',         'GS1 India DataKart (licensed)',             'CONTRACT',      2, false, NULL, 'https://www.gs1india.org/services/datakart'),
    (4,  'USDA_FDC_BRANDED',     'USDA FoodData Central - Branded Foods',     'CC0-1.0',       2, false, 'U.S. Department of Agriculture, Agricultural Research Service. FoodData Central.', 'https://fdc.nal.usda.gov/'),
    (5,  'USDA_FDC_FOUNDATION',  'USDA FoodData Central - Foundation/SR',     'CC0-1.0',       2, false, 'U.S. Department of Agriculture, Agricultural Research Service. FoodData Central.', 'https://fdc.nal.usda.gov/'),
    (6,  'IFCT_2017',            'ICMR-NIN Indian Food Composition Tables',   'LICENSED',      2, false, NULL, NULL),
    (7,  'PARTNER_FEED',         'Partner catalog feed',                      'PARTNER-OWNED', 3, false, NULL, NULL),
    (8,  'ONDC_CATALOG',         'ONDC seller catalog',                       'ONDC-NETWORK',  3, false, NULL, NULL),
    (9,  'OFF',                  'Open Food Facts',                           'ODbL-1.0',      4, true,  'Contains data from Open Food Facts, available under the Open Database License.', 'https://world.openfoodfacts.org/'),
    (10, 'LABEL_OCR',            'HealthSwap label OCR + extraction',         'INTERNAL',      5, false, NULL, NULL),
    (11, 'OPENFDA_ENFORCEMENT',  'openFDA food enforcement (recalls)',        'PUBLIC',        2, false, NULL, 'https://open.fda.gov/apis/food/enforcement/')
ON CONFLICT (source_id) DO NOTHING;

INSERT INTO kb.nutrient (nutrient_code, name, unit, role) VALUES
    ('ENERC_KCAL', 'Energy',                                         'kcal', 'NEUTRAL'),
    ('PROCNT',     'Protein',                                        'g',    'ENCOURAGE'),
    ('CHOCDF',     'Carbohydrate',                                   'g',    'NEUTRAL'),
    ('SUGAR',      'Sugars, total',                                  'g',    'LIMIT'),
    ('SUGAD',      'Sugars, added',                                  'g',    'LIMIT'),
    ('FAT',        'Total fat',                                      'g',    'NEUTRAL'),
    ('FASAT',      'Saturated fatty acids',                          'g',    'LIMIT'),
    ('FATRN',      'Trans fatty acids',                              'g',    'LIMIT'),
    ('CHOLE',      'Cholesterol',                                    'mg',   'LIMIT'),
    ('NA',         'Sodium',                                         'mg',   'LIMIT'),
    ('SALT',       'Salt (derived: sodium x 2.5)',                   'g',    'LIMIT'),
    ('FIBTG',      'Dietary fibre, total',                           'g',    'ENCOURAGE'),
    ('FVLN_PCT',   'Fruit, vegetables, legumes and nuts (share)',    '%',    'ENCOURAGE')
ON CONFLICT (nutrient_code) DO NOTHING;

INSERT INTO kb.allergen (allergen_code, name) VALUES
    ('GLUTEN_CEREALS', 'Cereals containing gluten'),
    ('WHEAT',          'Wheat'),
    ('CRUSTACEANS',    'Crustaceans'),
    ('MOLLUSCS',       'Molluscs'),
    ('EGGS',           'Eggs'),
    ('FISH',           'Fish'),
    ('PEANUTS',        'Peanuts'),
    ('TREE_NUTS',      'Tree nuts'),
    ('SOY',            'Soybeans'),
    ('MILK',           'Milk'),
    ('SESAME',         'Sesame'),
    ('MUSTARD',        'Mustard'),
    ('CELERY',         'Celery'),
    ('LUPIN',          'Lupin'),
    ('SULPHITES',      'Sulphur dioxide and sulphites')
ON CONFLICT (allergen_code) DO NOTHING;

INSERT INTO kb.mark (mark_code, jurisdiction, kind, description) VALUES
    ('IN_FSSAI_VEG',        'IN', 'REGULATORY_SYMBOL', 'FSSAI vegetarian symbol'),
    ('IN_FSSAI_NON_VEG',    'IN', 'REGULATORY_SYMBOL', 'FSSAI non-vegetarian symbol'),
    ('IN_FSSAI_VEGAN',      'IN', 'CERTIFICATION',     'FSSAI vegan logo (Vegan Foods Regulations 2022)'),
    ('IN_FSSAI_FORTIFIED',  'IN', 'REGULATORY_SYMBOL', 'FSSAI +F fortification logo'),
    ('IN_JAIVIK_BHARAT',    'IN', 'CERTIFICATION',     'Jaivik Bharat organic logo'),
    ('US_USDA_ORGANIC',     'US', 'CERTIFICATION',     'USDA Organic seal'),
    ('HALAL_CERTIFIED',     NULL, 'CERTIFICATION',     'Halal certification mark'),
    ('KOSHER_CERTIFIED',    NULL, 'CERTIFICATION',     'Kosher certification mark'),
    ('GLUTEN_FREE_CLAIM',   NULL, 'CLAIM',             'Gluten-free claim per jurisdiction rules'),
    ('JAIN_FRIENDLY',       NULL, 'DIETARY',           'Inferred from ingredients only (not a regulated mark)')
ON CONFLICT (mark_code) DO NOTHING;

INSERT INTO reg.jurisdiction (code, name) VALUES
    ('IN', 'India'), ('US', 'United States'), ('EU', 'European Union'), ('GB', 'United Kingdom'),
    ('CA', 'Canada'), ('AU', 'Australia'), ('NZ', 'New Zealand'), ('SG', 'Singapore')
ON CONFLICT (code) DO NOTHING;

-- Mandatory allergen declarations (ILLUSTRATIVE - verify against current FSSAI L&D 2020 compendium, FALCPA/FASTER Act,
-- Regulation (EU) No 1169/2011 Annex II).
INSERT INTO reg.allergen_rule (jurisdiction, allergen_code, mandatory_declaration)
SELECT j, a, true
FROM (VALUES
    ('IN', 'GLUTEN_CEREALS'), ('IN', 'CRUSTACEANS'), ('IN', 'MILK'), ('IN', 'EGGS'), ('IN', 'FISH'),
    ('IN', 'PEANUTS'), ('IN', 'TREE_NUTS'), ('IN', 'SOY'), ('IN', 'SULPHITES'),
    ('US', 'MILK'), ('US', 'EGGS'), ('US', 'FISH'), ('US', 'CRUSTACEANS'), ('US', 'TREE_NUTS'),
    ('US', 'PEANUTS'), ('US', 'WHEAT'), ('US', 'SOY'), ('US', 'SESAME'),
    ('EU', 'GLUTEN_CEREALS'), ('EU', 'CRUSTACEANS'), ('EU', 'EGGS'), ('EU', 'FISH'), ('EU', 'PEANUTS'),
    ('EU', 'SOY'), ('EU', 'MILK'), ('EU', 'TREE_NUTS'), ('EU', 'CELERY'), ('EU', 'MUSTARD'),
    ('EU', 'SESAME'), ('EU', 'SULPHITES'), ('EU', 'LUPIN'), ('EU', 'MOLLUSCS')
) AS v (j, a)
ON CONFLICT DO NOTHING;

-- US Daily Values for adults and children >= 4 (21 CFR 101.9(c)) - ILLUSTRATIVE, verify before use.
INSERT INTO reg.daily_value (jurisdiction, population, nutrient_code, amount) VALUES
    ('US', 'ADULT', 'FAT',    78),
    ('US', 'ADULT', 'FASAT',  20),
    ('US', 'ADULT', 'CHOLE',  300),
    ('US', 'ADULT', 'NA',     2300),
    ('US', 'ADULT', 'CHOCDF', 275),
    ('US', 'ADULT', 'FIBTG',  28),
    ('US', 'ADULT', 'SUGAD',  50),
    ('US', 'ADULT', 'PROCNT', 50)
ON CONFLICT DO NOTHING;

-- Front-of-pack thresholds - ILLUSTRATIVE, verify before use.
-- UK traffic light (foods, per 100 g): Low <= low_max, High > high_min.
-- US FDA FOP proposed rule (per serving, %DV): Low <= 5% DV, High >= 20% DV.
INSERT INTO reg.fop_threshold (jurisdiction, scheme, nutrient_code, physical_state, basis, low_max, high_min, high_inclusive) VALUES
    ('GB', 'UK_TRAFFIC_LIGHT',    'FAT',   'SOLID', 'PER_100G',           3.0,  17.5, false),
    ('GB', 'UK_TRAFFIC_LIGHT',    'FASAT', 'SOLID', 'PER_100G',           1.5,  5.0,  false),
    ('GB', 'UK_TRAFFIC_LIGHT',    'SUGAR', 'SOLID', 'PER_100G',           5.0,  22.5, false),
    ('GB', 'UK_TRAFFIC_LIGHT',    'SALT',  'SOLID', 'PER_100G',           0.3,  1.5,  false),
    ('US', 'US_FDA_FOP_PROPOSED', 'FASAT', 'ANY',   'PER_SERVING_PCT_DV', 5,    20,   true),
    ('US', 'US_FDA_FOP_PROPOSED', 'NA',    'ANY',   'PER_SERVING_PCT_DV', 5,    20,   true),
    ('US', 'US_FDA_FOP_PROPOSED', 'SUGAD', 'ANY',   'PER_SERVING_PCT_DV', 5,    20,   true)
ON CONFLICT DO NOTHING;

-- Claim thresholds - ILLUSTRATIVE (common Codex/EU/FSSAI pattern), verify against FSSAI Advertising & Claims
-- Regulations 2018 and Regulation (EC) No 1924/2006 before use.
INSERT INTO reg.claim_threshold (jurisdiction, claim_code, nutrient_code, physical_state, basis, max_value, min_relative_difference) VALUES
    ('IN', 'LOW_SUGAR',  'SUGAR', 'SOLID',  'PER_100G',  5.0, NULL),
    ('IN', 'LOW_SUGAR',  'SUGAR', 'LIQUID', 'PER_100ML', 2.5, NULL),
    ('IN', 'SUGAR_FREE', 'SUGAR', 'SOLID',  'PER_100G',  0.5, NULL),
    ('IN', 'SUGAR_FREE', 'SUGAR', 'LIQUID', 'PER_100ML', 0.5, NULL),
    ('IN', 'REDUCED',    'SUGAR', 'ANY',    'RELATIVE',  NULL, 0.25),
    ('IN', 'REDUCED',    'NA',    'ANY',    'RELATIVE',  NULL, 0.25),
    ('IN', 'REDUCED',    'FASAT', 'ANY',    'RELATIVE',  NULL, 0.25),
    ('EU', 'REDUCED',    'SUGAR', 'ANY',    'RELATIVE',  NULL, 0.30),
    ('EU', 'REDUCED',    'NA',    'ANY',    'RELATIVE',  NULL, 0.30),
    ('EU', 'REDUCED',    'FASAT', 'ANY',    'RELATIVE',  NULL, 0.30)
ON CONFLICT DO NOTHING;

-- Additive identities (ILLUSTRATIVE subset).
INSERT INTO kb.additive (ins_number, e_number, canonical_name, us_names, functional_classes) VALUES
    ('102',  'E102', 'Tartrazine',             ARRAY['FD&C Yellow No. 5', 'Yellow 5'], ARRAY['COLOUR']),
    ('110',  'E110', 'Sunset Yellow FCF',      ARRAY['FD&C Yellow No. 6', 'Yellow 6'], ARRAY['COLOUR']),
    ('129',  'E129', 'Allura Red AC',          ARRAY['FD&C Red No. 40', 'Red 40'],     ARRAY['COLOUR']),
    ('127',  'E127', 'Erythrosine',            ARRAY['FD&C Red No. 3', 'Red 3'],       ARRAY['COLOUR']),
    ('171',  'E171', 'Titanium dioxide',       ARRAY['Titanium dioxide'],              ARRAY['COLOUR']),
    ('951',  'E951', 'Aspartame',              ARRAY['Aspartame'],                     ARRAY['SWEETENER', 'FLAVOUR_ENHANCER']),
    ('955',  'E955', 'Sucralose',              ARRAY['Sucralose'],                     ARRAY['SWEETENER']),
    ('621',  'E621', 'Monosodium L-glutamate', ARRAY['Monosodium glutamate', 'MSG'],   ARRAY['FLAVOUR_ENHANCER']),
    ('322',  'E322', 'Lecithins',              ARRAY['Lecithin', 'Soy lecithin'],      ARRAY['EMULSIFIER', 'ANTIOXIDANT']),
    ('330',  'E330', 'Citric acid',            ARRAY['Citric acid'],                   ARRAY['ACIDITY_REGULATOR', 'ANTIOXIDANT', 'SEQUESTRANT']),
    ('924a', NULL,   'Potassium bromate',      ARRAY['Potassium bromate'],             ARRAY['FLOUR_TREATMENT_AGENT'])
ON CONFLICT DO NOTHING;

-- Regulatory documents and statuses (ILLUSTRATIVE - verify dates and wording against the official texts).
INSERT INTO reg.document (jurisdiction, authority, title, citation, effective_from) VALUES
    ('EU', 'European Commission', 'Commission Regulation (EU) 2022/63 - removal of titanium dioxide (E 171)', 'Reg. (EU) 2022/63', DATE '2022-02-07'),
    ('EU', 'European Union',      'Regulation (EC) No 1333/2008 Annex V - warning label for certain food colours', 'Reg. (EC) 1333/2008 Annex V', DATE '2010-07-20'),
    ('US', 'FDA',                 'Revocation of authorization of FD&C Red No. 3 in food and ingested drugs',     'FDA Red No. 3 revocation (2025)', DATE '2025-01-15'),
    ('EU', 'EFSA',                'EFSA safety assessment of titanium dioxide (E171) as a food additive (2021)',  'EFSA Journal 2021 E171', DATE '2021-05-06'),
    ('US', 'IARC / JECFA',        'IARC and JECFA hazard and risk assessments of aspartame (2023)',              'IARC-JECFA aspartame 2023', DATE '2023-07-14')
ON CONFLICT (citation) DO NOTHING;

INSERT INTO reg.additive_status (additive_id, jurisdiction, status, conditions, effective_from, effective_to, document_id)
SELECT a.additive_id, 'EU', 'PROHIBITED', 'No longer authorised as a food additive after the transition period',
       DATE '2022-08-07', NULL, d.document_id
FROM kb.additive a JOIN reg.document d ON d.citation = 'Reg. (EU) 2022/63'
WHERE a.ins_number = '171'
ON CONFLICT DO NOTHING;

INSERT INTO reg.additive_status (additive_id, jurisdiction, status, conditions, effective_from, effective_to, document_id)
SELECT a.additive_id, 'US', s.status, s.conditions, s.effective_from, s.effective_to, d.document_id
FROM kb.additive a
JOIN reg.document d ON d.citation = 'FDA Red No. 3 revocation (2025)'
CROSS JOIN (VALUES
    ('PHASE_OUT',  'Authorization revoked; food manufacturers must reformulate by the compliance date', DATE '2025-01-15', DATE '2027-01-15'),
    ('PROHIBITED', 'Food compliance date reached',                                                      DATE '2027-01-15', NULL::date)
) AS s (status, conditions, effective_from, effective_to)
WHERE a.ins_number = '127'
ON CONFLICT DO NOTHING;

INSERT INTO reg.additive_hazard (additive_id, authority, finding, concern_tier, as_of, document_id)
SELECT a.additive_id, 'EU', 'Mandatory label warning: may have an adverse effect on activity and attention in children',
       'B', DATE '2010-07-20', d.document_id
FROM kb.additive a JOIN reg.document d ON d.citation = 'Reg. (EC) 1333/2008 Annex V'
WHERE a.ins_number IN ('102', '110', '129')
ON CONFLICT DO NOTHING;

INSERT INTO reg.additive_hazard (additive_id, authority, finding, concern_tier, as_of, document_id)
SELECT a.additive_id, 'EFSA', 'No longer considered safe as a food additive; genotoxicity concern could not be ruled out',
       'B', DATE '2021-05-06', d.document_id
FROM kb.additive a JOIN reg.document d ON d.citation = 'EFSA Journal 2021 E171'
WHERE a.ins_number = '171'
ON CONFLICT DO NOTHING;

INSERT INTO reg.additive_hazard (additive_id, authority, finding, concern_tier, as_of, document_id)
SELECT a.additive_id, 'IARC', 'Group 2B (possibly carcinogenic to humans); JECFA acceptable daily intake unchanged',
       'C', DATE '2023-07-14', d.document_id
FROM kb.additive a JOIN reg.document d ON d.citation = 'IARC-JECFA aspartame 2023'
WHERE a.ins_number = '951'
ON CONFLICT DO NOTHING;

-- Scoring model registry placeholders (parameters are filled from the official specifications; see doc 03).
INSERT INTO rec.scoring_model (code, version, jurisdiction, status, methodology_url) VALUES
    ('IN_INR_DRAFT',    '0.1', 'IN', 'PREVIEW', NULL),
    ('HSR',             '0.1', 'AU', 'PREVIEW', NULL),
    ('NUTRISCORE_2023', '0.1', 'EU', 'PREVIEW', NULL),
    ('US_FDA_FOP',      '0.1', 'US', 'PREVIEW', NULL),
    ('US_FDA_HEALTHY',  '0.1', 'US', 'PREVIEW', NULL),
    ('UK_NPM_2004',     '0.1', 'GB', 'PREVIEW', NULL),
    ('NOVA',            '0.1', NULL, 'PREVIEW', NULL),
    ('ADDITIVE_TIERS',  '0.1', NULL, 'PREVIEW', NULL),
    ('HSI',             '0.1', NULL, 'PREVIEW', NULL)
ON CONFLICT (code, version) DO NOTHING;
