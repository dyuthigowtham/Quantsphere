from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from app.models.database import MT5ConnectionStatus, TradeDirection, TradeSource, TradeStatus, XAIPhase


def _to_naive_utc(value: datetime) -> datetime:
    """
    The trade_ledger open_time/close_time columns are naive (always-UTC)
    timestamps, but browsers send tz-aware ISO strings (Date.toISOString()
    always ends in "Z"). Without this normalization, asyncpg rejects any
    tz-aware value with "can't subtract offset-naive and offset-aware
    datetimes" — this keeps every datetime input consistently naive-UTC
    regardless of what timezone info the client attaches.
    """
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


class NewsArticle(BaseModel):
    """One headline from the market news panel's RSS aggregation."""

    title: str
    link: str
    source: str
    published_at: datetime | None
    image: str | None = None


class NewsImpactArticle(NewsArticle):
    """
    A news article tagged with which of the requester's known/traded
    symbols it mentions, plus real (never fabricated) context about
    whether the requester had an open position in that symbol at publish
    time. Never claims the article caused any price move — see
    PriceMoveCheckResult for the separate, on-demand, disclaimer-carrying
    price check.
    """

    matched_symbols: list[str] = Field(default_factory=list)
    your_position_context: str | None = None


class PriceMoveCheckRequest(BaseModel):
    """Payload for an on-demand, one-click check of a symbol's real price
    move around a specific article's publish time."""

    symbol: str = Field(min_length=1, max_length=32)
    published_at: datetime


class PriceMoveCheckResult(BaseModel):
    """
    A real price comparison around a news article's publish time — never
    evidence the article caused the move, only that a move (or lack of
    one) really happened in that window. disclaimer is always shown
    alongside pct_change.
    """

    symbol: str
    published_at: datetime
    price_before: float
    price_after: float
    pct_change: float
    checked_window_note: str
    disclaimer: str = (
        "A price move in the same window as this headline is not evidence the headline caused it — "
        "shown for context only."
    )


class UserCreate(BaseModel):
    """Payload for registering a new QuantSphere account."""

    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=8, max_length=128)


class UserRead(BaseModel):
    """Serialized view of a QuantSphere account."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    email: str
    created_at: datetime


class LoginRequest(BaseModel):
    """Payload for logging into an existing QuantSphere account."""

    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class LoginResponse(BaseModel):
    """Issued on successful login — a bearer token plus enough context for
    the frontend to skip straight to the dashboard without a second
    round trip."""

    access_token: str
    token_type: str = "bearer"
    user_id: int
    portfolio_id: int | None = Field(
        description="The user's portfolio, or null if they haven't created one yet."
    )


class PortfolioCreate(BaseModel):
    """Payload for creating a new paper-trading portfolio. The owner is
    always the authenticated user — never client-supplied."""

    name: str = Field(min_length=1, max_length=100)
    starting_balance: float = Field(gt=0)


class PortfolioRead(BaseModel):
    """Serialized view of a portfolio."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    starting_balance: float
    current_balance: float
    created_at: datetime


class PortfolioMetrics(BaseModel):
    """Advanced risk metrics computed from a portfolio's closed trade history."""

    trade_count: int
    win_rate_pct: float | None
    sharpe_ratio: float | None = Field(description="Mean/stdev of per-trade P&L; None if fewer than 2 closed trades.")
    max_drawdown_amount: float = Field(description="Largest peak-to-trough decline in cumulative P&L.")
    max_drawdown_pct: float | None = Field(description="Max drawdown as a percentage of starting balance.")


class MarketOrderRequest(BaseModel):
    """Payload for one-click paper trading: open a trade at the current live market price."""

    portfolio_id: int
    symbol: str = Field(min_length=1, max_length=32)
    direction: TradeDirection
    volume: float = Field(gt=0)
    comment: str | None = None
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)


class MT5ConnectRequest(BaseModel):
    """Payload to link a MetaTrader 5 account to a portfolio from the UI."""

    login: int = Field(gt=0)
    password: str = Field(min_length=1, max_length=200)
    server: str = Field(min_length=1, max_length=100)
    terminal_path: str | None = Field(default=None, max_length=500)


class MT5ConnectionRead(BaseModel):
    """
    Serialized view of a portfolio's linked MT5 account. Never includes the
    password, encrypted or otherwise.
    """

    model_config = ConfigDict(from_attributes=True)

    portfolio_id: int
    login: int
    server: str
    status: MT5ConnectionStatus
    last_error: str | None
    last_synced_at: datetime | None
    created_at: datetime


class MT5DealIngest(BaseModel):
    """One closed trade posted by a user's own desktop MT5 bridge — the
    exact shape MT5SyncService.fetch_new_closed_deals already produces, so
    the same ingest logic (app/services/mt5_sync.py's ingest_closed_trade)
    can be reused for both the local sync loop and this remote submission."""

    mt5_ticket_id: int
    mt5_position_id: int
    symbol: str = Field(min_length=1, max_length=32)
    direction: TradeDirection
    volume: float = Field(gt=0)
    open_price: float = Field(gt=0)
    close_price: float = Field(gt=0)
    open_time: datetime
    close_time: datetime
    profit: float
    swap: float = 0
    commission: float = 0
    comment: str | None = None

    @field_validator("open_time", "close_time")
    @classmethod
    def _normalize_times(cls, value: datetime) -> datetime:
        return _to_naive_utc(value)


class MT5IngestRequest(BaseModel):
    """Payload posted by the desktop bridge (bridge/mt5_bridge.py) —
    the trader's own closed MT5 deal history, batched."""

    deals: list[MT5DealIngest] = Field(max_length=500)


class MT5IngestResponse(BaseModel):
    """Acknowledges an MT5 bridge submission. Duplicate deals (already
    ingested on a prior submission) are silently skipped, not reported as
    failures — the bridge always re-sends full history, so re-submission is
    the normal, expected case, not an error condition."""

    received_count: int


class SetupCreate(BaseModel):
    """Payload for defining a new named trading setup/strategy under a portfolio."""

    portfolio_id: int
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class SetupRead(BaseModel):
    """Serialized view of a trading setup."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    name: str
    description: str | None
    created_at: datetime


class TradeSetupUpdate(BaseModel):
    """Payload for tagging (or clearing, via null) a trade's setup."""

    setup_id: int | None


class ChatMessage(BaseModel):
    """One turn in a chat assistant conversation."""

    role: str = Field(description="One of: user, assistant")
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    """Payload for one chat assistant turn."""

    message: str = Field(min_length=1, max_length=2000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=20)


class ChatResponse(BaseModel):
    """The chat assistant's reply."""

    reply: str


class ReplayFeedbackResponse(BaseModel):
    """The AI mentor's teaching commentary for a Backtest & Replay trade."""

    feedback: str


class PreTradeCheckRequest(BaseModel):
    """Payload for an instant pre-trade risk/RSI readout before a market order."""

    portfolio_id: int
    symbol: str = Field(min_length=1, max_length=32)
    volume: float = Field(gt=0)


class PreTradeCheckResult(BaseModel):
    """Instant Phase A pre-trade risk readout, shown before a market order is placed."""

    price: float
    notional_value: float
    risk_pct: float | None
    risk_level: str = Field(description="One of: low, moderate, high")
    rsi: float | None
    rsi_zone: str | None = Field(description="One of: overbought, oversold, neutral, or null")
    warnings: list[str]
    buy_note: str | None
    sell_note: str | None


class TradeCreate(BaseModel):
    """Payload for manually opening a trade in the journal."""

    portfolio_id: int
    symbol: str = Field(min_length=1, max_length=32)
    direction: TradeDirection
    volume: float = Field(gt=0)
    open_price: float = Field(gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    open_time: datetime
    comment: str | None = None

    @field_validator("open_time")
    @classmethod
    def _normalize_open_time(cls, value: datetime) -> datetime:
        return _to_naive_utc(value)


class TradeCloseRequest(BaseModel):
    """Payload for closing an existing open trade."""

    close_price: float = Field(gt=0)
    close_time: datetime
    swap: float = 0
    commission: float = 0

    @field_validator("close_time")
    @classmethod
    def _normalize_close_time(cls, value: datetime) -> datetime:
        return _to_naive_utc(value)


class ScreenshotRead(BaseModel):
    """Serialized view of an uploaded trade screenshot."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    trade_id: int
    original_filename: str
    content_type: str
    ai_analyzed: bool
    uploaded_at: datetime
    file_path: str = Field(exclude=True)

    @computed_field
    @property
    def url(self) -> str:
        """Browser-servable path to this screenshot, under the /media static mount."""
        return f"/media/trade_screenshots/{self.trade_id}/{Path(self.file_path).name}"


class XAIEvaluationRead(BaseModel):
    """Serialized view of a single grading pass (Phase A or Phase B)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    trade_id: int
    phase: XAIPhase
    grade: str | None
    verdict: str | None
    triggered_indicators: list | None
    reasoning: dict | None
    created_at: datetime


class TradeRead(BaseModel):
    """Full serialized view of a trade, including its evaluations and screenshots."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    source: TradeSource
    mt5_ticket_id: int | None
    symbol: str
    direction: TradeDirection
    volume: float
    open_price: float
    close_price: float | None
    stop_loss: float | None
    take_profit: float | None
    open_time: datetime
    close_time: datetime | None
    profit: float | None
    swap: float
    commission: float
    comment: str | None
    status: TradeStatus
    created_at: datetime
    screenshots: list[ScreenshotRead] = []
    evaluations: list[XAIEvaluationRead] = []
    backtest_journal: dict | None = None
    setup_id: int | None = None
    setup: "SetupRead | None" = Field(default=None, exclude=True)

    @computed_field
    @property
    def setup_name(self) -> str | None:
        """The tagged setup's name, if this trade has one — avoids a second round-trip from the frontend."""
        return self.setup.name if self.setup is not None else None


class TradeJournalUpdate(BaseModel):
    """Payload for saving a trade's Backtest & Replay reflection journal."""

    what_worked: str | None = Field(default=None, max_length=2000)
    what_to_improve: str | None = Field(default=None, max_length=2000)
    pattern_recognized: str | None = Field(default=None, max_length=2000)
    lesson: str | None = Field(default=None, max_length=2000)
    notes: str | None = Field(default=None, max_length=2000)


class RulesEngineResult(BaseModel):
    """Structured output of the Phase A local rules engine."""

    grade: str
    profit_pct: float
    rsi: float | None
    macd_line: float | None
    macd_signal: float | None
    triggered_indicators: list[str]


class MentorVerdict(BaseModel):
    """
    Strictly-typed JSON contract the local Ollama model must return for
    Phase B analysis, whether triggered from trade text data alone or
    from a trade text + screenshot pair.
    """

    verdict: str = Field(description="One of: good, bad, neutral")
    grade: str = Field(description="Letter grade, e.g. A, B-, C+")
    reasoning: str
    key_observations: list[str] = Field(default_factory=list)


# --- Trading Profile: the shared analytics source of truth behind Trading
# DNA, the Mistake Detector, and Trading Health (app/services/trading_profile.py) ---


class SymbolStat(BaseModel):
    """Closed-trade performance for one traded symbol."""

    symbol: str
    trade_count: int
    win_rate_pct: float
    total_profit: float
    avg_profit: float


class HourStat(BaseModel):
    """Closed-trade performance for one hour-of-day bucket (UTC)."""

    hour_utc: int
    trade_count: int
    win_rate_pct: float
    avg_profit: float


class SessionStat(BaseModel):
    """Closed-trade performance for one trading session window."""

    session: str = Field(description="One of: asian, london, new_york, other")
    trade_count: int
    win_rate_pct: float
    avg_profit: float


class RiskRewardStats(BaseModel):
    """
    How much of the closed-trade history actually has both a stop-loss and
    take-profit to compute a real risk/reward ratio from — most MT5-synced
    trades won't, since the broker sync only captures the final deal, not a
    trade plan. `coverage_pct` must always be shown alongside any R:R number
    so a thin sample never reads as a confident stat.
    """

    trades_with_both_sl_tp: int
    coverage_pct: float
    avg_planned_rr: float | None
    avg_realized_rr: float | None
    note: str | None = None


class SetupStat(BaseModel):
    """Closed-trade performance for one tagged setup."""

    setup_id: int
    setup_name: str
    trade_count: int
    win_rate_pct: float
    total_profit: float
    avg_profit: float


class MistakeFlag(BaseModel):
    """One recurring-mistake category, with the trades that triggered it."""

    category: str = Field(
        description=(
            "One of: overtrading, oversizing, poor_risk_reward, early_exit, "
            "revenge_trading, outside_preferred_hours, stop_loss_modification"
        )
    )
    status: str = Field(default="tracked", description="One of: tracked, not_yet_trackable, insufficient_data")
    severity: str | None = Field(default=None, description="One of: low, moderate, high, or null")
    occurrences: int = 0
    description: str
    example_trade_ids: list[int] = Field(default_factory=list)


class TradingHealthResult(BaseModel):
    """
    0-100 composite Trading Health score, broken into sub-scores. Any
    sub-score is null (never guessed) when the underlying data coverage is
    too thin to compute honestly; `notes` discloses which were excluded.
    """

    overall_score: int | None
    strategy_score: int | None
    risk_score: int | None
    discipline_score: int | None
    execution_score: int | None
    consistency_score: int | None
    has_sufficient_data: bool
    notes: list[str] = Field(default_factory=list)


class TradingProfile(BaseModel):
    """
    The single computed analytics snapshot behind Trading DNA, the Mistake
    Detector, and Trading Health — always derived fresh from real closed
    trades, never persisted or fabricated. See app/services/trading_profile.py.
    """

    portfolio_id: int
    trades_analyzed: int
    min_trades_required: int
    has_sufficient_data: bool

    best_symbol: SymbolStat | None
    worst_symbol: SymbolStat | None
    symbol_breakdown: list[SymbolStat]

    best_hour_utc: HourStat | None
    worst_hour_utc: HourStat | None
    hourly_breakdown: list[HourStat]

    avg_holding_minutes: float | None
    median_holding_minutes: float | None
    session_breakdown: list[SessionStat]

    risk_reward: RiskRewardStats
    setup_breakdown: list[SetupStat]
    setup_tagging_hint: str | None

    mistakes: list[MistakeFlag]
    health: TradingHealthResult

    strongest_edge: str | None
    biggest_weakness: str | None
    best_trading_window: str | None
    worst_trading_environment: str | None
    risk_behavior_note: str | None
    instant_insight: str | None

    timestamp_caveat: str
    generated_at: datetime


# --- Pre-Trade "Check My Trade" setup-quality check (separate from the
# existing, unchanged POST /trades/precheck used by the market-order modal) ---


class SetupCheckRequest(BaseModel):
    """Payload for a full pre-trade setup-quality check ("Check My Trade")."""

    portfolio_id: int
    symbol: str = Field(min_length=1, max_length=32)
    direction: TradeDirection
    entry_price: float = Field(gt=0)
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    position_size: float = Field(gt=0)
    setup_id: int | None = None
    timeframe: str | None = Field(default=None, max_length=20)
    reason_for_entry: str | None = Field(default=None, max_length=1000)
    confirmation_notes: str | None = Field(default=None, max_length=1000)


class SetupCheckItem(BaseModel):
    """One row of the setup-quality breakdown (e.g. "Trend alignment")."""

    label: str
    status: str = Field(description="One of: pass, warn, fail, unavailable")
    detail: str


class SetupQualityResult(BaseModel):
    """
    Deterministic, rule-based 0-100 setup-quality score — decision support,
    not a prediction. Computed entirely from the trader's own inputs plus
    recent price history; no LLM involved.
    """

    score: int
    rating: str = Field(description="One of: strong, moderate, weak")
    risk_reward_ratio: float | None
    risk_pct: float | None
    rsi: float | None
    checks: list[SetupCheckItem]
    disclaimer: str = (
        "This is automated decision support based on your inputs and recent price history — "
        "not a guarantee of outcome."
    )
    risk_assessment: "RiskAssessment | None" = None


class SetupCheckNarrativeResponse(BaseModel):
    """An optional, on-demand AI narrative expanding on a SetupQualityResult."""

    narrative: str
    disclaimer: str = "This reflects historical patterns, not a prediction — the outcome of any trade is uncertain."


# --- Setup Performance Engine (app/services/setup_performance.py) ---


class SetupPerformanceFilter(BaseModel):
    """Optional filters for the Setup Performance Engine. An unset field means "no filter"."""

    symbol: str | None = None
    session: str | None = Field(default=None, description="One of: asian, london, new_york, other")
    direction: TradeDirection | None = None
    setup_id: int | None = None
    date_from: datetime | None = None
    date_to: datetime | None = None

    @field_validator("date_from", "date_to")
    @classmethod
    def _normalize_dates(cls, value: datetime | None) -> datetime | None:
        return _to_naive_utc(value) if value is not None else None


class SetupPerformanceRow(BaseModel):
    """Performance for one setup under the applied filters."""

    setup_id: int | None = Field(description="Null for the 'Untagged' bucket (trades with no setup assigned).")
    setup_name: str
    trade_count: int
    win_rate_pct: float
    total_profit: float
    avg_profit: float
    avg_realized_r: float | None
    profit_factor: float | None = Field(description="Gross wins / gross losses. Null (never infinite) when there are no losses.")
    expectancy: float | None = Field(description="Expected profit per trade, in account-currency units.")
    has_sufficient_data: bool = Field(description="True once trade_count meets the minimum for reliable ratios.")


class SetupPerformanceResult(BaseModel):
    """Full Setup Performance Engine response for one filtered slice."""

    portfolio_id: int
    filters_applied: SetupPerformanceFilter
    trades_matched: int
    rows: list[SetupPerformanceRow]
    note: str | None
    generated_at: datetime


# --- Trade Similarity Engine ("Find Similar Trades" / "What Would Past You Do?") ---


class SimilarTradeSummary(BaseModel):
    """One matched trade in a similarity result — enough to render a clickable row."""

    trade_id: int
    symbol: str
    direction: TradeDirection
    open_time: datetime
    status: TradeStatus
    profit: float | None
    similarity_score: int


class SimilarTradesQuery(BaseModel):
    """Payload for POST /trades/similar-history ("What Would Past You Do?") — no anchor trade required."""

    portfolio_id: int
    symbol: str = Field(min_length=1, max_length=32)
    direction: TradeDirection
    setup_id: int | None = None


class SimilarTradesResult(BaseModel):
    """
    Shared result shape for both the trade-detail "Find Similar Trades" button
    and the pre-trade "What Would Past You Do?" check.
    """

    query_symbol: str
    query_direction: TradeDirection
    query_setup_id: int | None
    total_matched: int
    has_sufficient_data: bool = Field(description="True once total_matched meets the minimum for a reliable narrative.")
    winners: int
    losers: int
    win_rate_pct: float | None
    avg_profit: float | None
    avg_realized_r: float | None
    common_conditions: list[str] = Field(default_factory=list, description="Empty when has_sufficient_data is False.")
    matched_trades: list[SimilarTradeSummary]
    note: str | None


# --- Market Regime Detection (app/services/market_regime.py) ---


class RegimeResult(BaseModel):
    """
    Deterministic, heuristic-threshold classification of one symbol's current
    price regime from real recent candles. Live-only — does not reconstruct
    the regime as of any point in the past.
    """

    symbol: str
    interval: str
    range_: str
    candle_count: int
    has_sufficient_data: bool
    trend: str | None = Field(default=None, description="One of: trending_up, trending_down, ranging")
    volatility: str | None = Field(default=None, description="One of: high, low")
    bias: str | None = Field(default=None, description="One of: bullish, bearish, flat")
    label: str | None
    sma_short: float | None
    sma_long: float | None
    separation_pct: float | None
    volatility_cov_pct: float | None
    net_change_pct: float | None
    note: str | None
    disclaimer: str = (
        "Regime classification uses fixed statistical thresholds on real recent price data — "
        "a heuristic read, not a prediction."
    )


# --- Risk Management Engine (additive fields on SetupQualityResult) ---


class CorrelatedPosition(BaseModel):
    """One open position whose recent price history correlates with the candidate trade's symbol."""

    trade_id: int
    symbol: str
    direction: TradeDirection
    correlation: float
    is_high_correlation: bool


class RiskAssessment(BaseModel):
    """
    Additive risk read attached to a Check My Trade result. Never blocks or
    modifies the base setup-quality score — a failure computing this should
    always degrade to `None` rather than break the score.
    """

    max_loss_amount: float | None = Field(description="Null when no stop-loss is set — see `note`.")
    max_loss_pct: float | None
    candidate_notional: float
    existing_exposure_pct: float = Field(description="Sum of all open trades' notional / portfolio balance, before this trade.")
    portfolio_impact_pct: float | None
    open_position_count: int
    correlation_checked_count: int = Field(description="May be less than open_position_count due to the concurrency cap.")
    correlated_positions: list[CorrelatedPosition]
    note: str | None


# --- Strategy Lab (app/services/strategy_lab.py) ---


class StrategyCondition(BaseModel):
    """
    One entry-condition primitive. `type` selects which of the type-specific
    fields below are required; fields for other types stay None. Deliberately
    a small, closed set (not a general expression language) — see
    app/services/strategy_lab.py for why volume-based conditions were cut.
    """

    type: str = Field(description="One of: ema_cross, rsi_threshold, breakout")

    # ema_cross
    fast_period: int | None = Field(default=None, ge=2, le=100)
    slow_period: int | None = Field(default=None, ge=3, le=300)
    cross_direction: str | None = Field(default=None, description="One of: up, down")

    # rsi_threshold
    rsi_period: int | None = Field(default=None, ge=2, le=50)
    rsi_comparison: str | None = Field(default=None, description="One of: above, below")
    rsi_value: float | None = Field(default=None, ge=0, le=100)

    # breakout
    breakout_lookback: int | None = Field(default=None, ge=2, le=500)
    breakout_direction: str | None = Field(default=None, description="One of: above_high, below_low")

    @model_validator(mode="after")
    def _validate_type_fields(self) -> "StrategyCondition":
        if self.type == "ema_cross":
            if self.fast_period is None or self.slow_period is None or self.cross_direction not in ("up", "down"):
                raise ValueError("ema_cross requires fast_period, slow_period, and cross_direction (up/down)")
            if self.fast_period >= self.slow_period:
                raise ValueError("fast_period must be less than slow_period")
        elif self.type == "rsi_threshold":
            if self.rsi_period is None or self.rsi_comparison not in ("above", "below") or self.rsi_value is None:
                raise ValueError("rsi_threshold requires rsi_period, rsi_comparison (above/below), and rsi_value")
        elif self.type == "breakout":
            if self.breakout_lookback is None or self.breakout_direction not in ("above_high", "below_low"):
                raise ValueError("breakout requires breakout_lookback and breakout_direction (above_high/below_low)")
        else:
            raise ValueError(f"Unknown condition type: {self.type!r}")
        return self


class StrategyCreate(BaseModel):
    """Payload for defining a new Strategy Lab strategy."""

    portfolio_id: int
    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    direction: TradeDirection
    conditions: list[StrategyCondition] = Field(min_length=1, max_length=3)
    stop_loss_pct: float = Field(gt=0, le=20, description="Stop-loss distance from entry, as % of entry price.")
    target_r: float = Field(gt=0, le=20, description="Take-profit distance, as a multiple of the stop-loss distance.")


class StrategyRead(BaseModel):
    """Serialized view of a saved strategy."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    name: str
    description: str | None
    direction: TradeDirection
    conditions: list[StrategyCondition]
    stop_loss_pct: float
    target_r: float
    created_at: datetime


class BacktestRequest(BaseModel):
    """Payload to run a strategy's backtest against real historical candles."""

    model_config = ConfigDict(populate_by_name=True)

    symbol: str = Field(min_length=1, max_length=32)
    interval: str
    range_: str = Field(alias="range")
    nominal_balance: float = Field(default=10000.0, gt=0)
    max_holding_bars: int = Field(default=48, ge=1, le=500)


class SimulatedTrade(BaseModel):
    """One simulated entry/exit produced by a Strategy Lab backtest — never a real trade."""

    entry_index: int
    entry_time: int
    entry_price: float
    exit_index: int
    exit_time: int
    exit_price: float
    exit_reason: str = Field(description="One of: stop_loss, take_profit, timeout")
    bars_held: int
    r_multiple: float


class BacktestEquityPoint(BaseModel):
    """One point on the simulated equity curve, in cumulative R (not dollars)."""

    trade_index: int
    cumulative_r: float


class BacktestResult(BaseModel):
    """
    Full result of one Strategy Lab backtest run. Always recomputed fresh on
    request — never persisted — so it can never go stale or drift from what
    the strategy definition actually says today.
    """

    strategy_id: int
    strategy_name: str
    symbol: str
    interval: str
    range_: str
    candle_count: int
    warmup_bars_required: int
    trade_count: int
    min_trades_required: int
    has_sufficient_data: bool
    win_rate_pct: float | None
    profit_factor: float | None = Field(description="Null (never infinite) when there are no losing simulated trades.")
    expectancy_r: float | None
    max_drawdown_r: float | None
    equity_curve: list[BacktestEquityPoint]
    simulated_trades: list[SimulatedTrade]
    open_at_data_end: bool = Field(
        description="True if one position was still open when candles ran out — excluded from every metric above rather than force-closed at a price that never actually triggered."
    )
    note: str | None
    generated_at: datetime
    disclaimer: str = (
        "Simulated on real historical candles, one position at a time, entering at the signal bar's close "
        "with no slippage/spread modeled — a backtest of the past, not a prediction. Highly sensitive to the "
        "interval/range chosen."
    )


# --- Decision Training (app/services/decision_training.py; upgrades Backtest & Replay) ---


class DecisionTrainingAttemptCreate(BaseModel):
    """Payload for one graded 'what would you do here?' answer during a replay."""

    model_config = ConfigDict(populate_by_name=True)

    portfolio_id: int
    trade_id: int
    symbol: str = Field(min_length=1, max_length=32)
    interval: str
    range_: str = Field(alias="range")
    decision_candle_time: int
    guess: str = Field(description="One of: buy, sell, wait")


class DecisionTrainingAttemptRead(BaseModel):
    """Serialized, server-graded view of one attempt."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    trade_id: int
    symbol: str
    decision_candle_time: int
    guess: str
    outcome: str = Field(description="One of: correct, incorrect, inconclusive")
    price_move_pct: float
    evaluated_after_bars: int
    created_at: datetime


class AlertRead(BaseModel):
    """
    Serialized view of one Smart Alert — a real, deterministic, rule-triggered
    event, never generated by an LLM. See app/services/alerts.py.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    portfolio_id: int
    category: str = Field(description="One of: revenge_trading, overtrading, oversizing, sl_tp_proximity")
    severity: str | None = Field(default=None, description="One of: low, moderate, high, or null")
    message: str
    trade_id: int | None
    symbol: str | None
    read: bool
    created_at: datetime


class DecisionTrainingSummary(BaseModel):
    """Aggregate Decision Training accuracy for a portfolio — real counts always shown."""

    portfolio_id: int
    total_attempts: int
    correct: int
    incorrect: int
    inconclusive: int
    guess_counts: dict[str, int] = Field(default_factory=dict, description="Tally of buy/sell/wait guesses made.")
    accuracy_pct: float | None
    min_attempts_required: int
    has_sufficient_data: bool
    note: str | None


# --- Weekly AI Trading Review (app/services/weekly_review.py) ---


class WeeklyTradeSummary(BaseModel):
    """A single trade highlighted in the Weekly Review (best or worst of the week)."""

    trade_id: int
    symbol: str
    direction: TradeDirection
    profit: float
    r_multiple: float | None


class WeeklyReviewResult(BaseModel):
    """
    A date-windowed digest of the last 7 days of real closed trades. Computed
    fresh on every call, never persisted — same philosophy as TradingProfile.
    """

    portfolio_id: int
    window_start: datetime
    window_end: datetime
    closed_trade_count: int
    open_trade_count: int
    win_rate_pct: float | None
    total_profit: float | None
    avg_realized_r: float | None
    best_trade: WeeklyTradeSummary | None
    worst_trade: WeeklyTradeSummary | None
    best_setup_name: str | None
    worst_setup_name: str | None
    has_sufficient_data: bool
    min_trades_required: int
    note: str | None
    generated_at: datetime


class WeeklyReviewNarrativeResponse(BaseModel):
    """An optional, on-demand AI narrative expanding on a WeeklyReviewResult."""

    narrative: str
    focus_goals: list[str] = Field(default_factory=list, max_length=3)
    disclaimer: str = "Reflects last week's real trades — not a prediction of what comes next."


class ProgressionWindow(BaseModel):
    """One real time-window slice (current or baseline) of Trader Progression's trend."""

    label: str
    window_start: datetime
    window_end: datetime
    trades_closed: int
    min_trades_required: int
    has_sufficient_data: bool
    win_rate_pct: float | None
    overall_health_score: int | None
    discipline_score: int | None


class ProgressionTrend(BaseModel):
    """Current-window vs baseline-window comparison for one metric — null
    delta/direction unless both windows have sufficient data."""

    metric: str
    current_value: float | None
    baseline_value: float | None
    delta: float | None
    direction: str | None = Field(default=None, description="One of: improving, declining, flat, or null")


class ProgressionMilestone(BaseModel):
    """One real trade-count or Decision-Training-attempt-count milestone —
    achieved_at is the real timestamp of the Nth trade/attempt, never now()."""

    key: str
    label: str
    threshold: int
    achieved: bool
    achieved_at: datetime | None


class JournalingCoverage(BaseModel):
    """Real % of closed trades with a non-empty backtest_journal."""

    trades_closed: int
    trades_journaled: int
    journaled_pct: float | None
    has_sufficient_data: bool
    note: str | None


class TraderProgressionResult(BaseModel):
    """
    Honest trader-progression snapshot — a current-vs-baseline trend, real
    trade/Decision-Training milestones, and journaling coverage. Computed
    fresh from existing trade/attempt rows on every call, never persisted
    or fabricated, same philosophy as TradingProfile.
    """

    portfolio_id: int
    current_window: ProgressionWindow
    baseline_window: ProgressionWindow
    trend: list[ProgressionTrend]
    has_sufficient_trend_data: bool
    trend_note: str | None
    total_trades_closed: int
    trade_milestones: list[ProgressionMilestone]
    decision_training_milestones: list[ProgressionMilestone]
    journaling: JournalingCoverage
    generated_at: datetime


class BenchmarkResult(BaseModel):
    """
    Anonymous percentile comparison against other real QuantSphere traders
    who individually meet the same minimum-trade bar as Trading DNA. Never
    identifies which peer contributed which datapoint, and never fabricates
    a peer if the platform's real population is still too small — see
    app/services/benchmarking.py for the exact gating.
    """

    portfolio_id: int
    own_win_rate_pct: float | None
    own_avg_realized_r: float | None
    win_rate_percentile: float | None
    avg_realized_r_percentile: float | None
    peer_trader_count: int
    min_peer_traders_required: int
    min_trades_required: int
    has_sufficient_data: bool
    note: str | None


class WhyExplanationResponse(BaseModel):
    """
    An optional, on-demand AI narrative explaining an already-computed,
    deterministic analytics result (a Trading Health score, a Mistake
    Detector flag, a Trader Progression trend, etc.) — generalizes the
    reasoning/key_observations contract already proven for single-trade
    grading (see MentorVerdict) to any other analytics module, without a
    bespoke response schema per module. Never invoked automatically —
    the base result being explained is always computed and returned first,
    with zero Ollama dependency.
    """

    topic: str
    reasoning: str
    key_observations: list[str] = Field(default_factory=list)
    disclaimer: str = "Reflects your own historical trade data — not a prediction or financial advice."
