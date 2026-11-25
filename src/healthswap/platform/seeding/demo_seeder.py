"""Seeding/DemoSeeder.cs ported: builds a complete local environment from versioned files - reference data, the synthetic
knowledge base (brand portal, Open Food Facts and USDA FDC fixtures, an openFDA recall), scores, demo partners (tenant.partner,
OAuth client metadata, stores, a webhook endpoint with an encrypted signing secret), catalogs with enrichment, inventory
(FULL_SNAPSHOT), swap graphs and explanations. Idempotent: safe to re-run.

Client secrets in data/seed/demo/partners.json are NOT written anywhere: tenant.api_client holds metadata only (client id,
partner, scopes, status), exactly like the C# seeder; the development identity provider hashes the secrets in memory
(infrastructure.ClientSecret, PBKDF2-SHA256).
"""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from healthswap.domain.primitives import Clock
from healthswap.platform import jsonutil, netjson
from healthswap.platform.db import Db, DbRole, Uow
from healthswap.platform.knowledge.connectors import Connectors
from healthswap.platform.knowledge.ingestor import IngestReport, ProductIngestor
from healthswap.platform.knowledge.jobs import ExplanationPregen, ScoringJob, SwapGraphBuilder
from healthswap.platform.knowledge.kb_reader import RecallIngestor
from healthswap.platform.knowledge.reference_data import ReferenceDataLoader, ReferenceDataRepository
from healthswap.platform.services.catalog import CatalogEnrichment, CatalogService, InventoryService, read_catalog_item
from healthswap.platform.services.infrastructure import SecretProtector
from healthswap.platform.tenancy import PartnerDirectory

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SeedReport:
    # Step name -> result (strings, numbers, dicts or the job report dataclasses); jsonutil.dumps prints it like the C# CLI.
    steps: dict[str, Any]


def _prop(obj: Mapping[str, Any] | None, name: str, default: Any = None) -> Any:
    """A record property read like System.Text.Json with HsJson.Options (case-insensitive; the last duplicate wins)."""
    if obj is None:
        return default
    value, found = default, False
    lowered = name.lower()
    for key, item in obj.items():
        if str(key).lower() == lowered:
            value, found = item, True
    return value if found else default


def _read_json(path: Path) -> Any:
    """File.ReadAllText (a UTF-8 BOM is skipped) + JsonSerializer.Deserialize: decimals keep their scale."""
    return jsonutil.loads(path.read_text(encoding="utf-8-sig"))


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


class DemoSeeder:
    """
    Builds a complete local environment from versioned files: reference data, the synthetic knowledge base (brand portal,
    Open Food Facts and USDA FDC fixtures, an openFDA recall), demo partners, catalogs, inventory, scores, swap graphs and
    explanations. Idempotent: safe to re-run.
    """

    def __init__(
        self, db: Db, references: ReferenceDataLoader, refs: ReferenceDataRepository, ingestor: ProductIngestor, recalls: RecallIngestor,
        scoring: ScoringJob, graphs: SwapGraphBuilder, explanations: ExplanationPregen, catalog: CatalogService, enrichment: CatalogEnrichment,
        inventory: InventoryService, secrets: SecretProtector, partners: PartnerDirectory, clock: Clock,
    ) -> None:
        self.db = db
        self.references = references
        self.refs = refs
        self.ingestor = ingestor
        self.recalls = recalls
        self.scoring = scoring
        self.graphs = graphs
        self.explanations = explanations
        self.catalog = catalog
        self.enrichment = enrichment
        self.inventory = inventory
        self.secrets = secrets
        self.partners = partners
        self.clock = clock

    def seed(self, repo_root: str | os.PathLike[str], demo_tenants: bool) -> SeedReport:
        root = Path(repo_root)
        steps: dict[str, Any] = {}
        now = self.clock.utc_now()
        self.references.apply_all(root)
        self.refs.invalidate()
        steps["referenceData"] = "applied"

        kb = root / "data" / "seed" / "kb" / "products.json"
        steps["brandPortal"] = self._summ(self.ingestor.ingest(list(Connectors.brand_portal(kb, now))))
        fixtures = root / "data" / "fixtures"
        if (fixtures / "fdc-branded-sample.json").is_file():
            steps["usdaFdc"] = self._summ(self.ingestor.ingest(list(Connectors.usda_fdc_branded(fixtures / "fdc-branded-sample.json", now))))
        if (fixtures / "off-sample.jsonl").is_file():
            steps["openFoodFacts"] = self._summ(self.ingestor.ingest(list(Connectors.open_food_facts(fixtures / "off-sample.jsonl", now))))
        if (fixtures / "openfda-enforcement-sample.json").is_file():
            steps["recalls"] = self.recalls.ingest(list(Connectors.open_fda_enforcement(fixtures / "openfda-enforcement-sample.json")))

        steps["scoring"] = self.scoring.run_all()

        if demo_tenants:
            demo = root / "data" / "seed" / "demo"
            for p in _prop(_read_json(demo / "partners.json"), "partners", []):
                partner_id = uuid.UUID(str(_prop(p, "partnerId")))
                name = _prop(p, "displayName")
                self._upsert_partner(p, partner_id)
                self.partners.invalidate(partner_id)
                catalog_file = _prop(p, "catalogFile")
                if catalog_file is not None:
                    items = [read_catalog_item(i) for i in _prop(_read_json(demo / catalog_file), "items", [])]
                    outcome = self.catalog.upsert(partner_id, items, None)
                    self.enrichment.enrich(partner_id, outcome.needs_enrichment)
                    states: dict[str, int] = {}
                    for r in outcome.result["results"]:
                        key = (r.get("match") or {}).get("state") or r["status"]
                        states[key] = states.get(key, 0) + 1
                    steps[f"catalog:{name}"] = {"accepted": outcome.result["accepted"], "rejected": outcome.result["rejected"], "states": states}
                inventory_file = _prop(p, "inventoryFile")
                if inventory_file is not None:
                    inv = _read_json(demo / inventory_file)
                    currency = _prop(p, "config")["currency"]
                    for store_id, rows in (_prop(inv, "stores") or {}).items():
                        entries = [
                            _compact({
                                "partnerSku": _prop(r, "partnerSku"), "inStock": bool(_prop(r, "inStock", False)),
                                "availableQuantity": _prop(r, "availableQuantity"), "price": {"amount": _prop(r, "price"), "currency": currency},
                                "mrp": None if _prop(r, "mrp") is None else {"amount": _prop(r, "mrp"), "currency": currency},
                            })
                            for r in rows
                        ]
                        ack = self.inventory.upsert(partner_id, store_id, {"snapshotAt": now, "mode": "FULL_SNAPSHOT", "entries": entries})
                        steps[f"inventory:{name}:{store_id}"] = {"accepted": ack["accepted"], "rejected": ack["rejected"]}

        steps["swapGraphs"] = self.graphs.build_all()
        steps["explanations"] = self.explanations.run_all()
        log.info("Seed complete")
        return SeedReport(steps)

    @staticmethod
    def _summ(r: IngestReport) -> dict[str, int]:
        return {"received": r.received, "created": r.created, "newVersions": r.new_versions, "unchanged": r.unchanged, "rejected": r.rejected,
                "curationTasks": r.curation_tasks}

    def _upsert_partner(self, p: Mapping[str, Any], partner_id: uuid.UUID) -> None:
        webhook = _prop(p, "webhook")
        # Encrypt before the transaction (the first use may create the master key file).
        ciphertext = None if webhook is None else self.secrets.protect(_prop(webhook, "secret"))

        def work(u: Uow) -> None:
            u.execute(
                """
                insert into tenant.partner (partner_id, display_name, legal_name, geography, markets, status, config)
                values (@PartnerId, @DisplayName, @LegalName, @Geography, @markets, @Status, @config::jsonb)
                on conflict (partner_id) do update set display_name = excluded.display_name, legal_name = excluded.legal_name, geography = excluded.geography,
                    markets = excluded.markets, status = excluded.status, config = excluded.config
                """,
                {
                    "PartnerId": partner_id, "DisplayName": _prop(p, "displayName"), "LegalName": _prop(p, "legalName"), "Geography": _prop(p, "geography"),
                    "markets": list(_prop(p, "markets", [])), "Status": _prop(p, "status"), "config": netjson.to_json_string(_prop(p, "config")),
                },
            )
            for c in _prop(p, "clients", []):
                u.execute(
                    """
                    insert into tenant.api_client (client_id, partner_id, scopes, status) values (@ClientId, @partnerId, @scopes::text[], 'ACTIVE')
                    on conflict (client_id) do update set partner_id = excluded.partner_id, scopes = excluded.scopes, status = 'ACTIVE'
                    """,
                    {"ClientId": _prop(c, "clientId"), "partnerId": partner_id, "scopes": list(_prop(c, "scopes", []))},
                )
            for s in _prop(p, "stores", []):
                u.execute(
                    """
                    insert into tenant.store (partner_id, store_id, market, postal_code, timezone) values (@partnerId, @StoreId, @Market, @PostalCode, @Timezone)
                    on conflict (partner_id, store_id) do update set market = excluded.market, postal_code = excluded.postal_code, timezone = excluded.timezone, is_active = true
                    """,
                    {"partnerId": partner_id, "StoreId": _prop(s, "storeId"), "Market": _prop(s, "market"), "PostalCode": _prop(s, "postalCode"),
                     "Timezone": _prop(s, "timezone")},
                )
            if webhook is not None:
                url = _prop(webhook, "url")
                u.execute("delete from tenant.webhook_endpoint where partner_id = @partnerId and url = @url", {"partnerId": partner_id, "url": url})
                u.execute(
                    "insert into tenant.webhook_endpoint (partner_id, url, secret_ciphertext, event_types) values (@partnerId, @url, @secret, @types::text[])",
                    {"partnerId": partner_id, "url": url, "secret": ciphertext, "types": list(_prop(webhook, "eventTypes") or [])},
                )
            u.execute(
                "insert into audit.change_log (actor, action, entity_type, entity_id, details) values ('seed', 'PARTNER_UPSERT', 'PARTNER', @id, '{}'::jsonb)",
                {"id": str(partner_id)},
            )

        self.db.in_tx(DbRole.OPS, None, work)
