"""Reference symbols are charted, and must never be tradable."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trading.config import ConfigError, Settings, validate_settings
from trading.errors import OrderValidationError
from trading.models import Side, TradingMode
from trading.orchestrator import TradingService
from trading.validation import validate_order
from tests.conftest import make_broker, make_settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    from trading.api import create_app

    settings = make_settings(
        tmp_path, api_token=TOKEN, mode=TradingMode.PAPER,
        live_trading_enabled=False, reference_symbols=("NDX",),
    )
    settings.market_hours_guard = False
    brokers = {"ibkr": make_broker(settings, "ibkr", cash=25_000.0)}
    brokers["ibkr"].connect()
    service = TradingService(settings, brokers=brokers)
    app = create_app(settings, service)
    with TestClient(app) as c:
        c.service = service          # type: ignore[attr-defined]
        yield c


def test_chart_symbols_include_references_but_symbols_do_not():
    s = Settings(reference_symbols=("NDX",))
    assert s.symbols == ("QQQ", "PSQ")
    assert s.chart_symbols == ("QQQ", "PSQ", "NDX")


def test_a_reference_symbol_may_not_shadow_a_traded_one():
    s = Settings(reference_symbols=("QQQ",))
    with pytest.raises(ConfigError, match="chart-only"):
        validate_settings(s)


def test_order_validation_rejects_a_reference_symbol():
    """The tradable allowlist is settings.symbols, not chart_symbols."""
    s = Settings(reference_symbols=("NDX",))
    with pytest.raises(OrderValidationError, match="not tradable"):
        validate_order(
            symbol="NDX", side=Side.BUY, quantity=1, limit_price=29_000.0,
            account_id="acct", settings=s,
        )


def test_ladder_order_rejects_a_reference_symbol(client):
    resp = client.post("/api/ladder/order",
                       json={"symbol": "NDX", "limit_price": 29_000.0},
                       headers=AUTH)
    assert resp.status_code == 400
    assert "not tradable" in resp.json()["detail"]


def test_candles_are_served_for_a_reference_symbol(client):
    resp = client.get("/api/candles?symbol=NDX&refresh=false", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["symbol"] == "NDX"


def test_the_ladder_view_has_no_rungs_for_a_reference_symbol(client):
    body = client.get("/api/ladder", headers=AUTH).json()
    assert "NDX" not in body["symbols"]
    assert set(body["symbols"]) == {"QQQ", "PSQ"}


def test_status_advertises_the_chart_set(client):
    combined = client.get("/api/status", headers=AUTH).json()["combined"]
    assert combined["chart_symbols"] == ["QQQ", "PSQ", "NDX"]
