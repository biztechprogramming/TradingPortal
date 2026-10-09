"""Order execution: submit, monitor, reprice, handle partial fills.

One *leg* is one logical buy or sell. A leg may span several broker order
ids, because repricing is implemented as cancel/replace and most brokers
issue a new id each time. The engine tracks cumulative fills across all of
them so a partially-filled-then-replaced order is accounted for exactly once.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from . import audit as A
from .audit import AuditLog
from .brokers.base import Broker
from .config import Settings
from .errors import (
    MaxSlippageExceeded,
    OrderRejected,
    OrderTimeout,
    QuoteError,
    TradingError,
)
from .models import Balances, Fill, Order, OrderState, Quote, Side
from .pricing import compute_limit_price, should_reprice, validate_quote
from .sizing import lot_floor
from .validation import validate_order

log = logging.getLogger("trading.execution")


@dataclass
class LegRequest:
    symbol: str
    side: Side
    quantity: int
    #: For buys: the maximum dollars this leg may spend in total. Repricing
    #: upward can only shrink the order, never breach this number.
    budget: float | None = None
    #: Position size available to sell, used by pre-submit validation.
    position_quantity: float | None = None
    balances: Balances | None = None


@dataclass
class _LegState:
    """Cumulative accounting across every order id used by this leg."""

    requested: int
    filled_by_order: dict[str, tuple[float, float]] = field(default_factory=dict)
    order_ids: list[str] = field(default_factory=list)
    reprices: int = 0
    notes: list[str] = field(default_factory=list)

    def record(self, order: Order) -> None:
        if not order.order_id:
            return
        if order.order_id not in self.filled_by_order:
            self.order_ids.append(order.order_id)
        self.filled_by_order[order.order_id] = (
            order.filled_quantity,
            order.average_fill_price,
        )

    @property
    def filled(self) -> float:
        return sum(q for q, _ in self.filled_by_order.values())

    @property
    def notional(self) -> float:
        return sum(q * p for q, p in self.filled_by_order.values())

    @property
    def average_price(self) -> float:
        f = self.filled
        return self.notional / f if f > 0 else 0.0

    @property
    def remaining(self) -> int:
        return max(0, int(round(self.requested - self.filled)))


class ExecutionEngine:
    """Executes one leg at a time against one broker."""

    def __init__(
        self,
        settings: Settings,
        audit_log: AuditLog,
        command_id: str,
        *,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.settings = settings
        self.audit = audit_log
        self.command_id = command_id
        self._sleep = sleep
        self._clock = clock

    # --- helpers ----------------------------------------------------------

    def _log(self, event: str, broker: str, **payload) -> None:
        self.audit.record(
            event, command_id=self.command_id, broker=broker, **payload
        )

    def fresh_quote(self, broker: Broker, symbol: str) -> Quote:
        """Fetch and validate a quote, auditing both outcomes."""
        quote = broker.get_quote(symbol)
        try:
            validate_quote(quote, self.settings)
        except QuoteError as exc:
            self._log(
                A.QUOTE_REJECTED,
                broker.name,
                symbol=symbol,
                quote=quote.to_dict(),
                reason=str(exc),
                error_type=type(exc).__name__,
            )
            raise
        self._log(A.QUOTE, broker.name, symbol=symbol, quote=quote.to_dict())
        return quote

    def cancel_conflicting_orders(
        self, broker: Broker, symbols: set[str]
    ) -> list[str]:
        """Cancel every open order in our symbols and confirm each cancel.

        A working order we did not place can consume the cash we are about to
        commit, so this runs before any sizing.
        """
        cancelled: list[str] = []
        for order in broker.get_open_orders_for(symbols):
            self._log(
                A.ORDER_CANCELLED,
                broker.name,
                order_id=order.order_id,
                symbol=order.symbol,
                reason="conflicting order present before command",
                order=order.to_dict(),
            )
            broker.cancel_order(order.order_id)
            final = broker.wait_until_cancelled(
                order.order_id, self.settings.cancel_confirm_timeout_seconds
            )
            if not final.state.is_terminal:
                raise OrderTimeout(
                    f"{broker.name}: cancel of order {order.order_id} "
                    f"({order.symbol}) was not confirmed within "
                    f"{self.settings.cancel_confirm_timeout_seconds}s; it may "
                    "still be live. Refusing to trade against an unknown "
                    "outstanding order."
                )
            cancelled.append(order.order_id)
        return cancelled

    # --- the main loop ----------------------------------------------------

    def execute_leg(self, broker: Broker, req: LegRequest) -> Fill:
        """Submit and manage one buy or sell until it fills, times out, or aborts."""
        quote = self.fresh_quote(broker, req.symbol)
        anchor = quote.ask if req.side is Side.BUY else quote.bid

        decision = compute_limit_price(quote, req.side, self.settings, anchor_price=anchor)
        state = _LegState(requested=int(req.quantity))

        record = validate_order(
            symbol=req.symbol,
            side=req.side,
            quantity=req.quantity,
            limit_price=decision.limit_price,
            account_id=broker.account.account_id if broker.account else None,
            settings=self.settings,
            balances=req.balances,
            position_quantity=req.position_quantity,
        )
        self._log(
            A.ORDER_VALIDATED,
            broker.name,
            validation=record,
            price_decision=decision.to_dict(),
        )

        order = broker.submit_limit_order(
            req.symbol, req.side, int(req.quantity), decision.limit_price
        )
        state.record(order)
        self._log(
            A.ORDER_SUBMITTED,
            broker.name,
            order=order.to_dict(),
            price_decision=decision.to_dict(),
            simulated=order.simulated,
        )

        deadline = self._clock() + self.settings.order_fill_timeout_seconds
        next_reprice = self._clock() + self.settings.reprice_interval_seconds
        current_limit = decision.limit_price
        abort_error: TradingError | None = None
        last_filled = 0.0

        while True:
            order = broker.get_order_status(order.order_id)
            state.record(order)

            if state.filled > last_filled:
                self._log(
                    A.ORDER_PARTIAL if not order.state.is_terminal else A.ORDER_STATUS,
                    broker.name,
                    order=order.to_dict(),
                    cumulative_filled=state.filled,
                    cumulative_average=round(state.average_price, 4),
                )
                last_filled = state.filled

            if order.state is OrderState.FILLED and state.remaining == 0:
                break

            if order.state is OrderState.REJECTED:
                self._log(
                    A.ORDER_REJECTED_EVT,
                    broker.name,
                    order=order.to_dict(),
                    message=order.status_message,
                )
                abort_error = OrderRejected(
                    f"{broker.name}: {req.side.value} {req.symbol} rejected"
                    + (f": {order.status_message}" if order.status_message else "")
                )
                break

            if order.state in (OrderState.CANCELLED, OrderState.EXPIRED):
                # Someone or something ended our order. If there is still
                # work to do and time on the clock, resubmit the remainder;
                # otherwise stop and report what we got.
                if state.remaining <= 0 or self._clock() >= deadline:
                    break
                state.notes.append(
                    f"order {order.order_id} ended as {order.state.value}; "
                    "resubmitting the remainder"
                )
                try:
                    order, current_limit = self._resubmit_remainder(
                        broker, req, state, anchor, current_limit
                    )
                except (MaxSlippageExceeded, QuoteError, TradingError) as exc:
                    abort_error = exc
                    break
                continue

            now = self._clock()

            if now >= deadline:
                self._log(
                    A.WARNING,
                    broker.name,
                    message="order fill timeout reached",
                    order_id=order.order_id,
                    timeout_seconds=self.settings.order_fill_timeout_seconds,
                )
                abort_error = OrderTimeout(
                    f"{broker.name}: {req.side.value} {req.symbol} did not "
                    f"complete within {self.settings.order_fill_timeout_seconds}s "
                    f"({state.filled:g}/{state.requested} filled)"
                )
                break

            if now >= next_reprice and state.reprices < self.settings.max_reprice_attempts:
                next_reprice = now + self.settings.reprice_interval_seconds
                try:
                    order, current_limit = self._maybe_reprice(
                        broker, req, state, order, anchor, current_limit
                    )
                except MaxSlippageExceeded as exc:
                    abort_error = exc
                    break
                except QuoteError as exc:
                    # A bad quote mid-flight is not a reason to abandon a
                    # working order; keep it and try again next interval.
                    state.notes.append(f"reprice skipped: {exc}")
                    self._log(
                        A.WARNING, broker.name, message="reprice skipped",
                        reason=str(exc),
                    )

            self._sleep(self.settings.order_poll_interval_seconds)

        # Anything still working must be cancelled before we return, or we
        # would leave an order live that the next command knows nothing about.
        final_order = self._ensure_closed(broker, order, state)
        state.record(final_order)

        fill = Fill(
            symbol=req.symbol,
            side=req.side,
            requested_quantity=state.requested,
            filled_quantity=state.filled,
            average_price=round(state.average_price, 6),
            order_ids=list(state.order_ids),
            complete=state.remaining == 0 and state.filled > 0,
            reprice_count=state.reprices,
            notes=state.notes,
        )
        self._log(
            A.LEG_COMPLETE if fill.complete else A.ORDER_PARTIAL,
            broker.name,
            fill=fill.to_dict(),
            aborted=str(abort_error) if abort_error else None,
        )

        if abort_error is not None:
            abort_error.fill = fill  # type: ignore[attr-defined]
            raise abort_error

        if not fill.complete:
            err = OrderTimeout(
                f"{broker.name}: {req.side.value} {req.symbol} filled "
                f"{state.filled:g} of {state.requested}"
            )
            err.fill = fill  # type: ignore[attr-defined]
            raise err

        self._log(A.ORDER_FILLED, broker.name, fill=fill.to_dict())
        return fill

    # --- repricing --------------------------------------------------------

    def _maybe_reprice(
        self,
        broker: Broker,
        req: LegRequest,
        state: _LegState,
        order: Order,
        anchor: float,
        current_limit: float,
    ) -> tuple[Order, float]:
        quote = self.fresh_quote(broker, req.symbol)
        decision = compute_limit_price(
            quote,
            req.side,
            self.settings,
            anchor_price=anchor,
            attempt=state.reprices + 1,
        )
        if not should_reprice(current_limit, decision, self.settings.tick_size):
            return order, current_limit

        remaining = state.remaining
        if remaining <= 0:
            return order, current_limit

        affordable = self._affordable_quantity(req, state, decision.limit_price)

        state.reprices += 1
        if affordable >= remaining:
            new_order = broker.replace_order(order.order_id, decision.limit_price)
            self._log(
                A.ORDER_REPLACED,
                broker.name,
                previous_order_id=order.order_id,
                order=new_order.to_dict(),
                price_decision=decision.to_dict(),
                attempt=state.reprices,
            )
            state.record(new_order)
            return new_order, decision.limit_price

        # A higher limit no longer fits the budget for the full remainder, so
        # we cancel and resubmit smaller rather than risk a funds rejection.
        if affordable <= 0:
            state.notes.append(
                "budget exhausted at the new limit price; keeping the working "
                "order unchanged"
            )
            state.reprices -= 1
            return order, current_limit

        # The remainder no longer fits the budget at the higher price, so the
        # leg's target shrinks. This is a smaller-than-planned entry, not a
        # failure: reducing the request keeps the outcome reported as
        # complete-but-noted rather than as a spurious error.
        state.notes.append(
            f"reduced remaining quantity {remaining} -> {affordable} to stay "
            "inside the cash budget at the higher limit"
        )
        state.requested = int(round(state.filled)) + affordable
        broker.cancel_order(order.order_id)
        closed = broker.wait_until_cancelled(
            order.order_id, self.settings.cancel_confirm_timeout_seconds
        )
        state.record(closed)
        if not closed.state.is_terminal:
            raise OrderTimeout(
                f"{broker.name}: could not confirm cancel of {order.order_id} "
                "while repricing"
            )

        remaining = min(affordable, state.remaining)
        if remaining <= 0:
            return closed, current_limit

        new_order = broker.submit_limit_order(
            req.symbol, req.side, int(remaining), decision.limit_price
        )
        state.record(new_order)
        self._log(
            A.ORDER_REPLACED,
            broker.name,
            previous_order_id=order.order_id,
            order=new_order.to_dict(),
            price_decision=decision.to_dict(),
            attempt=state.reprices,
            resized=True,
        )
        return new_order, decision.limit_price

    def _resubmit_remainder(
        self,
        broker: Broker,
        req: LegRequest,
        state: _LegState,
        anchor: float,
        current_limit: float,
    ) -> tuple[Order, float]:
        quote = self.fresh_quote(broker, req.symbol)
        decision = compute_limit_price(
            quote, req.side, self.settings, anchor_price=anchor,
            attempt=state.reprices,
        )
        affordable = self._affordable_quantity(req, state, decision.limit_price)
        remaining = min(state.remaining, affordable)
        if remaining <= 0:
            raise OrderRejected(
                f"{broker.name}: cannot resubmit the remainder of "
                f"{req.symbol}; no budget left at {decision.limit_price:.2f}"
            )
        if remaining < state.remaining:
            state.notes.append(
                f"resubmitting {remaining} of {state.remaining} remaining "
                "shares to stay inside the cash budget"
            )
            state.requested = int(round(state.filled)) + remaining
        order = broker.submit_limit_order(
            req.symbol, req.side, int(remaining), decision.limit_price
        )
        state.record(order)
        self._log(
            A.ORDER_SUBMITTED,
            broker.name,
            order=order.to_dict(),
            price_decision=decision.to_dict(),
            resubmitted_remainder=True,
        )
        return order, decision.limit_price

    def _affordable_quantity(
        self, req: LegRequest, state: _LegState, limit_price: float
    ) -> int:
        """How many more shares the budget allows at ``limit_price``."""
        if req.side is Side.SELL or req.budget is None:
            return state.remaining
        spent = state.notional
        left = req.budget - spent
        if left <= 0 or limit_price <= 0:
            return 0
        # Lot-align what the budget affords, then take the smaller of that and
        # what is actually left. Aligning after the min() would instead round
        # away the tail of a partially-filled leg, which is the broker's share
        # count to finish, not ours to reshape.
        affordable = lot_floor(left // limit_price, self.settings)
        return min(state.remaining, affordable)

    def _ensure_closed(
        self, broker: Broker, order: Order, state: _LegState
    ) -> Order:
        """Guarantee no order from this leg is left working."""
        try:
            latest = broker.get_order_status(order.order_id)
        except Exception as exc:
            self._log(
                A.ERROR, broker.name,
                message="could not read final order status",
                order_id=order.order_id, error=str(exc),
            )
            return order

        if not latest.state.is_open:
            return latest

        try:
            broker.cancel_order(latest.order_id)
            latest = broker.wait_until_cancelled(
                latest.order_id, self.settings.cancel_confirm_timeout_seconds
            )
            self._log(
                A.ORDER_CANCELLED,
                broker.name,
                order_id=latest.order_id,
                reason="leg finished with the order still working",
                order=latest.to_dict(),
            )
        except Exception as exc:
            state.notes.append(
                f"WARNING: order {latest.order_id} may still be live: {exc}"
            )
            self._log(
                A.ERROR, broker.name,
                message="failed to cancel a still-working order",
                order_id=latest.order_id, error=str(exc),
            )
        return latest


def leg_from_exception(exc: BaseException) -> Fill | None:
    """Recover the partial :class:`Fill` an execution error carried, if any."""
    return getattr(exc, "fill", None)
