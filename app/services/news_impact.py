from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from app.models.database import TradeLedger
from app.models.schemas import PriceMoveCheckResult

# Small curated aliases for the platform's known default symbols — this is
# NOT general NER. A headline that only says "Apple" without "AAPL" will
# not tag; that's a real, disclosed limitation, not silently patched with a
# bigger keyword list that would start guessing.
SYMBOL_KEYWORDS: dict[str, list[str]] = {
    "EURUSD": ["EUR/USD", "euro"],
    "GBPUSD": ["GBP/USD", "pound sterling", "sterling"],
    "USDJPY": ["USD/JPY", "yen"],
    "USDCHF": ["USD/CHF", "swiss franc"],
    "USDCAD": ["USD/CAD", "loonie"],
    "AUDUSD": ["AUD/USD", "aussie dollar"],
    "NZDUSD": ["NZD/USD", "kiwi dollar"],
    "XAUUSD": ["gold", "XAU"],
    "BTCUSD": ["bitcoin", "BTC"],
    "NIFTY": ["Nifty", "Sensex", "NSE"],
    "SENSEX": ["Sensex", "BSE"],
}


def _to_naive_utc(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def tag_symbols(title: str, candidate_symbols: list[str]) -> list[str]:
    """
    Purpose:    Match a news headline against a small curated set of
                candidate symbols — a deliberately narrow keyword match, not
                general NER. A symbol not in SYMBOL_KEYWORDS is matched only
                against its literal ticker text.
    Args:       title (str): The article's headline.
                candidate_symbols (list[str]): Symbols worth checking for
                    this request (the platform defaults plus the requesting
                    portfolio's own traded symbols).
    Returns:    list[str]: The subset of candidate_symbols mentioned in the title.
    Raises:     None.
    """
    lowered = title.lower()
    matched = []
    for symbol in candidate_symbols:
        keywords = SYMBOL_KEYWORDS.get(symbol.upper(), []) + [symbol]
        if any(keyword.lower() in lowered for keyword in keywords):
            matched.append(symbol)
    return matched


def build_position_context(published_at: datetime | None, matched_symbols: list[str], user_trades: list[TradeLedger]) -> str | None:
    """
    Purpose:    Real temporal-overlap check — did the requester have an open
                position in a matched symbol when this article published?
                Pure arithmetic on real trade timestamps; never claims the
                article caused anything, only that a position existed.
    Args:       published_at (datetime | None): The article's publish time.
                matched_symbols (list[str]): Symbols tag_symbols found in the title.
                user_trades (list[TradeLedger]): The requesting portfolio's own trades.
    Returns:    str | None: A real count-based sentence, or None if there's
                    no overlap (or no publish timestamp to check against).
    Raises:     None.
    """
    if published_at is None or not matched_symbols:
        return None
    published_naive = _to_naive_utc(published_at)

    overlapping_by_symbol: dict[str, int] = {}
    for trade in user_trades:
        if trade.symbol.upper() not in {s.upper() for s in matched_symbols}:
            continue
        if trade.open_time > published_naive:
            continue
        if trade.close_time is not None and trade.close_time < published_naive:
            continue
        overlapping_by_symbol[trade.symbol] = overlapping_by_symbol.get(trade.symbol, 0) + 1

    if not overlapping_by_symbol:
        return None
    parts = [f"{count} open {symbol} trade{'s' if count != 1 else ''}" for symbol, count in overlapping_by_symbol.items()]
    return f"You had {', '.join(parts)} when this published."


async def compute_price_move(
    symbol: str, published_at: datetime, candle_fetcher: Callable[..., Awaitable[list[dict]]]
) -> PriceMoveCheckResult | None:
    """
    Purpose:    On-demand, one-click check of whether a symbol's real price
                actually moved in the window around a headline's publish
                time — never computed proactively for every article. Never
                claims causality; the response always carries a fixed
                disclaimer to that effect.
    Args:       symbol (str): The symbol to check.
                published_at (datetime): The article's publish time.
                candle_fetcher (Callable): Injected candle-fetching function
                    (real signature: fetch_yahoo_candles), so this stays
                    unit-testable with a fake fetcher, same DI pattern as
                    risk_management.assess_risk's candle_fetcher parameter.
    Returns:    PriceMoveCheckResult | None: None (not a guess) if
                    published_at falls outside what the fetched candles can
                    reach — never estimate or interpolate past the real data.
    Raises:     None.
    """
    published_naive = _to_naive_utc(published_at)
    published_epoch = published_naive.replace(tzinfo=timezone.utc).timestamp()

    candles = await candle_fetcher(symbol, interval="1h", range_="1mo")
    if not candles:
        return None

    before = [c for c in candles if c["time"] <= published_epoch]
    after = [c for c in candles if c["time"] >= published_epoch]
    if not before or not after:
        return None

    price_before = before[-1]["close"]
    price_after = after[-1]["close"]
    if price_before == 0:
        return None

    return PriceMoveCheckResult(
        symbol=symbol,
        published_at=published_at,
        price_before=price_before,
        price_after=price_after,
        pct_change=round((price_after - price_before) / price_before * 100, 3),
        checked_window_note="Compared the last real candle before publish time to the most recent real candle available (1h candles, last month).",
    )
