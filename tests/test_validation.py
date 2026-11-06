"""Pre-submit validation and the market-hours guard."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from trading.errors import (
    MarketClosed,
    OrderValidationError,
    SafetyLimitExceeded,
)
from trading.models import Balances, Side
from trading.validation import check_market_hours, market_status, validate_order
from tests.conftest import make_settings


def ok_args(**over):
    args = dict(
        symbol="QQQ",
        side=Side.BUY,
        quantity=10,
        limit_price=500.55,
        account_id="ACCT",
    )
    args.update(over)
    return args


def test_valid_order_passes(tmp_path):
    s = make_settings(tmp_path)
    record = validate_order(settings=s, **ok_args())
    assert record["estimated_value"] == 5005.50


@pytest.mark.parametrize("bad", ["SPY", "TQQQ", "", "qqq3"])
def test_only_the_two_configured_symbols_are_tradable(tmp_path, bad):
    s = make_settings(tmp_path)
    with pytest.raises(OrderValidationError, match="not tradable"):
        validate_order(settings=s, **ok_args(symbol=bad))


@pytest.mark.parametrize("qty", [0, -5, 10.5])
def test_quantity_must_be_a_positive_whole_number(tmp_path, qty):
    s = make_settings(tmp_path)
    with pytest.raises(OrderValidationError):
        validate_order(settings=s, **ok_args(quantity=qty))


def test_limit_price_must_be_positive(tmp_path):
    s = make_settings(tmp_path)
    with pytest.raises(OrderValidationError, match="limit price"):
        validate_order(settings=s, **ok_args(limit_price=0))


def test_account_must_be_selected(tmp_path):
    s = make_settings(tmp_path)
    with pytest.raises(OrderValidationError, match="no account"):
        validate_order(settings=s, **ok_args(account_id=None))


def test_buy_beyond_available_funds_is_blocked(tmp_path):
    s = make_settings(tmp_path, cash_buffer_dollars=25.0)
    balances = Balances(cash_available=1_000.0, buying_power=1_000.0)
    with pytest.raises(OrderValidationError, match="exceeds available"):
        validate_order(settings=s, balances=balances, **ok_args(quantity=10))


def test_selling_more_than_held_is_blocked(tmp_path):
    """The system liquidates; it must never establish a short by accident."""
    s = make_settings(tmp_path)
    with pytest.raises(OrderValidationError, match="never sells short"):
        validate_order(
            settings=s, position_quantity=5,
            **ok_args(side=Side.SELL, quantity=10),
        )


def test_notional_cap_enforced(tmp_path):
    s = make_settings(tmp_path, max_order_notional_dollars=100.0)
    with pytest.raises(SafetyLimitExceeded, match="MAX_ORDER_NOTIONAL"):
        validate_order(settings=s, **ok_args())


def test_share_cap_enforced(tmp_path):
    s = make_settings(tmp_path, max_shares_per_order=5)
    with pytest.raises(SafetyLimitExceeded, match="MAX_SHARES_PER_ORDER"):
        validate_order(settings=s, **ok_args(quantity=10))


# --- market hours -----------------------------------------------------------


def test_market_status_weekday_open():
    # Thursday 2026-08-13, 14:00 UTC == 10:00 ET
    now = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)
    assert market_status(now).is_open


def test_market_status_weekend_closed():
    now = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)  # Saturday
    status = market_status(now)
    assert not status.is_open and status.reason == "weekend"


def test_market_status_holiday_closed():
    now = datetime(2026, 12, 25, 15, 0, tzinfo=timezone.utc)
    assert market_status(now).reason == "market holiday"


def test_market_status_after_hours_is_extended():
    now = datetime(2026, 8, 13, 21, 30, tzinfo=timezone.utc)  # 17:30 ET
    status = market_status(now)
    assert not status.is_open and status.session == "extended"


def test_market_status_pre_market_is_extended():
    now = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)   # 08:00 ET
    assert market_status(now).session == "extended"


def test_market_status_overnight_is_closed():
    now = datetime(2026, 8, 14, 1, 0, tzinfo=timezone.utc)    # 21:00 ET
    assert market_status(now).session == "closed"


def test_extended_hours_pass_the_guard_and_flag_the_order(tmp_path):
    from trading.validation import wants_outside_rth

    s = make_settings(tmp_path, market_hours_guard=True)
    pre = datetime(2026, 8, 13, 12, 0, tzinfo=timezone.utc)    # 08:00 ET
    regular = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)
    check_market_hours(s, pre)                                  # no raise
    assert wants_outside_rth(s, pre)
    assert not wants_outside_rth(s, regular)


def test_missing_timezone_database_fails_closed(tmp_path, monkeypatch):
    """Without tzdata we cannot tell Eastern from UTC, so we must not guess.

    A UTC fallback would shift the session by 4-5 hours: pre-market trading
    would be permitted and the afternoon blocked, silently.
    """
    import trading.validation as v

    monkeypatch.setattr(v, "TZ_AVAILABLE", False)
    # A moment that is unambiguously inside the regular session.
    open_time = datetime(2026, 8, 13, 14, 0, tzinfo=timezone.utc)

    status = v.market_status(open_time)
    assert not status.is_open
    assert "timezone database unavailable" in status.reason

    with pytest.raises(MarketClosed, match="tzdata"):
        v.check_market_hours(make_settings(tmp_path, market_hours_guard=True),
                             open_time)

    # Still overridable, deliberately.
    v.check_market_hours(
        make_settings(tmp_path, market_hours_guard=False), open_time
    )


def test_guard_blocks_and_can_be_overridden(tmp_path):
    closed = datetime(2026, 8, 15, 14, 0, tzinfo=timezone.utc)   # Saturday
    s = make_settings(tmp_path, market_hours_guard=True)
    with pytest.raises(MarketClosed):
        check_market_hours(s, closed)

    check_market_hours(make_settings(tmp_path, market_hours_guard=False), closed)
    check_market_hours(
        make_settings(tmp_path, market_hours_guard=True, allow_outside_rth=True),
        closed,
    )
