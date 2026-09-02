# QuantSphere — Resume / Runbook

Read this first in any new session before touching the project. It's the
"what is actually true right now" snapshot — where the app runs, how to get
it running again if something died, where secrets live, and what's done vs.
still open. Last verified accurate: 2026-09-02.

## 1. What this is

QuantSphere is an AI-powered trading journal: FastAPI (Python) backend +
PostgreSQL, vanilla JS/HTML/CSS frontend (no framework/bundler), a local
Ollama LLM for AI feedback (never auto-triggered — every AI call is a
user-clicked "Why?"/"Analyze" button). Deployed natively on this Windows
machine (no Docker) as three always-on Windows services, with a Cloudflare
Tunnel giving it a public URL.

- Repo root: `C:\Users\Dyudhi T G\quantsphere`
- GitHub: https://github.com/dyuthigowtham/Quantsphere (branch `main`)
- This is a **separate git repo from the home-directory one** — never run
  git commands for this project from `C:\Users\Dyudhi T G` itself.

## 2. Quick resume checklist

Run this whenever picking the project back up after any gap:

```powershell
Get-Service QuantSphere, OllamaServe, postgresql-x64-16 | Select Name, Status
Invoke-RestMethod http://127.0.0.1:8000/health
```

All three should say `Running` and health should return `{"status":"ok"}`.
These three are set to auto-start, so they normally survive a reboot on
their own. If one isn't running: `Restart-Service <name> -Force` (must be
run from an **elevated/Administrator** PowerShell — Claude's own shell on
this machine is not elevated and cannot control services directly).

Then check the **public tunnel** separately — this is the one piece that
does NOT currently auto-restart (see §5):

```
tasklist | findstr cloudflared
```

If nothing shows up, the public URL is dead even though the app itself is
fine. Restart it (see §5) to get a new one.

## 3. Architecture as actually deployed

| Piece | How it runs | Notes |
|---|---|---|
| QuantSphere app | Windows service `QuantSphere` (NSSM) | `uvicorn app.main:app` on `127.0.0.1:8000` only — never exposed directly |
| Ollama | Windows service `OllamaServe` (NSSM) | Models: `llama3` (text), `llava` (vision, NOT `llama3.2-vision` — that one fails to load on this Ollama build) |
| PostgreSQL | Windows service `postgresql-x64-16` | **Runs on port 5433, not 5432** — port 5432 is occupied by an unrelated, pre-existing PostgreSQL 18 install on this machine that must never be touched or connected to |
| Public access | Ad-hoc `cloudflared` process, NOT a service | Ephemeral — see §5 |

Install/recovery scripts live in `deploy\windows\`:
- `setup.ps1` — the original install script; safe to re-run, every step is idempotent
- `reset-postgres-password.ps1` — only needed if the Postgres superuser password is ever lost again
- `README.md` — full original setup walkthrough, including the Cloudflare Tunnel section

## 4. Where secrets/config actually live

Never re-derive or ask the user to regenerate these — they already exist:

- `.env` at repo root — production config (`DATABASE_URL` on port 5433, `JWT_SECRET`, `OLLAMA_*`, `SMTP_*` if configured). Gitignored, never committed.
- `C:\ProgramData\QuantSphere\postgres_superuser_password.txt` — the `postgres` role's password on the v16 instance
- `C:\ProgramData\QuantSphere\postgres_port.txt` — records `5433`
- Logs: `deploy\windows\logs\quantsphere.out.log` / `.err.log`, `ollama.out.log` / `.err.log`

**Email is not yet enabled.** `SMTP_*` settings in `.env` are unset, so
welcome/password-reset emails currently just get logged instead of sent
(this is by design — signup/login/reset all still work without it). To
enable: add `SMTP_HOST`/`SMTP_USERNAME`/`SMTP_PASSWORD` etc. to `.env` (see
commented example in `.env.example`) and `Restart-Service QuantSphere -Force`.
Easiest source: a Gmail account + an App Password from
myaccount.google.com/apppasswords.

## 5. The Cloudflare Tunnel — the one fragile piece

The public URL is a free "quick tunnel" (`cloudflared tunnel --url
http://localhost:8000`), started as a plain background process, **not**
registered as a Windows service. This means:
- It does NOT survive a reboot or crash.
- Every time it's restarted, it gets a **brand new random**
  `*.trycloudflare.com` address — the old one stops working.
- This has already happened twice (2026-08-27, 2026-09-02) between
  sessions and needed manual restarting.

**Current URL** (as of 2026-09-02): `https://flexible-towards-furnished-wild.trycloudflare.com`
— verify it's still alive before trusting it; if not, get a fresh one:

```powershell
cloudflared tunnel --url http://localhost:8000
```
(watch its own output for the new `https://....trycloudflare.com` line)

After getting a new URL, update `mobile/capacitor.config.json`'s
`server.url` to match and commit/push — the mobile app build depends on it.

**The real fix, not yet done**: register it as a persistent NSSM service so
it survives reboots like the other three:
```powershell
nssm install CloudflaredTunnel "C:\ProgramData\chocolatey\bin\cloudflared.exe" "tunnel --url http://localhost:8000"
nssm set CloudflaredTunnel Start SERVICE_AUTO_START
nssm start CloudflaredTunnel
```
Note this still assigns a new random URL once, but after that it should stay
stable across reboots (barring cloudflared itself restarting). The
permanent real fix is a paid/free Cloudflare account + owned domain +named
tunnel (see `deploy\windows\README.md`'s original tunnel section) — not
done, requires the user to create the account/domain.

## 6. What's actually done (don't redo)

All 4 phases (20 features) of the original AI-trading-OS spec: Trading DNA,
AI Coach, Mistake Detector, Trading Health, Setup Performance, Trade
Similarity, Market Regime, Risk Management, Strategy Lab, Decision
Training, Weekly Review, WHY Explainability, Trader Progression, Anonymous
Benchmarking, News Impact, Smart Alerts, Mobile Cockpit — plus:

- Real JWT auth + per-user ownership enforcement on every route (404, not 403, on cross-user access)
- MT5 desktop bridge (`bridge/mt5_bridge.py` + `bridge/README.md`) — the supported way to sync real MT5 trades, since the `MetaTrader5` Python package can only talk to ONE local terminal and can't serve multiple hosted users directly
- Capacitor mobile wrapper (`mobile/`) in remote-URL mode — Android scaffold verified via `npx cap doctor`; iOS scaffolded but can't be built from this Windows machine (needs a Mac)
- Native Windows deployment (this whole doc)
- Welcome email + forgot/reset password flow (§4 — code is done, SMTP delivery is not yet turned on)
- Trade-monitoring chart: opening a trade no longer closes the chart modal — it switches to a live "Open Position" view with running P&L until you close the trade
- Nav restructure: Markets + Trades merged into one tab; News split into its own tab

Test suite: 176 tests passing — `.venv\Scripts\python.exe -m pytest tests/ -q`

## 7. Open / paused threads

- **MT5 "Web API" integration — paused, unresolved.** The user proposed
  integrating via a `@centroid/mt5-webapi-client` npm package that does
  **not actually exist** on npm (verified, not found). They then pasted
  local MCP server config (`.mcp.json`/`.codex/config.toml`) for two
  servers named "metaeditor" and "terminal" on `127.0.0.1:22345`/`:22346`
  with bearer tokens — origin and purpose unclear (what installed them?
  why point Claude at a "terminal" MCP server specifically?). **Nothing was
  configured or connected.** Waiting on the user to explain what tool
  created those MCP servers before proceeding — a "terminal" MCP server is
  high-privilege and shouldn't be wired up blind. If resuming this thread,
  re-ask that question rather than assuming an answer was given.
- **Real domain for the tunnel** — not set up; user would need to buy a
  domain and add it to a (free) Cloudflare account, both decisions/actions
  only they can make.
- **Android build** — needs Android Studio/SDK installed to actually
  produce an installable `.apk`; not installed yet.
- **Known latent bug, not fixed**: `execution.close_trade`'s profit formula
  combined with the `profit` column's 2-decimal precision rounds small
  forex price moves at `volume=1.0` down to exactly `$0.00` — affects the
  Mistake Detector's revenge-trading check and the matching Smart Alert.
  Would need `volume` to be treated as lot-standardized (multiplied by a
  contract-size constant) rather than a raw unit count to fix properly.

## 8. Common commands

```powershell
# Restart the app after a code change (elevated PowerShell required)
cd "C:\Users\Dyudhi T G\quantsphere"
git pull
.\.venv\Scripts\python.exe -m pip install . --quiet
Restart-Service QuantSphere -Force

# Check logs
Get-Content deploy\windows\logs\quantsphere.err.log -Tail 40

# Run tests
.\.venv\Scripts\python.exe -m pytest tests\ -q

# Postgres backup
& "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe" -U quantsphere -h localhost -p 5433 quantsphere > backup.sql
```
