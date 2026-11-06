"""The local HTTP interface: auth, status, commands, confirmation flow."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from trading.brokers.simulated import SimulationConfig
from trading.models import TradingMode
from trading.orchestrator import TradingService
from tests.conftest import make_broker, make_settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def client(tmp_path):
    from trading.api import create_app

    settings = make_settings(tmp_path, api_token=TOKEN)
    brokers = {
        "schwab": make_broker(settings, "schwab", cash=50_000.0),
        "ibkr": make_broker(settings, "ibkr", cash=25_000.0),
    }
    service = TradingService(settings, brokers=brokers)
    app = create_app(settings, service)
    with TestClient(app) as c:
        c.service = service          # type: ignore[attr-defined]
        yield c


# --- authentication ---------------------------------------------------------

@pytest.mark.parametrize("path", [
    "/api/status", "/api/holdings", "/api/audit", "/api/history", "/api/config",
])
def test_read_routes_require_a_token(client, path):
    assert client.get(path).status_code == 401


def test_command_route_requires_a_token(client):
    resp = client.post("/api/command", json={"command": "GO_LONG"})
    assert resp.status_code == 401
    # And nothing was executed.
    assert client.service.state.data.last_command is None


def test_wrong_token_is_rejected(client):
    resp = client.get("/api/status", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


def test_token_accepted_via_header_and_query(client):
    assert client.get("/api/status", headers=AUTH).status_code == 200
    assert client.get(f"/api/status?token={TOKEN}").status_code == 200
    assert client.get("/api/status", headers={"X-API-Token": TOKEN}).status_code == 200


def test_health_is_open_but_reveals_nothing(client):
    body = client.get("/api/health").json()
    assert body == {"ok": True, "mode": "live", "live": True}


# --- status -----------------------------------------------------------------


def test_status_shape(client):
    body = client.get("/api/status", headers=AUTH).json()

    assert set(body["brokers"]) == {"schwab", "ibkr"}
    for broker in body["brokers"].values():
        assert broker["connected"] is True
        assert broker["exposure"] == "FLAT"
        assert broker["balances"]["cash_available"] > 0
        assert broker["account"]["display_id"]

    combined = body["combined"]
    assert combined["cash_available"] == 75_000.0
    assert combined["exposure"] == "FLAT"
    assert combined["brokers_connected"] == 2
    assert body["config"]["long_symbol"] == "QQQ"


def test_status_exposes_no_secrets(client):
    text = client.get("/api/status", headers=AUTH).text
    assert TOKEN not in text
    assert "app_secret" not in text.lower()


def test_holdings_lists_every_position(client):
    client.service.brokers["schwab"].set_position("SPY", 12, 400.0)
    body = client.get("/api/holdings", headers=AUTH).json()

    symbols = {p["symbol"] for p in body["brokers"]["schwab"]["positions"]}
    assert "SPY" in symbols          # not just the two traded tickers
    assert body["brokers"]["ibkr"]["balances"]["cash_available"] == 25_000.0


# --- commands ---------------------------------------------------------------


def test_go_long_then_flat(client):
    resp = client.post("/api/command", headers=AUTH,
                       json={"command": "GO_LONG"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "OK"
    assert all(r["exposure_after"] == "LONG" for r in body["results"])
    assert all(r["last_execution_price"] > 0 for r in body["results"])

    flat = client.post("/api/command", headers=AUTH, json={"command": "GO_FLAT"})
    assert flat.status_code == 200
    assert all(r["exposure_after"] == "FLAT" for r in flat.json()["results"])


def test_shortcut_aliases(client):
    assert client.post("/api/short", headers=AUTH).status_code == 200
    assert client.get("/api/status", headers=AUTH).json()["combined"]["exposure"] == "SHORT"
    assert client.post("/api/flat", headers=AUTH).status_code == 200


def test_unknown_command_is_rejected(client):
    resp = client.post("/api/command", headers=AUTH, json={"command": "GO_SIDEWAYS"})
    assert resp.status_code == 400
    assert "unknown command" in resp.json()["detail"]


def test_partial_outcome_is_not_a_200(client):
    """A caller checking only the status code must not read PARTIAL as success."""
    client.service.brokers["ibkr"].sim = SimulationConfig(reject_next="down")
    resp = client.post("/api/command", headers=AUTH, json={"command": "GO_LONG"})

    assert resp.status_code == 207
    body = resp.json()
    assert body["status"] == "PARTIAL"
    assert body["mismatch"] is True


def test_mismatch_blocks_the_next_command_over_http(client):
    client.service.brokers["ibkr"].sim = SimulationConfig(reject_next="down")
    client.post("/api/command", headers=AUTH, json={"command": "GO_LONG"})

    blocked = client.post("/api/command", headers=AUTH,
                          json={"command": "GO_SHORT", "force": True})
    assert blocked.status_code == 409
    assert blocked.json()["status"] == "NEEDS_ACKNOWLEDGEMENT"

    assert client.post("/api/acknowledge", headers=AUTH,
                       json={"note": "checked"}).status_code == 200
    ok = client.post("/api/command", headers=AUTH,
                     json={"command": "GO_SHORT", "force": True})
    assert ok.status_code in (200, 207)


def test_duplicate_request_id_returns_409(client):
    first = client.post("/api/command", headers=AUTH,
                        json={"command": "GO_LONG", "request_id": "tap-1"})
    assert first.status_code == 200
    second = client.post("/api/command", headers=AUTH,
                         json={"command": "GO_LONG", "request_id": "tap-1"})
    assert second.status_code == 409
    assert second.json()["status"] == "DUPLICATE"


def test_execution_lock_returns_423(client):
    with client.service.lock.acquire("test"):
        resp = client.post("/api/command", headers=AUTH,
                           json={"command": "GO_LONG"})
    assert resp.status_code == 423
    assert resp.json()["status"] == "LOCKED"


# --- confirmation flow ------------------------------------------------------


@pytest.fixture
def confirming_client(tmp_path):
    from trading.api import create_app

    settings = make_settings(tmp_path, api_token=TOKEN, mode=TradingMode.LIVE,
                             live_trading_enabled=True, require_confirmation=True)
    brokers = {"schwab": make_broker(settings, "schwab", cash=50_000.0)}
    service = TradingService(settings, brokers=brokers)
    with TestClient(create_app(settings, service)) as c:
        yield c


def test_live_command_needs_two_calls(confirming_client):
    first = confirming_client.post("/api/command", headers=AUTH,
                                   json={"command": "GO_LONG"})
    assert first.status_code == 409
    body = first.json()
    assert body["status"] == "CONFIRMATION_REQUIRED"
    assert body["confirm_token"]

    second = confirming_client.post("/api/command", headers=AUTH, json={
        "command": "GO_LONG",
        "confirm_token": body["confirm_token"],
        "force": True,
    })
    assert second.status_code == 200
    assert second.json()["status"] == "OK"


def test_live_flat_needs_no_confirmation(confirming_client):
    resp = confirming_client.post("/api/flat", headers=AUTH)
    assert resp.status_code == 200


# --- dashboard --------------------------------------------------------------


def test_dashboard_assets_are_served(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "GO LONG" in page.text and "GO SHORT" in page.text and "GO FLAT" in page.text
    assert client.get("/app.js").status_code == 200
    assert client.get("/styles.css").status_code == 200


def test_audit_endpoint_returns_events(client):
    client.post("/api/command", headers=AUTH, json={"command": "GO_LONG"})
    events = client.get("/api/audit?limit=200", headers=AUTH).json()["events"]
    assert any(e["event"] == "command.finished" for e in events)


def test_history_endpoint(client):
    client.post("/api/command", headers=AUTH, json={"command": "GO_LONG"})
    history = client.get("/api/history", headers=AUTH).json()["history"]
    assert history[-1]["command"] == "GO_LONG"


# --- last fills ---------------------------------------------------------------


def test_last_fills_prefer_the_broker_and_drop_gone_accounts(settings):
    """Broker order history wins over this app's own records.

    The app's history misses ladder fills and trades made on the broker's
    site, and it keeps rows for accounts that no longer exist.
    """
    from trading.models import Order, OrderState, Side
    from trading.orchestrator import TradingService
    from tests.conftest import make_broker

    schwab = make_broker(settings, "schwab", cash=1.0)
    ibkr = make_broker(settings, "ibkr", cash=1.0)
    schwab.get_recent_fills = lambda days=30: [
        Order("1", "PSQ", Side.BUY, 5040, 27.85, OrderState.FILLED,
              filled_quantity=5040, average_fill_price=27.8485,
              updated_at=1_800_000_000.0),
        Order("0", "PSQ", Side.BUY, 966, 26.21, OrderState.FILLED,
              filled_quantity=966, average_fill_price=26.21,
              updated_at=1_700_000_000.0),
    ]
    ibkr.get_recent_fills = lambda days=30: None    # cannot list fills
    service = TradingService(settings, brokers={"schwab": schwab, "ibkr": ibkr})
    service.state.data.history = [{
        "command": "GO_SHORT", "finished_at": 1_700_000_000.0,
        "results": [
            {"broker": "schwab", "fills": [{"symbol": "PSQ", "filled_quantity": 966,
                                            "average_price": 26.21, "side": "BUY"}]},
            {"broker": "ibkr", "fills": [{"symbol": "PSQ", "filled_quantity": 10,
                                          "average_price": 26.0, "side": "BUY"}]},
            {"broker": "schwab:gone:1354", "fills": [{"symbol": "PSQ",
                                                      "filled_quantity": 5205,
                                                      "average_price": 26.21,
                                                      "side": "BUY"}]},
        ],
    }]

    fills = service.last_fills()
    assert set(fills) == {"schwab", "ibkr"}
    assert fills["schwab"]["PSQ"]["shares"] == 5040            # newest broker fill
    assert fills["schwab"]["PSQ"]["price"] == 27.8485
    assert fills["ibkr"]["PSQ"]["shares"] == 10                # history fallback
