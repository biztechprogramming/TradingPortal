"""Cancel or re-price one working order from the dashboard, even mid-command."""

from __future__ import annotations

import pytest

from trading.errors import LegCancelledOutside, OrderValidationError
from trading.execution import ExecutionEngine, LegRequest
from trading.models import Command, Side
from trading.order_actions import cancel_one_order, change_order_price
from trading.orchestrator import TradingService
from tests.conftest import make_broker, make_settings


def _svc(tmp_path, positions=None, **overrides):
    s = make_settings(tmp_path, health_check_interval_seconds=0, **overrides)
    b = make_broker(s, "sim", cash=10_000.0, positions=positions)
    return TradingService(s, brokers={"sim": b}), b


def _resting_sell(svc, b):
    svc.execute(Command.GO_FLAT, limit_prices={"QQQ": 510.00})
    (order,) = [o for o in b._orders.values() if o.state.is_open]
    return order


def test_change_price_of_a_working_sell(tmp_path):
    svc, b = _svc(tmp_path, positions={"QQQ": 10})
    order = _resting_sell(svc, b)
    out = change_order_price(svc, "sim", order.order_id, 505.004)
    assert out["order"]["limit_price"] == 505.00
    working = [o for o in b._orders.values() if o.state.is_open]
    assert [(o.limit_price, o.quantity) for o in working] == [(505.00, 10)]


def test_cancel_a_working_sell(tmp_path):
    svc, b = _svc(tmp_path, positions={"QQQ": 10})
    order = _resting_sell(svc, b)
    cancel_one_order(svc, "sim", order.order_id)
    assert not any(o.state.is_open for o in b._orders.values())
    assert b.get_position("QQQ").quantity == 10


def test_unknown_order_is_refused(tmp_path):
    svc, _ = _svc(tmp_path, positions={"QQQ": 10})
    with pytest.raises(OrderValidationError, match="not a working"):
        cancel_one_order(svc, "sim", "NOPE")


def test_fat_finger_price_change_is_refused(tmp_path):
    svc, b = _svc(tmp_path, positions={"QQQ": 10})
    order = _resting_sell(svc, b)
    with pytest.raises(OrderValidationError, match="fat-finger"):
        change_order_price(svc, "sim", order.order_id, 50.0)


def _engine(svc, sleep):
    return ExecutionEngine(svc.settings, svc.audit, "t", sleep=sleep)


def test_leg_follows_a_price_change_made_while_it_watches(tmp_path):
    svc, b = _svc(tmp_path, positions={"QQQ": 10}, order_fill_timeout_seconds=30)
    changed = []

    def sleep(_):
        if not changed:
            (o,) = [o for o in b._orders.values() if o.state.is_open]
            changed.append(change_order_price(svc, "sim", o.order_id, 499.90))

    req = LegRequest(symbol="QQQ", side=Side.SELL, quantity=10,
                     position_quantity=10, fixed_limit=510.00)
    fill = _engine(svc, sleep).execute_leg(b, req)
    assert fill.complete          # the lower, marketable replacement filled
    assert "price changed to $499.90" in " ".join(fill.notes)


def test_leg_reports_a_cancel_made_while_it_watches(tmp_path):
    svc, b = _svc(tmp_path, positions={"QQQ": 10}, order_fill_timeout_seconds=30)

    def sleep(_):
        for o in [o for o in b._orders.values() if o.state.is_open]:
            cancel_one_order(svc, "sim", o.order_id)

    req = LegRequest(symbol="QQQ", side=Side.SELL, quantity=10,
                     position_quantity=10, fixed_limit=510.00)
    with pytest.raises(LegCancelledOutside, match="cancelled from the dashboard"):
        _engine(svc, sleep).execute_leg(b, req)
