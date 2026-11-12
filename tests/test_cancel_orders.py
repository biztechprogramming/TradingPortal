"""The cancel-working-orders button."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trading.errors import ExecutionLocked
from trading.models import OrderState, Side
from trading.orchestrator import TradingService
from tests.conftest import make_broker, make_settings


@pytest.fixture
def setup(tmp_path):
    settings = make_settings(tmp_path, api_token="test-token")
    a = make_broker(settings, "a", cash=50_000, positions={"PSQ": 100})
    b = make_broker(settings, "b", cash=50_000)
    # Resting orders: below the ask / above the bid, so they never fill.
    a.submit_limit_order("QQQ", Side.BUY, 5, 480.00)
    a.submit_limit_order("PSQ", Side.SELL, 100, 40.00)
    b.submit_limit_order("PSQ", Side.BUY, 10, 30.00)
    other = b.submit_limit_order("SPY", Side.BUY, 1, 1.00)
    service = TradingService(settings, brokers={"a": a, "b": b})
    return service, a, b, other


def test_cancels_every_working_qqq_psq_order(setup):
    service, a, b, other = setup
    result = service.cancel_working_orders()

    assert result["status"] == "OK"
    assert result["cancelled"] == 3
    assert a.get_open_orders_for({"QQQ", "PSQ"}) == []
    assert b.get_open_orders_for({"QQQ", "PSQ"}) == []
    # Only this app's symbols are touched, and positions stay put.
    assert b.get_order_status(other.order_id).state is OrderState.WORKING
    assert a.get_position("PSQ").quantity == 100


def test_nothing_to_cancel_is_ok(tmp_path):
    settings = make_settings(tmp_path)
    service = TradingService(settings, brokers={"a": make_broker(settings, "a")})
    result = service.cancel_working_orders()
    assert result["status"] == "OK"
    assert result["cancelled"] == 0


def test_refuses_while_a_command_is_running(setup):
    service, a, *_ = setup
    with service.lock.acquire(owner="test"):
        with pytest.raises(ExecutionLocked):
            service.cancel_working_orders()
    assert len(a.get_open_orders_for({"QQQ", "PSQ"})) == 2


def test_one_account_failing_does_not_stop_the_other(setup):
    service, a, b, _ = setup

    def broken(order_id):
        raise RuntimeError("broker down")

    a.cancel_order = broken
    result = service.cancel_working_orders()
    assert result["status"] == "PARTIAL"
    assert len(result["results"]["a"]["errors"]) == 2
    assert b.get_open_orders_for({"QQQ", "PSQ"}) == []


def test_api_route(setup):
    from trading.api import create_app

    service, *_ = setup
    with TestClient(create_app(service.settings, service)) as client:
        assert client.post("/api/cancel-orders").status_code == 401
        resp = client.post(
            "/api/cancel-orders", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    assert resp.json()["cancelled"] == 3
