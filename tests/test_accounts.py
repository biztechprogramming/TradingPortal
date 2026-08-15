"""Multiple accounts, multiple logins, and the read-only permission wrapper."""

from __future__ import annotations

from pathlib import Path

import pytest

from trading.brokers.factory import build_brokers
from trading.brokers.readonly import AccountNotTradable, ReadOnlyBroker
from trading.brokers.simulated import ShadowBroker
from trading.models import Account, Command, Side, TradingMode
from trading.orchestrator import TradingService
from tests.conftest import make_broker, make_settings

THREE_SCHWAB = [
    ("personal", Account("HASH-A", "...1111", is_paper=False)),
    ("personal", Account("HASH-B", "...2222", is_paper=False)),
    ("joint", Account("HASH-C", "...3333", is_paper=False)),
]


# --- allowlist matching ------------------------------------------------------


def test_empty_allowlist_permits_every_account(tmp_path):
    s = make_settings(tmp_path, trading_accounts=())
    assert s.may_trade_account("...1111")
    assert s.may_trade_account("123456789")


def test_allowlist_matches_full_number_and_last_four(tmp_path):
    s = make_settings(tmp_path, trading_accounts=("123456789", "2222"))
    assert s.may_trade_account("123456789")
    assert s.may_trade_account("...6789")      # last 4 of the full number
    assert s.may_trade_account("...2222")
    assert not s.may_trade_account("...3333")
    assert not s.may_trade_account("")


def test_each_login_gets_its_own_token_file(tmp_path):
    """Sharing one token file between logins makes each refresh evict the other."""
    s = make_settings(
        tmp_path,
        schwab_logins=("personal", "joint"),
        schwab_token_path=tmp_path / "state" / "schwab_token.json",
    )
    personal = s.schwab_token_path_for("personal")
    joint = s.schwab_token_path_for("joint")
    assert personal != joint
    assert personal.name == "schwab_token_personal.json"
    assert joint.name == "schwab_token_joint.json"


def test_token_paths_follow_state_dir(tmp_path, monkeypatch):
    """STATE_DIR alone must relocate every stateful file, tokens included.

    A container mounts one volume; if the token path did not follow
    STATE_DIR it would be written outside the volume and lost on restart —
    and the Schwab refresh token cannot be regenerated without a browser.
    """
    from trading.config import load_settings

    for key in ("SCHWAB_TOKEN_PATH", "SCHWAB_LOGINS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "vol"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "log"))
    monkeypatch.setenv("SCHWAB_LOGINS", "personal,joint")

    s = load_settings(None)
    assert s.schwab_token_path_for("personal") == tmp_path / "vol" / "schwab_token_personal.json"
    assert s.schwab_token_path_for("joint") == tmp_path / "vol" / "schwab_token_joint.json"


def test_explicit_token_path_still_wins(tmp_path, monkeypatch):
    """An operator who names a path deliberately keeps it."""
    from trading.config import load_settings

    monkeypatch.setenv("STATE_DIR", str(tmp_path / "vol"))
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "log"))
    monkeypatch.setenv("SCHWAB_TOKEN_PATH", "/secure/tok.json")
    monkeypatch.delenv("SCHWAB_LOGINS", raising=False)

    s = load_settings(None)
    assert s.schwab_token_path_for("default") == Path("/secure/tok.json")


def test_explicit_token_path_override(tmp_path):
    s = make_settings(
        tmp_path,
        schwab_logins=("joint",),
        schwab_token_paths={"joint": Path("/custom/joint.json")},
    )
    assert s.schwab_token_path_for("joint") == Path("/custom/joint.json")


# --- factory: one instance per account ---------------------------------------


@pytest.fixture
def discovered(monkeypatch):
    def fake(broker_type, settings):
        return THREE_SCHWAB if broker_type == "schwab" else []

    monkeypatch.setattr("trading.brokers.factory.discover_accounts", fake)


def test_one_broker_instance_per_account(tmp_path, discovered):
    s = make_settings(
        tmp_path, mode=TradingMode.DRY_RUN, live_trading_enabled=False,
        brokers_enabled=("schwab",), schwab_app_key="k", schwab_app_secret="s",
        schwab_logins=("personal", "joint"),
    )
    brokers = build_brokers(s)

    assert len(brokers) == 3
    # Names are qualified so every result is attributable to one account.
    # Login-qualified, so two logins can never collide on a shared last-4.
    assert set(brokers) == {
        "schwab:personal:1111", "schwab:personal:2222", "schwab:joint:3333",
    }
    for broker in brokers.values():
        assert broker.broker_type == "schwab"


def test_allowlist_splits_tradable_from_readonly(tmp_path, discovered):
    s = make_settings(
        tmp_path, mode=TradingMode.DRY_RUN, live_trading_enabled=False,
        brokers_enabled=("schwab",), schwab_app_key="k", schwab_app_secret="s",
        schwab_logins=("personal", "joint"),
        trading_accounts=("1111", "3333"),
    )
    brokers = build_brokers(s)

    assert not brokers["schwab:personal:1111"].read_only
    assert not brokers["schwab:joint:3333"].read_only
    assert brokers["schwab:personal:2222"].read_only
    assert isinstance(brokers["schwab:personal:2222"], ReadOnlyBroker)


def test_all_three_accounts_tradable_when_no_allowlist(tmp_path, discovered):
    s = make_settings(
        tmp_path, mode=TradingMode.DRY_RUN, live_trading_enabled=False,
        brokers_enabled=("schwab",), schwab_app_key="k", schwab_app_secret="s",
        schwab_logins=("personal", "joint"), trading_accounts=(),
    )
    brokers = build_brokers(s)
    assert all(not b.read_only for b in brokers.values())


def test_readonly_accounts_can_be_hidden_entirely(tmp_path, discovered):
    s = make_settings(
        tmp_path, mode=TradingMode.DRY_RUN, live_trading_enabled=False,
        brokers_enabled=("schwab",), schwab_app_key="k", schwab_app_secret="s",
        schwab_logins=("personal", "joint"),
        trading_accounts=("1111",), show_readonly_accounts=False,
    )
    brokers = build_brokers(s)
    assert set(brokers) == {"schwab:personal:1111"}


def test_dry_run_still_shadows_every_account(tmp_path, discovered):
    s = make_settings(
        tmp_path, mode=TradingMode.DRY_RUN, live_trading_enabled=False,
        brokers_enabled=("schwab",), schwab_app_key="k", schwab_app_secret="s",
        schwab_logins=("personal",),
    )
    for broker in build_brokers(s).values():
        inner = broker.inner if isinstance(broker, ReadOnlyBroker) else broker
        assert isinstance(inner, ShadowBroker)


# --- the read-only wrapper ---------------------------------------------------


def test_readonly_broker_allows_reads(tmp_path):
    s = make_settings(tmp_path)
    inner = make_broker(s, "schwab", cash=1_000, positions={"QQQ": 5})
    ro = ReadOnlyBroker(inner, s)

    assert ro.read_only is True
    assert ro.get_available_cash().cash_available == 1_000
    assert ro.get_positions()[0].symbol == "QQQ"
    assert ro.get_quote("QQQ").ask > 0
    assert ro.get_open_orders() == []


@pytest.mark.parametrize("call", [
    lambda b: b.submit_limit_order("QQQ", Side.BUY, 1, 500.55),
    lambda b: b.replace_order("1", 500.60),
    lambda b: b.cancel_order("1"),
])
def test_readonly_broker_refuses_every_write(tmp_path, call):
    s = make_settings(tmp_path)
    inner = make_broker(s, "schwab", cash=100_000)
    ro = ReadOnlyBroker(inner, s)

    with pytest.raises(AccountNotTradable, match="TRADING_ACCOUNTS"):
        call(ro)
    # Nothing reached the underlying adapter.
    assert inner.submitted_orders == []
    assert inner.cancelled_orders == []


# --- orchestration across accounts -------------------------------------------


def _service(tmp_path, **overrides) -> TradingService:
    s = make_settings(tmp_path, **overrides)
    inner = [
        make_broker(s, "schwab:1111", cash=10_000),
        make_broker(s, "schwab:2222", cash=20_000),
        make_broker(s, "schwab:3333", cash=30_000),
        make_broker(s, "ibkr:DU1", cash=5_000),
    ]
    for broker in inner:
        broker.broker_type = "ibkr" if "ibkr" in broker.name else "schwab"
    # The third Schwab account is authorised for reading but not for trading.
    brokers = inner[:2] + [ReadOnlyBroker(inner[2], s)] + inner[3:]
    return TradingService(s, brokers={b.name: b for b in brokers})


def test_command_reaches_every_tradable_account(tmp_path):
    svc = _service(tmp_path)
    result = svc.execute(Command.GO_LONG)

    assert result.status == "OK"
    assert {r.broker for r in result.results} == {
        "schwab:1111", "schwab:2222", "ibkr:DU1",
    }
    for name in ("schwab:1111", "schwab:2222", "ibkr:DU1"):
        assert svc.brokers[name].get_position("QQQ").quantity > 0


def test_readonly_account_is_never_traded_but_is_still_shown(tmp_path):
    svc = _service(tmp_path)
    svc.execute(Command.GO_LONG)

    locked = svc.brokers["schwab:3333"]
    assert locked.get_position("QQQ") is None      # untouched
    assert "schwab:3333" not in {r.broker for r in svc.execute(
        Command.GO_FLAT).results}

    status = svc.status()
    assert "schwab:3333" in status["brokers"]      # but visible
    assert status["brokers"]["schwab:3333"]["connected"] is True


def test_readonly_cash_counts_toward_the_combined_view(tmp_path):
    """You are shown the whole picture even where you cannot trade."""
    svc = _service(tmp_path)
    combined = svc.status()["combined"]
    assert combined["cash_available"] == 65_000.0   # includes the locked 30k


def test_each_account_is_sized_from_its_own_cash(tmp_path):
    svc = _service(tmp_path)
    svc.execute(Command.GO_LONG)

    small = svc.brokers["schwab:1111"].get_position("QQQ").quantity
    large = svc.brokers["schwab:2222"].get_position("QQQ").quantity
    assert small == 19        # (10000 - 25) / 500.55
    assert large == 39        # (20000 - 25) / 500.55
    assert large > small


def test_one_account_failing_is_a_mismatch_not_a_success(tmp_path):
    from trading.brokers.simulated import SimulationConfig

    svc = _service(tmp_path)
    svc.brokers["schwab:2222"].sim = SimulationConfig(reject_next="rejected")
    result = svc.execute(Command.GO_LONG)

    assert result.status == "PARTIAL"
    assert result.mismatch is True
    assert svc.state.data.mismatch is not None


def test_holdings_include_readonly_accounts(tmp_path):
    svc = _service(tmp_path)
    holdings = svc.holdings()
    assert "schwab:3333" in holdings["brokers"]
    assert holdings["brokers"]["schwab:3333"]["balances"]["cash_available"] == 30_000
