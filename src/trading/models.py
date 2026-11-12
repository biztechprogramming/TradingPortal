"""Broker-neutral data model.

Every broker adapter converts its native payloads into these types, so the
strategy layer never sees a Schwab or IBKR field name.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Command(str, Enum):
    GO_LONG = "GO_LONG"
    GO_SHORT = "GO_SHORT"
    GO_FLAT = "GO_FLAT"


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Exposure(str, Enum):
    LONG = "LONG"
    SHORT = "SHORT"
    FLAT = "FLAT"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class OrderState(str, Enum):
    PENDING = "PENDING"          # accepted locally, not yet acknowledged
    WORKING = "WORKING"          # live at the venue
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"

    @property
    def is_terminal(self) -> bool:
        return self in (
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        )

    @property
    def is_open(self) -> bool:
        return self in (
            OrderState.PENDING,
            OrderState.WORKING,
            OrderState.PARTIALLY_FILLED,
        )


class TradingMode(str, Enum):
    DRY_RUN = "dry_run"   # never mutates anything at a broker
    PAPER = "paper"       # mutates only broker paper/sandbox environments
    LIVE = "live"         # real money


@dataclass(frozen=True)
class Quote:
    symbol: str
    bid: float
    ask: float
    last: float
    timestamp: float                 # epoch seconds of the broker's quote time
    bid_size: int = 0
    ask_size: int = 0
    delayed: bool = False
    halted: bool = False
    source: str = ""
    #: Session high/low as reported by the broker. ``0.0`` means the broker
    #: did not supply it, which is different from a genuine zero and is why
    #: the ladder treats it as "no anchor" rather than pricing off it.
    day_high: float = 0.0
    day_low: float = 0.0

    @property
    def has_day_range(self) -> bool:
        """True when both session extremes are usable as ladder anchors."""
        return self.day_high > 0 and self.day_low > 0 and self.day_high >= self.day_low

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2.0
        return self.last

    @property
    def spread(self) -> float:
        return self.ask - self.bid

    @property
    def spread_bps(self) -> float:
        m = self.mid
        if m <= 0:
            return float("inf")
        return (self.spread / m) * 10_000.0

    def age_seconds(self, now: float | None = None) -> float:
        return (now if now is not None else time.time()) - self.timestamp

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["mid"] = round(self.mid, 4)
        d["spread_bps"] = round(self.spread_bps, 2)
        return d


@dataclass(frozen=True)
class Bar:
    """One OHLCV candle. ``ts`` is the bar's *opening* epoch second, UTC.

    Anchoring on the open (not the close) is what lets bars from different
    sources be merged and de-duplicated by timestamp without an off-by-one
    bar, and is why the cache uses ``(symbol, ts)`` as its primary key.
    """

    symbol: str
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_row(self) -> list[float]:
        """Compact form for the dashboard: 1440 bars as objects is wasteful."""
        return [self.ts, self.open, self.high, self.low, self.close, self.volume]


@dataclass(frozen=True)
class Position:
    symbol: str
    quantity: float           # signed: negative means short
    average_price: float = 0.0
    market_value: float = 0.0
    #: Shares that have settled and are unambiguously sellable. Some brokers
    #: do not report this; then it equals ``quantity``.
    settled_quantity: float | None = None

    @property
    def is_long(self) -> bool:
        return self.quantity > 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Balances:
    #: Cash we are allowed to spend today without borrowing.
    cash_available: float
    #: Broker-reported buying power, which may include margin.
    buying_power: float
    #: Total cash in the account, settled or not.
    cash_balance: float = 0.0
    #: Cash that has fully settled (T+1). ``None`` when unreported.
    settled_cash: float | None = None
    currency: str = "USD"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Account:
    account_id: str          # opaque broker identifier used for API calls
    display_id: str          # masked identifier safe to show/log
    is_paper: bool = False
    account_type: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Order:
    order_id: str
    symbol: str
    side: Side
    quantity: float
    limit_price: float
    state: OrderState = OrderState.PENDING
    filled_quantity: float = 0.0
    average_fill_price: float = 0.0
    status_message: str = ""
    client_order_id: str = ""
    broker: str = ""
    submitted_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    simulated: bool = False
    #: "LIMIT" or "MARKET". A market order's ``limit_price`` is only the
    #: price it was sized against, not a bound on the fill.
    order_type: str = "LIMIT"

    @property
    def remaining_quantity(self) -> float:
        return max(0.0, self.quantity - self.filled_quantity)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["state"] = self.state.value
        d["remaining_quantity"] = self.remaining_quantity
        return d


@dataclass(frozen=True)
class Transaction:
    """One account activity record, normalised across brokers.

    Used only to reconstruct historical NAV, so it carries the two things
    that reconstruction needs: how many shares moved, and how much external
    money entered or left.
    """

    date: str                       # YYYY-MM-DD
    type: str                       # TRADE | DIVIDEND | TRANSFER | FEE | OTHER
    symbol: str = ""
    quantity: float = 0.0           # signed: + bought, - sold
    price: float = 0.0
    #: Net effect on cash. Negative for a purchase, positive for a sale.
    net_amount: float = 0.0
    #: Net *external* money in (+) or out (-). Non-zero only for deposits,
    #: withdrawals and transfers — a trade moves value between cash and
    #: positions without changing account value, so its flow is zero.
    external_flow: float = 0.0
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Fill:
    """Aggregate outcome of one logical buy/sell leg (may span replacements)."""

    symbol: str
    side: Side
    requested_quantity: float
    filled_quantity: float
    average_price: float
    order_ids: list[str] = field(default_factory=list)
    complete: bool = False
    reprice_count: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def notional(self) -> float:
        return self.filled_quantity * self.average_price

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["side"] = self.side.value
        d["notional"] = round(self.notional, 2)
        return d


@dataclass
class BrokerSnapshot:
    """Everything the dashboard shows for one broker."""

    broker: str
    connected: bool
    mode: TradingMode
    account: Account | None = None
    positions: list[Position] = field(default_factory=list)
    balances: Balances | None = None
    open_orders: list[Order] = field(default_factory=list)
    exposure: Exposure = Exposure.UNKNOWN
    error: str | None = None
    quotes: dict[str, Quote] = field(default_factory=dict)
    #: True when this account may be read but never traded.
    read_only: bool = False
    broker_type: str = ""

    def position_qty(self, symbol: str) -> float:
        for p in self.positions:
            if p.symbol.upper() == symbol.upper():
                return p.quantity
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "connected": self.connected,
            "mode": self.mode.value,
            "account": self.account.to_dict() if self.account else None,
            "positions": [p.to_dict() for p in self.positions],
            "balances": self.balances.to_dict() if self.balances else None,
            "open_orders": [o.to_dict() for o in self.open_orders],
            "exposure": self.exposure.value,
            "read_only": self.read_only,
            "broker_type": self.broker_type,
            "error": self.error,
            "quotes": {k: v.to_dict() for k, v in self.quotes.items()},
        }


@dataclass
class BrokerResult:
    """Outcome of running one command against one broker."""

    broker: str
    command: Command
    ok: bool
    mode: TradingMode
    exposure_before: Exposure = Exposure.UNKNOWN
    exposure_after: Exposure = Exposure.UNKNOWN
    fills: list[Fill] = field(default_factory=list)
    cash_before: float = 0.0
    cash_after: float = 0.0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: True when we may have left an order working or a position half-moved.
    needs_attention: bool = False
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    @property
    def entry_fill(self) -> Fill | None:
        """The buy leg, i.e. the fill that established the new exposure."""
        for f in self.fills:
            if f.side is Side.BUY and f.filled_quantity > 0:
                return f
        return None

    def to_dict(self) -> dict[str, Any]:
        entry = self.entry_fill
        return {
            "broker": self.broker,
            "command": self.command.value,
            "ok": self.ok,
            "mode": self.mode.value,
            "exposure_before": self.exposure_before.value,
            "exposure_after": self.exposure_after.value,
            "fills": [f.to_dict() for f in self.fills],
            "cash_before": round(self.cash_before, 2),
            "cash_after": round(self.cash_after, 2),
            "errors": self.errors,
            "warnings": self.warnings,
            "needs_attention": self.needs_attention,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_execution_price": (
                round(entry.average_price, 4) if entry else None
            ),
            "shares": entry.filled_quantity if entry else 0,
        }


@dataclass
class CommandResult:
    """Outcome across every broker for a single command."""

    command_id: str
    command: Command
    mode: TradingMode
    results: list[BrokerResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    aborted_reason: str | None = None
    #: "LIMIT" (marketable limits, repriced) or "MARKET" (market orders).
    order_type: str = "LIMIT"

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def any_ok(self) -> bool:
        return any(r.ok for r in self.results)

    @property
    def mismatch(self) -> bool:
        """One broker succeeded and another did not: never report success."""
        return self.any_ok and not self.all_ok

    @property
    def status(self) -> str:
        if self.aborted_reason:
            return "ABORTED"
        if not self.results:
            return "NO_BROKERS"
        if self.all_ok:
            return "OK"
        if self.any_ok:
            return "PARTIAL"
        return "FAILED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "command": self.command.value,
            "order_type": self.order_type,
            "mode": self.mode.value,
            "status": self.status,
            "mismatch": self.mismatch,
            "aborted_reason": self.aborted_reason,
            "results": [r.to_dict() for r in self.results],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def exposure_from_positions(
    positions: list[Position], long_symbol: str, short_symbol: str
) -> Exposure:
    """Classify an account's stance from its QQQ/PSQ holdings."""
    long_qty = short_qty = 0.0
    for p in positions:
        sym = p.symbol.upper()
        if sym == long_symbol.upper():
            long_qty += p.quantity
        elif sym == short_symbol.upper():
            short_qty += p.quantity
    if long_qty > 0 and short_qty > 0:
        return Exposure.MIXED
    if long_qty > 0:
        return Exposure.LONG
    if short_qty > 0:
        return Exposure.SHORT
    return Exposure.FLAT
