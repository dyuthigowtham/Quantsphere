from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import TradeLedger, XAIEvaluation, XAIPhase
from app.models.schemas import TradingProfile
from app.services import accounts, execution, trading_profile
from app.services.market_data import PriceCache

_MAX_TRADES_IN_CONTEXT = 40


def _latest_evaluation(trade: TradeLedger, phase: XAIPhase) -> XAIEvaluation | None:
    """The most recent evaluation of the given phase attached to a trade, if any."""
    matches = [e for e in trade.evaluations if e.phase == phase]
    return max(matches, key=lambda e: e.id) if matches else None


def _describe_trade(trade: TradeLedger) -> str:
    """One compact line summarizing a trade plus its grading/mentor history."""
    parts = [
        f"#{trade.id} {trade.symbol} {trade.direction.value} {trade.volume} lots",
        f"opened {trade.open_price} on {trade.open_time:%Y-%m-%d %H:%M}",
    ]
    if trade.status.value == "closed":
        parts.append(f"closed {trade.close_price} on {trade.close_time:%Y-%m-%d %H:%M}, profit={trade.profit}")
    else:
        parts.append("still OPEN")

    rules = _latest_evaluation(trade, XAIPhase.RULES_ENGINE)
    if rules:
        parts.append(f"rules-engine grade={rules.grade}")

    mentor = _latest_evaluation(trade, XAIPhase.LLM_MENTOR)
    if mentor:
        summary = (mentor.reasoning or {}).get("summary", "")
        parts.append(f'AI mentor verdict={mentor.verdict} grade={mentor.grade}: "{summary}"')

    if trade.comment:
        parts.append(f'note: "{trade.comment}"')

    return " | ".join(parts)


async def build_chat_context(db: AsyncSession, price_cache: PriceCache, portfolio_id: int) -> str:
    """
    Purpose:    Assemble a compact text summary of a portfolio's trades (with
                their rules-engine grades and any AI mentor verdicts) plus
                current live prices, to ground the chat assistant's answers
                in the user's own data instead of letting it guess.
    Args:       db (AsyncSession): The active database session.
                price_cache (PriceCache): Injected shared price cache.
                portfolio_id (int): Portfolio to summarize.
    Returns:    str: Plain-text context block for the assistant's system prompt.
    Raises:     None.
    """
    trades = (await execution.list_trades_for_portfolio(db, portfolio_id))[:_MAX_TRADES_IN_CONTEXT]
    trades_block = "\n".join(_describe_trade(t) for t in trades) if trades else "No trades logged yet."

    prices = await price_cache.snapshot()
    prices_block = (
        ", ".join(f"{symbol}={data['price']}" for symbol, data in sorted(prices.items()))
        if prices
        else "No live prices cached yet."
    )

    return f"Trades (most recent {len(trades)}):\n{trades_block}\n\nCurrent live prices: {prices_block}"


def _describe_trading_profile(profile: TradingProfile) -> str:
    """Compact plain-text rendering of a TradingProfile for the AI Coach's grounding context."""
    if not profile.has_sufficient_data:
        return f"Not enough closed trades yet for a behavioral profile ({profile.trades_analyzed} logged)."

    lines = [f"Closed trades analyzed: {profile.trades_analyzed}."]
    if profile.strongest_edge:
        lines.append(f"Strongest edge: {profile.strongest_edge}")
    if profile.biggest_weakness:
        lines.append(f"Biggest weakness: {profile.biggest_weakness}")
    if profile.best_trading_window:
        lines.append(f"Best trading window: {profile.best_trading_window}")
    if profile.worst_trading_environment:
        lines.append(f"Weakest environment: {profile.worst_trading_environment}")
    if profile.avg_holding_minutes is not None:
        lines.append(f"Average holding time: {profile.avg_holding_minutes:.0f} minutes.")

    tracked_mistakes = [m for m in profile.mistakes if m.status == "tracked" and m.occurrences > 0]
    if tracked_mistakes:
        lines.append(
            "Detected patterns: "
            + "; ".join(f"{m.category} ({m.occurrences}x, {m.severity})" for m in tracked_mistakes)
        )

    if profile.health.overall_score is not None:
        lines.append(f"Trading Health score: {profile.health.overall_score}/100.")

    lines.append(profile.timestamp_caveat)
    return "\n".join(lines)


async def build_coach_context(db: AsyncSession, price_cache: PriceCache, portfolio_id: int) -> str:
    """
    Purpose:    Ground the full-page AI Coach in the same structured
                behavioral profile that powers Trading DNA/Mistake Detector/
                Trading Health, layered on top of the existing trade-list
                context — so "why is my win rate falling" style questions are
                answered from real, derived stats instead of guessing. The
                floating chat widget keeps using build_chat_context as-is;
                this heavier context is only for the dedicated Coach view.
    Args:       db (AsyncSession): The active database session.
                price_cache (PriceCache): Injected shared price cache.
                portfolio_id (int): Portfolio to summarize.
    Returns:    str: Plain-text context block for the Coach's system prompt.
    Raises:     None.
    """
    base_context = await build_chat_context(db, price_cache, portfolio_id)
    portfolio = await accounts.get_portfolio(db, portfolio_id)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    profile = trading_profile.compute_trading_profile(portfolio_id, trades, float(portfolio.starting_balance))
    return f"{base_context}\n\n=== Derived trading profile ===\n{_describe_trading_profile(profile)}"
