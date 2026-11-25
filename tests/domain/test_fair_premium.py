"""FP-1 worked examples from docs/01-hld.md section 9.3 and properties of the fair-premium rule (port of FairPremiumTests.cs)."""

from __future__ import annotations

from decimal import Decimal as D

from hypothesis import given, settings
from hypothesis import strategies as st

from healthswap.domain.commerce.fees import FairPremiumInput, FairPremiumResult, FairPremiumRule
from healthswap.domain.commerce.partner_config import FeeCaps
from healthswap.domain.primitives import NetQuantity, round_money

# FsCheck PositiveInt: any Int32 >= 1.
positive_int = st.integers(min_value=1, max_value=2**31 - 1)
ZERO = D(0)


def compute(
    p_orig: D, q_orig: D, s_orig: NetQuantity | None, q_alt: D, net_alt: D, s_alt: NetQuantity | None, currency: str = "INR", original_in_order: D = ZERO
) -> FairPremiumResult:
    return FairPremiumRule.compute(
        FairPremiumInput(
            original_unit_price=p_orig, original_quantity=q_orig, original_size=s_orig, alternative_quantity=q_alt, alternative_net_amount=net_alt,
            alternative_size=s_alt, currency=currency, original_quantity_in_order=original_in_order,
        ),
        D("0.10"),
    )


def test_in1_millet_noodles_fee_is_5_80() -> None:
    # INR 56 x 2 noodles (280 g) -> millet noodles 256 g, list INR 89, INR 8 coupon pro-rated -> net INR 170 for 2 (INR 85 each).
    r = compute(D(56), D(2), NetQuantity(D(280), "g"), D(2), D(170), NetQuantity(D(256), "g"))
    assert r.pack_delta == D("29.00")
    assert r.size_normalized_delta == D("33.80")
    assert r.premium_per_unit == D("29.00")
    assert r.replaced_quantity == 2
    assert r.fee == D("5.80")


def test_in2_bigger_pack_is_charged_on_size_normalized_premium_only() -> None:
    r = compute(D(100), D(1), NetQuantity(D(500), "g"), D(1), D(240), NetQuantity(D(1000), "g"))
    assert r.pack_delta == D("140.00")
    assert r.size_normalized_delta == D("40.00")
    assert r.fee == D("4.00")  # not INR 14: HealthSwap never earns on extra volume


def test_in3_cheaper_swap_earns_nothing() -> None:
    r = compute(D(60), D(1), NetQuantity(D(400), "g"), D(1), D(45), NetQuantity(D(400), "g"))
    assert r.pack_delta == D("-15.00")
    assert r.fee == D(0)


def test_us1_cereal_fee_is_12_cents() -> None:
    r = compute(D("4.29"), D(1), NetQuantity.normalize(D("13.5"), "oz"), D(1), D("5.49"), NetQuantity.normalize(D(12), "oz"), "USD")
    assert r.pack_delta == D("1.20")
    assert r.fee == D("0.12")


def test_original_still_in_the_order_reduces_the_replaced_quantity() -> None:
    r = compute(D(56), D(2), NetQuantity(D(280), "g"), D(2), D(170), NetQuantity(D(256), "g"), original_in_order=D(1))
    assert r.replaced_quantity == 1
    assert r.fee == D("2.90")


def test_returning_one_of_two_units_reverses_half() -> None:
    assert FairPremiumRule.reversal(D("5.80"), D(0), D(2), D(1), "INR") == D("2.90")
    assert FairPremiumRule.reversal(D("5.80"), D("2.90"), D(2), D(2), "INR") == D("2.90")  # never more than what is left


def test_caps_apply_per_line_then_order_then_month() -> None:
    caps = FeeCaps(per_line=D(50), per_order=D(120), per_month=D(1000))
    assert FairPremiumRule.apply_caps(D(80), caps, D(0), D(0)) == D(50)
    assert FairPremiumRule.apply_caps(D(40), caps, D(100), D(0)) == D(20)
    assert FairPremiumRule.apply_caps(D(40), caps, D(0), D(1000)) == D(0)


def test_rounding_is_half_even_in_minor_units() -> None:
    assert round_money(D("0.125"), "USD") == D("0.12")
    assert round_money(D("0.135"), "USD") == D("0.14")
    assert round_money(D("100.4"), "JPY") == D(100)


@settings(max_examples=200)
@given(orig_cents=positive_int, alt_cents=positive_int, qty=positive_int)
def test_fee_never_exceeds_rate_times_extra_paid(orig_cents: int, alt_cents: int, qty: int) -> None:
    p_orig = D(orig_cents % 100_000) / 100 + 1
    p_alt = D(alt_cents % 100_000) / 100 + 1
    q = D(qty % 10 + 1)
    r = compute(p_orig, q, None, q, p_alt * q, None)
    extra_paid = max(D(0), p_alt - p_orig) * q
    assert r.fee >= 0 and r.fee <= round_money(D("0.10") * extra_paid, "INR") and (p_alt > p_orig or r.fee == 0)


@settings(max_examples=200)
@given(orig_cents=positive_int, size_factor=positive_int)
def test_bigger_packs_never_increase_the_fee_beyond_the_pack_premium(orig_cents: int, size_factor: int) -> None:
    p_orig = D(orig_cents % 50_000) / 100 + 1
    factor = size_factor % 4 + 1
    p_alt = p_orig * factor + D(5)  # a bigger pack that costs more in absolute terms
    r = compute(p_orig, D(1), NetQuantity(D(100), "g"), D(1), p_alt, NetQuantity(D(100 * factor), "g"))
    assert r.premium_per_unit <= D("5.0001")
