"""Broker construction, and the rules that decide what may touch real money.

Two independent gates are applied here, both structurally rather than by a
runtime flag an adapter could forget to check:

* **Mode** — in dry-run the adapter is wrapped in :class:`ShadowBroker`, which
  has no code path to the live order endpoints.
* **Account permission** — an authorised account that is not on
  ``TRADING_ACCOUNTS`` is wrapped in :class:`ReadOnlyBroker`, whose order
  methods raise instead of delegating.

One instance is built per *account*, not per broker, so several accounts at
one broker execute independently and are reported separately.
"""

from __future__ import annotations

import logging

from ..config import Settings
from ..errors import ConfigError
from ..models import Account, TradingMode
from .base import Broker
from .readonly import ReadOnlyBroker
from .simulated import ShadowBroker, SimulatedBroker

log = logging.getLogger("trading.brokers")


def _schwab_configured(settings: Settings) -> bool:
    return bool(settings.schwab_app_key and settings.schwab_app_secret)


# --- discovery ---------------------------------------------------------------


def discover_accounts(broker_type: str, settings: Settings) -> list[tuple[str, Account]]:
    """Find every authorised account, as ``(login_label, account)`` pairs."""
    if broker_type == "schwab":
        from .schwab import discover_schwab_accounts

        found: list[tuple[str, Account]] = []
        for login in settings.schwab_logins:
            try:
                for account in discover_schwab_accounts(settings, login):
                    found.append((login, account))
            except Exception as exc:
                log.warning(
                    "Schwab login %r could not be enumerated: %s. "
                    "Run `schwab-login --login %s` to authorise it.",
                    login, exc, login,
                )
        return found

    if broker_type == "ibkr":
        from .ibkr import IBKRBroker

        probe = IBKRBroker(settings)
        try:
            accounts = probe.get_accounts()
        except Exception as exc:
            log.warning("IBKR accounts could not be enumerated: %s", exc)
            return []
        finally:
            probe.close()

        wanted = {a.upper() for a in settings.ibkr_account_ids}
        if wanted:
            accounts = [a for a in accounts if a.account_id.upper() in wanted]
        return [("default", account) for account in accounts]

    raise ConfigError(f"unknown broker {broker_type!r}")


def _build_adapter(
    broker_type: str, settings: Settings, login: str, account: Account
) -> Broker:
    if broker_type == "schwab":
        from .schwab import SchwabBroker

        return SchwabBroker(settings, login=login, account=account)
    if broker_type == "ibkr":
        from .ibkr import IBKRBroker

        return IBKRBroker(settings, account=account)
    raise ConfigError(f"unknown broker {broker_type!r}")


# --- mode wrapping -----------------------------------------------------------


def apply_mode(broker: Broker, settings: Settings) -> Broker:
    """Wrap an adapter so it can only do what the current mode permits."""
    if settings.mode is TradingMode.DRY_RUN:
        return ShadowBroker(broker, settings)

    if settings.mode is TradingMode.PAPER and broker.broker_type == "schwab":
        # Schwab's Trader API has no paper environment. Sending orders here in
        # "paper" mode would place REAL trades, so we simulate instead.
        log.warning(
            "%s: Schwab has no paper-trading API. Running in shadow mode: real "
            "balances and quotes, simulated fills. Real Schwab orders require "
            "TRADING_MODE=live.", broker.name,
        )
        return ShadowBroker(broker, settings)

    if settings.mode is TradingMode.LIVE and not settings.live_trading_enabled:
        raise ConfigError("live mode requires LIVE_TRADING_ENABLED=true")

    return broker


def apply_permissions(broker: Broker, settings: Settings, account: Account) -> Broker:
    """Wrap accounts that are authorised for reading but not for trading."""
    # ``display_id`` is the masked number for Schwab and the id for IBKR;
    # both are what the allowlist is written against.
    identifier = account.display_id or account.account_id
    if settings.may_trade_account(identifier):
        return broker
    log.info(
        "%s: account %s is authorised but not on TRADING_ACCOUNTS — "
        "read-only", broker.name, account.display_id,
    )
    return ReadOnlyBroker(broker, settings)


def _block_shared_account(
    broker: Broker, settings: Settings, account: Account, logins: list[str]
) -> Broker:
    names = " and ".join(repr(l) for l in logins)
    message = (
        f"Logins {names} both reach account {account.display_id}, so trading "
        "it is blocked: every order would be sent there twice. One of those "
        "logins was authorised as the wrong person. Log in again in a private "
        "browser window, signed in to Schwab as the right person."
    )
    log.error("%s", message)
    return ReadOnlyBroker(
        broker, settings,
        reason=f"logins {names} both reach this account", warning=message,
    )


# --- entry points ------------------------------------------------------------


def build_broker(name: str, settings: Settings) -> Broker:
    """Build a single broker instance, selecting its own account.

    Used by tests and by simple single-account setups. :func:`build_brokers`
    is what the application uses.
    """
    name = name.lower()
    if name == "simulated":
        return SimulatedBroker(settings)

    if name == "schwab" and not _schwab_configured(settings):
        if settings.mode is TradingMode.LIVE:
            raise ConfigError(
                "SCHWAB_APP_KEY and SCHWAB_APP_SECRET are required for live "
                "trading"
            )
        log.warning(
            "Schwab credentials absent; using a synthetic simulated broker."
        )
        return SimulatedBroker(settings, name="schwab")

    if name == "schwab":
        from .schwab import SchwabBroker

        return apply_mode(SchwabBroker(settings), settings)
    if name == "ibkr":
        from .ibkr import IBKRBroker

        return apply_mode(IBKRBroker(settings), settings)
    raise ConfigError(f"unknown broker {name!r}")


def build_brokers(settings: Settings) -> dict[str, Broker]:
    """One instance per authorised account, across every enabled broker."""
    brokers: dict[str, Broker] = {}

    for broker_type in settings.brokers_enabled:
        if broker_type == "simulated":
            sim = SimulatedBroker(settings)
            brokers[sim.name] = sim
            continue

        if broker_type == "schwab" and not _schwab_configured(settings):
            if settings.mode is TradingMode.LIVE:
                raise ConfigError(
                    "SCHWAB_APP_KEY and SCHWAB_APP_SECRET are required for "
                    "live trading"
                )
            log.warning(
                "Schwab credentials absent; substituting a simulated broker."
            )
            sim = SimulatedBroker(settings, name="schwab")
            brokers[sim.name] = sim
            continue

        pairs = discover_accounts(broker_type, settings)
        if not pairs:
            # Discovery failed (gateway down, token expired). Fall back to a
            # single self-selecting instance so the dashboard can still show
            # the connection error rather than silently omitting the broker.
            log.warning(
                "%s: no accounts discovered; adding a single instance so the "
                "failure is visible on the dashboard", broker_type,
            )
            fallback = build_broker(broker_type, settings)
            brokers[fallback.name] = fallback
            continue

        # Two logins reaching one account means one was authorised as the
        # wrong person. Trading it would send every order there twice.
        logins_by_account: dict[str, set[str]] = {}
        for login, account in pairs:
            logins_by_account.setdefault(account.account_id, set()).add(login)

        for login, account in pairs:
            sharing = sorted(logins_by_account[account.account_id])
            duplicated = len(sharing) > 1
            tradable = settings.may_trade_account(
                account.display_id or account.account_id
            )
            if not tradable and not duplicated and not settings.show_readonly_accounts:
                continue

            adapter = _build_adapter(broker_type, settings, login, account)
            adapter = apply_mode(adapter, settings)
            adapter = apply_permissions(adapter, settings, account)
            if duplicated:
                adapter = _block_shared_account(adapter, settings, account, sharing)
            if adapter.name in brokers:
                # Silently overwriting would drop an account from every
                # command and every total, so refuse instead.
                raise ConfigError(
                    f"Two accounts resolved to the same identifier "
                    f"{adapter.name!r}. Rename a login in SCHWAB_LOGINS so "
                    "they can be told apart."
                )
            brokers[adapter.name] = adapter

    if not brokers:
        raise ConfigError(
            "No broker accounts could be built. Check credentials and "
            "BROKERS_ENABLED."
        )

    tradable = [b for b in brokers.values() if not b.read_only]
    log.info(
        "Built %d account(s): %d tradable, %d read-only",
        len(brokers), len(tradable), len(brokers) - len(tradable),
    )
    return brokers


def verify_environment(broker: Broker, settings: Settings) -> list[str]:
    """Post-connect sanity checks. Returns human-readable warnings.

    Raises :class:`ConfigError` when the mismatch is dangerous rather than
    merely surprising.
    """
    warnings: list[str] = []
    if getattr(broker, "warning", None):
        warnings.append(broker.warning)
    acct = broker.account
    if acct is None:
        return warnings

    if settings.mode is TradingMode.PAPER and broker.broker_type == "ibkr":
        if not acct.is_paper and not broker.read_only:
            raise ConfigError(
                f"TRADING_MODE=paper but the connected IBKR account "
                f"{acct.display_id} is a LIVE account. Log the Client Portal "
                "Gateway into your paper credentials, or switch modes "
                "deliberately."
            )

    if settings.mode is TradingMode.LIVE and acct.is_paper:
        warnings.append(
            f"{broker.name}: TRADING_MODE=live but account {acct.display_id} "
            "looks like a PAPER account. Orders will not use real money."
        )

    if (
        settings.mode is TradingMode.LIVE
        and broker.broker_type == "schwab"
        and not broker.read_only
    ):
        warnings.append(
            f"{broker.name}: live mode places REAL orders; Schwab offers no "
            "paper equivalent to fall back on."
        )
    return warnings
