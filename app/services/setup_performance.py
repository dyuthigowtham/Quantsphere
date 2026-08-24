from collections import defaultdict
from datetime import datetime, timezone

from app.models.database import TradeLedger, TradeStatus
from app.models.schemas import SetupPerformanceFilter, SetupPerformanceResult, SetupPerformanceRow
from app.services.trade_stats import expectancy, realized_r_multiple, session_for_hour, win_rate_pct
from app.services.trading_profile import MIN_TRADES_PER_BUCKET

MIN_TRADES_PER_ROW = MIN_TRADES_PER_BUCKET


def _matches_filters(trade: TradeLedger, filters: SetupPerformanceFilter) -> bool:
    if filters.symbol and trade.symbol.upper() != filters.symbol.upper():
        return False
    if filters.session and session_for_hour(trade.open_time.hour) != filters.session:
        return False
    if filters.direction and trade.direction != filters.direction:
        return False
    if filters.setup_id is not None and trade.setup_id != filters.setup_id:
        return False
    if filters.date_from and trade.open_time < filters.date_from:
        return False
    if filters.date_to and trade.open_time > filters.date_to:
        return False
    return True


def compute_setup_performance(
    portfolio_id: int, trades: list[TradeLedger], filters: SetupPerformanceFilter
) -> SetupPerformanceResult:
    """
    Purpose:    Filterable per-setup/strategy performance table — the Setup
                Performance Engine. Pure function, zero I/O; the caller has
                already fetched the portfolio's trades.
    Args:       portfolio_id (int): The portfolio being analyzed.
                trades (list[TradeLedger]): All of the portfolio's trades
                    (open and closed), with `setup` eager-loaded.
                filters (SetupPerformanceFilter): Optional symbol/session/
                    direction/setup/date-range filters; unset fields mean
                    "no filter".
    Returns:    SetupPerformanceResult: One row per setup (plus an "Untagged"
                    row for trades with no setup), honestly gated on trade count.
    Raises:     None.
    """
    closed = [t for t in trades if t.status == TradeStatus.CLOSED and t.profit is not None]
    matched = [t for t in closed if _matches_filters(t, filters)]

    by_setup: dict[int | None, list[TradeLedger]] = defaultdict(list)
    names: dict[int | None, str] = {None: "Untagged"}
    for trade in matched:
        by_setup[trade.setup_id].append(trade)
        if trade.setup_id is not None and trade.setup is not None:
            names[trade.setup_id] = trade.setup.name

    rows = []
    for setup_id, setup_trades in by_setup.items():
        has_sufficient_data = len(setup_trades) >= MIN_TRADES_PER_ROW
        total = sum(float(t.profit) for t in setup_trades)

        avg_realized_r = None
        profit_factor = None
        expectancy_amount = None
        if has_sufficient_data:
            r_multiples = [r for t in setup_trades if (r := realized_r_multiple(t)) is not None]
            avg_realized_r = round(sum(r_multiples) / len(r_multiples), 2) if r_multiples else None

            gross_wins = sum(float(t.profit) for t in setup_trades if float(t.profit) > 0)
            gross_losses = -sum(float(t.profit) for t in setup_trades if float(t.profit) < 0)
            profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else None

            wins = [float(t.profit) for t in setup_trades if float(t.profit) > 0]
            losses = [-float(t.profit) for t in setup_trades if float(t.profit) < 0]
            expectancy_amount, _ = expectancy(wins, losses)
            expectancy_amount = round(expectancy_amount, 2)

        rows.append(
            SetupPerformanceRow(
                setup_id=setup_id,
                setup_name=names.get(setup_id, "Unknown"),
                trade_count=len(setup_trades),
                win_rate_pct=win_rate_pct(setup_trades),
                total_profit=round(total, 2),
                avg_profit=round(total / len(setup_trades), 2),
                avg_realized_r=avg_realized_r,
                profit_factor=profit_factor,
                expectancy=expectancy_amount,
                has_sufficient_data=has_sufficient_data,
            )
        )

    rows.sort(key=lambda r: r.total_profit, reverse=True)

    note = None
    if not matched:
        note = "No closed trades match these filters."

    return SetupPerformanceResult(
        portfolio_id=portfolio_id,
        filters_applied=filters,
        trades_matched=len(matched),
        rows=rows,
        note=note,
        generated_at=datetime.now(timezone.utc),
    )
