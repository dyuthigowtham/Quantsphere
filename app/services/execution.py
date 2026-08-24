import statistics
from datetime import datetime

import httpx
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.ai.rules_engine import calculate_rsi, grade_trade, pretrade_check, score_trade_setup
from app.models.database import Portfolio, TradeLedger, TradeStatus, User, XAIEvaluation, XAIPhase
from app.services.accounts import get_owned_portfolio
from app.models.schemas import (
    MarketOrderRequest,
    PortfolioMetrics,
    PreTradeCheckRequest,
    PreTradeCheckResult,
    SetupCheckRequest,
    SetupQualityResult,
    TradeCloseRequest,
    TradeCreate,
    TradeJournalUpdate,
    TradeSetupUpdate,
)
from app.services.market_data import PriceCache, fetch_yahoo_candles, fetch_yahoo_price
from app.services.risk_management import assess_risk


async def list_trades_for_portfolio(db: AsyncSession, portfolio_id: int) -> list[TradeLedger]:
    """
    Purpose:    Fetch every trade in a portfolio's journal, newest first, for
                the dashboard/trade-list view.
    Args:       db (AsyncSession): The active database session.
                portfolio_id (int): Portfolio whose trades should be listed.
    Returns:    list[TradeLedger]: Trades with screenshots/evaluations eagerly loaded.
    Raises:     None.
    """
    stmt = (
        select(TradeLedger)
        .where(TradeLedger.portfolio_id == portfolio_id)
        .options(
            selectinload(TradeLedger.screenshots), selectinload(TradeLedger.evaluations), selectinload(TradeLedger.setup)
        )
        .order_by(TradeLedger.created_at.desc())
    )
    return list((await db.execute(stmt)).scalars().all())


async def open_trade(db: AsyncSession, trade_data: TradeCreate) -> TradeLedger:
    """
    Purpose:    Journal a new manually-entered open trade.
    Args:       db (AsyncSession): The active database session.
                trade_data (TradeCreate): Validated Pydantic schema of the requested trade.
    Returns:    TradeLedger: The persisted, open trade row.
    Raises:     None.
    """
    trade = TradeLedger(
        portfolio_id=trade_data.portfolio_id,
        symbol=trade_data.symbol,
        direction=trade_data.direction,
        volume=trade_data.volume,
        open_price=trade_data.open_price,
        stop_loss=trade_data.stop_loss,
        take_profit=trade_data.take_profit,
        open_time=trade_data.open_time,
        comment=trade_data.comment,
        status=TradeStatus.OPEN,
    )
    db.add(trade)
    await db.commit()
    await db.refresh(trade)
    return trade


async def open_trade_at_market(db: AsyncSession, order: MarketOrderRequest) -> TradeLedger:
    """
    Purpose:    One-click paper trading: open a trade using the current live
                market price fetched from Yahoo Finance, so the user never
                has to type a price in by hand.
    Args:       db (AsyncSession): The active database session.
                order (MarketOrderRequest): Validated portfolio/symbol/direction/volume.
    Returns:    TradeLedger: The persisted, open trade row at the fetched market price.
    Raises:     HTTPException: 502 if a live price for the symbol can't be fetched.
    """
    async with httpx.AsyncClient(timeout=10.0) as client:
        price = await fetch_yahoo_price(client, order.symbol)
    if price is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch a live price for {order.symbol}",
        )

    trade = TradeLedger(
        portfolio_id=order.portfolio_id,
        symbol=order.symbol.upper(),
        direction=order.direction,
        volume=order.volume,
        open_price=price,
        stop_loss=order.stop_loss,
        take_profit=order.take_profit,
        open_time=datetime.utcnow(),
        comment=order.comment,
        status=TradeStatus.OPEN,
    )
    db.add(trade)
    await db.commit()
    await db.refresh(trade)
    return trade


async def precheck_market_trade(
    db: AsyncSession, price_cache: PriceCache, payload: PreTradeCheckRequest
) -> PreTradeCheckResult:
    """
    Purpose:    Instant pre-trade risk/RSI readout for the paper-trading
                learning area — fetches the live price/history and portfolio
                balance, then hands off to the pure Phase A pretrade_check
                calculation. Never calls Ollama.
    Args:       db (AsyncSession): The active database session.
                price_cache (PriceCache): Injected shared price cache.
                payload (PreTradeCheckRequest): Validated portfolio/symbol/volume.
    Returns:    PreTradeCheckResult: Position-size risk level plus RSI-based
                    buy/sell guidance notes.
    Raises:     HTTPException: 404 if the portfolio doesn't exist; 502 if a
                    live price for the symbol can't be fetched.
    """
    portfolio = await db.get(Portfolio, payload.portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio not found")

    symbol = payload.symbol.upper()
    cached = (await price_cache.snapshot()).get(symbol)
    price = cached["price"] if cached else None
    history = cached["history"] if cached else []

    if price is None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            price = await fetch_yahoo_price(client, symbol)
    if price is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch a live price for {symbol}",
        )

    rsi = calculate_rsi(history) if history else None
    return pretrade_check(
        volume=payload.volume,
        price=price,
        portfolio_balance=float(portfolio.current_balance),
        rsi=rsi,
    )


async def close_trade_at_market(db: AsyncSession, trade_id: int) -> TradeLedger:
    """
    Purpose:    One-click paper trading: close an open trade using the
                current live market price for its symbol.
    Args:       db (AsyncSession): The active database session.
                trade_id (int): Identifier of the trade to close.
    Returns:    TradeLedger: The updated, closed trade with its Phase A evaluation.
    Raises:     HTTPException: 404 if not found; 409 if already closed; 502 if
                    a live price for the trade's symbol can't be fetched.
    """
    trade = await db.get(TradeLedger, trade_id)
    if trade is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade not found")
    if trade.status == TradeStatus.CLOSED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Trade is already closed")

    async with httpx.AsyncClient(timeout=10.0) as client:
        price = await fetch_yahoo_price(client, trade.symbol)
    if price is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch a live price for {trade.symbol}",
        )

    close_data = TradeCloseRequest(close_price=price, close_time=datetime.utcnow(), swap=0, commission=0)
    return await close_trade(db, trade_id, close_data)


async def close_trade(db: AsyncSession, trade_id: int, close_data: TradeCloseRequest) -> TradeLedger:
    """
    Purpose:    Close an open trade and instantly run the Phase A local
                rules engine to attach a zero-latency grade — the Phase B
                LLM mentor is intentionally NOT invoked here; it is only
                ever triggered by an explicit "Analyze with AI" request.
    Args:       db (AsyncSession): The active database session.
                trade_id (int): Identifier of the trade to close.
                close_data (TradeCloseRequest): Validated close price/time/costs.
    Returns:    TradeLedger: The updated, closed trade row with its Phase A evaluation attached.
    Raises:     HTTPException: 404 if the trade doesn't exist; 409 if it is already closed.
    """
    trade = await db.get(TradeLedger, trade_id)
    if trade is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade not found")
    if trade.status == TradeStatus.CLOSED:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Trade is already closed")

    trade.close_price = close_data.close_price
    trade.close_time = close_data.close_time
    trade.swap = close_data.swap
    trade.commission = close_data.commission
    trade.status = TradeStatus.CLOSED

    open_price = float(trade.open_price)
    result = grade_trade(
        open_price=open_price,
        close_price=close_data.close_price,
        direction_is_buy=trade.direction.value == "buy",
    )
    trade.profit = result.profit_pct / 100 * open_price * float(trade.volume)

    portfolio = await db.get(Portfolio, trade.portfolio_id)
    portfolio.current_balance = float(portfolio.current_balance) + trade.profit

    db.add(
        XAIEvaluation(
            trade_id=trade.id,
            phase=XAIPhase.RULES_ENGINE,
            grade=result.grade,
            triggered_indicators=result.triggered_indicators,
        )
    )
    await db.commit()
    await db.refresh(trade)
    return trade


def calculate_portfolio_metrics(trades: list[TradeLedger], starting_balance: float) -> PortfolioMetrics:
    """
    Purpose:    Compute advanced risk metrics (Sharpe ratio, max drawdown) from
                a portfolio's closed trade history. Pure, synchronous math —
                no I/O — mirroring the Phase A rules engine's approach.
    Args:       trades (list[TradeLedger]): All trades in the portfolio (open
                    and closed; only closed ones with a profit are used).
                starting_balance (float): Portfolio's starting balance, used
                    to express max drawdown as a percentage.
    Returns:    PortfolioMetrics: Trade count, win rate, Sharpe ratio (None if
                    fewer than 2 closed trades or zero variance), and max drawdown.
    Raises:     None.
    """
    closed = [t for t in trades if t.status == TradeStatus.CLOSED and t.profit is not None]
    closed.sort(key=lambda t: t.close_time)
    returns = [float(t.profit) for t in closed]

    if not returns:
        return PortfolioMetrics(
            trade_count=0, win_rate_pct=None, sharpe_ratio=None, max_drawdown_amount=0.0, max_drawdown_pct=None
        )

    wins = sum(1 for r in returns if r > 0)
    win_rate_pct = (wins / len(returns)) * 100

    sharpe_ratio = None
    if len(returns) >= 2:
        stdev_r = statistics.pstdev(returns)
        if stdev_r > 0:
            sharpe_ratio = statistics.mean(returns) / stdev_r

    running_total = 0.0
    peak = float("-inf")
    max_drawdown_amount = 0.0
    for r in returns:
        running_total += r
        peak = max(peak, running_total)
        max_drawdown_amount = max(max_drawdown_amount, peak - running_total)

    max_drawdown_pct = (max_drawdown_amount / starting_balance) * 100 if starting_balance > 0 else None

    return PortfolioMetrics(
        trade_count=len(returns),
        win_rate_pct=round(win_rate_pct, 2),
        sharpe_ratio=round(sharpe_ratio, 4) if sharpe_ratio is not None else None,
        max_drawdown_amount=round(max_drawdown_amount, 2),
        max_drawdown_pct=round(max_drawdown_pct, 2) if max_drawdown_pct is not None else None,
    )


async def get_trade_with_relations(db: AsyncSession, trade_id: int) -> TradeLedger:
    """
    Purpose:    Fetch a trade along with its screenshots and XAI evaluations for display.
    Args:       db (AsyncSession): The active database session.
                trade_id (int): Identifier of the trade to fetch.
    Returns:    TradeLedger: The trade row with `screenshots` and `evaluations` eagerly loaded.
    Raises:     HTTPException: 404 if the trade doesn't exist.
    """
    stmt = (
        select(TradeLedger)
        .where(TradeLedger.id == trade_id)
        .options(
            selectinload(TradeLedger.screenshots), selectinload(TradeLedger.evaluations), selectinload(TradeLedger.setup)
        )
    )
    trade = (await db.execute(stmt)).scalar_one_or_none()
    if trade is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade not found")
    return trade


async def get_owned_trade(db: AsyncSession, trade_id: int, user: User) -> TradeLedger:
    """
    Purpose:    Fetch a trade with relations, enforcing that its portfolio
                belongs to the authenticated user — the ownership check
                every trade-scoped route must use.
    Args:       db (AsyncSession): The active database session.
                trade_id (int): Identifier of the trade to fetch.
                user (User): The authenticated requester.
    Returns:    TradeLedger: The trade row with relations loaded.
    Raises:     HTTPException: 404 if the trade doesn't exist OR its
                    portfolio belongs to a different user.
    """
    trade = await get_trade_with_relations(db, trade_id)
    await get_owned_portfolio(db, trade.portfolio_id, user)
    return trade


async def update_trade_journal(db: AsyncSession, trade_id: int, journal: TradeJournalUpdate) -> TradeLedger:
    """
    Purpose:    Save a trade's Backtest & Replay reflection journal — the
                user's own notes/takeaways from reviewing that trade's chart,
                separate from and never touched by the AI mentor.
    Args:       db (AsyncSession): The active database session.
                trade_id (int): Trade whose journal to update.
                journal (TradeJournalUpdate): The prompt answers to save.
    Returns:    TradeLedger: The updated trade with relations loaded.
    Raises:     HTTPException: 404 if the trade doesn't exist.
    """
    trade = await get_trade_with_relations(db, trade_id)
    trade.backtest_journal = journal.model_dump()
    await db.commit()
    await db.refresh(trade)
    return await get_trade_with_relations(db, trade_id)


async def update_trade_setup(db: AsyncSession, trade_id: int, payload: TradeSetupUpdate) -> TradeLedger:
    """
    Purpose:    Tag (or clear) a trade's setup, so Trading DNA can compute a
                setup-level performance breakdown.
    Args:       db (AsyncSession): The active database session.
                trade_id (int): Trade to tag.
                payload (TradeSetupUpdate): The setup id to assign, or null to clear.
    Returns:    TradeLedger: The updated trade with relations loaded.
    Raises:     HTTPException: 404 if the trade doesn't exist.
    """
    trade = await get_trade_with_relations(db, trade_id)
    trade.setup_id = payload.setup_id
    await db.commit()
    await db.refresh(trade)
    return await get_trade_with_relations(db, trade_id)


async def compute_setup_check(db: AsyncSession, price_cache: PriceCache, payload: SetupCheckRequest) -> SetupQualityResult:
    """
    Purpose:    Full pre-trade setup-quality check ("Check My Trade") —
                deeper than the existing quick precheck used by the market
                order modal. Resolves recent price history from the live
                cache or a direct Yahoo Finance fallback, then hands off to
                the pure Phase A rules-engine scoring function. Never calls
                Ollama; the score alone is decision support.
    Args:       db (AsyncSession): The active database session.
                price_cache (PriceCache): Injected shared price cache.
                payload (SetupCheckRequest): Validated trade-setup inputs.
    Returns:    SetupQualityResult: Rule-based 0-100 setup-quality score.
    Raises:     HTTPException: 404 if the portfolio doesn't exist.
    """
    portfolio = await db.get(Portfolio, payload.portfolio_id)
    if portfolio is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Portfolio not found")

    symbol = payload.symbol.upper()
    cached = (await price_cache.snapshot()).get(symbol)
    history = cached["history"] if cached else []
    if len(history) < 20:
        # The live-tick cache may be freshly booted or too sparse for a
        # reliable RSI/trend read — fall back to real historical candles
        # rather than scoring off a handful of ticks.
        candles = await fetch_yahoo_candles(symbol, interval="15m", range_="5d")
        if len(candles) > len(history):
            history = [c["close"] for c in candles]

    result = score_trade_setup(
        direction_is_buy=payload.direction.value == "buy",
        entry_price=payload.entry_price,
        stop_loss=payload.stop_loss,
        take_profit=payload.take_profit,
        position_size=payload.position_size,
        portfolio_balance=float(portfolio.current_balance),
        price_history=history,
    )

    try:
        all_trades = await list_trades_for_portfolio(db, payload.portfolio_id)
        open_trades = [t for t in all_trades if t.status == TradeStatus.OPEN]
        result.risk_assessment = await assess_risk(
            candidate_symbol=symbol,
            candidate_direction=payload.direction,
            candidate_stop_loss=payload.stop_loss,
            candidate_entry_price=payload.entry_price,
            candidate_position_size=payload.position_size,
            portfolio_balance=float(portfolio.current_balance),
            open_trades=open_trades,
            candle_fetcher=fetch_yahoo_candles,
        )
    except Exception:
        # A risk-assessment failure must never break the base setup-quality
        # score — the score is the load-bearing part of this response.
        result.risk_assessment = None

    return result
