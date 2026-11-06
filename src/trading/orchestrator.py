"""Multi-broker coordination: locking, confirmation, dispatch, aggregation.

This is the layer the CLI and the web API both call. It owns every safety
gate that applies to a *command* as opposed to an individual order.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

from . import audit as A
from .audit import AuditLog, new_command_id, setup_logging
from .backfill import Backfiller
from .candles import CandleService, CandleStore
from .brokers import build_brokers, verify_environment
from .brokers.base import Broker
from .config import Settings
from .performance import (
    DEFAULT_WINDOWS,
    BrokerPerformance,
    NavLedger,
    NavPoint,
    combine_series,
    compute_windows,
    nav_from_snapshot,
    today_utc,
    trim_series,
)
from .errors import (
    ConfirmationRequired,
    ExecutionLocked,
    OrderValidationError,
    TradingError,
)
from .ladder import (
    ANCHOR_NAMES,
    LadderExecutor,
    LadderOrderResult,
    build_rung_views,
    rung_key,
)
from .locks import ExecutionLock
from .session import bar_seconds, compute_session_range
from .models import (
    BrokerResult,
    Quote,
    BrokerSnapshot,
    Command,
    CommandResult,
    Exposure,
    TradingMode,
    exposure_from_positions,
)
from .state import StateStore
from .strategy import Strategy
from .validation import market_status

log = logging.getLogger("trading.orchestrator")


class TradingService:
    def __init__(self, settings: Settings, brokers: dict[str, Broker] | None = None):
        self.settings = settings
        setup_logging(settings)
        settings.state_dir.mkdir(parents=True, exist_ok=True)
        self.audit = AuditLog(settings.log_dir)
        self.state = StateStore(settings.state_dir / "state.json")
        self.lock = ExecutionLock(settings.state_dir / "execution.lock")
        #: Injected brokers (tests) are kept as given; built ones can be
        #: rebuilt after a login, when accounts may have appeared.
        self._rebuildable = brokers is None
        self.brokers: dict[str, Broker] = brokers or build_brokers(settings)
        self.environment_warnings: list[str] = []
        self._connect_lock = threading.Lock()
        self._connected_once = False
        self.nav = NavLedger(settings.state_dir / "nav_history.jsonl")
        self.candle_store = CandleStore(settings.state_dir / "candles.db")
        self.candles = CandleService(settings, self.candle_store)
        #: Last fine-series refresh per symbol. The dashboard polls the ladder
        #: every few seconds; without this each poll would make a broker round
        #: trip per symbol just to re-read minute bars that have not changed.
        self._session_refreshed: dict[str, float] = {}
        #: Short-lived quote memo for the *display* paths only. See
        #: :meth:`display_quote`.
        self._quote_memo: dict[str, tuple[float, Any, str | None]] = {}
        self._quote_memo_lock = threading.Lock()
        #: account name -> (fetched_at, fills by symbol or None if unsupported)
        self._fills_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
        self.backfiller = Backfiller(settings, self.nav, self.audit)
        self._nav_recorded: dict[str, str] = {}

    # --- connection -------------------------------------------------------

    def connect_all(self) -> dict[str, str | None]:
        """Connect every broker. Returns per-broker error strings (or None)."""
        errors: dict[str, str | None] = {}
        with self._connect_lock:
            self.environment_warnings = []
            for name, broker in self.brokers.items():
                try:
                    broker.connect()
                    self.environment_warnings.extend(
                        verify_environment(broker, self.settings)
                    )
                    errors[name] = None
                except Exception as exc:
                    errors[name] = str(exc)
                    log.warning("%s failed to connect: %s", name, exc)
            self._connected_once = True
        for warning in self.environment_warnings:
            log.warning("%s", warning)
        return errors

    def rebuild_brokers(self) -> dict[str, str | None]:
        """Re-discover accounts and reconnect, e.g. after a fresh login.

        A broker that was down at startup is represented by a single fallback
        instance, and a new Schwab authorisation may cover new accounts, so a
        reconnect alone is not enough. Held under the execution lock so the
        set of brokers never changes underneath a running command.
        """
        with self.lock.acquire(owner="rebuild_brokers"):
            if self._rebuildable:
                fresh = build_brokers(self.settings)
                with self._connect_lock:
                    old, self.brokers = self.brokers, fresh
                for broker in old.values():
                    try:
                        broker.close()
                    except Exception:
                        pass
            with self._quote_memo_lock:
                self._quote_memo.clear()
            self._fills_cache.clear()
            return self.connect_all()

    def display_quote(self, broker: Broker, symbol: str) -> tuple[Any, str | None]:
        """A quote for the dashboard, memoised for a few seconds.

        NEVER use this to price an order. Execution calls the broker directly
        through ``ExecutionEngine.fresh_quote``, which validates staleness
        against ``MAX_QUOTE_AGE_SECONDS``; a memo would undermine exactly that
        check. This exists only because the dashboard asks for the same quote
        several times per poll, and because a *failing* snapshot costs several
        seconds of retries inside the adapter -- repeated once per symbol per
        broker per poll, that alone made the whole dashboard sluggish.

        Returns ``(quote, error)``; exactly one is set.
        """
        key = f"{broker.name}:{symbol.upper()}"
        ttl = self.settings.quote_display_ttl_seconds
        now = time.time()
        with self._quote_memo_lock:
            cached = self._quote_memo.get(key)
            if cached and now - cached[0] < ttl:
                return cached[1], cached[2]
        try:
            quote = broker.get_quote(symbol)
            entry = (time.time(), quote, None)
        except Exception as exc:
            # Failures are memoised too, and that is the point: a missing
            # market-data subscription must not cost a retry storm per poll.
            entry = (time.time(), None, str(exc))
        with self._quote_memo_lock:
            self._quote_memo[key] = entry
        return entry[1], entry[2]

    # --- status -----------------------------------------------------------

    def snapshot_broker(self, broker: Broker) -> BrokerSnapshot:
        snapshot = BrokerSnapshot(
            broker=broker.name,
            connected=broker.connected,
            mode=self.settings.mode,
            read_only=broker.read_only,
            broker_type=broker.broker_type,
        )
        try:
            if not broker.connected:
                broker.connect()
            snapshot.connected = broker.connected
            snapshot.account = broker.account
            snapshot.positions = broker.get_positions()
            snapshot.balances = broker.get_available_cash()
            snapshot.open_orders = broker.get_open_orders()
            snapshot.exposure = exposure_from_positions(
                snapshot.positions,
                self.settings.long_symbol,
                self.settings.short_symbol,
            )
            for symbol in self.settings.symbols:
                quote, quote_error = self.display_quote(broker, symbol)
                if quote is not None:
                    snapshot.quotes[symbol] = quote
                else:  # quotes are nice-to-have on a dashboard
                    log.debug(
                        "%s quote for %s failed: %s",
                        broker.name, symbol, quote_error,
                    )
            self._record_nav(broker, snapshot)
        except Exception as exc:
            snapshot.connected = False
            snapshot.error = str(exc)
        return snapshot

    def _record_nav(self, broker: Broker, snapshot: BrokerSnapshot) -> None:
        """Write one NAV point per account per day, from observed state.

        This is the exact half of the performance picture: from today onward
        every account has a measured daily value, with no reconstruction and
        no dependence on a broker's analytics endpoint.
        """
        if snapshot.balances is None:
            return
        day = today_utc().isoformat()
        if self._nav_recorded.get(broker.name) == day:
            return
        positions_value = sum(p.market_value for p in snapshot.positions)
        point = nav_from_snapshot(snapshot.balances.cash_available, positions_value)
        if not self.nav.has_date(broker.name, day):
            self.nav.record(broker.name, point)
        self._nav_recorded[broker.name] = day

    def status(self) -> dict[str, Any]:
        snapshots: dict[str, BrokerSnapshot] = {}
        if self.brokers:
            with ThreadPoolExecutor(max_workers=len(self.brokers)) as pool:
                futures = {
                    name: pool.submit(self.snapshot_broker, broker)
                    for name, broker in self.brokers.items()
                }
                for name, future in futures.items():
                    snapshots[name] = future.result()

        market = market_status()
        return {
            "mode": self.settings.mode.value,
            "live": self.settings.is_live,
            "generated_at": time.time(),
            "market": {
                "open": market.is_open,
                "session": market.session,
                "reason": market.reason,
                "now_eastern": market.now_eastern,
            },
            "config": self.settings.to_public_dict(),
            "environment_warnings": self.environment_warnings,
            "brokers": {n: s.to_dict() for n, s in snapshots.items()},
            "combined": self._combine(snapshots),
            "state": self.state.snapshot(),
            "last_fills": self.last_fills(),
        }

    def _combine(self, snapshots: dict[str, BrokerSnapshot]) -> dict[str, Any]:
        long_sym, short_sym = self.settings.symbols
        total_long = total_short = total_cash = total_value = 0.0
        exposures: set[Exposure] = set()
        connected = 0
        open_orders = 0

        for snapshot in snapshots.values():
            if snapshot.connected and not snapshot.error:
                connected += 1
            total_long += snapshot.position_qty(long_sym)
            total_short += snapshot.position_qty(short_sym)
            if snapshot.balances:
                total_cash += snapshot.balances.cash_available
            total_value += sum(p.market_value for p in snapshot.positions)
            open_orders += len(snapshot.open_orders)
            if snapshot.connected and not snapshot.error:
                exposures.add(snapshot.exposure)

        if not exposures:
            combined_exposure = Exposure.UNKNOWN
        elif len(exposures) == 1:
            combined_exposure = next(iter(exposures))
        else:
            combined_exposure = Exposure.MIXED

        return {
            "brokers_total": len(snapshots),
            "brokers_connected": connected,
            f"{long_sym}_shares": total_long,
            f"{short_sym}_shares": total_short,
            "long_symbol": long_sym,
            "short_symbol": short_sym,
            "chart_symbols": list(self.settings.chart_symbols),
            "long_shares": total_long,
            "short_shares": total_short,
            "cash_available": round(total_cash, 2),
            "positions_market_value": round(total_value, 2),
            "account_value": round(total_cash + total_value, 2),
            "exposure": combined_exposure.value,
            "open_orders": open_orders,
            "exposures_agree": len(exposures) <= 1,
        }

    def performance(
        self, months: int = 6, backfill: bool = True
    ) -> dict[str, Any]:
        """Per-account, per-broker and combined returns over the windows.

        ``backfill`` reaches out to the brokers for history that predates
        this system; it self-limits to one attempt per account per process
        because IBKR rate limits its performance endpoint hard.
        """
        per_account: list[BrokerPerformance] = []
        series_by_account: dict[str, list[NavPoint]] = {}

        for name, broker in self.brokers.items():
            entry = BrokerPerformance(broker=name)
            try:
                if backfill:
                    if not broker.connected:
                        broker.connect()
                    self.backfiller.backfill(broker, months)
            except Exception as exc:
                entry.error = f"backfill unavailable: {exc}"

            series = self.nav.series(name)
            series_by_account[name] = series
            entry.series = trim_series(series, months)
            entry.windows = compute_windows(series)
            entry.current_nav = series[-1].nav if series else None
            per_account.append(entry)

        combined = combine_series(series_by_account)

        # Group accounts into their broker family, so three Schwab accounts
        # roll up to one "schwab" figure as well as reporting individually.
        by_type: dict[str, dict[str, list[NavPoint]]] = {}
        for name, broker in self.brokers.items():
            by_type.setdefault(broker.broker_type, {})[name] = series_by_account[name]

        grouped: list[dict[str, Any]] = []
        for broker_type, members in sorted(by_type.items()):
            merged = combine_series(members)
            grouped.append({
                "broker_type": broker_type,
                "accounts": sorted(members),
                "current_nav": round(merged[-1].nav, 2) if merged else None,
                "windows": [w.to_dict() for w in compute_windows(merged)],
                "series": [p.to_dict() for p in trim_series(merged, months)],
            })

        return {
            "generated_at": time.time(),
            "months": months,
            "windows": [label for label, _ in DEFAULT_WINDOWS],
            "accounts": [
                e.to_dict() for e in sorted(per_account, key=lambda x: x.broker)
            ],
            "by_broker": grouped,
            "combined": {
                "current_nav": round(combined[-1].nav, 2) if combined else None,
                "windows": [w.to_dict() for w in compute_windows(combined)],
                "series": [p.to_dict() for p in trim_series(combined, months)],
            },
            "account_labels": {
                name: {
                    "display_id": (
                        broker.account.display_id if broker.account else None
                    ),
                    "broker_type": broker.broker_type,
                    "read_only": broker.read_only,
                }
                for name, broker in self.brokers.items()
            },
        }

    def holdings(self) -> dict[str, Any]:
        """Every position in every account, not just the two traded symbols."""
        out: dict[str, Any] = {"brokers": {}, "generated_at": time.time()}
        for name, broker in self.brokers.items():
            entry: dict[str, Any] = {"error": None, "positions": []}
            try:
                if not broker.connected:
                    broker.connect()
                entry["account"] = broker.account.to_dict() if broker.account else None
                entry["positions"] = [p.to_dict() for p in broker.get_positions()]
                balances = broker.get_available_cash()
                entry["balances"] = balances.to_dict()
            except Exception as exc:
                entry["error"] = str(exc)
            out["brokers"][name] = entry
        return out

    # --- candles ----------------------------------------------------------

    def candle_series(
        self, symbol: str, days: int | None = None, refresh: bool = True
    ) -> dict[str, Any]:
        """Cached hourly bars for one symbol, refreshing only the recent edge."""
        # Read-only accounts are perfectly good sources of market data, so
        # unlike order routing this uses every connected broker.
        brokers = list(self.brokers.values())
        return self.candles.series(brokers, symbol, days=days, refresh=refresh)

    #: Broker order history changes rarely; the dashboard polls every 10s.
    FILLS_TTL_SECONDS = 60.0

    def last_fills(self) -> dict[str, dict[str, Any]]:
        """Most recent fill per account per symbol, for the current accounts.

        The broker's own order history wins: it includes ladder orders, which
        return before filling, and trades placed outside this app. A broker
        that cannot list fills falls back to this app's command history.
        """
        history = self._history_fills()
        out: dict[str, dict[str, Any]] = {}
        for name, broker in self.brokers.items():
            fills = self._broker_fills(name, broker)
            out[name] = fills if fills is not None else history.get(name, {})
        return out

    def _broker_fills(self, name: str, broker: Broker) -> dict[str, Any] | None:
        cached = self._fills_cache.get(name)
        if cached and time.time() - cached[0] < self.FILLS_TTL_SECONDS:
            return cached[1]
        if not broker.connected:
            return cached[1] if cached else None
        try:
            orders = broker.get_recent_fills()
        except Exception as exc:
            log.debug("%s: recent fills unavailable: %s", name, exc)
            return cached[1] if cached else None
        result: dict[str, Any] | None = None
        if orders is not None:
            result = {}
            for order in sorted(orders, key=lambda o: o.updated_at, reverse=True):
                if order.symbol in result:
                    continue
                result[order.symbol] = {
                    "shares": order.filled_quantity,
                    "price": order.average_fill_price,
                    "side": order.side.value,
                    "at": order.updated_at,
                    "command": "broker",
                }
        self._fills_cache[name] = (time.time(), result)
        return result

    def _history_fills(self) -> dict[str, dict[str, Any]]:
        """Most recent fill per broker per symbol, from the command history.

        Walks history newest-first and keeps the first hit for each
        (broker, symbol) pair, so a symbol that was not touched by the latest
        command still reports the last time it *was* traded rather than
        blanking out.
        """
        out: dict[str, dict[str, Any]] = {}
        for record in reversed(self.state.data.history):
            for broker_result in record.get("results", []) or []:
                broker = broker_result.get("broker")
                if not broker:
                    continue
                bucket = out.setdefault(broker, {})
                for fill in broker_result.get("fills", []) or []:
                    symbol = (fill.get("symbol") or "").upper()
                    if not symbol or symbol in bucket:
                        continue
                    if not fill.get("filled_quantity"):
                        continue
                    bucket[symbol] = {
                        "shares": fill.get("filled_quantity"),
                        "price": fill.get("average_price"),
                        "side": fill.get("side"),
                        "at": record.get("finished_at") or record.get("started_at"),
                        "command": record.get("command"),
                    }
        return out

    # --- ladder -----------------------------------------------------------

    def ladder(self) -> dict[str, Any]:
        """Session high/low and the rung list for each traded symbol.

        Quotes and open orders are read live from one broker each time. The
        rungs themselves are broker-independent -- they come from the session
        range -- but the *open orders* shown against them are per account,
        because that is what tells the operator whether a click will place a
        new order or re-price an existing one.
        """
        out: dict[str, Any] = {
            "generated_at": time.time(),
            "enabled": self.settings.ladder_enabled,
            "levels": self.settings.ladder_levels,
            "symbols": {},
            "accounts": {},
        }

        # One quote per symbol, from the first broker that can supply one.
        quotes: dict[str, Any] = {}
        for symbol in self.settings.symbols:
            entry: dict[str, Any] = {
                "symbol": symbol,
                "step": self.settings.ladder_step_for(symbol),
                "tick": self.settings.ladder_tick_for(symbol),
                "quote": None,
                "rungs": [],
                "anchors": [],
                "error": None,
            }
            for broker in self.brokers.values():
                try:
                    if not broker.connected:
                        broker.connect()
                except Exception as exc:
                    entry["error"] = str(exc)
                    continue
                quote, quote_error = self.display_quote(broker, symbol)
                if quote is None:
                    entry["error"] = quote_error
                    continue
                quotes[symbol] = quote
                entry["quote"] = quote.to_dict()
                entry["error"] = None
                break

            # Session extremes come from a *minute* series, not the hourly
            # one the chart uses: an hourly bar can only say which hour a high
            # happened in, which is not a time. This also keeps the ladder
            # anchored when the quote feed is down but history still works.
            fine = self.settings.session_bar_size
            bars = []
            try:
                due = (
                    time.time() - self._session_refreshed.get(symbol, 0.0)
                    >= self.settings.session_refresh_seconds
                )
                if due:
                    self.candles.refresh(
                        list(self.brokers.values()), symbol,
                        days=self.settings.session_days, bar_size=fine,
                    )
                    self._session_refreshed[symbol] = time.time()
                bars = self.candle_store.bars(
                    symbol,
                    time.time() - self.settings.session_days * 86400,
                    time.time(),
                    fine,
                )
            except Exception as exc:
                log.debug("session bars for %s unavailable: %s", symbol, exc)
            session = compute_session_range(
                symbol,
                bars,
                now=time.time(),
                quote=quotes.get(symbol),
                outside_rth=self.settings.candle_outside_rth,
                resolution_seconds=bar_seconds(fine),
            )
            entry["session"] = session.to_dict()

            quote = quotes.get(symbol)
            if session.has_swing_anchors:
                # Rungs hang off the live swing -- high since the low, low
                # since the high -- not off the day extremes, which can both
                # be hours stale and on the wrong side of the current leg.
                entry["anchors"] = [
                    {"code": code, "name": ANCHOR_NAMES.get(code, code),
                     "price": price}
                    for code, price in session.anchors
                ]
                entry["rungs"] = [
                    r.to_dict()
                    for r in build_rung_views(
                        symbol,
                        self.settings,
                        session.anchors,
                        reference_price=(quote.ask if quote else 0.0),
                        last_price=(
                            quote.last if quote else session.high_since_low
                        ),
                    )
                ]
                entry["error"] = None
            elif not entry["error"]:
                entry["error"] = (
                    f"no swing levels for {symbol} yet — neither the live "
                    "quote nor the cached bars cover the current session, so "
                    "no rungs can be anchored"
                )
            out["symbols"][symbol] = entry

        # Per-account working orders and positions, straight from the broker.
        for name, broker in self.brokers.items():
            account: dict[str, Any] = {
                "broker_type": broker.broker_type,
                "read_only": broker.read_only,
                "connected": broker.connected,
                "display_id": broker.account.display_id if broker.account else None,
                "open_orders": [],
                "positions": {},
                "cash_available": None,
                "error": None,
            }
            try:
                if not broker.connected:
                    broker.connect()
                account["connected"] = broker.connected
                account["open_orders"] = [
                    o.to_dict()
                    for o in broker.get_open_orders_for(set(self.settings.symbols))
                ]
                account["positions"] = {
                    sym: broker.get_position(sym).quantity
                    if broker.get_position(sym) else 0.0
                    for sym in self.settings.symbols
                }
                account["cash_available"] = broker.get_available_cash().cash_available
            except Exception as exc:
                account["error"] = str(exc)
            out["accounts"][name] = account

        return out

    def place_ladder_order(
        self,
        symbol: str,
        limit_price: float,
        *,
        request_id: str | None = None,
        confirm_token: str | None = None,
        force: bool = False,
        source: str = "api",
    ) -> LadderOrderResult:
        """Place or re-price one resting BUY limit at ``limit_price``.

        Every rung order requires a confirmation token, in every mode. That is
        stricter than the LONG/SHORT commands, which only confirm in live
        mode, and it is deliberate: a ladder is a grid of one-click buttons,
        so the click itself carries no weight and the confirmation is the
        only thing standing between a stray tap and an order.
        """
        symbol = (symbol or "").upper().strip()
        command_id = new_command_id()
        key = rung_key(symbol, limit_price)

        self.audit.record(
            A.COMMAND_RECEIVED,
            command_id=command_id,
            command=key,
            mode=self.settings.mode.value,
            source=source,
            request_id=request_id,
            force=force,
        )

        try:
            self._ladder_preflight(
                symbol, limit_price, key, request_id, confirm_token,
                force, command_id,
            )
        except TradingError as exc:
            self.audit.record(
                A.COMMAND_REJECTED,
                command_id=command_id,
                command=key,
                reason=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        result = LadderOrderResult(
            command_id=command_id,
            symbol=symbol,
            limit_price=limit_price,
            mode=self.settings.mode,
        )

        with self.lock.acquire(owner=f"{source}:{key}:{command_id}"):
            self.audit.record(A.LOCK_ACQUIRED, command_id=command_id, command=key)
            self.state.record_command(key, command_id, request_id)
            targets = self.tradable_brokers
            self.audit.record(
                A.COMMAND_STARTED,
                command_id=command_id,
                command=key,
                brokers=list(targets),
                read_only_skipped=[
                    n for n, b in self.brokers.items() if b.read_only
                ],
            )

            def run_one(broker: Broker) -> Any:
                return LadderExecutor(
                    self.settings, self.audit, command_id
                ).place(broker, symbol, limit_price)

            if targets:
                with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                    futures = {
                        name: pool.submit(run_one, broker)
                        for name, broker in targets.items()
                    }
                    for name, future in futures.items():
                        try:
                            result.results.append(future.result())
                        except Exception as exc:
                            log.exception("%s: ladder executor crashed", name)
                            from .ladder import RungOrderResult

                            result.results.append(
                                RungOrderResult(
                                    broker=name,
                                    ok=False,
                                    mode=self.settings.mode,
                                    symbol=symbol,
                                    limit_price=limit_price,
                                    errors=[f"{type(exc).__name__}: {exc}"],
                                    needs_attention=True,
                                )
                            )

        result.finished_at = time.time()
        payload = result.to_dict()
        self.state.record_result(payload)
        self.audit.record(
            A.COMMAND_FINISHED,
            command_id=command_id,
            command=key,
            status=result.status,
            result=payload,
        )
        return result

    def _ladder_preflight(
        self,
        symbol: str,
        limit_price: float,
        key: str,
        request_id: str | None,
        confirm_token: str | None,
        force: bool,
        command_id: str,
    ) -> None:
        if not self.settings.ladder_enabled:
            raise OrderValidationError(
                "the price ladder is disabled; set LADDER_ENABLED=true to use it"
            )
        if symbol not in {s.upper() for s in self.settings.symbols}:
            raise OrderValidationError(
                f"symbol {symbol!r} is not tradable by this system; only "
                f"{sorted(self.settings.symbols)} are permitted"
            )
        if limit_price is None or limit_price <= 0:
            raise OrderValidationError(
                f"limit price must be positive, got {limit_price!r}"
            )

        self.state.check_mismatch_clear(self.settings.require_mismatch_ack)
        self.state.check_daily_limit(self.settings.max_daily_commands)
        # Keyed on symbol AND price, so two different rungs never suppress
        # each other but a double-tap on one rung does.
        self.state.check_duplicate(
            key,
            self.settings.duplicate_command_window_seconds,
            request_id=request_id,
            force=force,
        )

        if not confirm_token or not self.state.consume_confirmation(
            confirm_token, key
        ):
            pending = self.state.issue_confirmation(
                key, self.settings.confirmation_ttl_seconds
            )
            self.audit.record(
                A.CONFIRMATION_ISSUED,
                command_id=command_id,
                command=key,
                expires_at=pending.expires_at,
            )
            raise ConfirmationRequired(
                f"BUY {symbol} at ${limit_price:,.4f}. Re-send with this "
                f"confirmation token within "
                f"{self.settings.confirmation_ttl_seconds:.0f}s to proceed.",
                pending.token,
                pending.expires_at,
            )
        self.audit.record(
            A.CONFIRMATION_ACCEPTED, command_id=command_id, command=key
        )

    # --- command execution ------------------------------------------------

    def execute(
        self,
        command: Command,
        *,
        request_id: str | None = None,
        confirm_token: str | None = None,
        force: bool = False,
        source: str = "api",
    ) -> CommandResult:
        command_id = new_command_id()
        self.audit.record(
            A.COMMAND_RECEIVED,
            command_id=command_id,
            command=command.value,
            mode=self.settings.mode.value,
            source=source,
            request_id=request_id,
            force=force,
        )

        try:
            self._preflight(command, request_id, confirm_token, force, command_id)
        except TradingError as exc:
            self.audit.record(
                A.COMMAND_REJECTED,
                command_id=command_id,
                command=command.value,
                reason=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        try:
            with self.lock.acquire(owner=f"{source}:{command.value}:{command_id}"):
                self.audit.record(
                    A.LOCK_ACQUIRED, command_id=command_id, command=command.value
                )
                self.state.record_command(command.value, command_id, request_id)
                return self._run_all(command, command_id)
        except TradingError as exc:
            if isinstance(exc, ExecutionLocked):
                self.audit.record(
                    A.LOCK_CONTENDED,
                    command_id=command_id,
                    command=command.value,
                    reason=str(exc),
                )
            raise

    def _preflight(
        self,
        command: Command,
        request_id: str | None,
        confirm_token: str | None,
        force: bool,
        command_id: str,
    ) -> None:
        """Every gate that can reject a command before the lock is taken."""
        # A prior broker disagreement blocks everything except GO FLAT, which
        # is the command you want available when things have gone wrong.
        if command is not Command.GO_FLAT:
            self.state.check_mismatch_clear(self.settings.require_mismatch_ack)

        self.state.check_daily_limit(self.settings.max_daily_commands)
        self.state.check_duplicate(
            command.value,
            self.settings.duplicate_command_window_seconds,
            request_id=request_id,
            force=force,
        )

        if self._needs_confirmation(command):
            if not confirm_token or not self.state.consume_confirmation(
                confirm_token, command.value
            ):
                pending = self.state.issue_confirmation(
                    command.value, self.settings.confirmation_ttl_seconds
                )
                self.audit.record(
                    A.CONFIRMATION_ISSUED,
                    command_id=command_id,
                    command=command.value,
                    expires_at=pending.expires_at,
                )
                raise ConfirmationRequired(
                    f"{command.value} in LIVE mode deploys the whole account. "
                    "Re-send the command with this confirmation token within "
                    f"{self.settings.confirmation_ttl_seconds:.0f}s to proceed.",
                    pending.token,
                    pending.expires_at,
                )
            self.audit.record(
                A.CONFIRMATION_ACCEPTED,
                command_id=command_id,
                command=command.value,
            )

    def _needs_confirmation(self, command: Command) -> bool:
        """Live LONG/SHORT commands deploy the entire account, so they confirm.

        GO FLAT is deliberately exempt: it only ever reduces risk, and making
        the panic button slower helps nobody.
        """
        if not self.settings.require_confirmation:
            return False
        if self.settings.mode is not TradingMode.LIVE:
            return False
        return command in (Command.GO_LONG, Command.GO_SHORT)

    @property
    def tradable_brokers(self) -> dict[str, Broker]:
        """Accounts a command may actually touch.

        Read-only accounts are shown on the dashboard but never dispatched
        to, so they cannot appear as failures in a command result either.
        """
        return {n: b for n, b in self.brokers.items() if not b.read_only}

    def _run_all(self, command: Command, command_id: str) -> CommandResult:
        result = CommandResult(
            command_id=command_id, command=command, mode=self.settings.mode
        )
        targets = self.tradable_brokers
        self.audit.record(
            A.COMMAND_STARTED,
            command_id=command_id,
            command=command.value,
            brokers=list(targets),
            read_only_skipped=[
                n for n, b in self.brokers.items() if b.read_only
            ],
        )

        def run_one(name: str, broker: Broker) -> BrokerResult:
            strategy = Strategy(self.settings, self.audit, command_id)
            return strategy.run(broker, command)

        if targets:
            # Accounts run concurrently and independently: none can block or
            # abort another, which is exactly the requested semantics.
            with ThreadPoolExecutor(max_workers=len(targets)) as pool:
                futures = {
                    name: pool.submit(run_one, name, broker)
                    for name, broker in targets.items()
                }
                for name, future in futures.items():
                    try:
                        result.results.append(future.result())
                    except Exception as exc:  # a Strategy that itself exploded
                        log.exception("%s: strategy crashed", name)
                        failed = BrokerResult(
                            broker=name,
                            command=command,
                            ok=False,
                            mode=self.settings.mode,
                            errors=[f"{type(exc).__name__}: {exc}"],
                            needs_attention=True,
                        )
                        failed.finished_at = time.time()
                        result.results.append(failed)

        result.finished_at = time.time()
        self._handle_mismatch(result)

        payload = result.to_dict()
        self.state.record_result(payload)
        self.audit.record(
            A.COMMAND_FINISHED,
            command_id=command_id,
            command=command.value,
            status=result.status,
            result=payload,
        )
        return result

    def _handle_mismatch(self, result: CommandResult) -> None:
        """Never let a half-succeeded command look like a success."""
        needs_attention = any(r.needs_attention for r in result.results)
        if not result.mismatch and not needs_attention:
            return

        ok = [r.broker for r in result.results if r.ok]
        failed = [r.broker for r in result.results if not r.ok]
        summary = (
            f"{result.command.value}: succeeded at {ok or 'none'}, "
            f"failed at {failed or 'none'}"
        )
        detail = {
            "command_id": result.command_id,
            "command": result.command.value,
            "succeeded": ok,
            "failed": failed,
            "results": [r.to_dict() for r in result.results],
        }
        self.audit.record(
            A.MISMATCH,
            command_id=result.command_id,
            command=result.command.value,
            summary=summary,
            detail=detail,
        )
        log.error("BROKER MISMATCH — %s", summary)
        if self.settings.require_mismatch_ack:
            self.state.set_mismatch(summary, detail)

    def acknowledge_mismatch(self, note: str = "") -> dict[str, Any] | None:
        previous = self.state.clear_mismatch()
        self.audit.record(A.MISMATCH_ACK, note=note, cleared=previous)
        return previous

    def close(self) -> None:
        for broker in self.brokers.values():
            try:
                broker.close()
            except Exception:
                pass
