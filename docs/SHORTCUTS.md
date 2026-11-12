# One-tap triggers: iPhone Shortcut, Stream Deck, keyboard, voice

Three endpoints, one bearer token. Anything that can make an HTTP POST can
drive this system.

```
POST /api/long     GO LONG  -> QQQ
POST /api/short    GO SHORT -> PSQ
POST /api/flat     GO FLAT  -> cash
```

All three require `Authorization: Bearer <API_TOKEN>` (or an `X-API-Token`
header, or `?token=…` for clients that cannot set headers).

---

## Before anything else: reachability

The server binds to `127.0.0.1` by default, which your phone cannot reach. Pick
one:

| Option | Security | Notes |
|---|---|---|
| **Tailscale** (recommended, and what `./start` does) | Best | `./start` binds the machine's Tailscale IPv4 when the tailnet is up, so any machine on it reaches `http://<tailscale-ip>:8787/`. Nothing is exposed to the LAN or publicly. `API_HOST=127.0.0.1` stays the default when Tailscale is down. |
| SSH tunnel from the phone | Good | Works with Shortcuts + a terminal app, fiddly. |
| LAN bind (`API_HOST=0.0.0.0`) | Weakest | Anyone on the network can reach the port. Only with a strong token, and never on untrusted Wi-Fi. |

**Never port-forward this to the public internet.** A single POST moves your
entire account.

---

## iPhone Shortcut

### Simple version — dry-run/paper, or live with `REQUIRE_CONFIRMATION=false`

1. Shortcuts → **+** → **Add Action** → *Get Contents of URL*.
2. URL: `http://<host>:8787/api/long`
3. Method: **POST**
4. Headers:
   - `Authorization` = `Bearer YOUR_API_TOKEN`
   - `Content-Type` = `application/json`
5. Request Body → **JSON**:
   - `command` (Text) = `GO_LONG`
   - `request_id` (Text) = tap the variable button → **Current Date** →
     format as *ISO 8601*. This makes an accidental double-tap idempotent.
6. Add *Get Dictionary Value* → key `status`.
7. Add *Show Notification* with that value.
8. Name it **GO LONG**, give it the green icon, **Add to Home Screen**.

Duplicate twice for `/api/short` (`GO_SHORT`) and `/api/flat` (`GO_FLAT`).

### Live version — handles the confirmation handshake

With `REQUIRE_CONFIRMATION=true` (the default for live), the first POST returns
**409** with a single-use `confirm_token`. Two extra actions handle it:

1. *Get Contents of URL* — POST `/api/long` as above → **Contents of URL**.
2. *Get Dictionary Value* → key `confirm_token` → variable `Token`.
3. *If* `Token` **has any value**:
   - *Ask for Confirmation*: "Deploy the whole account LONG QQQ?"
   - *Get Contents of URL* — POST `/api/long`, JSON body:
     - `command` = `GO_LONG`
     - `confirm_token` = `Token`
     - `force` = `true` (the first attempt already consumed the duplicate
       window)
   - *Show Notification* with the result.
4. *Otherwise*: *Show Notification* with the first response.

The confirmation token expires after `CONFIRMATION_TTL_SECONDS` (default 60),
so the tap-to-confirm has to be prompt — which is the point.

### Voice

Siri runs any shortcut by name: *"Hey Siri, GO FLAT."* Name them exactly as you
want to say them. Consider naming the live ones something you would never say
by accident near the phone.

---

## Stream Deck

**System → Website**, or the *Open* action, will not send headers. Use a
**System → Open** action pointing at a small script instead:

```bash
#!/usr/bin/env bash
# ~/bin/go-long.sh
set -euo pipefail
source ~/.config/qqq-switch/env      # exports API_TOKEN, API_URL
curl -fsS -X POST "$API_URL/api/long" \
  -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"command\":\"GO_LONG\",\"request_id\":\"deck-$(date +%s)\"}" \
  | jq -r '.status'
```

```bash
chmod 700 ~/bin/go-long.sh
chmod 600 ~/.config/qqq-switch/env
```

Set the button title to **GO LONG**, colour it green; red for SHORT, grey for
FLAT.

## Keyboard shortcut (macOS)

Automator → *Quick Action* → *Run Shell Script* with the same curl, then bind
it under **System Settings → Keyboard → Keyboard Shortcuts → Services**.

## Keyboard shortcut (Linux)

```bash
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings \
  "['/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom0/']"
# then set name/command/binding for custom0 to ~/bin/go-long.sh
```

## Command line

```bash
./qqq long      # prompts for confirmation in live mode
./qqq flat
./qqq status
```

---

## Response reference

| HTTP | `status` | Meaning |
|---|---|---|
| 200 | `OK` | Every broker completed. |
| 207 | `PARTIAL` | **One broker succeeded, another did not.** Trading is now blocked until acknowledged. Open the dashboard. |
| 400 | `ERROR` | Rejected before execution (bad command, quote failure). |
| 409 | `CONFIRMATION_REQUIRED` | Re-send with `confirm_token`. |
| 409 | `DUPLICATE` | Same command or `request_id` too recent. |
| 409 | `NEEDS_ACKNOWLEDGEMENT` | A prior mismatch is unresolved. |
| 409 | `BLOCKED` | Market closed, or a daily/notional cap was hit. |
| 423 | `LOCKED` | Another command is running. |
| 502 | `FAILED` | Every broker failed. |
| 401 | — | Bad or missing token. |

Have your shortcut surface anything that is not 200 — **207 in particular must
never be treated as success.**

---

## Token hygiene

- Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`
- Store it in `.env` (mode 600) on the server.
- On iPhone, the token lives inside the Shortcut. Anyone with your unlocked
  phone can trade your account — use Face ID on the shortcut, or keep the live
  ones off the Home Screen.
- Rotate by changing `API_TOKEN` and restarting; update each client.
