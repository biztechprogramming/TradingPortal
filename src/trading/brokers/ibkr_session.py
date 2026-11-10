"""One persistent IB Gateway (TWS API) connection, shared by every IBKR account.

The TWS API is a single long-lived socket per client id, so the connection is
owned here rather than by each broker instance: two instances opening their
own socket with the same client id would knock each other off.

ib_async is asyncio-based and not thread-safe. The app is threaded (FastAPI
workers, one strategy thread per account), so the IB object lives on its own
event-loop thread and every call is marshalled onto it with :meth:`call`.

A watchdog task keeps the socket up: if the gateway restarts (nightly), loses
IBKR's servers, or is stopped, it reconnects every few seconds with the same
client id, which is what lets IBKR hand our open orders back to us.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import threading
import time
from typing import Any, Callable

from ..errors import BrokerConnectionError, BrokerTimeout

log = logging.getLogger("trading.broker.ibkr_tws")

#: Seconds between reconnect attempts while the gateway is unreachable.
RECONNECT_INTERVAL = 5.0

#: TWS API message codes worth surfacing. 1100 = lost IBKR's servers, 1101/1102
#: = restored, 2110 = gateway<->IBKR link broken.
CONNECTIVITY_CODES = {1100, 1101, 1102, 2110}


def _default_ib_factory() -> Any:
    from ib_async import IB

    return IB()


class TwsSession:
    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        *,
        ib_factory: Callable[[], Any] = _default_ib_factory,
        reconnect_interval: float = RECONNECT_INTERVAL,
    ) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self._ib_factory = ib_factory
        self._reconnect_interval = reconnect_interval
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._connected = threading.Event()
        self._start_lock = threading.Lock()
        self._stopping = False
        self.ib: Any = None
        self.last_error: str | None = None
        #: Bumped on every (re)connect so adapters can drop per-socket state
        #: such as market-data subscriptions.
        self.generation = 0
        #: Latest error text per request id (order id for orders).
        self.errors: dict[int, tuple[int, str]] = {}
        #: Latest market-data error per contract id.
        self.contract_errors: dict[int, tuple[int, str]] = {}
        #: Set once the first connect attempt has finished either way, so
        #: callers stop waiting the full timeout on a gateway that is down.
        self.attempted = threading.Event()

    # --- lifecycle --------------------------------------------------------

    def start(self) -> None:
        with self._start_lock:
            if self._thread and self._thread.is_alive():
                return
            self._stopping = False
            self._ready.clear()
            self._thread = threading.Thread(
                target=self._run, name="ibkr-tws-loop", daemon=True
            )
            self._thread.start()
        self._ready.wait(10)

    def stop(self) -> None:
        self._stopping = True
        loop = self._loop
        if loop and loop.is_running():
            def _shutdown() -> None:
                try:
                    if self.ib is not None and self.ib.isConnected():
                        self.ib.disconnect()
                finally:
                    loop.stop()
            loop.call_soon_threadsafe(_shutdown)
        if self._thread:
            self._thread.join(5)
        self._connected.clear()

    @property
    def connected(self) -> bool:
        return self._connected.is_set()

    def wait_connected(self, timeout: float) -> bool:
        self.start()
        if self._connected.is_set():
            return True
        if self.attempted.is_set():
            # Known to be down: a short grace for a reconnect in progress.
            timeout = min(timeout, 2.0)
        return self._connected.wait(timeout)

    # --- calls ------------------------------------------------------------

    def call(self, fn: Callable[[Any], Any], timeout: float = 20.0) -> Any:
        """Run ``fn(ib)`` on the loop thread; await it if it returns a coroutine."""
        self.start()
        loop = self._loop
        if loop is None:
            raise BrokerConnectionError("IB Gateway session did not start")

        async def runner() -> Any:
            result = fn(self.ib)
            if inspect.isawaitable(result):
                result = await result
            return result

        future = asyncio.run_coroutine_threadsafe(runner(), loop)
        try:
            return future.result(timeout)
        except TimeoutError as exc:
            future.cancel()
            raise BrokerTimeout(
                f"IB Gateway did not answer within {timeout:.0f}s"
            ) from exc

    # --- loop thread ------------------------------------------------------

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            self.ib = self._ib_factory()
            self._wire_events()
            loop.create_task(self._watchdog())
            self._ready.set()
            loop.run_forever()
        except Exception:
            log.exception("IB Gateway loop crashed")
        finally:
            self._connected.clear()
            self._ready.set()
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        asyncio.gather(*pending, return_exceptions=True))
                loop.close()
            except Exception:
                pass
            self._loop = None

    def _wire_events(self) -> None:
        ib = self.ib
        for name, handler in (
            ("connectedEvent", self._on_connected),
            ("disconnectedEvent", self._on_disconnected),
            ("errorEvent", self._on_error),
        ):
            event = getattr(ib, name, None)
            if event is not None:
                event += handler

    async def _watchdog(self) -> None:
        was_up = False
        while not self._stopping:
            if not self.ib.isConnected():
                if was_up:
                    log.warning("IB Gateway connection lost; reconnecting")
                    was_up = False
                try:
                    await self.ib.connectAsync(
                        self.host, self.port, clientId=self.client_id, timeout=10
                    )
                except Exception as exc:
                    msg = f"IB Gateway not reachable at {self.host}:{self.port}: {exc or type(exc).__name__}"
                    if msg != self.last_error:
                        log.warning("%s", msg)
                    self.last_error = msg
                    self._connected.clear()
                    self.attempted.set()
                    await asyncio.sleep(self._reconnect_interval)
                    continue
            if self.ib.isConnected():
                if not was_up:
                    self._on_connected()
                was_up = True
            self.attempted.set()
            await asyncio.sleep(1.0)

    def _on_connected(self) -> None:
        if self._connected.is_set():
            return
        self.generation += 1
        self.last_error = None
        self._connected.set()
        log.info("IB Gateway connected at %s:%s (client id %s)",
                 self.host, self.port, self.client_id)

    def _on_disconnected(self) -> None:
        self._connected.clear()

    def _on_error(self, req_id: int, code: int, message: str,
                  contract: Any = None, *_: Any) -> None:
        self.errors[int(req_id)] = (int(code), str(message))
        con_id = getattr(contract, "conId", 0)
        if con_id:
            self.contract_errors[int(con_id)] = (int(code), str(message))
        if len(self.errors) > 500:
            for key in list(self.errors)[:250]:
                self.errors.pop(key, None)
        if int(code) in CONNECTIVITY_CODES:
            log.warning("IB Gateway %s: %s", code, message)


_sessions: dict[tuple[str, int, int], TwsSession] = {}
_sessions_lock = threading.Lock()


def shared_session(host: str, port: int, client_id: int) -> TwsSession:
    """The process-wide session for this gateway and client id."""
    key = (host, int(port), int(client_id))
    with _sessions_lock:
        session = _sessions.get(key)
        if session is None:
            session = TwsSession(host, int(port), int(client_id))
            _sessions[key] = session
        return session


def reset_sessions() -> None:
    """Stop and forget every session (logout, tests)."""
    with _sessions_lock:
        sessions = list(_sessions.values())
        _sessions.clear()
    for session in sessions:
        try:
            session.stop()
        except Exception:
            log.debug("IB Gateway session stop failed", exc_info=True)


def wait_until(predicate: Callable[[], bool], timeout: float, step: float = 0.1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(step)
    return predicate()
