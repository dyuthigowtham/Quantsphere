"""
QuantSphere MT5 Bridge.

Run this on your own Windows PC, next to your MetaTrader 5 terminal. It
reads your closed trade history locally (the MetaTrader5 package only
talks to a terminal installed on the SAME machine — it is not a remote
API) and pushes it up to your hosted QuantSphere account, so MT5 sync
keeps working even though the server itself can no longer connect to
MT5 directly (that flow can't serve multiple users from one process).

Setup:
    pip install MetaTrader5 httpx

Configure via environment variables (see README.md in this folder for
the full list and how to set them), then run:
    python mt5_bridge.py

The script polls in a loop until you stop it (Ctrl+C). It always
re-reads your full deal history each cycle and re-submits it — the
server safely skips trades it has already ingested, so this is not
wasteful of anything but a little bandwidth, and never needs a local
"last synced" file to stay correct.
"""

import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("quantsphere.mt5_bridge")

# --- Configuration (environment variables) ---
QS_SERVER_URL = os.environ.get("QS_SERVER_URL", "").rstrip("/")
QS_EMAIL = os.environ.get("QS_EMAIL", "")
QS_PASSWORD = os.environ.get("QS_PASSWORD", "")
QS_PORTFOLIO_ID = os.environ.get("QS_PORTFOLIO_ID", "")
MT5_LOGIN = os.environ.get("MT5_LOGIN", "")
MT5_PASSWORD = os.environ.get("MT5_PASSWORD", "")
MT5_SERVER = os.environ.get("MT5_SERVER", "")
MT5_TERMINAL_PATH = os.environ.get("MT5_TERMINAL_PATH") or None
POLL_INTERVAL_SECONDS = float(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
MAX_DEALS_PER_BATCH = 500


def _require_config() -> None:
    missing = [
        name
        for name, value in [
            ("QS_SERVER_URL", QS_SERVER_URL),
            ("QS_EMAIL", QS_EMAIL),
            ("QS_PASSWORD", QS_PASSWORD),
            ("QS_PORTFOLIO_ID", QS_PORTFOLIO_ID),
            ("MT5_LOGIN", MT5_LOGIN),
            ("MT5_PASSWORD", MT5_PASSWORD),
            ("MT5_SERVER", MT5_SERVER),
        ]
        if not value
    ]
    if missing:
        logger.error("Missing required environment variables: %s (see README.md)", ", ".join(missing))
        sys.exit(1)


def connect_mt5():
    """Connect to the local MT5 terminal. Exits the process on failure —
    there is nothing useful to retry without a human checking the terminal."""
    import MetaTrader5 as mt5

    kwargs = {"login": int(MT5_LOGIN), "password": MT5_PASSWORD, "server": MT5_SERVER}
    if MT5_TERMINAL_PATH:
        kwargs["path"] = MT5_TERMINAL_PATH
    if not mt5.initialize(**kwargs):
        logger.error("Could not connect to the local MT5 terminal: %s", mt5.last_error())
        sys.exit(1)
    logger.info("Connected to MT5 terminal (login=%s, server=%s)", MT5_LOGIN, MT5_SERVER)
    return mt5


def fetch_closed_deals(mt5) -> list[dict]:
    """
    Purpose:    Read the account's full closed-trade history and pair each
                position's opening/closing deal into one record — the same
                logic app/services/mt5_sync.py's
                MT5SyncService._fetch_new_closed_deals_blocking uses
                server-side, duplicated here since this script runs
                standalone on the user's machine.
    Returns:    list[dict]: One entry per closed trade, JSON-serializable.
    """
    from_date = datetime(2000, 1, 1)
    to_date = datetime.now() + timedelta(days=1)
    deals = mt5.history_deals_get(from_date, to_date)
    if deals is None:
        return []

    pairs: dict[int, dict] = {}
    for deal in deals:
        pair = pairs.setdefault(deal.position_id, {})
        if deal.entry == mt5.DEAL_ENTRY_IN:
            pair["open"] = deal
        elif deal.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY):
            pair["close"] = deal

    closed_trades = []
    for pair in pairs.values():
        open_deal, close_deal = pair.get("open"), pair.get("close")
        if open_deal is None or close_deal is None:
            continue
        closed_trades.append(
            {
                "mt5_ticket_id": close_deal.ticket,
                "mt5_position_id": close_deal.position_id,
                "symbol": close_deal.symbol,
                "direction": "buy" if open_deal.type == mt5.DEAL_TYPE_BUY else "sell",
                "volume": close_deal.volume,
                "open_price": open_deal.price,
                "close_price": close_deal.price,
                "open_time": datetime.fromtimestamp(open_deal.time, tz=timezone.utc).isoformat(),
                "close_time": datetime.fromtimestamp(close_deal.time, tz=timezone.utc).isoformat(),
                "profit": close_deal.profit,
                "swap": close_deal.swap,
                "commission": close_deal.commission,
                "comment": close_deal.comment or None,
            }
        )
    closed_trades.sort(key=lambda d: d["mt5_ticket_id"])
    return closed_trades


def login_to_quantsphere(client: httpx.Client) -> str:
    """Re-authenticate every cycle rather than caching a token — sidesteps
    the server's 24h token expiry with no extra complexity."""
    response = client.post(f"{QS_SERVER_URL}/api/auth/login", json={"email": QS_EMAIL, "password": QS_PASSWORD})
    response.raise_for_status()
    return response.json()["access_token"]


def submit_deals(client: httpx.Client, token: str, deals: list[dict]) -> None:
    headers = {"Authorization": f"Bearer {token}"}
    for i in range(0, len(deals), MAX_DEALS_PER_BATCH):
        batch = deals[i : i + MAX_DEALS_PER_BATCH]
        response = client.post(
            f"{QS_SERVER_URL}/api/portfolios/{QS_PORTFOLIO_ID}/mt5/ingest",
            json={"deals": batch},
            headers=headers,
        )
        response.raise_for_status()
    logger.info("Submitted %d closed trade(s) to QuantSphere", len(deals))


def main() -> None:
    _require_config()
    mt5 = connect_mt5()

    with httpx.Client(timeout=30.0) as client:
        while True:
            try:
                deals = fetch_closed_deals(mt5)
                if deals:
                    token = login_to_quantsphere(client)
                    submit_deals(client, token, deals)
                else:
                    logger.info("No closed trades found.")
            except httpx.HTTPError as exc:
                logger.error("Could not reach QuantSphere server: %s", exc)
            except Exception:
                logger.exception("Unexpected error during sync cycle")

            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Stopped.")
