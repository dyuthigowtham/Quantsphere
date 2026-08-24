from datetime import datetime, timezone

from app.ai.rules_engine import calculate_rsi
from app.models.database import TradeLedger
from app.services.market_data import fetch_yahoo_candles

_PRE_ENTRY_CANDLES = 40
_POST_EXIT_CANDLES = 10


def _pick_replay_params(open_time: datetime) -> tuple[str, str]:
    """
    Purpose:    Choose a Yahoo interval/range for a trade's replay window,
                mirroring the frontend's pickReplayParams so the AI mentor
                sees the same chart the user is actually looking at.
    Args:       open_time (datetime): The trade's open time.
    Returns:    tuple[str, str]: (interval, range) for fetch_yahoo_candles.
    Raises:     None.
    """
    reference = open_time if open_time.tzinfo else open_time.replace(tzinfo=timezone.utc)
    age_days = (datetime.now(timezone.utc) - reference).total_seconds() / 86400
    if age_days <= 5:
        return "5m", "5d"
    if age_days <= 30:
        return "1h", "1mo"
    if age_days <= 85:
        return "1d", "3mo"
    return "1d", "1y"


def _closest_index(candles: list[dict], target_seconds: float) -> int:
    """Index of the candle whose time is nearest target_seconds."""
    best_idx, best_diff = 0, float("inf")
    for i, candle in enumerate(candles):
        diff = abs(candle["time"] - target_seconds)
        if diff < best_diff:
            best_idx, best_diff = i, diff
    return best_idx


def _describe_candles(candles: list[dict], label: str) -> str:
    """One line summarizing a span of candles: range, direction, RSI if computable."""
    if not candles:
        return f"{label}: no chart data available."
    closes = [c["close"] for c in candles]
    low = min(c["low"] for c in candles)
    high = max(c["high"] for c in candles)
    direction = "up" if closes[-1] >= closes[0] else "down"
    rsi = calculate_rsi(closes) if len(closes) > 14 else None
    parts = [
        f"{label}: {len(candles)} candles, moved {direction} from {closes[0]:.5f} to {closes[-1]:.5f}",
        f"(range {low:.5f}-{high:.5f})",
    ]
    if rsi is not None:
        zone = "overbought" if rsi >= 70 else "oversold" if rsi <= 30 else "neutral"
        parts.append(f", RSI {rsi:.0f} ({zone})")
    return "".join(parts)


async def build_replay_context(trade: TradeLedger) -> str | None:
    """
    Purpose:    Assemble a plain-text summary of a trade plus the real price
                action around it (before entry, during the trade, after exit)
                to ground the replay AI mentor's teaching commentary — this
                is the context that makes it richer than the trade-detail
                mentor, which only ever sees the trade's own fields.
    Args:       trade (TradeLedger): The trade to build replay context for.
    Returns:    str | None: The context block, or None if no candle data
                    could be fetched for this symbol/window.
    Raises:     None.
    """
    interval, range_ = _pick_replay_params(trade.open_time)
    candles = await fetch_yahoo_candles(trade.symbol, interval=interval, range_=range_)
    if not candles:
        return None

    open_seconds = trade.open_time.replace(tzinfo=timezone.utc).timestamp()
    entry_idx = _closest_index(candles, open_seconds)

    exit_idx = None
    if trade.close_time:
        close_seconds = trade.close_time.replace(tzinfo=timezone.utc).timestamp()
        exit_idx = _closest_index(candles, close_seconds)

    pre_entry = candles[max(0, entry_idx - _PRE_ENTRY_CANDLES) : entry_idx + 1]
    during = candles[entry_idx : (exit_idx + 1) if exit_idx is not None else entry_idx + 1]
    post_exit = (
        candles[exit_idx : exit_idx + _POST_EXIT_CANDLES + 1] if exit_idx is not None else []
    )

    lines = [
        f"Symbol: {trade.symbol} | Direction: {trade.direction.value} | Volume: {trade.volume} lots",
        f"Entry: {trade.open_price} at {trade.open_time:%Y-%m-%d %H:%M} UTC",
    ]
    if trade.close_time:
        lines.append(f"Exit: {trade.close_price} at {trade.close_time:%Y-%m-%d %H:%M} UTC")
        lines.append(f"Profit: {trade.profit}")
    else:
        lines.append("This trade is still open (no exit yet).")
    if trade.comment:
        lines.append(f'Trader\'s note: "{trade.comment}"')

    lines.append(f"Chart interval used: {interval}, range: {range_}")
    lines.append(_describe_candles(pre_entry, "Price action leading into entry"))
    lines.append(_describe_candles(during, "Price action during the trade"))
    if post_exit:
        lines.append(_describe_candles(post_exit, "Price action just after exit"))

    return "\n".join(lines)
