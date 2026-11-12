"""Interactive Brokers adapter over IB Gateway and the TWS API (ib_async).

This replaces the Client Portal Web API path (``ibkr.py``) when
``IBKR_BACKEND=tws``. IB Gateway runs in Docker under IBC, which logs in,
restarts the gateway nightly *without* a new login, and only needs the phone
approval once a week. The app holds one persistent socket to it
(:mod:`.ibkr_session`) that reconnects by itself.

Order ids are the TWS API order id for orders this client placed. The session
uses a fixed client id that IB Gateway is configured to treat as the master
client, so orders placed elsewhere (IBKR Mobile, the website) are visible and
cancellable too; those are identified by their permanent id.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any

from ..config import Settings
from ..errors import (
    BrokerAuthError,
    BrokerConnectionError,
    ConfigError,
    InvalidQuoteError,
    OrderRejected,
)
from ..models import Account, Balances, Bar, Order, OrderState, Position, Quote, Side
from ..validation import wants_outside_rth
from .base import Broker, normalize_state
from .ibkr import is_paper_account
from .ibkr_session import TwsSession, shared_session

log = logging.getLogger("trading.broker.ibkr_tws")

STATUS_MAP: dict[str, OrderState] = {
    "PENDINGSUBMIT": OrderState.PENDING,
    "APIPENDING": OrderState.PENDING,
    "PRESUBMITTED": OrderState.WORKING,
    "SUBMITTED": OrderState.WORKING,
    "APIUPDATE": OrderState.WORKING,
    "PENDINGCANCEL": OrderState.WORKING,   # still live until confirmed
    "FILLED": OrderState.FILLED,
    "CANCELLED": OrderState.CANCELLED,
    "APICANCELLED": OrderState.CANCELLED,
    "INACTIVE": OrderState.REJECTED,
    "VALIDATIONERROR": OrderState.REJECTED,
}

#: Our bar-size names -> TWS API barSizeSetting.
BAR_SIZES = {
    "1min": "1 min", "5min": "5 mins", "15min": "15 mins", "30min": "30 mins",
    "1h": "1 hour", "1d": "1 day",
}

#: TWS market data types: 1 live, 2 frozen, 3 delayed, 4 delayed-frozen.
DELAYED_TYPES = {3, 4}

#: How long a just-placed order may sit in PendingSubmit before we report it.
PLACE_ACK_TIMEOUT = 5.0


def _ok(value: Any) -> bool:
    try:
        return value is not None and not math.isnan(float(value)) and float(value) > 0
    except (TypeError, ValueError):
        return False


def _f(value: Any) -> float:
    return float(value) if _ok(value) else 0.0


def _epoch(value: Any) -> float:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    return time.time()


class IBKRTwsBroker(Broker):
    broker_type = "ibkr"
    name = "ibkr"

    def __init__(
        self,
        settings: Settings,
        *,
        account: Account | None = None,
        session: TwsSession | None = None,
    ) -> None:
        super().__init__(settings)
        self.session = session or shared_session(
            settings.ibkr_tws_host, settings.ibkr_tws_port, settings.ibkr_tws_client_id
        )
        self._contracts: dict[str, Any] = {}
        self._tickers: dict[str, Any] = {}
        self._ticker_generation = -1
        if account is not None:
            self._account = account
            self.name = f"ibkr:{account.account_id}"

    # --- lifecycle --------------------------------------------------------

    def _require_connection(self) -> None:
        if not self.session.wait_connected(self.settings.ibkr_tws_connect_timeout):
            self._connected = False
            raise BrokerConnectionError(
                self.session.last_error
                or f"IB Gateway is not connected at {self.session.host}:"
                   f"{self.session.port}. Log in to IBKR from the dashboard."
            )

    def connect(self) -> None:
        try:
            self._require_connection()
            accounts = self.get_accounts()
            if not accounts:
                raise BrokerAuthError("IB Gateway reported no IBKR accounts")
            if self._account is None:
                self._account = self._select_account(accounts)
                self.name = f"ibkr:{self._account.account_id}"
            elif self._account.account_id not in {a.account_id for a in accounts}:
                raise BrokerAuthError(
                    f"IB Gateway is logged in, but not to account "
                    f"{self._account.display_id}"
                )
            self.session.call(lambda ib: ib.reqMarketDataType(
                3 if self.settings.ibkr_allow_delayed_quotes else 1))
            self._connected = True
            self._last_error = None
            log.info("IBKR (IB Gateway) connected, account %s (%s)",
                     self._account.display_id,
                     "PAPER" if self._account.is_paper else "LIVE")
        except Exception as exc:
            self._connected = False
            self._last_error = str(exc)
            raise

    @property
    def connected(self) -> bool:
        return self._connected and self.session.connected

    def close(self) -> None:
        """The socket is shared and outlives any one adapter; nothing to do."""

    def _select_account(self, accounts: list[Account]) -> Account:
        want = (self.settings.ibkr_account_id or "").strip().upper()
        if want:
            for acct in accounts:
                if acct.account_id.upper() == want:
                    return acct
            raise ConfigError(
                f"IBKR_ACCOUNT_ID={want} not found among "
                f"{[a.account_id for a in accounts]}"
            )
        if len(accounts) > 1:
            raise ConfigError(
                "Multiple IBKR accounts available; set IBKR_ACCOUNT_ID. Found: "
                f"{[a.account_id for a in accounts]}"
            )
        return accounts[0]

    @property
    def _acct_id(self) -> str:
        if not self._account:
            raise BrokerAuthError("IBKR broker is not connected")
        return self._account.account_id

    def _call(self, fn, timeout: float = 20.0) -> Any:
        self._require_connection()
        return self.session.call(fn, timeout)

    # --- reads ------------------------------------------------------------

    def get_accounts(self) -> list[Account]:
        ids = self._call(lambda ib: list(ib.managedAccounts()))
        return [
            Account(
                account_id=str(a),
                display_id=str(a),
                is_paper=is_paper_account(str(a)),
                account_type="PAPER" if is_paper_account(str(a)) else "LIVE",
            )
            for a in ids
            if a
        ]

    def get_positions(self) -> list[Position]:
        acct = self._acct_id

        async def fetch(ib):
            await ib.reqPositionsAsync()
            values = {
                p.contract.conId: p.marketValue for p in ib.portfolio(acct)
            }
            return [(p, values.get(p.contract.conId)) for p in ib.positions(acct)]

        out: list[Position] = []
        for pos, market_value in self._call(fetch):
            qty = float(pos.position or 0)
            if abs(qty) < 1e-9:
                continue
            symbol = str(pos.contract.symbol or "").upper()
            self._contracts.setdefault(symbol, pos.contract)
            avg = float(pos.avgCost or 0)
            out.append(Position(
                symbol=symbol,
                quantity=qty,
                average_price=avg,
                market_value=float(market_value) if market_value is not None
                else qty * avg,
                settled_quantity=qty,
            ))
        return out

    def get_available_cash(self) -> Balances:
        acct = self._acct_id
        rows = self._call(lambda ib: ib.accountSummaryAsync(acct))
        tags: dict[str, float] = {}
        for row in rows or []:
            if getattr(row, "account", acct) != acct:
                continue
            if row.currency not in ("USD", "BASE", ""):
                continue
            try:
                value = float(row.value)
            except (TypeError, ValueError):
                continue
            # Prefer the USD figure over BASE when both are present.
            if row.tag not in tags or row.currency == "USD":
                tags[row.tag] = value
        if not tags:
            raise BrokerConnectionError("IB Gateway returned no account summary")
        available = tags.get("AvailableFunds", tags.get("TotalCashValue", 0.0))
        settled = tags.get("SettledCash", 0.0)
        # Cash-only means we spend settled cash, not funds that only exist
        # because IBKR is willing to lend against the portfolio.
        cash_available = min(available, settled) if settled > 0 else available
        return Balances(
            cash_available=cash_available,
            buying_power=tags.get("BuyingPower") or cash_available,
            cash_balance=tags.get("TotalCashValue", 0.0),
            settled_cash=settled or None,
            currency="USD",
        )

    # --- contracts & quotes ----------------------------------------------

    def _contract(self, symbol: str) -> Any:
        symbol = symbol.upper()
        if symbol in self._contracts:
            return self._contracts[symbol]
        from ib_async import Contract, Stock

        override = self.settings.ibkr_conid_overrides.get(symbol)
        candidate = (Contract(conId=int(override), exchange="SMART")
                     if override else Stock(symbol, "SMART", "USD"))
        qualified = self._call(lambda ib: ib.qualifyContractsAsync(candidate))
        contract = qualified[0] if qualified else None
        if contract is None or not getattr(contract, "conId", 0):
            raise ConfigError(
                f"IB Gateway could not identify {symbol}. Set IBKR_CONID_{symbol} "
                "to remove the ambiguity."
            )
        self._contracts[symbol] = contract
        log.info("IBKR resolved %s -> conid %s", symbol, contract.conId)
        return contract

    def get_quote(self, symbol: str) -> Quote:
        symbol = symbol.upper()
        contract = self._contract(symbol)
        if self._ticker_generation != self.session.generation:
            self._tickers.clear()   # subscriptions die with the socket
            self._ticker_generation = self.session.generation
        ticker = self._tickers.get(symbol)
        if ticker is None:
            ticker = self._call(lambda ib: ib.reqMktData(contract, "", False, False))
            self._tickers[symbol] = ticker

        async def settled(ib):
            for _ in range(30):
                if _ok(ticker.bid) and _ok(ticker.ask):
                    break
                await asyncio.sleep(0.1)
            return ticker

        t = self._call(settled)
        if not (_ok(t.bid) and _ok(t.ask)):
            req_err = self.session.contract_errors.get(int(contract.conId))
            hint = f" IBKR said: {req_err[1]}" if req_err else ""
            raise InvalidQuoteError(
                f"IB Gateway returned no bid/ask for {symbol}.{hint} Check that "
                "this IBKR username has a US equities market-data subscription."
            )
        delayed = int(getattr(t, "marketDataType", 1) or 1) in DELAYED_TYPES
        if delayed and not self.settings.ibkr_allow_delayed_quotes:
            raise InvalidQuoteError(
                f"IBKR is returning delayed data for {symbol}. Trading on "
                "delayed prices is refused; subscribe to real-time US equity "
                "data or set IBKR_ALLOW_DELAYED_QUOTES=true to accept the risk."
            )
        return Quote(
            symbol=symbol,
            bid=float(t.bid),
            ask=float(t.ask),
            last=_f(t.last) or (float(t.bid) + float(t.ask)) / 2,
            timestamp=_epoch(getattr(t, "time", None)),
            bid_size=int(_f(t.bidSize)),
            ask_size=int(_f(t.askSize)),
            delayed=delayed,
            halted=_f(getattr(t, "halted", 0)) > 0,
            source="ibkr",
            day_high=_f(getattr(t, "high", 0)),
            day_low=_f(getattr(t, "low", 0)),
        )

    def get_price_bars(
        self, symbol: str, start: float, end: float, *,
        bar_size: str = "1h", outside_rth: bool = True,
    ) -> list[Bar] | None:
        setting = BAR_SIZES.get(bar_size)
        if setting is None:
            return None
        try:
            contract = self._contract(symbol)
            days = max(1, math.ceil((time.time() - start) / 86400.0))
            duration = f"{days} D" if days <= 365 else f"{math.ceil(days / 365)} Y"
            rows = self._call(lambda ib: ib.reqHistoricalDataAsync(
                contract, "", duration, setting, "TRADES", not outside_rth,
                formatDate=2, timeout=50), timeout=60)
        except Exception as exc:
            log.info("IBKR bars unavailable for %s (%s): %s", symbol, bar_size, exc)
            return None
        bars: list[Bar] = []
        for raw in rows or []:
            ts = int(_epoch(raw.date) if isinstance(raw.date, datetime)
                     else datetime(raw.date.year, raw.date.month, raw.date.day,
                                   tzinfo=timezone.utc).timestamp())
            if start <= ts <= end:
                bars.append(Bar(symbol=symbol.upper(), ts=ts, open=float(raw.open),
                                high=float(raw.high), low=float(raw.low),
                                close=float(raw.close), volume=float(raw.volume or 0)))
        return sorted(bars, key=lambda b: b.ts)

    # --- orders -----------------------------------------------------------

    def _to_order(self, trade: Any) -> Order:
        o, st = trade.order, trade.orderStatus
        state = normalize_state(st.status.replace(" ", ""), STATUS_MAP)
        filled = float(st.filled or 0)
        total = float(o.totalQuantity or 0)
        if state is OrderState.WORKING and 0 < filled < total:
            state = OrderState.PARTIALLY_FILLED
        message = ""
        for entry in reversed(getattr(trade, "log", []) or []):
            if getattr(entry, "errorCode", 0) or (state is OrderState.REJECTED and entry.message):
                message = entry.message
                break
        return Order(
            order_id=self._order_key(trade),
            symbol=str(trade.contract.symbol or "").upper(),
            side=Side.SELL if str(o.action).upper() == "SELL" else Side.BUY,
            quantity=total,
            limit_price=_f(o.lmtPrice),
            state=state,
            filled_quantity=filled,
            average_fill_price=_f(st.avgFillPrice),
            status_message=message,
            client_order_id=str(o.orderRef or ""),
            broker=self.name,
            updated_at=_epoch(trade.log[-1].time) if getattr(trade, "log", None) else time.time(),
        )

    def _order_key(self, trade: Any) -> str:
        o = trade.order
        mine = o.orderId and o.orderId > 0 and o.clientId == self.session.client_id
        return str(o.orderId) if mine else f"p{o.permId}"

    def _find_trade(self, order_id: str, refresh: bool = True) -> Any | None:
        def match(ib):
            for t in ib.trades():
                if self._order_key(t) == order_id:
                    return t
            return None

        trade = self._call(match)
        if trade is None and refresh:
            async def reload(ib):
                await ib.reqAllOpenOrdersAsync()
                await ib.reqCompletedOrdersAsync(False)
            self._call(reload)
            trade = self._call(match)
        return trade

    def get_open_orders(self) -> list[Order]:
        acct = self._acct_id
        trades = self._call(lambda ib: ib.reqAllOpenOrdersAsync())
        out = []
        for t in trades or []:
            if t.order.account and t.order.account != acct:
                continue
            order = self._to_order(t)
            if order.state.is_open:
                out.append(order)
        return out

    def get_order_status(self, order_id: str) -> Order:
        trade = self._find_trade(str(order_id))
        if trade is None:
            return Order(
                order_id=str(order_id), symbol="", side=Side.BUY, quantity=0,
                limit_price=0, state=OrderState.UNKNOWN, broker=self.name,
                status_message="IB Gateway has no record of this order",
            )
        return self._to_order(trade)

    def get_recent_fills(self, days: int = 30) -> list[Order] | None:
        from ib_async import ExecutionFilter

        acct = self._acct_id
        fills = self._call(lambda ib: ib.reqExecutionsAsync(ExecutionFilter(acctCode=acct)))
        grouped: dict[str, dict[str, Any]] = {}
        for fill in fills or []:
            ex = fill.execution
            if ex.acctNumber and ex.acctNumber != acct:
                continue
            g = grouped.setdefault(str(ex.permId or ex.execId), {
                "symbol": str(fill.contract.symbol).upper(),
                "side": Side.SELL if str(ex.side).upper().startswith("S") else Side.BUY,
                "shares": 0.0, "notional": 0.0, "at": 0.0,
            })
            g["shares"] += float(ex.shares)
            g["notional"] += float(ex.shares) * float(ex.price)
            g["at"] = max(g["at"], _epoch(ex.time))
        return [
            Order(order_id=f"p{key}", symbol=g["symbol"], side=g["side"],
                  quantity=g["shares"], limit_price=0.0, state=OrderState.FILLED,
                  filled_quantity=g["shares"],
                  average_fill_price=g["notional"] / g["shares"],
                  broker=self.name, updated_at=g["at"])
            for key, g in grouped.items() if g["shares"] > 0
        ]

    def submit_limit_order(
        self, symbol: str, side: Side, quantity: int, limit_price: float
    ) -> Order:
        from ib_async import LimitOrder

        contract = self._contract(symbol)
        ref = f"qs{int(time.time() * 1000) % 10_000_000_000}"
        order = LimitOrder(
            side.value, int(quantity), round(float(limit_price), 2),
            tif="DAY", outsideRth=wants_outside_rth(self.settings),
            account=self._acct_id, orderRef=ref,
        )
        return self._place(contract, order)

    def submit_market_order(
        self, symbol: str, side: Side, quantity: int
    ) -> Order:
        from ib_async import MarketOrder

        contract = self._contract(symbol)
        ref = f"qs{int(time.time() * 1000) % 10_000_000_000}"
        order = MarketOrder(
            side.value, int(quantity), tif="DAY", outsideRth=False,
            account=self._acct_id, orderRef=ref,
        )
        result = self._place(contract, order)
        result.order_type = "MARKET"
        return result

    def replace_order(self, order_id: str, limit_price: float) -> Order:
        trade = self._find_trade(str(order_id))
        if trade is None:
            raise OrderRejected(f"IBKR order {order_id} not found to modify")
        current = self._to_order(trade)
        if current.state.is_terminal or current.remaining_quantity <= 0:
            raise OrderRejected(f"IBKR order {order_id} has nothing left to modify")
        # Re-placing the same order id modifies it in place; totalQuantity
        # stays the original size and IBKR keeps the filled part.
        trade.order.lmtPrice = round(float(limit_price), 2)
        trade.order.outsideRth = wants_outside_rth(self.settings)
        return self._place(trade.contract, trade.order)

    def _place(self, contract: Any, order: Any) -> Order:
        async def place(ib):
            trade = ib.placeOrder(contract, order)
            deadline = time.monotonic() + PLACE_ACK_TIMEOUT
            while time.monotonic() < deadline:
                if trade.orderStatus.status not in ("PendingSubmit", "ApiPending", ""):
                    break
                await asyncio.sleep(0.05)
            return trade

        trade = self._call(place)
        result = self._to_order(trade)
        if result.state is OrderState.REJECTED or (
            result.state is OrderState.CANCELLED and result.filled_quantity == 0
            and result.status_message
        ):
            raise OrderRejected(
                f"IBKR rejected the order: {result.status_message or trade.orderStatus.status}"
            )
        return result

    def cancel_order(self, order_id: str) -> None:
        trade = self._find_trade(str(order_id))
        if trade is None:
            raise OrderRejected(f"IBKR order {order_id} not found to cancel")
        if trade.isDone():
            return
        self._call(lambda ib: ib.cancelOrder(trade.order))
