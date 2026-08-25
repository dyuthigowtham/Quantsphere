import logging
from datetime import datetime

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.ollama_client import OllamaClient, OllamaMentorError
from app.ai.rules_engine import grade_trade
from app.dependencies import get_current_user, get_db, get_mt5_manager, get_news_cache, get_ollama_client, get_price_cache
from app.models.database import Alert, TradeDirection, TradeScreenshot, TradeStatus, User, XAIEvaluation, XAIPhase
from app.services import auth_tokens
from app.services.market_data import PriceCache, fetch_yahoo_candles
from app.services.mt5_sync import MT5ConnectionManager, ingest_closed_trade
from app.services.news import NewsCache
from config.settings import settings
from app.models.schemas import (
    AlertRead,
    BacktestRequest,
    BacktestResult,
    BenchmarkResult,
    ChatRequest,
    ChatResponse,
    DecisionTrainingAttemptCreate,
    DecisionTrainingAttemptRead,
    DecisionTrainingSummary,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    LoginResponse,
    MarketOrderRequest,
    MT5ConnectionRead,
    MT5ConnectRequest,
    MT5IngestRequest,
    MT5IngestResponse,
    NewsArticle,
    NewsImpactArticle,
    PortfolioCreate,
    PriceMoveCheckRequest,
    PriceMoveCheckResult,
    PortfolioMetrics,
    PortfolioRead,
    PreTradeCheckRequest,
    PreTradeCheckResult,
    RegimeResult,
    ReplayFeedbackResponse,
    ResetPasswordRequest,
    ResetPasswordResponse,
    ScreenshotRead,
    SetupCheckNarrativeResponse,
    SetupCheckRequest,
    SetupCreate,
    SetupPerformanceFilter,
    SetupPerformanceResult,
    SetupQualityResult,
    SetupRead,
    SimilarTradesQuery,
    SimilarTradesResult,
    StrategyCreate,
    TraderProgressionResult,
    StrategyRead,
    TradeCloseRequest,
    TradeCreate,
    TradeJournalUpdate,
    TradeRead,
    TradeSetupUpdate,
    TradingProfile,
    UserCreate,
    UserRead,
    WeeklyReviewNarrativeResponse,
    WeeklyReviewResult,
    WhyExplanationResponse,
    XAIEvaluationRead,
)
from app.services import (
    accounts,
    alerts,
    benchmarking,
    chat_context,
    decision_training,
    email,
    execution,
    mt5_accounts,
    news_impact,
    replay_mentor,
    setup_performance,
    strategy_lab,
    trade_similarity,
    trader_progression,
    trading_profile,
    weekly_review,
)
from app.services.market_regime import classify_regime
from app.services.storage import read_screenshot_bytes, save_trade_screenshot

router = APIRouter()


async def _fire_trade_open_alerts(request: Request, db: AsyncSession, user_id: int, new_trade) -> None:
    """
    Purpose:    Run Tier-1 Smart Alert detection right after a NEW trade is
                opened (never on close — the entry-pattern checks
                (revenge-trading/overtrading/oversizing) describe the
                moment a position was opened, so evaluating them at close
                time would misattribute the alert to the wrong event) and
                push anything that fires over the authenticated
                /ws/alerts channel. Best-effort: never raises, since a
                failure here must not break the trade-open response itself.
    Args:       request (Request): Used to reach app.state.alert_ws_manager.
                db (AsyncSession): The active database session.
                user_id (int): The trade's owning user, for push targeting.
                new_trade (TradeLedger): The just-opened trade, with relations loaded.
    Returns:    None.
    Raises:     None.
    """
    try:
        other_trades = [t for t in await execution.list_trades_for_portfolio(db, new_trade.portfolio_id) if t.id != new_trade.id]
        events = await alerts.evaluate_trade_event(new_trade, other_trades)
        for event in events:
            alert = await alerts.persist_alert(new_trade.portfolio_id, event)
            await request.app.state.alert_ws_manager.send_to_user(user_id, AlertRead.model_validate(alert).model_dump(mode="json"))
    except Exception:
        logging.getLogger("quantsphere.routes").exception("Smart Alert evaluation failed for trade_id=%s", new_trade.id)


@router.get("/market/prices")
async def get_market_prices(price_cache: PriceCache = Depends(get_price_cache)) -> dict:
    """
    Purpose:    Instant snapshot of the live market watchlist, so the
                dashboard has data to render immediately without waiting on
                a /ws/prices WebSocket connection to deliver the first tick.
    Args:       price_cache (PriceCache): Injected shared price cache.
    Returns:    dict: Symbol -> {"price": float, "history": list[float]}.
    Raises:     None.
    """
    return await price_cache.snapshot()


@router.get("/market/candles/{symbol}")
async def get_market_candles(symbol: str, interval: str = "15m", range: str = "1d") -> list[dict]:
    """
    Purpose:    Fetch real OHLC candle data for one symbol, for rendering an
                actual candlestick chart.
    Args:       symbol (str): Journal ticker, e.g. "EURUSD".
                interval (str): Candle bucket size (Yahoo-supported values,
                    e.g. "5m", "15m", "1h", "1d"). Defaults to "15m".
                range (str): How far back to fetch (e.g. "1d", "5d", "1mo").
                    Defaults to "1d".
    Returns:    list[dict]: Chronological OHLC candles; empty list if the
                    symbol/interval/range combination has no data.
    Raises:     None.
    """
    return await fetch_yahoo_candles(symbol, interval=interval, range_=range)


@router.get("/market/regime/{symbol}", response_model=RegimeResult)
async def get_market_regime(symbol: str, interval: str = "1h", range: str = "1mo") -> RegimeResult:
    """
    Purpose:    Live, deterministic classification of a symbol's current
                price regime (trending/ranging, high/low volatility, bullish/
                bearish bias) from real recent candles. Pure math, no AI.
    Args:       symbol (str): Journal ticker, e.g. "EURUSD".
                interval (str): Candle bucket size. Defaults to "1h" — 30+
                    hourly candles span multiple weeks, a more defensible
                    "current regime" read than a short 15m/1d window.
                range (str): How far back to fetch. Defaults to "1mo".
    Returns:    RegimeResult: The classification, or an honest "not enough
                    data" state if too few candles are available.
    Raises:     None.
    """
    candles = await fetch_yahoo_candles(symbol, interval=interval, range_=range)
    return classify_regime(candles, symbol.upper(), interval, range)


@router.get("/news", response_model=list[NewsArticle])
async def get_news(category: str = "international", news_cache: NewsCache = Depends(get_news_cache)) -> list[NewsArticle]:
    """
    Purpose:    Live market news headlines for the dashboard's news panel —
                sourced from official RSS feeds (Yahoo Finance/CNBC for
                "international", Economic Times/Business Standard for
                "national"), so traders who take news-driven trades have it
                inside QuantSphere instead of switching apps.
    Args:       category (str): "international" or "national". Defaults to "international".
                news_cache (NewsCache): Injected shared, time-boxed RSS cache.
    Returns:    list[NewsArticle]: Newest-first headlines, deduped across
                    that category's feeds.
    Raises:     HTTPException: 400 if category isn't recognized.
    """
    try:
        articles = await news_cache.get_articles(category)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    return [NewsArticle.model_validate(a) for a in articles]


@router.post("/users", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def create_user(user_data: UserCreate, db: AsyncSession = Depends(get_db)) -> UserRead:
    """
    Purpose:    Register a new QuantSphere account.
    Args:       user_data (UserCreate): Validated email/password payload.
                db (AsyncSession): The active database session.
    Returns:    UserRead: The persisted user.
    Raises:     HTTPException: 409 if the email is already registered.
    """
    user = await accounts.create_user(db, user_data)
    try:
        await email.send_welcome_email(user.email)
    except Exception:
        logging.getLogger("quantsphere.routes").exception("Welcome email failed for user_id=%s", user.id)
    return UserRead.model_validate(user)


@router.post("/auth/login", response_model=LoginResponse)
async def login(payload: LoginRequest, db: AsyncSession = Depends(get_db)) -> LoginResponse:
    """
    Purpose:    Authenticate an existing QuantSphere account and issue a
                bearer token for subsequent requests.
    Args:       payload (LoginRequest): The attempted email/password.
                db (AsyncSession): The active database session.
    Returns:    LoginResponse: The bearer token plus the user's portfolio id
                    (if they've created one) so the frontend can skip
                    straight to the dashboard.
    Raises:     HTTPException: 401 on an unknown email or wrong password
                    (same generic error either way).
    """
    user = await accounts.authenticate_user(db, payload.email, payload.password)
    token = auth_tokens.create_access_token(user.id)
    portfolio = await accounts.get_portfolio_for_user(db, user.id)
    return LoginResponse(access_token=token, user_id=user.id, portfolio_id=portfolio.id if portfolio else None)


@router.post("/auth/forgot-password", response_model=ForgotPasswordResponse)
async def forgot_password(
    payload: ForgotPasswordRequest, request: Request, db: AsyncSession = Depends(get_db)
) -> ForgotPasswordResponse:
    """
    Purpose:    Request a password-reset email. Always returns the same
                generic response whether or not the email matches a real
                account — the response, timing-sensitive info aside, must
                never let someone probe which emails are registered.
    Args:       payload (ForgotPasswordRequest): The email to send a reset
                    link to, if it belongs to an account.
                request (Request): Used to build the reset link against
                    whatever host the app is actually being reached on
                    (localhost, a tunnel, or a real domain) without needing
                    a separate hardcoded base-URL setting.
                db (AsyncSession): The active database session.
    Returns:    ForgotPasswordResponse: Always the same generic message.
    Raises:     None.
    """
    user = await accounts.find_user_by_email(db, payload.email)
    if user is not None:
        raw_token = await accounts.create_password_reset_token(db, user)
        reset_link = f"{str(request.base_url).rstrip('/')}/?reset_token={raw_token}"
        try:
            await email.send_password_reset_email(user.email, reset_link)
        except Exception:
            logging.getLogger("quantsphere.routes").exception("Password reset email failed for user_id=%s", user.id)
    return ForgotPasswordResponse()


@router.post("/auth/reset-password", response_model=ResetPasswordResponse)
async def reset_password(payload: ResetPasswordRequest, db: AsyncSession = Depends(get_db)) -> ResetPasswordResponse:
    """
    Purpose:    Complete a password reset using the token from the emailed link.
    Args:       payload (ResetPasswordRequest): The raw token plus the new password.
                db (AsyncSession): The active database session.
    Returns:    ResetPasswordResponse: Confirmation message.
    Raises:     HTTPException: 400 if the token is invalid, already used, or expired.
    """
    await accounts.reset_password_with_token(db, payload.token, payload.new_password)
    return ResetPasswordResponse()


@router.post("/portfolios", response_model=PortfolioRead, status_code=status.HTTP_201_CREATED)
async def create_portfolio(
    portfolio_data: PortfolioCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PortfolioRead:
    """
    Purpose:    Create a new paper-trading portfolio for the authenticated user.
    Args:       portfolio_data (PortfolioCreate): Validated name/starting balance.
                db (AsyncSession): The active database session.
                user (User): The authenticated owner.
    Returns:    PortfolioRead: The persisted portfolio.
    Raises:     None.
    """
    portfolio = await accounts.create_portfolio(db, user.id, portfolio_data)
    return PortfolioRead.model_validate(portfolio)


@router.get("/portfolios/{portfolio_id}", response_model=PortfolioRead)
async def get_portfolio(
    portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> PortfolioRead:
    """
    Purpose:    Fetch a portfolio's current state (e.g. live balance) for display.
    Args:       portfolio_id (int): Identifier of the portfolio to fetch.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    PortfolioRead: The portfolio's current state.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    portfolio = await accounts.get_owned_portfolio(db, portfolio_id, user)
    return PortfolioRead.model_validate(portfolio)


@router.post("/portfolios/{portfolio_id}/chat", response_model=ChatResponse)
async def chat_with_assistant(
    portfolio_id: int,
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    price_cache: PriceCache = Depends(get_price_cache),
    ollama: OllamaClient = Depends(get_ollama_client),
    user: User = Depends(get_current_user),
) -> ChatResponse:
    """
    Purpose:    Conversational assistant: answers questions about the user's
                own trades/AI-mentor feedback (grounded in their actual
                journal data, so it doesn't guess) or general trading-domain
                questions (from the model's own knowledge). Runs entirely
                through the local Ollama instance — same as the Phase B mentor.
    Args:       portfolio_id (int): Portfolio whose data grounds the answer.
                payload (ChatRequest): The user's message plus recent history.
                db (AsyncSession): The active database session.
                price_cache (PriceCache): Injected shared price cache.
                ollama (OllamaClient): Injected local Ollama client.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    ChatResponse: The assistant's reply.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't
                    the user's; 502 if the local Ollama instance is
                    unreachable or returns an unexpected response.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    context = await chat_context.build_chat_context(db, price_cache, portfolio_id)
    history = [{"role": m.role, "content": m.content} for m in payload.history]
    try:
        reply = await ollama.chat(context, history, payload.message)
    except OllamaMentorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return ChatResponse(reply=reply)


@router.get("/portfolios/{portfolio_id}/metrics", response_model=PortfolioMetrics)
async def get_portfolio_metrics(
    portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> PortfolioMetrics:
    """
    Purpose:    Compute advanced risk metrics (Sharpe ratio, max drawdown, win
                rate) for a portfolio's trade history.
    Args:       portfolio_id (int): Portfolio to compute metrics for.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    PortfolioMetrics: Trade count, win rate, Sharpe ratio, and max drawdown.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    portfolio = await accounts.get_owned_portfolio(db, portfolio_id, user)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    return execution.calculate_portfolio_metrics(trades, float(portfolio.starting_balance))


@router.get("/portfolios/{portfolio_id}/trading-profile", response_model=TradingProfile)
async def get_trading_profile(
    portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> TradingProfile:
    """
    Purpose:    The single computed analytics snapshot behind Trading DNA,
                the Mistake Detector, and Trading Health — always derived
                fresh from real closed trades, never persisted, never
                fabricated. Every surface that shows behavioral analytics
                renders from this one response.
    Args:       portfolio_id (int): Portfolio to analyze.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    TradingProfile: The full analytics snapshot.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    portfolio = await accounts.get_owned_portfolio(db, portfolio_id, user)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    return trading_profile.compute_trading_profile(portfolio_id, trades, float(portfolio.starting_balance))


@router.post("/portfolios/{portfolio_id}/trading-health/why", response_model=WhyExplanationResponse)
async def explain_trading_health(
    portfolio_id: int,
    db: AsyncSession = Depends(get_db),
    ollama: OllamaClient = Depends(get_ollama_client),
    user: User = Depends(get_current_user),
) -> WhyExplanationResponse:
    """
    Purpose:    Optional, explicitly-triggered AI narrative explaining a
                Trading Health score — separate endpoint so the base score
                never waits on Ollama.
    Args:       portfolio_id (int): Portfolio to explain.
                db (AsyncSession): The active database session.
                ollama (OllamaClient): Injected local Ollama client.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    WhyExplanationResponse: The AI's grounded explanation.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't
                    the user's; 422 if the Trading Health score itself is
                    gated on insufficient data (nothing real to explain yet);
                    502 if the local Ollama instance is unreachable or
                    returns an invalid response.
    """
    portfolio = await accounts.get_owned_portfolio(db, portfolio_id, user)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    profile = trading_profile.compute_trading_profile(portfolio_id, trades, float(portfolio.starting_balance))
    if not profile.health.has_sufficient_data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Trading Health is gated on insufficient data — nothing to explain yet.",
        )

    grounding = {
        "trades_analyzed": profile.trades_analyzed,
        "health": profile.health.model_dump(mode="json"),
    }
    try:
        return await ollama.explain_why("trading_health", grounding)
    except OllamaMentorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.post("/portfolios/{portfolio_id}/mistakes/{category}/why", response_model=WhyExplanationResponse)
async def explain_mistake(
    portfolio_id: int,
    category: str,
    db: AsyncSession = Depends(get_db),
    ollama: OllamaClient = Depends(get_ollama_client),
    user: User = Depends(get_current_user),
) -> WhyExplanationResponse:
    """
    Purpose:    Optional, explicitly-triggered AI narrative explaining one
                Mistake Detector flag — separate endpoint so the base
                Mistake Detector never waits on Ollama.
    Args:       portfolio_id (int): Portfolio to explain.
                category (str): The mistake category to explain, e.g.
                    "overtrading" — must be one of TradingProfile.mistakes'
                    real categories.
                db (AsyncSession): The active database session.
                ollama (OllamaClient): Injected local Ollama client.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    WhyExplanationResponse: The AI's grounded explanation.
    Raises:     HTTPException: 404 if the portfolio doesn't exist/isn't the
                    user's, or category isn't a recognized mistake category;
                    422 if that category isn't actually tracked with real
                    occurrences yet; 502 if the local Ollama instance is
                    unreachable or returns an invalid response.
    """
    portfolio = await accounts.get_owned_portfolio(db, portfolio_id, user)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    profile = trading_profile.compute_trading_profile(portfolio_id, trades, float(portfolio.starting_balance))

    mistake = next((m for m in profile.mistakes if m.category == category), None)
    if mistake is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown mistake category")
    if mistake.status != "tracked" or mistake.occurrences == 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This mistake isn't tracked with real occurrences yet — nothing to explain.",
        )

    example_trades = [t for t in trades if t.id in mistake.example_trade_ids][:10]
    grounding = {
        "mistake": mistake.model_dump(mode="json"),
        "example_trades": [
            {
                "id": t.id,
                "symbol": t.symbol,
                "direction": t.direction.value,
                "volume": float(t.volume),
                "open_time": t.open_time.isoformat(),
                "close_time": t.close_time.isoformat() if t.close_time else None,
                "profit": float(t.profit) if t.profit is not None else None,
            }
            for t in example_trades
        ],
    }
    try:
        return await ollama.explain_why(f"mistake:{category}", grounding)
    except OllamaMentorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc


@router.get("/portfolios/{portfolio_id}/progression", response_model=TraderProgressionResult)
async def get_trader_progression(
    portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> TraderProgressionResult:
    """
    Purpose:    Honest trader-progression snapshot — a current-vs-baseline
                trend, real trade/Decision-Training milestones, and
                journaling coverage — always derived fresh from real trades
                and attempts, never persisted, never fabricated.
    Args:       portfolio_id (int): Portfolio to analyze.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    TraderProgressionResult: The full progression snapshot.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    portfolio = await accounts.get_owned_portfolio(db, portfolio_id, user)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    attempts = await decision_training.list_attempts_for_portfolio(db, portfolio_id)
    return trader_progression.compute_progression(portfolio_id, trades, float(portfolio.starting_balance), attempts)


@router.get("/portfolios/{portfolio_id}/benchmark", response_model=BenchmarkResult)
async def get_benchmark(
    portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> BenchmarkResult:
    """
    Purpose:    Anonymous percentile comparison against other real
                QuantSphere traders — the one endpoint whose underlying
                query spans multiple users' data. Never exposes which user
                contributed which datapoint, and never fabricates a peer if
                the platform's real population is still too small.
    Args:       portfolio_id (int): Portfolio to benchmark.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    BenchmarkResult: Honestly gated percentile comparison.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    portfolio = await accounts.get_owned_portfolio(db, portfolio_id, user)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    closed = [t for t in trades if t.status == TradeStatus.CLOSED and t.profit is not None]
    profile = trading_profile.compute_trading_profile(portfolio_id, trades, float(portfolio.starting_balance))
    peers = await benchmarking.fetch_peer_aggregates(db, exclude_user_id=user.id)
    return benchmarking.compute_benchmark(portfolio_id, closed, profile.risk_reward.avg_realized_rr, peers)


@router.get("/portfolios/{portfolio_id}/alerts", response_model=list[AlertRead])
async def list_alerts(
    portfolio_id: int,
    unread_only: bool = False,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[AlertRead]:
    """
    Purpose:    List a portfolio's persisted Smart Alerts, newest first —
                what makes an alert durable across a reopened app, since
                live delivery over /ws/alerts is best-effort.
    Args:       portfolio_id (int): Portfolio whose alerts should be listed.
                unread_only (bool): If true, only return alerts not yet marked read.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    list[AlertRead]: Alerts, newest first.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    stmt = select(Alert).where(Alert.portfolio_id == portfolio_id).order_by(Alert.created_at.desc())
    if unread_only:
        stmt = stmt.where(Alert.read == False)  # noqa: E712
    rows = (await db.execute(stmt)).scalars().all()
    return [AlertRead.model_validate(a) for a in rows]


@router.post("/alerts/{alert_id}/read", response_model=AlertRead)
async def mark_alert_read(
    alert_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> AlertRead:
    """
    Purpose:    Mark one Smart Alert as read.
    Args:       alert_id (int): Identifier of the alert to mark read.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this alert.
    Returns:    AlertRead: The updated alert.
    Raises:     HTTPException: 404 if the alert doesn't exist or isn't the user's.
    """
    alert = await accounts.get_owned_alert(db, alert_id, user)
    alert.read = True
    await db.commit()
    await db.refresh(alert)
    return AlertRead.model_validate(alert)


@router.get("/portfolios/{portfolio_id}/news-impact", response_model=list[NewsImpactArticle])
async def get_news_impact(
    portfolio_id: int,
    category: str = "international",
    db: AsyncSession = Depends(get_db),
    news_cache: NewsCache = Depends(get_news_cache),
    user: User = Depends(get_current_user),
) -> list[NewsImpactArticle]:
    """
    Purpose:    Tag news headlines with the requester's own known/traded
                symbols and real position-overlap context — pure arithmetic,
                never a causality claim, computed on every request from the
                same shared NewsCache the plain /news feed uses.
    Args:       portfolio_id (int): Portfolio whose traded symbols/trades ground the tagging.
                category (str): "international" or "national". Defaults to "international".
                db (AsyncSession): The active database session.
                news_cache (NewsCache): Injected shared, time-boxed RSS cache.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    list[NewsImpactArticle]: Newest-first, tagged headlines.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't
                    the user's; 400 if category isn't recognized.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    traded_symbols = {t.symbol.upper() for t in trades}
    candidate_symbols = list({*settings.market_data_default_symbols, *traded_symbols})

    try:
        articles = await news_cache.get_articles(category)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    results = []
    for article in articles:
        matched = news_impact.tag_symbols(article["title"], candidate_symbols)
        context = news_impact.build_position_context(article["published_at"], matched, trades)
        results.append(NewsImpactArticle(**article, matched_symbols=matched, your_position_context=context))
    return results


@router.post("/portfolios/{portfolio_id}/news-impact/price-check", response_model=PriceMoveCheckResult)
async def check_news_price_move(
    portfolio_id: int,
    payload: PriceMoveCheckRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> PriceMoveCheckResult:
    """
    Purpose:    On-demand, one-click check of whether a symbol's real price
                moved around a specific article's publish time — never
                computed proactively for every headline. Always carries a
                fixed disclaimer that a coincidental move is not evidence
                of causation.
    Args:       portfolio_id (int): Portfolio (ownership-checked; the check
                    itself doesn't depend on portfolio data beyond auth).
                payload (PriceMoveCheckRequest): The symbol and the
                    article's publish time to check around.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    PriceMoveCheckResult: The real price comparison.
    Raises:     HTTPException: 404 if the portfolio doesn't exist/isn't the
                    user's, or if published_at falls outside what real
                    candle data can reach (never a guessed estimate).
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    result = await news_impact.compute_price_move(payload.symbol, payload.published_at, fetch_yahoo_candles)
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No real candle data covers this article's publish time for this symbol.",
        )
    return result


@router.get("/portfolios/{portfolio_id}/setup-performance", response_model=SetupPerformanceResult)
async def get_setup_performance(
    portfolio_id: int,
    symbol: str | None = None,
    session: str | None = None,
    direction: TradeDirection | None = None,
    setup_id: int | None = None,
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> SetupPerformanceResult:
    """
    Purpose:    Filterable per-setup/strategy performance table (the Setup
                Performance Engine) — win rate, avg realized R, profit
                factor, and expectancy per setup, optionally narrowed by
                symbol/session/direction/setup/date range.
    Args:       portfolio_id (int): Portfolio to analyze.
                symbol/session/direction/setup_id/date_from/date_to: Optional filters.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    SetupPerformanceResult: One row per setup, honestly gated on trade count.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    filters = SetupPerformanceFilter(
        symbol=symbol, session=session, direction=direction, setup_id=setup_id, date_from=date_from, date_to=date_to
    )
    return setup_performance.compute_setup_performance(portfolio_id, trades, filters)


@router.get("/portfolios/{portfolio_id}/setups", response_model=list[SetupRead])
async def list_setups(
    portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[SetupRead]:
    """
    Purpose:    List every trading setup defined under a portfolio, for the
                setup picker on trade tagging and the "Check My Trade" form.
    Args:       portfolio_id (int): Portfolio whose setups should be listed.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    list[SetupRead]: Setups, newest first.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    setups = await accounts.list_setups_for_portfolio(db, portfolio_id)
    return [SetupRead.model_validate(s) for s in setups]


@router.post("/setups", response_model=SetupRead, status_code=status.HTTP_201_CREATED)
async def create_setup(
    setup_data: SetupCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> SetupRead:
    """
    Purpose:    Define a new named trading setup/strategy under a portfolio.
    Args:       setup_data (SetupCreate): Validated portfolio/name/description.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own the target portfolio.
    Returns:    SetupRead: The persisted setup.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't
                    the user's; 409 if a setup with that name already exists
                    on the portfolio.
    """
    await accounts.get_owned_portfolio(db, setup_data.portfolio_id, user)
    setup = await accounts.create_setup(db, setup_data)
    return SetupRead.model_validate(setup)


@router.put("/trades/{trade_id}/setup", response_model=TradeRead)
async def set_trade_setup(
    trade_id: int, payload: TradeSetupUpdate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> TradeRead:
    """
    Purpose:    Tag (or clear) a trade's setup, so Trading DNA can compute a
                setup-level performance breakdown.
    Args:       trade_id (int): Trade to tag.
                payload (TradeSetupUpdate): The setup id to assign, or null to clear.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this trade.
    Returns:    TradeRead: The updated trade.
    Raises:     HTTPException: 404 if the trade doesn't exist or isn't the user's.
    """
    await execution.get_owned_trade(db, trade_id, user)
    trade = await execution.update_trade_setup(db, trade_id, payload)
    return TradeRead.model_validate(trade)


@router.post("/trades/setup-check", response_model=SetupQualityResult)
async def check_trade_setup(
    payload: SetupCheckRequest,
    db: AsyncSession = Depends(get_db),
    price_cache: PriceCache = Depends(get_price_cache),
    user: User = Depends(get_current_user),
) -> SetupQualityResult:
    """
    Purpose:    Full pre-trade setup-quality check ("Check My Trade") —
                deeper than /trades/precheck (which stays unchanged and keeps
                powering the quick market-order modal). Deterministic,
                rule-based scoring only; never calls Ollama.
    Args:       payload (SetupCheckRequest): Validated trade-setup inputs.
                db (AsyncSession): The active database session.
                price_cache (PriceCache): Injected shared price cache.
                user (User): The authenticated requester; must own the target portfolio.
    Returns:    SetupQualityResult: Rule-based 0-100 setup-quality score.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    await accounts.get_owned_portfolio(db, payload.portfolio_id, user)
    return await execution.compute_setup_check(db, price_cache, payload)


@router.post("/trades/setup-check/narrative", response_model=SetupCheckNarrativeResponse)
async def narrate_trade_setup_check(
    payload: SetupCheckRequest,
    db: AsyncSession = Depends(get_db),
    price_cache: PriceCache = Depends(get_price_cache),
    ollama: OllamaClient = Depends(get_ollama_client),
    user: User = Depends(get_current_user),
) -> SetupCheckNarrativeResponse:
    """
    Purpose:    Optional, explicitly-triggered AI narrative expanding on a
                "Check My Trade" score — separate endpoint from the instant
                rule-based score so the score itself never waits on Ollama.
    Args:       payload (SetupCheckRequest): Validated trade-setup inputs.
                db (AsyncSession): The active database session.
                price_cache (PriceCache): Injected shared price cache.
                ollama (OllamaClient): Injected local Ollama client.
                user (User): The authenticated requester; must own the target portfolio.
    Returns:    SetupCheckNarrativeResponse: The AI's narrative commentary.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't
                    the user's; 502 if the local Ollama instance is
                    unreachable or returns an empty response.
    """
    await accounts.get_owned_portfolio(db, payload.portfolio_id, user)
    quality_result = await execution.compute_setup_check(db, price_cache, payload)
    try:
        narrative = await ollama.setup_check_narrative(quality_result, payload.model_dump(mode="json"))
    except OllamaMentorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return SetupCheckNarrativeResponse(narrative=narrative)


@router.post("/trades/similar-history", response_model=SimilarTradesResult)
async def get_similar_trade_history(
    payload: SimilarTradesQuery, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> SimilarTradesResult:
    """
    Purpose:    "What Would Past You Do?" — before taking a trade, search
                historical trades with the same symbol (and score by
                direction/setup/session match) to show how similar setups
                have played out. Pure DB query; never touches Ollama.
    Args:       payload (SimilarTradesQuery): Symbol/direction/setup to match against.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own the target portfolio.
    Returns:    SimilarTradesResult: Win/loss split and matched trades.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    await accounts.get_owned_portfolio(db, payload.portfolio_id, user)
    closed = await execution.list_trades_for_portfolio(db, payload.portfolio_id)
    return trade_similarity.find_similar_trades(
        closed, payload.symbol, payload.direction, payload.setup_id, query_hour_utc=None
    )


@router.post("/portfolios/{portfolio_id}/coach/chat", response_model=ChatResponse)
async def chat_with_coach(
    portfolio_id: int,
    payload: ChatRequest,
    db: AsyncSession = Depends(get_db),
    price_cache: PriceCache = Depends(get_price_cache),
    ollama: OllamaClient = Depends(get_ollama_client),
    user: User = Depends(get_current_user),
) -> ChatResponse:
    """
    Purpose:    The full-page AI Coach — same local Ollama chat call as the
                floating assistant, but grounded in the richer structured
                trading profile (Trading DNA/Mistake Detector/Health stats)
                so "why is my win rate falling" style questions are answered
                from real derived numbers, not guessed.
    Args:       portfolio_id (int): Portfolio whose data grounds the answer.
                payload (ChatRequest): The user's message plus recent history.
                db (AsyncSession): The active database session.
                price_cache (PriceCache): Injected shared price cache.
                ollama (OllamaClient): Injected local Ollama client.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    ChatResponse: The coach's reply.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't
                    the user's; 502 if the local Ollama instance is
                    unreachable or returns an unexpected response.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    context = await chat_context.build_coach_context(db, price_cache, portfolio_id)
    history = [{"role": m.role, "content": m.content} for m in payload.history]
    try:
        reply = await ollama.chat(context, history, payload.message)
    except OllamaMentorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
    return ChatResponse(reply=reply)


@router.post("/portfolios/{portfolio_id}/mt5/connect", response_model=MT5ConnectionRead)
async def connect_mt5_account(
    portfolio_id: int,
    payload: MT5ConnectRequest,
    db: AsyncSession = Depends(get_db),
    mt5_manager: MT5ConnectionManager = Depends(get_mt5_manager),
    user: User = Depends(get_current_user),
) -> MT5ConnectionRead:
    """
    Purpose:    Link a MetaTrader 5 account to a portfolio from the UI. The
                MT5 terminal must be installed and reachable on THIS machine —
                this is not a remote broker API — so this is disabled by
                default on hosted multi-user deployments (settings.mt5_enabled).
                Real trades are never placed; this only reads closed deal
                history into the journal.
    Args:       portfolio_id (int): Portfolio to link the account to.
                payload (MT5ConnectRequest): Login/password/server/terminal_path.
                db (AsyncSession): The active database session.
                mt5_manager (MT5ConnectionManager): Injected shared sync-task registry.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    MT5ConnectionRead: The now-connected account (no password fields).
    Raises:     HTTPException: 501 if MT5 sync is disabled on this deployment;
                    404 if the portfolio doesn't exist or isn't the user's;
                    502 if the terminal is unreachable or credentials are rejected.
    """
    if not settings.mt5_enabled:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="MT5 sync is not available on this deployment")
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    connection = await mt5_accounts.connect_account(db, mt5_manager, portfolio_id, payload)
    return MT5ConnectionRead.model_validate(connection)


@router.post("/portfolios/{portfolio_id}/mt5/disconnect", status_code=status.HTTP_204_NO_CONTENT)
async def disconnect_mt5_account(
    portfolio_id: int,
    db: AsyncSession = Depends(get_db),
    mt5_manager: MT5ConnectionManager = Depends(get_mt5_manager),
    user: User = Depends(get_current_user),
) -> None:
    """
    Purpose:    Unlink a portfolio's MT5 account and stop its background sync loop.
    Args:       portfolio_id (int): Portfolio whose account should be unlinked.
                db (AsyncSession): The active database session.
                mt5_manager (MT5ConnectionManager): Injected shared sync-task registry.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    None.
    Raises:     HTTPException: 501 if MT5 sync is disabled on this deployment;
                    404 if no account is linked to this portfolio or it isn't the user's.
    """
    if not settings.mt5_enabled:
        raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="MT5 sync is not available on this deployment")
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    await mt5_accounts.disconnect_account(db, mt5_manager, portfolio_id)


@router.get("/portfolios/{portfolio_id}/mt5/status", response_model=MT5ConnectionRead | None)
async def get_mt5_status(
    portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> MT5ConnectionRead | None:
    """
    Purpose:    Fetch a portfolio's current MT5 link status for the dashboard.
    Args:       portfolio_id (int): Portfolio to check.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    MT5ConnectionRead | None: The connection's status, or None if
                    never linked or MT5 sync is disabled on this deployment.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    if not settings.mt5_enabled:
        return None
    connection = await mt5_accounts.get_status(db, portfolio_id)
    return MT5ConnectionRead.model_validate(connection) if connection else None


@router.post("/portfolios/{portfolio_id}/mt5/ingest", response_model=MT5IngestResponse)
async def ingest_mt5_deals(
    portfolio_id: int,
    payload: MT5IngestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MT5IngestResponse:
    """
    Purpose:    Receive closed MT5 deal history pushed by a user's own
                desktop MT5 bridge (bridge/mt5_bridge.py, run on the user's
                own Windows PC next to their MT5 terminal) — the
                multi-user-safe alternative to the disabled direct-connect
                flow, since the server never talks to MT5 itself here.
                Always available regardless of settings.mt5_enabled, which
                only gates the old direct-connect routes above.
    Args:       portfolio_id (int): Portfolio to attribute the trades to.
                payload (MT5IngestRequest): Batched closed-deal history.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    MT5IngestResponse: How many deals were received (duplicates
                    from a re-sent batch are silently skipped, not reported
                    as failures — the bridge always re-sends full history).
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    for deal in payload.deals:
        await ingest_closed_trade(portfolio_id, deal.model_dump())
    return MT5IngestResponse(received_count=len(payload.deals))


@router.get("/portfolios/{portfolio_id}/trades", response_model=list[TradeRead])
async def list_portfolio_trades(
    portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[TradeRead]:
    """
    Purpose:    List every trade in a portfolio's journal, newest first.
    Args:       portfolio_id (int): Portfolio whose trades should be listed.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    list[TradeRead]: The portfolio's trades with evaluations/screenshots.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    return [TradeRead.model_validate(trade) for trade in trades]


@router.post("/trades", response_model=TradeRead, status_code=status.HTTP_201_CREATED)
async def create_trade(
    trade_data: TradeCreate, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> TradeRead:
    """
    Purpose:    Manually journal a newly opened trade.
    Args:       trade_data (TradeCreate): Validated Pydantic schema of the requested trade.
                request (Request): Used to reach app.state.alert_ws_manager for Smart Alerts.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own the target portfolio.
    Returns:    TradeRead: The persisted, open trade.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't
                    the user's; otherwise propagated from the execution
                    service on invalid input.
    """
    await accounts.get_owned_portfolio(db, trade_data.portfolio_id, user)
    trade = await execution.open_trade(db, trade_data)
    full_trade = await execution.get_trade_with_relations(db, trade.id)
    await _fire_trade_open_alerts(request, db, user.id, full_trade)
    return TradeRead.model_validate(full_trade)


@router.post("/trades/precheck", response_model=PreTradeCheckResult)
async def precheck_market_trade(
    payload: PreTradeCheckRequest,
    db: AsyncSession = Depends(get_db),
    price_cache: PriceCache = Depends(get_price_cache),
    user: User = Depends(get_current_user),
) -> PreTradeCheckResult:
    """
    Purpose:    Instant, zero-latency pre-trade risk/RSI readout shown before
                a market order is placed — the paper-trading learning area's
                pre-trade check. Pure local computation; never calls Ollama.
    Args:       payload (PreTradeCheckRequest): Validated portfolio/symbol/volume.
                db (AsyncSession): The active database session.
                price_cache (PriceCache): Injected shared price cache.
                user (User): The authenticated requester; must own the target portfolio.
    Returns:    PreTradeCheckResult: Position-size risk level plus RSI-based
                    buy/sell guidance notes.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't
                    the user's; 502 if a live price for the symbol can't be fetched.
    """
    await accounts.get_owned_portfolio(db, payload.portfolio_id, user)
    return await execution.precheck_market_trade(db, price_cache, payload)


@router.post("/trades/market/open", response_model=TradeRead, status_code=status.HTTP_201_CREATED)
async def open_market_trade(
    order: MarketOrderRequest, request: Request, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> TradeRead:
    """
    Purpose:    One-click paper trading: open a trade at the current live
                market price, so the user never types a price in by hand.
    Args:       order (MarketOrderRequest): Validated portfolio/symbol/direction/volume.
                request (Request): Used to reach app.state.alert_ws_manager for Smart Alerts.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own the target portfolio.
    Returns:    TradeRead: The persisted, open trade at the fetched market price.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't
                    the user's; 502 if a live price for the symbol can't be fetched.
    """
    await accounts.get_owned_portfolio(db, order.portfolio_id, user)
    trade = await execution.open_trade_at_market(db, order)
    full_trade = await execution.get_trade_with_relations(db, trade.id)
    await _fire_trade_open_alerts(request, db, user.id, full_trade)
    return TradeRead.model_validate(full_trade)


@router.post("/trades/{trade_id}/close/market", response_model=TradeRead)
async def close_market_trade(
    trade_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> TradeRead:
    """
    Purpose:    One-click paper trading: close an open trade at the current
                live market price for its symbol.
    Args:       trade_id (int): Identifier of the trade to close.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this trade.
    Returns:    TradeRead: The updated, closed trade with its Phase A evaluation.
    Raises:     HTTPException: 404 if not found or isn't the user's; 409 if
                    already closed; 502 if a live price for the symbol can't be fetched.
    """
    await execution.get_owned_trade(db, trade_id, user)
    trade = await execution.close_trade_at_market(db, trade_id)
    full_trade = await execution.get_trade_with_relations(db, trade.id)
    return TradeRead.model_validate(full_trade)


@router.post("/trades/{trade_id}/close", response_model=TradeRead)
async def close_trade(
    trade_id: int,
    close_data: TradeCloseRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TradeRead:
    """
    Purpose:    Close an open trade, instantly attaching the Phase A rules-engine grade.
    Args:       trade_id (int): Identifier of the trade to close.
                close_data (TradeCloseRequest): Validated close price/time/costs.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this trade.
    Returns:    TradeRead: The updated, closed trade with its Phase A evaluation.
    Raises:     HTTPException: 404 if not found or isn't the user's; 409 if already closed.
    """
    await execution.get_owned_trade(db, trade_id, user)
    trade = await execution.close_trade(db, trade_id, close_data)
    full_trade = await execution.get_trade_with_relations(db, trade.id)
    return TradeRead.model_validate(full_trade)


@router.get("/trades/{trade_id}", response_model=TradeRead)
async def get_trade(
    trade_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> TradeRead:
    """
    Purpose:    Fetch a single journaled trade with its screenshots and evaluations.
    Args:       trade_id (int): Identifier of the trade to fetch.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this trade.
    Returns:    TradeRead: The full trade record.
    Raises:     HTTPException: 404 if the trade doesn't exist or isn't the user's.
    """
    trade = await execution.get_owned_trade(db, trade_id, user)
    return TradeRead.model_validate(trade)


@router.get("/trades/{trade_id}/similar", response_model=SimilarTradesResult)
async def get_similar_trades(
    trade_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> SimilarTradesResult:
    """
    Purpose:    "Find Similar Trades" — for an existing trade, find other
                trades in the portfolio with the same symbol, scored by
                direction/setup/session match, to show how similar setups
                have played out and what separated winners from losers.
    Args:       trade_id (int): The anchor trade to find similar trades for.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this trade.
    Returns:    SimilarTradesResult: Win/loss split and matched trades
                    (excluding the anchor trade itself).
    Raises:     HTTPException: 404 if the trade doesn't exist or isn't the user's.
    """
    trade = await execution.get_owned_trade(db, trade_id, user)
    closed = await execution.list_trades_for_portfolio(db, trade.portfolio_id)
    return trade_similarity.find_similar_trades(
        closed,
        trade.symbol,
        trade.direction,
        trade.setup_id,
        query_hour_utc=trade.open_time.hour,
        exclude_trade_id=trade.id,
    )


@router.post("/trades/{trade_id}/screenshot", response_model=ScreenshotRead, status_code=status.HTTP_201_CREATED)
async def upload_trade_screenshot(
    trade_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ScreenshotRead:
    """
    Purpose:    Attach a chart/terminal screenshot to a trade for the trader's
                journal and for optional later Phase B AI analysis.
    Args:       trade_id (int): Trade to attach the screenshot to.
                file (UploadFile): The uploaded image (jpeg/png/webp, size-limited).
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this trade.
    Returns:    ScreenshotRead: The persisted screenshot record.
    Raises:     HTTPException: 404 if the trade doesn't exist or isn't the
                    user's; 415/413 from storage on an invalid content-type
                    or oversized file.
    """
    await execution.get_owned_trade(db, trade_id, user)

    file_path, _ = await save_trade_screenshot(trade_id, file)
    screenshot = TradeScreenshot(
        trade_id=trade_id,
        file_path=str(file_path),
        original_filename=file.filename or "screenshot",
        content_type=file.content_type or "application/octet-stream",
    )
    db.add(screenshot)
    await db.commit()
    await db.refresh(screenshot)
    return ScreenshotRead.model_validate(screenshot)


@router.post("/trades/{trade_id}/analyze", response_model=XAIEvaluationRead)
async def analyze_trade_with_ai(
    trade_id: int,
    db: AsyncSession = Depends(get_db),
    ollama: OllamaClient = Depends(get_ollama_client),
    user: User = Depends(get_current_user),
) -> XAIEvaluationRead:
    """
    Purpose:    Strictly on-demand Phase B endpoint ("Analyze with AI"). Packages
                the Phase A grade, raw trade data, and the trade's latest
                screenshot (if any) and asks the local Ollama mentor for a
                good/bad verdict with reasoning. This is the ONLY code path
                that ever calls the LLM — it is never invoked automatically.
    Args:       trade_id (int): Trade to analyze.
                db (AsyncSession): The active database session.
                ollama (OllamaClient): Injected local Ollama client.
                user (User): The authenticated requester; must own this trade.
    Returns:    XAIEvaluationRead: The persisted Phase B evaluation.
    Raises:     HTTPException: 404 if the trade doesn't exist, isn't the
                    user's, or has no close price yet; 502 if the local
                    Ollama instance is unreachable or returns an invalid response.
    """
    trade = await execution.get_owned_trade(db, trade_id, user)
    if trade.close_price is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade has not been closed yet")

    rules_result = grade_trade(
        open_price=float(trade.open_price),
        close_price=float(trade.close_price),
        direction_is_buy=trade.direction.value == "buy",
    )
    trade_context = {
        "symbol": trade.symbol,
        "direction": trade.direction.value,
        "volume": float(trade.volume),
        "open_price": float(trade.open_price),
        "close_price": float(trade.close_price),
        "profit": float(trade.profit) if trade.profit is not None else None,
        "comment": trade.comment,
    }

    screenshot_bytes = None
    if trade.screenshots:
        screenshot_bytes = await read_screenshot_bytes(trade.screenshots[-1].file_path)

    try:
        verdict = await ollama.analyze_trade(rules_result, trade_context, screenshot_bytes)
    except OllamaMentorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    evaluation = XAIEvaluation(
        trade_id=trade.id,
        phase=XAIPhase.LLM_MENTOR,
        grade=verdict.grade,
        verdict=verdict.verdict,
        reasoning={"summary": verdict.reasoning, "key_observations": verdict.key_observations},
    )
    db.add(evaluation)
    await db.commit()
    await db.refresh(evaluation)
    return XAIEvaluationRead.model_validate(evaluation)


@router.post("/trades/{trade_id}/replay-feedback", response_model=ReplayFeedbackResponse)
async def get_replay_feedback(
    trade_id: int,
    db: AsyncSession = Depends(get_db),
    ollama: OllamaClient = Depends(get_ollama_client),
    user: User = Depends(get_current_user),
) -> ReplayFeedbackResponse:
    """
    Purpose:    On-demand AI mentor teaching commentary for the Backtest &
                Replay view. Unlike /trades/{id}/analyze (which only sees the
                trade's own fields), this is grounded in the real price
                action around the trade — before entry, during, and after
                exit — for genuinely chart-aware coaching. Never auto-invoked.
    Args:       trade_id (int): Trade currently being replayed.
                db (AsyncSession): The active database session.
                ollama (OllamaClient): Injected local Ollama client.
                user (User): The authenticated requester; must own this trade.
    Returns:    ReplayFeedbackResponse: The mentor's teaching commentary.
    Raises:     HTTPException: 404 if the trade doesn't exist or isn't the
                    user's; 502 if no chart data could be fetched, or the
                    local Ollama instance is unreachable or returns an empty response.
    """
    trade = await execution.get_owned_trade(db, trade_id, user)

    context = await replay_mentor.build_replay_context(trade)
    if context is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Could not fetch chart data for {trade.symbol} to build replay feedback",
        )

    try:
        feedback = await ollama.replay_feedback(context)
    except OllamaMentorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc

    return ReplayFeedbackResponse(feedback=feedback)


@router.put("/trades/{trade_id}/journal", response_model=TradeRead)
async def update_trade_journal(
    trade_id: int,
    journal: TradeJournalUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TradeRead:
    """
    Purpose:    Save a trade's Backtest & Replay reflection journal — the
                user's own notes/takeaways from reviewing that trade, kept
                entirely separate from the AI mentor's commentary.
    Args:       trade_id (int): Trade whose journal to update.
                journal (TradeJournalUpdate): The prompt answers to save.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this trade.
    Returns:    TradeRead: The updated trade.
    Raises:     HTTPException: 404 if the trade doesn't exist or isn't the user's.
    """
    await execution.get_owned_trade(db, trade_id, user)
    trade = await execution.update_trade_journal(db, trade_id, journal)
    return TradeRead.model_validate(trade)


@router.post("/strategies", response_model=StrategyRead, status_code=status.HTTP_201_CREATED)
async def create_strategy(
    strategy_data: StrategyCreate, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> StrategyRead:
    """
    Purpose:    Save a new Strategy Lab strategy definition for later
                backtesting against real historical candles. A Strategy is a
                separate, simulated-only concept from a Setup — never tagged
                onto real trades.
    Args:       strategy_data (StrategyCreate): Validated name/direction/
                    conditions/risk parameters.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own the target portfolio.
    Returns:    StrategyRead: The persisted strategy.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't
                    the user's; 409 if a strategy with that name already
                    exists on the portfolio.
    """
    await accounts.get_owned_portfolio(db, strategy_data.portfolio_id, user)
    strategy = await accounts.create_strategy(db, strategy_data)
    return StrategyRead.model_validate(strategy)


@router.get("/portfolios/{portfolio_id}/strategies", response_model=list[StrategyRead])
async def list_strategies(
    portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> list[StrategyRead]:
    """
    Purpose:    List every Strategy Lab strategy saved under a portfolio.
    Args:       portfolio_id (int): Portfolio whose strategies should be listed.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    list[StrategyRead]: Strategies, newest first.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    strategies = await accounts.list_strategies_for_portfolio(db, portfolio_id)
    return [StrategyRead.model_validate(s) for s in strategies]


@router.delete("/strategies/{strategy_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_strategy(
    strategy_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> None:
    """
    Purpose:    Permanently remove a saved strategy. Does not affect any real
                trades — strategies are never tagged onto the trade ledger.
    Args:       strategy_id (int): Identifier of the strategy to delete.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this strategy.
    Returns:    None.
    Raises:     HTTPException: 404 if the strategy doesn't exist or isn't the user's.
    """
    await accounts.get_owned_strategy(db, strategy_id, user)
    await accounts.delete_strategy(db, strategy_id)


@router.post("/strategies/{strategy_id}/backtest", response_model=BacktestResult)
async def backtest_strategy(
    strategy_id: int,
    payload: BacktestRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> BacktestResult:
    """
    Purpose:    Run a saved strategy against real historical candles — always
                recomputed fresh on click, never persisted, so a result is
                never stale or reused across a strategy edit.
    Args:       strategy_id (int): Strategy to backtest.
                payload (BacktestRequest): Symbol/interval/range/risk sizing.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this strategy.
    Returns:    BacktestResult: Simulated trades, equity curve, and metrics,
                    honestly gated on simulated trade count.
    Raises:     HTTPException: 404 if the strategy doesn't exist or isn't
                    the user's; 400 if the requested interval/range isn't an
                    allowed backtest window.
    """
    strategy = await accounts.get_owned_strategy(db, strategy_id, user)
    try:
        strategy_lab.validate_backtest_window(payload.interval, payload.range_)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    candles = await fetch_yahoo_candles(payload.symbol, interval=payload.interval, range_=payload.range_)
    return strategy_lab.run_backtest(
        StrategyRead.model_validate(strategy),
        candles,
        payload.symbol.upper(),
        payload.interval,
        payload.range_,
        payload.nominal_balance,
        payload.max_holding_bars,
    )


@router.post(
    "/decision-training/attempts", response_model=DecisionTrainingAttemptRead, status_code=status.HTTP_201_CREATED
)
async def create_decision_training_attempt(
    payload: DecisionTrainingAttemptCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> DecisionTrainingAttemptRead:
    """
    Purpose:    Persist one Decision Training "what would you do here?" guess,
                graded server-side from a freshly-fetched real candle series —
                the client's own instant feedback (it already has the future
                candles for UI purposes) is never trusted for this persisted
                accuracy stat.
    Args:       payload (DecisionTrainingAttemptCreate): The trade/symbol/
                    interval/range/decision point/guess being graded.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own the target trade/portfolio.
    Returns:    DecisionTrainingAttemptRead: The persisted, server-graded attempt.
    Raises:     HTTPException: 404 if the trade doesn't exist or isn't the
                    user's; 400 if the decision candle time isn't found in
                    the freshly-fetched candles.
    """
    trade = await execution.get_owned_trade(db, payload.trade_id, user)
    await accounts.get_owned_portfolio(db, payload.portfolio_id, user)
    if trade.portfolio_id != payload.portfolio_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trade not found")

    candles = await fetch_yahoo_candles(payload.symbol, interval=payload.interval, range_=payload.range_)
    decision_index = next((i for i, c in enumerate(candles) if c["time"] == payload.decision_candle_time), None)
    if decision_index is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="decision_candle_time was not found in the freshly-fetched candles",
        )

    outcome, price_move_pct, evaluated_after_bars = decision_training.grade_attempt(
        candles, decision_index, payload.guess
    )
    attempt = await decision_training.create_attempt(db, payload, outcome, price_move_pct, evaluated_after_bars)
    return DecisionTrainingAttemptRead.model_validate(attempt)


@router.get("/portfolios/{portfolio_id}/decision-training/summary", response_model=DecisionTrainingSummary)
async def get_decision_training_summary(
    portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> DecisionTrainingSummary:
    """
    Purpose:    Honest accuracy summary for the Decision Training exercise —
                real counts always shown, accuracy_pct gated on a minimum
                number of gradeable attempts.
    Args:       portfolio_id (int): Portfolio whose attempts should be summarized.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    DecisionTrainingSummary: Attempt counts, guess tally, and gated accuracy.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    attempts = await decision_training.list_attempts_for_portfolio(db, portfolio_id)
    return decision_training.compute_summary(portfolio_id, attempts)


@router.get("/portfolios/{portfolio_id}/weekly-review", response_model=WeeklyReviewResult)
async def get_weekly_review(
    portfolio_id: int, db: AsyncSession = Depends(get_db), user: User = Depends(get_current_user)
) -> WeeklyReviewResult:
    """
    Purpose:    Date-windowed digest of the portfolio's real closed trades
                over the last 7 days — always computed fresh, never
                persisted, same philosophy as /trading-profile.
    Args:       portfolio_id (int): Portfolio to review.
                db (AsyncSession): The active database session.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    WeeklyReviewResult: The digest, honestly gated on trade count.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't the user's.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    return weekly_review.compute_weekly_review(portfolio_id, trades)


@router.post("/portfolios/{portfolio_id}/weekly-review/narrative", response_model=WeeklyReviewNarrativeResponse)
async def narrate_weekly_review(
    portfolio_id: int,
    db: AsyncSession = Depends(get_db),
    ollama: OllamaClient = Depends(get_ollama_client),
    user: User = Depends(get_current_user),
) -> WeeklyReviewNarrativeResponse:
    """
    Purpose:    Optional, explicitly-triggered AI narrative expanding on the
                Weekly Review digest — separate endpoint so the base digest
                never waits on Ollama.
    Args:       portfolio_id (int): Portfolio to review.
                db (AsyncSession): The active database session.
                ollama (OllamaClient): Injected local Ollama client.
                user (User): The authenticated requester; must own this portfolio.
    Returns:    WeeklyReviewNarrativeResponse: The AI's narrative and focus goals.
    Raises:     HTTPException: 404 if the portfolio doesn't exist or isn't
                    the user's; 502 if the local Ollama instance is
                    unreachable or returns an invalid response.
    """
    await accounts.get_owned_portfolio(db, portfolio_id, user)
    trades = await execution.list_trades_for_portfolio(db, portfolio_id)
    review = weekly_review.compute_weekly_review(portfolio_id, trades)
    try:
        return await ollama.weekly_review_narrative(review, {"portfolio_id": portfolio_id})
    except OllamaMentorError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)) from exc
