"""Schwab adapter: request shapes and response parsing, over a mock transport."""

from __future__ import annotations

import json
import time

import httpx
import pytest

from trading.brokers.schwab import (
    SchwabBroker,
    _order_id_from_location,
    _parse_order,
    _parse_quote,
    build_order_payload,
)
from trading.errors import (
    BrokerAuthError,
    InvalidQuoteError,
    OrderRejected,
)
from trading.models import OrderState, Side
from tests.conftest import make_settings

BASE = "https://api.schwabapi.com"


def broker_with(handler, tmp_path, **overrides) -> SchwabBroker:
    settings = make_settings(tmp_path, **overrides)
    client = httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)
    broker = SchwabBroker(settings, client=client)
    broker._access_token = lambda: "test-access-token"  # type: ignore[assignment]
    broker._account_hash = "HASH123"
    from trading.models import Account

    broker._account = Account("HASH123", "...6789", is_paper=False)
    broker._connected = True
    return broker


# --- accounts and balances --------------------------------------------------


def test_account_numbers_are_masked_and_hashed(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/trader/v1/accounts/accountNumbers"
        assert request.headers["Authorization"] == "Bearer test-access-token"
        return httpx.Response(200, json=[
            {"accountNumber": "123456789", "hashValue": "ABCDEF"},
        ])

    accounts = broker_with(handler, tmp_path).get_accounts()
    assert accounts[0].account_id == "ABCDEF"      # hash is used in URLs
    assert accounts[0].display_id == "...6789"     # number is never shown whole
    assert accounts[0].is_paper is False           # Schwab has no paper API


def test_margin_account_cash_ignores_borrowable_funds(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"securitiesAccount": {
            "type": "MARGIN",
            "currentBalances": {
                "cashBalance": 10_000.0,
                "cashAvailableForTrading": 10_000.0,
                "buyingPowerNonMarginableTrade": 10_000.0,
                "availableFundsNonMarginableTrade": 9_500.0,
                "buyingPower": 40_000.0,
            },
        }})

    balances = broker_with(handler, tmp_path).get_available_cash()
    # Spendable without borrowing — not the $40k that includes margin.
    assert balances.cash_available == 10_000.0
    assert balances.buying_power == 40_000.0


def test_margin_account_holding_stock_still_reports_its_cash(tmp_path):
    """The live payload that exposed the bug.

    Schwab zeroes availableFundsNonMarginableTrade once the account holds
    marginable stock. Reading it would have sized every GO LONG to zero
    shares on an account that was already invested.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"securitiesAccount": {
            "type": "MARGIN",
            "currentBalances": {
                "cashBalance": 487.8799999999999,
                "availableFunds": 487.88,
                "availableFundsNonMarginableTrade": 0.0,
                "buyingPowerNonMarginableTrade": 487.88,
                "buyingPower": 487.88,
                "longMarketValue": 136_240.0,
                "maintenanceRequirement": 136_240.0,
                "marginBalance": 0.0,
            },
        }})

    balances = broker_with(handler, tmp_path).get_available_cash()
    assert balances.cash_available == pytest.approx(487.88)


def test_margin_cash_never_exceeds_the_cash_balance(tmp_path):
    """Buying power above the cash balance means borrowing. Refuse it."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"securitiesAccount": {
            "type": "MARGIN",
            "currentBalances": {
                "cashBalance": 1_000.0,
                "buyingPowerNonMarginableTrade": 5_000.0,
                "buyingPower": 20_000.0,
            },
        }})

    balances = broker_with(handler, tmp_path).get_available_cash()
    assert balances.cash_available == 1_000.0


def test_margin_loan_reports_no_spendable_cash(tmp_path):
    """A negative cash balance is an outstanding loan, not spending money."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"securitiesAccount": {
            "type": "MARGIN",
            "currentBalances": {
                "cashBalance": -2_500.0,
                "buyingPowerNonMarginableTrade": 0.0,
                "marginBalance": -2_500.0,
            },
        }})

    balances = broker_with(handler, tmp_path).get_available_cash()
    assert balances.cash_available == 0.0


def test_cash_account_uses_cash_available_for_trading(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"securitiesAccount": {
            "type": "CASH",
            "currentBalances": {
                "cashBalance": 8_000.0,
                "cashAvailableForTrading": 7_500.0,
            },
        }})

    balances = broker_with(handler, tmp_path).get_available_cash()
    assert balances.cash_available == 7_500.0
    assert balances.settled_cash == 7_500.0


def test_positions_parse_long_short_and_settled(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fields"] == "positions"
        return httpx.Response(200, json={"securitiesAccount": {
            "positions": [
                {"instrument": {"symbol": "QQQ", "assetType": "EQUITY"},
                 "longQuantity": 100, "shortQuantity": 0,
                 "settledLongQuantity": 60, "averagePrice": 480.0,
                 "marketValue": 50_000.0},
                {"instrument": {"symbol": "ZERO"}, "longQuantity": 0,
                 "shortQuantity": 0},
            ],
        }})

    positions = broker_with(handler, tmp_path).get_positions()
    assert len(positions) == 1                 # the zero row is dropped
    assert positions[0].symbol == "QQQ"
    assert positions[0].quantity == 100
    assert positions[0].settled_quantity == 60


# --- quotes -----------------------------------------------------------------


def test_quote_parsing():
    now_ms = time.time() * 1000
    quote = _parse_quote("QQQ", {
        "realtime": True,
        "quote": {
            "bidPrice": 500.00, "askPrice": 500.04, "lastPrice": 500.02,
            "bidSize": 8, "askSize": 9, "quoteTime": now_ms,
            "securityStatus": "Normal",
        },
    })
    assert quote.bid == 500.00 and quote.ask == 500.04
    assert quote.age_seconds() < 2
    assert not quote.delayed and not quote.halted


def test_quote_without_a_timestamp_is_refused():
    """Without a quote time we cannot prove freshness, so we refuse."""
    with pytest.raises(InvalidQuoteError, match="freshness"):
        _parse_quote("QQQ", {"quote": {"bidPrice": 1.0, "askPrice": 1.1}})


def test_delayed_and_halted_flags():
    now_ms = time.time() * 1000
    delayed = _parse_quote("QQQ", {"realtime": False, "quote": {
        "bidPrice": 1.0, "askPrice": 1.1, "quoteTime": now_ms}})
    assert delayed.delayed

    halted = _parse_quote("QQQ", {"quote": {
        "bidPrice": 1.0, "askPrice": 1.1, "quoteTime": now_ms,
        "securityStatus": "Halted"}})
    assert halted.halted


# --- orders -----------------------------------------------------------------


def test_order_payload_shape():
    payload = build_order_payload("QQQ", Side.BUY, 19, 500.55)
    assert payload == {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "price": "500.55",
        "orderLegCollection": [{
            "instruction": "BUY",
            "quantity": 19,
            "instrument": {"symbol": "QQQ", "assetType": "EQUITY"},
        }],
    }
    # Selling a held position is a plain SELL, never SELL_SHORT.
    sell = build_order_payload("PSQ", Side.SELL, 5, 32.05)
    assert sell["orderLegCollection"][0]["instruction"] == "SELL"


def test_order_id_comes_from_the_location_header(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["orderType"] == "LIMIT"
            return httpx.Response(201, headers={
                "Location":
                    f"{BASE}/trader/v1/accounts/HASH123/orders/1000000123456"
            })
        raise AssertionError("unexpected request")

    order = broker_with(handler, tmp_path).submit_limit_order(
        "QQQ", Side.BUY, 19, 500.55
    )
    assert order.order_id == "1000000123456"
    assert order.state is OrderState.PENDING


def test_missing_location_header_is_an_error(tmp_path):
    """An accepted order whose id we do not know is a reconciliation hazard."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201)

    with pytest.raises(OrderRejected, match="Location"):
        broker_with(handler, tmp_path).submit_limit_order(
            "QQQ", Side.BUY, 1, 500.55
        )


def test_location_parser_handles_trailing_slash():
    resp = httpx.Response(201, headers={"Location": "https://x/orders/42/"})
    assert _order_id_from_location(resp) == "42"


@pytest.mark.parametrize("raw,expected", [
    ("FILLED", OrderState.FILLED),
    ("WORKING", OrderState.WORKING),
    ("QUEUED", OrderState.WORKING),
    ("PENDING_CANCEL", OrderState.WORKING),     # still live until confirmed
    ("CANCELED", OrderState.CANCELLED),
    ("REPLACED", OrderState.CANCELLED),
    ("REJECTED", OrderState.REJECTED),
    ("EXPIRED", OrderState.EXPIRED),
    ("SOMETHING_NEW", OrderState.UNKNOWN),      # never guessed as terminal
])
def test_status_mapping(raw, expected):
    order = _parse_order({"orderId": 1, "status": raw, "quantity": 10,
                          "orderLegCollection": [
                              {"instruction": "BUY",
                               "instrument": {"symbol": "QQQ"}}]}, "schwab")
    assert order.state is expected


def test_average_fill_price_from_execution_legs():
    order = _parse_order({
        "orderId": 7, "status": "FILLED", "quantity": 10, "filledQuantity": 10,
        "price": 500.55,
        "orderLegCollection": [
            {"instruction": "BUY", "instrument": {"symbol": "QQQ"}}],
        "orderActivityCollection": [
            {"executionLegs": [
                {"quantity": 4, "price": 500.00},
                {"quantity": 6, "price": 500.10},
            ]},
        ],
    }, "schwab")
    assert order.filled_quantity == 10
    assert order.average_fill_price == pytest.approx(500.06)


def test_replace_submits_only_the_unfilled_quantity(tmp_path):
    """After a partial fill, the replacement must not re-buy what filled."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={
                # Schwab reports a partial as WORKING plus filledQuantity.
                "orderId": 55, "status": "WORKING", "quantity": 10,
                "filledQuantity": 4, "price": 500.55,
                "orderLegCollection": [
                    {"instruction": "BUY", "instrument": {"symbol": "QQQ"}}],
            })
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, headers={"Location": "https://x/orders/66"})

    order = broker_with(handler, tmp_path).replace_order("55", 500.75)
    assert seen["body"]["orderLegCollection"][0]["quantity"] == 6
    assert seen["body"]["price"] == "500.75"
    assert order.order_id == "66"
    assert order.quantity == 6


# --- transport-level failures -----------------------------------------------


def test_unauthorised_raises_auth_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "expired"})

    with pytest.raises(BrokerAuthError, match="rejected credentials"):
        broker_with(handler, tmp_path).get_accounts()


def test_client_error_body_is_surfaced(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "insufficient buying power"})

    with pytest.raises(OrderRejected, match="insufficient buying power"):
        broker_with(handler, tmp_path).submit_limit_order(
            "QQQ", Side.BUY, 1, 500.55
        )


def test_post_timeouts_are_not_retried(tmp_path):
    """A retried order submission could double the position."""
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.TimeoutException("timed out", request=request)

    from trading.errors import BrokerTimeout

    with pytest.raises(BrokerTimeout):
        broker_with(handler, tmp_path).submit_limit_order(
            "QQQ", Side.BUY, 1, 500.55
        )
    assert calls["n"] == 1


def test_multiple_accounts_without_a_selection_is_refused(tmp_path):
    from trading.errors import ConfigError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"accountNumber": "111111111", "hashValue": "A"},
            {"accountNumber": "222222222", "hashValue": "B"},
        ])

    broker = broker_with(handler, tmp_path, schwab_account_number=None)
    with pytest.raises(ConfigError, match="SCHWAB_ACCOUNT_NUMBER"):
        broker._select_account(broker.get_accounts())


def test_account_selection_matches_on_last_four(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"accountNumber": "111111111", "hashValue": "A"},
            {"accountNumber": "222222222", "hashValue": "B"},
        ])

    broker = broker_with(handler, tmp_path, schwab_account_number="222222222")
    assert broker._select_account(broker.get_accounts()).account_id == "B"
