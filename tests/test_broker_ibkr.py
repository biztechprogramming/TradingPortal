"""IBKR Client Portal adapter: snapshots, ledger, order replies, statuses."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from trading.brokers.ibkr import (
    IBKRBroker,
    _clean_price,
    _extract_order_id,
    _parse_order_status,
    _parse_snapshot,
    is_paper_account,
)
from trading.errors import InvalidQuoteError, OrderRejected
from trading.models import Account, OrderState, Side
from tests.conftest import make_settings

BASE = "https://localhost:5000/v1/api"


def broker_with(handler, tmp_path, account="DU1234567", **overrides) -> IBKRBroker:
    settings = make_settings(tmp_path, **overrides)
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    broker = IBKRBroker(settings, client=client)
    broker._account = Account(account, account, is_paper=is_paper_account(account))
    broker._connected = True
    broker._portfolio_primed = True
    broker._iserver_primed = True
    broker._last_tickle = time.time()      # suppress keepalive during tests
    broker._conids = {"QQQ": 320227571, "PSQ": 43645828}
    return broker


# --- account identification -------------------------------------------------


@pytest.mark.parametrize("account,paper", [
    ("DU1234567", True),
    ("DF9876543", True),
    ("U1234567", False),
])
def test_paper_accounts_are_identified_by_prefix(account, paper):
    assert is_paper_account(account) is paper


# --- market data ------------------------------------------------------------


def test_snapshot_parsing():
    quote = _parse_snapshot("QQQ", {
        "31": "500.02", "84": "500.00", "86": "500.04",
        "88": "8", "85": "9", "6509": "RB",
        "_updated": time.time() * 1000,
    })
    assert quote.bid == 500.00 and quote.ask == 500.04
    assert quote.bid_size == 8 and quote.ask_size == 9
    assert not quote.delayed and not quote.halted
    assert quote.age_seconds() < 2


def test_price_prefixes_are_stripped():
    assert _clean_price("C500.02") == (500.02, False)   # previous close
    assert _clean_price("H499.50") == (499.50, True)    # halted
    assert _clean_price("1,234.50") == (1234.50, False)
    assert _clean_price(None) == (0.0, False)
    assert _clean_price("") == (0.0, False)


def test_halted_prices_set_the_flag():
    quote = _parse_snapshot("QQQ", {
        "84": "H500.00", "86": "H500.04", "31": "H500.02",
        "_updated": time.time() * 1000,
    })
    assert quote.halted


def test_delayed_data_is_flagged():
    quote = _parse_snapshot("QQQ", {
        "84": "500.00", "86": "500.04", "6509": "DP",
        "_updated": time.time() * 1000,
    })
    assert quote.delayed


def test_snapshot_without_updated_timestamp_is_refused():
    with pytest.raises(InvalidQuoteError, match="freshness"):
        _parse_snapshot("QQQ", {"84": "500.00", "86": "500.04"})


def test_snapshot_is_polled_until_fields_appear(tmp_path):
    """IBKR's first snapshot call returns an empty shell by design."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, json=[{"conid": 320227571}])
        return httpx.Response(200, json=[{
            "conid": 320227571, "84": "500.00", "86": "500.04",
            "31": "500.02", "6509": "RB", "_updated": time.time() * 1000,
        }])

    quote = broker_with(handler, tmp_path).get_quote("QQQ")
    assert calls["n"] >= 2
    assert quote.ask == 500.04


def test_delayed_quotes_are_refused_unless_opted_in(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "conid": 320227571, "84": "500.00", "86": "500.04",
            "6509": "DP", "_updated": time.time() * 1000,
        }])

    with pytest.raises(InvalidQuoteError, match="delayed"):
        broker_with(handler, tmp_path).get_quote("QQQ")

    quote = broker_with(
        handler, tmp_path, ibkr_allow_delayed_quotes=True
    ).get_quote("QQQ")
    assert quote.delayed


# --- ledger and positions ---------------------------------------------------


def test_ledger_prefers_settled_cash(tmp_path):
    """Shapes as IBKR really sends them: the ledger has no buying power.

    Buying power and available funds come from /summary, as {"amount": n}.
    Reading them from the ledger reported a buying power of 0.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/summary"):
            return httpx.Response(200, json={
                "availablefunds": {"amount": 24_000.0},
                "buyingpower": {"amount": 96_000.0},
            })
        assert request.url.path.endswith("/ledger")
        return httpx.Response(200, json={"USD": {
            "cashbalance": 25_000.0,
            "settledcash": 20_000.0,
        }})

    balances = broker_with(handler, tmp_path).get_available_cash()
    # Cash-only means the settled figure wins over the marginable one.
    assert balances.cash_available == 20_000.0
    assert balances.buying_power == 96_000.0


def test_buying_power_falls_back_to_cash_without_a_summary(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/summary"):
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"USD": {
            "cashbalance": 620.44, "settledcash": 620.44,
        }})

    balances = broker_with(handler, tmp_path).get_available_cash()
    assert balances.cash_available == 620.44
    assert balances.buying_power == 620.44       # never a misleading 0


def test_positions_paginate(tmp_path):
    page_one = [
        {"conid": i, "ticker": f"SYM{i}", "position": 1, "avgPrice": 10.0,
         "mktValue": 10.0}
        for i in range(30)
    ]
    page_two = [{"conid": 99, "ticker": "QQQ", "position": 100,
                 "avgPrice": 480.0, "mktValue": 50_000.0}]

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.path.rstrip("/").split("/")[-1]
        return httpx.Response(200, json=page_one if page == "0" else page_two)

    positions = broker_with(handler, tmp_path).get_positions()
    assert len(positions) == 31
    assert positions[-1].symbol == "QQQ" and positions[-1].quantity == 100


def test_zero_quantity_positions_are_dropped(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"conid": 1, "ticker": "QQQ", "position": 0},
            {"conid": 2, "ticker": "PSQ", "position": 300, "avgPrice": 32.0},
        ])

    positions = broker_with(handler, tmp_path).get_positions()
    assert [p.symbol for p in positions] == ["PSQ"]


# --- orders -----------------------------------------------------------------


def test_submit_sends_the_expected_payload(tmp_path, monkeypatch):
    # Pin the session: the flag depends on the time of day.
    monkeypatch.setattr("trading.brokers.ibkr.wants_outside_rth", lambda s: False)
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["path"] = request.url.path
        return httpx.Response(200, json=[{"order_id": "123456",
                                          "order_status": "PreSubmitted"}])

    order = broker_with(handler, tmp_path).submit_limit_order(
        "QQQ", Side.BUY, 19, 500.55
    )
    body = seen["body"]["orders"][0]
    assert body["conid"] == 320227571
    assert body["orderType"] == "LMT"
    assert body["side"] == "BUY"
    assert body["quantity"] == 19
    assert body["price"] == 500.55
    assert body["tif"] == "DAY"
    assert body["outsideRTH"] is False
    assert body["cOID"]                       # broker-side duplicate guard
    assert seen["path"].endswith("/iserver/account/DU1234567/orders")
    assert order.order_id == "123456"


def test_extended_hours_order_is_flagged_outside_rth(tmp_path, monkeypatch):
    monkeypatch.setattr("trading.brokers.ibkr.wants_outside_rth", lambda s: True)
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=[{"order_id": "1",
                                          "order_status": "PreSubmitted"}])

    broker_with(handler, tmp_path).submit_limit_order("PSQ", Side.BUY, 5, 27.91)
    assert seen["body"]["orders"][0]["outsideRTH"] is True


def test_safe_confirmation_prompt_is_answered(tmp_path):
    """IBKR often asks a benign question before accepting a marketable limit."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/orders"):
            return httpx.Response(200, json=[{
                "id": "reply-1",
                "message": ["The price you have entered exceeds the "
                            "percentage constraint of 3%."],
            }])
        assert json.loads(request.content) == {"confirmed": True}
        return httpx.Response(200, json=[{"order_id": "999", "order_status": "Submitted"}])

    order = broker_with(handler, tmp_path).submit_limit_order(
        "QQQ", Side.BUY, 5, 500.55
    )
    assert order.order_id == "999"
    assert any("/iserver/reply/reply-1" in c for c in calls)


def test_unrecognised_prompt_blocks_the_order(tmp_path):
    """An unknown warning must stop the order, not be blanket-confirmed."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "id": "reply-9",
            "message": ["This account is flagged for pattern day trading "
                        "and the order may liquidate other holdings."],
        }])

    with pytest.raises(OrderRejected, match="unrecognised confirmation"):
        broker_with(handler, tmp_path).submit_limit_order(
            "QQQ", Side.BUY, 5, 500.55
        )


def test_auto_confirm_all_is_opt_in(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/orders"):
            return httpx.Response(200, json=[{
                "id": "reply-9", "message": ["Some novel warning."],
            }])
        return httpx.Response(200, json=[{"order_id": "777"}])

    order = broker_with(
        handler, tmp_path, ibkr_auto_confirm_all_questions=True
    ).submit_limit_order("QQQ", Side.BUY, 5, 500.55)
    assert order.order_id == "777"


def test_error_response_raises(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"error": "No trading permissions"}])

    with pytest.raises(OrderRejected, match="No trading permissions"):
        broker_with(handler, tmp_path).submit_limit_order(
            "QQQ", Side.BUY, 5, 500.55
        )


def test_endless_confirmation_loop_is_broken(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{
            "id": "loop", "message": ["Are you sure you want to submit this order?"],
        }])

    with pytest.raises(OrderRejected, match="unbounded confirmation loop"):
        broker_with(handler, tmp_path).submit_limit_order(
            "QQQ", Side.BUY, 5, 500.55
        )


@pytest.mark.parametrize("raw,expected", [
    ("Filled", OrderState.FILLED),
    ("Submitted", OrderState.WORKING),
    ("PreSubmitted", OrderState.WORKING),
    ("PendingCancel", OrderState.WORKING),
    ("Cancelled", OrderState.CANCELLED),
    ("Rejected", OrderState.REJECTED),
    ("Inactive", OrderState.REJECTED),
    ("Nonsense", OrderState.UNKNOWN),
])
def test_order_status_mapping(raw, expected):
    order = _parse_order_status("1", {
        "order_status": raw, "size": "10", "cum_fill": "4",
        "average_price": "500.10", "side": "B",
    }, "ibkr", {})
    assert order.state is expected
    assert order.filled_quantity == 4
    assert order.remaining_quantity == 6
    assert order.side is Side.BUY


def test_order_status_falls_back_to_remembered_metadata():
    """IBKR omits fields on some status payloads; we must not lose the symbol."""
    order = _parse_order_status("1", {"order_status": "Filled", "cum_fill": "5"},
                                "ibkr",
                                {"symbol": "PSQ", "side": Side.SELL,
                                 "quantity": 5, "limit_price": 32.05})
    assert order.symbol == "PSQ"
    assert order.side is Side.SELL
    assert order.limit_price == 32.05


def test_extract_order_id_variants():
    assert _extract_order_id([{"order_id": "1"}]) == "1"
    assert _extract_order_id([{"orderId": 2}]) == "2"
    assert _extract_order_id({"id": 3}) == "3"
    assert _extract_order_id([]) is None
    assert _extract_order_id([{"nothing": "useful"}]) is None


def test_cancel_uses_the_delete_route(tmp_path):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        return httpx.Response(200, json={"order_id": "5", "msg": "Request was submitted"})

    broker_with(handler, tmp_path).cancel_order("5")
    assert seen["method"] == "DELETE"
    assert seen["path"] == "/v1/api/iserver/account/DU1234567/order/5"


def test_gateway_down_gives_an_actionable_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    from trading.errors import BrokerConnectionError

    with pytest.raises(BrokerConnectionError, match="Client Portal"):
        broker_with(handler, tmp_path).get_positions()


def test_unauthenticated_gateway_is_reported_clearly(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    from trading.errors import BrokerAuthError

    with pytest.raises(BrokerAuthError, match="log in"):
        broker_with(handler, tmp_path).get_positions()


def test_connect_refuses_a_competing_session(tmp_path):
    """A competing TWS session can silently take over order routing."""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/iserver/auth/status"):
            return httpx.Response(200, json={
                "authenticated": True, "connected": True, "competing": True,
            })
        return httpx.Response(200, json={})

    from trading.errors import BrokerAuthError

    broker = broker_with(handler, tmp_path)
    with pytest.raises(BrokerAuthError, match="competing"):
        broker.connect()
    assert broker.connected is False


def test_live_order_sizes_in_display_format_are_parsed():
    """IBKR can omit totalSize and send sizeAndFills as "filled/total" with commas."""
    from trading.brokers.ibkr import _parse_live_order

    order = _parse_live_order({
        "orderId": 7, "ticker": "PSQ", "side": "BUY", "status": "Submitted",
        "sizeAndFills": "0/2,470", "price": "27.85",
    }, "ibkr:U1")
    assert order.quantity == 2470 and order.filled_quantity == 0
    assert order.limit_price == 27.85
    assert order.state.is_open

    partial = _parse_live_order({
        "orderId": 8, "ticker": "PSQ", "side": "BUY", "status": "Submitted",
        "totalSize": "1,000", "filledQuantity": "250", "avgPrice": "27.80",
    }, "ibkr:U1")
    assert partial.quantity == 1000 and partial.filled_quantity == 250
    assert partial.remaining_quantity == 750


def test_executions_group_into_one_fill_per_order():
    """IBKR reports each partial fill separately; the dashboard wants the order."""
    from trading.brokers.ibkr import _group_executions

    rows = [
        {"symbol": "PSQ", "side": "S", "size": 70.0, "price": "27.85",
         "trade_time_r": 1793973014000, "order_id": 1078818583, "accountCode": "U1"},
        {"symbol": "PSQ", "side": "S", "size": 930.0, "price": "27.86",
         "trade_time_r": 1793973015000, "order_id": 1078818583, "accountCode": "U1"},
        {"symbol": "PSQ", "side": "B", "size": "2,400", "price": "26.8373",
         "trade_time_r": 1793739260000, "order_id": 663127805, "accountCode": "U1"},
        {"symbol": "PSQ", "side": "B", "size": 5.0, "price": "1",
         "trade_time_r": 1793739260000, "order_id": 9, "accountCode": "U2"},  # other account
    ]
    fills = {f.order_id: f for f in _group_executions(rows, "U1", "ibkr:U1")}
    assert set(fills) == {"1078818583", "663127805"}
    sale = fills["1078818583"]
    assert sale.side.value == "SELL" and sale.filled_quantity == 1000
    assert sale.average_fill_price == pytest.approx((70 * 27.85 + 930 * 27.86) / 1000)
    assert sale.updated_at == 1793973015.0
    assert fills["663127805"].filled_quantity == 2400
