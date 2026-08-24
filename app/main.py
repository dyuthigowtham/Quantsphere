import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from sqlalchemy import select

from app.api.routes import router as api_router
from app.api.websockets import AlertConnectionManager, ConnectionManager
from app.api.websockets import router as ws_router
from app.models.database import AsyncSessionLocal, Base, MT5Connection, engine
from app.services.alert_loop import run_alert_price_loop
from app.services.crypto import decrypt_password
from app.services.market_data import PriceCache, run_market_data_loop
from app.services.mt5_sync import MT5ConnectionManager
from app.services.news import NewsCache
from config.settings import settings

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("quantsphere.main")


class RevalidateStaticFiles(StaticFiles):
    """
    Purpose:    Serve the frontend with Cache-Control: no-cache instead of
                StaticFiles' default (no explicit header, which lets browsers
                apply heuristic caching and can serve a stale app.js/style.css
                after an edit until a hard refresh). no-cache still allows the
                browser to cache the file, but forces an ETag/Last-Modified
                revalidation on every load, so edits are always picked up on
                a normal refresh without giving up conditional-GET caching.
    """

    async def get_response(self, path: str, scope) -> Any:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Purpose:    Application lifespan: create tables, wire per-instance shared
                state onto app.state (no module-level globals), and start/stop
                the background MT5 sync and live market data loops.
    Args:       app (FastAPI): The application instance.
    Returns:    AsyncGenerator[None, None]: Yields control while the app runs.
    Raises:     None.
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app.state.price_cache = PriceCache()
    app.state.ws_manager = ConnectionManager()
    app.state.alert_ws_manager = AlertConnectionManager()
    app.state.mt5_manager = MT5ConnectionManager()
    app.state.news_cache = NewsCache()

    if settings.mt5_enabled:
        async with AsyncSessionLocal() as session:
            linked_accounts = (
                await session.execute(select(MT5Connection).where(MT5Connection.enabled == True))  # noqa: E712
            ).scalars().all()
        for connection in linked_accounts:
            try:
                password = decrypt_password(connection.encrypted_password)
            except ValueError:
                logger.exception(
                    "Skipping MT5 account for portfolio_id=%s: could not decrypt password", connection.portfolio_id
                )
                continue
            await app.state.mt5_manager.start(
                connection.portfolio_id, connection.login, password, connection.server, connection.terminal_path
            )

    stop_event = asyncio.Event()
    market_data_task = asyncio.create_task(
        run_market_data_loop(
            stop_event,
            app.state.price_cache,
            settings.market_data_default_symbols,
            settings.market_data_poll_interval_seconds,
        )
    )
    alert_loop_task = asyncio.create_task(
        run_alert_price_loop(stop_event, app.state.price_cache, app.state.alert_ws_manager)
    )

    yield

    stop_event.set()
    await market_data_task
    await alert_loop_task
    await app.state.mt5_manager.stop_all()
    await engine.dispose()


app = FastAPI(title="QuantSphere", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(api_router, prefix="/api")
app.include_router(ws_router)


@app.get("/health")
async def health_check() -> dict[str, str]:
    """
    Purpose:    Liveness probe.
    Args:       None.
    Returns:    dict[str, str]: {"status": "ok"}.
    Raises:     None.
    """
    return {"status": "ok"}


settings.media_root.mkdir(parents=True, exist_ok=True)
app.mount("/media", StaticFiles(directory=str(settings.media_root)), name="media")

if FRONTEND_DIR.is_dir():
    app.mount("/", RevalidateStaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
