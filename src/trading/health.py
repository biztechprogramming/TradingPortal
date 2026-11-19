"""Is trading actually available on each account, right now?

``Broker.connected`` only says a session was established once. It stays true
while IB Gateway has lost IBKR's servers, while the gateway is hung, and
until the next Schwab call notices an expired token. This module asks every
account a cheap question on a timer and keeps the answer, with its time, so
the dashboard can show a real "trading available" light and the order paths
can leave out accounts that would only fail.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from typing import Any, Callable

from .brokers.base import Broker
from .errors import BrokerAuthError, BrokerLinkDown, BrokerTimeout

log = logging.getLogger("trading.health")

OK = "ok"
LINK_DOWN = "link_down"          # gateway up, but cut off from the broker
UNRESPONSIVE = "unresponsive"    # did not answer in time
LOGIN_REQUIRED = "login_required"
DOWN = "down"                    # not reachable / other failure


@dataclass
class Health:
    state: str
    message: str
    checked_at: float
    latency_ms: float | None = None

    @property
    def tradable(self) -> bool:
        return self.state == OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "tradable": self.tradable,
            "message": self.message,
            "checked_at": self.checked_at,
            "latency_ms": self.latency_ms,
        }


def _label(broker: Broker) -> str:
    return "Schwab" if broker.broker_type == "schwab" else (
        "IBKR" if broker.broker_type == "ibkr" else broker.broker_type)


def classify(broker: Broker, exc: BaseException) -> tuple[str, str]:
    """Turn a probe failure into a state and a message for a person."""
    text = str(exc) or type(exc).__name__
    if isinstance(exc, BrokerLinkDown):
        return LINK_DOWN, text
    if isinstance(exc, BrokerTimeout):
        return UNRESPONSIVE, text
    if isinstance(exc, BrokerAuthError):
        return LOGIN_REQUIRED, f"{_label(broker)} session expired, log in. ({text})"
    return DOWN, text


class HealthMonitor:
    """Probes every account every ``interval`` seconds on a daemon thread."""

    def __init__(
        self,
        brokers: Callable[[], dict[str, Broker]],
        *,
        interval: float = 45.0,
        timeout: float = 5.0,
        on_change: Callable[[str, Health, Health | None], None] | None = None,
    ) -> None:
        self._brokers = brokers
        self.interval = interval
        self.timeout = timeout
        self._on_change = on_change
        self._results: dict[str, Health] = {}
        self._inflight: dict[str, Future] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="health")
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        if self.interval <= 0 or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop, name="health-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(2)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_all()
            except Exception:
                log.exception("health check round failed")
            self._stop.wait(self.interval)

    # --- checks -----------------------------------------------------------

    def check_all(self, names: list[str] | None = None) -> dict[str, Health]:
        """Probe the given accounts (default all) concurrently and wait."""
        brokers = self._brokers()
        targets = {n: b for n, b in brokers.items() if names is None or n in names}
        futures = {n: self._submit(n, b) for n, b in targets.items()}
        out: dict[str, Health] = {}
        for name, (future, started) in futures.items():
            remaining = max(0.1, self.timeout + 1.0 - (time.monotonic() - started))
            try:
                future.result(remaining)
                health = Health(OK, "trading available", time.time(),
                                round((time.monotonic() - started) * 1000, 1))
            except FutureTimeout:
                health = Health(
                    UNRESPONSIVE,
                    f"{_label(targets[name])} did not answer within "
                    f"{self.timeout:.0f}s"
                    + (" (Gateway unresponsive)" if targets[name].broker_type == "ibkr" else ""),
                    time.time())
            except Exception as exc:
                state, message = classify(targets[name], exc)
                health = Health(state, message, time.time())
            out[name] = self._store(name, health)
        return out

    def _submit(self, name: str, broker: Broker) -> tuple[Future, float]:
        """One probe per account at a time; a hung one is waited on, not stacked."""
        with self._lock:
            running = self._inflight.get(name)
            if running is not None and not running.done():
                return running, getattr(running, "_started", time.monotonic())
            future = self._pool.submit(broker.health_check, self.interval)
            future._started = time.monotonic()  # type: ignore[attr-defined]
            self._inflight[name] = future
            return future, future._started  # type: ignore[attr-defined]

    def _store(self, name: str, health: Health) -> Health:
        with self._lock:
            previous = self._results.get(name)
            self._results[name] = health
        if previous is None or previous.state != health.state:
            if health.tradable:
                log.info("%s: trading available", name)
            else:
                log.warning("%s: NOT tradable (%s): %s",
                            name, health.state, health.message)
            if self._on_change:
                try:
                    self._on_change(name, health, previous)
                except Exception:
                    log.debug("health change hook failed", exc_info=True)
        return health

    # --- reads ------------------------------------------------------------

    def get(self, name: str) -> Health | None:
        with self._lock:
            return self._results.get(name)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return {n: h.to_dict() for n, h in self._results.items()}

    def unhealthy(self, names: list[str]) -> dict[str, Health]:
        """Accounts that are red *now*, re-probing any that were red or unknown.

        A stale red light must never keep a recovered account out of an
        order, so anything not known-green is asked again before deciding.
        """
        recheck = [n for n in names if not (h := self.get(n)) or not h.tradable]
        if recheck:
            self.check_all(recheck)
        return {n: h for n in names if (h := self.get(n)) and not h.tradable}

    def forget(self) -> None:
        """Drop results, e.g. after the set of accounts was rebuilt."""
        with self._lock:
            self._results.clear()
            self._inflight.clear()
