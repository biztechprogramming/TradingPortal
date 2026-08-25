# Running in Docker

Three things about this system make containerising it more than "write a
Dockerfile", and they are worth understanding before you start:

1. **The Schwab login is interactive.** You paste a URL out of a browser. That
   needs a TTY, so it is a `docker compose run -it` step, not part of `up`.
2. **The IBKR gateway needs a browser login too**, and it is a separate Java
   process. You either run it on the host or automate it in a sibling
   container — each has a real trade-off.
3. **State must persist.** The Schwab refresh token expires after 7 days and
   cannot be regenerated without a browser. Lose the volume, lose your
   authorisation.

---

## Quick start

```bash
cp .env.example .env
chmod 600 .env
# Set at minimum: API_TOKEN, and your broker credentials.
python -c "import secrets; print(secrets.token_urlsafe(32))"   # for API_TOKEN

docker compose build
docker compose run --rm app doctor      # what is set, what is missing, and why
docker compose run --rm -it app schwab-login --login default
docker compose up -d

docker compose ps                       # should show (healthy)
docker compose run --rm app accounts    # verify trading permissions
```

Dashboard: `http://127.0.0.1:8787/` — the token is in your `.env`.

> If port 8787 is already used on your machine, set `API_PORT=8790` in `.env`.
> That single variable moves both the container's listener and the published
> host port, so the two can never disagree. (`HOST_API_PORT` exists only for
> the unusual case of wanting them to differ.)

---

## What lives where

| Path | Contents | Survives `down`? |
|---|---|---|
| volume `qqq-switch-state` → `/app/var` | Schwab tokens, NAV history, command state, execution lock, audit logs | **yes** |
| `.env` on the host | every credential | yes (never in the image) |
| image | code only | rebuilt freely |

Nothing secret is baked into the image. `.dockerignore` keeps `.env`, `var/`
and any `schwab_token*.json` out of the build context entirely, so a
`docker save` or a pushed image cannot leak them.

### Back up the state volume

Worth doing before any upgrade — it is the only copy of your Schwab
authorisation and your measured NAV history.

```bash
docker run --rm -v qqq-switch-state:/var -v "$PWD":/backup alpine \
  tar czf /backup/qqq-state-$(date +%F).tgz -C /var .
```

Restore:

```bash
docker run --rm -v qqq-switch-state:/var -v "$PWD":/backup alpine \
  sh -c "cd /var && tar xzf /backup/qqq-state-YYYY-MM-DD.tgz"
```

---

## Schwab: authorising inside the container

The flow is interactive, so it needs `-it`. Run it **once per login**:

```bash
docker compose run --rm -it app schwab-login --login personal
docker compose run --rm -it app schwab-login --login joint
```

Outside Docker, `schwab-login` catches the redirect itself. In a container it
cannot: the browser's `127.0.0.1:8182` is the *host*, while the listener would
bind inside the container. Two ways round it:

```bash
# (a) publish the port so the host's browser reaches the listener
docker compose run --rm -it -p 127.0.0.1:8182:8182 app schwab-login --login personal

# (b) skip the listener and paste the redirected URL by hand
docker compose run --rm -it app schwab-login --login personal --no-listener
```

Prefer (a). With (b) you are racing Schwab's ~30-second code expiry with a
copy-and-paste, which is the usual cause of `invalid_grant`.

Tokens land in the volume, so `docker compose down` and rebuilds do not lose
them.

**Weekly:** the refresh token expires after 7 days. Re-run the same command.
Set a calendar reminder on a non-trading day.

---

## IBKR: two ways to connect the gateway

### Option A — gateway on the host (default, and what I would start with)

Run the Client Portal Gateway on the host as described in
[IBKR_SETUP.md](IBKR_SETUP.md), log in through a browser, and let the
container reach it:

```bash
# .env
IBKR_BASE_URL=https://host.docker.internal:5000/v1/api
IBKR_VERIFY_SSL=false
```

`host.docker.internal` is already wired up in `docker-compose.yml` via
`extra_hosts: host-gateway`, which is what makes this work on Linux as well as
Docker Desktop.

**Trade-off:** you log in manually through a browser, and again after the
gateway restarts or its session ages out (~24h). Nothing is automated, and no
IBKR credentials are stored anywhere.

### Option B — IBeam sibling container (the default)

```bash
# .env
IBEAM_ACCOUNT=your_ibkr_username
IBEAM_PASSWORD=your_ibkr_password
IBKR_BASE_URL=https://ibeam:5000/v1/api
```

```bash
docker compose up -d
```

IBeam starts alongside the app. If you chose Option A instead, comment the
`ibeam` service out of `docker-compose.yml` — two gateways on one IBKR login
fight over the session and log each other out.

[IBeam](https://github.com/Voyz/ibeam) runs the gateway headlessly and injects
your credentials into its login page to keep the session alive.

**Read this before choosing it.** IBeam works by holding your IBKR username
and password in environment variables and typing them into a login form. Those
credentials sit in your `.env` and in the container's environment. IBeam's own
documentation recommends paper credentials for exactly this reason. Two-factor
authentication also complicates it.

This is a genuine convenience-versus-exposure trade, and it is yours to make.
If unattended operation matters more than credential isolation, use it — with
paper credentials first. Otherwise use Option A.

The `ibeam` service starts with `docker compose up -d`. Its variables are not
declared as required, so every other compose command still works if you
comment the service out.

---

## Running commands

The entrypoint dispatches to the CLI, so every subcommand works:

```bash
docker compose run --rm app status
docker compose run --rm app holdings
docker compose run --rm app performance
docker compose run --rm app accounts
docker compose run --rm app long          # respects TRADING_MODE
docker compose run --rm app flat
docker compose run --rm app acknowledge
docker compose run --rm -it app sh        # anything unrecognised runs verbatim
```

These share the same state volume as the running server, so the **execution
lock works across them** — a CLI command cannot race the web server, because
`flock` is held on a file both see.

---

## Security posture

What the compose stack does, and why:

| Setting | Effect |
|---|---|
| `ports: 127.0.0.1:…` | reachable only from the host, never the network |
| `API_HOST=0.0.0.0` inside | required in a container; the publish above is what contains it |
| `API_TOKEN` mandatory | config validation refuses a non-loopback bind without one, so this cannot be forgotten |
| `read_only: true` | the image filesystem is immutable; all writes go to the volume |
| `cap_drop: ALL`, `no-new-privileges` | no capabilities, no privilege escalation |
| non-root `uid 10001` | the process never runs as root |
| `tmpfs /tmp` | scratch space without a writable image layer |
| `TRADING_MODE=dry_run` in the image | the image itself can never trade; live requires explicit `.env` |

**Do not** change the port publish to `0.0.0.0` or put this behind a public
reverse proxy. One POST moves your entire account. If you need it from your
phone, use a VPN (Tailscale/WireGuard) as described in
[SHORTCUTS.md](SHORTCUTS.md).

---

## Timezone

The image installs `tzdata` and sets `TZ=America/New_York`. This matters more
than it looks: the market-hours guard converts to Eastern, and without a
timezone database it could only compute the session in UTC — permitting
pre-market trading and blocking the afternoon, silently.

The code now **fails closed** if the timezone database is missing: it reports
"timezone database unavailable" and refuses to trade rather than guessing. If
you ever see that, your base image lost `tzdata`.

Verify:

```bash
docker compose run --rm app python -c \
  "from trading.validation import TZ_AVAILABLE, market_status; \
   print(TZ_AVAILABLE, market_status().now_eastern)"
```

---

## Upgrading

```bash
docker compose down
git pull
docker compose build --no-cache
docker compose up -d
docker compose run --rm app accounts     # confirm permissions after upgrade
```

The state volume is untouched by this, so tokens and NAV history carry over.
Back it up first anyway.

---

## Running it as a service

`restart: unless-stopped` handles daemon restarts and reboots once Docker
itself starts on boot:

```bash
sudo systemctl enable docker
```

For a stricter setup, a systemd unit wrapping compose:

```ini
[Unit]
Description=QQQ/PSQ Switch
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/srv/environments/dev/trade
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
```

Logs are JSON-file with rotation (10 MB × 5) so they cannot fill the disk. The
audit log rotates daily by filename inside the volume and is **not** rotated
away — it is your trade record, so prune it deliberately.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Bind for 127.0.0.1:8787 failed: port is already allocated` | Something else holds the port. Set `API_PORT=8790` in `.env`. |
| `API_TOKEN is required when API_HOST is not loopback` | Working as intended. Set `API_TOKEN` in `.env`. |
| `FATAL: /app/var/state is not writable` | Volume owned by the wrong uid. `docker compose down -v` and recreate, or `docker compose run --user $(id -u):$(id -g) …`. |
| `Is the Client Portal Gateway running` | With Option A, check `IBKR_BASE_URL` uses `host.docker.internal`, not `localhost` — inside a container `localhost` is the container. |
| Schwab token lost after a rebuild | You are not using the named volume, or ran `down -v`. |
| Container is `unhealthy` | `docker compose logs app`. Usually a config error surfaced at startup. |
| `schwab-login` shows no prompt | Missing `-it`. Use `docker compose run --rm -it app schwab-login`. |
| Everything is `dry_run` unexpectedly | `.env` is not being read. It must sit beside `docker-compose.yml`; it is loaded as optional, so a typo in the filename fails silently. |

---

## Development

To run the tests inside the image (dev dependencies are not in the runtime
image, so install them in a throwaway layer):

```bash
docker run --rm -v "$PWD":/src -w /src python:3.12-slim sh -c \
  "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

Or just run them on the host — `python -m pytest -q` needs no broker and no
network.
