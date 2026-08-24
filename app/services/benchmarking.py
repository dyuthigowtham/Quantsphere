from dataclasses import dataclass

from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import Portfolio, TradeDirection, TradeLedger, TradeStatus
from app.models.schemas import BenchmarkResult
from app.services.trade_stats import win_rate_pct
from app.services.trading_profile import MIN_TRADES_FOR_PROFILE

# The platform-population floor — distinct from MIN_TRADES_FOR_PROFILE (an
# individual's own eligibility bar). Below this many *other* qualifying
# traders, a percentile would be statistically meaningless and would risk
# revealing roughly where a handful of named people rank — never show one
# below this floor. Disclosed, not fitted to any particular launch size.
MIN_PEER_TRADERS_FOR_BENCHMARK = 20


@dataclass(frozen=True)
class PeerAggregate:
    """One anonymous qualifying trader's lifetime aggregate. Deliberately
    carries no user_id/portfolio_id — enforced at the type level, not just
    by convention, since this is the only object in the codebase that
    crosses a user boundary."""

    win_rate_pct: float
    avg_realized_r: float | None


async def fetch_peer_aggregates(db: AsyncSession, exclude_user_id: int) -> list[PeerAggregate]:
    """
    Purpose:    The one query in QuantSphere that spans multiple users'
                trade data. Aggregation happens entirely in SQL (GROUP BY
                user, not portfolio, so a multi-portfolio user still counts
                once) — no raw trade row for another user is ever pulled
                into application memory, so a bug here can't accidentally
                serialize someone else's trade detail into a response.
    Args:       db (AsyncSession): The active database session.
                exclude_user_id (int): The requesting user — excluded so
                    they can never be counted as their own peer.
    Returns:    list[PeerAggregate]: One row per OTHER user with at least
                    MIN_TRADES_FOR_PROFILE closed trades.
    Raises:     None.
    """
    signed_move = case(
        (TradeLedger.direction == TradeDirection.SELL, TradeLedger.open_price - TradeLedger.close_price),
        else_=TradeLedger.close_price - TradeLedger.open_price,
    )
    risk = func.abs(TradeLedger.open_price - TradeLedger.stop_loss)
    r_eligible = and_(TradeLedger.stop_loss.isnot(None), TradeLedger.close_price.isnot(None), risk > 0)

    stmt = (
        select(
            func.count(TradeLedger.id).label("closed_count"),
            (func.sum(case((TradeLedger.profit > 0, 1), else_=0)) * 100.0 / func.count(TradeLedger.id)).label(
                "win_rate_pct"
            ),
            func.avg(case((r_eligible, signed_move / risk), else_=None)).label("avg_realized_r"),
        )
        .select_from(TradeLedger)
        .join(Portfolio, Portfolio.id == TradeLedger.portfolio_id)
        .where(
            TradeLedger.status == TradeStatus.CLOSED,
            TradeLedger.profit.isnot(None),
            Portfolio.user_id != exclude_user_id,
        )
        .group_by(Portfolio.user_id)
        .having(func.count(TradeLedger.id) >= MIN_TRADES_FOR_PROFILE)
    )
    rows = (await db.execute(stmt)).all()
    return [
        PeerAggregate(win_rate_pct=round(float(r.win_rate_pct), 1), avg_realized_r=float(r.avg_realized_r) if r.avg_realized_r is not None else None)
        for r in rows
    ]


def compute_benchmark(
    portfolio_id: int,
    closed_trades: list[TradeLedger],
    own_avg_realized_r: float | None,
    peers: list[PeerAggregate],
) -> BenchmarkResult:
    """
    Purpose:    Honestly-gated percentile comparison against other real
                QuantSphere users. Pure — takes already-fetched rows and an
                already-fetched anonymous peer list, so it's unit-testable
                without a database, same as compute_weekly_review. Never
                fabricates a peer if the platform's real population is
                still too small.
    Args:       portfolio_id (int): Requester's portfolio.
                closed_trades (list[TradeLedger]): Requester's closed trades.
                own_avg_realized_r (float | None): Reused from
                    TradingProfile.risk_reward.avg_realized_rr — never
                    recomputed here, so the two features can't disagree.
                peers (list[PeerAggregate]): Anonymous qualifying peers,
                    already excluding the requester.
    Returns:    BenchmarkResult: Gated on both the requester's own eligibility
                    and the platform's real peer population.
    Raises:     None.
    """
    own_trade_count = len(closed_trades)
    own_eligible = own_trade_count >= MIN_TRADES_FOR_PROFILE
    peer_count = len(peers)
    has_sufficient_data = own_eligible and peer_count >= MIN_PEER_TRADERS_FOR_BENCHMARK

    own_win_rate = win_rate_pct(closed_trades) if own_eligible else None

    win_rate_percentile = None
    avg_r_percentile = None
    if has_sufficient_data:
        win_rate_percentile = round(sum(1 for p in peers if p.win_rate_pct <= own_win_rate) / peer_count * 100, 1)
        if own_avg_realized_r is not None:
            r_values = [p.avg_realized_r for p in peers if p.avg_realized_r is not None]
            if r_values:
                avg_r_percentile = round(sum(1 for v in r_values if v <= own_avg_realized_r) / len(r_values) * 100, 1)

    note = None
    if not own_eligible:
        note = f"Log at least {MIN_TRADES_FOR_PROFILE} closed trades to unlock benchmarking ({own_trade_count} so far)."
    elif peer_count < MIN_PEER_TRADERS_FOR_BENCHMARK:
        note = (
            f"Only {peer_count} of the {MIN_PEER_TRADERS_FOR_BENCHMARK} traders needed on the platform "
            "meet the minimum trade count yet — not enough real peers to compare against."
        )

    return BenchmarkResult(
        portfolio_id=portfolio_id,
        own_win_rate_pct=own_win_rate,
        own_avg_realized_r=own_avg_realized_r,
        win_rate_percentile=win_rate_percentile,
        avg_realized_r_percentile=avg_r_percentile,
        peer_trader_count=peer_count,
        min_peer_traders_required=MIN_PEER_TRADERS_FOR_BENCHMARK,
        min_trades_required=MIN_TRADES_FOR_PROFILE,
        has_sufficient_data=has_sufficient_data,
        note=note,
    )
