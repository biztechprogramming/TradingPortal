"""Command line interface.

    python -m trading.cli status
    python -m trading.cli long|short|flat [--force] [--yes]
    python -m trading.cli holdings
    python -m trading.cli serve
    python -m trading.cli schwab-login
    python -m trading.cli acknowledge
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .config import ensure_api_token, load_settings
from .errors import ConfirmationRequired, TradingError
from .models import Command, TradingMode
from .orchestrator import TradingService

GREEN, RED, YELLOW, BLUE, DIM, RESET = (
    "\033[32m", "\033[31m", "\033[33m", "\033[34m", "\033[2m", "\033[0m"
)


def _colour(text: str, colour: str) -> str:
    return f"{colour}{text}{RESET}" if sys.stdout.isatty() else text


def _mode_banner(settings) -> str:
    if settings.mode is TradingMode.LIVE:
        return _colour("*** LIVE TRADING — REAL MONEY ***", RED)
    if settings.mode is TradingMode.PAPER:
        return _colour("PAPER TRADING", GREEN)
    return _colour("DRY RUN — no orders will be sent", BLUE)


# --- commands ----------------------------------------------------------------


def cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings(args.env_file)
    service = TradingService(settings)
    service.connect_all()
    status = service.status()

    if args.json:
        print(json.dumps(status, indent=2, default=str))
        return 0

    print(f"\n{_mode_banner(settings)}")
    market = status["market"]
    print(
        f"{DIM}Market: {'OPEN' if market['open'] else 'CLOSED'} "
        f"({market['reason']}) — {market['now_eastern']}{RESET}\n"
    )

    combined = status["combined"]
    print(f"COMBINED   exposure={combined['exposure']}  "
          f"{combined['long_symbol']}={combined['long_shares']:g}  "
          f"{combined['short_symbol']}={combined['short_shares']:g}  "
          f"cash=${combined['cash_available']:,.2f}  "
          f"value=${combined['account_value']:,.2f}")
    print()

    for name, b in status["brokers"].items():
        state = _colour("connected", GREEN) if b["connected"] and not b["error"] \
            else _colour("DISCONNECTED", RED)
        account = (b["account"] or {}).get("display_id", "—")
        paper = " [PAPER]" if (b["account"] or {}).get("is_paper") else ""
        balances = b["balances"] or {}
        print(f"{name.upper():<8} {state}  account={account}{paper}  "
              f"exposure={b['exposure']}")
        for symbol in (combined["long_symbol"], combined["short_symbol"]):
            qty = next(
                (p["quantity"] for p in b["positions"] if p["symbol"] == symbol), 0
            )
            print(f"         {symbol}: {qty:g}")
        print(f"         cash: ${balances.get('cash_available', 0):,.2f}  "
              f"buying power: ${balances.get('buying_power', 0):,.2f}")
        if b["open_orders"]:
            for o in b["open_orders"]:
                print(f"         {_colour('OPEN ORDER', YELLOW)} {o['side']} "
                      f"{o['quantity']:g} {o['symbol']} @ {o['limit_price']} "
                      f"({o['state']})")
        if b["error"]:
            print(f"         {_colour('ERROR: ' + b['error'], RED)}")
        print()

    for warning in status["environment_warnings"]:
        print(_colour(f"! {warning}", YELLOW))

    mismatch = status["state"].get("mismatch")
    if mismatch:
        print(_colour(
            f"\n! UNACKNOWLEDGED MISMATCH: {mismatch['summary']}\n"
            "  Trading is blocked. Run `acknowledge` after checking both "
            "accounts.", RED))

    service.close()
    return 0


def cmd_holdings(args: argparse.Namespace) -> int:
    settings = load_settings(args.env_file)
    service = TradingService(settings)
    service.connect_all()
    data = service.holdings()
    if args.json:
        print(json.dumps(data, indent=2, default=str))
    else:
        for name, b in data["brokers"].items():
            print(f"\n{name.upper()}  {(b.get('account') or {}).get('display_id', '')}")
            if b["error"]:
                print(f"  {_colour('ERROR: ' + b['error'], RED)}")
                continue
            if not b["positions"]:
                print("  (no positions)")
            for p in b["positions"]:
                print(f"  {p['symbol']:<8} {p['quantity']:>10,.0f}  "
                      f"avg ${p['average_price']:>10,.2f}  "
                      f"value ${p['market_value']:>12,.2f}")
            print(f"  {'CASH':<8} {'':>10}  {'':>15}  "
                  f"value ${b['balances']['cash_available']:>12,.2f}")
    service.close()
    return 0


def cmd_performance(args: argparse.Namespace) -> int:
    settings = load_settings(args.env_file)
    service = TradingService(settings)
    service.connect_all()
    data = service.performance(months=args.months, backfill=not args.no_backfill)

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        service.close()
        return 0

    labels = data["account_labels"]

    def render(title: str, windows: list[dict], nav: float | None) -> None:
        nav_text = f"${nav:,.2f}" if nav is not None else "—"
        print(f"\n{title:<28} NAV {nav_text}")
        cells = []
        for w in windows:
            if w["twr_pct"] is None:
                cells.append(f"  {w['label']}: {'n/a':>9}")
                continue
            pct = w["twr_pct"]
            colour = GREEN if pct >= 0 else RED
            text = f"{pct:+.2f}%"
            marker = "" if w["complete"] else "*"
            cells.append(f"  {w['label']}: {_colour(f'{text:>9}', colour)}{marker}")
        print("".join(cells))

    print(f"\n{_mode_banner(settings)}")
    print(f"{DIM}Time-weighted returns; deposits and withdrawals excluded.{RESET}")

    for account in data["accounts"]:
        meta = labels.get(account["broker"], {})
        suffix = " [read-only]" if meta.get("read_only") else ""
        display = meta.get("display_id") or ""
        render(
            f"{account['broker']} {display}{suffix}",
            account["windows"],
            account["current_nav"],
        )
        if account["error"]:
            print(_colour(f"  {account['error']}", YELLOW))

    for group in data["by_broker"]:
        if len(group["accounts"]) > 1:
            render(
                f"{group['broker_type'].upper()} (all accounts)",
                group["windows"], group["current_nav"],
            )

    render("COMBINED", data["combined"]["windows"], data["combined"]["current_nav"])

    partial = [
        w["label"]
        for w in data["combined"]["windows"]
        if not w["complete"] and w["twr_pct"] is not None
    ]
    if partial:
        print(_colour(
            f"\n* {', '.join(partial)} cover less than the full period — "
            "history does not reach that far back yet.", YELLOW))
    notes = {
        w["note"] for w in data["combined"]["windows"] if w["note"]
    }
    for note in sorted(notes):
        print(f"{DIM}  {note}{RESET}")

    service.close()
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report which environment variables are set, and how to obtain the rest.

    Never prints a secret's value — only whether it is present — so this is
    safe to paste into an issue or share on a screen.
    """
    import json as _json
    import os

    from .config import SECRET_MARKERS

    if args.env_file:
        try:
            from dotenv import load_dotenv

            load_dotenv(args.env_file, override=False)
        except ImportError:
            pass

    # Pad before colouring: ANSI escapes make ljust count the wrong width.
    ok_mark = _colour("set    ", GREEN)
    missing = _colour("MISSING", RED)
    optional = _colour("unset  ", DIM)
    problems: list[str] = []

    # A variable that is present in the file but commented out, or present
    # with an empty value, looks identical to "never set" once the process
    # environment is loaded. That is a confusing failure, so read the file
    # back and say which it is. Only variable NAMES are inspected here —
    # values are never read or printed.
    commented: set[str] = set()
    blank: set[str] = set()
    env_path = Path(args.env_file) if args.env_file else None
    if env_path and env_path.exists():
        try:
            for line in env_path.read_text(errors="replace").splitlines():
                stripped = line.strip()
                if "=" not in stripped:
                    continue
                if stripped.startswith("#"):
                    name = stripped.lstrip("#").split("=", 1)[0].strip()
                    if name.replace("_", "").isalnum():
                        commented.add(name.upper())
                else:
                    name, _, value = stripped.partition("=")
                    if not value.strip():
                        blank.add(name.strip().upper())
        except OSError:
            pass

    def show(name: str, *, required: bool, hint: str, note: str = "") -> bool:
        raw = os.environ.get(name, "").strip()
        present = bool(raw)
        secret = any(m in name.upper() for m in SECRET_MARKERS)
        if present:
            shown = "********" if secret else raw
            print(f"  {ok_mark}  {name:<28} = {shown}")
            if note:
                print(f"          {DIM}{note}{RESET}")
            return True
        # Distinguish "never mentioned" from the two cases that look identical
        # once loaded but are almost always an editing slip.
        if name.upper() in commented:
            print(f"  {_colour('COMMENT', YELLOW)}  {name:<28} "
                  f"{DIM}present in {env_path} but COMMENTED OUT — "
                  f"remove the leading '#'{RESET}")
            problems.append(f"{name} (commented out)")
            return False
        if name.upper() in blank:
            print(f"  {missing if required else optional}  {name:<28} "
                  f"{DIM}present in {env_path} but has no value{RESET}")
            if required:
                problems.append(name)
            return False

        print(f"  {missing if required else optional}  {name:<28} {DIM}{hint}{RESET}")
        if required:
            problems.append(name)
        return False

    print(f"\n{_colour('CONFIGURATION CHECK', YELLOW)}")
    print(f"{DIM}Reading {args.env_file or 'the process environment'}. "
          f"Secret values are never printed.{RESET}")

    # --- 1. things you can set right now ---------------------------------
    print(f"\n{_colour('1. Set these yourself — no signup needed', BLUE)}")
    show("TRADING_MODE", required=False,
         hint="defaults to dry_run (safe). live requires LIVE_TRADING_ENABLED too.")
    show("API_TOKEN", required=True,
         hint="generate: python -c \"import secrets; print(secrets.token_urlsafe(32))\"")
    show("HOST_API_PORT", required=False,
         hint="Docker host port. Set only if 8787 is taken on this machine.")

    # --- 2. Schwab -------------------------------------------------------
    print(f"\n{_colour('2. Schwab — from developer.schwab.com', BLUE)}")
    print(f"{DIM}  Create ONE app with BOTH products: 'Accounts and Trading "
          f"Production'\n  and 'Market Data Production'. Approval is not "
          f"instant. Then copy its\n  App Key and App Secret.{RESET}")
    has_key = show("SCHWAB_APP_KEY", required=False,
                   hint="developer.schwab.com -> Dashboard -> your app")
    show("SCHWAB_APP_SECRET", required=False,
         hint="same page as the App Key")
    show("SCHWAB_CALLBACK_URL", required=False,
         hint="defaults to https://127.0.0.1:8182/ — must match the app exactly")
    show("SCHWAB_LOGINS", required=False,
         hint="one label per Schwab user ID, e.g. personal,joint")

    # Token files are the thing people actually get wrong, and the 7-day
    # expiry is invisible until it bites.
    try:
        settings = load_settings(None)
        logins = settings.schwab_logins
    except Exception:
        settings = None
        logins = tuple(
            l.strip() for l in os.environ.get("SCHWAB_LOGINS", "default").split(",")
            if l.strip()
        ) or ("default",)

    if has_key:
        print(f"\n  {DIM}Authorisation state per login:{RESET}")
        for login in logins:
            path = (
                settings.schwab_token_path_for(login) if settings
                else Path(f"var/state/schwab_token_{login}.json")
            )
            if not path.exists():
                print(f"  {missing}  login {login!r}: no token at {path}")
                print(f"          {DIM}run: python -m trading.cli "
                      f"schwab-login --login {login}{RESET}")
                problems.append(f"schwab token for {login}")
                continue
            try:
                data = _json.loads(path.read_text())
                obtained = float(data.get("obtained_at", 0))
                age_days = (time.time() - obtained) / 86400 if obtained else None
            except Exception:
                print(f"  {missing}  login {login!r}: token file unreadable")
                problems.append(f"schwab token for {login}")
                continue
            if age_days is None:
                print(f"  {ok_mark}  login {login!r}: token present ({path})")
            elif age_days >= 7:
                print(f"  {missing}  login {login!r}: refresh token EXPIRED "
                      f"({age_days:.1f} days old)")
                print(f"          {DIM}run: python -m trading.cli "
                      f"schwab-login --login {login}{RESET}")
                problems.append(f"expired schwab token for {login}")
            else:
                left = 7 - age_days
                colour = YELLOW if left < 2 else GREEN
                print(f"  {ok_mark}  login {login!r}: valid, "
                      f"{_colour(f'{left:.1f} days left', colour)}")

    # --- 3. IBKR ---------------------------------------------------------
    print(f"\n{_colour('3. Interactive Brokers — from the Client Portal Gateway', BLUE)}")
    show("IBKR_BASE_URL", required=False,
         hint="defaults to https://localhost:5000/v1/api; "
              "in Docker use https://host.docker.internal:5000/v1/api")
    show("IBKR_ACCOUNT_ID", required=False,
         hint="e.g. U1234567 (live) or DU1234567 (paper). Leave empty to use all.")
    show("IBKR_CONID_QQQ", required=False,
         hint="optional pin; discover with cli accounts or secdef/search")

    # --- 4. discovered after authorising ---------------------------------
    print(f"\n{_colour('4. Discovered AFTER you authorise — not needed first', BLUE)}")
    print(f"{DIM}  Leave TRADING_ACCOUNTS empty for the first run, then run\n"
          f"  `python -m trading.cli accounts` and copy the last 4 digits of\n"
          f"  each account you want to trade.{RESET}")
    show("TRADING_ACCOUNTS", required=False,
         hint="EMPTY MEANS EVERY AUTHORISED ACCOUNT CAN TRADE",
         note="restricted to these accounts; all others are read-only")

    # --- verdict ---------------------------------------------------------
    print()
    mode = os.environ.get("TRADING_MODE", "dry_run").strip().lower()
    if settings is None:
        try:
            load_settings(None)
        except Exception as exc:
            print(_colour(f"CONFIG ERROR: {exc}", RED))
            return 2

    if problems:
        print(_colour(f"Incomplete: {len(problems)} item(s) need attention.", YELLOW))
        for item in problems:
            print(f"  - {item}")
        if mode == "dry_run":
            print(f"\n{DIM}You can still run in dry_run with what you have; "
                  f"missing brokers are simulated.{RESET}")
        return 1

    print(_colour("Configuration looks complete.", GREEN))
    print(f"{DIM}Next: python -m trading.cli accounts{RESET}")
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    settings = load_settings(args.env_file)
    service = TradingService(settings)
    errors = service.connect_all()

    print(f"\n{_mode_banner(settings)}\n")
    for name, broker in sorted(service.brokers.items()):
        account = broker.account
        display = account.display_id if account else "—"
        paper = " [PAPER]" if account and account.is_paper else ""
        if broker.read_only:
            permission = _colour("READ-ONLY", YELLOW)
        else:
            permission = _colour("TRADABLE", GREEN)
        state = (
            _colour("connected", GREEN) if broker.connected
            else _colour("DISCONNECTED", RED)
        )
        print(f"{name:<26} {display:<12}{paper:<9} {permission:<22} {state}")
        if errors.get(name):
            print(f"  {_colour(errors[name], RED)}")

    if settings.trading_accounts:
        print(f"\n{DIM}TRADING_ACCOUNTS allowlist is active "
              f"({len(settings.trading_accounts)} entries). Accounts outside it "
              f"are shown but cannot trade.{RESET}")
    else:
        print(f"\n{_colour('No TRADING_ACCOUNTS allowlist set — every '
                           'authorised account can trade.', YELLOW)}")
    service.close()
    return 0


def cmd_trade(args: argparse.Namespace, command: Command) -> int:
    settings = load_settings(args.env_file)
    service = TradingService(settings)

    print(f"\n{_mode_banner(settings)}")
    print(f"Command: {_colour(command.value, YELLOW)}\n")

    errors = service.connect_all()
    for name, err in errors.items():
        if err:
            print(_colour(f"! {name} failed to connect: {err}", RED))

    confirm_token: str | None = None
    for attempt in range(2):
        try:
            result = service.execute(
                command,
                request_id=args.request_id,
                confirm_token=confirm_token,
                force=args.force,
                source="cli",
            )
            break
        except ConfirmationRequired as exc:
            if args.yes:
                confirm_token = exc.token
                print(_colour("Confirmation auto-accepted via --yes", YELLOW))
                continue
            print(_colour("\nCONFIRMATION REQUIRED", RED))
            print(str(exc))
            phrase = settings.confirmation_phrase
            answer = input(f'Type "{phrase}" to proceed: ').strip()
            if answer != phrase:
                print("Aborted; nothing was sent.")
                service.close()
                return 2
            confirm_token = exc.token
        except TradingError as exc:
            print(_colour(f"\nBLOCKED: {type(exc).__name__}: {exc}", RED))
            service.close()
            return 2
    else:
        print(_colour("Could not obtain confirmation.", RED))
        service.close()
        return 2

    print(_print_result(result))
    service.close()
    return 0 if result.status == "OK" else 1


def _print_result(result) -> str:
    lines = [""]
    colour = {"OK": GREEN, "PARTIAL": RED, "FAILED": RED, "ABORTED": YELLOW}.get(
        result.status, YELLOW
    )
    lines.append(_colour(f"=== {result.command.value}: {result.status} ===", colour))

    if result.mismatch:
        lines.append(_colour(
            "!! BROKERS ARE NOT IN THE SAME STATE. Do not assume the command "
            "succeeded. Review both accounts before trading again.", RED))

    for r in result.results:
        status = _colour("OK", GREEN) if r.ok else _colour("FAILED", RED)
        lines.append(f"\n{r.broker.upper()}: {status}  "
                     f"{r.exposure_before.value} -> {r.exposure_after.value}")
        for fill in r.fills:
            lines.append(
                f"  {fill.side.value:<4} {fill.filled_quantity:g}/"
                f"{fill.requested_quantity:g} {fill.symbol} @ "
                f"${fill.average_price:,.4f}  (${fill.notional:,.2f}, "
                f"{fill.reprice_count} reprice(s))"
            )
            for note in fill.notes:
                lines.append(f"       {DIM}{note}{RESET}")
        lines.append(f"  cash: ${r.cash_before:,.2f} -> ${r.cash_after:,.2f}")
        for w in r.warnings:
            lines.append(_colour(f"  ! {w}", YELLOW))
        for e in r.errors:
            lines.append(_colour(f"  ERROR: {e}", RED))
        if r.needs_attention:
            lines.append(_colour(
                "  ** NEEDS ATTENTION: this account may be in an "
                "intermediate state. **", RED))
    return "\n".join(lines) + "\n"


def cmd_acknowledge(args: argparse.Namespace) -> int:
    settings = load_settings(args.env_file)
    service = TradingService(settings)
    mismatch = service.state.data.mismatch
    if not mismatch:
        print("No outstanding mismatch.")
        return 0
    print(json.dumps(mismatch, indent=2, default=str))
    if not args.yes:
        answer = input(
            "\nHave you checked BOTH accounts and confirmed their real "
            "positions? Type ACKNOWLEDGE: "
        ).strip()
        if answer != "ACKNOWLEDGE":
            print("Not acknowledged.")
            return 2
    service.acknowledge_mismatch(args.note)
    print("Acknowledged. Trading is unblocked.")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from .api import create_app
    import uvicorn

    settings = load_settings(args.env_file)
    token = ensure_api_token(settings)
    app = create_app(settings)
    print(f"\n{_mode_banner(settings)}")
    print(f"Dashboard: http://{settings.api_host}:{settings.api_port}/?token={token}")
    print(f"API token: {token}\n")
    uvicorn.run(app, host=settings.api_host, port=settings.api_port,
                log_level=settings.log_level.lower())
    return 0


def cmd_schwab_login(args: argparse.Namespace) -> int:
    """Interactive OAuth authorisation-code flow for the Schwab Trader API."""
    import base64
    import urllib.parse

    import httpx

    settings = load_settings(args.env_file)
    if not settings.schwab_app_key or not settings.schwab_app_secret:
        print(_colour("SCHWAB_APP_KEY and SCHWAB_APP_SECRET must be set.", RED))
        return 2

    login = args.login or settings.schwab_logins[0]
    if login not in settings.schwab_logins:
        print(_colour(
            f"Login {login!r} is not in SCHWAB_LOGINS "
            f"({', '.join(settings.schwab_logins)}). Add it there first.", RED))
        return 2
    token_path = settings.schwab_token_path_for(login)

    print(f"\nAuthorising Schwab login: {_colour(login, YELLOW)}")
    print(f"Token file: {token_path}")
    if len(settings.schwab_logins) > 1:
        print(f"{DIM}Each Schwab user ID needs its own run of this command, "
              f"and its own token file. Sharing one file between logins makes "
              f"each refresh invalidate the other.{RESET}")

    params = urllib.parse.urlencode({
        "client_id": settings.schwab_app_key,
        "redirect_uri": settings.schwab_callback_url,
        "response_type": "code",
    })
    auth_url = f"{settings.schwab_base_url}/v1/oauth/authorize?{params}"

    print("\n1. Open this URL and log in to Schwab with the credentials for")
    print(f"   the {login!r} login (your brokerage login, NOT the developer "
          "portal one):\n")
    print(f"   {auth_url}\n")
    print("2. TICK ONLY THE ACCOUNTS this program should reach. Schwab has no")
    print("   read-only scope, so any account you tick can be traded. Accounts")
    print("   you leave unticked are invisible to the program.")
    print("3. Approve. Your browser will be redirected to")
    print(f"   {settings.schwab_callback_url} — it will probably show an error")
    print("   page, which is fine.")
    print("4. Copy the ENTIRE redirected URL from the address bar and paste it "
          "below.\n")
    redirected = input("Redirected URL: ").strip()

    parsed = urllib.parse.urlparse(redirected)
    query = urllib.parse.parse_qs(parsed.query)
    code = (query.get("code") or [""])[0]
    if not code:
        print(_colour("No ?code= parameter found in that URL.", RED))
        return 2

    basic = base64.b64encode(
        f"{settings.schwab_app_key}:{settings.schwab_app_secret}".encode()
    ).decode()
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
    if resp.status_code != 200:
        print(_colour(f"Token exchange failed: HTTP {resp.status_code}", RED))
        print(resp.text[:500])
        return 2

    payload = resp.json()
    from .brokers.schwab import _TokenStore, discover_schwab_accounts

    store = _TokenStore(token_path)
    store.save({
        "access_token": payload["access_token"],
        "refresh_token": payload["refresh_token"],
        "expires_at": time.time() + float(payload.get("expires_in", 1800)),
        "obtained_at": time.time(),
        "login": login,
    })
    print(_colour(f"\nSaved to {token_path} (mode 0600).", GREEN))

    try:
        accounts = discover_schwab_accounts(settings, login)
    except Exception as exc:
        print(_colour(f"Could not list accounts: {exc}", YELLOW))
        accounts = []

    if accounts:
        print(f"\nAccounts authorised by the {login!r} login:")
        for account in accounts:
            tradable = settings.may_trade_account(account.display_id)
            mark = (_colour("tradable", GREEN) if tradable
                    else _colour("read-only", YELLOW))
            print(f"  {account.display_id}   {mark}")
        if settings.trading_accounts:
            print(f"\n{DIM}Trading is limited to TRADING_ACCOUNTS. Edit that "
                  f"variable to change which accounts can trade.{RESET}")
        else:
            print(_colour(
                "\nTRADING_ACCOUNTS is empty, so every account above can "
                "trade. Set it to restrict them.", YELLOW))

    print(_colour(
        "\nThe refresh token expires in 7 days. Re-run this command weekly "
        f"for the {login!r} login.", YELLOW))
    return 0


# --- argument parsing --------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qqq-switch",
        description="GO LONG (QQQ) / GO SHORT (PSQ) / GO FLAT across Schwab and IBKR",
    )
    parser.add_argument("--env-file", default=".env", help="path to the .env file")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_trade(name: str, help_text: str) -> None:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--force", action="store_true",
                       help="bypass the duplicate-command window")
        p.add_argument("--yes", action="store_true",
                       help="auto-accept the live confirmation prompt")
        p.add_argument("--request-id", default=None,
                       help="idempotency key; re-using one never trades twice")

    add_trade("long", "move the accounts into QQQ")
    add_trade("short", "move the accounts into PSQ")
    add_trade("flat", "sell QQQ/PSQ and stay in cash")

    p_status = sub.add_parser("status", help="show the dashboard in the terminal")
    p_status.add_argument("--json", action="store_true")

    p_hold = sub.add_parser("holdings", help="list every position in every account")
    p_hold.add_argument("--json", action="store_true")

    p_perf = sub.add_parser(
        "performance", help="account returns over 1M / 2M / 3M / 6M"
    )
    p_perf.add_argument("--json", action="store_true")
    p_perf.add_argument("--months", type=int, default=6,
                        help="longest window to report and chart")
    p_perf.add_argument("--no-backfill", action="store_true",
                        help="use only locally recorded history")

    sub.add_parser("accounts", help="list accounts and their trading permission")

    sub.add_parser(
        "doctor",
        help="check which environment variables are set and how to get the rest",
    )

    sub.add_parser("serve", help="run the local web dashboard and API")

    p_login = sub.add_parser(
        "schwab-login", help="authorise one Schwab login for the Trader API"
    )
    p_login.add_argument(
        "--login", default=None,
        help="label from SCHWAB_LOGINS; run once per Schwab user ID",
    )

    p_ack = sub.add_parser("acknowledge", help="clear a broker-mismatch block")
    p_ack.add_argument("--yes", action="store_true")
    p_ack.add_argument("--note", default="")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers: dict[str, Any] = {
        "status": cmd_status,
        "holdings": cmd_holdings,
        "performance": cmd_performance,
        "accounts": cmd_accounts,
        "doctor": cmd_doctor,
        "serve": cmd_serve,
        "schwab-login": cmd_schwab_login,
        "acknowledge": cmd_acknowledge,
        "long": lambda a: cmd_trade(a, Command.GO_LONG),
        "short": lambda a: cmd_trade(a, Command.GO_SHORT),
        "flat": lambda a: cmd_trade(a, Command.GO_FLAT),
    }
    try:
        return handlers[args.cmd](args)
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
