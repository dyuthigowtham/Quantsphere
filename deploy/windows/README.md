# Deploying QuantSphere natively on Windows (no Docker, no WSL2)

This is the Docker-free path: Postgres, Ollama, and the QuantSphere app all
run directly on this machine as Windows services (via Chocolatey + NSSM),
and Cloudflare Tunnel exposes it publicly without opening any router ports
or touching the Windows Firewall.

## Before you run anything

**Resource check.** Ollama's `llama3` (8B) needs roughly 5-6 GB of free RAM
just to load, and `llama3.2-vision` needs more on top of that — both run on
CPU here (no GPU passthrough assumed). If this machine has 16 GB+ RAM free
it'll work but responses may take longer than you saw in local dev if
anything else is using memory at the same time. If it's an 8 GB machine,
expect the vision model in particular to be slow or to fail to load — you'd
want to either skip vision-based screenshot analysis in production or budget
for a bigger machine later. This was already an accepted tradeoff when you
chose "Ollama runs on the server too," but it's worth knowing concretely
before relying on it for real users.

**This machine must stay on and connected** for QuantSphere to be reachable
— there's no failover. That's the fundamental tradeoff of this path vs. a
real always-on VPS; you can move to a VPS later using the same `.env`/DB
dump without changing any application code.

## What `setup.ps1` does

Run it yourself, as Administrator, from the repo root:

```powershell
cd "C:\Users\Dyudhi T G\quantsphere"
.\deploy\windows\setup.ps1
```

It will (every step is safe to re-run):

1. Install `postgresql16`, `ollama`, `nssm`, `cloudflared` via Chocolatey.
2. Create a `quantsphere` Postgres role + database with a freshly generated password.
3. Write a real `.env` at the repo root (`ENVIRONMENT=production`, a fresh
   `JWT_SECRET`, the real `DATABASE_URL`, `MT5_ENABLED=false`) — this file is
   already covered by `.gitignore`, so it never gets committed.
4. Build/refresh `.venv` and `pip install` the project into it.
5. Register **OllamaServe** as an always-on Windows service (`ollama.exe serve`)
   so it's running before QuantSphere starts, and survives reboots with
   nobody logged in.
6. Pull the `llama3` and `llama3.2-vision` models.
7. Register **QuantSphere** as an always-on Windows service running
   `uvicorn app.main:app --host 127.0.0.1 --port 8000` — bound to localhost
   only; nothing this script does opens it to the network directly.

It does **not** touch the Windows Firewall or open any inbound ports. If
step 7's health check fails, check `deploy\windows\logs\quantsphere.err.log`.

**Why a single service, not multiple workers:** QuantSphere keeps live
WebSocket connections and caches (`PriceCache`, `AlertConnectionManager`,
`NewsCache`) in the process's own memory, not in Postgres/Redis. Running
more than one `uvicorn` worker or service instance would split users across
processes that can't see each other's state (e.g. one user's Smart Alert
WebSocket connecting to a worker that never sees the trade that should
trigger it). Don't add `--workers` to the NSSM command line.

## Exposing it publicly: Cloudflare Tunnel

This part needs your own free Cloudflare account and a domain (any
registrar) added to it — that's a signup/purchase decision that's yours to
make, not something I can do on your behalf. Once you have both:

```powershell
cloudflared tunnel login                                    # opens a browser to authorize
cloudflared tunnel create quantsphere                       # prints a Tunnel ID
cloudflared tunnel route dns quantsphere trade.yourdomain.com
```

Create `%USERPROFILE%\.cloudflared\config.yml`:

```yaml
tunnel: <the-tunnel-id-from-above>
credentials-file: C:\Users\<you>\.cloudflared\<the-tunnel-id>.json
ingress:
  - hostname: trade.yourdomain.com
    service: http://localhost:8000
  - service: http_status:404
```

Then install it as a service (cloudflared has its own installer, no NSSM needed):

```powershell
cloudflared service install
```

Verify from any other device: `https://trade.yourdomain.com/health` should
return `{"status":"ok"}` with a valid Cloudflare-issued certificate.

**Once you have the real hostname**, tell me — I'll update
`mobile/capacitor.config.json`'s `server.url` to point the native mobile
app at it (currently a placeholder).

## Enabling account emails (welcome + password reset)

Optional — signup and password reset both work without this, they just
silently skip sending the email (logged, not an error) until it's
configured. To turn it on, add SMTP credentials to `.env` (see the commented
example in `.env.example`) and restart the service:

```powershell
notepad .env    # add SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD, etc.
Restart-Service QuantSphere -Force
```

The easiest source of credentials if you don't already have SMTP access
anywhere: a Gmail account + an **App Password** (not your normal Gmail
password) from https://myaccount.google.com/apppasswords — set
`SMTP_HOST=smtp.gmail.com`, `SMTP_USERNAME=` your Gmail address, and
`SMTP_PASSWORD=` the generated App Password.

## Verifying the deployment

```powershell
Get-Service QuantSphere, OllamaServe, cloudflared     # all should be "Running"
Invoke-RestMethod http://127.0.0.1:8000/health
Get-Content deploy\windows\logs\quantsphere.out.log -Tail 40
```

Sign up a real account through the public URL and confirm login, opening a
trade, and the Trading Health "Why?" button (exercises the full
app → Postgres → Ollama path) all work end-to-end.

## Redeploying after a code change

```powershell
cd "C:\Users\Dyudhi T G\quantsphere"
git pull
.\.venv\Scripts\python.exe -m pip install . --quiet
nssm restart QuantSphere
```

## Backups

Postgres is the only stateful piece that matters (media/screenshots are the
other). At minimum, schedule:

```powershell
& "C:\Program Files\PostgreSQL\16\bin\pg_dump.exe" -U quantsphere -h localhost quantsphere > backup.sql
```

Ollama's pulled models don't need backing up — they're re-pulled from `ollama pull` on any new machine.
