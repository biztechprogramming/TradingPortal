"""The broker-independent strategy: GO LONG / GO SHORT / GO FLAT.

Nothing in this module knows what a Schwab account hash or an IBKR conid is.
It reconciles real broker state, decides what must be sold and bought, and
hands legs to :class:`~trading.execution.ExecutionEngine`.
"""

from __future__ import annotations

import logging
import time
from typing import Callable

from . import audit as A
from .audit import AuditLog
from .brokers.base import Broker
from .config import Settings
from .errors import TradingError
from .execution import ExecutionEngine, LegRequest, leg_from_exception
from .models import (
    Balances,
    BrokerResult,
    BrokerSnapshot,
    Command,
    Fill,
    Side,
    exposure_from_positions,
)
from .sizing import calculate_quantity
from .validation import check_market_hours

log = logging.getLogger("trading.strategy")


class Strategy:
    """Runs one command against one broker, start to finish."""

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
        self.engine = ExecutionEngine(
            settings, audit_log, command_id, sleep=sleep, clock=clock
        )

    # --- entry point ------------------------------------------------------

    def run(self, broker: Broker, command: Command) -> BrokerResult:
        result = BrokerResult(
            broker=broker.name, command=command, ok=False, mode=self.settings.mode
        )
        try:
            check_market_hours(self.settings)

            if not broker.connected:
                broker.connect()

            snapshot = self.reconcile(broker)
            result.exposure_before = snapshot.exposure
            result.cash_before = (
                snapshot.balances.cash_available if snapshot.balances else 0.0
            )

            cancelled = self.engine.cancel_conflicting_orders(
                broker, set(self.settings.symbols)
            )
            if cancelled:
                result.warnings.append(
                    f"cancelled {len(cancelled)} conflicting open order(s): "
                    f"{cancelled}"
                )

            if command is Command.GO_FLAT:
                self._go_flat(broker, result)
            else:
                self._go_directional(broker, command, result)

            final = self.reconcile(broker, label="post-command")
            result.exposure_after = final.exposure
            result.cash_after = (
                final.balances.cash_available if final.balances else 0.0
            )
            result.ok = not result.errors

        except TradingError as exc:
            self._record_failure(broker, result, exc)
        except Exception as exc:  # never let an adapter bug look like success
            log.exception("%s: unexpected failure during %s", broker.name, command)
            self._record_failure(broker, result, exc, unexpected=True)
        finally:
            result.finished_at = time.time()

        return result

    def _record_failure(
        self,
        broker: Broker,
        result: BrokerResult,
        exc: BaseException,
        unexpected: bool = False,
    ) -> None:
        message = f"{type(exc).__name__}: {exc}"
        result.errors.append(message)
        result.ok = False

        partial = leg_from_exception(exc)
        if partial is not None and partial.filled_quantity > 0:
            result.fills.append(partial)
            result.needs_attention = True
            result.errors.append(
                f"PARTIAL EXECUTION: {partial.filled_quantity:g} of "
                f"{partial.requested_quantity:g} {partial.symbol} "
                f"{partial.side.value} filled at {partial.average_price:.4f}. "
                "The account is in an intermediate state."
            )

        if getattr(exc, "position_at_risk", False):
            result.needs_attention = True

        self.audit.record(
            A.ERROR,
            command_id=self.command_id,
            broker=broker.name,
            message=message,
            unexpected=unexpected,
            needs_attention=result.needs_attention,
        )

        # Always try to leave a truthful picture of where the account ended up.
        try:
            final = self.reconcile(broker, label="post-failure")
            result.exposure_after = final.exposure
            result.cash_after = (
                final.balances.cash_available if final.balances else 0.0
            )
        except Exception as reconcile_exc:
            result.errors.append(
                f"could not reconcile after failure: {reconcile_exc}"
            )
            result.needs_attention = True

    # --- reconciliation ---------------------------------------------------

    def reconcile(self, broker: Broker, label: str = "pre-command") -> BrokerSnapshot:
        """Read live broker state. Never trust what the last command did."""
        positions = broker.get_positions()
        balances = broker.get_available_cash()
        open_orders = broker.get_open_orders_for(set(self.settings.symbols))
        exposure = exposure_from_positions(
            positions, self.settings.long_symbol, self.settings.short_symbol
        )
        snapshot = BrokerSnapshot(
            broker=broker.name,
            connected=broker.connected,
            mode=self.settings.mode,
            account=broker.account,
            positions=positions,
            balances=balances,
            open_orders=open_orders,
            exposure=exposure,
        )
        self.audit.record(
            A.RECONCILE,
            command_id=self.command_id,
            broker=broker.name,
            label=label,
            exposure=exposure.value,
            positions=[p.to_dict() for p in positions],
            balances=balances.to_dict(),
            open_orders=[o.to_dict() for o in open_orders],
        )
        return snapshot

    # --- GO FLAT ----------------------------------------------------------

    def _go_flat(self, broker: Broker, result: BrokerResult) -> None:
        """Sell everything we hold in either symbol. Buy nothing."""
        sold_any = False
        for symbol in (self.settings.long_symbol, self.settings.short_symbol):
            fill = self._liquidate(broker, symbol, result)
            if fill is not None:
                sold_any = True
        if not sold_any:
            result.warnings.append("already flat; nothing to sell")

    def _liquidate(
        self, broker: Broker, symbol: str, result: BrokerResult
    ) -> Fill | None:
        """Sell the entire holding of ``symbol``, confirming the fill."""
        position = broker.get_position(symbol)
        if position is None or position.quantity <= 0:
            return None

        quantity = int(position.quantity)
        if quantity <= 0:
            result.warnings.append(
                f"{symbol} position of {position.quantity:g} is not a whole "
                "share count; skipping"
            )
            return None

        # Some brokers report unsettled shares separately; never try to sell
        # more than is confirmed sellable.
        if position.settled_quantity is not None:
            sellable = int(min(quantity, position.settled_quantity))
            if sellable < quantity:
                result.warnings.append(
                    f"{symbol}: only {sellable} of {quantity} shares are "
                    "settled/sellable; selling what is available"
                )
                quantity = sellable
        if quantity <= 0:
            return None

        fill = self.engine.execute_leg(
            broker,
            LegRequest(
                symbol=symbol,
                side=Side.SELL,
                quantity=quantity,
                position_quantity=position.quantity,
            ),
        )
        result.fills.append(fill)
        return fill

    # --- GO LONG / GO SHORT -----------------------------------------------

    def _go_directional(
        self, broker: Broker, command: Command, result: BrokerResult
    ) -> None:
        if command is Command.GO_LONG:
            target, opposite = self.settings.long_symbol, self.settings.short_symbol
        else:
            target, opposite = self.settings.short_symbol, self.settings.long_symbol

        # 1. Exit the opposite side first and confirm it is gone.
        opposite_fill = self._liquidate(broker, opposite, result)
        if opposite_fill is not None:
            remaining = broker.get_position(opposite)
            if remaining is not None and remaining.quantity > 0:
                raise TradingError(
                    f"{broker.name}: {opposite} still shows "
                    f"{remaining.quantity:g} shares after the sell filled; "
                    "refusing to buy the other side while both are held"
                )

        # 2. Find out what cash is genuinely available now.
        balances = self._settled_balances(
            broker,
            result,
            expect_proceeds=(opposite_fill.notional if opposite_fill else 0.0),
            baseline_cash=result.cash_before,
        )

        # 3. Size the entry.
        existing = broker.get_position(target)
        existing_qty = existing.quantity if existing else 0.0

        quote = self.engine.fresh_quote(broker, target)
        from .pricing import compute_limit_price

        decision = compute_limit_price(quote, Side.BUY, self.settings)
        sizing = calculate_quantity(balances, decision.limit_price, self.settings)
        self.audit.record(
            A.SIZING,
            command_id=self.command_id,
            broker=broker.name,
            symbol=target,
            sizing=sizing.to_dict(),
            price_decision=decision.to_dict(),
            existing_position=existing_qty,
        )

        if existing_qty > 0 and sizing.quantity < self.settings.min_order_shares:
            result.warnings.append(
                f"already holding {existing_qty:g} {target}; remaining cash "
                f"${sizing.usable_cash:,.2f} buys no further whole shares, so "
                "no order was placed"
            )
            return

        if sizing.quantity < self.settings.min_order_shares:
            result.errors.append(
                f"insufficient funds: ${balances.cash_available:,.2f} available, "
                f"${self.settings.cash_buffer_dollars:,.2f} buffer, "
                f"{target} limit ${decision.limit_price:,.2f} -> "
                f"{sizing.quantity} shares"
            )
            return

        if existing_qty > 0:
            result.warnings.append(
                f"already holding {existing_qty:g} {target}; keeping it and "
                f"adding {sizing.quantity} share(s) with the remaining cash"
            )

        # 4. Buy, bounded by exactly the money we said we would spend.
        budget = sizing.quantity * decision.limit_price
        fill = self.engine.execute_leg(
            broker,
            LegRequest(
                symbol=target,
                side=Side.BUY,
                quantity=sizing.quantity,
                budget=budget,
                balances=balances,
            ),
        )
        result.fills.append(fill)

    def _settled_balances(
        self,
        broker: Broker,
        result: BrokerResult,
        expect_proceeds: float = 0.0,
        baseline_cash: float = 0.0,
    ) -> Balances:
        """Re-read cash after a sale, giving the broker a moment to credit it.

        We never assume proceeds are available; we poll until the broker says
        so or the wait expires, then size against whatever it actually reports.
        ``baseline_cash`` is the cash reported *before* the sale, so the
        comparison measures the credit rather than comparing a number to
        itself.
        """
        balances = broker.get_available_cash()
        if expect_proceeds <= 0:
            return balances

        target = baseline_cash + expect_proceeds * 0.95
        deadline = time.monotonic() + self.settings.settlement_wait_seconds
        while balances.cash_available < target and time.monotonic() < deadline:
            self._sleep(min(1.0, max(0.1, self.settings.settlement_wait_seconds / 5)))
            balances = broker.get_available_cash()

        credited = balances.cash_available - baseline_cash
        if credited < expect_proceeds * 0.95:
            result.warnings.append(
                f"only ${max(0.0, credited):,.2f} of ${expect_proceeds:,.2f} in "
                "sale proceeds is showing as available for trading (cash "
                "accounts settle T+1). Sizing against reported availability "
                "only."
            )
        return balances
