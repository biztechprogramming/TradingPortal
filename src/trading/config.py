"""Configuration, loaded exclusively from the environment.

No secret is ever hard-coded, and no secret is ever returned by
:meth:`Settings.to_public_dict`, which is what the dashboard and audit log
consume.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError
from .models import TradingMode

#: Default ladder rung spacing per symbol, in dollars. QQQ moves far more per
#: unit of time than PSQ, so the two ladders are deliberately not the same
#: width. Override either with ``LADDER_STEP_<SYMBOL>``.
DEFAULT_LADDER_STEPS: dict[str, float] = {"QQQ": 0.10, "PSQ": 0.005}

#: Fallback spacing for a symbol with no entry above and no explicit override.
FALLBACK_LADDER_STEP = 0.01

#: Substrings that mark an environment variable as secret. Used by the
#: redactor so a future variable named ``*_TOKEN`` is protected by default.
SECRET_MARKERS = (
    "SECRET", "TOKEN", "PASSWORD", "PASSWD", "KEY", "CREDENTIAL", "AUTH",
)


def _env(name: str, default: str | None = None) -> str | None:
    val = os.environ.get(name)
    if val is None:
        return default
    val = val.strip()
    return val if val else default


def _bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _float(name: str, default: float) -> float:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(float(raw))
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _resolve_bps(pct_name: str, bps_name: str, default_bps: float) -> float:
    """Slippage may be expressed as a percentage or in basis points.

    Percentage wins when both are set, because it is the more human-readable
    of the two and therefore the more likely to be the deliberate choice.
    """
    pct = _env(pct_name)
    bps = _env(bps_name)
    if pct is not None and bps is not None:
        return float(pct) * 100.0
    if pct is not None:
        return float(pct) * 100.0
    if bps is not None:
        return float(bps)
    return default_bps


@dataclass
class Settings:
    # --- mode -------------------------------------------------------------
    mode: TradingMode = TradingMode.DRY_RUN
    live_trading_enabled: bool = False
    require_confirmation: bool = True
    confirmation_ttl_seconds: float = 60.0
    confirmation_phrase: str = "EXECUTE"

    # --- symbols ----------------------------------------------------------
    long_symbol: str = "QQQ"
    short_symbol: str = "PSQ"

    # --- brokers ----------------------------------------------------------
    brokers_enabled: tuple[str, ...] = ("schwab", "ibkr")

    schwab_app_key: str | None = None
    schwab_app_secret: str | None = None
    schwab_callback_url: str = "https://127.0.0.1:8182/"
    schwab_token_path: Path = Path("var/state/schwab_token.json")
    schwab_account_number: str | None = None
    schwab_base_url: str = "https://api.schwabapi.com"
    #: One Schwab *login* per label. A single developer app can be authorised
    #: by several Schwab user IDs; each authorisation has its own refresh
    #: token and must have its own token file — sharing one makes each refresh
    #: invalidate the other.
    schwab_logins: tuple[str, ...] = ("default",)
    schwab_token_paths: dict[str, Path] = field(default_factory=dict)

    ibkr_base_url: str = "https://localhost:5000/v1/api"
    ibkr_account_id: str | None = None
    ibkr_account_ids: tuple[str, ...] = ()
    ibkr_verify_ssl: bool = False
    ibkr_conid_overrides: dict[str, int] = field(default_factory=dict)
    ibkr_allow_delayed_quotes: bool = False
    ibkr_auto_confirm_all_questions: bool = False

    # --- account permissions ---------------------------------------------
    #: Accounts the trade commands may touch, by full number or last 4 digits.
    #: Empty means "every authorised account". Anything discovered but not
    #: listed is still shown on the dashboard, wrapped so it cannot trade.
    trading_accounts: tuple[str, ...] = ()
    #: Show authorised-but-not-tradable accounts read-only.
    show_readonly_accounts: bool = True

    # --- sizing -----------------------------------------------------------
    cash_buffer_dollars: float = 25.0
    allow_margin: bool = False
    max_order_notional_dollars: float = 0.0   # 0 = no cap
    max_shares_per_order: int = 0             # 0 = no cap
    min_order_shares: int = 1
    estimated_fee_fixed: float = 0.0
    estimated_fee_per_share: float = 0.0

    # --- limit pricing ----------------------------------------------------
    buy_slippage_bps: float = 10.0
    sell_slippage_bps: float = 10.0
    max_slippage_bps: float = 50.0
    reprice_step_bps: float = 5.0
    max_spread_bps: float = 75.0
    max_quote_age_seconds: float = 10.0
    tick_size: float = 0.01

    # --- ladder -----------------------------------------------------------
    #: Rungs are placed at HIGH/LOW of day plus or minus N steps. Each rung is
    #: a plain resting BUY limit order at exactly that price.
    ladder_enabled: bool = True
    ladder_levels: int = 5
    ladder_steps: dict[str, float] = field(default_factory=dict)
    ladder_ticks: dict[str, float] = field(default_factory=dict)
    #: Fat-finger guard: a rung price further than this from the last trade is
    #: refused, so a stale page or a mistyped API call cannot place an order at
    #: an absurd price.
    ladder_max_deviation_pct: float = 10.0

    # --- candles ----------------------------------------------------------
    #: Hourly extended-hours history, cached in SQLite. Only the most recent
    #: ``candle_refresh_hours`` is re-fetched on a refresh; older bars are
    #: final and are served from the cache.
    candle_days: int = 90
    candle_bar_size: str = "1h"
    candle_outside_rth: bool = True
    candle_refresh_hours: float = 2.0
    #: A weekend is a legitimate ~65h hole in an extended-hours hourly series,
    #: so anything at or below that must not be mistaken for missing data.
    candle_max_gap_hours: float = 80.0

    # --- order monitoring -------------------------------------------------
    order_fill_timeout_seconds: float = 90.0
    order_poll_interval_seconds: float = 1.0
    reprice_interval_seconds: float = 5.0
    max_reprice_attempts: int = 5
    cancel_confirm_timeout_seconds: float = 15.0
    settlement_wait_seconds: float = 5.0

    # --- safety -----------------------------------------------------------
    duplicate_command_window_seconds: float = 30.0
    require_mismatch_ack: bool = True
    max_daily_commands: int = 50
    market_hours_guard: bool = True
    allow_outside_rth: bool = False
    http_timeout_seconds: float = 15.0

    # --- interfaces -------------------------------------------------------
    api_host: str = "127.0.0.1"
    api_port: int = 8787
    api_token: str = ""
    log_dir: Path = Path("var/log")
    state_dir: Path = Path("var/state")
    log_level: str = "INFO"

    # ------------------------------------------------------------------ #

    @property
    def is_live(self) -> bool:
        return self.mode is TradingMode.LIVE

    @property
    def mutations_allowed(self) -> bool:
        """Whether *any* order may be sent to a real broker endpoint."""
        return self.mode in (TradingMode.PAPER, TradingMode.LIVE)

    @property
    def symbols(self) -> tuple[str, str]:
        return (self.long_symbol, self.short_symbol)

    def slippage_bps(self, side: str) -> float:
        return self.buy_slippage_bps if side == "BUY" else self.sell_slippage_bps

    def ladder_step_for(self, symbol: str) -> float:
        """Rung spacing for one symbol, in dollars."""
        sym = (symbol or "").upper()
        if sym in self.ladder_steps:
            return self.ladder_steps[sym]
        return DEFAULT_LADDER_STEPS.get(sym, FALLBACK_LADDER_STEP)

    def ladder_tick_for(self, symbol: str) -> float:
        """Smallest price increment a rung may land on.

        This is about *tradability*, not spacing: it is the grid the venue
        accepts, which the ladder step then walks across. Defaulting it to the
        step would be wrong -- QQQ steps by 0.10, and snapping to a 0.10 grid
        would throw away a session low of 497.75. So the default is the
        ordinary tick, narrowed only when the step is finer than it (PSQ's
        0.005).
        """
        sym = (symbol or "").upper()
        if sym in self.ladder_ticks:
            return self.ladder_ticks[sym]
        return min(self.ladder_step_for(sym), self.tick_size)

    def schwab_token_path_for(self, login: str) -> Path:
        """Token file for one Schwab login. Never shared between logins."""
        if login in self.schwab_token_paths:
            return self.schwab_token_paths[login]
        if login == "default":
            return self.schwab_token_path
        return self.schwab_token_path.parent / f"schwab_token_{login}.json"

    def may_trade_account(self, account_number: str) -> bool:
        """Whether this account is on the trading allowlist.

        Matching accepts the full number or the last four digits, because the
        full number is deliberately not kept in configuration or logs.
        """
        if not self.trading_accounts:
            return True
        number = (account_number or "").strip()
        if not number:
            return False
        for allowed in self.trading_accounts:
            allowed = allowed.strip()
            if not allowed:
                continue
            if number == allowed or number.endswith(allowed[-4:]):
                return True
        return False

    def to_public_dict(self) -> dict[str, Any]:
        """Configuration safe to display and log: no secrets, ever."""
        return {
            "mode": self.mode.value,
            "live_trading_enabled": self.live_trading_enabled,
            "require_confirmation": self.require_confirmation,
            "long_symbol": self.long_symbol,
            "short_symbol": self.short_symbol,
            "brokers_enabled": list(self.brokers_enabled),
            "cash_buffer_dollars": self.cash_buffer_dollars,
            "allow_margin": self.allow_margin,
            "max_order_notional_dollars": self.max_order_notional_dollars,
            "max_shares_per_order": self.max_shares_per_order,
            "buy_slippage_bps": self.buy_slippage_bps,
            "sell_slippage_bps": self.sell_slippage_bps,
            "max_slippage_bps": self.max_slippage_bps,
            "reprice_step_bps": self.reprice_step_bps,
            "max_spread_bps": self.max_spread_bps,
            "max_quote_age_seconds": self.max_quote_age_seconds,
            "order_fill_timeout_seconds": self.order_fill_timeout_seconds,
            "max_reprice_attempts": self.max_reprice_attempts,
            "duplicate_command_window_seconds": self.duplicate_command_window_seconds,
            "market_hours_guard": self.market_hours_guard,
            "allow_outside_rth": self.allow_outside_rth,
            "max_daily_commands": self.max_daily_commands,
            "ladder_enabled": self.ladder_enabled,
            "ladder_levels": self.ladder_levels,
            "ladder_steps": {
                sym: self.ladder_step_for(sym) for sym in self.symbols
            },
            "ladder_ticks": {
                sym: self.ladder_tick_for(sym) for sym in self.symbols
            },
            "ladder_max_deviation_pct": self.ladder_max_deviation_pct,
            "candle_days": self.candle_days,
            "candle_bar_size": self.candle_bar_size,
            "candle_outside_rth": self.candle_outside_rth,
            "candle_refresh_hours": self.candle_refresh_hours,
            "schwab_logins": list(self.schwab_logins),
            "trading_accounts_restricted": bool(self.trading_accounts),
            "trading_accounts_count": len(self.trading_accounts),
            "show_readonly_accounts": self.show_readonly_accounts,
            "schwab_account_configured": bool(self.schwab_account_number),
            "ibkr_account_configured": bool(
                self.ibkr_account_id or self.ibkr_account_ids
            ),
        }


def load_settings(env_file: str | os.PathLike[str] | None = ".env") -> Settings:
    """Build :class:`Settings` from the environment, validating as we go."""
    if env_file:
        try:
            from dotenv import load_dotenv

            load_dotenv(env_file, override=False)
        except ImportError:  # dotenv is optional; real env vars still work
            pass

    mode_raw = (_env("TRADING_MODE", "dry_run") or "dry_run").lower()
    try:
        mode = TradingMode(mode_raw)
    except ValueError as exc:
        valid = ", ".join(m.value for m in TradingMode)
        raise ConfigError(
            f"TRADING_MODE must be one of: {valid} (got {mode_raw!r})"
        ) from exc

    brokers_raw = _env("BROKERS_ENABLED", "schwab,ibkr") or ""
    brokers = tuple(
        b.strip().lower() for b in brokers_raw.split(",") if b.strip()
    )
    unknown = set(brokers) - {"schwab", "ibkr", "simulated"}
    if unknown:
        raise ConfigError(f"Unknown broker(s) in BROKERS_ENABLED: {sorted(unknown)}")

    conid_overrides: dict[str, int] = {}
    for sym in ("QQQ", "PSQ"):
        raw = _env(f"IBKR_CONID_{sym}")
        if raw:
            conid_overrides[sym] = int(raw)

    def _csv(name: str) -> tuple[str, ...]:
        raw = _env(name, "") or ""
        return tuple(v.strip() for v in raw.split(",") if v.strip())

    schwab_logins = _csv("SCHWAB_LOGINS") or ("default",)
    if len(set(schwab_logins)) != len(schwab_logins):
        raise ConfigError("SCHWAB_LOGINS contains duplicate labels")

    token_paths: dict[str, Path] = {}
    for login in schwab_logins:
        override = _env(f"SCHWAB_TOKEN_PATH_{login.upper()}")
        if override:
            token_paths[login] = Path(override)

    ibkr_ids = _csv("IBKR_ACCOUNT_IDS")
    single_ibkr = _env("IBKR_ACCOUNT_ID")
    if single_ibkr and single_ibkr not in ibkr_ids:
        ibkr_ids = ibkr_ids + (single_ibkr,)

    # The token file lives beside the rest of the state unless explicitly
    # placed elsewhere. Deriving it means STATE_DIR alone relocates every
    # stateful file, which is what a container volume needs.
    state_dir = Path(_env("STATE_DIR", "var/state"))
    token_path_env = _env("SCHWAB_TOKEN_PATH")
    schwab_token_path = (
        Path(token_path_env) if token_path_env else state_dir / "schwab_token.json"
    )

    # Ladder spacing is per symbol, keyed on whatever LONG_SYMBOL/SHORT_SYMBOL
    # are actually set to rather than a hard-coded QQQ/PSQ pair.
    long_sym = (_env("LONG_SYMBOL", "QQQ") or "QQQ").upper()
    short_sym = (_env("SHORT_SYMBOL", "PSQ") or "PSQ").upper()
    ladder_steps: dict[str, float] = {}
    ladder_ticks: dict[str, float] = {}
    for sym in (long_sym, short_sym):
        step_raw = _env(f"LADDER_STEP_{sym}")
        if step_raw is not None:
            ladder_steps[sym] = _float(f"LADDER_STEP_{sym}", 0.0)
        tick_raw = _env(f"LADDER_TICK_{sym}")
        if tick_raw is not None:
            ladder_ticks[sym] = _float(f"LADDER_TICK_{sym}", 0.0)

    trading_accounts = _csv("TRADING_ACCOUNTS")
    # Back-compat: the original single-account variable still pins Schwab.
    legacy_schwab = _env("SCHWAB_ACCOUNT_NUMBER")
    if legacy_schwab and not trading_accounts:
        trading_accounts = (legacy_schwab,)

    s = Settings(
        mode=mode,
        live_trading_enabled=_bool("LIVE_TRADING_ENABLED", False),
        require_confirmation=_bool("REQUIRE_CONFIRMATION", True),
        confirmation_ttl_seconds=_float("CONFIRMATION_TTL_SECONDS", 60.0),
        confirmation_phrase=_env("LIVE_CONFIRMATION_PHRASE", "EXECUTE") or "EXECUTE",
        long_symbol=(_env("LONG_SYMBOL", "QQQ") or "QQQ").upper(),
        short_symbol=(_env("SHORT_SYMBOL", "PSQ") or "PSQ").upper(),
        brokers_enabled=brokers,
        schwab_app_key=_env("SCHWAB_APP_KEY"),
        schwab_app_secret=_env("SCHWAB_APP_SECRET"),
        schwab_callback_url=_env("SCHWAB_CALLBACK_URL", "https://127.0.0.1:8182/"),
        schwab_token_path=schwab_token_path,
        schwab_account_number=legacy_schwab,
        schwab_base_url=_env("SCHWAB_BASE_URL", "https://api.schwabapi.com"),
        schwab_logins=schwab_logins,
        schwab_token_paths=token_paths,
        trading_accounts=trading_accounts,
        show_readonly_accounts=_bool("SHOW_READONLY_ACCOUNTS", True),
        ibkr_base_url=_env("IBKR_BASE_URL", "https://localhost:5000/v1/api"),
        ibkr_account_id=single_ibkr,
        ibkr_account_ids=ibkr_ids,
        ibkr_verify_ssl=_bool("IBKR_VERIFY_SSL", False),
        ibkr_conid_overrides=conid_overrides,
        ibkr_allow_delayed_quotes=_bool("IBKR_ALLOW_DELAYED_QUOTES", False),
        ibkr_auto_confirm_all_questions=_bool("IBKR_AUTO_CONFIRM_ALL_QUESTIONS", False),
        cash_buffer_dollars=_float("CASH_BUFFER_DOLLARS", 25.0),
        allow_margin=_bool("ALLOW_MARGIN", False),
        max_order_notional_dollars=_float("MAX_ORDER_NOTIONAL_DOLLARS", 0.0),
        max_shares_per_order=_int("MAX_SHARES_PER_ORDER", 0),
        min_order_shares=_int("MIN_ORDER_SHARES", 1),
        estimated_fee_fixed=_float("ESTIMATED_FEE_FIXED", 0.0),
        estimated_fee_per_share=_float("ESTIMATED_FEE_PER_SHARE", 0.0),
        buy_slippage_bps=_resolve_bps("BUY_SLIPPAGE_PCT", "BUY_SLIPPAGE_BPS", 10.0),
        sell_slippage_bps=_resolve_bps("SELL_SLIPPAGE_PCT", "SELL_SLIPPAGE_BPS", 10.0),
        max_slippage_bps=_resolve_bps("MAX_SLIPPAGE_PCT", "MAX_SLIPPAGE_BPS", 50.0),
        reprice_step_bps=_resolve_bps("REPRICE_STEP_PCT", "REPRICE_STEP_BPS", 5.0),
        max_spread_bps=_resolve_bps("MAX_SPREAD_PCT", "MAX_SPREAD_BPS", 75.0),
        max_quote_age_seconds=_float("MAX_QUOTE_AGE_SECONDS", 10.0),
        tick_size=_float("TICK_SIZE", 0.01),
        order_fill_timeout_seconds=_float("ORDER_FILL_TIMEOUT_SECONDS", 90.0),
        order_poll_interval_seconds=_float("ORDER_POLL_INTERVAL_SECONDS", 1.0),
        reprice_interval_seconds=_float("REPRICE_INTERVAL_SECONDS", 5.0),
        max_reprice_attempts=_int("MAX_REPRICE_ATTEMPTS", 5),
        cancel_confirm_timeout_seconds=_float("CANCEL_CONFIRM_TIMEOUT_SECONDS", 15.0),
        settlement_wait_seconds=_float("SETTLEMENT_WAIT_SECONDS", 5.0),
        duplicate_command_window_seconds=_float(
            "DUPLICATE_COMMAND_WINDOW_SECONDS", 30.0
        ),
        require_mismatch_ack=_bool("REQUIRE_MISMATCH_ACK", True),
        max_daily_commands=_int("MAX_DAILY_COMMANDS", 50),
        ladder_enabled=_bool("LADDER_ENABLED", True),
        ladder_levels=_int("LADDER_LEVELS", 5),
        ladder_steps=ladder_steps,
        ladder_ticks=ladder_ticks,
        ladder_max_deviation_pct=_float("LADDER_MAX_DEVIATION_PCT", 10.0),
        candle_days=_int("CANDLE_DAYS", 90),
        candle_bar_size=_env("CANDLE_BAR_SIZE", "1h") or "1h",
        candle_outside_rth=_bool("CANDLE_OUTSIDE_RTH", True),
        candle_refresh_hours=_float("CANDLE_REFRESH_HOURS", 2.0),
        candle_max_gap_hours=_float("CANDLE_MAX_GAP_HOURS", 80.0),
        market_hours_guard=_bool("MARKET_HOURS_GUARD", True),
        allow_outside_rth=_bool("ALLOW_OUTSIDE_RTH", False),
        http_timeout_seconds=_float("HTTP_TIMEOUT_SECONDS", 15.0),
        api_host=_env("API_HOST", "127.0.0.1"),
        api_port=_int("API_PORT", 8787),
        api_token=_env("API_TOKEN", "") or "",
        log_dir=Path(_env("LOG_DIR", "var/log")),
        state_dir=state_dir,
        log_level=(_env("LOG_LEVEL", "INFO") or "INFO").upper(),
    )
    validate_settings(s)
    return s


def validate_settings(s: Settings) -> None:
    """Reject configurations that are unsafe or internally contradictory."""
    if s.mode is TradingMode.LIVE and not s.live_trading_enabled:
        raise ConfigError(
            "TRADING_MODE=live requires LIVE_TRADING_ENABLED=true. "
            "This second switch exists so that live trading is never a "
            "one-variable accident."
        )
    if s.long_symbol == s.short_symbol:
        raise ConfigError("LONG_SYMBOL and SHORT_SYMBOL must differ")
    if s.cash_buffer_dollars < 0:
        raise ConfigError("CASH_BUFFER_DOLLARS must be >= 0")
    if s.max_slippage_bps < max(s.buy_slippage_bps, s.sell_slippage_bps):
        raise ConfigError(
            "MAX_SLIPPAGE_BPS must be >= BUY_SLIPPAGE_BPS and SELL_SLIPPAGE_BPS; "
            "the hard guard cannot be tighter than the working offset."
        )
    if s.max_slippage_bps <= 0:
        raise ConfigError("MAX_SLIPPAGE_BPS must be > 0")
    if s.max_slippage_bps > 1000:
        raise ConfigError(
            "MAX_SLIPPAGE_BPS above 1000 (10%) is refused as a fat-finger guard"
        )
    if s.tick_size <= 0:
        raise ConfigError("TICK_SIZE must be > 0")
    if s.min_order_shares < 1:
        raise ConfigError("MIN_ORDER_SHARES must be >= 1")
    if s.order_poll_interval_seconds <= 0:
        raise ConfigError("ORDER_POLL_INTERVAL_SECONDS must be > 0")
    if s.max_reprice_attempts < 0:
        raise ConfigError("MAX_REPRICE_ATTEMPTS must be >= 0")
    if not s.brokers_enabled:
        raise ConfigError("BROKERS_ENABLED must list at least one broker")
    if s.ladder_levels < 1:
        raise ConfigError("LADDER_LEVELS must be >= 1")
    if s.ladder_levels > 20:
        raise ConfigError(
            "LADDER_LEVELS above 20 is refused; each rung is a one-click live "
            "order and a wall of them is a fat-finger hazard, not a feature."
        )
    for sym in s.symbols:
        if s.ladder_step_for(sym) <= 0:
            raise ConfigError(f"LADDER_STEP_{sym} must be > 0")
        if s.ladder_tick_for(sym) <= 0:
            raise ConfigError(f"LADDER_TICK_{sym} must be > 0")
    if s.ladder_max_deviation_pct <= 0:
        raise ConfigError("LADDER_MAX_DEVIATION_PCT must be > 0")
    if s.candle_days < 1:
        raise ConfigError("CANDLE_DAYS must be >= 1")
    if s.candle_refresh_hours <= 0:
        raise ConfigError("CANDLE_REFRESH_HOURS must be > 0")
    if s.candle_max_gap_hours <= 72:
        raise ConfigError(
            "CANDLE_MAX_GAP_HOURS must exceed 72; a weekend is a legitimate "
            "~65 hour hole in an extended-hours hourly series and must not be "
            "treated as missing data."
        )
    if s.mode is TradingMode.LIVE and not s.api_token:
        raise ConfigError(
            "API_TOKEN must be set explicitly before live trading; an "
            "auto-generated token would change on every restart."
        )
    if s.api_host not in ("127.0.0.1", "localhost", "::1") and not s.api_token:
        raise ConfigError(
            "API_TOKEN is required when API_HOST is not loopback."
        )


def ensure_api_token(s: Settings) -> str:
    """Return the API token, generating an ephemeral one when permitted.

    Auto-generation is only allowed outside live mode; :func:`validate_settings`
    requires an explicit token before any real money can move.
    """
    if s.api_token:
        return s.api_token
    s.api_token = secrets.token_urlsafe(32)
    return s.api_token


def redact(value: Any, key: str = "") -> Any:
    """Recursively strip anything that looks like a credential."""
    upper_key = key.upper()
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, key) for v in value]
    if any(marker in upper_key for marker in SECRET_MARKERS):
        return "***REDACTED***"
    return value
