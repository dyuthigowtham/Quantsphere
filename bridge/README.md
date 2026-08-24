# QuantSphere MT5 Bridge

QuantSphere's hosted, multi-user server can't connect to your MetaTrader 5
terminal directly — MT5's API only talks to a terminal installed on the
*same machine*, and only one login can be active at a time. This bridge is
a small script you run on your own Windows PC, next to your own MT5
terminal, that reads your closed trade history and pushes it up to your
QuantSphere account.

## Requirements

- Windows (the `MetaTrader5` Python package is Windows-only)
- Python 3.11+
- Your MT5 terminal installed and able to log in to your broker account
- Your QuantSphere account email/password and portfolio id (find your
  portfolio id in the QuantSphere web app — it's shown on the Trades page)

## Setup

```
pip install MetaTrader5 httpx
```

Set the following environment variables (in PowerShell, `$env:NAME = "value"`
for the current session, or add them to your Windows user environment
variables to persist across restarts):

| Variable | Description |
|---|---|
| `QS_SERVER_URL` | Your QuantSphere server, e.g. `https://your-domain.example` |
| `QS_EMAIL` | Your QuantSphere account email |
| `QS_PASSWORD` | Your QuantSphere account password |
| `QS_PORTFOLIO_ID` | Your portfolio's numeric id |
| `MT5_LOGIN` | Your MT5 account number |
| `MT5_PASSWORD` | Your MT5 account password |
| `MT5_SERVER` | Your broker's MT5 server name |
| `MT5_TERMINAL_PATH` | *(optional)* Path to `terminal64.exe` if MT5 isn't auto-detected |
| `POLL_INTERVAL_SECONDS` | *(optional, default 60)* How often to check for new closed trades |

Then run:

```
python mt5_bridge.py
```

Leave it running in the background while you trade. It checks for newly
closed trades every `POLL_INTERVAL_SECONDS` and re-syncs your full closed
trade history each time — QuantSphere automatically skips trades it has
already seen, so this is always safe to re-run or restart.

## Security note

Your MT5 and QuantSphere credentials are read from environment variables
in plain text for this first version — the same trust model as the MT5
terminal itself. Keep the machine you run this on secured, and don't
share your environment/config with anyone.

## Not included yet

- A standalone `.exe` (currently requires Python installed)
- A GUI or system tray icon
- Auto-start on Windows boot

These are reasonable follow-ups if the bridge proves useful — file a note
with the QuantSphere maintainer if you want them prioritized.
