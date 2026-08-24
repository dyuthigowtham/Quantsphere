from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.models.database import TradeLedger, TradeStatus
from app.models.schemas import WeeklyReviewResult, WeeklyTradeSummary
from app.services.trade_stats import realized_r_multiple, win_rate_pct
from app.services.trading_profile import MIN_TRADES_PER_BUCKET

WEEKLY_REVIEW_WINDOW_DAYS = 7
MIN_TRADES_FOR_WEEKLY_REVIEW = 3


def compute_weekly_review(
    portfolio_id: int, trades: list[TradeLedger], now: datetime | None = None
) -> WeeklyReviewResult:
    """
    Purpose:    Date-windowed digest of the last 7 days of real closed
                trades — computed fresh on every call, never persisted, same
                philosophy as trading_profile.compute_trading_profile.
    Args:       portfolio_id (int): The portfolio being reviewed.
                trades (list[TradeLedger]): All of the portfolio's trades
                    (open and closed), with `setup` eager-loaded.
                now (datetime | None): Reference "now" for the window;
                    defaults to the real current UTC time. Exposed as a
                    parameter for deterministic testing.
    Returns:    WeeklyReviewResult: The digest, honestly gated on trade count.
    Raises:     None.
    """
    reference_now = now if now is not None else datetime.now(timezone.utc)
    now_naive = reference_now.replace(tzinfo=None) if reference_now.tzinfo else reference_now
    window_start = now_naive - timedelta(days=WEEKLY_REVIEW_WINDOW_DAYS)

    closed_in_window = [
        t
        for t in trades
        if t.status == TradeStatus.CLOSED
        and t.profit is not None
        and t.close_time is not None
        and window_start <= t.close_time <= now_naive
    ]
    open_in_window = [
        t for t in trades if t.status == TradeStatus.OPEN and window_start <= t.open_time <= now_naive
    ]

    closed_trade_count = len(closed_in_window)
    has_sufficient_data = closed_trade_count >= MIN_TRADES_FOR_WEEKLY_REVIEW

    win_rate = None
    total_profit = None
    avg_realized_r = None
    best_trade = None
    worst_trade = None
    best_setup_name = None
    worst_setup_name = None

    if closed_trade_count > 0:
        total_profit = round(sum(float(t.profit) for t in closed_in_window), 2)

    if has_sufficient_data:
        win_rate = win_rate_pct(closed_in_window)
        r_multiples = [r for t in closed_in_window if (r := realized_r_multiple(t)) is not None]
        avg_realized_r = round(sum(r_multiples) / len(r_multiples), 2) if r_multiples else None

        best = max(closed_in_window, key=lambda t: float(t.profit))
        worst = min(closed_in_window, key=lambda t: float(t.profit))
        best_trade = WeeklyTradeSummary(
            trade_id=best.id,
            symbol=best.symbol,
            direction=best.direction,
            profit=float(best.profit),
            r_multiple=realized_r_multiple(best),
        )
        worst_trade = WeeklyTradeSummary(
            trade_id=worst.id,
            symbol=worst.symbol,
            direction=worst.direction,
            profit=float(worst.profit),
            r_multiple=realized_r_multiple(worst),
        )

        by_setup: dict[int, list[TradeLedger]] = defaultdict(list)
        names: dict[int, str] = {}
        for t in closed_in_window:
            if t.setup_id is not None:
                by_setup[t.setup_id].append(t)
                if t.setup is not None:
                    names[t.setup_id] = t.setup.name

        setup_totals = [
            (names.get(setup_id, "Unknown"), sum(float(t.profit) for t in setup_trades))
            for setup_id, setup_trades in by_setup.items()
            if len(setup_trades) >= MIN_TRADES_PER_BUCKET
        ]
        if setup_totals:
            setup_totals.sort(key=lambda pair: pair[1], reverse=True)
            best_setup_name = setup_totals[0][0]
            if len(setup_totals) > 1:
                worst_setup_name = setup_totals[-1][0]

    note = None
    if not has_sufficient_data:
        note = (
            f"Only {closed_trade_count} trade(s) closed in the last {WEEKLY_REVIEW_WINDOW_DAYS} days — "
            f"need at least {MIN_TRADES_FOR_WEEKLY_REVIEW} for a full review."
        )

    return WeeklyReviewResult(
        portfolio_id=portfolio_id,
        window_start=window_start,
        window_end=now_naive,
        closed_trade_count=closed_trade_count,
        open_trade_count=len(open_in_window),
        win_rate_pct=win_rate,
        total_profit=total_profit,
        avg_realized_r=avg_realized_r,
        best_trade=best_trade,
        worst_trade=worst_trade,
        best_setup_name=best_setup_name,
        worst_setup_name=worst_setup_name,
        has_sufficient_data=has_sufficient_data,
        min_trades_required=MIN_TRADES_FOR_WEEKLY_REVIEW,
        note=note,
        generated_at=reference_now,
    )
