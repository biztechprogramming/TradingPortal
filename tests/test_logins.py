"""Dashboard-driven logins: the Schwab OAuth flow and the IBKR gateway restart.

The token exchange itself is stubbed; what matters here is that the redirect
or a pasted URL reaches it with the right code, that a superseded attempt
cannot overwrite a newer one, and that every outcome ends in a state the
dashboard can show rather than a spinner.
"""

from __future__ import annotations

import json
import socket
import subprocess
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from trading import logins as L
from trading.orchestrator import TradingService
from tests.conftest import make_broker, make_settings

TOKEN = "test-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _settings(tmp_path, **overrides):
    defaults = dict(
        schwab_app_key="key",
        schwab_app_secret="secret",
        schwab_callback_url=f"http://127.0.0.1:{_free_port()}/",
        schwab_logins=("personal", "joint"),
        # The default is relative to the working directory: the real tokens.
        schwab_token_path=tmp_path / "state" / "schwab_token.json",
    )
    defaults.update(overrides)
    return make_settings(tmp_path, **defaults)


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def exchanged(monkeypatch):
    calls: list[tuple[str, str]] = []

    def fake_exchange(settings, login, code):
        calls.append((login, code))
        return settings.schwab_token_path_for(login)

    monkeypatch.setattr(L, "exchange_schwab_code", fake_exchange)
    return calls


# --- Schwab token status -----------------------------------------------------


def test_token_status_reports_each_state(tmp_path):
    settings = _settings(tmp_path)
    path = settings.schwab_token_path_for("personal")
    assert L.schwab_token_status(settings, "personal")["state"] == "missing"

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json")
    assert L.schwab_token_status(settings, "personal")["state"] == "unreadable"

    path.write_text(json.dumps({"obtained_at": time.time() - 8 * 86400}))
    assert L.schwab_token_status(settings, "personal")["state"] == "expired"

    path.write_text(json.dumps({"obtained_at": time.time() - 6 * 86400}))
    assert L.schwab_token_status(settings, "personal")["state"] == "expiring"

    path.write_text(json.dumps({"obtained_at": time.time()}))
    status = L.schwab_token_status(settings, "personal")
    assert status["state"] == "ok" and 6.9 < status["days_left"] <= 7


def test_token_status_never_exposes_token_material(tmp_path):
    settings = _settings(tmp_path)
    path = settings.schwab_token_path_for("personal")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"obtained_at": time.time(),
                                "refresh_token": "SECRET", "access_token": "SECRET"}))
    assert "SECRET" not in json.dumps(L.schwab_token_status(settings, "personal"))


# --- Schwab flow -------------------------------------------------------------


def test_redirect_is_caught_and_exchanged(tmp_path, exchanged):
    settings = _settings(tmp_path)
    succeeded: list[str] = []
    flow = L.SchwabLoginFlow(settings, on_success=lambda login: succeeded.append(login))

    started = flow.start("joint")
    assert started["state"] == "waiting" and started["listening"]
    assert "client_id=key" in started["auth_url"]

    httpx.get(f"{settings.schwab_callback_url}?code=abc%40", timeout=5)

    assert _wait_for(lambda: flow.status()["state"] == "done")
    assert exchanged == [("joint", "abc@")]
    assert succeeded == ["joint"]


def test_pasted_url_completes_the_login(tmp_path, exchanged):
    settings = _settings(tmp_path)
    flow = L.SchwabLoginFlow(settings, on_success=lambda login: "all good")
    flow.start("personal")

    result = flow.paste("  https://127.0.0.1:8182/?code=pasted%40&session=x \n")

    assert result["state"] == "done" and result["message"] == "all good"
    assert exchanged == [("personal", "pasted@")]


def test_paste_frees_the_callback_port(tmp_path, exchanged):
    settings = _settings(tmp_path)
    flow = L.SchwabLoginFlow(settings, on_success=lambda login: None)
    flow.start("personal")
    flow.paste("https://127.0.0.1:8182/?code=x")
    # A second login must be able to listen again.
    assert flow.start("personal")["listening"]
    flow.paste("https://127.0.0.1:8182/?code=y")


def test_paste_without_a_code_is_rejected(tmp_path, exchanged):
    flow = L.SchwabLoginFlow(_settings(tmp_path), on_success=lambda login: None)
    flow.start("personal")
    with pytest.raises(L.LoginError, match="code"):
        flow.paste("https://127.0.0.1:8182/?error=nope")
    assert exchanged == []
    flow.paste("https://127.0.0.1:8182/?code=x")   # releases the port


def test_paste_with_no_login_in_progress_is_rejected(tmp_path, exchanged):
    flow = L.SchwabLoginFlow(_settings(tmp_path), on_success=lambda login: None)
    with pytest.raises(L.LoginError, match="in progress"):
        flow.paste("https://127.0.0.1:8182/?code=x")


def test_denied_consent_ends_in_failed(tmp_path, exchanged):
    settings = _settings(tmp_path)
    flow = L.SchwabLoginFlow(settings, on_success=lambda login: None)
    flow.start("personal")
    httpx.get(f"{settings.schwab_callback_url}?error=access_denied", timeout=5)
    assert _wait_for(lambda: flow.status()["state"] == "failed")
    assert "access_denied" in flow.status()["message"]
    assert exchanged == []


def test_exchange_failure_ends_in_failed(tmp_path, monkeypatch):
    def boom(settings, login, code):
        raise L.LoginError("invalid_grant")

    monkeypatch.setattr(L, "exchange_schwab_code", boom)
    flow = L.SchwabLoginFlow(_settings(tmp_path), on_success=lambda login: None)
    flow.start("personal")
    result = flow.paste("https://127.0.0.1:8182/?code=x")
    assert result["state"] == "failed" and "invalid_grant" in result["message"]


def test_unknown_login_and_missing_app_credentials_are_refused(tmp_path):
    flow = L.SchwabLoginFlow(_settings(tmp_path), on_success=lambda login: None)
    with pytest.raises(L.LoginError, match="SCHWAB_LOGINS"):
        flow.start("nobody")

    bare = L.SchwabLoginFlow(
        _settings(tmp_path, schwab_app_key="", schwab_app_secret=""),
        on_success=lambda login: None,
    )
    with pytest.raises(L.LoginError, match="SCHWAB_APP_KEY"):
        bare.start("personal")


def test_unavailable_listener_falls_back_to_paste(tmp_path, exchanged):
    settings = _settings(tmp_path)
    port = int(settings.schwab_callback_url.rsplit(":", 1)[1].strip("/"))
    with socket.socket() as squatter:
        squatter.bind(("127.0.0.1", port))
        squatter.listen()
        flow = L.SchwabLoginFlow(settings, on_success=lambda login: None)
        started = flow.start("personal")
    assert started["state"] == "waiting" and not started["listening"]
    assert "Paste" in started["message"]
    assert flow.paste("https://127.0.0.1:8182/?code=x")["state"] == "done"


# --- IBKR gateway restart ----------------------------------------------------


def _completed(code=0, stderr=""):
    return subprocess.CompletedProcess([], code, stdout="", stderr=stderr)


def test_ibkr_restart_waits_for_login_then_reconnects(tmp_path, monkeypatch):
    monkeypatch.setattr(L, "_docker", lambda *a, **k: _completed())
    gate = threading.Event()
    probes = iter([False, False, True])
    reconnected: list[bool] = []

    def probe():
        gate.wait(5)
        return next(probes)

    restart = L.IbkrGatewayRestart(
        make_settings(tmp_path),
        on_authenticated=lambda: reconnected.append(True) or "back",
        probe=probe,
    )
    monkeypatch.setattr(L.time, "sleep", lambda s: None)

    assert restart.restart()["state"] == "restarting"
    gate.set()
    assert _wait_for(lambda: restart.status()["state"] == "done")
    assert restart.status()["message"] == "back" and reconnected == [True]


def test_ibkr_login_reports_a_gateway_that_will_not_start(tmp_path, monkeypatch):
    monkeypatch.setattr(
        L, "_docker",
        lambda *a, **k: _completed(1, "Error: No such container: qqq-ibeam"),
    )
    restart = L.IbkrGatewayRestart(
        make_settings(tmp_path), on_authenticated=lambda: None, probe=lambda: True
    )
    restart.restart()
    assert _wait_for(lambda: restart.status()["state"] == "failed")
    assert "Could not start the IBKR gateway" in restart.status()["message"]


def test_ibkr_login_creates_a_missing_container(tmp_path, monkeypatch):
    calls: list[tuple] = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "restart":
            return _completed(1, "Error: No such container: qqq-ibeam")
        return _completed()

    monkeypatch.setattr(L, "_docker", docker)
    restart = L.IbkrGatewayRestart(
        make_settings(tmp_path), on_authenticated=lambda: "in", probe=lambda: True
    )
    restart.restart()
    assert _wait_for(lambda: restart.status()["state"] == "done")
    assert calls[0] == ("stop", "qqq-ibgateway")   # one session per username
    assert calls[-1] == ("compose", "up", "-d", "ibeam")


def test_tws_backend_drives_the_ib_gateway_container(tmp_path, monkeypatch):
    calls: list[tuple] = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "restart":
            return _completed(1, "Error: No such container: qqq-ibgateway")
        return _completed()

    monkeypatch.setattr(L, "_docker", docker)
    restart = L.IbkrGatewayRestart(
        make_settings(tmp_path, ibkr_backend="tws"),
        on_authenticated=lambda: "in", probe=lambda: True,
    )
    restart.restart()
    assert _wait_for(lambda: restart.status()["state"] == "done")
    assert calls[0] == ("stop", "qqq-ibeam")
    assert calls[-1] == ("compose", "up", "-d", "ibgateway")


def test_ibkr_logout_ends_session_then_stops_gateway(tmp_path, monkeypatch):
    order: list[str] = []
    monkeypatch.setattr(
        L, "_docker", lambda *a, **k: order.append("docker " + a[0]) or _completed()
    )
    restart = L.IbkrGatewayRestart(
        make_settings(tmp_path),
        on_authenticated=lambda: None,
        probe=lambda: True,
        on_logged_out=lambda: order.append("rebuilt") or None,
        end_session=lambda: order.append("logout"),
    )
    assert restart.logout()["state"] in ("logging_out", "logged_out")
    assert _wait_for(lambda: restart.status()["state"] == "logged_out")
    assert order == ["logout", "docker stop", "rebuilt"]


def test_ibkr_logout_stops_gateway_even_if_session_call_fails(tmp_path, monkeypatch):
    stopped: list[bool] = []
    monkeypatch.setattr(L, "_docker", lambda *a, **k: stopped.append(True) or _completed())

    def boom():
        raise httpx.ConnectError("gateway unreachable")

    restart = L.IbkrGatewayRestart(
        make_settings(tmp_path), on_authenticated=lambda: None,
        probe=lambda: True, end_session=boom,
    )
    restart.logout()
    assert _wait_for(lambda: restart.status()["state"] == "logged_out")
    assert stopped == [True]


def test_gateway_that_logged_in_alone_is_picked_up_once(tmp_path):
    reconnected: list[bool] = []
    restart = L.IbkrGatewayRestart(
        make_settings(tmp_path),
        on_authenticated=lambda: reconnected.append(True),
        probe=lambda: True,
    )
    restart.reconnect_if_ready()
    assert _wait_for(lambda: reconnected == [True])
    restart.reconnect_if_ready()          # throttled
    time.sleep(0.1)
    assert reconnected == [True]


# --- HTTP routes -------------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch, exchanged):
    from trading.api import create_app

    monkeypatch.setattr("trading.api.ibeam_running", lambda *a: True)
    settings = _settings(tmp_path, api_token=TOKEN)
    brokers = {
        "schwab": make_broker(settings, "schwab", cash=50_000.0),
        "ibkr": make_broker(settings, "ibkr", cash=25_000.0),
    }
    service = TradingService(settings, brokers=brokers)
    with TestClient(create_app(settings, service)) as c:
        c.settings = settings      # type: ignore[attr-defined]
        yield c


@pytest.mark.parametrize("method,path", [
    ("get", "/api/logins"),
    ("post", "/api/logins/schwab/start"),
    ("post", "/api/logins/schwab/paste"),
    ("post", "/api/logins/ibkr/restart"),
    ("post", "/api/logins/ibkr/logout"),
])
def test_login_routes_require_a_token(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_logins_shape(client):
    body = client.get("/api/logins", headers=AUTH).json()
    assert [l["login"] for l in body["schwab"]["logins"]] == ["personal", "joint"]
    assert all(l["state"] == "missing" for l in body["schwab"]["logins"])
    assert body["schwab"]["flow"]["state"] == "idle"
    assert body["ibkr"]["enabled"] and body["ibkr"]["gateway_running"]


def test_schwab_login_over_http(client, exchanged):
    started = client.post("/api/logins/schwab/start", headers=AUTH,
                          json={"login": "personal"})
    assert started.status_code == 200 and started.json()["auth_url"]

    done = client.post("/api/logins/schwab/paste", headers=AUTH,
                       json={"url": "https://127.0.0.1:8182/?code=c%40"})
    assert done.status_code == 200
    assert done.json()["state"] == "done"
    assert "Connected" in done.json()["message"]
    assert exchanged == [("personal", "c@")]


def test_schwab_login_errors_are_400s(client):
    res = client.post("/api/logins/schwab/start", headers=AUTH, json={"login": "nobody"})
    assert res.status_code == 400 and "SCHWAB_LOGINS" in res.json()["detail"]
    res = client.post("/api/logins/schwab/paste", headers=AUTH, json={"url": "x"})
    assert res.status_code == 400


# --- live login status -------------------------------------------------------


class _FakeSchwab:
    broker_type = "schwab"

    def __init__(self, login, connected=True, last_error=None, warning=None):
        self.login = login
        self.connected = connected
        self.last_error = last_error
        if warning:
            self.warning = warning


class _Wrapper:
    """Stands in for ReadOnlyBroker/ShadowBroker, which hide the login."""
    broker_type = "schwab"

    def __init__(self, inner):
        self.inner = inner
        self.connected = inner.connected
        self.last_error = inner.last_error
        self.warning = getattr(inner, "warning", None)


def _ok(login):
    return {"login": login, "state": "ok", "days_left": 6.9}


def test_working_login_stays_ok():
    from trading.api import _live_login_status

    brokers = {"a": _Wrapper(_FakeSchwab("andrew"))}
    assert _live_login_status(_ok("andrew"), brokers)["state"] == "ok"


def test_token_schwab_rejected_is_not_shown_as_ok():
    """Days left said OK while every call failed: that hid a dead login."""
    from trading.api import _live_login_status

    err = "Schwab token refresh rejected (HTTP 400)."
    brokers = {"a": _FakeSchwab("andrew", connected=False, last_error=err)}
    out = _live_login_status(_ok("andrew"), brokers)
    assert out["state"] == "rejected" and out["detail"] == err


def test_login_with_no_accounts_is_rejected():
    from trading.api import _live_login_status

    out = _live_login_status(_ok("andrew"), {})
    assert out["state"] == "rejected"


def test_login_sharing_an_account_is_flagged():
    from trading.api import _live_login_status

    brokers = {"a": _Wrapper(_FakeSchwab("anastasia", warning="both reach ...1354"))}
    out = _live_login_status(_ok("anastasia"), brokers)
    assert out["state"] == "shared" and "1354" in out["detail"]
