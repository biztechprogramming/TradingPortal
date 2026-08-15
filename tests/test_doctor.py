"""The `doctor` command: reports config state without ever leaking a secret."""

from __future__ import annotations

import argparse
import json
import time

import pytest

from trading.cli import cmd_doctor

SECRETS = {
    "SCHWAB_APP_KEY": "APPKEY-SUPER-SECRET",
    "SCHWAB_APP_SECRET": "APPSECRET-SUPER-SECRET",
    "API_TOKEN": "TOKEN-SUPER-SECRET",
}


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """A predictable environment with no inherited configuration."""
    for key in list(SECRETS) + [
        "TRADING_MODE", "SCHWAB_LOGINS", "TRADING_ACCOUNTS", "IBKR_ACCOUNT_ID",
        "HOST_API_PORT", "IBKR_CONID_QQQ", "LIVE_TRADING_ENABLED",
        "SCHWAB_CALLBACK_URL", "IBKR_BASE_URL", "SCHWAB_TOKEN_PATH",
    ]:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "log"))
    return tmp_path


def run(capsys, env_file=None) -> tuple[int, str]:
    code = cmd_doctor(argparse.Namespace(env_file=env_file))
    return code, capsys.readouterr().out


def test_reports_missing_api_token(clean_env, capsys):
    code, out = run(capsys)
    assert code == 1
    assert "API_TOKEN" in out
    assert "MISSING" in out
    assert "secrets.token_urlsafe" in out      # tells you how to make one


def test_never_prints_secret_values(clean_env, capsys, monkeypatch):
    for key, value in SECRETS.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("SCHWAB_LOGINS", "personal")

    _, out = run(capsys)
    for value in SECRETS.values():
        assert value not in out, f"{value} leaked into doctor output"
    assert "********" in out


def test_shows_non_secret_values(clean_env, capsys, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("TRADING_ACCOUNTS", "1111,2222")
    _, out = run(capsys)
    assert "1111,2222" in out                  # not a secret, so shown


def test_flags_a_login_with_no_token_file(clean_env, capsys, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    monkeypatch.setenv("SCHWAB_LOGINS", "personal,joint")

    code, out = run(capsys)
    assert code == 1
    assert "schwab-login --login personal" in out
    assert "schwab-login --login joint" in out


def _write_token(clean_env, login: str, age_days: float) -> None:
    state = clean_env / "state"
    state.mkdir(parents=True, exist_ok=True)
    name = "schwab_token.json" if login == "default" else f"schwab_token_{login}.json"
    (state / name).write_text(json.dumps({
        "access_token": "a", "refresh_token": "r",
        "expires_at": time.time() + 1800,
        "obtained_at": time.time() - age_days * 86400,
    }))


def test_reports_days_left_on_a_fresh_token(clean_env, capsys, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    _write_token(clean_env, "default", age_days=1.0)

    code, out = run(capsys)
    assert code == 0
    assert "days left" in out
    assert "6.0 days left" in out


def test_flags_an_expired_refresh_token(clean_env, capsys, monkeypatch):
    """The 7-day expiry is invisible until it bites, so surface it."""
    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    _write_token(clean_env, "default", age_days=8.0)

    code, out = run(capsys)
    assert code == 1
    assert "EXPIRED" in out
    assert "schwab-login" in out


def test_unreadable_token_file_is_reported(clean_env, capsys, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    state = clean_env / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "schwab_token.json").write_text("{ this is not json")

    code, out = run(capsys)
    assert code == 1
    assert "unreadable" in out


def test_complete_configuration_passes(clean_env, capsys, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "x")
    monkeypatch.setenv("SCHWAB_APP_KEY", "k")
    monkeypatch.setenv("SCHWAB_APP_SECRET", "s")
    monkeypatch.setenv("TRADING_ACCOUNTS", "1111")
    _write_token(clean_env, "default", age_days=0.5)

    code, out = run(capsys)
    assert code == 0
    assert "Configuration looks complete" in out


def test_warns_that_an_empty_allowlist_permits_everything(clean_env, capsys,
                                                          monkeypatch):
    monkeypatch.setenv("API_TOKEN", "x")
    _, out = run(capsys)
    assert "EVERY AUTHORISED ACCOUNT CAN TRADE" in out


def test_dry_run_is_survivable_with_missing_config(clean_env, capsys):
    """You should be able to start exploring before Schwab approves an app."""
    code, out = run(capsys)
    assert code == 1
    assert "dry_run" in out
    assert "missing brokers are simulated" in out
