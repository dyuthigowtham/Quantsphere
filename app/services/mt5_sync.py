import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.ai.rules_engine import grade_trade
from app.models.database import (
    AsyncSessionLocal,
    MT5Connection,
    MT5ConnectionStatus,
    TradeDirection,
    TradeLedger,
    TradeSource,
    TradeStatus,
    XAIEvaluation,
    XAIPhase,
)
from config.settings import settings

logger = logging.getLogger("quantsphere.mt5_sync")

# Dedicated, single-worker pool for every blocking MetaTrader5 call. The MT5
# terminal's IPC only supports one active session at a time anyway, and
# keeping this off asyncio's default executor is what fixes the earlier
# incident where a broken/retrying connection saturated the same shared pool
# used for static file serving, making the whole app randomly slow.
_MT5_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mt5-sync")

_MAX_BACKOFF_SECONDS = 300.0
# A bad server/login can leave mt5.initialize()/login() blocking far longer
# than a UI request should wait. This bounds how long the CALLER waits for a
# result — Python can't forcibly kill the underlying blocking thread, so a
# stuck call still occupies the one MT5 executor slot until it eventually
# returns on its own, but the API/UI at least gets a prompt, clear error
# instead of hanging indefinitely.
_MT5_OPERATION_TIMEOUT_SECONDS = 45.0


class MT5ConnectionError(RuntimeError):
    """Raised when the local MetaTrader 5 terminal cannot be reached or logged into."""


class MT5SyncService:
    """
    Purpose:    Bridge a locally-running MetaTrader 5 terminal into the
                QuantSphere trade journal for one linked account. The
                `MetaTrader5` python package only talks to a terminal
                installed and logged in on THIS same machine — it is not a
                remote/cloud API — so this service must run on the trader's
                own Windows box. All calls into the package are blocking
                C-extension calls and are therefore always dispatched onto
                the dedicated MT5 thread pool to keep the FastAPI event loop
                (and the shared default executor) free.
    """

    def __init__(self, login: int, password: str, server: str, terminal_path: str | None = None) -> None:
        """
        Purpose:    Configure the service with one account's credentials.
        Args:       login (int): MT5 account number.
                    password (str): MT5 account password (investor/read-only
                        password recommended, since this integration never
                        places trades — it only reads closed deal history).
                    server (str): Broker server name, e.g. "MetaQuotes-Demo".
                    terminal_path (str | None): Explicit path to terminal64.exe;
                        falls back to settings.mt5_terminal_path, then to
                        MetaTrader5's own auto-detection if both are unset.
        Returns:    None.
        Raises:     None.
        """
        self._login = login
        self._password = password
        self._server = server
        self._terminal_path = terminal_path or settings.mt5_terminal_path
        self._connected = False

    def _connect_blocking(self) -> bool:
        """Blocking MT5 terminal handshake + login. Runs on the MT5 executor only."""
        import MetaTrader5 as mt5

        # MetaTrader5's initialize()/login() accept their own millisecond
        # timeout that bounds the underlying blocking call itself — pass one
        # explicitly rather than relying solely on the outer asyncio.wait_for
        # in _run_blocking, which can't actually interrupt a stuck C call.
        timeout_ms = int(_MT5_OPERATION_TIMEOUT_SECONDS * 1000)

        init_kwargs = {"timeout": timeout_ms}
        if self._terminal_path:
            init_kwargs["path"] = self._terminal_path
        if not mt5.initialize(**init_kwargs):
            logger.error("MT5 initialize() failed: %s", mt5.last_error())
            return False

        authorized = mt5.login(self._login, password=self._password, server=self._server, timeout=timeout_ms)
        if not authorized:
            logger.error("MT5 login() failed: %s", mt5.last_error())
            mt5.shutdown()
            return False

        return True

    def _disconnect_blocking(self) -> None:
        """Blocking MT5 terminal teardown. Runs on the MT5 executor only."""
        import MetaTrader5 as mt5

        mt5.shutdown()

    def _fetch_new_closed_deals_blocking(self, last_ticket: int) -> list[dict]:
        """
        Blocking scan of the full deal history, pairing each position's
        opening (DEAL_ENTRY_IN) and closing (DEAL_ENTRY_OUT/OUT_BY) deal so
        that a single closed trade record can be built. Runs on the MT5
        executor only.
        """
        import MetaTrader5 as mt5

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

        closed_trades: list[dict] = []
        for pair in pairs.values():
            open_deal, close_deal = pair.get("open"), pair.get("close")
            if open_deal is None or close_deal is None or close_deal.ticket <= last_ticket:
                continue
            closed_trades.append(
                {
                    "mt5_ticket_id": close_deal.ticket,
                    "mt5_position_id": close_deal.position_id,
                    "symbol": close_deal.symbol,
                    "direction": TradeDirection.BUY if open_deal.type == mt5.DEAL_TYPE_BUY else TradeDirection.SELL,
                    "volume": close_deal.volume,
                    "open_price": open_deal.price,
                    "close_price": close_deal.price,
                    # MT5 deal timestamps are Unix seconds in UTC — convert
                    # explicitly rather than datetime.fromtimestamp()'s
                    # local-wall-clock default, then drop tzinfo since the
                    # trade_ledger columns are naive (always-UTC) timestamps.
                    "open_time": datetime.fromtimestamp(open_deal.time, tz=timezone.utc).replace(tzinfo=None),
                    "close_time": datetime.fromtimestamp(close_deal.time, tz=timezone.utc).replace(tzinfo=None),
                    "profit": close_deal.profit,
                    "swap": close_deal.swap,
                    "commission": close_deal.commission,
                    "comment": close_deal.comment,
                }
            )
        closed_trades.sort(key=lambda d: d["mt5_ticket_id"])
        return closed_trades

    async def _run_blocking(self, func, *args):
        """
        Dispatch one blocking MT5 call onto the dedicated single-worker
        executor, bounded by _MT5_OPERATION_TIMEOUT_SECONDS so a bad
        server/login can't hang the caller indefinitely.
        """
        loop = asyncio.get_running_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(_MT5_EXECUTOR, func, *args), timeout=_MT5_OPERATION_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError as exc:
            raise MT5ConnectionError(
                f"MT5 terminal did not respond within {_MT5_OPERATION_TIMEOUT_SECONDS:.0f}s"
            ) from exc

    async def connect(self) -> None:
        """
        Purpose:    Open a connection to the local MT5 terminal off the event loop.
        Args:       None.
        Returns:    None.
        Raises:     MT5ConnectionError: If initialize() or login() fails.
        """
        ok = await self._run_blocking(self._connect_blocking)
        if not ok:
            raise MT5ConnectionError(
                "Could not connect to the local MT5 terminal — check that it's installed, "
                "running, and that the login/password/server are correct."
            )
        self._connected = True

    async def disconnect(self) -> None:
        """
        Purpose:    Close the connection to the local MT5 terminal off the event loop.
        Args:       None.
        Returns:    None.
        Raises:     None.
        """
        if self._connected:
            await self._run_blocking(self._disconnect_blocking)
            self._connected = False

    async def fetch_new_closed_deals(self, last_ticket: int) -> list[dict]:
        """
        Purpose:    Retrieve closed-position deals newer than `last_ticket`.
        Args:       last_ticket (int): Highest mt5_ticket_id already ingested for this account.
        Returns:    list[dict]: Newly closed trades, oldest first, each with
                    the fields needed to populate a TradeLedger row.
        Raises:     MT5ConnectionError: If not currently connected.
        """
        if not self._connected:
            raise MT5ConnectionError("fetch_new_closed_deals() called before connect()")
        return await self._run_blocking(self._fetch_new_closed_deals_blocking, last_ticket)


async def ingest_closed_trade(portfolio_id: int, deal: dict) -> None:
    """
    Purpose:    Persist one MT5-sourced closed trade and instantly run the
                Phase A rules engine against it (no LLM call — Phase B stays
                strictly on-demand per the zero-auto-trigger constraint).
                Public — called both by the local sync loop below and by the
                POST /portfolios/{id}/mt5/ingest route (app/api/routes.py),
                which receives deals from a user's own desktop MT5 bridge
                instead of a server-side terminal connection.
    Args:       portfolio_id (int): Portfolio to attribute the trade to.
                deal (dict): One entry from MT5SyncService.fetch_new_closed_deals,
                    or an equivalent dict posted by the desktop bridge.
    Returns:    None.
    Raises:     None. Errors (including a duplicate mt5_ticket_id, which is
                expected on a re-submitted deal) are logged and swallowed so
                one bad/duplicate deal doesn't stop the rest from ingesting.
    """
    async with AsyncSessionLocal() as session:
        try:
            trade = TradeLedger(
                portfolio_id=portfolio_id,
                source=TradeSource.MT5,
                mt5_ticket_id=deal["mt5_ticket_id"],
                mt5_position_id=deal["mt5_position_id"],
                symbol=deal["symbol"],
                direction=deal["direction"],
                volume=deal["volume"],
                open_price=deal["open_price"],
                close_price=deal["close_price"],
                open_time=deal["open_time"],
                close_time=deal["close_time"],
                profit=deal["profit"],
                swap=deal["swap"],
                commission=deal["commission"],
                comment=deal["comment"],
                status=TradeStatus.CLOSED,
            )
            session.add(trade)
            await session.flush()

            result = grade_trade(
                open_price=deal["open_price"],
                close_price=deal["close_price"],
                direction_is_buy=deal["direction"] == TradeDirection.BUY,
            )
            session.add(
                XAIEvaluation(
                    trade_id=trade.id,
                    phase=XAIPhase.RULES_ENGINE,
                    grade=result.grade,
                    triggered_indicators=result.triggered_indicators,
                )
            )
            await session.commit()
            logger.info("Ingested MT5 deal ticket=%s symbol=%s grade=%s", deal["mt5_ticket_id"], deal["symbol"], result.grade)
        except Exception:
            await session.rollback()
            logger.exception("Failed to ingest MT5 deal ticket=%s", deal.get("mt5_ticket_id"))


async def _last_synced_ticket(portfolio_id: int) -> int:
    """The portfolio's MT5Connection.last_synced_ticket, or 0 if unset/missing."""
    async with AsyncSessionLocal() as session:
        connection = (
            await session.execute(select(MT5Connection).where(MT5Connection.portfolio_id == portfolio_id))
        ).scalar_one_or_none()
        return connection.last_synced_ticket if connection else 0


async def _set_connection_state(
    portfolio_id: int,
    *,
    status: MT5ConnectionStatus,
    last_error: str | None = None,
    last_synced_ticket: int | None = None,
    touch_synced_at: bool = False,
) -> None:
    """
    Purpose:    Update one portfolio's MT5Connection row with the sync loop's
                latest status, so the UI can show connected/error state and
                last-synced time without polling the terminal directly.
    Args:       portfolio_id (int): Portfolio whose connection row to update.
                status (MT5ConnectionStatus): New status.
                last_error (str | None): Error detail, or None to clear it.
                last_synced_ticket (int | None): New high-water-mark ticket, if advanced.
                touch_synced_at (bool): Whether to stamp last_synced_at with now.
    Returns:    None.
    Raises:     None. Swallowed — this is best-effort bookkeeping, not core sync logic.
    """
    async with AsyncSessionLocal() as session:
        try:
            connection = (
                await session.execute(select(MT5Connection).where(MT5Connection.portfolio_id == portfolio_id))
            ).scalar_one_or_none()
            if connection is None:
                return
            connection.status = status
            connection.last_error = last_error
            if last_synced_ticket is not None:
                connection.last_synced_ticket = last_synced_ticket
            if touch_synced_at:
                connection.last_synced_at = datetime.utcnow()
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Failed to update MT5 connection state for portfolio_id=%s", portfolio_id)


async def _run_portfolio_sync_loop(
    portfolio_id: int,
    login: int,
    password: str,
    server: str,
    terminal_path: str | None,
    stop_event: asyncio.Event,
) -> None:
    """
    Purpose:    Long-running per-portfolio background loop that periodically
                polls one linked MT5 terminal for newly closed deals and
                journals them. Backs off on repeated connection failures
                instead of hammering the terminal every poll interval —
                the earlier static-config version didn't, which is what
                caused the thread-pool-saturation incident this design fixes.
    Args:       portfolio_id (int): Portfolio this account is linked to.
                login, password, server, terminal_path: MT5 credentials.
                stop_event (asyncio.Event): Signaled to end the loop cleanly
                    (on disconnect, or app shutdown).
    Returns:    None.
    Raises:     None. All errors are logged and reflected onto the
                MT5Connection row rather than propagating.
    """
    service = MT5SyncService(login, password, server, terminal_path)
    consecutive_failures = 0

    while not stop_event.is_set():
        try:
            if not service._connected:
                await service.connect()
            consecutive_failures = 0

            last_ticket = await _last_synced_ticket(portfolio_id)
            new_deals = await service.fetch_new_closed_deals(last_ticket)
            for deal in new_deals:
                await ingest_closed_trade(portfolio_id, deal)

            newest_ticket = max((d["mt5_ticket_id"] for d in new_deals), default=None)
            await _set_connection_state(
                portfolio_id,
                status=MT5ConnectionStatus.CONNECTED,
                last_synced_ticket=newest_ticket,
                touch_synced_at=True,
            )
        except MT5ConnectionError as exc:
            consecutive_failures += 1
            logger.warning("MT5 sync connection issue for portfolio_id=%s: %s", portfolio_id, exc)
            await _set_connection_state(portfolio_id, status=MT5ConnectionStatus.ERROR, last_error=str(exc))
        except Exception as exc:
            consecutive_failures += 1
            logger.exception("Unexpected error in MT5 sync loop for portfolio_id=%s", portfolio_id)
            await _set_connection_state(portfolio_id, status=MT5ConnectionStatus.ERROR, last_error=str(exc))

        delay = settings.mt5_poll_interval_seconds
        if consecutive_failures:
            delay = min(settings.mt5_poll_interval_seconds * (2**consecutive_failures), _MAX_BACKOFF_SECONDS)

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

    await service.disconnect()


class MT5ConnectionManager:
    """
    Purpose:    Tracks one background sync task per portfolio that has a
                linked MT5 account, so accounts can be connected/disconnected
                live from the UI instead of via a single static .env config.
                Stored on app.state (see app/main.py's lifespan), not as a
                module-level global.
    """

    def __init__(self) -> None:
        """
        Purpose:    Initialize with no active sync tasks.
        Args:       None.
        Returns:    None.
        Raises:     None.
        """
        self._tasks: dict[int, asyncio.Task] = {}
        self._stop_events: dict[int, asyncio.Event] = {}

    def is_running(self, portfolio_id: int) -> bool:
        """
        Purpose:    Check whether a portfolio currently has an active sync loop.
        Args:       portfolio_id (int): Portfolio to check.
        Returns:    bool: True if a sync task is running for it.
        Raises:     None.
        """
        return portfolio_id in self._tasks

    async def start(self, portfolio_id: int, login: int, password: str, server: str, terminal_path: str | None) -> None:
        """
        Purpose:    Start (or restart, if already running) the sync loop for
                    one portfolio's linked MT5 account.
        Args:       portfolio_id (int): Portfolio to sync into.
                    login, password, server, terminal_path: MT5 credentials.
        Returns:    None.
        Raises:     None.
        """
        await self.stop(portfolio_id)
        stop_event = asyncio.Event()
        self._stop_events[portfolio_id] = stop_event
        self._tasks[portfolio_id] = asyncio.create_task(
            _run_portfolio_sync_loop(portfolio_id, login, password, server, terminal_path, stop_event)
        )

    async def stop(self, portfolio_id: int) -> None:
        """
        Purpose:    Stop one portfolio's sync loop, if running, and wait for
                    it to finish disconnecting from the terminal.
        Args:       portfolio_id (int): Portfolio whose sync loop to stop.
        Returns:    None.
        Raises:     None.
        """
        stop_event = self._stop_events.pop(portfolio_id, None)
        task = self._tasks.pop(portfolio_id, None)
        if stop_event:
            stop_event.set()
        if task:
            await task

    async def stop_all(self) -> None:
        """
        Purpose:    Stop every active sync loop, e.g. on app shutdown.
        Args:       None.
        Returns:    None.
        Raises:     None.
        """
        for portfolio_id in list(self._tasks):
            await self.stop(portfolio_id)
