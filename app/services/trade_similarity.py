import statistics

from app.models.database import TradeDirection, TradeLedger, TradeStatus
from app.models.schemas import SimilarTradeSummary, SimilarTradesResult
from app.services.trade_stats import realized_r_multiple, session_for_hour, win_rate_pct

# Below this many matched trades, a "what separated winners from losers"
# narrative would be drawn from too small a sample to say anything honest —
# the raw counts are still shown, just without an interpretive claim.
MIN_SIMILAR_TRADES_FOR_INSIGHT = 5

DIRECTION_MATCH_SCORE = 2
SETUP_MATCH_SCORE = 2
SESSION_MATCH_SCORE = 1


def _similarity_score(
    trade: TradeLedger,
    query_direction: TradeDirection,
    query_setup_id: int | None,
    query_session: str | None,
) -> int:
    score = 0
    if trade.direction == query_direction:
        score += DIRECTION_MATCH_SCORE
    if query_setup_id is not None and trade.setup_id == query_setup_id:
        score += SETUP_MATCH_SCORE
    if query_session is not None and session_for_hour(trade.open_time.hour) == query_session:
        score += SESSION_MATCH_SCORE
    return score


def _common_conditions(winners: list[TradeLedger], losers: list[TradeLedger]) -> list[str]:
    """
    Purpose:    Compare winners vs. losers within a matched set and describe
                what differed, only when both groups are large enough that
                the comparison isn't driven by a single trade.
    Args:       winners (list[TradeLedger]): Matched trades with positive profit.
                losers (list[TradeLedger]): Matched trades with non-positive profit.
    Returns:    list[str]: Zero or more short, honest observation sentences.
    Raises:     None.
    """
    conditions: list[str] = []
    if len(winners) < 2 or len(losers) < 2:
        return conditions

    winner_r = [r for t in winners if (r := realized_r_multiple(t)) is not None]
    loser_r = [r for t in losers if (r := realized_r_multiple(t)) is not None]
    if len(winner_r) >= 2 and len(loser_r) >= 2:
        avg_winner_r = statistics.mean(winner_r)
        avg_loser_r = statistics.mean(loser_r)
        if abs(avg_winner_r - avg_loser_r) > 0.3:
            conditions.append(
                f"Winners averaged {avg_winner_r:.1f}R vs. {avg_loser_r:.1f}R for losers on this setup."
            )

    winner_volume = statistics.mean(float(t.volume) for t in winners)
    loser_volume = statistics.mean(float(t.volume) for t in losers)
    if loser_volume > 0 and winner_volume / loser_volume > 1.3:
        conditions.append("Winning trades were sized larger than losing ones.")
    elif winner_volume > 0 and loser_volume / winner_volume > 1.3:
        conditions.append("Losing trades were sized larger than winning ones.")

    winner_sessions = [session_for_hour(t.open_time.hour) for t in winners]
    loser_sessions = [session_for_hour(t.open_time.hour) for t in losers]
    top_winner_session = max(set(winner_sessions), key=winner_sessions.count)
    if winner_sessions.count(top_winner_session) / len(winner_sessions) >= 0.6 and top_winner_session not in loser_sessions:
        conditions.append(f"Winners clustered in the {top_winner_session.replace('_', ' ')} session.")

    return conditions


def find_similar_trades(
    closed: list[TradeLedger],
    query_symbol: str,
    query_direction: TradeDirection,
    query_setup_id: int | None,
    query_hour_utc: int | None,
    exclude_trade_id: int | None = None,
) -> SimilarTradesResult:
    """
    Purpose:    Single shared engine behind both "Find Similar Trades" (an
                existing trade) and "What Would Past You Do?" (before taking
                a new one). Exact symbol match is required — cross-symbol
                similarity isn't meaningful for this comparison; direction/
                setup/session matches add to a similarity score used only for
                sorting, not filtering.
    Args:       closed (list[TradeLedger]): The portfolio's closed trades.
                query_symbol (str): Symbol to match (case-insensitive, exact).
                query_direction (TradeDirection): Direction to score against.
                query_setup_id (int | None): Setup to score against, if any.
                query_hour_utc (int | None): Hour-of-day to derive a session
                    from for scoring; None when there's no anchor trade.
                exclude_trade_id (int | None): A trade id to exclude from
                    matches (the anchor trade itself, for "Find Similar Trades").
    Returns:    SimilarTradesResult: Win/loss split, avg result, an honestly
                    gated "common conditions" narrative, and the matched trades.
    Raises:     None.
    """
    query_session = session_for_hour(query_hour_utc) if query_hour_utc is not None else None
    symbol_upper = query_symbol.upper()

    matched = [
        t
        for t in closed
        if t.status == TradeStatus.CLOSED
        and t.profit is not None
        and t.symbol.upper() == symbol_upper
        and t.id != exclude_trade_id
    ]

    scored = [(t, _similarity_score(t, query_direction, query_setup_id, query_session)) for t in matched]
    scored.sort(key=lambda pair: (pair[1], pair[0].open_time), reverse=True)

    winners = [t for t, _ in scored if float(t.profit) > 0]
    losers = [t for t, _ in scored if float(t.profit) <= 0]
    total_matched = len(scored)
    has_sufficient_data = total_matched >= MIN_SIMILAR_TRADES_FOR_INSIGHT

    r_multiples = [r for t, _ in scored if (r := realized_r_multiple(t)) is not None]

    note = None
    if not has_sufficient_data:
        note = f"Not enough similar trade history yet ({total_matched} found, need {MIN_SIMILAR_TRADES_FOR_INSIGHT})."

    return SimilarTradesResult(
        query_symbol=symbol_upper,
        query_direction=query_direction,
        query_setup_id=query_setup_id,
        total_matched=total_matched,
        has_sufficient_data=has_sufficient_data,
        winners=len(winners),
        losers=len(losers),
        win_rate_pct=win_rate_pct([t for t, _ in scored]) if scored else None,
        avg_profit=round(statistics.mean(float(t.profit) for t, _ in scored), 2) if scored else None,
        avg_realized_r=round(statistics.mean(r_multiples), 2) if r_multiples else None,
        common_conditions=_common_conditions(winners, losers) if has_sufficient_data else [],
        matched_trades=[
            SimilarTradeSummary(
                trade_id=t.id,
                symbol=t.symbol,
                direction=t.direction,
                open_time=t.open_time,
                status=t.status,
                profit=float(t.profit) if t.profit is not None else None,
                similarity_score=score,
            )
            for t, score in scored[:20]
        ],
        note=note,
    )
