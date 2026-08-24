import asyncio
import logging

from sqlalchemy import select

from app.models.database import AsyncSessionLocal, Portfolio, TradeLedger, TradeStatus
from app.models.schemas import AlertRead
from app.services import alerts
from app.services.market_data import PriceCache

logger = logging.getLogger("quantsphere.alert_loop")

DEFAULT_POLL_INTERVAL_SECONDS = 10.0


async def run_alert_price_loop(
    stop_event: asyncio.Event,
    price_cache: PriceCache,
    alert_ws_manager,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> None:
    """
    Purpose:    Long-running background task, started from the FastAPI
                lifespan, that polls every open trade with a real
                stop-loss/take-profit against the live PriceCache and fires
                a Tier-2 Smart Alert when price gets close to a level the
                trader actually set. Deterministic only — never calls Ollama.
    Args:       stop_event (asyncio.Event): Signaled on app shutdown to end
                    the loop cleanly.
                price_cache (PriceCache): Shared live-price cache (same one
                    run_market_data_loop writes into).
                alert_ws_manager (AlertConnectionManager): Shared per-user
                    push-channel registry.
                poll_interval_seconds (float): Delay between polling cycles.
    Returns:    None.
    Raises:     None. Errors are logged; the loop keeps retrying on its interval.
    """
    # In-memory only — a duplicate alert after a restart is an acceptable
    # cost for never risking a missed real one. Cleared when the trade
    # closes or the proximity condition itself clears.
    already_fired: set[int] = set()

    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as session:
                stmt = (
                    select(TradeLedger, Portfolio.user_id)
                    .join(Portfolio, Portfolio.id == TradeLedger.portfolio_id)
                    .where(
                        TradeLedger.status == TradeStatus.OPEN,
                        (TradeLedger.stop_loss.isnot(None)) | (TradeLedger.take_profit.isnot(None)),
                    )
                )
                rows = (await session.execute(stmt)).all()

                still_open_ids = {trade.id for trade, _ in rows}
                already_fired &= still_open_ids  # drop anything for a trade that's no longer open

                for trade, user_id in rows:
                    price = await price_cache.get_price(trade.symbol)
                    if price is None:
                        continue
                    event = alerts.check_sl_tp_proximity(trade, price)
                    if event is None:
                        already_fired.discard(trade.id)
                        continue
                    if trade.id in already_fired:
                        continue
                    already_fired.add(trade.id)
                    alert = await alerts.persist_alert(trade.portfolio_id, event)
                    await alert_ws_manager.send_to_user(user_id, AlertRead.model_validate(alert).model_dump(mode="json"))
        except Exception:
            logger.exception("Alert price loop iteration failed")

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
        except asyncio.TimeoutError:
            pass
