"""Local HTTP interface: dashboard plus a token-authenticated JSON API.

Binds to loopback by default. Every mutating route requires a bearer token,
so a Shortcut, Stream Deck macro or curl one-liner can drive it, but a stray
page in your browser cannot.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import anyio.to_thread
from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from . import audit as A
from .config import Settings, ensure_api_token, load_settings
from .errors import (
    ConfirmationRequired,
    DuplicateCommand,
    ExecutionLocked,
    MarketClosed,
    SafetyLimitExceeded,
    TradingError,
    UnreconciledState,
)
from .logins import IbkrGatewayRestart, LoginError, SchwabLoginFlow, ibeam_running, schwab_token_status
from .models import Command
from .orchestrator import TradingService

log = logging.getLogger("trading.api")

WEB_DIR = Path(__file__).parent / "web"

#: Changes on every restart, so the dashboard reloads after a server-side
#: (Python) change too, not only when a static asset is edited.
STARTED_AT = time.time()


class CommandRequest(BaseModel):
    command: str = Field(..., description="GO_LONG | GO_SHORT | GO_FLAT")
    request_id: str | None = Field(
        None,
        description=(
            "Client-supplied idempotency key. Re-sending the same id never "
            "executes twice."
        ),
    )
    confirm_token: str | None = Field(
        None, description="Token returned by a previous confirmation-required response"
    )
    force: bool = Field(
        False, description="Bypass the duplicate-command time window"
    )


class AcknowledgeRequest(BaseModel):
    note: str = ""


class SchwabLoginRequest(BaseModel):
    login: str = Field(..., description="A label from SCHWAB_LOGINS")


class SchwabPasteRequest(BaseModel):
    url: str = Field(..., description="The full URL Schwab redirected to")


class LadderOrderRequest(BaseModel):
    symbol: str = Field(..., description="Traded symbol, e.g. QQQ or PSQ")
    limit_price: float = Field(..., gt=0, description="Exact rung price to buy at")
    request_id: str | None = Field(
        None, description="Client-supplied idempotency key"
    )
    confirm_token: str | None = Field(
        None, description="Token returned by the confirmation-required response"
    )
    force: bool = Field(False, description="Bypass the duplicate-rung window")


#: Static assets are read from disk on every request, so editing app.js or
#: styles.css takes effect without restarting the server. This fingerprint
#: lets the open dashboard notice that and reload itself.
def _asset_version() -> str:
    digest = hashlib.sha256()
    for name in sorted(("index.html", "app.js", "styles.css")):
        digest.update(name.encode())
        try:
            # Hashed by content, not mtime: a touch, a git checkout or an
            # editor saving an unchanged file must not reload the dashboard
            # out from under someone mid-click.
            digest.update((WEB_DIR / name).read_bytes())
        except OSError:
            digest.update(b"<missing>")
    return digest.hexdigest()[:16]


def create_app(
    settings: Settings | None = None, service: TradingService | None = None
) -> FastAPI:
    settings = settings or load_settings()
    token = ensure_api_token(settings)
    service = service or TradingService(settings)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if settings.api_host not in ("127.0.0.1", "localhost", "::1"):
            log.warning(
                "API is bound to %s, not loopback. Anyone who can reach this "
                "port and holds the token can trade your accounts.",
                settings.api_host,
            )
        log.info(
            "Trading mode: %s%s",
            settings.mode.value.upper(),
            "  *** LIVE ORDERS ENABLED ***" if settings.is_live else "",
        )
        # Connecting talks to the network, so keep it off the event loop.
        await anyio.to_thread.run_sync(service.connect_all)
        yield

    app = FastAPI(
        title="QQQ/PSQ Switch",
        version="1.0.0",
        description="GO LONG / GO SHORT / GO FLAT across Schwab and IBKR",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service

    def require_token(request: Request) -> None:
        """Accept Authorization: Bearer, X-API-Token, or ?token= (Shortcuts)."""
        supplied = ""
        auth = request.headers.get("Authorization", "")
        if auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        if not supplied:
            supplied = request.headers.get("X-API-Token", "").strip()
        if not supplied:
            supplied = request.query_params.get("token", "").strip()
        if not supplied or not hmac.compare_digest(supplied, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Missing or invalid API token",
                headers={"WWW-Authenticate": "Bearer"},
            )

    Auth = Depends(require_token)

    # --- dashboard --------------------------------------------------------

    # Never cached: the dashboard polls /api/version and reloads when these
    # change, which only works if the reload actually refetches them.
    NO_STORE = {"Cache-Control": "no-store, must-revalidate"}

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html, headers=NO_STORE)

    @app.get("/app.js", include_in_schema=False)
    def app_js() -> Any:
        from fastapi.responses import Response

        return Response(
            (WEB_DIR / "app.js").read_text(encoding="utf-8"),
            media_type="application/javascript",
            headers=NO_STORE,
        )

    @app.get("/styles.css", include_in_schema=False)
    def styles() -> Any:
        from fastapi.responses import Response

        return Response(
            (WEB_DIR / "styles.css").read_text(encoding="utf-8"),
            media_type="text/css",
            headers=NO_STORE,
        )

    @app.get("/api/version")
    def version() -> dict[str, Any]:
        """Fingerprint of the dashboard assets.

        Unauthenticated on purpose, like ``/api/health``: it exposes nothing
        about the account, and the dashboard must be able to poll it even
        while the token prompt is up.
        """
        return {"assets": _asset_version(), "server_started_at": STARTED_AT}

    # --- read-only --------------------------------------------------------

    @app.get("/api/health")
    def health() -> dict[str, Any]:
        """Unauthenticated liveness probe. Exposes no account information."""
        return {"ok": True, "mode": settings.mode.value, "live": settings.is_live}

    @app.get("/api/status", dependencies=[Auth])
    def get_status() -> dict[str, Any]:
        return service.status()

    @app.get("/api/holdings", dependencies=[Auth])
    def get_holdings() -> dict[str, Any]:
        return service.holdings()

    @app.get("/api/performance", dependencies=[Auth])
    def get_performance(
        months: int = Query(6, ge=1, le=24),
        backfill: bool = Query(True),
    ) -> dict[str, Any]:
        return service.performance(months=months, backfill=backfill)

    @app.get("/api/accounts", dependencies=[Auth])
    def get_accounts() -> dict[str, Any]:
        """Which accounts exist, and which of them may trade."""
        return {
            "accounts": [
                {
                    "name": name,
                    "broker_type": broker.broker_type,
                    "account": (
                        broker.account.to_dict() if broker.account else None
                    ),
                    "read_only": broker.read_only,
                    "connected": broker.connected,
                    "error": broker.last_error,
                }
                for name, broker in sorted(service.brokers.items())
            ],
            "tradable": sorted(service.tradable_brokers),
            "trading_allowlist_active": bool(settings.trading_accounts),
        }

    @app.get("/api/audit", dependencies=[Auth])
    def get_audit(
        limit: int = Query(100, ge=1, le=500),
        command_id: str | None = None,
    ) -> dict[str, Any]:
        return {"events": service.audit.tail(limit=limit, command_id=command_id)}

    @app.get("/api/history", dependencies=[Auth])
    def get_history(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
        return {"history": service.state.data.history[-limit:]}

    @app.get("/api/config", dependencies=[Auth])
    def get_config() -> dict[str, Any]:
        return settings.to_public_dict()

    # --- commands ---------------------------------------------------------

    def _run(payload: CommandRequest, source: str) -> JSONResponse:
        try:
            command = Command(payload.command.strip().upper().replace(" ", "_"))
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"unknown command {payload.command!r}; expected one of "
                f"{[c.value for c in Command]}",
            )

        try:
            result = service.execute(
                command,
                request_id=payload.request_id,
                confirm_token=payload.confirm_token,
                force=payload.force,
                source=source,
            )
        except ConfirmationRequired as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "CONFIRMATION_REQUIRED",
                    "detail": str(exc),
                    "confirm_token": exc.token,
                    "expires_at": exc.expires_at,
                    "command": command.value,
                },
            )
        except DuplicateCommand as exc:
            return JSONResponse(
                status_code=409,
                content={"status": "DUPLICATE", "detail": str(exc)},
            )
        except ExecutionLocked as exc:
            return JSONResponse(
                status_code=423,
                content={"status": "LOCKED", "detail": str(exc)},
            )
        except UnreconciledState as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "NEEDS_ACKNOWLEDGEMENT",
                    "detail": str(exc),
                    "mismatch": service.state.data.mismatch,
                },
            )
        except (MarketClosed, SafetyLimitExceeded) as exc:
            return JSONResponse(
                status_code=409,
                content={"status": "BLOCKED", "detail": str(exc)},
            )
        except TradingError as exc:
            service.audit.record(
                A.ERROR, command=command.value, message=str(exc), source=source
            )
            return JSONResponse(
                status_code=400,
                content={"status": "ERROR", "detail": str(exc)},
            )

        body = result.to_dict()
        # A partial outcome must never read as success to a caller that only
        # checks the HTTP status code.
        http_status = 200 if result.status == "OK" else 207 if result.any_ok else 502
        return JSONResponse(status_code=http_status, content=body)

    @app.post("/api/command", dependencies=[Auth])
    def post_command(payload: CommandRequest) -> JSONResponse:
        return _run(payload, source="api")

    @app.post("/api/long", dependencies=[Auth])
    def post_long(payload: CommandRequest | None = None) -> JSONResponse:
        return _run(_alias(payload, "GO_LONG"), source="shortcut")

    @app.post("/api/short", dependencies=[Auth])
    def post_short(payload: CommandRequest | None = None) -> JSONResponse:
        return _run(_alias(payload, "GO_SHORT"), source="shortcut")

    @app.post("/api/flat", dependencies=[Auth])
    def post_flat(payload: CommandRequest | None = None) -> JSONResponse:
        return _run(_alias(payload, "GO_FLAT"), source="shortcut")

    @app.get("/api/candles", dependencies=[Auth])
    def get_candles(
        symbol: str = Query(..., description="Traded symbol, e.g. QQQ"),
        days: int = Query(0, ge=0, le=365, description="0 uses CANDLE_DAYS"),
        refresh: bool = Query(True),
    ) -> dict[str, Any]:
        sym = symbol.upper().strip()
        # Charts cover the reference symbols too; trading does not. The order
        # routes still validate against settings.symbols alone.
        if sym not in {x.upper() for x in settings.chart_symbols}:
            raise HTTPException(
                status_code=400,
                detail=f"symbol {symbol!r} is not tracked; only "
                       f"{sorted(settings.chart_symbols)} are available",
            )
        return service.candle_series(sym, days=days or None, refresh=refresh)

    @app.get("/api/ladder", dependencies=[Auth])
    def get_ladder() -> dict[str, Any]:
        return service.ladder()

    @app.post("/api/ladder/order", dependencies=[Auth])
    def post_ladder_order(payload: LadderOrderRequest) -> JSONResponse:
        try:
            result = service.place_ladder_order(
                payload.symbol,
                payload.limit_price,
                request_id=payload.request_id,
                confirm_token=payload.confirm_token,
                force=payload.force,
                source="dashboard",
            )
        except ConfirmationRequired as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "CONFIRMATION_REQUIRED",
                    "detail": str(exc),
                    "confirm_token": exc.token,
                    "expires_at": exc.expires_at,
                    "symbol": payload.symbol.upper(),
                    "limit_price": payload.limit_price,
                },
            )
        except DuplicateCommand as exc:
            return JSONResponse(
                status_code=409,
                content={"status": "DUPLICATE", "detail": str(exc)},
            )
        except ExecutionLocked as exc:
            return JSONResponse(
                status_code=423,
                content={"status": "LOCKED", "detail": str(exc)},
            )
        except UnreconciledState as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "status": "NEEDS_ACKNOWLEDGEMENT",
                    "detail": str(exc),
                    "mismatch": service.state.data.mismatch,
                },
            )
        except (MarketClosed, SafetyLimitExceeded) as exc:
            return JSONResponse(
                status_code=409,
                content={"status": "BLOCKED", "detail": str(exc)},
            )
        except TradingError as exc:
            return JSONResponse(
                status_code=400,
                content={"status": "ERROR", "detail": str(exc)},
            )

        body = result.to_dict()
        http_status = (
            200 if result.status == "OK" else 207 if result.any_ok else 502
        )
        return JSONResponse(status_code=http_status, content=body)

    @app.post("/api/acknowledge", dependencies=[Auth])
    def acknowledge(payload: AcknowledgeRequest) -> dict[str, Any]:
        cleared = service.acknowledge_mismatch(payload.note)
        return {"cleared": cleared}

    @app.post("/api/reconnect", dependencies=[Auth])
    def reconnect() -> dict[str, Any]:
        return {"errors": service.connect_all()}

    # --- logins -----------------------------------------------------------

    def _reconnect_after_login(broker_type: str, done: str) -> str:
        try:
            errors = service.rebuild_brokers()
        except ExecutionLocked:
            return f"{done} A command is running; press Refresh once it finishes."
        failed = [
            f"{name}: {err}" for name, err in errors.items()
            if err and service.brokers[name].broker_type == broker_type
        ]
        if failed:
            return f"{done} But reconnecting failed — " + "; ".join(failed)
        return f"{done} Connected."

    def _drop_ibkr_session() -> str | None:
        # Rebuild so the dashboard stops showing the old IBKR accounts as
        # connected; Schwab accounts are rebuilt too, harmlessly.
        try:
            service.rebuild_brokers()
        except ExecutionLocked:
            return ("Logged out of IBKR. A command is running; press Refresh "
                    "once it finishes.")
        return None

    schwab_login = SchwabLoginFlow(
        settings,
        on_success=lambda login: _reconnect_after_login(
            "schwab", f"Schwab login {login!r} authorised."),
    )
    ibkr_restart = IbkrGatewayRestart(
        settings,
        on_authenticated=lambda: _reconnect_after_login(
            "ibkr", "IBKR gateway logged in."),
        on_logged_out=_drop_ibkr_session,
    )

    @app.get("/api/logins", dependencies=[Auth])
    def get_logins() -> dict[str, Any]:
        """Login state for each broker, for the dashboard's Logins panel."""
        ibkr_brokers = [
            b for b in service.brokers.values() if b.broker_type == "ibkr"
        ]
        ibkr_enabled = "ibkr" in settings.brokers_enabled
        ibkr_connected = any(b.connected for b in ibkr_brokers)
        gateway_running = ibeam_running() if ibkr_enabled else None
        # Only chase a gateway that is actually up: a stopped one is the
        # logged-out state, not a fault to recover from.
        if ibkr_enabled and not ibkr_connected and gateway_running is not False:
            ibkr_restart.reconnect_if_ready()
        return {
            "schwab": {
                "enabled": "schwab" in settings.brokers_enabled,
                "configured": bool(
                    settings.schwab_app_key and settings.schwab_app_secret
                ),
                "callback_url": settings.schwab_callback_url,
                "logins": [
                    _live_login_status(
                        schwab_token_status(settings, login), service.brokers
                    )
                    for login in settings.schwab_logins
                ],
                "flow": schwab_login.status(),
            },
            "ibkr": {
                "enabled": ibkr_enabled,
                "connected": ibkr_connected,
                "error": next(
                    (b.last_error for b in ibkr_brokers if b.last_error), None
                ),
                "gateway_running": gateway_running,
                "restart": ibkr_restart.status(),
            },
        }

    @app.post("/api/logins/schwab/start", dependencies=[Auth])
    def start_schwab_login(payload: SchwabLoginRequest) -> dict[str, Any]:
        try:
            return schwab_login.start(payload.login)
        except LoginError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/logins/schwab/paste", dependencies=[Auth])
    def paste_schwab_redirect(payload: SchwabPasteRequest) -> dict[str, Any]:
        try:
            return schwab_login.paste(payload.url)
        except LoginError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    @app.post("/api/logins/ibkr/restart", dependencies=[Auth])
    def restart_ibkr_gateway() -> dict[str, Any]:
        return ibkr_restart.restart()

    @app.post("/api/logins/ibkr/logout", dependencies=[Auth])
    def logout_ibkr_gateway() -> dict[str, Any]:
        return ibkr_restart.logout()

    return app


def _broker_login(broker: Any) -> str | None:
    """The Schwab login behind a broker, looking through mode/permission wrappers."""
    for _ in range(4):
        login = getattr(broker, "login", None)
        if login:
            return login
        broker = getattr(broker, "inner", None) or getattr(broker, "real", None)
        if broker is None:
            return None
    return None


def _live_login_status(status: dict[str, Any], brokers: dict[str, Any]) -> dict[str, Any]:
    """Correct a token's age-based status with what Schwab actually said.

    Days left only says the token is not *expired*. Schwab can still reject
    it (re-authorised elsewhere, revoked), and two logins can turn out to
    reach the same account. Either way the panel must not say OK.
    """
    if status.get("state") not in ("ok", "expiring", "unknown"):
        return status
    mine = [b for b in brokers.values()
            if b.broker_type == "schwab" and _broker_login(b) == status["login"]]
    shared = next((b.warning for b in mine if getattr(b, "warning", None)), None)
    if shared:
        return {**status, "state": "shared", "detail": shared}
    if not mine:
        return {**status, "state": "rejected",
                "detail": "Schwab returned no accounts for this login. Log in again."}
    if not any(b.connected for b in mine):
        error = next((b.last_error for b in mine if b.last_error), None)
        if error:
            return {**status, "state": "rejected", "detail": error}
    return status


def _alias(payload: CommandRequest | None, command: str) -> CommandRequest:
    if payload is None:
        return CommandRequest(command=command)
    data = payload.model_dump()
    data["command"] = command
    return CommandRequest(**data)


def app_factory() -> FastAPI:  # pragma: no cover - uvicorn entry point
    """Build the app from the environment.

    Uvicorn's reloader re-imports the application in a fresh worker process,
    so it needs an import string rather than an object. This is that target.
    """
    return create_app(load_settings())


def run() -> None:  # pragma: no cover - entry point
    import uvicorn

    settings = load_settings()
    token = ensure_api_token(settings)
    app = create_app(settings)
    banner = "LIVE TRADING" if settings.is_live else settings.mode.value.upper()
    print(f"\n  QQQ/PSQ Switch — mode: {banner}")
    print(f"  Dashboard: http://{settings.api_host}:{settings.api_port}/?token={token}")
    print(f"  API token: {token}\n")
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="info")
