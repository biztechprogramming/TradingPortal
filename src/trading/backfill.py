"""Reconstruct account NAV history that predates this system.

The local ledger only knows what it has observed, which on day one is
nothing. To answer "how did this account do over the last 6 months?" we have
to rebuild history from what the brokers *can* tell us:

* **IBKR** publishes its own Portfolio Analyst NAV series. When available
  that is used directly — it is the broker's own arithmetic.
* **Schwab** has no performance endpoint, but does expose ~180 days of
  transactions and daily price history. Those are enough to roll the current
  portfolio backwards day by day and mark it to market.

Reconstruction is *approximate* by nature: it values positions at official
daily closes and assumes the transaction feed is complete. Every point it
produces is tagged ``reconstructed`` so the dashboard can say so, and the
README tells you to check it against your statements.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from .audit import AuditLog
from .brokers.base import Broker
from .config import Settings
from .models import Transaction
from .performance import (
    SOURCE_RECONSTRUCTED,
    NavLedger,
    NavPoint,
    months_ago,
    today_utc,
)

log = logging.getLogger("trading.backfill")


class PriceLookup:
    """Daily closes with carry-forward, so weekends do not create holes."""

    def __init__(self) -> None:
        self._by_symbol: dict[str, dict[str, float]] = {}
        self._sorted: dict[str, list[str]] = {}

    def add(self, symbol: str, closes: dict[str, float]) -> None:
        symbol = symbol.upper()
        self._by_symbol.setdefault(symbol, {}).update(closes)
        self._sorted[symbol] = sorted(self._by_symbol[symbol])

    def close_on(self, symbol: str, day: date) -> float | None:
        """The close for ``day``, or the most recent one before it."""
        symbol = symbol.upper()
        table = self._by_symbol.get(symbol)
        if not table:
            return None
        key = day.isoformat()
        direct = table.get(key)
        if direct is not None:
            return direct
        earlier = [d for d in self._sorted.get(symbol, []) if d <= key]
        return table[earlier[-1]] if earlier else None

    def trading_days(self) -> list[str]:
        """Union of every date any symbol traded on."""
        days: set[str] = set()
        for table in self._by_symbol.values():
            days.update(table)
        return sorted(days)

    @property
    def symbols(self) -> list[str]:
        return sorted(self._by_symbol)


def reconstruct_nav_series(
    *,
    current_positions: dict[str, float],
    current_cash: float,
    transactions: list[Transaction],
    prices: PriceLookup,
    start: date,
    end: date | None = None,
) -> list[NavPoint]:
    """Roll today's portfolio backwards to build a daily NAV series.

    Walking backwards is the only direction that works: we know exactly what
    the account holds *now*, and each transaction tells us what changed. Going
    forwards would require knowing the starting state, which is precisely what
    we lack.
    """
    end = end or today_utc()
    positions = {k.upper(): float(v) for k, v in current_positions.items()}
    cash = float(current_cash)

    by_date: dict[str, list[Transaction]] = {}
    for txn in transactions:
        by_date.setdefault(txn.date, []).append(txn)

    # Value the portfolio only on days the market actually traded, plus any
    # day with activity, so the series has no weekend plateaus.
    candidate_days = {d for d in prices.trading_days() if start.isoformat() <= d <= end.isoformat()}
    candidate_days.update(d for d in by_date if start.isoformat() <= d <= end.isoformat())
    candidate_days.add(end.isoformat())
    if not candidate_days:
        return []

    points: list[NavPoint] = []
    for day_str in sorted(candidate_days, reverse=True):
        day = date.fromisoformat(day_str)
        if day > end:
            continue

        positions_value = 0.0
        priced_everything = True
        for symbol, quantity in positions.items():
            if abs(quantity) < 1e-9:
                continue
            close = prices.close_on(symbol, day)
            if close is None:
                priced_everything = False
                continue
            positions_value += quantity * close

        todays = by_date.get(day_str, [])
        external = sum(t.external_flow for t in todays)

        if priced_everything or not positions:
            points.append(
                NavPoint(
                    date=day_str,
                    nav=cash + positions_value,
                    cash=cash,
                    positions_value=positions_value,
                    external_flow=external,
                    source=SOURCE_RECONSTRUCTED,
                )
            )

        # Undo the day's activity to arrive at the previous day's state.
        for txn in todays:
            if txn.symbol and abs(txn.quantity) > 1e-9:
                positions[txn.symbol.upper()] = (
                    positions.get(txn.symbol.upper(), 0.0) - txn.quantity
                )
            cash -= txn.net_amount

        for symbol in [s for s, q in positions.items() if abs(q) < 1e-9]:
            positions.pop(symbol, None)

    points.reverse()
    return points


class Backfiller:
    """Fills the ledger's blind spot, preferring broker-supplied history."""

    def __init__(
        self, settings: Settings, ledger: NavLedger, audit: AuditLog | None = None
    ) -> None:
        self.settings = settings
        self.ledger = ledger
        self.audit = audit
        self._attempted: set[str] = set()

    def backfill(self, broker: Broker, months: int = 6, force: bool = False) -> int:
        """Populate history for one account. Returns points written.

        Runs at most once per broker per process unless forced: IBKR rate
        limits its performance endpoint to roughly one call per 15 minutes,
        and Schwab reconstruction costs a transaction call plus one price
        call per held symbol.
        """
        if not force and broker.name in self._attempted:
            return 0
        self._attempted.add(broker.name)

        start = months_ago(today_utc(), months)
        earliest = self.ledger.earliest(broker.name)
        if earliest is not None and earliest <= start and not force:
            return 0            # the ledger already reaches far enough back

        try:
            points = self._from_broker(broker, months)
            if points is None:
                points = self._reconstruct(broker, start)
        except Exception as exc:
            log.warning("%s: backfill failed: %s", broker.name, exc)
            self._record(broker, 0, "failed", str(exc))
            return 0

        if not points:
            self._record(broker, 0, "no data", None)
            return 0

        written = self.ledger.fill_gaps(broker.name, points)
        log.info(
            "%s: backfilled %d NAV point(s) from %s",
            broker.name, written, points[0].source if points else "?",
        )
        self._record(broker, written, points[0].source if points else "?", None)
        return written

    def _from_broker(self, broker: Broker, months: int) -> list[NavPoint] | None:
        try:
            return broker.get_broker_performance(months)
        except Exception as exc:
            log.info("%s: broker performance unavailable (%s)", broker.name, exc)
            return None

    def _reconstruct(self, broker: Broker, start: date) -> list[NavPoint]:
        end = today_utc()
        transactions = broker.get_transactions(start, end)
        if transactions is None:
            log.info(
                "%s: no transaction history available; performance will build "
                "up from today's daily snapshots", broker.name,
            )
            return []

        positions = {p.symbol: p.quantity for p in broker.get_positions()}
        balances = broker.get_available_cash()

        # Every symbol we hold now, plus everything traded in the window,
        # since a position may have been closed part-way through.
        symbols = set(positions)
        symbols.update(t.symbol.upper() for t in transactions if t.symbol)

        prices = PriceLookup()
        for symbol in sorted(symbols):
            history = broker.get_price_history(symbol, start - timedelta(days=7), end)
            if history:
                prices.add(symbol, history)
            else:
                log.info("%s: no price history for %s", broker.name, symbol)

        return reconstruct_nav_series(
            current_positions=positions,
            current_cash=balances.cash_available,
            transactions=transactions,
            prices=prices,
            start=start,
            end=end,
        )

    def _record(
        self, broker: Broker, written: int, source: str, error: str | None
    ) -> None:
        if self.audit is None:
            return
        self.audit.record(
            "performance.backfill",
            broker=broker.name,
            points_written=written,
            source=source,
            error=error,
        )


def summarise_sources(points: list[NavPoint]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for point in points:
        counts[point.source] = counts.get(point.source, 0) + 1
    return counts
