"""Interactive Brokers Client Portal Web API adapter.

Talks to the local Client Portal Gateway (default
``https://localhost:5000/v1/api``), which is IBKR's current REST interface.
The legacy TWS socket API is deliberately not used: the Web API needs no
JVM, no desktop session, and works headlessly on a server.

API surface used:

  POST /tickle                                   (session keepalive, ~60s)
  POST /iserver/auth/status
  GET  /iserver/accounts                         (required before snapshots)
  GET  /portfolio/accounts                       (required before portfolio)
  GET  /portfolio/{acct}/positions/{page}
  GET  /portfolio/{acct}/ledger
  GET  /iserver/secdef/search                    (symbol -> conid)
  GET  /iserver/marketdata/snapshot
  GET  /iserver/account/orders
  POST /iserver/account/{acct}/orders
  POST /iserver/reply/{replyId}                  (order confirmation prompts)
  GET  /iserver/account/order/status/{orderId}
  POST /iserver/account/{acct}/order/{orderId}   (modify)
  DELETE /iserver/account/{acct}/order/{orderId}

Behaviours this adapter compensates for:
  * The first ``/iserver/marketdata/snapshot`` call after subscribing returns
    no fields; it must be polled again. We do that automatically.
  * Snapshot prices arrive as strings and may carry a prefix character
    (``C`` = previous close, ``H`` = halted).
  * Order submission can return a list of *questions* that must each be
    confirmed via ``/iserver/reply/{id}`` before the order exists.
  * The brokerage session dies after ~5 minutes of silence unless tickled.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any

import httpx

from ..config import Settings
from ..errors import (
    BrokerAuthError,
    BrokerConnectionError,
    BrokerTimeout,
    ConfigError,
    InvalidQuoteError,
    OrderRejected,
    RateLimited,
)
from ..models import (
    Account,
    Balances,
    Order,
    OrderState,
    Position,
    Quote,
    Side,
)
from ..performance import SOURCE_BROKER, NavPoint
from .base import Broker, normalize_state

log = logging.getLogger("trading.broker.ibkr")


def _pa_date(raw: Any) -> str | None:
    """Portfolio Analyst dates arrive as YYYYMMDD or YYYY-MM-DD."""
    text = str(raw).strip()
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text
    return None

# Snapshot field ids.
F_LAST = "31"
F_BID = "84"
F_ASK_SIZE = "85"
F_ASK = "86"
F_BID_SIZE = "88"
F_AVAILABILITY = "6509"
SNAPSHOT_FIELDS = ",".join([F_LAST, F_BID, F_ASK_SIZE, F_ASK, F_BID_SIZE,
                            F_AVAILABILITY])

STATUS_MAP: dict[str, OrderState] = {
    "FILLED": OrderState.FILLED,
    "CANCELLED": OrderState.CANCELLED,
    "APICANCELLED": OrderState.CANCELLED,
    "REJECTED": OrderState.REJECTED,
    "INACTIVE": OrderState.REJECTED,
    "SUBMITTED": OrderState.WORKING,
    "PRESUBMITTED": OrderState.WORKING,
    "PENDINGSUBMIT": OrderState.PENDING,
    "PENDINGCANCEL": OrderState.WORKING,   # still live until confirmed
    "WARNSTATE": OrderState.WORKING,
}

#: Order-confirmation prompts we are willing to answer automatically. Any
#: prompt outside this list stops the order, because IBKR uses the same
#: mechanism for benign notices and for genuine risk warnings.
SAFE_QUESTION_PATTERNS = (
    r"price.{0,40}exceeds the percentage constraint",
    r"exceeds the size constraint",
    r"are you sure you want to submit this order",
    r"confirm.*order",
    r"this order will most likely trigger",
    r"orders that are outside of.*regular trading hours",
    r"the following order .*size.*exceeds",
    r"mandatory cap price",
)

#: IBKR paper account identifiers start with D (DU/DF); live start with U.
PAPER_PREFIXES = ("DU", "DF", "D")


class IBKRBroker(Broker):
    broker_type = "ibkr"
    name = "ibkr"

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        *,
        account: Account | None = None,
    ) -> None:
        super().__init__(settings)
        if account is not None:
            self._account = account
            self.name = f"ibkr:{account.account_id}"
        self.base_url = settings.ibkr_base_url.rstrip("/")
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=settings.http_timeout_seconds,
            verify=settings.ibkr_verify_ssl,
            headers={"Accept": "application/json", "User-Agent": "qqq-switch/1.0"},
        )
        self._owns_client = client is None
        self._conids: dict[str, int] = dict(settings.ibkr_conid_overrides)
        self._portfolio_primed = False
        self._iserver_primed = False
        self._subscribed: set[int] = set()
        self._lock = threading.RLock()
        self._last_tickle = 0.0
        #: conid remembered per order so modify/cancel can rebuild payloads.
        self._order_conids: dict[str, int] = {}
        self._order_meta: dict[str, dict[str, Any]] = {}

    # --- HTTP -------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: Any = None,
        retries: int = 2,
    ) -> Any:
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._client.request(
                    method, path, params=params, json=json_body
                )
            except httpx.TimeoutException as exc:
                last_exc = BrokerTimeout(
                    f"IBKR {method} {path} timed out after "
                    f"{self.settings.http_timeout_seconds}s"
                )
                if method in ("POST", "DELETE"):
                    raise last_exc from exc
            except httpx.HTTPError as exc:
                last_exc = BrokerConnectionError(
                    f"IBKR {method} {path}: {exc}. Is the Client Portal "
                    f"Gateway running at {self.base_url}?"
                )
            else:
                if resp.status_code == 429:
                    if attempt >= retries:
                        raise RateLimited(f"IBKR rate limited {method} {path}", 1.0)
                    time.sleep(1.0 + attempt)
                    continue
                if resp.status_code == 401:
                    raise BrokerAuthError(
                        "IBKR gateway session is not authenticated. Open "
                        f"{self.base_url.replace('/v1/api', '')} and log in."
                    )
                if resp.status_code >= 500:
                    if attempt >= retries or method in ("POST", "DELETE"):
                        raise BrokerConnectionError(
                            f"IBKR server error {resp.status_code} on {method} {path}"
                        )
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if resp.status_code >= 400:
                    raise OrderRejected(
                        f"IBKR {method} {path} failed (HTTP {resp.status_code}): "
                        f"{resp.text[:400]}"
                    )
                if not resp.content:
                    return None
                try:
                    return resp.json()
                except ValueError:
                    return resp.text
            time.sleep(0.3 * (attempt + 1))
        raise last_exc or BrokerConnectionError(f"IBKR {method} {path} failed")

    # --- session ----------------------------------------------------------

    def tickle(self) -> None:
        """Keep the brokerage session alive. Cheap; call it before reads."""
        if time.time() - self._last_tickle < 45:
            return
        try:
            self._request("POST", "/tickle", retries=0)
            self._last_tickle = time.time()
        except Exception as exc:  # a failed tickle is not itself fatal
            log.debug("IBKR tickle failed: %s", exc)

    def auth_status(self) -> dict[str, Any]:
        data = self._request("POST", "/iserver/auth/status", retries=1)
        return data if isinstance(data, dict) else {}

    def connect(self) -> None:
        try:
            status = self.auth_status()
            if not status.get("authenticated"):
                if status.get("connected"):
                    # Session exists but the brokerage leg dropped; IBKR can
                    # re-establish it without a new login.
                    self._request("POST", "/iserver/reauthenticate", retries=1)
                    time.sleep(1.5)
                    status = self.auth_status()
            if not status.get("authenticated"):
                raise BrokerAuthError(
                    "IBKR gateway is not authenticated. Start the Client "
                    "Portal Gateway and log in via the browser, then retry."
                )
            if status.get("competing"):
                raise BrokerAuthError(
                    "Another IBKR session is competing for this account. "
                    "Log out of TWS/other API sessions before trading."
                )

            if self._account is None:
                accounts = self.get_accounts()
                if not accounts:
                    raise BrokerAuthError("IBKR returned no accounts")
                self._account = self._select_account(accounts)
                self.name = f"ibkr:{self._account.account_id}"
            else:
                self.get_accounts()      # primes /iserver for snapshots
            self._connected = True
            self._last_error = None
            log.info(
                "IBKR connected, account %s (%s)",
                self._account.display_id,
                "PAPER" if self._account.is_paper else "LIVE",
            )
        except Exception as exc:
            self._connected = False
            self._last_error = str(exc)
            raise

    # --- history (for performance) ----------------------------------------

    def get_broker_performance(self, months: int = 6) -> list[NavPoint] | None:
        """IBKR's own Portfolio Analyst NAV series.

        Rate limited by IBKR to roughly one request per 15 minutes because
        the calculation is expensive, so callers must cache the result.

        The response shape is not fully documented publicly, so this parses
        defensively: it looks for a NAV block with parallel ``dates`` and
        per-account ``navs`` arrays, and returns ``None`` — meaning "fall
        back to reconstruction" — rather than raising if the shape differs.
        """
        period = f"{max(1, int(months))}M"
        try:
            body = self._request(
                "POST",
                "/pa/performance",
                json_body={
                    "acctIds": [self._acct_id],
                    "period": period,
                    "freq": "D",
                },
                retries=0,
            )
        except Exception as exc:
            log.info("IBKR /pa/performance unavailable (%s); will reconstruct", exc)
            return None

        if not isinstance(body, dict):
            return None

        nav_block = body.get("nav") or {}
        dates = nav_block.get("dates") or []
        rows = nav_block.get("data") or []
        if not dates or not rows:
            log.info("IBKR /pa/performance returned no NAV series")
            return None

        row = next(
            (r for r in rows
             if str(r.get("idAccount", r.get("id", ""))).upper()
             == self._acct_id.upper()),
            rows[0] if rows else None,
        )
        if not isinstance(row, dict):
            return None
        navs = row.get("navs") or row.get("nav") or row.get("values") or []
        if len(navs) != len(dates):
            log.info(
                "IBKR /pa/performance dates (%d) and navs (%d) disagree; "
                "falling back to reconstruction", len(dates), len(navs),
            )
            return None

        points: list[NavPoint] = []
        for raw_date, raw_nav in zip(dates, navs):
            day = _pa_date(raw_date)
            if day is None:
                continue
            try:
                value = float(raw_nav)
            except (TypeError, ValueError):
                continue
            points.append(
                NavPoint(date=day, nav=value, cash=0.0, positions_value=value,
                         external_flow=0.0, source=SOURCE_BROKER)
            )
        return points or None

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

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    @property
    def _acct_id(self) -> str:
        if not self._account:
            raise BrokerAuthError("IBKR broker is not connected")
        return self._account.account_id

    # --- reads ------------------------------------------------------------

    def get_accounts(self) -> list[Account]:
        self.tickle()
        data = self._request("GET", "/iserver/accounts") or {}
        ids: list[str] = list(data.get("accounts", []) or [])
        aliases = (data.get("aliases") or {}) if isinstance(data, dict) else {}
        if not ids:
            portfolio = self._request("GET", "/portfolio/accounts") or []
            ids = [str(a.get("accountId") or a.get("id")) for a in portfolio]
        self._iserver_primed = True
        return [
            Account(
                account_id=str(a),
                display_id=str(aliases.get(a) or a),
                is_paper=is_paper_account(str(a)),
                account_type="PAPER" if is_paper_account(str(a)) else "LIVE",
            )
            for a in ids
            if a
        ]

    def _prime_portfolio(self) -> None:
        """/portfolio/accounts must be called before other portfolio routes."""
        if self._portfolio_primed:
            return
        self._request("GET", "/portfolio/accounts")
        self._portfolio_primed = True

    def get_positions(self) -> list[Position]:
        self.tickle()
        self._prime_portfolio()
        out: list[Position] = []
        page = 0
        while page < 20:  # 30 rows/page; 600 positions is a generous ceiling
            rows = self._request(
                "GET", f"/portfolio/{self._acct_id}/positions/{page}"
            ) or []
            if not isinstance(rows, list) or not rows:
                break
            for raw in rows:
                qty = float(raw.get("position", 0) or 0)
                if abs(qty) < 1e-9:
                    continue
                symbol = str(
                    raw.get("ticker")
                    or raw.get("contractDesc")
                    or ""
                ).upper().strip()
                conid = raw.get("conid")
                if conid:
                    try:
                        self._order_conids.setdefault(symbol, int(conid))
                    except (TypeError, ValueError):
                        pass
                out.append(
                    Position(
                        symbol=symbol,
                        quantity=qty,
                        average_price=float(
                            raw.get("avgPrice") or raw.get("avgCost") or 0
                        ),
                        market_value=float(raw.get("mktValue", 0) or 0),
                        settled_quantity=qty,
                    )
                )
            if len(rows) < 30:
                break
            page += 1
        return out

    def get_available_cash(self) -> Balances:
        self.tickle()
        self._prime_portfolio()
        ledger = self._request("GET", f"/portfolio/{self._acct_id}/ledger") or {}
        usd = ledger.get("USD") or ledger.get("BASE") or {}
        if not usd:
            raise BrokerConnectionError(
                "IBKR ledger returned no USD/BASE section"
            )

        def num(*keys: str) -> float:
            for k in keys:
                v = usd.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            return 0.0

        settled = num("settledcash")
        available = num("availablefunds", "cashbalance")
        # Cash-only means we spend settled cash, not funds that only exist
        # because IBKR is willing to lend against the portfolio.
        cash_available = min(available, settled) if settled > 0 else available

        return Balances(
            cash_available=cash_available,
            buying_power=num("buyingpower", "availablefunds"),
            cash_balance=num("cashbalance"),
            settled_cash=settled or None,
            currency="USD",
        )

    # --- contracts & quotes ----------------------------------------------

    def resolve_conid(self, symbol: str) -> int:
        symbol = symbol.upper()
        with self._lock:
            if symbol in self._conids:
                return self._conids[symbol]

        results = self._request(
            "GET",
            "/iserver/secdef/search",
            params={"symbol": symbol, "name": "false", "secType": "STK"},
        ) or []
        if not isinstance(results, list):
            raise ConfigError(f"IBKR contract search for {symbol} failed")

        for row in results:
            if str(row.get("symbol", "")).upper() != symbol:
                continue
            conid = row.get("conid")
            if conid is None:
                continue
            with self._lock:
                self._conids[symbol] = int(conid)
            log.info("IBKR resolved %s -> conid %s", symbol, conid)
            return int(conid)

        raise ConfigError(
            f"Could not resolve an IBKR conid for {symbol}. Set "
            f"IBKR_CONID_{symbol} explicitly to remove the ambiguity."
        )

    def get_quote(self, symbol: str) -> Quote:
        self.tickle()
        if not self._iserver_primed:
            self.get_accounts()
        conid = self.resolve_conid(symbol)

        raw: dict[str, Any] = {}
        # The first snapshot call after subscribing returns an empty shell.
        for attempt in range(4):
            data = self._request(
                "GET",
                "/iserver/marketdata/snapshot",
                params={"conids": str(conid), "fields": SNAPSHOT_FIELDS},
            ) or []
            if isinstance(data, list) and data:
                raw = data[0]
                if F_BID in raw and F_ASK in raw:
                    break
            time.sleep(0.35 * (attempt + 1))

        if not raw or F_BID not in raw or F_ASK not in raw:
            raise InvalidQuoteError(
                f"IBKR returned no bid/ask for {symbol} (conid {conid}). "
                "Check that the account has a market-data subscription for "
                "US equities."
            )

        self._subscribed.add(conid)
        quote = _parse_snapshot(symbol, raw)
        if quote.delayed and not self.settings.ibkr_allow_delayed_quotes:
            raise InvalidQuoteError(
                f"IBKR is returning delayed data for {symbol}. Trading on "
                "delayed prices is refused; subscribe to real-time US equity "
                "data or set IBKR_ALLOW_DELAYED_QUOTES=true to accept the risk."
            )
        return quote

    def unsubscribe_all(self) -> None:
        try:
            self._request("GET", "/iserver/marketdata/unsubscribeall", retries=0)
        except Exception:
            pass

    # --- orders -----------------------------------------------------------

    def get_open_orders(self) -> list[Order]:
        self.tickle()
        data = self._request("GET", "/iserver/account/orders") or {}
        rows = data.get("orders", []) if isinstance(data, dict) else []
        out = []
        for raw in rows or []:
            order = _parse_live_order(raw, self.name)
            if order.state.is_open:
                out.append(order)
        return out

    def get_order_status(self, order_id: str) -> Order:
        self.tickle()
        raw = self._request(
            "GET", f"/iserver/account/order/status/{order_id}", retries=1
        ) or {}
        if not isinstance(raw, dict) or not raw:
            # Fall back to the live-orders list, which is sometimes the only
            # place a just-submitted order appears.
            for order in self.get_open_orders():
                if order.order_id == str(order_id):
                    return order
            return Order(
                order_id=str(order_id),
                symbol="",
                side=Side.BUY,
                quantity=0,
                limit_price=0,
                state=OrderState.UNKNOWN,
                broker=self.name,
                status_message="IBKR returned no status for this order",
            )
        return _parse_order_status(str(order_id), raw, self.name,
                                   self._order_meta.get(str(order_id), {}))

    def submit_limit_order(
        self, symbol: str, side: Side, quantity: int, limit_price: float
    ) -> Order:
        conid = self.resolve_conid(symbol)
        client_id = f"qs{int(time.time() * 1000) % 10_000_000_000}"
        payload = {
            "orders": [
                {
                    "conid": conid,
                    "orderType": "LMT",
                    "side": side.value,
                    "quantity": int(quantity),
                    "price": round(float(limit_price), 2),
                    "tif": "DAY",
                    "outsideRTH": bool(self.settings.allow_outside_rth),
                    "cOID": client_id,   # duplicate-submission guard at IBKR
                }
            ]
        }
        response = self._request(
            "POST", f"/iserver/account/{self._acct_id}/orders", json_body=payload
        )
        result = self._resolve_replies(response)
        order_id = _extract_order_id(result)
        if not order_id:
            raise OrderRejected(f"IBKR did not return an order id: {result!r}")

        self._order_conids[str(order_id)] = conid
        self._order_meta[str(order_id)] = {
            "symbol": symbol.upper(),
            "side": side,
            "conid": conid,
            "quantity": int(quantity),
            "limit_price": float(limit_price),
        }
        return Order(
            order_id=str(order_id),
            symbol=symbol.upper(),
            side=side,
            quantity=int(quantity),
            limit_price=float(limit_price),
            state=OrderState.PENDING,
            broker=self.name,
            client_order_id=client_id,
        )

    def replace_order(self, order_id: str, limit_price: float) -> Order:
        meta = self._order_meta.get(str(order_id), {})
        current = self.get_order_status(order_id)
        remaining = int(current.remaining_quantity) or int(meta.get("quantity", 0))
        if remaining <= 0:
            raise OrderRejected(f"IBKR order {order_id} has nothing left to modify")

        symbol = current.symbol or str(meta.get("symbol", ""))
        side = current.side if current.symbol else meta.get("side", Side.BUY)
        conid = self._order_conids.get(str(order_id)) or self.resolve_conid(symbol)

        payload = {
            "conid": conid,
            "orderType": "LMT",
            "side": side.value if isinstance(side, Side) else str(side),
            "quantity": remaining,
            "price": round(float(limit_price), 2),
            "tif": "DAY",
            "outsideRTH": bool(self.settings.allow_outside_rth),
        }
        response = self._request(
            "POST",
            f"/iserver/account/{self._acct_id}/order/{order_id}",
            json_body=payload,
        )
        result = self._resolve_replies(response)
        new_id = _extract_order_id(result) or str(order_id)

        self._order_conids[new_id] = conid
        self._order_meta[new_id] = {
            "symbol": symbol,
            "side": side,
            "conid": conid,
            "quantity": remaining,
            "limit_price": float(limit_price),
        }
        return Order(
            order_id=new_id,
            symbol=symbol,
            side=side if isinstance(side, Side) else Side.BUY,
            quantity=remaining,
            limit_price=float(limit_price),
            state=OrderState.PENDING,
            broker=self.name,
        )

    def cancel_order(self, order_id: str) -> None:
        self._request(
            "DELETE", f"/iserver/account/{self._acct_id}/order/{order_id}"
        )

    # --- confirmation prompts --------------------------------------------

    def _resolve_replies(self, response: Any, depth: int = 0) -> Any:
        """Answer IBKR's order-confirmation questions, but only safe ones."""
        if depth > 5:
            raise OrderRejected(
                "IBKR kept asking confirmation questions; aborting to avoid "
                "an unbounded confirmation loop"
            )
        if isinstance(response, dict) and response.get("error"):
            raise OrderRejected(f"IBKR rejected the order: {response['error']}")
        if not isinstance(response, list) or not response:
            return response

        first = response[0]
        if not isinstance(first, dict):
            return response
        if first.get("error"):
            raise OrderRejected(f"IBKR rejected the order: {first['error']}")

        reply_id = first.get("id")
        messages = first.get("message")
        if not reply_id or not messages:
            return response

        text = " ".join(str(m) for m in messages)
        if not self.settings.ibkr_auto_confirm_all_questions and not _is_safe_question(text):
            raise OrderRejected(
                "IBKR asked an unrecognised confirmation question and the "
                f"order was NOT placed: {text!r}. Review it manually; set "
                "IBKR_AUTO_CONFIRM_ALL_QUESTIONS=true only if you understand "
                "every prompt your account can raise."
            )

        log.info("IBKR confirmation prompt auto-answered: %s", text[:200])
        nxt = self._request(
            "POST", f"/iserver/reply/{reply_id}", json_body={"confirmed": True}
        )
        return self._resolve_replies(nxt, depth + 1)


# --- parsing helpers ---------------------------------------------------------


def is_paper_account(account_id: str) -> bool:
    return str(account_id).upper().startswith(PAPER_PREFIXES)


def _is_safe_question(text: str) -> bool:
    lowered = text.lower()
    return any(re.search(p, lowered) for p in SAFE_QUESTION_PATTERNS)


def _extract_order_id(result: Any) -> str | None:
    if isinstance(result, list) and result:
        first = result[0]
        if isinstance(first, dict):
            for key in ("order_id", "orderId", "id"):
                if first.get(key):
                    return str(first[key])
    if isinstance(result, dict):
        for key in ("order_id", "orderId", "id"):
            if result.get(key):
                return str(result[key])
    return None


_PRICE_PREFIX = re.compile(r"^[A-Za-z]+")


def _clean_price(value: Any) -> tuple[float, bool]:
    """Strip IBKR's price prefixes. Returns (price, halted)."""
    if value is None:
        return 0.0, False
    text = str(value).strip().replace(",", "")
    halted = text.upper().startswith("H")
    text = _PRICE_PREFIX.sub("", text)
    try:
        return float(text), halted
    except ValueError:
        return 0.0, halted


def _parse_snapshot(symbol: str, raw: dict[str, Any]) -> Quote:
    bid, halted_b = _clean_price(raw.get(F_BID))
    ask, halted_a = _clean_price(raw.get(F_ASK))
    last, halted_l = _clean_price(raw.get(F_LAST))

    availability = str(raw.get(F_AVAILABILITY, "") or "").upper()
    # Availability letters: R=realtime, D=delayed, Z=frozen, Y=frozen delayed.
    delayed = availability.startswith("D") or availability.startswith("Y")

    updated_ms = raw.get("_updated") or raw.get("updated")
    if updated_ms:
        timestamp = float(updated_ms) / 1000.0
    else:
        raise InvalidQuoteError(
            f"IBKR snapshot for {symbol} has no _updated timestamp; cannot "
            "verify freshness, refusing to trade on it"
        )

    return Quote(
        symbol=symbol.upper(),
        bid=bid,
        ask=ask,
        last=last,
        timestamp=timestamp,
        bid_size=int(_clean_price(raw.get(F_BID_SIZE))[0] or 0),
        ask_size=int(_clean_price(raw.get(F_ASK_SIZE))[0] or 0),
        delayed=delayed,
        halted=halted_b or halted_a or halted_l,
        source="ibkr",
    )


def _parse_live_order(raw: dict[str, Any], broker: str) -> Order:
    side_raw = str(raw.get("side", "BUY")).upper()
    total = float(raw.get("totalSize", 0) or raw.get("sizeAndFills", 0) or 0)
    filled = float(raw.get("filledQuantity", 0) or 0)
    return Order(
        order_id=str(raw.get("orderId", "")),
        symbol=str(raw.get("ticker", "")).upper(),
        side=Side.SELL if side_raw.startswith("S") else Side.BUY,
        quantity=total,
        limit_price=float(raw.get("price", 0) or 0),
        state=normalize_state(str(raw.get("status", "")), STATUS_MAP),
        filled_quantity=filled,
        average_fill_price=float(raw.get("avgPrice", 0) or 0),
        status_message=str(raw.get("order_ccp_status", "") or ""),
        broker=broker,
    )


def _parse_order_status(
    order_id: str, raw: dict[str, Any], broker: str, meta: dict[str, Any]
) -> Order:
    total = float(raw.get("size", 0) or meta.get("quantity", 0) or 0)
    filled = float(raw.get("cum_fill", 0) or 0)
    avg = float(raw.get("average_price", 0) or 0)
    side_raw = str(raw.get("side", "") or "").upper()
    if side_raw:
        side = Side.SELL if side_raw.startswith("S") else Side.BUY
    else:
        side = meta.get("side") or Side.BUY

    return Order(
        order_id=order_id,
        symbol=str(raw.get("symbol") or meta.get("symbol", "")).upper(),
        side=side if isinstance(side, Side) else Side.BUY,
        quantity=total,
        limit_price=float(raw.get("limit_price", 0) or meta.get("limit_price", 0) or 0),
        state=normalize_state(str(raw.get("order_status", "")), STATUS_MAP),
        filled_quantity=filled,
        average_fill_price=avg,
        status_message=str(raw.get("order_status_desc") or raw.get("text") or ""),
        broker=broker,
    )
