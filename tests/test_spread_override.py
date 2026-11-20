"""The per-command spread override, and the absence of market orders."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trading.brokers.base import Broker
from trading.config import DEFAULT_MAX_SPREAD_TICKS
from trading.errors import OrderValidationError
from trading.models import Command
from trading.orchestrator import TradingService
from tests.conftest import make_broker, make_settings


def test_brokers_cannot_place_market_orders():
    assert not hasattr(Broker, "submit_market_order")


# --- spread ---------------------------------------------------------------


def wide_service(tmp_path, spread):
    settings = make_settings(tmp_path)
    broker = make_broker(settings, "sim", cash=10_000,
                         qqq_bid=500.00, qqq_ask=500.00 + spread)
    return TradingService(settings, brokers={"sim": broker})


def test_wide_spread_blocks_a_limit_command(tmp_path):
    result = wide_service(tmp_path, 0.15).execute(Command.GO_LONG)
    assert result.status == "FAILED"
    assert "WideSpreadError" in result.results[0].errors[0]


def test_qqq_spread_widens_for_one_command(tmp_path):
    service = wide_service(tmp_path, 0.15)
    result = service.execute(Command.GO_LONG, max_spread_ticks={"QQQ": 20})
    assert result.status == "OK"
    # The configured limit itself is untouched.
    assert service.settings.max_spread_ticks_for("QQQ") == 10


def test_qqq_spread_can_narrow(tmp_path):
    result = wide_service(tmp_path, 0.04).execute(
        Command.GO_LONG, max_spread_ticks={"QQQ": 3})
    assert result.status == "FAILED"
    assert "WideSpreadError" in result.results[0].errors[0]


def test_symbols_have_independent_spread_limits(tmp_path):
    # Widening QQQ does nothing for a PSQ that is 5 cents wide.
    settings = make_settings(
        tmp_path, max_spread_ticks_by_symbol=dict(DEFAULT_MAX_SPREAD_TICKS))
    broker = make_broker(settings, "sim", cash=10_000,
                         psq_bid=32.10, psq_ask=32.15)
    service = TradingService(settings, brokers={"sim": broker})
    assert service.execute(
        Command.GO_SHORT, max_spread_ticks={"QQQ": 50}).status == "FAILED"
    assert service.execute(
        Command.GO_SHORT, max_spread_ticks={"PSQ": 5}).status == "OK"


def test_psq_default_is_tighter_than_qqq():
    assert DEFAULT_MAX_SPREAD_TICKS == {"QQQ": 10.0, "PSQ": 3.0}


@pytest.mark.parametrize("override", [{"QQQ": 500}, {"QQQ": 0}, {"SPY": 5}])
def test_bad_spread_override_is_rejected(service, override):
    with pytest.raises(OrderValidationError):
        service.execute(Command.GO_LONG, max_spread_ticks=override)


# --- API --------------------------------------------------------------------


@pytest.fixture
def client(tmp_path):
    from trading.api import create_app

    settings = make_settings(tmp_path, api_token="test-token")
    service = TradingService(settings, brokers={
        "sim": make_broker(settings, "sim", cash=10_000,
                           qqq_bid=500.00, qqq_ask=500.15)})
    with TestClient(create_app(settings, service)) as c:
        yield c


AUTH = {"Authorization": "Bearer test-token"}


def test_api_rejects_market_orders(client):
    resp = client.post("/api/command", headers=AUTH, json={
        "command": "GO_LONG", "order_type": "MARKET"})
    assert resp.status_code == 422


def test_api_per_symbol_spread(client):
    resp = client.post("/api/command", headers=AUTH, json={
        "command": "GO_LONG", "max_spread_ticks": {"QQQ": 20, "PSQ": 2}})
    assert resp.status_code == 200, resp.json()


def test_api_rejects_unknown_order_type(client):
    resp = client.post("/api/command", headers=AUTH, json={
        "command": "GO_LONG", "order_type": "STOP"})
    assert resp.status_code == 422
