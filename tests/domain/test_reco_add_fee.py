"""RA-1: INR 1.00 per add of a HealthSwap-recommended (labeled) product, with an evidence chain and undo grace (port of RecoAddFeeTests.cs).

The file also holds the ported LedgerTests, plus Python-only checks of the PartnerConfig JSON mapping (System.Text.Json does
that work in C#).
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from healthswap.domain.commerce.fees import RecoAddFeeRule, RecoChargeEvidence, RecoChargeReasons, RecoFeeSnapshot
from healthswap.domain.commerce.ledger import Accounts, InvalidPostingError, Postings, StatementTotalsData
from healthswap.domain.commerce.partner_config import FeeConfig, PartnerConfig, RecommendationAddFeeConfig
from healthswap.domain.primitives import UTC

T0 = datetime(2026, 11, 4, 10, 15, 30, tzinfo=UTC)
ZERO = D(0)


def fee() -> RecoFeeSnapshot:
    snapshot = RecoAddFeeRule.snapshot(RecommendationAddFeeConfig(), "INR")
    assert snapshot is not None
    return snapshot


def evidence(impression: datetime | None = None, served: bool = True, recommended: bool = True, finals: int = 0, mtd: D = ZERO) -> RecoChargeEvidence:
    return RecoChargeEvidence(
        suggestion_served=served, sku_recommended=recommended, first_impression_at=impression if impression is not None else T0 + timedelta(seconds=2),
        suggestion_created_at=T0, attribution_expires_at=T0 + timedelta(hours=24), final_charges_for_suggestion=finals, month_to_date_amount=mtd,
    )


class TestRecoAddFee:
    def test_default_terms_are_one_rupee_per_add_event(self) -> None:
        s = fee()
        assert s.rule == "RA-1"
        assert s.amount == D("1.00")
        assert s.basis == "PER_ADD_EVENT"
        assert RecoAddFeeRule.amount(s, D(3)) == D("1.00")  # quantity does not matter per add event
        assert RecoAddFeeRule.amount(replace(s, basis="PER_UNIT"), D(3)) == D("3.00")

    def test_no_snapshot_when_disabled_or_currency_differs(self) -> None:
        assert RecoAddFeeRule.snapshot(RecommendationAddFeeConfig(enabled=False), "INR") is None
        assert RecoAddFeeRule.snapshot(RecommendationAddFeeConfig(), "USD") is None
        assert RecoAddFeeRule.snapshot(RecommendationAddFeeConfig(amount=D(0)), "INR") is None

    @pytest.mark.parametrize(
        ("event_type", "charge_accept", "expected"),
        [
            ("ADD_TO_CART", True, True),
            ("ACCEPT", True, True),
            ("ACCEPT", False, False),
            ("IMPRESSION", True, False),
            ("UNDO", True, False),
        ],
    )
    def test_chargeable_events(self, event_type: str, charge_accept: bool, expected: bool) -> None:
        assert RecoAddFeeRule.is_chargeable_event(event_type, replace(fee(), charge_accept=charge_accept)) == expected

    def test_undo_within_grace_voids_after_grace_does_not(self) -> None:
        s = fee()
        assert RecoAddFeeRule.undo_voids(T0, T0 + timedelta(seconds=5), s)
        assert not RecoAddFeeRule.undo_voids(T0, T0 + timedelta(seconds=6), s)
        assert not RecoAddFeeRule.undo_voids(T0, T0 + timedelta(seconds=-1), s)

    def test_charges_settle_after_the_window_even_for_late_events(self) -> None:
        s = fee()
        assert RecoAddFeeRule.finalizes_at(T0, T0 + timedelta(seconds=1), s) == T0 + timedelta(seconds=600)
        # An add reported an hour late still gets the undo grace from receipt.
        assert RecoAddFeeRule.finalizes_at(T0, T0 + timedelta(hours=1), s) == T0 + timedelta(hours=1, seconds=5)

    def test_full_evidence_chain_is_final(self) -> None:
        assert RecoAddFeeRule.decide(T0 + timedelta(seconds=10), D(1), fee(), evidence()) == ("FINAL", None)

    def test_each_missing_link_is_rejected_with_a_reason(self) -> None:
        s = fee()
        at = T0 + timedelta(seconds=10)
        assert RecoAddFeeRule.decide(at, D(1), s, evidence(served=False))[1] == RecoChargeReasons.NOT_SERVED
        assert RecoAddFeeRule.decide(at, D(1), s, evidence(recommended=False))[1] == RecoChargeReasons.SKU_NOT_RECOMMENDED
        assert RecoAddFeeRule.decide(at, D(1), s, evidence(impression=T0 + timedelta(minutes=5)))[1] == RecoChargeReasons.NO_IMPRESSION
        assert RecoAddFeeRule.decide(at, D(1), s, replace(evidence(), first_impression_at=None))[1] == RecoChargeReasons.NO_IMPRESSION
        assert RecoAddFeeRule.decide(T0 + timedelta(hours=25), D(1), s, evidence())[1] == RecoChargeReasons.OUTSIDE_WINDOW
        assert RecoAddFeeRule.decide(at, D(1), replace(s, max_chargeable_adds_per_suggestion=2), evidence(finals=2))[1] == RecoChargeReasons.CAP_REACHED
        assert RecoAddFeeRule.decide(at, D(1), replace(s, monthly_cap=D(500)), evidence(mtd=D(500)))[1] == RecoChargeReasons.CAP_REACHED

    def test_impression_clock_skew_is_tolerated(self) -> None:
        assert RecoAddFeeRule.decide(T0 + timedelta(seconds=10), D(1), fee(), evidence(impression=T0 + timedelta(seconds=14)))[0] == "FINAL"

    @pytest.mark.parametrize("locale", ["en-IN", "hi-IN", "en-US", "es-US"])
    def test_label_and_disclosure_never_state_the_amount(self, locale: str) -> None:
        labels = pytest.importorskip("healthswap.domain.recommendation.labels", reason="Recommendation/Labels.cs (ShopperCopy) is not ported yet")
        label = labels.ShopperCopy.label(locale, "QuickBasket", add_fee_enabled=True)
        assert label.code == labels.ShopperCopy.RECOMMENDED_CODE
        assert label.chargeable
        assert label.fee_rule == "RA-1"
        assert "₹" not in label.disclosure
        assert "$" not in label.disclosure
        assert not re.search(r"\d", label.disclosure)
        assert "QuickBasket" in label.disclosure

    def test_disclosure_mentions_add_fee_only_when_enabled(self) -> None:
        labels = pytest.importorskip("healthswap.domain.recommendation.labels", reason="Recommendation/Labels.cs (ShopperCopy) is not ported yet")
        assert "add a recommended item" in labels.ShopperCopy.fee_disclosure("en-IN", "QuickBasket", True)
        assert "add a recommended item" not in labels.ShopperCopy.fee_disclosure("en-IN", "QuickBasket", False)

    def test_partner_config_validation_covers_ra1(self) -> None:
        cfg = PartnerConfig(fee=FeeConfig(recommendation_add=RecommendationAddFeeConfig(basis="PER_DAY", settle_after_seconds=1, undo_grace_seconds=5)))
        errors = cfg.validate()
        assert any("basis" in e for e in errors)
        assert any("settleAfterSeconds" in e for e in errors)
        assert PartnerConfig().validate() == []


class TestLedger:
    def test_all_postings_balance(self) -> None:
        id_ = uuid.uuid4()
        for p in [
            Postings.fee_accrual(id_, D("5.80")), Postings.fee_reversal(id_, "r1", D("2.90")), Postings.fee_finalize(id_, D("2.90")),
            Postings.reco_fee(id_, D("1.00")), Postings.statement(id_, D("4.90"), D("0.88")), Postings.payment("p1", id_, D("5.78")),
            Postings.credit_note(id_, D(0), D("1.00"), D("0.18")),
        ]:
            assert p.total_debit == p.total_credit
            assert all((line.debit == 0) != (line.credit == 0) for line in p.lines)

    def test_ra1_is_recognized_as_separate_revenue(self) -> None:
        p = Postings.reco_fee(uuid.uuid4(), D("1.00"))
        assert any(line.account == Accounts.RECO_FEE_REVENUE and line.credit == D("1.00") for line in p.lines)
        assert any(line.account == Accounts.UNBILLED_AR and line.debit == D("1.00") for line in p.lines)

    def test_statement_totals_combine_fp1_and_ra1(self) -> None:
        # Smoke-test scenario: FP-1 5.80 with a 2.90 return reversal, plus two RA-1 adds, 18% GST.
        t = StatementTotalsData("INR", D("5.80"), D("2.00"), 2, D("2.90"), D(0), D("0.18"), D(0))
        assert t.gross_fee == D("7.80")
        assert t.net_fee == D("4.90")
        assert t.tax == D("0.88")
        assert t.total_due == D("5.78")

    def test_unbalanced_or_negative_postings_are_impossible(self) -> None:
        with pytest.raises(InvalidPostingError):
            Postings.fee_accrual(uuid.uuid4(), D(-1))


class TestPartnerConfigJson:
    """Python-only: C# gets this from System.Text.Json (HsJson web defaults, strict numbers, AllowReadingFromString on money fields)."""

    def test_seed_configs_load_validate_and_round_trip(self, root: Path) -> None:
        partners = json.loads((root / "data" / "seed" / "demo" / "partners.json").read_text(encoding="utf-8"))["partners"]
        configs = [PartnerConfig.from_dict(p["config"]) for p in partners]
        for cfg in configs:
            assert cfg.validate() == []
            assert PartnerConfig.from_dict(json.loads(json.dumps(cfg.to_dict()))) == cfg
        quick_basket, hometown = configs
        assert quick_basket.fee.caps.per_line == D("50.00") and quick_basket.fee.caps.per_month is None
        assert quick_basket.fee.recommendation_add.amount == D("1.00") and quick_basket.inventory_staleness_seconds == 86400
        assert quick_basket.price_guards.max_abs_premium_per_unit == D(150) and quick_basket.exploration_rate == 0
        assert hometown.placements == PartnerConfig().placements  # absent -> C# initializer
        assert hometown.fee.recommendation_add.enabled is False and hometown.fee.recommendation_add.monthly_cap is None
        assert hometown.return_window_for("PICKUP") == 30 and hometown.return_window_for("DRONE") == 30
        assert RecoAddFeeRule.snapshot(quick_basket.fee.recommendation_add, quick_basket.currency) == fee()

    def test_mapping_follows_the_csharp_serializer(self) -> None:
        cfg = PartnerConfig.from_dict({"Currency": "USD", "unknownSetting": 1, "fee": {"caps": {"PERLINE": 2, "perOrder": "5.00"}}, "returnWindowDays": {}})
        assert cfg.currency == "USD"
        assert cfg.fee.rate == D("0.10") and cfg.fee.caps == replace(cfg.fee.caps, per_line=D(2), per_order=D("5.00"), per_month=None)
        assert cfg.return_window_for("PICKUP") == 7  # empty map -> DefaultIfEmpty(7)
        for bad in ({"taxRate": "0.18"}, {"maxLines": 2.5}, {"killSwitch": 1}, {"markets": "IN"}, {"fee": None}, {"fee": {"caps": {"perLine": "1e"}}}):
            with pytest.raises(ValueError):
                PartnerConfig.from_dict(bad)
        d = PartnerConfig().to_dict()
        assert d["fee"]["caps"] == {"perLine": "50", "perOrder": "120", "perMonth": None, "currency": "INR"}
        assert d["fee"]["recommendationAdd"]["amount"] == "1.00" and d["taxRate"] == 0.18 and d["markets"] == ["IN"]
        assert PartnerConfig.from_dict(d) == PartnerConfig()
