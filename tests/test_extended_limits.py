"""Extended hours: GO LONG / GO SHORT run only at a limit the user chose."""

from __future__ import annotations

import pytest

import trading.orchestrator as orch
from trading.errors import OrderValidationError
from trading.models import Command
from trading.orchestrator import TradingService
from trading.validation import MarketStatus
from tests.conftest import make_broker, make_settings


@pytest.fixture
def extended(monkeypatch):
    monkeypatch.setattr(orch, "market_status", lambda now=None: MarketStatus(
        is_open=False, reason="pre-market", now_eastern="", session="extended"))


def _svc(tmp_path, positions=None, **overrides):
    s = make_settings(tmp_path, extended_hours_require_limit=True,
                      health_check_interval_seconds=0, **overrides)
    b = make_broker(s, "sim", cash=10_000.0, positions=positions)
    return TradingService(s, brokers={"sim": b}), b


def test_extended_hours_go_long_without_a_limit_is_refused(tmp_path, extended):
    svc, b = _svc(tmp_path)
    with pytest.raises(OrderValidationError, match="buy limit you choose for QQQ"):
        svc.execute(Command.GO_LONG)
    assert b.get_open_orders() == []


def test_go_flat_needs_no_limit(tmp_path, extended):
    svc, _ = _svc(tmp_path, positions={"QQQ": 5})
    result = svc.execute(Command.GO_FLAT)
    assert result.results[0].ok, result.results[0].errors


def test_chosen_buy_limit_is_placed_exactly_and_sized_at_it(tmp_path, extended):
    svc, b = _svc(tmp_path)
    result = svc.execute(Command.GO_LONG, limit_prices={"QQQ": 500.04})
    r = result.results[0]
    assert r.ok, r.errors
    orders = list(b._orders.values())
    assert {o.limit_price for o in orders} == {500.04}
    # (10000 - 25 buffer) / 500.04, whole shares, sized at the chosen price
    assert sum(f.filled_quantity for f in r.fills) == orders[0].quantity


def test_chosen_limit_is_never_repriced(tmp_path, extended):
    svc, b = _svc(tmp_path, order_fill_timeout_seconds=0.2)
    result = svc.execute(Command.GO_LONG, limit_prices={"QQQ": 499.50})
    assert not result.results[0].ok            # below the ask: did not fill
    orders = list(b._orders.values())
    assert len(orders) == 1 and orders[0].limit_price == 499.50
    assert not orders[0].state.is_open         # remainder cancelled


def test_a_held_opposite_side_needs_a_sell_limit(tmp_path, extended):
    svc, b = _svc(tmp_path, positions={"PSQ": 100})
    result = svc.execute(Command.GO_LONG, limit_prices={"QQQ": 500.04})
    r = result.results[0]
    assert not r.ok
    assert "choose a sell limit for PSQ" in " ".join(r.errors)
    assert b.get_position("PSQ").quantity == 100


def test_sell_then_buy_at_chosen_limits(tmp_path, extended):
    svc, b = _svc(tmp_path, positions={"PSQ": 100})
    result = svc.execute(Command.GO_LONG,
                         limit_prices={"QQQ": 500.04, "PSQ": 32.10})
    r = result.results[0]
    assert r.ok, r.errors
    assert {(o.symbol, o.limit_price) for o in b._orders.values()} == {
        ("PSQ", 32.10), ("QQQ", 500.04)}


def test_fat_finger_limit_is_refused(tmp_path, extended):
    svc, b = _svc(tmp_path)
    result = svc.execute(Command.GO_LONG, limit_prices={"QQQ": 50.0})
    assert not result.results[0].ok
    assert "fat-finger" in " ".join(result.results[0].errors)
    assert b._orders == {}


def test_limit_for_an_unknown_symbol_is_refused(tmp_path, extended):
    svc, _ = _svc(tmp_path)
    with pytest.raises(OrderValidationError, match="only"):
        svc.execute(Command.GO_LONG, limit_prices={"QQQ": 500.0, "SPY": 1.0})


def test_confirmation_is_bound_to_the_prices(tmp_path, extended):
    from trading.errors import ConfirmationRequired

    svc, _ = _svc(tmp_path, require_confirmation=True)
    with pytest.raises(ConfirmationRequired) as first:
        svc.execute(Command.GO_LONG, limit_prices={"QQQ": 500.04})
    # The same token cannot authorise a different price.
    with pytest.raises(ConfirmationRequired):
        svc.execute(Command.GO_LONG, limit_prices={"QQQ": 501.00},
                    confirm_token=first.value.token)
