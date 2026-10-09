"""Broker logins driven from the dashboard rather than a terminal.

Schwab: the same authorisation-code flow as ``cli schwab-login``, but the
redirect catcher runs inside the server process. The dashboard opens Schwab's
consent page in a new tab, the browser lands on the callback address, and the
code is exchanged here. A pasted redirected URL is accepted as the fallback,
exactly as the CLI accepts one on stdin.

IBKR: the bundled ibeam container logs in by itself with the credentials in
``.env``; the only thing worth a button is restarting it, which makes it log in
again (and send the phone approval) when the session has died.
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
import subprocess
import threading
import time
import urllib.parse
from pathlib import Path
from typing import Any, Callable

import httpx

from .config import Settings
from .oauth_listener import start_catcher

log = logging.getLogger("trading.logins")

#: Container name fixed in docker-compose.yml.
IBEAM_CONTAINER = "qqq-ibeam"

#: How long a dashboard-started Schwab login waits for the redirect.
SCHWAB_REDIRECT_TIMEOUT = 300.0

#: How long to wait for the gateway to authenticate after a restart. ibeam's
#: own startup delay is 30s, plus however long the phone approval takes.
IBKR_AUTH_TIMEOUT = 180.0


class LoginError(RuntimeError):
    """A login failed for a reason the user can act on. Message is shown as-is."""


# --- Schwab ------------------------------------------------------------------


def code_from_url(url: str) -> str:
    """Pull ``?code=`` out of a redirected URL.

    ``parse_qs`` percent-decodes, which matters: Schwab codes end in ``@``,
    shown as ``%40`` in the address bar. Sending the encoded form is rejected.
    """
    parsed = urllib.parse.urlparse(url.strip())
    return (urllib.parse.parse_qs(parsed.query).get("code") or [""])[0]


def schwab_auth_url(settings: Settings) -> str:
    params = urllib.parse.urlencode({
        "client_id": settings.schwab_app_key,
        "redirect_uri": settings.schwab_callback_url,
        "response_type": "code",
    })
    return f"{settings.schwab_base_url}/v1/oauth/authorize?{params}"


def exchange_schwab_code(settings: Settings, login: str, code: str) -> Path:
    """Trade an authorisation code for tokens and save them. Returns the path."""
    from .brokers.schwab import _TokenStore

    basic = base64.b64encode(
        f"{settings.schwab_app_key}:{settings.schwab_app_secret}".encode()
    ).decode()
    try:
        resp = httpx.post(
            f"{settings.schwab_base_url}/v1/oauth/token",
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.schwab_callback_url,
            },
            timeout=30,
        )
    except httpx.RequestError as exc:
        raise LoginError(
            f"Could not reach Schwab: {exc}. The code is already spent, so "
            "this needs a fresh login."
        ) from exc

    if resp.status_code != 200:
        message = f"Token exchange failed: HTTP {resp.status_code} {resp.text[:300]}"
        if "invalid_grant" in resp.text:
            message += (
                " — that code was expired or already used. Codes live about "
                "30 seconds and work once; log in again. Over VS Code Remote, "
                "a stale port 8182 forward causes this too."
            )
        elif "invalid_client" in resp.text:
            message += (
                " — Schwab rejected the app credentials. Check SCHWAB_APP_KEY "
                "and SCHWAB_APP_SECRET, and that the app is Ready For Use."
            )
        raise LoginError(message)

    payload = resp.json()
    token_path = settings.schwab_token_path_for(login)
    _TokenStore(token_path).save({
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_at": time.time() + float(payload.get("expires_in", 1800)),
        "obtained_at": time.time(),
        "login": login,
    })
    log.info("Schwab login %r authorised; token saved to %s", login, token_path)
    return token_path


def schwab_token_status(settings: Settings, login: str) -> dict[str, Any]:
    """Days left on a login's refresh token. Never reads token material out."""
    path = settings.schwab_token_path_for(login)
    out: dict[str, Any] = {"login": login, "days_left": None}
    if not path.exists():
        return {**out, "state": "missing"}
    try:
        obtained = json.loads(path.read_text()).get("obtained_at")
    except (OSError, json.JSONDecodeError):
        return {**out, "state": "unreadable"}
    if not obtained:
        return {**out, "state": "unknown"}
    days_left = 7 - (time.time() - float(obtained)) / 86400
    state = "expired" if days_left <= 0 else "expiring" if days_left < 2 else "ok"
    return {**out, "state": state, "days_left": round(days_left, 2)}


class SchwabLoginFlow:
    """At most one dashboard-driven Schwab login at a time.

    Each :meth:`start` bumps a generation number; a waiting thread from an
    earlier attempt notices it has been superseded and exits without touching
    the state, so a second click never races the first.
    """

    def __init__(
        self, settings: Settings, on_success: Callable[[str], str | None]
    ) -> None:
        self.settings = settings
        self._on_success = on_success
        self._lock = threading.Lock()
        self._generation = 0
        self._catcher = None
        self._state: dict[str, Any] = {"state": "idle"}

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def start(self, login: str) -> dict[str, Any]:
        settings = self.settings
        if not settings.schwab_app_key or not settings.schwab_app_secret:
            raise LoginError("SCHWAB_APP_KEY and SCHWAB_APP_SECRET must be set in .env.")
        if login not in settings.schwab_logins:
            raise LoginError(
                f"Login {login!r} is not in SCHWAB_LOGINS "
                f"({', '.join(settings.schwab_logins)})."
            )

        with self._lock:
            self._generation += 1
            generation = self._generation
            self._close_catcher()
            catcher, why_not = start_catcher(
                settings.schwab_callback_url, settings.state_dir
            )
            if catcher is not None:
                catcher.start()
            self._catcher = catcher
            self._state = {
                "state": "waiting",
                "login": login,
                "auth_url": schwab_auth_url(settings),
                "listening": catcher is not None,
                "message": (
                    f"Waiting for Schwab to redirect to "
                    f"{settings.schwab_callback_url}"
                    if catcher is not None
                    else f"Automatic capture unavailable ({why_not}). "
                         "Paste the redirected URL below."
                ),
                "started_at": time.time(),
            }
            snapshot = dict(self._state)

        if catcher is not None:
            threading.Thread(
                target=self._await_redirect,
                args=(catcher, login, generation),
                name=f"schwab-login-{login}",
                daemon=True,
            ).start()
        return snapshot

    def paste(self, url: str) -> dict[str, Any]:
        code = code_from_url(url)
        if not code:
            raise LoginError("No ?code= parameter found in that URL.")
        with self._lock:
            if self._state.get("state") != "waiting":
                raise LoginError("No Schwab login is in progress. Click Log in first.")
            login = self._state["login"]
            self._generation += 1
            generation = self._generation
            self._close_catcher()
        self._finish(login, code, generation)
        return self.status()

    def _await_redirect(self, catcher, login: str, generation: int) -> None:
        result = catcher.wait(SCHWAB_REDIRECT_TIMEOUT)
        with self._lock:
            if generation != self._generation:
                return  # superseded by a paste or a newer login
            self._close_catcher()
        if result is None:
            self._set(generation, state="failed", login=login,
                      message="Timed out waiting for Schwab. Log in again.")
        elif result.error:
            self._set(generation, state="failed", login=login,
                      message=f"Schwab did not grant authorisation: {result.error}")
        else:
            self._finish(login, result.code, generation)

    def _finish(self, login: str, code: str, generation: int) -> None:
        self._set(generation, state="exchanging", login=login,
                  message="Code received; exchanging it for tokens…")
        try:
            exchange_schwab_code(self.settings, login, code)
        except LoginError as exc:
            self._set(generation, state="failed", login=login, message=str(exc))
            return
        except Exception as exc:  # never leave the dashboard stuck "exchanging"
            log.exception("Schwab token exchange crashed")
            self._set(generation, state="failed", login=login, message=str(exc))
            return
        note = self._on_success(login)
        self._set(generation, state="done", login=login,
                  message=note or f"Schwab login {login!r} authorised.")

    def _set(self, generation: int, **state: Any) -> None:
        with self._lock:
            if generation == self._generation:
                self._state = state

    def _close_catcher(self) -> None:
        if self._catcher is not None:
            try:
                self._catcher.cancel()   # release a thread still waiting on it
                self._catcher.close()
            except Exception:
                pass
            self._catcher = None


# --- IBKR --------------------------------------------------------------------


def _docker(*args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    if shutil.which("docker") is None:
        raise LoginError("Docker is not installed or not on PATH.")
    return subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=timeout
    )


def ibeam_running() -> bool | None:
    """True/False for the gateway container; None if Docker can't tell us."""
    try:
        res = _docker("inspect", "-f", "{{.State.Running}}", IBEAM_CONTAINER, timeout=10)
    except (LoginError, subprocess.SubprocessError, OSError):
        return None
    if res.returncode != 0:
        return False
    return res.stdout.strip() == "true"


class IbkrGatewayRestart:
    """Restart ibeam, wait for it to log in, then hand back to the caller."""

    def __init__(
        self,
        settings: Settings,
        on_authenticated: Callable[[], str | None],
        probe: Callable[[], bool] | None = None,
    ) -> None:
        self.settings = settings
        self._on_authenticated = on_authenticated
        self._probe = probe or self._gateway_authenticated
        self._lock = threading.Lock()
        self._state: dict[str, Any] = {"state": "idle"}
        self._checking = False
        self._last_check = 0.0

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._state)

    def reconnect_if_ready(self, min_interval: float = 30.0) -> None:
        """Pick up a gateway that logged in by itself, e.g. just after ./start.

        Called from the dashboard poll while IBKR is disconnected. Runs in the
        background and at most every *min_interval* seconds, so a gateway that
        authenticates but still fails to connect cannot trigger a rebuild loop.
        """
        with self._lock:
            if (self._checking or self._state.get("state") == "restarting"
                    or time.time() - self._last_check < min_interval):
                return
            self._checking = True
            self._last_check = time.time()

        def check() -> None:
            try:
                if self._probe():
                    self._on_authenticated()
            except Exception:
                log.exception("IBKR reconnect check failed")
            finally:
                with self._lock:
                    self._checking = False

        threading.Thread(target=check, name="ibkr-reconnect", daemon=True).start()

    def restart(self) -> dict[str, Any]:
        with self._lock:
            if self._state.get("state") == "restarting":
                return dict(self._state)
            self._state = {
                "state": "restarting",
                "message": "Restarting the IBKR gateway — approve the login on your phone.",
                "started_at": time.time(),
            }
        threading.Thread(target=self._run, name="ibkr-restart", daemon=True).start()
        return self.status()

    def _run(self) -> None:
        try:
            res = _docker("restart", IBEAM_CONTAINER, timeout=90)
            if res.returncode != 0:
                if "No such container" in res.stderr:
                    raise LoginError(
                        f"The {IBEAM_CONTAINER} container does not exist. "
                        "Run ./start (or docker compose up -d ibeam)."
                    )
                raise LoginError(f"docker restart failed: {res.stderr.strip()[:300]}")

            deadline = time.time() + IBKR_AUTH_TIMEOUT
            while time.time() < deadline:
                if self._probe():
                    break
                time.sleep(3)
            else:
                raise LoginError(
                    "The gateway restarted but did not log in within "
                    f"{int(IBKR_AUTH_TIMEOUT)}s. Did the phone approval arrive? "
                    "Check: docker compose logs -f ibeam"
                )
            note = self._on_authenticated()
            self._set(state="done", message=note or "IBKR gateway logged in.")
        except LoginError as exc:
            self._set(state="failed", message=str(exc))
        except Exception as exc:
            log.exception("IBKR gateway restart crashed")
            self._set(state="failed", message=str(exc))

    def _gateway_authenticated(self) -> bool:
        from .brokers.ibkr import IBKRBroker

        probe = IBKRBroker(self.settings)
        try:
            return bool(probe.auth_status().get("authenticated"))
        except Exception:
            return False
        finally:
            probe.close()

    def _set(self, **state: Any) -> None:
        with self._lock:
            self._state = state
