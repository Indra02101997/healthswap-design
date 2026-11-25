"""End-to-end knowledge pipeline on PostgreSQL (`integration`): the knowledge steps of the C# DemoSeeder
(Seeding/DemoSeeder.cs, i.e. `hs seed` without --demo) run through the Python ports in the same order - reference data,
brand portal, USDA FDC, Open Food Facts, openFDA recalls, scoring, swap graphs, explanations - with the Mock LLM provider
(fault injection every 7th explanation call) and an in-memory hot cache.

Runs on a fresh dev database of its own (the `fresh_database_url` fixture in tests/conftest.py): the test writes the whole
knowledge base. The expected numbers are those the C# seeder reports on a fresh database; the digests
are SHA-256 values over the sorted hashes the C# seeder stored (label hashes, score inputs hashes, LLM audit input hashes,
explanation fact-sheet hashes), identical in two independent C# runs, so the hashed texts are byte-identical to C#.

Which two US explanations receive the mock's every-7th-call fault is NOT asserted: the C# jobs read rec.health_profile and
rec.v_active_swap_edge without ORDER BY, so the call order follows the heap layout and differs between C# runs too.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from healthswap.domain.primitives import SystemClock
from healthswap.platform.cache import MemoryCacheStore
from healthswap.platform.db import Db, DbOptions, DbRole, Uow
from healthswap.platform.hot_cache import HotCache
from healthswap.platform.knowledge.connectors import Connectors
from healthswap.platform.knowledge.ingestor import IngestReport, ProductIngestor
from healthswap.platform.knowledge.jobs import ExplanationPregen, ExplanationReport, ScoringJob, ScoringReport, SwapGraphBuilder
from healthswap.platform.knowledge.kb_reader import KbReader, RecallIngestor
from healthswap.platform.knowledge.reference_data import ReferenceDataLoader, ReferenceDataRepository
from healthswap.platform.llm.gateway import LlmGateway
from healthswap.platform.llm.mock import MockLlmProvider
from healthswap.platform.ml.registry import ModelRegistry
from healthswap.platform.settings import load_settings


def _summ(r: IngestReport) -> tuple[int, int, int, int, int, int]:
    """DemoSeeder.Summ: (Received, Created, NewVersions, Unchanged, Rejected, CurationTasks)."""
    return r.received, r.created, r.new_versions, r.unchanged, r.rejected, r.curation_tasks


def _rows(u: Uow, sql: str) -> list[tuple[object, ...]]:
    return [tuple(r.values()) for r in u.query(sql)]


def _digest(u: Uow, sql: str) -> str:
    """SHA-256 over the sorted values of a one-column (hex text) query, one per line."""
    return hashlib.sha256("\n".join(sorted(str(v) for (v,) in _rows(u, sql))).encode()).hexdigest()


@pytest.fixture()
def fresh_db(fresh_database_url: str) -> Iterator[Db]:
    db = Db(DbOptions(url=fresh_database_url, max_pool_size=4))
    db.wait_ready(30)
    try:
        yield db
    finally:
        db.close()


@pytest.mark.integration
def test_knowledge_pipeline_matches_the_csharp_seeder(fresh_db: Db, tmp_path: Path) -> None:
    db = fresh_db
    settings = load_settings(
        overrides={
            "database.url": db.options.url, "llm.provider": "Mock", "llm.mock_fault_every": 7, "llm.enable_explanation_polish": True,
            "graph.llm_judge_uncertain": True, "lake.root": str(tmp_path / "lake"),
        },
        env={},
    )
    root = settings.repo_root
    clock = SystemClock()
    # Singletons in the C# host: one gateway (result cache, per-task budgets) and one mock provider (fault counter).
    llm = LlmGateway(MockLlmProvider(settings.llm), settings.llm, db)
    models = ModelRegistry(settings.models)
    refs = ReferenceDataRepository(db)
    cache = HotCache(MemoryCacheStore())
    ingestor = ProductIngestor(db, refs, models, llm, clock, settings.lake)

    now = clock.utc_now()
    ReferenceDataLoader(db).apply_all(root)
    refs.invalidate()
    brand_portal = ingestor.ingest(list(Connectors.brand_portal(root / "data" / "seed" / "kb" / "products.json", now)))
    fixtures = root / "data" / "fixtures"
    usda_fdc = ingestor.ingest(list(Connectors.usda_fdc_branded(fixtures / "fdc-branded-sample.json", now)))
    open_food_facts = ingestor.ingest(list(Connectors.open_food_facts(fixtures / "off-sample.jsonl", now)))
    recalls = RecallIngestor(db).ingest(list(Connectors.open_fda_enforcement(fixtures / "openfda-enforcement-sample.json")))
    scoring = ScoringJob(db, KbReader(db, refs), refs, cache, clock).run_all()
    graphs = SwapGraphBuilder(db, refs, models, llm, cache, settings.graph, clock).build_all()
    explanations = ExplanationPregen(db, refs, llm, cache, clock).run_all()

    # C# `hs seed` on a fresh database: 47 brand-portal products, the FDC and OFF records merge into them except one each.
    assert _summ(brand_portal) == (47, 47, 47, 0, 0, 1)
    assert _summ(usda_fdc) == (2, 1, 2, 0, 0, 1)
    assert _summ(open_food_facts) == (3, 1, 3, 0, 0, 0)
    assert recalls == 1
    assert scoring == [ScoringReport("IN", 33, 32, 33, 12), ScoringReport("US", 16, 15, 16, 7)]
    assert [(g.market, g.status, g.products, g.edges) for g in graphs] == [("IN", "ACTIVE", 11, 15), ("US", "ACTIVE", 6, 7)]
    assert [g.validation for g in graphs] == [
        {"edges": 15, "productsWithEdges": 11, "edgesIntoIneligible": 0, "edgesBelowDeltaMin": 0, "llmJudged": 4, "substitutabilityModel": "heuristic",
         "rulesVersion": "R-1.3"},
        {"edges": 7, "productsWithEdges": 6, "edgesIntoIneligible": 0, "edgesBelowDeltaMin": 0, "llmJudged": 1, "substitutabilityModel": "heuristic",
         "rulesVersion": "R-1.3"},
    ]
    assert explanations == [ExplanationReport("IN", 15, 30, 26, 4), ExplanationReport("US", 7, 12, 10, 2)]

    # Database state behind the reports.
    with db.tx(DbRole.OPS) as u:
        assert u.scalar("select count(*) from kb.product") == 49
        assert _rows(u, "select status, count(*)::int from kb.product group by status order by status") == [("ACTIVE", 48), ("QUARANTINED", 1)]
        assert u.scalar("select count(*) from kb.product_version") == 52
        assert _rows(u, "select s.code, count(*)::int from kb.source_record r join kb.source s using (source_id) group by s.code order by s.code") == [
            ("BRAND_PORTAL", 47), ("OFF", 3), ("USDA_FDC_BRANDED", 2),
        ]
        # 2 from ingestion (prohibited additive, nutrient conflict) + the recall confirmation.
        assert _rows(u, "select kind, priority, rule_code from kb.curation_task order by kind") == [
            ("COMPLIANCE", "P1", "QA-14"), ("FIELD_CONFLICT", "P2", "nutrient.NA.PER_100G"), ("RECALL_CONFIRM", "P1", "F-9999-2026"),
        ]
        assert _rows(u, "select is_selected, conflict, count(*)::int from kb.field_provenance group by 1, 2 order by 1, 2") == [
            (False, False, 34), (True, False, 657), (True, True, 1),
        ]
        assert _rows(u, "select topic, count(*)::int from txn.outbox group by topic") == [("hs.kb-product-changes.v1", 102)]
        assert _rows(u, "select market, count(*)::int from rec.product_score group by market order by market") == [("IN", 66), ("US", 16)]
        assert u.query("select market, edge_count from rec.swap_graph_version where status = 'ACTIVE' order by market") == [
            {"market": "IN", "edge_count": 15}, {"market": "US", "edge_count": 7},
        ]
        assert u.query("select generator, count(*)::int as n from rec.explanation group by generator order by generator") == [
            {"generator": "LLM_VALIDATED", "n": 36}, {"generator": "TEMPLATE", "n": 6},
        ]
        assert _rows(u, "select task, accepted, count(*)::int from rec.llm_audit group by 1, 2 order by 1, 2") == [
            ("T4", True, 5), ("T5", False, 6), ("T5", True, 36),
        ]
        assert u.scalar("select count(*) from rec.product_embedding") == 49
        # Hashed texts are byte-identical to the C# seeder's.
        assert _digest(u, "select encode(label_hash, 'hex') from kb.product_version") == "e0169e1f0f50c3e5fb6584813f6d11d18345daa4b7c7c650de8ad5c214d1389e"
        assert _digest(u, "select encode(inputs_hash, 'hex') from rec.product_score") == "18fd8f8c3d3343f82ca5461b3bc8003cf6a4d600cfde4cf49a1b21d001a287a3"
        assert _digest(u, "select encode(input_hash, 'hex') from rec.llm_audit") == "27577713e3d265f66b94e574604341ac58dcf922e667365a3ece02a71275b48e"
        assert _digest(u, "select encode(facts_hash, 'hex') from rec.explanation") == "32fe11678feb4d20be0c863ad565e91424a7fb6ba8ddf27f4dc589980c08cdd7"

    # Hot cache: profiles, the active graphs (blue/green pointer + edge lists) and every explanation.
    for g in graphs:
        assert cache.get_active_graph(g.market) == g.graph_version
        assert cache.store.count(f"swap:{g.market}:{g.graph_version}:") == g.products
    assert cache.store.count("hp:") == 49
    assert cache.store.count("expl:") == 42
