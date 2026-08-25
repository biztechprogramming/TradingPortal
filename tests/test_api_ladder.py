"""End-to-end HTTP behaviour of the ladder, candles and version routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trading.models import TradingMode
from trading.orchestrator import TradingService
from tests.conftest import make_broker, make_settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    from trading.api import create_app

    # PAPER with REQUIRE_CONFIRMATION off, so the tests below prove the
    # ladder confirms regardless of mode *and* regardless of that flag.
    settings = make_settings(
        tmp_path,
        api_token=TOKEN,
        mode=TradingMode.PAPER,
        live_trading_enabled=False,
        require_confirmation=False,
    )
    settings.market_hours_guard = False
    brokers = {
        "schwab": make_broker(settings, "schwab", cash=50_000.0),
        "ibkr": make_broker(settings, "ibkr", cash=25_000.0),
    }
    for broker in brokers.values():
        broker.connect()
        broker.set_quote("QQQ", 500.00, 500.04, day_high=502.00, day_low=498.00)
        broker.set_quote("PSQ", 32.10, 32.12, day_high=32.20, day_low=32.00)
    service = TradingService(settings, brokers=brokers)
    app = create_app(settings, service)
    with TestClient(app) as c:
        c.service = service          # type: ignore[attr-defined]
        yield c


def _place(client, symbol, price, **extra):
    """Full two-step click: get the confirmation, then spend it."""
    body = {"symbol": symbol, "limit_price": price, **extra}
    first = client.post("/api/ladder/order", json=body, headers=AUTH)
    assert first.status_code == 409
    assert first.json()["status"] == "CONFIRMATION_REQUIRED"
    body["confirm_token"] = first.json()["confirm_token"]
    return client.post("/api/ladder/order", json=body, headers=AUTH)


# --- auth -------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/ladder", "/api/candles?symbol=QQQ"])
def test_ladder_routes_require_a_token(client, path):
    assert client.get(path).status_code == 401


def test_ladder_order_requires_a_token(client):
    resp = client.post("/api/ladder/order",
                       json={"symbol": "QQQ", "limit_price": 498.0})
    assert resp.status_code == 401


# --- ladder view ------------------------------------------------------------


def test_ladder_reports_session_range_and_rungs(client):
    body = client.get("/api/ladder", headers=AUTH).json()

    qqq = body["symbols"]["QQQ"]
    assert qqq["quote"]["day_high"] == 502.00
    assert qqq["quote"]["day_low"] == 498.00
    assert qqq["step"] == 0.10
    assert len(qqq["rungs"]) > 0

    psq = body["symbols"]["PSQ"]
    assert psq["step"] == 0.005
    assert any(str(r["price"]).endswith("5") for r in psq["rungs"])


def test_ladder_exposes_per_account_orders_and_positions(client):
    body = client.get("/api/ladder", headers=AUTH).json()
    for name in ("schwab", "ibkr"):
        assert name in body["accounts"]
        assert body["accounts"][name]["open_orders"] == []
        assert "QQQ" in body["accounts"][name]["positions"]


# --- confirmation is unconditional -----------------------------------------


def test_every_order_requires_confirmation_even_in_paper(client):
    """Unlike GO_LONG, a rung click confirms in every mode."""
    assert client.service.settings.mode is TradingMode.PAPER
    assert client.service.settings.require_confirmation is False
    resp = client.post("/api/ladder/order",
                       json={"symbol": "QQQ", "limit_price": 498.0}, headers=AUTH)
    assert resp.status_code == 409
    assert resp.json()["status"] == "CONFIRMATION_REQUIRED"
    # Nothing reached a broker.
    for broker in client.service.brokers.values():
        assert broker.get_open_orders() == []


def test_confirmation_token_is_bound_to_symbol_and_price(client):
    first = client.post("/api/ladder/order",
                        json={"symbol": "QQQ", "limit_price": 498.0}, headers=AUTH)
    token = first.json()["confirm_token"]

    # Same token, different price -> must not execute.
    resp = client.post(
        "/api/ladder/order",
        json={"symbol": "QQQ", "limit_price": 497.0, "confirm_token": token},
        headers=AUTH,
    )
    assert resp.status_code == 409
    assert resp.json()["status"] == "CONFIRMATION_REQUIRED"


def test_confirmation_token_is_single_use(client):
    body = {"symbol": "QQQ", "limit_price": 498.0}
    first = client.post("/api/ladder/order", json=body, headers=AUTH)
    body["confirm_token"] = first.json()["confirm_token"]
    assert client.post("/api/ladder/order", json=body, headers=AUTH).status_code == 200

    replay = client.post("/api/ladder/order", json=dict(body, request_id="x"),
                         headers=AUTH)
    assert replay.status_code == 409


# --- placing and modifying --------------------------------------------------


def test_confirmed_order_reaches_every_tradable_account(client):
    resp = _place(client, "QQQ", 498.00)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert {r["broker"] for r in body["results"]} == {"schwab", "ibkr"}
    for r in body["results"]:
        assert r["action"] == "PLACED"
        assert r["quantity"] > 0
        assert len(r["open_orders"]) == 1
        # Placing an order must not move the position.
        assert r["position_quantity"] == 0.0


def test_second_price_modifies_and_returns_refreshed_orders(client):
    _place(client, "QQQ", 498.00)
    resp = _place(client, "QQQ", 497.00, request_id="second")

    assert resp.status_code == 200
    for r in resp.json()["results"]:
        assert r["action"] == "MODIFIED"
        assert r["previous_limit_price"] == 498.00
        # Still one order per account, re-priced -- not two.
        assert len(r["open_orders"]) == 1
        assert r["open_orders"][0]["limit_price"] == 497.00


def test_open_orders_appear_on_status_for_each_account(client):
    _place(client, "QQQ", 498.00)
    status = client.get("/api/status", headers=AUTH).json()
    for name in ("schwab", "ibkr"):
        orders = status["brokers"][name]["open_orders"]
        assert len(orders) == 1
        assert orders[0]["symbol"] == "QQQ"
        # And no position was invented from the order.
        assert status["brokers"][name]["positions"] == []


def test_unknown_symbol_is_refused(client):
    resp = client.post("/api/ladder/order",
                       json={"symbol": "SPY", "limit_price": 500.0}, headers=AUTH)
    assert resp.status_code == 400
    assert "not tradable" in resp.json()["detail"]


def test_non_positive_price_is_refused(client):
    resp = client.post("/api/ladder/order",
                       json={"symbol": "QQQ", "limit_price": 0}, headers=AUTH)
    assert resp.status_code == 422


# --- candles ----------------------------------------------------------------


def test_candles_route_rejects_an_untracked_symbol(client):
    resp = client.get("/api/candles?symbol=SPY", headers=AUTH)
    assert resp.status_code == 400


def test_candles_route_returns_a_series(client):
    body = client.get("/api/candles?symbol=QQQ&refresh=false", headers=AUTH).json()
    assert body["symbol"] == "QQQ"
    assert body["bar_size"] == "1h"
    assert body["outside_rth"] is True
    assert isinstance(body["bars"], list)


# --- live reload ------------------------------------------------------------


def test_version_is_open_and_changes_when_an_asset_changes(client, tmp_path):
    from trading import api as api_module

    first = client.get("/api/version")
    assert first.status_code == 200          # unauthenticated on purpose
    before = first.json()["assets"]

    target = api_module.WEB_DIR / "app.js"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"\n// touched\n")
        after = client.get("/api/version").json()["assets"]
        assert after != before
    finally:
        target.write_bytes(original)

    assert client.get("/api/version").json()["assets"] == before


def test_dashboard_assets_are_not_cached(client):
    for path in ("/", "/app.js", "/styles.css"):
        resp = client.get(path)
        assert "no-store" in resp.headers.get("cache-control", "")
