"""Price ladder around the session high and low.

The ladder answers one question: *"buy this symbol if it reaches this price."*
Rungs sit at ``HIGH ± n·step`` and ``LOW ± n·step``, and each rung is one
click.

Two behaviours here are deliberate and worth stating plainly, because both
have money consequences:

**Every rung is a plain BUY limit order at exactly the rung price.** A buy
limit at or above the current ask is *marketable* — it fills immediately at
the market, not at the rung price. So the rungs above the session high do not
wait for a breakout; they buy now. :attr:`RungView.marketable` flags exactly
which rungs behave that way so the dashboard can say so before the click. A
rung below the market rests until the price comes down to it.

**Placing an order is not taking a position.** A working order changes
nothing about what the account holds. Positions on the dashboard come only
from the broker's own position report, never from "we just sent an order",
which is why :class:`RungOrderResult` carries the refreshed open-order list
rather than any optimistic position figure.

Clicking a rung while an order is already working *modifies that order's
price* instead of adding a second one — see :meth:`LadderExecutor.place`.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

from . import audit as A
from .audit import AuditLog
from .brokers.base import Broker
from .config import Settings
from .errors import (
    OrderValidationError,
    QuoteError,
    OrderTimeout,
    TradingError,
    WideSpreadError,
)
from .models import Balances, Order, Quote, Side, TradingMode
from .pricing import validate_quote
from .sizing import calculate_quantity, spendable_cash
from .validation import check_market_hours, validate_order

log = logging.getLogger("trading.ladder")

#: What a rung is measured from.
ANCHOR_HIGH = "HIGH"
ANCHOR_LOW = "LOW"


def round_to_ladder_tick(price: float, tick: float) -> float:
    """Snap a price onto the symbol's ladder grid.

    Deliberately separate from :func:`trading.pricing.round_to_tick`, which
    rounds onto ``TICK_SIZE`` (the penny grid used for marketable limits). A
    ladder tick may be finer — PSQ steps by 0.005 — and rounding those rungs
    onto pennies would silently collapse every second rung onto its neighbour.
    """
    if tick <= 0:
        return round(price, 4)
    # math.floor(x + 0.5) rather than round(): round() is banker's rounding,
    # which sends .5 to the nearest *even* unit, so two adjacent rungs can
    # land on the same grid point and silently collapse into one.
    units = math.floor(price / tick + 0.5)
    return round(units * tick, 6)


@dataclass(frozen=True)
class Rung:
    """One price level on the ladder."""

    symbol: str
    price: float
    anchor: str          # ANCHOR_HIGH | ANCHOR_LOW
    offset_steps: int    # signed distance from the anchor, in steps
    anchor_price: float

    @property
    def label(self) -> str:
        if self.offset_steps == 0:
            return self.anchor
        return f"{self.anchor} {self.offset_steps:+d}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "price": self.price,
            "anchor": self.anchor,
            "offset_steps": self.offset_steps,
            "anchor_price": self.anchor_price,
            "label": self.label,
        }


@dataclass(frozen=True)
class RungView(Rung):
    """A rung plus what it would do against the current quote."""

    marketable: bool = False
    distance_from_last: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        d = super().to_dict()
        d["marketable"] = self.marketable
        d["distance_from_last"] = round(self.distance_from_last, 4)
        return d


def build_rungs(symbol: str, quote: Quote, settings: Settings) -> list[Rung]:
    """Every rung for one symbol, lowest price first.

    Ascending rather than the conventional high-at-top ladder: every rung at
    or above the ask is marketable and fills on the spot, so the rungs that
    actually rest and wait -- the ones being hunted for -- are the low ones.
    Those go first rather than at the end of a long list.

    Returns ``[]`` when the broker has not reported a session range yet — a
    ladder anchored on a zero high would place orders at absurd prices, so
    "no anchor" must mean "no rungs" rather than "anchor at zero".
    """
    if not quote.has_day_range:
        return []

    step = settings.ladder_step_for(symbol)
    tick = settings.ladder_tick_for(symbol)
    levels = settings.ladder_levels

    rungs: dict[float, Rung] = {}
    for anchor_name, anchor_price in (
        (ANCHOR_HIGH, quote.day_high),
        (ANCHOR_LOW, quote.day_low),
    ):
        # Snap the anchor onto the grid once, then step from there. Stepping
        # from a raw off-grid anchor and rounding each result independently
        # gives uneven spacing and duplicate prices.
        base = round_to_ladder_tick(anchor_price, tick)
        for n in range(-levels, levels + 1):
            price = round_to_ladder_tick(base + n * step, tick)
            if price <= 0:
                continue
            # The high and low ladders can overlap when the session range is
            # narrow. First writer wins, so a price keeps the anchor whose
            # rung is nearest it rather than being listed twice.
            existing = rungs.get(price)
            if existing is not None and abs(existing.offset_steps) <= abs(n):
                continue
            rungs[price] = Rung(
                symbol=symbol.upper(),
                price=price,
                anchor=anchor_name,
                offset_steps=n,
                anchor_price=round(anchor_price, 4),
            )

    return sorted(rungs.values(), key=lambda r: r.price)


def build_rung_views(
    symbol: str, quote: Quote, settings: Settings
) -> list[RungView]:
    """Rungs annotated with how each would behave against this quote."""
    reference = quote.ask if quote.ask > 0 else quote.last
    out: list[RungView] = []
    for r in build_rungs(symbol, quote, settings):
        out.append(
            RungView(
                symbol=r.symbol,
                price=r.price,
                anchor=r.anchor,
                offset_steps=r.offset_steps,
                anchor_price=r.anchor_price,
                # A buy limit at or above the ask crosses the spread and fills
                # now; below it, the order rests.
                marketable=bool(reference > 0 and r.price >= reference),
                distance_from_last=(r.price - quote.last) if quote.last else 0.0,
            )
        )
    return out


def rung_key(symbol: str, price: float) -> str:
    """Identity of one rung, used for confirmation tokens and de-duplication.

    Binding the token to symbol *and* price means a confirmation issued for
    ``QQQ @ 612.30`` can never be spent on a different symbol or price.
    """
    return f"LADDER_BUY:{symbol.upper()}@{price:.4f}"


# --- results -----------------------------------------------------------------

#: What actually happened to the broker's working order.
ACTION_PLACED = "PLACED"
ACTION_MODIFIED = "MODIFIED"
ACTION_RESIZED = "RESIZED"      # cancel + resubmit smaller to stay in budget
ACTION_UNCHANGED = "UNCHANGED"  # already working at this exact price
ACTION_NONE = "NONE"


@dataclass
class RungOrderResult:
    """Outcome of one rung click against one account."""

    broker: str
    ok: bool
    mode: TradingMode
    symbol: str
    limit_price: float
    action: str = ACTION_NONE
    quantity: int = 0
    order: dict[str, Any] | None = None
    previous_order_id: str | None = None
    previous_limit_price: float | None = None
    #: Broker-confirmed open orders *after* the action. The dashboard renders
    #: these; it never infers a working order from the fact we just sent one.
    open_orders: list[dict[str, Any]] = field(default_factory=list)
    #: Broker-confirmed position at the time of the action. A newly placed
    #: order does not move this, and is not supposed to.
    position_quantity: float = 0.0
    marketable: bool = False
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    needs_attention: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "broker": self.broker,
            "ok": self.ok,
            "mode": self.mode.value,
            "symbol": self.symbol,
            "limit_price": self.limit_price,
            "action": self.action,
            "quantity": self.quantity,
            "order": self.order,
            "previous_order_id": self.previous_order_id,
            "previous_limit_price": self.previous_limit_price,
            "open_orders": self.open_orders,
            "position_quantity": self.position_quantity,
            "marketable": self.marketable,
            "errors": self.errors,
            "warnings": self.warnings,
            "needs_attention": self.needs_attention,
        }


@dataclass
class LadderOrderResult:
    """Outcome of one rung click across every tradable account."""

    command_id: str
    symbol: str
    limit_price: float
    mode: TradingMode
    results: list[RungOrderResult] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0

    @property
    def all_ok(self) -> bool:
        return bool(self.results) and all(r.ok for r in self.results)

    @property
    def any_ok(self) -> bool:
        return any(r.ok for r in self.results)

    @property
    def status(self) -> str:
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
            "symbol": self.symbol,
            "limit_price": self.limit_price,
            "mode": self.mode.value,
            "status": self.status,
            "results": [r.to_dict() for r in self.results],
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


# --- execution ---------------------------------------------------------------


class LadderExecutor:
    """Places or re-prices one resting BUY limit order against one account.

    Unlike :class:`~trading.strategy.Strategy` this does **not** wait for a
    fill and does **not** cancel unrelated working orders. A ladder order is
    meant to sit at its price until the market reaches it, and the whole point
    of the feature is that several of them can be working at once.
    """

    def __init__(
        self, settings: Settings, audit_log: AuditLog, command_id: str
    ) -> None:
        self.settings = settings
        self.audit = audit_log
        self.command_id = command_id

    def _log(self, event: str, broker: str, **payload: Any) -> None:
        self.audit.record(
            event, command_id=self.command_id, broker=broker, **payload
        )

    # -- helpers ----------------------------------------------------------

    def _quote(self, broker: Broker, symbol: str, result: RungOrderResult) -> Quote:
        """Fetch and vet the quote used for the guards.

        A wide spread is downgraded to a warning here, unlike in the
        marketable-limit path: we are placing an order at a price *we* chose,
        so a wide spread does not expose us to a bad fill the way crossing it
        would. Halted, stale and malformed quotes still hard-fail.
        """
        quote = broker.get_quote(symbol)
        try:
            validate_quote(quote, self.settings)
        except WideSpreadError as exc:
            result.warnings.append(f"wide spread accepted for a resting order: {exc}")
        except QuoteError as exc:
            self._log(
                A.QUOTE_REJECTED, broker.name, symbol=symbol,
                quote=quote.to_dict(), reason=str(exc),
                error_type=type(exc).__name__,
            )
            raise
        self._log(A.QUOTE, broker.name, symbol=symbol, quote=quote.to_dict())
        return quote

    def _check_deviation(self, symbol: str, price: float, quote: Quote) -> None:
        """Refuse a price absurdly far from the market."""
        reference = quote.last or quote.mid
        if reference <= 0:
            return
        deviation = abs(price - reference) / reference * 100.0
        if deviation > self.settings.ladder_max_deviation_pct:
            raise OrderValidationError(
                f"{symbol} rung price ${price:,.4f} is {deviation:.1f}% away "
                f"from the last price ${reference:,.4f}, beyond "
                f"LADDER_MAX_DEVIATION_PCT={self.settings.ladder_max_deviation_pct:g}%. "
                "Refusing as a fat-finger guard; refresh the ladder and retry."
            )

    def _open_buys(self, broker: Broker, symbol: str) -> list[Order]:
        """Working BUY orders for this symbol, oldest first.

        Sell orders are left strictly alone: the ladder only ever adjusts the
        buy side, and re-pricing a working exit would be a surprise.
        """
        return [
            o
            for o in broker.get_open_orders_for({symbol})
            if o.side is Side.BUY and o.state.is_open
        ]

    def _refresh(self, broker: Broker, symbol: str, result: RungOrderResult) -> None:
        """Re-read broker truth after acting, for the dashboard to render."""
        try:
            result.open_orders = [
                o.to_dict() for o in broker.get_open_orders_for({symbol})
            ]
        except Exception as exc:
            result.warnings.append(f"could not refresh open orders: {exc}")
        try:
            position = broker.get_position(symbol)
            result.position_quantity = position.quantity if position else 0.0
        except Exception as exc:
            result.warnings.append(f"could not refresh position: {exc}")

    # -- entry point -------------------------------------------------------

    def place(self, broker: Broker, symbol: str, price: float) -> RungOrderResult:
        symbol = symbol.upper()
        result = RungOrderResult(
            broker=broker.name,
            ok=False,
            mode=self.settings.mode,
            symbol=symbol,
            limit_price=price,
        )
        try:
            check_market_hours(self.settings)
            if not broker.connected:
                broker.connect()

            quote = self._quote(broker, symbol, result)
            self._check_deviation(symbol, price, quote)
            reference = quote.ask if quote.ask > 0 else quote.last
            result.marketable = bool(reference > 0 and price >= reference)

            balances = broker.get_available_cash()
            position = broker.get_position(symbol)
            result.position_quantity = position.quantity if position else 0.0

            working = self._open_buys(broker, symbol)
            if working:
                self._modify(broker, symbol, price, working, balances, result)
            else:
                self._place_new(broker, symbol, price, balances, result)

            result.ok = not result.errors

        except TradingError as exc:
            result.errors.append(f"{type(exc).__name__}: {exc}")
            result.needs_attention = bool(getattr(exc, "position_at_risk", False))
            self._log(
                A.ERROR, broker.name, message=str(exc),
                error_type=type(exc).__name__, symbol=symbol, limit_price=price,
            )
        except Exception as exc:
            log.exception("%s: ladder order failed", broker.name)
            result.errors.append(f"{type(exc).__name__}: {exc}")
            result.needs_attention = True
            self._log(
                A.ERROR, broker.name, message=str(exc), unexpected=True,
                symbol=symbol, limit_price=price,
            )
        finally:
            # Always report broker-confirmed state, including after a failure:
            # the operator needs to know what is actually working right now.
            self._refresh(broker, symbol, result)

        self._log(
            A.LADDER_ORDER, broker.name,
            symbol=symbol, limit_price=price, result=result.to_dict(),
        )
        return result

    # -- new order ---------------------------------------------------------

    def _place_new(
        self,
        broker: Broker,
        symbol: str,
        price: float,
        balances: Balances,
        result: RungOrderResult,
    ) -> None:
        """No order working: size against the account and submit one."""
        sizing = calculate_quantity(balances, price, self.settings)
        self._log(
            A.SIZING, broker.name, symbol=symbol,
            sizing=sizing.to_dict(), limit_price=price,
        )

        if sizing.quantity < self.settings.min_order_shares:
            result.errors.append(
                f"insufficient funds: ${balances.cash_available:,.2f} available, "
                f"${self.settings.cash_buffer_dollars:,.2f} buffer, {symbol} "
                f"limit ${price:,.4f} -> {sizing.quantity} shares"
            )
            return

        record = validate_order(
            symbol=symbol,
            side=Side.BUY,
            quantity=sizing.quantity,
            limit_price=price,
            account_id=broker.account.account_id if broker.account else None,
            settings=self.settings,
            balances=balances,
        )
        self._log(A.ORDER_VALIDATED, broker.name, validation=record, ladder=True)

        order = broker.submit_limit_order(symbol, Side.BUY, sizing.quantity, price)
        result.order = order.to_dict()
        result.quantity = sizing.quantity
        result.action = ACTION_PLACED
        self._log(
            A.ORDER_SUBMITTED, broker.name, order=order.to_dict(),
            ladder=True, simulated=order.simulated,
        )

    # -- modify existing ---------------------------------------------------

    def _modify(
        self,
        broker: Broker,
        symbol: str,
        price: float,
        working: list[Order],
        balances: Balances,
        result: RungOrderResult,
    ) -> None:
        """An order is already working: re-price it rather than adding another.

        Only the price changes. The share count is never increased — that
        would turn a re-price into a bigger bet the operator did not ask for —
        and is reduced only when the higher price no longer fits the cash.
        """
        if len(working) > 1:
            result.warnings.append(
                f"{len(working)} working BUY orders for {symbol}; re-pricing "
                f"the oldest ({working[0].order_id}) and leaving the rest "
                "untouched"
            )
        order = working[0]
        result.previous_order_id = order.order_id
        result.previous_limit_price = order.limit_price

        tick = self.settings.ladder_tick_for(symbol)
        if abs(order.limit_price - price) < tick / 2:
            result.action = ACTION_UNCHANGED
            result.quantity = int(order.remaining_quantity)
            result.order = order.to_dict()
            result.warnings.append(
                f"order {order.order_id} is already working at "
                f"${order.limit_price:,.4f}; nothing to change"
            )
            return

        remaining = int(order.remaining_quantity)
        if remaining < self.settings.min_order_shares:
            result.errors.append(
                f"order {order.order_id} has only {remaining} share(s) "
                "unfilled; refusing to re-price it"
            )
            return

        affordable = self._affordable(order, price, balances)
        quantity = min(remaining, affordable)

        if quantity < self.settings.min_order_shares:
            result.errors.append(
                f"re-pricing {symbol} to ${price:,.4f} needs more cash than is "
                f"available (${balances.cash_available:,.2f} free, "
                f"{remaining} share(s) working at ${order.limit_price:,.4f}). "
                "The existing order was left untouched."
            )
            return

        record = validate_order(
            symbol=symbol,
            side=Side.BUY,
            quantity=quantity,
            limit_price=price,
            account_id=broker.account.account_id if broker.account else None,
            settings=self.settings,
            # The cash reserved by the order we are replacing is released when
            # it is replaced, so it must count as available for this check.
            balances=self._budget_balances(order, balances),
        )
        self._log(A.ORDER_VALIDATED, broker.name, validation=record, ladder=True)

        if quantity >= remaining:
            new_order = broker.replace_order(order.order_id, price)
            result.order = new_order.to_dict()
            result.quantity = quantity
            result.action = ACTION_MODIFIED
            self._log(
                A.ORDER_REPLACED, broker.name,
                previous_order_id=order.order_id, order=new_order.to_dict(),
                ladder=True, new_limit_price=price,
            )
            return

        # The full remainder no longer fits at the higher price, so the order
        # must shrink. A broker replace cannot reduce size reliably, so this
        # is an explicit cancel-then-submit, with the cancel confirmed before
        # anything new is sent.
        result.warnings.append(
            f"reduced {remaining} -> {quantity} share(s) to stay inside the "
            f"cash budget at ${price:,.4f}"
        )
        broker.cancel_order(order.order_id)
        closed = broker.wait_until_cancelled(
            order.order_id, self.settings.cancel_confirm_timeout_seconds
        )
        if not closed.state.is_terminal:
            result.needs_attention = True
            raise OrderTimeout(
                f"{broker.name}: cancel of {order.order_id} was not confirmed "
                f"within {self.settings.cancel_confirm_timeout_seconds}s; it "
                "may still be live. Nothing new was submitted."
            )
        self._log(
            A.ORDER_CANCELLED, broker.name, order_id=order.order_id,
            reason="re-pricing a ladder order that no longer fits the budget",
            order=closed.to_dict(),
        )

        new_order = broker.submit_limit_order(symbol, Side.BUY, quantity, price)
        result.order = new_order.to_dict()
        result.quantity = quantity
        result.action = ACTION_RESIZED
        self._log(
            A.ORDER_SUBMITTED, broker.name, order=new_order.to_dict(),
            ladder=True, resized=True, previous_order_id=order.order_id,
        )

    def _budget_balances(self, order: Order, balances: Balances) -> Balances:
        """Cash the re-priced order may draw on, once its own is released.

        Brokers disagree on whether a working order's cash is already excluded
        from reported availability -- IBKR reserves it, others report it as
        still free. Adding the released amount to the reported figure is right
        under the first regime and *double counts* under the second, which
        would over-size the replacement.

        So this takes ``max(reported, released)`` instead: exact where the
        broker does not reserve, and conservative where it does. Erring small
        only ever shrinks a replacement, which is the safe direction.
        """
        released = order.remaining_quantity * order.limit_price
        return Balances(
            cash_available=max(balances.cash_available, released),
            buying_power=max(balances.buying_power, released),
            cash_balance=balances.cash_balance,
            settled_cash=balances.settled_cash,
            currency=balances.currency,
        )

    def _affordable(
        self, order: Order, price: float, balances: Balances
    ) -> int:
        """Shares purchasable at ``price`` once the old order releases its cash."""
        if price <= 0:
            return 0
        budget = self._budget_balances(order, balances)
        available = spendable_cash(budget, self.settings)
        usable = (
            available
            - self.settings.cash_buffer_dollars
            - self.settings.estimated_fee_fixed
        )
        if usable <= 0:
            return 0
        per_share = price + self.settings.estimated_fee_per_share
        return max(0, int(math.floor(usable / per_share)))
