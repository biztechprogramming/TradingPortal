"""IB Gateway (TWS API) adapter, against a fake IB built from real ib_async types."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone

import pytest
from ib_async import (
    AccountValue,
    Execution,
    Fill,
    LimitOrder,
    OrderStatus,
    PortfolioItem,
    Position as IBPosition,
    Stock,
    Ticker,
    Trade,
)
from ib_async.objects import TradeLogEntry

from trading.brokers.ibkr_session import TwsSession
from trading.brokers.ibkr_tws import IBKRTwsBroker
from trading.errors import BrokerConnectionError, InvalidQuoteError, OrderRejected
from trading.models import OrderState, Side
from tests.conftest import make_settings

ACCT = "U1234567"
CLIENT_ID = 11


class Event:
    def __init__(self) -> None:
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def emit(self, *args):
        for h in self.handlers:
            h(*args)


class FakeIB:
    """Just enough of ib_async.IB for the adapter."""

    def __init__(self, *, up: bool = True) -> None:
        self.up = up
        self._connected = False
        self.connect_calls = 0
        self.connectedEvent = Event()
        self.disconnectedEvent = Event()
        self.errorEvent = Event()
        self._trades: list[Trade] = []
        self._next_id = 100
        self.positions_rows: list[IBPosition] = []
        self.portfolio_rows: list[PortfolioItem] = []
        self.summary: list[AccountValue] = []
        self.tickers: dict[str, Ticker] = {}
        self.fills: list[Fill] = []
        self.reject_next = ""
        self.cancelled: list[int] = []

    # connection
    def isConnected(self):
        return self._connected

    async def connectAsync(self, host, port, clientId, timeout):
        self.connect_calls += 1
        if not self.up:
            raise ConnectionRefusedError("refused")
        self._connected = True
        self.connectedEvent.emit()

    def drop(self):
        self._connected = False
        self.disconnectedEvent.emit()

    def disconnect(self):
        self._connected = False

    def managedAccounts(self):
        return [ACCT]

    def reqMarketDataType(self, t):
        self.market_data_type = t

    # account
    async def reqPositionsAsync(self):
        return self.positions_rows

    def positions(self, account=""):
        return [p for p in self.positions_rows if p.account == account]

    def portfolio(self, account=""):
        return [p for p in self.portfolio_rows if p.account == account]

    async def accountSummaryAsync(self, account=""):
        return self.summary

    # contracts & data
    async def qualifyContractsAsync(self, *contracts):
        out = []
        for c in contracts:
            c.conId = {"QQQ": 320227571, "PSQ": 692767261}.get(c.symbol, 0)
            out.append(c if c.conId else None)
        return out

    def reqMktData(self, contract, *_):
        return self.tickers.setdefault(contract.symbol, Ticker(contract=contract))

    # orders
    def placeOrder(self, contract, order):
        if not order.orderId:
            order.orderId = self._next_id
            self._next_id += 1
            order.clientId = CLIENT_ID
            order.permId = 9_000 + order.orderId
            trade = Trade(contract, order, OrderStatus(orderId=order.orderId,
                                                      status="PendingSubmit"))
            self._trades.append(trade)
        else:
            trade = next(t for t in self._trades if t.order.orderId == order.orderId)
        if self.reject_next:
            trade.orderStatus.status = "Inactive"
            trade.log.append(TradeLogEntry(datetime.now(timezone.utc), "Inactive",
                                           self.reject_next, 201))
            self.reject_next = ""
        else:
            trade.orderStatus.status = "Submitted"
        return trade

    def trades(self):
        return list(self._trades)

    async def reqAllOpenOrdersAsync(self):
        return [t for t in self._trades if not t.isDone()]

    async def reqCompletedOrdersAsync(self, apiOnly):
        return [t for t in self._trades if t.isDone()]

    def cancelOrder(self, order):
        self.cancelled.append(order.orderId)
        t = next(t for t in self._trades if t.order.orderId == order.orderId)
        t.orderStatus.status = "Cancelled"
        return t

    async def reqExecutionsAsync(self, execFilter=None):
        return self.fills


@pytest.fixture
def fake():
    return FakeIB()


def make_broker(tmp_path, fake, **overrides):
    settings = make_settings(tmp_path, ibkr_backend="tws",
                             ibkr_tws_connect_timeout=2.0, **overrides)
    session = TwsSession("127.0.0.1", 4001, CLIENT_ID, ib_factory=lambda: fake,
                         reconnect_interval=0.05)
    broker = IBKRTwsBroker(settings, session=session)
    return broker, session


@pytest.fixture
def broker(tmp_path, fake):
    b, session = make_broker(tmp_path, fake)
    b.connect()
    yield b
    session.stop()


# --- connection ---------------------------------------------------------------


def test_connect_selects_the_account_and_names_the_instance(broker, fake):
    assert broker.connected
    assert broker.name == f"ibkr:{ACCT}"
    assert broker.account.is_paper is False
    assert fake.market_data_type == 1     # live data unless delayed allowed


def test_gateway_down_fails_fast_with_a_clear_error(tmp_path):
    down = FakeIB(up=False)
    b, session = make_broker(tmp_path, down)
    try:
        started = time.monotonic()
        with pytest.raises(BrokerConnectionError, match="not reachable"):
            b.connect()
        assert time.monotonic() - started < 5
        assert not b.connected
    finally:
        session.stop()


def test_reconnects_by_itself_after_the_socket_drops(broker, fake):
    calls = fake.connect_calls
    broker.session.call(lambda ib: ib.drop())
    deadline = time.time() + 5
    while time.time() < deadline and fake.connect_calls == calls:
        time.sleep(0.05)
    assert fake.connect_calls > calls
    assert broker.session.wait_connected(3)
    assert broker.get_accounts()[0].account_id == ACCT


# --- reads ---------------------------------------------------------------------


def test_positions_use_portfolio_market_value(broker, fake):
    psq = Stock("PSQ", "SMART", "USD")
    psq.conId = 692767261
    fake.positions_rows = [IBPosition(ACCT, psq, 2540.0, 27.27),
                           IBPosition("U999", psq, 5.0, 1.0)]
    fake.portfolio_rows = [PortfolioItem(psq, 2540.0, 27.3, 69342.0, 27.27, 0, 0, ACCT)]
    [pos] = broker.get_positions()
    assert (pos.symbol, pos.quantity, pos.market_value) == ("PSQ", 2540.0, 69342.0)


def test_cash_is_settled_cash_capped(broker, fake):
    fake.summary = [
        AccountValue(ACCT, "AvailableFunds", "1600", "USD", ""),
        AccountValue(ACCT, "SettledCash", "1499.77", "USD", ""),
        AccountValue(ACCT, "BuyingPower", "1600", "USD", ""),
        AccountValue(ACCT, "TotalCashValue", "1499.77", "USD", ""),
    ]
    bal = broker.get_available_cash()
    assert bal.cash_available == pytest.approx(1499.77)
    assert bal.buying_power == 1600


def test_quote_waits_for_bid_and_ask(broker, fake):
    def later():
        time.sleep(0.3)
        t = fake.tickers["QQQ"]
        t.bid, t.ask, t.last, t.marketDataType = 500.0, 500.04, 500.02, 1

    import threading
    fake.reqMktData(_stock("QQQ"))
    threading.Thread(target=later).start()
    q = broker.get_quote("QQQ")
    assert (q.bid, q.ask, q.source) == (500.0, 500.04, "ibkr")


def test_no_market_data_is_an_invalid_quote(broker, fake):
    with pytest.raises(InvalidQuoteError, match="no bid/ask"):
        broker.get_quote("PSQ")


def test_delayed_data_is_refused(broker, fake):
    t = fake.reqMktData(_stock("QQQ"))
    t.bid, t.ask, t.marketDataType = 500.0, 500.04, 3
    with pytest.raises(InvalidQuoteError, match="delayed"):
        broker.get_quote("QQQ")


# --- orders --------------------------------------------------------------------


def test_submit_and_track_a_limit_order(broker, fake):
    order = broker.submit_limit_order("PSQ", Side.SELL, 2540, 27.214)
    assert order.order_id == "100" and order.state is OrderState.WORKING
    trade = fake.trades()[0]
    assert trade.order.lmtPrice == 27.21 and trade.order.account == ACCT
    assert trade.order.tif == "DAY"

    trade.orderStatus.status, trade.orderStatus.filled = "Submitted", 1000
    assert broker.get_order_status("100").state is OrderState.PARTIALLY_FILLED

    trade.orderStatus.status, trade.orderStatus.filled = "Filled", 2540
    trade.orderStatus.avgFillPrice = 27.215
    done = broker.get_order_status("100")
    assert done.state is OrderState.FILLED and done.average_fill_price == 27.215


def test_rejection_raises_with_ibkrs_reason(broker, fake):
    fake.reject_next = "Order rejected - insufficient funds"
    with pytest.raises(OrderRejected, match="insufficient funds"):
        broker.submit_limit_order("QQQ", Side.BUY, 10, 500.0)


def test_replace_modifies_the_same_order_id(broker, fake):
    broker.submit_limit_order("PSQ", Side.SELL, 100, 27.20)
    new = broker.replace_order("100", 27.18)
    assert new.order_id == "100"
    assert len(fake.trades()) == 1 and fake.trades()[0].order.lmtPrice == 27.18


def test_cancel_and_open_orders(broker, fake):
    broker.submit_limit_order("PSQ", Side.SELL, 100, 27.20)
    assert [o.order_id for o in broker.get_open_orders()] == ["100"]
    broker.cancel_order("100")
    assert fake.cancelled == [100]
    assert broker.get_open_orders() == []
    assert broker.get_order_status("100").state is OrderState.CANCELLED


def test_orders_placed_elsewhere_are_keyed_by_perm_id(broker, fake):
    order = LimitOrder("SELL", 50, 27.0, account=ACCT)
    order.orderId, order.clientId, order.permId = 0, 0, 555
    fake._trades.append(Trade(_stock("PSQ"), order,
                              OrderStatus(orderId=0, status="Submitted")))
    assert [o.order_id for o in broker.get_open_orders()] == ["p555"]
    broker.cancel_order("p555")
    assert broker.get_order_status("p555").state is OrderState.CANCELLED


def test_unknown_order_is_unknown_not_terminal(broker):
    assert broker.get_order_status("424242").state is OrderState.UNKNOWN


def test_recent_fills_group_executions_by_order(broker, fake):
    now = datetime.now(timezone.utc)
    for shares, price in ((1000, 27.2), (1540, 27.23)):
        ex = Execution(execId=f"e{shares}", time=now, acctNumber=ACCT, side="SLD",
                       shares=shares, price=price, permId=777)
        fake.fills.append(Fill(_stock("PSQ"), ex, None, now))
    [fill] = broker.get_recent_fills()
    assert fill.side is Side.SELL and fill.filled_quantity == 2540
    assert math.isclose(fill.average_fill_price,
                        (1000 * 27.2 + 1540 * 27.23) / 2540)


def _stock(symbol: str):
    s = Stock(symbol, "SMART", "USD")
    s.conId = {"QQQ": 320227571, "PSQ": 692767261}[symbol]
    return s


def test_factory_picks_the_backend_and_honours_read_only(tmp_path):
    from trading.brokers.factory import _ibkr_adapter, apply_permissions
    from trading.brokers.ibkr import IBKRBroker
    from trading.brokers.readonly import ReadOnlyBroker
    from trading.models import Account

    assert isinstance(_ibkr_adapter(make_settings(tmp_path)), IBKRBroker)
    tws = make_settings(tmp_path, ibkr_backend="tws", ibkr_read_only=True)
    adapter = _ibkr_adapter(tws)
    assert isinstance(adapter, IBKRTwsBroker)
    wrapped = apply_permissions(adapter, tws, Account(ACCT, ACCT))
    assert isinstance(wrapped, ReadOnlyBroker) and wrapped.read_only
