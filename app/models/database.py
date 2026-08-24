import enum
from datetime import datetime

from sqlalchemy import Enum as SAEnum
from sqlalchemy import JSON, ForeignKey, Numeric, UniqueConstraint, func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from config.settings import settings

engine = create_async_engine(settings.database_url, echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model in the app."""


class TradeSource(str, enum.Enum):
    """Origin of a TradeLedger row: typed manually, or synced from MT5."""

    MANUAL = "manual"
    MT5 = "mt5"


class TradeDirection(str, enum.Enum):
    """Market side of a trade."""

    BUY = "buy"
    SELL = "sell"


class TradeStatus(str, enum.Enum):
    """Lifecycle state of a trade."""

    OPEN = "open"
    CLOSED = "closed"


class XAIPhase(str, enum.Enum):
    """Which stage of the Hybrid XAI pipeline produced an evaluation."""

    RULES_ENGINE = "rules_engine"
    LLM_MENTOR = "llm_mentor"


class MT5ConnectionStatus(str, enum.Enum):
    """Live state of a portfolio's linked MetaTrader 5 account."""

    CONNECTED = "connected"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class User(Base):
    """A QuantSphere account holder."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(unique=True, index=True)
    hashed_password: Mapped[str]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    portfolios: Mapped[list["Portfolio"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


class Portfolio(Base):
    """A paper-trading account balance bucket owned by a User."""

    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str]
    starting_balance: Mapped[float] = mapped_column(Numeric(18, 2))
    current_balance: Mapped[float] = mapped_column(Numeric(18, 2))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    owner: Mapped["User"] = relationship(back_populates="portfolios")
    trades: Mapped[list["TradeLedger"]] = relationship(back_populates="portfolio", cascade="all, delete-orphan")
    mt5_connection: Mapped["MT5Connection | None"] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )
    setups: Mapped[list["Setup"]] = relationship(back_populates="portfolio", cascade="all, delete-orphan")
    strategies: Mapped[list["Strategy"]] = relationship(back_populates="portfolio", cascade="all, delete-orphan")


class Setup(Base):
    """
    A user-defined trading setup/strategy label (e.g. "Breakout", "Pullback")
    that trades can be tagged with, to power setup-level analytics in
    Trading DNA and (in a later phase) the Setup Performance Engine.
    """

    __tablename__ = "setups"
    __table_args__ = (UniqueConstraint("portfolio_id", "name", name="uq_setups_portfolio_id_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"))
    name: Mapped[str]
    description: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    portfolio: Mapped["Portfolio"] = relationship(back_populates="setups")
    trades: Mapped[list["TradeLedger"]] = relationship(back_populates="setup")


class TradeLedger(Base):
    """
    A single trade — either entered manually through the journal UI or
    synced automatically from a MetaTrader 5 terminal's closed deal history.
    """

    __tablename__ = "trade_ledger"
    __table_args__ = (UniqueConstraint("mt5_ticket_id", name="uq_trade_ledger_mt5_ticket_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"))

    source: Mapped[TradeSource] = mapped_column(SAEnum(TradeSource), default=TradeSource.MANUAL)
    mt5_ticket_id: Mapped[int | None] = mapped_column(index=True)
    mt5_position_id: Mapped[int | None]

    symbol: Mapped[str] = mapped_column(index=True)
    direction: Mapped[TradeDirection] = mapped_column(SAEnum(TradeDirection))
    volume: Mapped[float] = mapped_column(Numeric(18, 4))

    open_price: Mapped[float] = mapped_column(Numeric(18, 6))
    close_price: Mapped[float | None] = mapped_column(Numeric(18, 6))
    stop_loss: Mapped[float | None] = mapped_column(Numeric(18, 6))
    take_profit: Mapped[float | None] = mapped_column(Numeric(18, 6))

    open_time: Mapped[datetime]
    close_time: Mapped[datetime | None]

    profit: Mapped[float | None] = mapped_column(Numeric(18, 2))
    swap: Mapped[float] = mapped_column(Numeric(18, 2), default=0)
    commission: Mapped[float] = mapped_column(Numeric(18, 2), default=0)

    comment: Mapped[str | None]
    status: Mapped[TradeStatus] = mapped_column(SAEnum(TradeStatus), default=TradeStatus.OPEN)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    # Free-form reflection notes from the Backtest & Replay view — keyed by
    # prompt (what_worked, what_to_improve, pattern_recognized, lesson,
    # notes), not a rigid schema, so prompts can evolve without a migration.
    backtest_journal: Mapped[dict | None] = mapped_column(JSON)

    setup_id: Mapped[int | None] = mapped_column(ForeignKey("setups.id", ondelete="SET NULL"), index=True)

    portfolio: Mapped["Portfolio"] = relationship(back_populates="trades")
    screenshots: Mapped[list["TradeScreenshot"]] = relationship(
        back_populates="trade", cascade="all, delete-orphan"
    )
    evaluations: Mapped[list["XAIEvaluation"]] = relationship(
        back_populates="trade", cascade="all, delete-orphan"
    )
    setup: Mapped["Setup | None"] = relationship(back_populates="trades")


class TradeScreenshot(Base):
    """A user-uploaded screenshot (chart, broker terminal, etc.) attached to a trade."""

    __tablename__ = "trade_screenshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trade_ledger.id", ondelete="CASCADE"))

    file_path: Mapped[str]
    original_filename: Mapped[str]
    content_type: Mapped[str]
    ai_analyzed: Mapped[bool] = mapped_column(default=False)
    uploaded_at: Mapped[datetime] = mapped_column(server_default=func.now())

    trade: Mapped["TradeLedger"] = relationship(back_populates="screenshots")


class XAIEvaluation(Base):
    """
    One grading pass over a trade, produced by either the instant local
    rules engine (Phase A) or the on-demand local LLM mentor (Phase B).
    """

    __tablename__ = "xai_evaluations"

    id: Mapped[int] = mapped_column(primary_key=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trade_ledger.id", ondelete="CASCADE"))

    phase: Mapped[XAIPhase] = mapped_column(SAEnum(XAIPhase))
    grade: Mapped[str | None]
    verdict: Mapped[str | None]
    triggered_indicators: Mapped[list | None] = mapped_column(JSON)
    reasoning: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    trade: Mapped["TradeLedger"] = relationship(back_populates="evaluations")


class MT5Connection(Base):
    """
    A portfolio's MetaTrader 5 account, linked from the UI's "Connect MT5
    Account" flow. The password is encrypted at rest (app/services/crypto.py)
    — this table is the ONLY place an MT5 password is persisted; it never
    touches .env or a config file.
    """

    __tablename__ = "mt5_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), unique=True)

    login: Mapped[int]
    encrypted_password: Mapped[str]
    server: Mapped[str]
    terminal_path: Mapped[str | None]

    enabled: Mapped[bool] = mapped_column(default=True)
    status: Mapped[MT5ConnectionStatus] = mapped_column(
        SAEnum(MT5ConnectionStatus), default=MT5ConnectionStatus.DISCONNECTED
    )
    last_error: Mapped[str | None]
    last_synced_at: Mapped[datetime | None]
    last_synced_ticket: Mapped[int] = mapped_column(default=0)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    portfolio: Mapped["Portfolio"] = relationship(back_populates="mt5_connection")


class Strategy(Base):
    """
    A saved, hypothetical rule-based strategy definition for the Strategy Lab
    (Practice tab). Never tagged onto real trades — a Strategy (structured
    entry conditions + risk%/target R, backtested against simulated
    candle-by-candle history) is a different concept from a Setup (Phase 1's
    free-text label trades get tagged with).
    """

    __tablename__ = "strategies"
    __table_args__ = (UniqueConstraint("portfolio_id", "name", name="uq_strategies_portfolio_id_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"))
    name: Mapped[str]
    description: Mapped[str | None]
    direction: Mapped[TradeDirection] = mapped_column(SAEnum(TradeDirection))
    # List of condition-primitive dicts (type: ema_cross/rsi_threshold/breakout
    # + type-specific fields) — validated at the Pydantic layer, not here.
    conditions: Mapped[list] = mapped_column(JSON)
    stop_loss_pct: Mapped[float] = mapped_column(Numeric(6, 3))
    target_r: Mapped[float] = mapped_column(Numeric(6, 2))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    portfolio: Mapped["Portfolio"] = relationship(back_populates="strategies")


class DecisionTrainingAttempt(Base):
    """
    One graded "what would you do here?" answer from Decision Training
    (the upgraded Backtest & Replay). outcome/price_move_pct are always
    computed server-side from re-fetched real candles — never trusted from
    the client — so the persisted accuracy stat can never be fabricated or
    tampered with, even though the browser already has the future candles
    needed to grade the guess instantly for its own immediate UI feedback.
    """

    __tablename__ = "decision_training_attempts"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    trade_id: Mapped[int] = mapped_column(ForeignKey("trade_ledger.id", ondelete="CASCADE"))
    symbol: Mapped[str]
    interval: Mapped[str]
    range_: Mapped[str]
    decision_candle_time: Mapped[int]
    guess: Mapped[str]
    outcome: Mapped[str]
    price_move_pct: Mapped[float] = mapped_column(Numeric(10, 4))
    evaluated_after_bars: Mapped[int]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Alert(Base):
    """
    One Smart Alert — a real, deterministic, rule-triggered event (revenge
    trading, overtrading, oversizing, or a stop-loss/take-profit getting
    close), persisted so it survives a reopened app even though live
    delivery happens over the authenticated /ws/alerts WebSocket. Never
    created by an LLM — see app/services/alerts.py.
    """

    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("portfolios.id", ondelete="CASCADE"), index=True)
    category: Mapped[str]
    severity: Mapped[str | None]
    message: Mapped[str]
    trade_id: Mapped[int | None] = mapped_column(ForeignKey("trade_ledger.id", ondelete="SET NULL"))
    symbol: Mapped[str | None]
    read: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
