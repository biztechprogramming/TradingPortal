# Safety mechanisms

Every guard in the system, what it prevents, how it is configured, and where it
is tested.

---

## 1. Nothing trades by default

| Mechanism | Detail |
|---|---|
| `TRADING_MODE=dry_run` is the default | Absent configuration, the system cannot place an order. |
| Two-key live trading | `TRADING_MODE=live` is **refused** unless `LIVE_TRADING_ENABLED=true`. Live trading can never be enabled by editing one variable. |
| Structural dry-run | In dry-run, the broker object is a `ShadowBroker` that has **no code path to the live order endpoints**. Safety comes from the object graph, not from an `if` inside the adapter that a future edit could bypass. |
| Explicit API token for live | `validate_settings` refuses live mode without `API_TOKEN`, so the token cannot silently rotate on restart. |

Tests: `test_safety.py::test_shadow_broker_never_touches_the_real_order_endpoints`,
`::test_live_mode_requires_the_second_switch`, `::test_live_mode_requires_an_explicit_api_token`.

## 2. Paper and live are distinguished, loudly

- IBKR paper accounts are detected by their id prefix (`DU…`/`DF…` = paper,
  `U…` = live).
- `TRADING_MODE=paper` with a **live** IBKR account is a hard `ConfigError`.
- `TRADING_MODE=live` with a paper account raises a visible warning.
- **Schwab has no paper environment.** In `paper` mode the Schwab adapter is
  wrapped in `ShadowBroker` rather than being allowed to send "paper" orders
  that would in fact be real.
- The dashboard banner is blue (dry run), green (paper), or pulsing red
  (`⚠ LIVE TRADING — REAL MONEY`). The CLI prints the same banner.

Tests: `::test_paper_mode_refuses_to_send_real_schwab_orders`,
`::test_paper_mode_rejects_a_live_ibkr_account`.

## 3. Credentials

- No key, secret, token, account number or password is hard-coded. All come
  from the environment (`.env`, a systemd `EnvironmentFile`, or a secrets
  manager that exports env vars).
- `.env` and `var/` are git-ignored.
- The Schwab token file is written atomically with mode `0600`.
- **Redaction is by key name, not by allowlist.** Any audit field whose name
  contains `SECRET`, `TOKEN`, `PASSWORD`, `KEY`, `CREDENTIAL` or `AUTH` is
  written as `***REDACTED***`, so a field added later is protected by default.
- A logging filter scrubs `Bearer …` and `*_token` patterns from log messages.
- Token-refresh failures deliberately exclude the response body, which echoes
  token material on some error paths.
- Account numbers are masked to `...1234` everywhere they are displayed; the
  full number never appears in logs or API responses.

Tests: `::test_secrets_are_redacted_from_audit_payloads`,
`::test_settings_public_dict_leaks_nothing`, `::test_log_scrubber_removes_bearer_tokens`.

## 3b. Account permissions

Schwab has no read-only API scope: an account the app can see is an account it
can trade. The `TRADING_ACCOUNTS` allowlist restores the separation in
software.

- Accounts outside the allowlist are wrapped in `ReadOnlyBroker`, whose
  `submit_limit_order`, `replace_order` and `cancel_order` **raise instead of
  delegating** — the wrapped adapter is never called, so this is structural
  rather than a flag.
- Read-only accounts still appear on the dashboard with full holdings and
  performance, and their cash counts toward combined totals.
- The orchestrator dispatches commands only to tradable accounts, so a
  read-only account can never appear as a failure in a command result either.
- An empty allowlist means *every* authorised account can trade. `cli accounts`
  prints the permission for each account; check it before enabling live
  trading.
- Two accounts resolving to the same internal name is a hard error rather than
  a silent overwrite, which would drop an account from every command and total.

Tests: `test_accounts.py::test_readonly_broker_refuses_every_write`,
`::test_readonly_account_is_never_traded_but_is_still_shown`,
`::test_allowlist_splits_tradable_from_readonly`.

## 3c. Credential isolation between logins

Each Schwab login gets its own token file. Sharing one between logins makes
each refresh invalidate the other's refresh token, locking both out in a loop.
The default paths are derived per login so this cannot happen by accident.

Tests: `test_accounts.py::test_each_login_gets_its_own_token_file`.

## 4. Pre-submit validation

`validation.validate_order()` runs before **every** order, including in
dry-run. It checks:

- **Ticker** is on the allowlist (`LONG_SYMBOL`/`SHORT_SYMBOL` only). Any other
  symbol is refused — a bug elsewhere cannot cause this system to trade AAPL.
- **Side** is a `Side` enum, not a string.
- **Quantity** is positive and a whole number of shares.
- **Limit price** is positive.
- **Account** is selected and non-empty.
- **Estimated order value** is within `MAX_ORDER_NOTIONAL_DOLLARS` and
  `MAX_SHARES_PER_ORDER`.
- **Buys**: estimated cost ≤ available cash − buffer.
- **Sells**: quantity ≤ shares actually held. *The system never sells short.*

Tests: `test_validation.py` (14 cases).

## 5. Never overspend

- Sizing divides by the **limit price**, not the ask or last trade. Because
  every order is a limit order, `quantity × limit_price` is the true worst-case
  cost, which makes an insufficient-funds rejection structurally impossible
  short of a broker-side balance change.
- `CASH_BUFFER_DOLLARS` (default $25) is withheld to absorb fees and rounding.
- A float-safety loop decrements the quantity if rounding produces a cost even
  a cent over budget.
- **Cash only by default.** `ALLOW_MARGIN=false` takes the *minimum* of reported
  cash and reported buying power, so a margin-enabled account cannot lever up
  because the broker advertises 4× buying power. Schwab margin accounts are
  read via `availableFundsNonMarginableTrade`; IBKR uses `min(availablefunds,
  settledcash)`.
- Repricing a buy shrinks the order rather than breaching the budget (see
  `LIMIT_ALGORITHM.md` §4).

Tests: `test_sizing.py::test_estimated_cost_never_exceeds_available_funds`
sweeps 25 cash/price combinations; `::test_margin_is_not_used_by_default`.

## 6. GO SHORT is not a short sale

`GO SHORT` buys **PSQ**, the inverse ETF. The system:

- never submits `SELL_SHORT` (Schwab payloads always use `SELL`),
- never sells more shares than are held,
- never borrows.

## 7. No duplicate orders

Two independent guards:

- **Time window.** Re-issuing the *same* command within
  `DUPLICATE_COMMAND_WINDOW_SECONDS` (default 30) is refused. Catches a
  double-tapped button or a Shortcut fired twice.
- **Request id.** A client-supplied `request_id` is recorded persistently; the
  same id never executes twice, even across a restart. This makes an HTTP retry
  over a flaky phone connection safe.

Additionally, IBKR orders carry a client order id (`cOID`) so the broker itself
rejects an exact duplicate submission.

Tests: `::test_duplicate_command_inside_the_window_is_suppressed`,
`::test_request_id_makes_a_retry_idempotent`, `::test_request_id_dedupe_survives_a_restart`.

## 8. Execution lock

Commands are serialised by a lock that is both **in-process** (threading) and
**cross-process** (advisory `flock` on `var/state/execution.lock`). A CLI
invocation cannot race the web server, and a second CLI cannot race the first.

Without it, a LONG and a SHORT could interleave — one selling QQQ while the
other buys it — and leave the account somewhere neither command intended.

The lock file records the holder (`owner`, `pid`) so a contended command
reports *who* is holding it.

Tests: `::test_commands_cannot_run_simultaneously`,
`::test_long_and_short_serialise_under_contention` (asserts the account is
never left holding both instruments).

## 9. Reconcile, never assume

Before acting, the strategy re-reads positions, balances and open orders from
the broker. It never trusts what the previous command believed. After acting —
including after a failure — it re-reads again to report where the account
actually ended up.

Tests: `test_transitions.py::test_positions_are_reread_not_assumed`.

## 10. Conflicting orders are cancelled and *confirmed*

Any open order in QQQ or PSQ is cancelled before sizing, because a working
order can consume the cash we are about to commit. Crucially, each cancel is
polled until the broker confirms a terminal state. If a cancel is not confirmed
within `CANCEL_CONFIRM_TIMEOUT_SECONDS`, the command **aborts** — trading
around an order that might still fill is worse than not trading.

Tests: `test_execution.py::test_unconfirmed_cancel_raises_rather_than_proceeding`.

## 11. Adverse market conditions

| Condition | Behaviour |
|---|---|
| Stale quote | abort before ordering (`MAX_QUOTE_AGE_SECONDS`) |
| Halted security | abort |
| Zero / crossed / missing bid-ask | abort |
| Spread too wide | abort (`MAX_SPREAD_BPS`) |
| Delayed IBKR data | refuse unless `IBKR_ALLOW_DELAYED_QUOTES=true` |
| Price runs past the guard | abort with `MaxSlippageExceeded`, report partial fills |
| Market closed | refuse (`MARKET_HOURS_GUARD`) |

The holiday calendar is hard-coded for 2026 and does **not** encode half-days
(1 pm closes). It is a convenience guard, not an exchange calendar — the real
protection against odd sessions is the spread and staleness checks, which
tighten automatically when liquidity is poor.

Tests: `test_failures.py` (11 cases).

## 12. API failures and timeouts

- **Mutating requests (POST/PUT/DELETE) are never retried automatically.** A
  timed-out order submission may have been applied; retrying could double the
  position. The error carries `position_at_risk = True`, which marks the result
  `needs_attention` and surfaces it in the UI.
- Read requests retry with backoff.
- `429` honours `Retry-After`; Schwab requests are additionally throttled
  client-side to 100/min against the documented 120/min ceiling.
- `401/403` raise a distinct auth error naming the remedy (re-run
  `schwab-login`; log into the IBKR gateway).
- Unrecognised order statuses map to `UNKNOWN`, never to a terminal state — an
  unfamiliar status must never be mistaken for "filled" or "cancelled".

Tests: `::test_api_timeout_marks_the_result_as_needing_attention`,
`test_broker_schwab.py::test_post_timeouts_are_not_retried`.

## 13. One broker succeeding while the other fails

This is treated as an incident, not a partial success:

1. The command result status is `PARTIAL`, never `OK`.
2. HTTP returns **207**, so a caller checking only the status code cannot read
   it as success.
3. A `safety.broker_mismatch` audit event is written.
4. The mismatch is persisted to state and **blocks all further LONG/SHORT
   commands** until acknowledged via `POST /api/acknowledge` or
   `cli acknowledge`.
5. **GO FLAT remains available** — you must always be able to de-risk during an
   incident.
6. The dashboard shows a red banner with the summary and an Acknowledge button
   that requires a second confirmation.

Tests: `::test_one_broker_failing_reports_partial_not_success`,
`::test_a_mismatch_blocks_the_next_command_until_acknowledged`,
`::test_go_flat_is_allowed_while_a_mismatch_is_outstanding`,
`test_api.py::test_partial_outcome_is_not_a_200`.

## 14. Live confirmation

In live mode, `GO LONG` and `GO SHORT` require a deliberate confirmation
immediately before execution, because they deploy essentially the whole
account:

- The first request returns **409** with a single-use token and an expiry.
- The token is bound to its command (a LONG token cannot authorise a SHORT),
  expires after `CONFIRMATION_TTL_SECONDS`, and is consumed on use.
- The CLI prompts for the literal phrase `LIVE_CONFIRMATION_PHRASE`.
- `REQUIRE_CONFIRMATION=false` removes the prompt **but no other check** —
  validation, sizing, guards, locking and reconciliation all still run.
- `GO FLAT` is exempt by design.

Tests: `::test_confirmation_token_is_single_use`,
`::test_confirmation_token_is_bound_to_its_command`, `::test_confirmation_expires`,
`::test_go_flat_never_needs_confirmation`.

## 15. Runaway guards

- `MAX_DAILY_COMMANDS` (default 50) caps commands per UTC day.
- `MAX_REPRICE_ATTEMPTS` caps cancel/replace cycles per leg.
- `ORDER_FILL_TIMEOUT_SECONDS` caps how long any leg can run.
- The IBKR confirmation-reply loop is depth-limited to 5.
- `MAX_SLIPPAGE_BPS` above 1000 (10%) is refused outright as a fat-finger guard.

## 16. Unknown broker prompts stop the order

IBKR uses the same confirmation mechanism for benign notices and for genuine
risk warnings. Only an allowlist of known-safe prompts is auto-answered; an
unrecognised prompt **aborts the order** and reports the exact text.
`IBKR_AUTO_CONFIRM_ALL_QUESTIONS=true` opts out, and is documented as such.

Tests: `test_broker_ibkr.py::test_unrecognised_prompt_blocks_the_order`.

## 17. Audit log

Append-only JSON Lines, one file per UTC day, `fsync`'d on every write so a
crash cannot lose the record of an order that may already exist at the broker.

Recorded: every command received/rejected/started/finished, confirmations,
reconciliation snapshots, quotes and quote rejections, sizing decisions,
validation records, submissions, replacements, cancellations, status polls,
partial fills, fills, errors, mismatches, lock acquisition and contention.

Every record carries a sequence number, timestamps, the `command_id`, the
broker, and a redacted payload.

## 18. Network exposure

The API binds to `127.0.0.1` by default. A bearer token is required for every
route except `/api/health` (which reveals only the mode). Binding to a
non-loopback address without a token is refused at startup, and doing so with
one logs a prominent warning.

---

## What this system does *not* protect against

Stated plainly, because knowing the gaps is part of the safety story:

- **Your directional call.** It executes the trade you asked for.
- **Schwab has no paper API.** Schwab logic can only be exercised for real in
  live mode. Rehearse with dry-run against real balances, and size down first.
- **Half-day sessions** are not in the holiday calendar.
- **Cash-account settlement.** After a sale, proceeds may be unsettled (T+1).
  The system polls for reported availability and sizes against what the broker
  actually reports, warning when proceeds have not been credited — but
  repeatedly round-tripping unsettled funds in a *cash* account can still cause
  a good-faith violation. Use a margin account (without borrowing) to avoid it.
- **A compromised host.** Anyone with your `.env` and loopback access can trade.
