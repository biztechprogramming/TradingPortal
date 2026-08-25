"""Charles Schwab Trader API adapter.

API surface used (verified against the current Trader API, not the retired
TD Ameritrade API):

  GET    /trader/v1/accounts/accountNumbers
  GET    /trader/v1/accounts/{hash}?fields=positions
  GET    /trader/v1/accounts/{hash}/orders
  POST   /trader/v1/accounts/{hash}/orders          -> id in Location header
  GET    /trader/v1/accounts/{hash}/orders/{id}
  PUT    /trader/v1/accounts/{hash}/orders/{id}     -> new id in Location
  DELETE /trader/v1/accounts/{hash}/orders/{id}
  GET    /marketdata/v1/quotes?symbols=...
  POST   /v1/oauth/token                            (refresh_token grant)

Notes that shaped this implementation:
  * Account numbers are never used directly in URLs; Schwab issues an opaque
    ``hashValue`` per account and that is what the order endpoints take.
  * ``POST``/``PUT`` return an empty body. The new order id is in the
    ``Location`` response header.
  * Access tokens last ~30 minutes; refresh tokens last 7 days and cannot be
    extended, so the operator must re-authorise weekly.
  * Order-mutating requests are rate limited per account (up to 120/min).
  * There is no paper-trading environment for individual Trader API
    accounts. This adapter therefore reports ``is_paper=False`` always, and
    the broker factory refuses to send it real orders outside live mode.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import stat
import threading
import time
from collections import deque
from datetime import date, datetime, time as dtime, timezone
from pathlib import Path
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
    Bar,
    Balances,
    Order,
    OrderState,
    Position,
    Quote,
    Side,
    Transaction,
)
from .base import Broker, mask_account, normalize_state

#: Activity types worth pulling for NAV reconstruction. Trades move value
#: between cash and positions; the transfer types are the ones that change
#: account value without being performance.
TRANSACTION_TYPES = (
    "TRADE",
    "RECEIVE_AND_DELIVER",
    "DIVIDEND_OR_INTEREST",
    "ACH_RECEIPT",
    "ACH_DISBURSEMENT",
    "CASH_RECEIPT",
    "CASH_DISBURSEMENT",
    "ELECTRONIC_FUND",
    "WIRE_IN",
    "WIRE_OUT",
    "JOURNAL",
)

#: Types that represent money crossing the account boundary.
EXTERNAL_FLOW_TYPES = frozenset({
    "ACH_RECEIPT", "ACH_DISBURSEMENT", "CASH_RECEIPT", "CASH_DISBURSEMENT",
    "ELECTRONIC_FUND", "WIRE_IN", "WIRE_OUT", "JOURNAL",
})

log = logging.getLogger("trading.broker.schwab")

STATUS_MAP: dict[str, OrderState] = {
    "FILLED": OrderState.FILLED,
    "CANCELED": OrderState.CANCELLED,
    "CANCELLED": OrderState.CANCELLED,
    "REPLACED": OrderState.CANCELLED,      # superseded by the replacement
    "REJECTED": OrderState.REJECTED,
    "EXPIRED": OrderState.EXPIRED,
    "WORKING": OrderState.WORKING,
    "QUEUED": OrderState.WORKING,
    "ACCEPTED": OrderState.WORKING,
    "NEW": OrderState.PENDING,
    "PENDING_ACTIVATION": OrderState.PENDING,
    "PENDING_ACKNOWLEDGEMENT": OrderState.PENDING,
    "AWAITING_PARENT_ORDER": OrderState.PENDING,
    "AWAITING_CONDITION": OrderState.PENDING,
    "AWAITING_STOP_CONDITION": OrderState.PENDING,
    "AWAITING_MANUAL_REVIEW": OrderState.PENDING,
    "AWAITING_UR_OUT": OrderState.WORKING,
    "AWAITING_RELEASE_TIME": OrderState.PENDING,
    "PENDING_CANCEL": OrderState.WORKING,   # still live until confirmed
    "PENDING_REPLACE": OrderState.WORKING,
    "PENDING_RECALL": OrderState.WORKING,
}

#: Schwab documents up to 120 order requests/min/account. We stay under it.
MAX_REQUESTS_PER_MINUTE = 100


class _TokenStore:
    """Reads/writes the OAuth token file. Never logs token material."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, Any] = {}

    def load(self) -> dict[str, Any]:
        with self._lock:
            if not self.path.exists():
                raise BrokerAuthError(
                    f"Schwab token file not found at {self.path}. Run "
                    "`python -m trading.cli schwab-login` to authorise."
                )
            try:
                self._data = json.loads(self.path.read_text())
            except (OSError, json.JSONDecodeError) as exc:
                raise BrokerAuthError(
                    f"Schwab token file at {self.path} is unreadable"
                ) from exc
            return self._data

    def save(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._data = data
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            tmp.replace(self.path)

    @property
    def data(self) -> dict[str, Any]:
        return self._data


class SchwabBroker(Broker):
    """One Schwab *account*, reached through one Schwab *login*.

    A single developer app can be authorised by several Schwab user IDs, and
    each authorisation can expose several brokerage accounts. Every
    (login, account) pair gets its own instance so that commands, results and
    audit records are always attributable to exactly one account.
    """

    broker_type = "schwab"

    def __init__(
        self,
        settings: Settings,
        client: httpx.Client | None = None,
        *,
        login: str = "default",
        account: Account | None = None,
    ) -> None:
        super().__init__(settings)
        self.login = login
        self.base_url = settings.schwab_base_url.rstrip("/")
        self._tokens = _TokenStore(settings.schwab_token_path_for(login))
        if account is not None:
            self._account = account
            self._account_hash = account.account_id
        self.name = _instance_name(login, account)
        self._client = client or httpx.Client(
            base_url=self.base_url,
            timeout=settings.http_timeout_seconds,
            headers={"Accept": "application/json"},
        )
        self._owns_client = client is None
        self._account_hash: str | None = None
        self._req_times: deque[float] = deque()
        self._rate_lock = threading.Lock()
        self._auth_lock = threading.Lock()

    # --- auth -------------------------------------------------------------

    def _basic_auth_header(self) -> str:
        key = self.settings.schwab_app_key
        secret = self.settings.schwab_app_secret
        if not key or not secret:
            raise ConfigError(
                "SCHWAB_APP_KEY and SCHWAB_APP_SECRET must be set in the "
                "environment."
            )
        raw = f"{key}:{secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    def _access_token(self) -> str:
        """Return a valid access token, refreshing with 60s of headroom."""
        with self._auth_lock:
            data = self._tokens.data or self._tokens.load()
            expires_at = float(data.get("expires_at", 0))
            if data.get("access_token") and time.time() < expires_at - 60:
                return str(data["access_token"])
            return self._refresh(data)

    def _refresh(self, data: dict[str, Any]) -> str:
        refresh_token = data.get("refresh_token")
        if not refresh_token:
            raise BrokerAuthError(
                "No Schwab refresh token stored; re-run `schwab-login`."
            )
        try:
            resp = httpx.post(
                f"{self.base_url}/v1/oauth/token",
                headers={
                    "Authorization": self._basic_auth_header(),
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                timeout=self.settings.http_timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise BrokerTimeout(f"Schwab token refresh timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise BrokerConnectionError(f"Schwab token refresh failed: {exc}") from exc

        if resp.status_code != 200:
            # Deliberately does not include the response body, which echoes
            # token material on some error paths.
            raise BrokerAuthError(
                f"Schwab token refresh rejected (HTTP {resp.status_code}). "
                "Refresh tokens expire after 7 days; re-run `schwab-login`."
            )
        payload = resp.json()
        new_data = {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token", refresh_token),
            "expires_at": time.time() + float(payload.get("expires_in", 1800)),
            "obtained_at": time.time(),
        }
        self._tokens.save(new_data)
        log.info("Schwab access token refreshed (expires in %ss)",
                 int(payload.get("expires_in", 1800)))
        return str(new_data["access_token"])

    # --- HTTP -------------------------------------------------------------

    def _throttle(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            while self._req_times and now - self._req_times[0] > 60:
                self._req_times.popleft()
            if len(self._req_times) >= MAX_REQUESTS_PER_MINUTE:
                sleep_for = 60 - (now - self._req_times[0]) + 0.05
                log.warning("Schwab rate limit reached; sleeping %.1fs", sleep_for)
                time.sleep(max(0.0, sleep_for))
            self._req_times.append(time.monotonic())

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        retries: int = 2,
    ) -> httpx.Response:
        self._throttle()
        headers = {"Authorization": f"Bearer {self._access_token()}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = self._client.request(
                    method, path, params=params, json=json_body, headers=headers
                )
            except httpx.TimeoutException as exc:
                last_exc = BrokerTimeout(
                    f"Schwab {method} {path} timed out after "
                    f"{self.settings.http_timeout_seconds}s"
                )
                # A timed-out mutation may still have been applied; do not
                # blindly retry it.
                if method in ("POST", "PUT", "DELETE"):
                    raise last_exc from exc
            except httpx.HTTPError as exc:
                last_exc = BrokerConnectionError(f"Schwab {method} {path}: {exc}")
            else:
                if resp.status_code == 429:
                    retry_after = float(resp.headers.get("Retry-After", 2))
                    if attempt >= retries:
                        raise RateLimited(
                            f"Schwab rate limited {method} {path}", retry_after
                        )
                    time.sleep(retry_after)
                    continue
                if resp.status_code in (401, 403):
                    raise BrokerAuthError(
                        f"Schwab rejected credentials on {method} {path} "
                        f"(HTTP {resp.status_code})"
                    )
                if resp.status_code >= 500:
                    if attempt >= retries or method in ("POST", "PUT", "DELETE"):
                        raise BrokerConnectionError(
                            f"Schwab server error {resp.status_code} on "
                            f"{method} {path}"
                        )
                    time.sleep(0.5 * (attempt + 1))
                    continue
                if resp.status_code >= 400:
                    raise OrderRejected(
                        f"Schwab {method} {path} failed "
                        f"(HTTP {resp.status_code}): {_safe_body(resp)}"
                    )
                return resp
            time.sleep(0.3 * (attempt + 1))

        raise last_exc or BrokerConnectionError(f"Schwab {method} {path} failed")

    # --- lifecycle --------------------------------------------------------

    def connect(self) -> None:
        try:
            self._tokens.load()
            if self._account is None:
                # Standalone use (no factory): fall back to selecting one.
                accounts = self.get_accounts()
                if not accounts:
                    raise BrokerAuthError("Schwab returned no linked accounts")
                self._account = self._select_account(accounts)
                self.name = _instance_name(self.login, self._account)
            self._account_hash = self._account.account_id
            # Prove the token actually works before reporting a connection.
            self._account_payload(None)
            self._connected = True
            self._last_error = None
            log.info(
                "Schwab connected: login=%s account=%s",
                self.login, self._account.display_id,
            )
        except Exception as exc:
            self._connected = False
            self._last_error = str(exc)
            raise

    def _select_account(self, accounts: list[Account]) -> Account:
        want = (self.settings.schwab_account_number or "").strip()
        if want:
            for acct in accounts:
                # ``display_id`` holds the masked real account number.
                if acct.display_id.endswith(want[-4:]) or acct.account_id == want:
                    return acct
            raise ConfigError(
                f"SCHWAB_ACCOUNT_NUMBER={mask_account(want)} did not match any "
                f"linked account ({[a.display_id for a in accounts]})"
            )
        if len(accounts) > 1:
            raise ConfigError(
                "Multiple Schwab accounts are linked; set SCHWAB_ACCOUNT_NUMBER "
                "so the system cannot trade the wrong one. Found: "
                f"{[a.display_id for a in accounts]}"
            )
        return accounts[0]

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # --- reads ------------------------------------------------------------

    def get_accounts(self) -> list[Account]:
        resp = self._request("GET", "/trader/v1/accounts/accountNumbers")
        out: list[Account] = []
        for item in resp.json() or []:
            number = str(item.get("accountNumber", ""))
            out.append(
                Account(
                    account_id=str(item.get("hashValue", "")),
                    display_id=mask_account(number),
                    # Schwab's Trader API has no paper environment.
                    is_paper=False,
                    account_type="",
                )
            )
        return out

    def _account_payload(self, fields: str | None = "positions") -> dict[str, Any]:
        if not self._account_hash:
            raise BrokerAuthError("Schwab broker is not connected")
        params = {"fields": fields} if fields else None
        resp = self._request(
            "GET", f"/trader/v1/accounts/{self._account_hash}", params=params
        )
        body = resp.json()
        if isinstance(body, list):  # some deployments wrap in a list
            body = body[0] if body else {}
        return body.get("securitiesAccount", body) or {}

    def get_positions(self) -> list[Position]:
        payload = self._account_payload("positions")
        out: list[Position] = []
        for raw in payload.get("positions", []) or []:
            instrument = raw.get("instrument", {}) or {}
            symbol = str(instrument.get("symbol", "")).upper()
            if not symbol:
                continue
            long_qty = float(raw.get("longQuantity", 0) or 0)
            short_qty = float(raw.get("shortQuantity", 0) or 0)
            qty = long_qty - short_qty
            if abs(qty) < 1e-9:
                continue
            settled = raw.get("settledLongQuantity")
            out.append(
                Position(
                    symbol=symbol,
                    quantity=qty,
                    average_price=float(raw.get("averagePrice", 0) or 0),
                    market_value=float(raw.get("marketValue", 0) or 0),
                    settled_quantity=(
                        float(settled) if settled is not None else None
                    ),
                )
            )
        return out

    def get_available_cash(self) -> Balances:
        payload = self._account_payload(None)
        current = payload.get("currentBalances", {}) or {}
        account_type = str(payload.get("type", "")).upper()

        def num(*keys: str) -> float:
            for k in keys:
                v = current.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        continue
            return 0.0

        # For a margin account we want the money spendable without borrowing.
        #
        # NOT availableFundsNonMarginableTrade: despite the name, Schwab
        # reports that as *excess* funds, and it collapses to 0 as soon as the
        # account holds marginable stock (maintenanceRequirement then equals
        # longMarketValue). Observed live: an account with $487.88 cash and
        # QQQ reported 0.0, while the same field on a flat account was
        # correct. Sizing off it would deploy nothing whenever we are already
        # invested — which is exactly when GO LONG needs to add idle cash.
        #
        # buyingPowerNonMarginableTrade is the buying power for a security
        # requiring 100% cash, which is the figure we want. It is bounded by
        # cashBalance so a future Schwab change cannot let us spend borrowed
        # money, and floored at zero because cashBalance goes negative when a
        # margin loan is outstanding.
        if account_type == "MARGIN":
            cash_available = max(0.0, min(
                num("buyingPowerNonMarginableTrade", "cashBalance"),
                num("cashBalance"),
            ))
        else:
            cash_available = num("cashAvailableForTrading", "cashBalance")

        return Balances(
            cash_available=cash_available,
            buying_power=num("buyingPower", "availableFunds", "cashAvailableForTrading"),
            cash_balance=num("cashBalance"),
            settled_cash=(
                num("cashAvailableForTrading") if account_type != "MARGIN" else None
            ),
        )

    def get_quote(self, symbol: str) -> Quote:
        resp = self._request(
            "GET", "/marketdata/v1/quotes", params={"symbols": symbol.upper()}
        )
        body = resp.json() or {}
        entry = body.get(symbol.upper())
        if not entry:
            raise InvalidQuoteError(f"Schwab returned no quote for {symbol}")
        return _parse_quote(symbol.upper(), entry)

    def get_open_orders(self) -> list[Order]:
        if not self._account_hash:
            raise BrokerAuthError("Schwab broker is not connected")
        now = time.time()
        params = {
            "fromEnteredTime": _iso(now - 7 * 86400),
            "toEnteredTime": _iso(now + 86400),
            "maxResults": 200,
        }
        resp = self._request(
            "GET", f"/trader/v1/accounts/{self._account_hash}/orders", params=params
        )
        orders = [_parse_order(o, self.name) for o in (resp.json() or [])]
        return [o for o in orders if o.state.is_open]

    def get_order_status(self, order_id: str) -> Order:
        resp = self._request(
            "GET", f"/trader/v1/accounts/{self._account_hash}/orders/{order_id}"
        )
        return _parse_order(resp.json() or {}, self.name)

    # --- writes -----------------------------------------------------------

    def submit_limit_order(
        self, symbol: str, side: Side, quantity: int, limit_price: float
    ) -> Order:
        body = build_order_payload(symbol, side, quantity, limit_price)
        resp = self._request(
            "POST",
            f"/trader/v1/accounts/{self._account_hash}/orders",
            json_body=body,
        )
        order_id = _order_id_from_location(resp)
        if not order_id:
            raise OrderRejected(
                "Schwab accepted the order but returned no Location header; "
                "the order id is unknown. Reconcile open orders before "
                "retrying."
            )
        return Order(
            order_id=order_id,
            symbol=symbol.upper(),
            side=side,
            quantity=quantity,
            limit_price=limit_price,
            state=OrderState.PENDING,
            broker=self.name,
        )

    def replace_order(self, order_id: str, limit_price: float) -> Order:
        """Schwab replaces atomically; the response carries a *new* order id.

        The replacement is submitted for the quantity still unfilled, which
        we read back from the live order rather than assuming.
        """
        existing = self.get_order_status(order_id)
        remaining = int(existing.remaining_quantity)
        if remaining <= 0:
            raise OrderRejected(
                f"Schwab order {order_id} has nothing left to replace"
            )
        body = build_order_payload(
            existing.symbol, existing.side, remaining, limit_price
        )
        resp = self._request(
            "PUT",
            f"/trader/v1/accounts/{self._account_hash}/orders/{order_id}",
            json_body=body,
        )
        new_id = _order_id_from_location(resp) or order_id
        return Order(
            order_id=new_id,
            symbol=existing.symbol,
            side=existing.side,
            quantity=remaining,
            limit_price=limit_price,
            state=OrderState.PENDING,
            broker=self.name,
        )

    def cancel_order(self, order_id: str) -> None:
        self._request(
            "DELETE", f"/trader/v1/accounts/{self._account_hash}/orders/{order_id}"
        )


    # --- history (for performance reconstruction) -------------------------

    def get_transactions(self, start: date, end: date) -> list[Transaction] | None:
        """Activity for one account.

        Schwab returns roughly the last 180 days, which is exactly the span
        the 6-month performance window needs.
        """
        resp = self._request(
            "GET",
            f"/trader/v1/accounts/{self._account_hash}/transactions",
            params={
                "startDate": _iso(
                    datetime.combine(start, dtime.min, timezone.utc).timestamp()
                ),
                "endDate": _iso(
                    datetime.combine(end, dtime.max, timezone.utc).timestamp()
                ),
                "types": ",".join(TRANSACTION_TYPES),
            },
        )
        body = resp.json() or []
        if not isinstance(body, list):
            return []
        return [t for t in (_parse_transaction(raw) for raw in body) if t]

    def get_price_bars(
        self,
        symbol: str,
        start: float,
        end: float,
        *,
        bar_size: str = "1h",
        outside_rth: bool = True,
    ) -> list[Bar] | None:
        """Hourly candles, aggregated up from Schwab's 30-minute bars.

        Schwab's ``frequencyType=minute`` tops out at a 30-minute bar, so an
        hourly series has to be built here. Bars are bucketed by the UTC hour
        of their opening timestamp, which keeps them aligned with IBKR's
        hourly bars so the two can share one cache table.
        """
        if bar_size != "1h":
            return None

        resp = self._request(
            "GET",
            "/marketdata/v1/pricehistory",
            params={
                "symbol": symbol.upper(),
                "periodType": "day",
                "frequencyType": "minute",
                "frequency": 30,
                "startDate": int(start * 1000),
                "endDate": int(end * 1000),
                "needExtendedHoursData": "true" if outside_rth else "false",
            },
        )
        body = resp.json() or {}

        buckets: dict[int, list[dict[str, Any]]] = {}
        for candle in body.get("candles", []) or []:
            stamp = candle.get("datetime")
            if not stamp:
                continue
            ts = int(float(stamp) / 1000.0)
            if ts < start or ts > end:
                continue
            buckets.setdefault(ts - (ts % 3600), []).append(candle)

        bars: list[Bar] = []
        for hour, group in sorted(buckets.items()):
            group.sort(key=lambda c: c.get("datetime", 0))
            try:
                bars.append(
                    Bar(
                        symbol=symbol.upper(),
                        ts=hour,
                        open=float(group[0].get("open", 0) or 0),
                        high=max(float(c.get("high", 0) or 0) for c in group),
                        low=min(float(c.get("low", 0) or 0) for c in group),
                        close=float(group[-1].get("close", 0) or 0),
                        volume=sum(float(c.get("volume", 0) or 0) for c in group),
                    )
                )
            except (TypeError, ValueError):
                continue
        return bars

    def get_price_history(
        self, symbol: str, start: date, end: date
    ) -> dict[str, float] | None:
        """Daily closes, keyed by ``YYYY-MM-DD``."""
        resp = self._request(
            "GET",
            "/marketdata/v1/pricehistory",
            params={
                "symbol": symbol.upper(),
                "periodType": "year",
                "frequencyType": "daily",
                "frequency": 1,
                "startDate": int(
                    datetime.combine(start, dtime.min, timezone.utc).timestamp() * 1000
                ),
                "endDate": int(
                    datetime.combine(end, dtime.max, timezone.utc).timestamp() * 1000
                ),
                "needExtendedHoursData": "false",
            },
        )
        body = resp.json() or {}
        closes: dict[str, float] = {}
        for candle in body.get("candles", []) or []:
            stamp = candle.get("datetime")
            close = candle.get("close")
            if stamp is None or close is None:
                continue
            day = datetime.fromtimestamp(
                float(stamp) / 1000.0, timezone.utc
            ).date().isoformat()
            closes[day] = float(close)
        return closes


def discover_schwab_accounts(
    settings: Settings, login: str, client: httpx.Client | None = None
) -> list[Account]:
    """List the accounts one Schwab login has authorised for this app.

    Accounts the user did not tick during the consent flow simply do not
    appear here — that opt-in list is the only account-level permission
    control Schwab's individual Trader API offers.
    """
    probe = SchwabBroker(settings, client=client, login=login)
    try:
        return probe.get_accounts()
    finally:
        if client is None:
            probe.close()


def _instance_name(login: str, account: Account | None) -> str:
    tail = ""
    if account and account.display_id:
        tail = account.display_id.lstrip(".")
    if login == "default":
        return f"schwab:{tail}" if tail else "schwab"
    return f"schwab:{login}:{tail}" if tail else f"schwab:{login}"


# --- payload helpers ---------------------------------------------------------


def build_order_payload(
    symbol: str, side: Side, quantity: int, limit_price: float
) -> dict[str, Any]:
    """A single-leg equity day limit order, regular session only.

    ``SELL`` is used rather than ``SELL_SHORT``: this system only ever
    liquidates a long position, never establishes a short.
    """
    return {
        "orderType": "LIMIT",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "price": f"{limit_price:.2f}",
        "orderLegCollection": [
            {
                "instruction": "BUY" if side is Side.BUY else "SELL",
                "quantity": int(quantity),
                "instrument": {"symbol": symbol.upper(), "assetType": "EQUITY"},
            }
        ],
    }


def _order_id_from_location(resp: httpx.Response) -> str | None:
    location = resp.headers.get("Location") or resp.headers.get("location")
    if not location:
        return None
    return location.rstrip("/").split("/")[-1] or None


def _parse_order(raw: dict[str, Any], broker: str) -> Order:
    legs = raw.get("orderLegCollection") or [{}]
    leg = legs[0] if legs else {}
    instrument = leg.get("instrument", {}) or {}
    instruction = str(leg.get("instruction", "BUY")).upper()
    side = Side.SELL if instruction.startswith("SELL") else Side.BUY

    filled = float(raw.get("filledQuantity", 0) or 0)
    avg_price = _average_fill_price(raw, filled)

    return Order(
        order_id=str(raw.get("orderId", "")),
        symbol=str(instrument.get("symbol", "")).upper(),
        side=side,
        quantity=float(raw.get("quantity", 0) or 0),
        limit_price=float(raw.get("price", 0) or 0),
        state=normalize_state(str(raw.get("status", "")), STATUS_MAP),
        filled_quantity=filled,
        average_fill_price=avg_price,
        status_message=str(raw.get("statusDescription", "") or ""),
        broker=broker,
    )


def _average_fill_price(raw: dict[str, Any], filled: float) -> float:
    """Schwab reports executions as legs inside orderActivityCollection."""
    total_qty = 0.0
    total_notional = 0.0
    for activity in raw.get("orderActivityCollection") or []:
        for exec_leg in activity.get("executionLegs") or []:
            qty = float(exec_leg.get("quantity", 0) or 0)
            price = float(exec_leg.get("price", 0) or 0)
            total_qty += qty
            total_notional += qty * price
    if total_qty > 0:
        return total_notional / total_qty
    # Fall back to the order-level average when the activity list is absent.
    for key in ("avgFillPrice", "averageFillPrice"):
        if raw.get(key):
            return float(raw[key])
    return 0.0


def _parse_transaction(raw: dict[str, Any]) -> Transaction | None:
    """Normalise one Schwab activity record.

    Schwab nests the interesting parts in ``transferItems``: one entry per
    instrument leg plus, usually, a currency leg. We take the first equity
    leg for the share movement and the record's ``netAmount`` for cash.
    """
    stamp = raw.get("tradeDate") or raw.get("time") or raw.get("settlementDate")
    if not stamp:
        return None
    day = str(stamp)[:10]

    kind = str(raw.get("type", "OTHER")).upper()
    net_amount = float(raw.get("netAmount", 0) or 0)

    symbol = ""
    quantity = 0.0
    price = 0.0
    for item in raw.get("transferItems", []) or []:
        instrument = item.get("instrument", {}) or {}
        asset_type = str(instrument.get("assetType", "")).upper()
        if asset_type in ("CURRENCY", ""):
            continue
        symbol = str(instrument.get("symbol", "")).upper()
        quantity = float(item.get("amount", 0) or 0)
        price = float(item.get("price", 0) or 0)
        break

    if kind == "TRADE":
        normalised = "TRADE"
        external = 0.0            # a trade never changes account value
    elif kind == "DIVIDEND_OR_INTEREST":
        normalised = "DIVIDEND"
        external = 0.0            # income is performance, not a deposit
    elif kind in EXTERNAL_FLOW_TYPES:
        normalised = "TRANSFER"
        external = net_amount     # this is the money that must not count
    elif kind == "RECEIVE_AND_DELIVER":
        # Securities moved in or out; value crosses the boundary as shares.
        normalised = "TRANSFER"
        external = quantity * price
    else:
        normalised = "OTHER"
        external = 0.0

    return Transaction(
        date=day,
        type=normalised,
        symbol=symbol,
        quantity=quantity,
        price=price,
        net_amount=net_amount,
        external_flow=external,
        description=str(raw.get("description", "") or kind),
    )


def _parse_quote(symbol: str, entry: dict[str, Any]) -> Quote:
    quote = entry.get("quote", {}) or {}

    ts_ms = quote.get("quoteTime") or quote.get("tradeTime")
    if not ts_ms:
        raise InvalidQuoteError(
            f"Schwab quote for {symbol} has no quoteTime; cannot verify "
            "freshness, refusing to trade on it"
        )
    timestamp = float(ts_ms) / 1000.0

    status = str(
        quote.get("securityStatus") or entry.get("securityStatus") or ""
    ).upper()
    realtime = entry.get("realtime")

    return Quote(
        symbol=symbol,
        bid=float(quote.get("bidPrice", 0) or 0),
        ask=float(quote.get("askPrice", 0) or 0),
        last=float(quote.get("lastPrice", 0) or 0),
        timestamp=timestamp,
        bid_size=int(quote.get("bidSize", 0) or 0),
        ask_size=int(quote.get("askSize", 0) or 0),
        delayed=(realtime is False),
        halted=("HALT" in status or "CLOSED" == status),
        source="schwab",
        # Absent before the session's first trade; 0.0 then means "no anchor".
        day_high=float(quote.get("highPrice", 0) or 0),
        day_low=float(quote.get("lowPrice", 0) or 0),
    )


def _iso(epoch: float) -> str:
    from datetime import datetime, timezone

    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.000Z")
    )


def _safe_body(resp: httpx.Response) -> str:
    """Truncated response text for error messages, with tokens scrubbed."""
    try:
        text = resp.text[:400]
    except Exception:
        return "<unreadable body>"
    from ..audit import _scrub

    return _scrub(text)
