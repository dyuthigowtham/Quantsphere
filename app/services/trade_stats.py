import statistics

from app.models.database import TradeDirection, TradeLedger


def win_rate_pct(trades: list[TradeLedger]) -> float:
    """
    Purpose:    Percentage of trades with a positive profit. Shared by
                trading_profile.py and setup_performance.py so both compute
                "win rate" identically.
    Args:       trades (list[TradeLedger]): Closed trades with `profit` set.
    Returns:    float: 0-100, rounded to 1 decimal. 0.0 for an empty list.
    Raises:     None.
    """
    if not trades:
        return 0.0
    wins = sum(1 for t in trades if float(t.profit) > 0)
    return round(wins / len(trades) * 100, 1)


def session_for_hour(hour: int) -> str:
    """
    Approximate, non-overlapping UTC session buckets. Real sessions overlap
    (e.g. London/New York), but non-overlapping buckets keep every trade in
    exactly one bucket for filtering/grouping purposes.
    """
    if 0 <= hour < 7:
        return "asian"
    if 7 <= hour < 12:
        return "london"
    if 12 <= hour < 21:
        return "new_york"
    return "other"


def realized_r_multiple(trade: TradeLedger) -> float | None:
    """
    Purpose:    Signed realized R-multiple for one trade — how many multiples
                of its planned risk (distance to stop-loss) the trade actually
                moved, in the trade's favor (positive) or against it (negative).
    Args:       trade (TradeLedger): A trade with stop_loss and close_price set.
    Returns:    float | None: The R-multiple, or None if the trade lacks a
                    stop-loss/close price, or the stop-loss equals the entry
                    (undefined risk).
    Raises:     None.
    """
    if trade.stop_loss is None or trade.close_price is None:
        return None
    open_price = float(trade.open_price)
    risk = abs(open_price - float(trade.stop_loss))
    if risk <= 0:
        return None
    signed_move = float(trade.close_price) - open_price
    if trade.direction == TradeDirection.SELL:
        signed_move = -signed_move
    return signed_move / risk


def expectancy(wins: list[float], losses: list[float]) -> tuple[float, float]:
    """
    Purpose:    Expected profit per trade given a set of winning and losing
                trade amounts, both in account-currency units and as an
                R-multiple-like ratio against the average loss.
    Args:       wins (list[float]): Positive profit amounts of winning trades.
                losses (list[float]): Positive loss magnitudes (already sign-flipped)
                    of losing trades.
    Returns:    tuple[float, float]: (expectancy_amount, expectancy_r). If there
                    are no trades at all, both are 0.0. expectancy_r divides by
                    avg_loss when available, else falls back to 1.0/0.0 based on
                    the sign of expectancy_amount (mirrors trading_profile's
                    original Strategy sub-score formula).
    Raises:     None.
    """
    total = len(wins) + len(losses)
    if total == 0:
        return 0.0, 0.0
    win_rate = len(wins) / total
    avg_win = statistics.mean(wins) if wins else 0.0
    avg_loss = statistics.mean(losses) if losses else 0.0
    expectancy_amount = win_rate * avg_win - (1 - win_rate) * avg_loss
    expectancy_r = expectancy_amount / avg_loss if avg_loss > 0 else (1.0 if expectancy_amount > 0 else 0.0)
    return expectancy_amount, expectancy_r
