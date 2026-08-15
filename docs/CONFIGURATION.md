# Where each environment variable comes from

Run this at any point to see exactly what is set, what is missing, and what to
do about each — it never prints a secret's value:

```bash
./qqq doctor
# or, in Docker:
docker compose run --rm app doctor
```

---

## The order matters

Some values cannot be known until after you have authorised, so there is a
sequence. Trying to fill in `.env` top to bottom in one sitting will stall.

```
1. Generate what you own          -> API_TOKEN                (30 seconds)
2. Register a Schwab app          -> APP_KEY / APP_SECRET     (days: approval)
3. Authorise each Schwab login    -> token files              (5 min each)
4. DISCOVER your account numbers  -> TRADING_ACCOUNTS         (after step 3)
5. Start the IBKR gateway         -> IBKR_ACCOUNT_ID          (30 min)
```

**Leave `TRADING_ACCOUNTS` empty until step 4.** You cannot sensibly fill it in
before you have seen which accounts your logins actually expose. Stay in
`dry_run` until it is set — an empty allowlist means every authorised account
can trade.

---

## 1. Values you generate yourself

No signup, no waiting.

| Variable | How to get it |
|---|---|
| `API_TOKEN` | `python -c "import secrets; print(secrets.token_urlsafe(32))"` |
| `TRADING_MODE` | Your choice: `dry_run` (default), `paper`, `live` |
| `LIVE_TRADING_ENABLED` | `false` until you have finished the go-live checklist |
| `API_PORT` | The port to listen on (default 8787). Under Docker this moves both sides of the port mapping |
| `HOST_API_PORT` | Docker only, rarely needed: publish on a different host port than the container listens on |

`API_TOKEN` is mandatory before live trading and whenever the API is not bound
to loopback (which includes every Docker run). Generate it once and keep it —
it is what your iPhone Shortcut and Stream Deck will send.

---

## 2. Schwab app credentials

**From <https://developer.schwab.com>.** Approval is not instant — historically
days to a couple of weeks — so start this first.

1. Register with the same credentials as your brokerage login.
2. **Dashboard → Apps → Create App.**
3. Select **both** products:
   - *Accounts and Trading Production* — positions, balances, orders
   - *Market Data Production* — quotes and price history
   Missing the second is the most common setup mistake. Without it there are no
   quotes, and this system refuses to trade without a fresh quote, so every
   command aborts.
4. Callback URL: `https://127.0.0.1:8182/` — must match `SCHWAB_CALLBACK_URL`
   exactly, trailing slash included. Nothing needs to listen there.
5. Wait for the app to show **Ready For Use**.
6. Copy the App Key and App Secret.

| Variable | Value |
|---|---|
| `SCHWAB_APP_KEY` | App Key from the app's page |
| `SCHWAB_APP_SECRET` | App Secret from the same page |
| `SCHWAB_CALLBACK_URL` | `https://127.0.0.1:8182/` |

**One app serves every login.** The key and secret belong to the *app*, not to
a Schwab user ID.

---

## 3. Schwab logins and their token files

`SCHWAB_LOGINS` is a label per Schwab **user ID**. The names are yours; they
only decide which token file is which.

```bash
SCHWAB_LOGINS=personal,joint
```

Then authorise each one:

```bash
./qqq schwab-login --login personal
./qqq schwab-login --login joint
```

During each consent screen, **tick only the accounts this program should
reach**. Schwab has no read-only scope, so any account you tick can be traded;
unticked accounts stay invisible to the program.

Token files are written automatically to `<STATE_DIR>/schwab_token_<label>.json`.
You do not need to configure paths. Do **not** point two logins at one file —
each refresh would invalidate the other's, locking both out.

> `SCHWAB_TOKEN_PATH` is commented out in `.env.example` on purpose. Setting it
> overrides the automatic derivation, so `STATE_DIR` would stop moving the
> token files with the rest of your state — which in Docker means writing them
> outside the volume and losing them on restart.

**These expire every 7 days.** `doctor` shows the countdown per login:

```
set      login 'personal': valid, 6.8 days left
MISSING  login 'joint': refresh token EXPIRED (8.1 days old)
```

---

## 4. Which accounts may trade

Only knowable after step 3. Run:

```bash
./qqq accounts
```

```
schwab:personal:1111   ...1111   TRADABLE    connected
schwab:personal:2222   ...2222   TRADABLE    connected
schwab:joint:3333      ...3333   TRADABLE    connected
```

Copy the last 4 digits of the accounts you want to trade:

```bash
TRADING_ACCOUNTS=1111,2222,3333
```

Re-run `accounts` and confirm each shows the permission you intended. Anything
authorised but absent from this list becomes READ-ONLY: fully visible on the
dashboard, but every order path raises.

---

## 5. Interactive Brokers

| Variable | How to get it |
|---|---|
| `IBKR_BASE_URL` | `https://localhost:5000/v1/api` on the host; `https://host.docker.internal:5000/v1/api` from Docker |
| `IBKR_ACCOUNT_ID` | Shown in Client Portal / TWS. `U…` = live, `DU…` = paper. Leave empty to use all |
| `IBKR_ACCOUNT_IDS` | Comma-separated, if you have several |
| `IBKR_VERIFY_SSL` | `false` — the local gateway uses a self-signed certificate |
| `IBKR_CONID_QQQ` / `_PSQ` | Optional. Pin them so symbol lookup cannot resolve the wrong listing |

Start the gateway and log in first ([IBKR_SETUP.md](IBKR_SETUP.md)), then
`cli accounts` will show the account id — you do not have to hunt for it.

To find the conids once:

```bash
curl -k "https://localhost:5000/v1/api/iserver/secdef/search?symbol=QQQ&secType=STK"
```

`IBEAM_ACCOUNT` / `IBEAM_PASSWORD` are needed **only** if you opt into the
containerised gateway profile — see [DOCKER.md](DOCKER.md) for the trade-off
that involves.

---

## 6. Everything else

Every remaining variable in `.env.example` has a working default and is
documented inline there. The ones most worth a deliberate decision:

| Variable | Default | Why you might change it |
|---|---|---|
| `CASH_BUFFER_DOLLARS` | 25 | Larger if you want more headroom against fees |
| `MAX_ORDER_NOTIONAL_DOLLARS` | 0 (no cap) | **Set this small for your first live trade** |
| `MAX_SHARES_PER_ORDER` | 0 (no cap) | Same — start at 1 |
| `MAX_SLIPPAGE_BPS` | 50 | The hard abort guard |
| `MARKET_HOURS_GUARD` | true | Leave on |

---

## Keeping secrets out of the wrong places

- `.env` is git-ignored and excluded from the Docker build context.
- `chmod 600 .env`.
- Nothing secret is baked into the image; credentials arrive at runtime.
- The audit log redacts by key name, so any variable containing `TOKEN`,
  `SECRET`, `KEY`, `PASSWORD`, `CREDENTIAL` or `AUTH` is written as
  `***REDACTED***`.
- `doctor` prints `********` for secret-named variables, so its output is safe
  to share.

For stronger isolation than a dotfile, export the same variable names from a
secrets manager (systemd `EnvironmentFile`, Docker secrets, 1Password CLI,
Vault). The code only ever reads the environment — it has no idea where the
values came from.
