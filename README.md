# Gold Trader

Opens XAU/USD trades on MT5 and manages P1–P3 steps (partial close and stop moves), trailing after P3, retries, restart recovery and risk limits. Phase 2 adds the API server and a phone dashboard (installable on Android) to open trades and change SL, TP, steps and partial closes from anywhere.

```
engine/
  units.py        pips <-> price (10 pips = $1), volume rounding
  models.py       settings, presets, trade and step state
  validation.py   the spec's validation rules
  engine.py       the trading logic
  store.py        MemoryStore / JsonStore / SqliteStore (trades, presets, events, limits)
  broker.py       broker interface
  sim_broker.py   simulated broker for testing anywhere
  mt5_broker.py   real MT5 adapter (Windows only)
server/
  service.py      runs the engine loop in a thread, thread-safe calls for the API
  app.py          FastAPI routes (/api/...) + serves the web app, bearer-token auth
web/              the phone app (PWA: vanilla JS, no build step)
signals/
  parser.py       Claude turns a Telegram message into a structured signal
  mapper.py       signal + channel trust + preset rules -> trade plan (lot by weight, Far SL, P1-P3)
  service.py      confirm/auto flow, follow-ups (close, BE, partial), channel scorecards
  telegram.py     Telethon listener (your own account) running in a background thread
scripts/
  simulate.py     replay a price path with the simulator
  run_demo.py     CLI run against your MT5 terminal
  run_server.bat  start the server with settings from .env
  install_service.ps1  keep the server running on the VPS (Task Scheduler)
tests/
```

## Run it

```
pip install -r requirements.txt
copy .env.example .env        # set GT_TOKEN to a long random string
scripts\run_server.bat        # http://<vps-ip>:8080
```

Simulator (no MT5, no money): `set GT_BROKER=sim` then `py -m server`. Tests: `py -m pytest -q`.

Install always-on on the VPS (as Administrator): `powershell -ExecutionPolicy Bypass -File scripts\install_service.ps1`. The task runs at logon in the desktop session, because MT5 is a desktop app; keep the VPS logged in with the terminal open and **Algo Trading** enabled.

## The phone app

Open `http://<vps-ip>:8080` in Chrome on Android, enter the token, then **menu → Add to Home screen**. It runs full-screen like a native app.

- **Home**: balance, equity, today's and floating P&L, open trades with live pips/$ and P1–P3 progress, event feed.
- **Trade**: buy/sell, preset, lot, stop mode (Tight 20 / Medium 50 / Far), TP, the three steps, trailing. Preview shows prices and $ before you confirm.
- **Trade detail**: move SL/TP (in pips from entry, with the resulting price shown), edit steps that haven't fired, retry a failed step, partial close (25/50/75% or any volume), close all, re-enter after a close.
- **History**, **Presets** (create/edit/delete) and **Settings** (risk limits, notifications, disconnect).

Live updates come over Server-Sent Events; toasts and phone notifications fire on every engine event (step hit, SL moved, closed, error).

### Telegram signals

The bot can follow signal channels with your own Telegram account, read each message with Claude, and turn it into a trade under your own rules.

1. In `.env` set `ANTHROPIC_API_KEY`, and `TG_API_ID` / `TG_API_HASH` from https://my.telegram.org ("API development tools").
2. Run `py -m scripts.telegram_login` once on the VPS: it asks for your phone number and the code Telegram sends, saves `telegram.session`, and prints the channels you're in.
3. Restart the server. In the app open **Signals → Channels**, pick channels and give each a **trust weight** (1–5) and a **mode**.

How a signal becomes a trade:

- The AI extracts side, entry, SL and TPs. Non-gold messages, chat and results are ignored (shown greyed out in the Signals list with the reason).
- **Weight** sets the lot: lot = weight × 0.01 (configurable), so weight 1 trades 0.01 and weight 5 trades 0.05. **Mode** decides what happens: `auto` places the trade immediately, `confirm` waits in **Signals → Waiting for you** with a Place button (you can change the lot before placing), `off` pauses the channel.
- The SL comes from the signal (Far mode). P1–P3 come from the signal's TP1–TP3 when it gives three (toggle), otherwise from the channel's preset. Close percentages, SL moves and trailing always come from the preset: our rules, their levels.
- Follow-ups from the same channel ("close half", "SL to breakeven", "close now", new TP) are matched to the last trade placed from that channel and applied the same way (auto or confirm).
- Each channel has a **scorecard** (signals, placed, win rate, pips, $) so you can raise or lower its weight as it proves itself. After 10 closed trades it suggests a direction.
- **Try a message** on the Signals screen parses any text without trading, to check how a channel's style is read.

### Reaching it from outside the VPS

Port 8080 is plain HTTP. Don't expose it to the internet directly. Pick one:

- **Tailscale** (simplest): install on the VPS and the phone, then open `http://<tailscale-ip>:8080`. Private network, no ports opened.
- **Cloudflare Tunnel**: `cloudflared tunnel --url http://localhost:8080` gives an HTTPS URL on your domain. HTTPS also enables the service worker and notifications in Chrome.

The token is the only lock on the door, so keep it long and private. Use a demo account until you trust it.

## API (bearer token)

| Method | Path | What |
|---|---|---|
| GET | /api/status | quote, account, open trades (live P&L), limits, engine health |
| POST | /api/trades/preview | prices, $ amounts, validation errors for a planned trade |
| POST | /api/trades | open `{side, settings}` |
| PATCH | /api/trades/{ticket} | `sl_pips`, `tp_pips`, `clear_tp`, `steps: {idx: {pips, close_pct, move_sl_pips, clear_move_sl}}` |
| POST | /api/trades/{ticket}/close | `{volume}` for partial, empty for all |
| POST | /api/trades/{ticket}/retry/{idx} · /reenter | retry a failed step · re-open a closed trade |
| GET | /api/history · /api/events · /api/events/stream | closed trades · alert feed · SSE |
| GET/POST/PUT/DELETE | /api/presets | presets |
| GET/PUT | /api/limits | max lot, daily loss, max spread |

## Defaults (change in `engine/engine.py` or `.env`)

| Setting | Default |
|---|---|
| Breakeven buffer | +1 pip |
| Backup SL in Far mode with no SL | 300 pips |
| Retries per step | 3, with a 2 s / 4 s backoff |
| Max spread for new trades | 10 pips |
| Max lot / daily loss | `GT_MAX_LOT` (0.04) / $50, editable in Settings; no cap on the number of open trades |
