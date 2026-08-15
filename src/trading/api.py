"""Local HTTP interface: dashboard plus a token-authenticated JSON API.

Binds to loopback by default. Every mutating route requires a bearer token,
so a Shortcut, Stream Deck macro or curl one-liner can drive it, but a stray
page in your browser cannot.
"""

from __future__ import annotations

import hmac
import logging
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
from .models import Command
from .orchestrator import TradingService

log = logging.getLogger("trading.api")

WEB_DIR = Path(__file__).parent / "web"


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

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        html = (WEB_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(html)

    @app.get("/app.js", include_in_schema=False)
    def app_js() -> Any:
        from fastapi.responses import Response

        return Response(
            (WEB_DIR / "app.js").read_text(encoding="utf-8"),
            media_type="application/javascript",
        )

    @app.get("/styles.css", include_in_schema=False)
    def styles() -> Any:
        from fastapi.responses import Response

        return Response(
            (WEB_DIR / "styles.css").read_text(encoding="utf-8"),
            media_type="text/css",
        )

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

    @app.post("/api/acknowledge", dependencies=[Auth])
    def acknowledge(payload: AcknowledgeRequest) -> dict[str, Any]:
        cleared = service.acknowledge_mismatch(payload.note)
        return {"cleared": cleared}

    @app.post("/api/reconnect", dependencies=[Auth])
    def reconnect() -> dict[str, Any]:
        return {"errors": service.connect_all()}

    return app


def _alias(payload: CommandRequest | None, command: str) -> CommandRequest:
    if payload is None:
        return CommandRequest(command=command)
    data = payload.model_dump()
    data["command"] = command
    return CommandRequest(**data)


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
