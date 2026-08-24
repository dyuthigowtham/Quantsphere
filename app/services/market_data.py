import asyncio
import logging

import httpx
from sqlalchemy import select

logger = logging.getLogger("quantsphere.market_data")

_YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
_FOREX_SUFFIX = "=X"

# Symbols that aren't tradeable-by-ticker the way a stock or FX pair is, so
# they need an explicit alias rather than the generic heuristics below.
# - NSE/BSE are commonly used as shorthand for their flagship index.
# - Spot gold has no "XAUUSD=X" on Yahoo (confirmed 404); the tradeable proxy
#   there is COMEX gold futures ("GC=F").
_SPECIAL_YAHOO_SYMBOLS = {
    "NIFTY": "^NSEI",
    "NIFTY50": "^NSEI",
    "NSE": "^NSEI",
    "SENSEX": "^BSESN",
    "BSE": "^BSESN",
    "XAUUSD": "GC=F",
    "XAU": "GC=F",
    "GOLD": "GC=F",
}


_CRYPTO_BASES = {"BTC", "ETH", "SOL", "XRP", "DOGE", "LTC", "ADA"}


def _map_clean_symbol(upper: str) -> str | None:
    """The mapping rules for an already-clean 6-letter alphabetic symbol, or None if it doesn't match."""
    if upper in _SPECIAL_YAHOO_SYMBOLS:
        return _SPECIAL_YAHOO_SYMBOLS[upper]
    if len(upper) != 6 or not upper.isalpha():
        return None
    base = upper[:3]
    if upper.endswith("USD") and base in _CRYPTO_BASES:
        return f"{base}-USD"
    return f"{upper}{_FOREX_SUFFIX}"


def to_yahoo_symbol(symbol: str) -> str:
    """
    Purpose:    Map a plain trade-journal ticker to the symbol format Yahoo
                Finance's public chart API expects.
    Args:       symbol (str): Journal ticker, e.g. "EURUSD", "BTCUSD", "AAPL",
                    "NIFTY", or an MT5-synced broker symbol with a suffix like
                    "EURUSD.a" or "XAUUSDm".
    Returns:    str: Yahoo-compatible symbol, e.g. "EURUSD=X", "BTC-USD", "AAPL", "^NSEI".
    Raises:     None.
    """
    upper = symbol.upper()
    mapped = _map_clean_symbol(upper)
    if mapped:
        return mapped

    # MT5-synced trades carry the broker's own symbol, which often tacks on
    # a suffix ("EURUSD.a", "XAUUSDm", "GBPJPY_i") that breaks the clean
    # patterns above — fall back to treating a leading 6-letter alphabetic
    # run as the underlying pair.
    core = "".join(ch for ch in upper if ch.isalpha())[:6]
    if len(core) == 6:
        mapped = _map_clean_symbol(core)
        if mapped:
            return mapped

    return upper


class PriceCache:
    """
    Purpose:    In-memory, lock-protected store of the latest known price per
                symbol. Exists so live ticks (once a broker WebSocket feed —
                Alpaca/Polygon.io — is wired in) never hit PostgreSQL on
                every tick; only trade opens/closes touch the database.
                Injected via app.dependencies rather than used as a module
                global, per the no-global-mutable-state constraint.
    """

    _HISTORY_LENGTH = 120

    def __init__(self) -> None:
        """
        Purpose:    Initialize an empty price cache.
        Args:       None.
        Returns:    None.
        Raises:     None.
        """
        self._prices: dict[str, float] = {}
        self._history: dict[str, list[float]] = {}
        self._lock = asyncio.Lock()

    async def set_price(self, symbol: str, price: float) -> None:
        """
        Purpose:    Record the latest tick for a symbol and append it to that
                    symbol's rolling history (used for sparkline charts).
        Args:       symbol (str): Instrument ticker, e.g. "EURUSD".
                    price (float): Latest traded/quoted price.
        Returns:    None.
        Raises:     None.
        """
        async with self._lock:
            self._prices[symbol] = price
            history = self._history.setdefault(symbol, [])
            history.append(price)
            if len(history) > self._HISTORY_LENGTH:
                del history[: len(history) - self._HISTORY_LENGTH]

    async def get_price(self, symbol: str) -> float | None:
        """
        Purpose:    Read the latest known price for a symbol.
        Args:       symbol (str): Instrument ticker, e.g. "EURUSD".
        Returns:    float | None: Latest price, or None if no tick has arrived yet.
        Raises:     None.
        """
        async with self._lock:
            return self._prices.get(symbol)

    async def snapshot(self) -> dict[str, dict]:
        """
        Purpose:    Read a consistent copy of every cached price plus its
                    recent history, for broadcasting over /ws/prices.
        Args:       None.
        Returns:    dict[str, dict]: Symbol -> {"price": float, "history": list[float]}.
        Raises:     None.
        """
        async with self._lock:
            return {
                symbol: {"price": price, "history": list(self._history.get(symbol, []))}
                for symbol, price in self._prices.items()
            }


async def fetch_yahoo_price(client: httpx.AsyncClient, symbol: str) -> float | None:
    """
    Purpose:    Fetch the latest quoted price for one symbol from Yahoo
                Finance's public (no API key required) chart endpoint.
    Args:       client (httpx.AsyncClient): Shared async HTTP client.
                symbol (str): Journal ticker, e.g. "EURUSD".
    Returns:    float | None: Latest regular-market price, or None on failure.
    Raises:     None. Network/parse errors are logged and swallowed so one bad
                symbol doesn't stop the rest of the watchlist from updating.
    """
    yahoo_symbol = to_yahoo_symbol(symbol)
    try:
        response = await client.get(
            _YAHOO_CHART_URL.format(symbol=yahoo_symbol),
            params={"interval": "1m", "range": "1d"},
            headers={"User-Agent": "Mozilla/5.0"},
        )
        response.raise_for_status()
        data = response.json()
        return data["chart"]["result"][0]["meta"]["regularMarketPrice"]
    except Exception:
        logger.warning("Failed to fetch price for %s (%s)", symbol, yahoo_symbol, exc_info=True)
        return None


async def fetch_yahoo_candles(symbol: str, interval: str = "15m", range_: str = "1d") -> list[dict]:
    """
    Purpose:    Fetch real OHLC candle data for a symbol from Yahoo Finance's
                free public chart endpoint, for rendering an actual
                candlestick chart (not just a single latest-price line).
    Args:       symbol (str): Journal ticker, e.g. "EURUSD".
                interval (str): Candle bucket size, e.g. "15m", "1h", "1d".
                range_ (str): How far back to fetch, e.g. "1d", "5d", "1mo".
    Returns:    list[dict]: Chronological candles, each
                    {"time": unix_seconds, "open": float, "high": float,
                     "low": float, "close": float}. Empty list on failure.
    Raises:     None. Errors are logged and result in an empty list.
    """
    yahoo_symbol = to_yahoo_symbol(symbol)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                _YAHOO_CHART_URL.format(symbol=yahoo_symbol),
                params={"interval": interval, "range": range_},
                headers={"User-Agent": "Mozilla/5.0"},
            )
        response.raise_for_status()
        result = response.json()["chart"]["result"][0]
        timestamps = result["timestamp"]
        quote = result["indicators"]["quote"][0]

        candles = []
        for i, ts in enumerate(timestamps):
            o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
            if None in (o, h, l, c):
                continue
            candles.append({"time": ts, "open": o, "high": h, "low": l, "close": c})
        return candles
    except Exception:
        logger.warning("Failed to fetch candles for %s (%s)", symbol, yahoo_symbol, exc_info=True)
        return []


async def _current_watchlist(default_symbols: list[str]) -> set[str]:
    """
    Purpose:    Build the set of symbols to poll: every symbol with an
                currently-open trade, plus a small default watchlist so the
                dashboard always shows something even with no open trades.
    Args:       default_symbols (list[str]): Always-included fallback symbols.
    Returns:    set[str]: Symbols to fetch prices for this cycle.
    Raises:     None.
    """
    from app.models.database import AsyncSessionLocal, TradeLedger, TradeStatus

    watchlist = set(default_symbols)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(TradeLedger.symbol).where(TradeLedger.status == TradeStatus.OPEN))
        watchlist.update(row[0] for row in result.all())
    return watchlist


async def run_market_data_loop(
    stop_event: asyncio.Event,
    price_cache: PriceCache,
    default_symbols: list[str],
    poll_interval_seconds: float,
) -> None:
    """
    Purpose:    Long-running background task, started from the FastAPI
                lifespan, that periodically polls free/no-key Yahoo Finance
                quotes for the current watchlist and writes them into the
                shared, dependency-injected PriceCache for the dashboard's
                live price panel and /ws/prices stream.
    Args:       stop_event (asyncio.Event): Signaled on app shutdown to end the loop cleanly.
                price_cache (PriceCache): Shared cache to write ticks into.
                default_symbols (list[str]): Always-polled fallback symbols.
                poll_interval_seconds (float): Delay between polling cycles.
    Returns:    None.
    Raises:     None. Errors are logged; the loop keeps retrying on its interval.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        while not stop_event.is_set():
            try:
                symbols = await _current_watchlist(default_symbols)
                prices = await asyncio.gather(*(fetch_yahoo_price(client, s) for s in symbols))
                for symbol, price in zip(symbols, prices):
                    if price is not None:
                        await price_cache.set_price(symbol, price)
            except Exception:
                logger.exception("Unexpected error in market data loop")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            except asyncio.TimeoutError:
                pass
