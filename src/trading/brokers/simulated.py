"""Simulated broker: the engine behind dry-run mode and the unit tests.

Two classes live here.

``SimulatedBroker``
    A self-contained fake exchange with a configurable order book, cash
    ledger and fill behaviour. Deterministic — no randomness — so tests
    assert exact prices and quantities.

``ShadowBroker``
    Wraps a *real* broker. Reads (accounts, positions, cash, quotes) go to
    the live API; every mutation is intercepted and simulated locally. This
    is what dry-run uses when credentials are present, so you can rehearse a
    command against your genuine balances and genuine quotes with zero risk
    of an order reaching the venue.
"""

from __future__ import annotations

import itertools
import threading
import time
from dataclasses import dataclass, replace

from ..config import Settings
from ..errors import (
    BrokerTimeout,
    InsufficientFunds,
    OrderRejected,
    OrderValidationError,
)
from ..models import (
    Account,
    Bar,
    Balances,
    Order,
    OrderState,
    Position,
    Quote,
    Side,
)
from .base import Broker

_ids = itertools.count(1)


@dataclass
class SimulationConfig:
    """Knobs the tests use to reproduce awkward real-world behaviour."""

    #: Polls of ``get_order_status`` before any fill occurs.
    polls_before_fill: int = 1
    #: Shares filled per poll once filling starts. ``None`` = fill it all.
    fill_chunk: int | None = None
    #: Never fill, regardless of price (simulates a resting order).
    never_fill: bool = False
    #: Reject the next submitted order with this message.
    reject_next: str | None = None
    #: Raise BrokerTimeout on the next mutating call.
    timeout_next: bool = False
    #: Raise BrokerTimeout on the next read call.
    timeout_next_read: bool = False
    #: Per-call artificial latency in seconds.
    latency: float = 0.0
    #: Fill at the limit price rather than the touch (pessimistic fills).
    fill_at_limit: bool = False
    #: Applied to every quote after each poll, to simulate a moving market.
    price_drift_per_poll: float = 0.0


class SimulatedBroker(Broker):
    """A deterministic in-memory broker."""

    def __init__(
        self,
        settings: Settings,
        *,
        name: str = "simulated",
        cash: float = 100_000.0,
        positions: dict[str, float] | None = None,
        quotes: dict[str, Quote] | None = None,
        sim: SimulationConfig | None = None,
        account_id: str = "SIM000001",
        is_paper: bool = True,
    ) -> None:
        super().__init__(settings)
        self.name = name
        self.broker_type = name
        self.sim = sim or SimulationConfig()
        self._lock = threading.RLock()
        self._cash = cash
        self._initial_cash = cash
        self._positions: dict[str, float] = dict(positions or {})
        self._avg_price: dict[str, float] = {}
        self._orders: dict[str, Order] = {}
        self._poll_counts: dict[str, int] = {}
        #: Per-symbol staleness, in seconds. Zero means the quote is always
        #: current at the moment it is fetched, which is how a real feed
        #: behaves; a test sets it non-zero to exercise the staleness guard.
        self._quote_age: dict[str, float] = {}
        self._quotes: dict[str, Quote] = quotes or self._default_quotes()
        self._account = Account(
            account_id=account_id,
            display_id=f"...{account_id[-4:]}",
            is_paper=is_paper,
            account_type="SIMULATED",
        )
        self.submitted_orders: list[Order] = []
        self.cancelled_orders: list[str] = []
        self.replaced_orders: list[tuple[str, float]] = []

    # --- setup helpers used by tests and dry-run --------------------------

    def _default_quotes(self) -> dict[str, Quote]:
        now = time.time()
        long_sym, short_sym = self.settings.symbols
        return {
            long_sym: Quote(long_sym, 500.00, 500.04, 500.02, now, 800, 900,
                            source="simulated",
                            day_high=502.50, day_low=497.75),
            short_sym: Quote(short_sym, 32.10, 32.12, 32.11, now, 4000, 3800,
                             source="simulated",
                             day_high=32.28, day_low=31.95),
        }

    def set_quote(self, symbol: str, bid: float, ask: float,
                  last: float | None = None, *, age: float = 0.0,
                  halted: bool = False, delayed: bool = False,
                  day_high: float | None = None,
                  day_low: float | None = None) -> None:
        mid = (bid + ask) / 2
        self._quote_age[symbol.upper()] = age
        self._quotes[symbol.upper()] = Quote(
            symbol=symbol.upper(),
            bid=bid,
            ask=ask,
            last=last if last is not None else (bid + ask) / 2,
            timestamp=time.time() - age,
            bid_size=1000,
            ask_size=1000,
            day_high=day_high if day_high is not None else round(mid * 1.005, 4),
            day_low=day_low if day_low is not None else round(mid * 0.995, 4),
            halted=halted,
            delayed=delayed,
            source="simulated",
        )

    def set_cash(self, cash: float) -> None:
        self._cash = cash

    def set_position(self, symbol: str, quantity: float,
                     average_price: float = 0.0) -> None:
        if quantity:
            self._positions[symbol.upper()] = quantity
            self._avg_price[symbol.upper()] = average_price
        else:
            self._positions.pop(symbol.upper(), None)

    # --- Broker interface -------------------------------------------------

    def connect(self) -> None:
        self._connected = True
        self._last_error = None

    def _tick(self, mutating: bool = False) -> None:
        if self.sim.latency:
            time.sleep(self.sim.latency)
        if mutating and self.sim.timeout_next:
            self.sim.timeout_next = False
            raise BrokerTimeout(f"{self.name}: simulated request timeout")
        if not mutating and self.sim.timeout_next_read:
            self.sim.timeout_next_read = False
            raise BrokerTimeout(f"{self.name}: simulated read timeout")

    def get_accounts(self) -> list[Account]:
        self._tick()
        return [self._account] if self._account else []

    def get_positions(self) -> list[Position]:
        self._tick()
        with self._lock:
            out = []
            for sym, qty in self._positions.items():
                if abs(qty) < 1e-9:
                    continue
                q = self._quotes.get(sym)
                px = q.mid if q else self._avg_price.get(sym, 0.0)
                out.append(
                    Position(
                        symbol=sym,
                        quantity=qty,
                        average_price=self._avg_price.get(sym, px),
                        market_value=qty * px,
                        settled_quantity=qty,
                    )
                )
            return out

    def get_available_cash(self) -> Balances:
        self._tick()
        with self._lock:
            return Balances(
                cash_available=round(self._cash, 2),
                buying_power=round(self._cash, 2),
                cash_balance=round(self._cash, 2),
                settled_cash=round(self._cash, 2),
            )

    def get_quote(self, symbol: str) -> Quote:
        self._tick()
        q = self._quotes.get(symbol.upper())
        if q is None:
            raise OrderValidationError(f"{self.name}: no simulated quote for {symbol}")
        # Re-stamp on every fetch, so a quote is as current as a real feed's
        # would be. Only a deliberately configured age makes it stale.
        age = self._quote_age.get(symbol.upper(), 0.0)
        return replace(q, timestamp=time.time() - age)

    def get_open_orders(self) -> list[Order]:
        self._tick()
        with self._lock:
            return [o for o in self._orders.values() if o.state.is_open]

    def cancel_order(self, order_id: str) -> None:
        self._tick(mutating=True)
        with self._lock:
            self.cancelled_orders.append(order_id)
            order = self._orders.get(order_id)
            if order is None:
                raise OrderRejected(f"{self.name}: unknown order {order_id}")
            if order.state.is_terminal:
                return
            order.state = (
                OrderState.CANCELLED
                if order.filled_quantity == 0
                else OrderState.CANCELLED
            )
            order.updated_at = time.time()

    def submit_limit_order(
        self, symbol: str, side: Side, quantity: int, limit_price: float
    ) -> Order:
        self._tick(mutating=True)
        symbol = symbol.upper()
        with self._lock:
            if self.sim.reject_next:
                msg = self.sim.reject_next
                self.sim.reject_next = None
                raise OrderRejected(f"{self.name}: {msg}")

            if side is Side.BUY:
                cost = quantity * limit_price
                if cost > self._cash + 1e-6:
                    raise InsufficientFunds(
                        f"{self.name}: order cost ${cost:,.2f} exceeds cash "
                        f"${self._cash:,.2f}"
                    )
            else:
                held = self._positions.get(symbol, 0.0)
                if quantity > held + 1e-9:
                    raise OrderRejected(
                        f"{self.name}: cannot sell {quantity} {symbol}, "
                        f"holding {held:g}"
                    )

            order_id = f"SIM-{next(_ids):06d}"
            order = Order(
                order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                limit_price=limit_price,
                state=OrderState.WORKING,
                broker=self.name,
                client_order_id=order_id,
                simulated=True,
            )
            self._orders[order_id] = order
            self._poll_counts[order_id] = 0
            self.submitted_orders.append(order)
            return order

    def replace_order(self, order_id: str, limit_price: float) -> Order:
        self._tick(mutating=True)
        with self._lock:
            old = self._orders.get(order_id)
            if old is None:
                raise OrderRejected(f"{self.name}: unknown order {order_id}")
            if old.state.is_terminal:
                raise OrderRejected(
                    f"{self.name}: cannot replace {order_id} in state {old.state.value}"
                )
            self.replaced_orders.append((order_id, limit_price))
            remaining = int(old.remaining_quantity)
            old.state = OrderState.CANCELLED
            old.updated_at = time.time()

            new_id = f"SIM-{next(_ids):06d}"
            new = Order(
                order_id=new_id,
                symbol=old.symbol,
                side=old.side,
                quantity=remaining,
                limit_price=limit_price,
                state=OrderState.WORKING,
                broker=self.name,
                client_order_id=new_id,
                simulated=True,
            )
            self._orders[new_id] = new
            self._poll_counts[new_id] = 0
            self.submitted_orders.append(new)
            return new

    def get_order_status(self, order_id: str) -> Order:
        self._tick()
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise OrderRejected(f"{self.name}: unknown order {order_id}")
            if order.state.is_open:
                self._poll_counts[order_id] = self._poll_counts.get(order_id, 0) + 1
                self._maybe_fill(order)
                self._apply_drift()
            return order

    # --- simulation internals --------------------------------------------

    def _maybe_fill(self, order: Order) -> None:
        if self.sim.never_fill:
            return
        if self._poll_counts.get(order.order_id, 0) < self.sim.polls_before_fill:
            return

        quote = self._quotes.get(order.symbol)
        if quote is None:
            return

        # Marketability: a buy fills only if it is priced at or above the
        # offer; a sell only if at or below the bid.
        if order.side is Side.BUY:
            if order.limit_price + 1e-9 < quote.ask:
                return
            fill_price = order.limit_price if self.sim.fill_at_limit else quote.ask
        else:
            if order.limit_price - 1e-9 > quote.bid:
                return
            fill_price = order.limit_price if self.sim.fill_at_limit else quote.bid

        chunk = self.sim.fill_chunk or int(order.remaining_quantity)
        chunk = min(chunk, int(order.remaining_quantity))
        if chunk <= 0:
            return

        prior_qty = order.filled_quantity
        prior_notional = prior_qty * order.average_fill_price
        order.filled_quantity = prior_qty + chunk
        order.average_fill_price = (
            (prior_notional + chunk * fill_price) / order.filled_quantity
        )
        order.updated_at = time.time()
        order.state = (
            OrderState.FILLED
            if order.remaining_quantity <= 1e-9
            else OrderState.PARTIALLY_FILLED
        )

        if order.side is Side.BUY:
            self._cash -= chunk * fill_price
            held = self._positions.get(order.symbol, 0.0)
            prior_avg = self._avg_price.get(order.symbol, 0.0)
            new_qty = held + chunk
            self._avg_price[order.symbol] = (
                (held * prior_avg + chunk * fill_price) / new_qty if new_qty else 0.0
            )
            self._positions[order.symbol] = new_qty
        else:
            self._cash += chunk * fill_price
            held = self._positions.get(order.symbol, 0.0) - chunk
            if abs(held) < 1e-9:
                self._positions.pop(order.symbol, None)
                self._avg_price.pop(order.symbol, None)
            else:
                self._positions[order.symbol] = held

    def _apply_drift(self) -> None:
        if not self.sim.price_drift_per_poll:
            return
        d = self.sim.price_drift_per_poll
        for sym, q in list(self._quotes.items()):
            self._quotes[sym] = Quote(
                symbol=q.symbol,
                bid=round(q.bid + d, 4),
                ask=round(q.ask + d, 4),
                last=round(q.last + d, 4),
                timestamp=time.time(),
                bid_size=q.bid_size,
                ask_size=q.ask_size,
                delayed=q.delayed,
                halted=q.halted,
                source=q.source,
                day_high=round(max(q.day_high, q.last + d), 4),
                day_low=round(min(q.day_low, q.last + d), 4) if q.day_low else 0.0,
            )


class ShadowBroker(Broker):
    """Real market data and real balances; orders are simulated locally.

    Used by dry-run mode so a rehearsal reflects your actual account. Every
    mutating method is overridden to touch the simulator only — the wrapped
    adapter's ``submit_limit_order``/``cancel_order``/``replace_order`` are
    never called, by construction rather than by a flag check.
    """

    def __init__(self, real: Broker, settings: Settings) -> None:
        super().__init__(settings)
        self.real = real
        self.name = real.name
        self.broker_type = real.broker_type
        self._sim = SimulatedBroker(settings, name=f"{real.name}-shadow")
        self._primed = False

    def connect(self) -> None:
        self.real.connect()
        self._connected = self.real.connected
        self._last_error = self.real.last_error
        self._account = self.real.account
        self._prime()

    def _prime(self) -> None:
        """Seed the simulator from the live account, once."""
        if self._primed:
            return
        try:
            balances = self.real.get_available_cash()
            self._sim.set_cash(balances.cash_available)
            for p in self.real.get_positions():
                self._sim.set_position(p.symbol, p.quantity, p.average_price)
            self._primed = True
        except Exception as exc:  # keep dry-run usable when reads fail
            self._last_error = f"shadow prime failed: {exc}"

    # Reads: straight through to the live API.
    def get_accounts(self) -> list[Account]:
        return self.real.get_accounts()

    def get_quote(self, symbol: str) -> Quote:
        quote = self.real.get_quote(symbol)
        # Carry the session range across too, or the simulator's copy of the
        # quote would report no high/low and the ladder would lose its anchors
        # in dry run.
        self._sim.set_quote(
            symbol, quote.bid, quote.ask, quote.last,
            day_high=quote.day_high, day_low=quote.day_low,
        )
        return quote

    def get_recent_fills(self, days: int = 30):
        # Real fills are real history; dry run only shadows new orders.
        return self.real.get_recent_fills(days)

    def get_price_bars(
        self,
        symbol: str,
        start: float,
        end: float,
        *,
        bar_size: str = "1h",
        outside_rth: bool = True,
    ) -> list[Bar] | None:
        """Historical bars are pure market data, so they come from the real API.

        Without this the base class's "unsupported" default would apply and a
        dry run would silently show an empty chart.
        """
        return self.real.get_price_bars(
            symbol, start, end, bar_size=bar_size, outside_rth=outside_rth
        )

    def get_positions(self) -> list[Position]:
        self._prime()
        # After a simulated fill the live API would still show the old
        # position, so the simulator is authoritative once it has been primed.
        return self._sim.get_positions() if self._primed else self.real.get_positions()

    def get_available_cash(self) -> Balances:
        self._prime()
        return self._sim.get_available_cash() if self._primed else self.real.get_available_cash()

    def get_open_orders(self) -> list[Order]:
        """Live working orders plus anything this dry run has simulated."""
        live: list[Order] = []
        try:
            live = self.real.get_open_orders()
        except Exception as exc:
            self._last_error = str(exc)
        return live + self._sim.get_open_orders()

    # Mutations: simulator only.
    def cancel_order(self, order_id: str) -> None:
        try:
            self._sim.cancel_order(order_id)
        except OrderRejected:
            # The id belongs to a genuine working order at the broker. In a
            # dry run we must not cancel it, so this is a logged no-op.
            self._last_error = None

    def submit_limit_order(
        self, symbol: str, side: Side, quantity: int, limit_price: float
    ) -> Order:
        self._prime()
        order = self._sim.submit_limit_order(symbol, side, quantity, limit_price)
        order.simulated = True
        return order

    def replace_order(self, order_id: str, limit_price: float) -> Order:
        return self._sim.replace_order(order_id, limit_price)

    def get_order_status(self, order_id: str) -> Order:
        return self._sim.get_order_status(order_id)

    def close(self) -> None:
        self.real.close()
