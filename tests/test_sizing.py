"""Position sizing: deploy everything, overspend nothing."""

from __future__ import annotations

import pytest

from trading.models import Balances
from trading.sizing import calculate_quantity, spendable_cash
from tests.conftest import make_settings


def bal(cash=10_000.0, bp=None, settled=None) -> Balances:
    return Balances(
        cash_available=cash,
        buying_power=cash if bp is None else bp,
        cash_balance=cash,
        settled_cash=settled,
    )


def test_deploys_the_target_band_in_whole_lots(tmp_path):
    """The intended shape: divisible by 10 and inside 95-98% of cash."""
    s = make_settings(tmp_path, cash_buffer_dollars=25.0)
    d = calculate_quantity(bal(1_000_000.0), 100.00, s)
    assert d.quantity == 9_800
    assert d.quantity % 10 == 0
    assert 95.0 <= d.deployed_pct <= 98.0
    assert d.estimated_cost <= 1_000_000.0 - 25.0


def test_never_deploys_more_than_the_target_ceiling(tmp_path):
    s = make_settings(tmp_path, cash_buffer_dollars=0)
    for cash in (12_345.0, 250_000.0, 1_000_000.0):
        for price in (32.11, 100.0, 500.08):
            d = calculate_quantity(bal(cash), price, s)
            assert d.deployed_pct <= 98.0 + 1e-9, (cash, price)
            if d.lot_size == 10:
                assert d.quantity % 10 == 0, (cash, price)


def test_lots_that_miss_the_band_fall_back_to_single_shares(tmp_path):
    """A lot worth more than the band is wide would under-invest the account.

    $10k at $100 a share allows 98 shares at the ceiling, but the nearest
    whole lot is 90 (90%). Single shares are used instead, and the switch is
    recorded.
    """
    s = make_settings(tmp_path, cash_buffer_dollars=25.0)
    d = calculate_quantity(bal(10_000.0), 100.00, s)
    assert d.quantity == 98
    assert d.lot_size == 1
    assert any("SHARE_LOT_SIZE=10 skipped" in c for c in d.caps_applied)


def test_small_account_buys_single_shares_of_an_expensive_symbol(tmp_path):
    """QQQ at $500: $5k used to buy nothing (no whole lot of 10 fits)."""
    s = make_settings(tmp_path, cash_buffer_dollars=0)
    d = calculate_quantity(bal(5_000.0), 500.08, s)
    assert d.quantity == 8           # 9 would leave less than one share of cash
    assert calculate_quantity(bal(6_000.0), 500.08, s).quantity == 10


def test_one_share_of_cash_is_always_left(tmp_path):
    s = make_settings(tmp_path, cash_buffer_dollars=25.0, target_cash_max_pct=100.0)
    for cash in (1_000.0, 5_000.0, 12_345.0, 30_000.0, 1_000_000.0):
        for price in (32.11, 100.0, 500.08, 612.40):
            d = calculate_quantity(bal(cash), price, s)
            assert cash - d.estimated_cost >= price + 25.0 - 1e-9, (cash, price)


def test_large_account_keeps_whole_lots(tmp_path):
    s = make_settings(tmp_path, cash_buffer_dollars=25.0)
    d = calculate_quantity(bal(500_000.0), 612.40, s)
    assert d.lot_size == 10
    assert d.quantity % 10 == 0
    assert d.deployed_pct >= 95.0


def test_lot_size_one_restores_share_level_sizing(tmp_path):
    s = make_settings(tmp_path, cash_buffer_dollars=25.0, share_lot_size=1,
                      target_cash_max_pct=100.0)
    d = calculate_quantity(bal(10_000.0), 100.00, s)
    assert d.quantity == 98                     # (10000 - 25 - 100) / 100
    assert d.estimated_cost == 9_800.00


def test_estimated_cost_never_exceeds_available_funds(tmp_path):
    """The invariant that makes an insufficient-funds rejection impossible."""
    s = make_settings(tmp_path, cash_buffer_dollars=25.0)
    for cash in (100.0, 999.99, 1_000.0, 12_345.67, 1_000_000.0):
        for price in (1.01, 32.11, 100.0, 499.99, 500.55):
            d = calculate_quantity(bal(cash), price, s)
            assert d.estimated_cost <= cash - 25.0 + 1e-9, (cash, price)
            assert d.quantity == int(d.quantity) and d.quantity >= 0


def test_buffer_is_respected(tmp_path):
    s = make_settings(tmp_path, cash_buffer_dollars=600.0, share_lot_size=1,
                      target_cash_max_pct=100.0)
    d = calculate_quantity(bal(1_000.0), 500.00, s)
    assert d.quantity == 0                      # only $400 usable
    d2 = calculate_quantity(bal(1_700.0), 500.00, s)
    assert d2.quantity == 1                     # 1700 - 600 - one share = 600


def test_margin_is_not_used_by_default(tmp_path):
    s = make_settings(tmp_path, allow_margin=False, cash_buffer_dollars=0,
                      share_lot_size=1, target_cash_max_pct=100.0)
    # Broker advertises 4x buying power; we must ignore it.
    d = calculate_quantity(bal(cash=10_000.0, bp=40_000.0), 100.0, s)
    assert d.quantity == 99                     # one share of cash kept back
    assert d.margin_allowed is False


def test_margin_used_only_when_explicitly_enabled(tmp_path):
    s = make_settings(tmp_path, allow_margin=True, cash_buffer_dollars=0,
                      share_lot_size=1, target_cash_max_pct=100.0)
    d = calculate_quantity(bal(cash=10_000.0, bp=40_000.0), 100.0, s)
    assert d.quantity == 399


def test_buying_power_below_cash_constrains_us(tmp_path):
    """A margin call or restriction can make buying power the binding limit."""
    s = make_settings(tmp_path, allow_margin=False, cash_buffer_dollars=0,
                      share_lot_size=1, target_cash_max_pct=100.0)
    d = calculate_quantity(bal(cash=10_000.0, bp=2_000.0), 100.0, s)
    assert d.quantity == 19


def test_notional_and_share_caps(tmp_path):
    s = make_settings(tmp_path, max_order_notional_dollars=5_000.0,
                      cash_buffer_dollars=0)
    d = calculate_quantity(bal(50_000.0), 100.0, s)
    assert d.quantity == 50
    assert any("MAX_ORDER_NOTIONAL" in c for c in d.caps_applied)

    s2 = make_settings(tmp_path, max_shares_per_order=10, cash_buffer_dollars=0)
    d2 = calculate_quantity(bal(50_000.0), 100.0, s2)
    assert d2.quantity == 10


def test_fees_are_deducted(tmp_path):
    s = make_settings(tmp_path, cash_buffer_dollars=0, share_lot_size=1,
                      target_cash_max_pct=100.0,
                      estimated_fee_per_share=0.005, estimated_fee_fixed=1.0)
    d = calculate_quantity(bal(1_000.0), 100.0, s)
    assert d.quantity == 8                      # one share of cash kept back
    # $1.00 fixed + 8 * $0.005, reported rounded to cents.
    assert d.estimated_fees == pytest.approx(1.04, abs=0.005)


def test_insufficient_cash_returns_zero(tmp_path):
    s = make_settings(tmp_path)
    d = calculate_quantity(bal(10.0), 500.0, s)
    assert d.quantity == 0


def test_spendable_cash_handles_zero_buying_power(tmp_path):
    """Some endpoints report 0 buying power; that must not zero our size."""
    s = make_settings(tmp_path, allow_margin=False)
    assert spendable_cash(bal(cash=5_000.0, bp=0.0), s) == 5_000.0
