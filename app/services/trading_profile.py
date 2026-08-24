import statistics
from collections import defaultdict
from datetime import datetime, timezone

from app.models.database import TradeDirection, TradeLedger, TradeStatus
from app.models.schemas import (
    HourStat,
    MistakeFlag,
    PortfolioMetrics,
    RiskRewardStats,
    SessionStat,
    SetupStat,
    SymbolStat,
    TradingHealthResult,
    TradingProfile,
)
from app.services.execution import calculate_portfolio_metrics
from app.services.trade_stats import expectancy, realized_r_multiple, session_for_hour, win_rate_pct

# Below this many closed trades, every derived stat is too noisy to present
# as real — the whole profile degrades to an honest "not enough data yet"
# state instead of guessing.
MIN_TRADES_FOR_PROFILE = 10
MIN_TRADES_PER_BUCKET = 3

OVERTRADING_STDEV_MULTIPLIER = 2.0
OVERTRADING_MIN_TRADES_ON_DAY = 4
OVERSIZING_STDEV_MULTIPLIER = 2.0
REVENGE_WINDOW_MINUTES = 30
REVENGE_VOLUME_INCREASE_RATIO = 1.2
EARLY_EXIT_MAX_CAPTURED_FRACTION = 0.3
POOR_RR_THRESHOLD = 1.0
UNDERPERFORMING_HOUR_WIN_RATE_GAP_PP = 20.0

_MISTAKE_CATEGORIES = [
    "overtrading",
    "oversizing",
    "poor_risk_reward",
    "early_exit",
    "revenge_trading",
    "outside_preferred_hours",
]

TIMESTAMP_CAVEAT = (
    "Trades synced from MetaTrader 5 before this feature was added may have their open/close time "
    "recorded in the sync server's local time rather than UTC, which can skew hour-of-day and session "
    "stats for older trades. Newly-synced MT5 trades and manually-logged trades use UTC consistently."
)


def _symbol_breakdown(closed: list[TradeLedger]) -> list[SymbolStat]:
    by_symbol: dict[str, list[TradeLedger]] = defaultdict(list)
    for trade in closed:
        by_symbol[trade.symbol].append(trade)

    stats = []
    for symbol, trades in by_symbol.items():
        total = sum(float(t.profit) for t in trades)
        stats.append(
            SymbolStat(
                symbol=symbol,
                trade_count=len(trades),
                win_rate_pct=win_rate_pct(trades),
                total_profit=round(total, 2),
                avg_profit=round(total / len(trades), 2),
            )
        )
    return sorted(stats, key=lambda s: s.total_profit, reverse=True)


def _hourly_breakdown(closed: list[TradeLedger]) -> list[HourStat]:
    by_hour: dict[int, list[TradeLedger]] = defaultdict(list)
    for trade in closed:
        by_hour[trade.open_time.hour].append(trade)

    stats = []
    for hour, trades in sorted(by_hour.items()):
        total = sum(float(t.profit) for t in trades)
        stats.append(
            HourStat(
                hour_utc=hour,
                trade_count=len(trades),
                win_rate_pct=win_rate_pct(trades),
                avg_profit=round(total / len(trades), 2),
            )
        )
    return stats


def _session_breakdown(closed: list[TradeLedger]) -> list[SessionStat]:
    by_session: dict[str, list[TradeLedger]] = defaultdict(list)
    for trade in closed:
        by_session[session_for_hour(trade.open_time.hour)].append(trade)

    stats = []
    for session, trades in by_session.items():
        total = sum(float(t.profit) for t in trades)
        stats.append(
            SessionStat(
                session=session,
                trade_count=len(trades),
                win_rate_pct=win_rate_pct(trades),
                avg_profit=round(total / len(trades), 2),
            )
        )
    return sorted(stats, key=lambda s: s.trade_count, reverse=True)


def _risk_reward_stats(closed: list[TradeLedger]) -> RiskRewardStats:
    with_both = [t for t in closed if t.stop_loss is not None and t.take_profit is not None]
    coverage_pct = round(len(with_both) / len(closed) * 100, 1) if closed else 0.0

    planned_rrs = []
    realized_rrs = []
    for trade in with_both:
        open_price = float(trade.open_price)
        stop_loss = float(trade.stop_loss)
        take_profit = float(trade.take_profit)
        risk = abs(open_price - stop_loss)
        if risk <= 0:
            continue
        reward = abs(take_profit - open_price)
        planned_rrs.append(reward / risk)

        r_multiple = realized_r_multiple(trade)
        if r_multiple is not None:
            realized_rrs.append(r_multiple)

    note = None
    if coverage_pct < 20:
        note = (
            f"Only {coverage_pct:.0f}% of your closed trades have both a stop-loss and take-profit set — "
            "risk/reward analysis needs more of these to be reliable."
        )

    return RiskRewardStats(
        trades_with_both_sl_tp=len(with_both),
        coverage_pct=coverage_pct,
        avg_planned_rr=round(statistics.mean(planned_rrs), 2) if planned_rrs else None,
        avg_realized_rr=round(statistics.mean(realized_rrs), 2) if realized_rrs else None,
        note=note,
    )


def _setup_breakdown(closed: list[TradeLedger]) -> list[SetupStat]:
    by_setup: dict[int, list[TradeLedger]] = defaultdict(list)
    names: dict[int, str] = {}
    for trade in closed:
        if trade.setup_id is None:
            continue
        by_setup[trade.setup_id].append(trade)
        if trade.setup is not None:
            names[trade.setup_id] = trade.setup.name

    stats = []
    for setup_id, trades in by_setup.items():
        total = sum(float(t.profit) for t in trades)
        stats.append(
            SetupStat(
                setup_id=setup_id,
                setup_name=names.get(setup_id, "Unknown"),
                trade_count=len(trades),
                win_rate_pct=win_rate_pct(trades),
                total_profit=round(total, 2),
                avg_profit=round(total / len(trades), 2),
            )
        )
    return sorted(stats, key=lambda s: s.total_profit, reverse=True)


def _insufficient(category: str, hint: str) -> MistakeFlag:
    return MistakeFlag(category=category, status="insufficient_data", occurrences=0, description=hint)


def detect_overtrading(closed: list[TradeLedger]) -> MistakeFlag:
    by_day: dict[str, list[TradeLedger]] = defaultdict(list)
    for trade in closed:
        by_day[trade.open_time.date().isoformat()].append(trade)

    if len(by_day) < 5:
        return _insufficient("overtrading", "Trade across more days to detect an overtrading pattern.")

    counts = [len(trades) for trades in by_day.values()]
    mean_count = statistics.mean(counts)
    stdev_count = statistics.pstdev(counts)
    threshold = max(mean_count + OVERTRADING_STDEV_MULTIPLIER * stdev_count, OVERTRADING_MIN_TRADES_ON_DAY)

    flagged_ids: list[int] = []
    flagged_days = 0
    for day, trades in by_day.items():
        if len(trades) >= threshold and len(trades) >= OVERTRADING_MIN_TRADES_ON_DAY:
            flagged_days += 1
            flagged_ids.extend(t.id for t in trades)

    if not flagged_days:
        return MistakeFlag(
            category="overtrading",
            status="tracked",
            severity=None,
            occurrences=0,
            description="No days stand out as overtrading relative to your own average trade frequency.",
        )

    severity = "high" if flagged_days >= 3 else "moderate" if flagged_days == 2 else "low"
    return MistakeFlag(
        category="overtrading",
        status="tracked",
        severity=severity,
        occurrences=flagged_days,
        description=(
            f"You traded well above your own daily average on {flagged_days} day(s) "
            f"(your typical day is ~{mean_count:.1f} trades)."
        ),
        example_trade_ids=flagged_ids[:20],
    )


def detect_oversizing(closed: list[TradeLedger]) -> MistakeFlag:
    if len(closed) < MIN_TRADES_PER_BUCKET + 2:
        return _insufficient("oversizing", "Log more trades to establish your typical position size.")

    volumes = [float(t.volume) for t in closed]
    mean_volume = statistics.mean(volumes)
    stdev_volume = statistics.pstdev(volumes)
    if stdev_volume == 0:
        return MistakeFlag(
            category="oversizing",
            status="tracked",
            severity=None,
            occurrences=0,
            description="Your position sizing has been consistent — no outliers detected.",
        )

    threshold = mean_volume + OVERSIZING_STDEV_MULTIPLIER * stdev_volume
    flagged = [t for t in closed if float(t.volume) > threshold]
    if not flagged:
        return MistakeFlag(
            category="oversizing",
            status="tracked",
            severity=None,
            occurrences=0,
            description="No trades stand out as oversized relative to your own average.",
        )

    severity = "high" if len(flagged) >= 5 else "moderate" if len(flagged) >= 2 else "low"
    return MistakeFlag(
        category="oversizing",
        status="tracked",
        severity=severity,
        occurrences=len(flagged),
        description=f"{len(flagged)} trade(s) were sized well above your typical position size.",
        example_trade_ids=[t.id for t in flagged][:20],
    )


def detect_poor_risk_reward(closed: list[TradeLedger]) -> MistakeFlag:
    with_both = [t for t in closed if t.stop_loss is not None and t.take_profit is not None]
    if len(with_both) < MIN_TRADES_PER_BUCKET:
        return _insufficient(
            "poor_risk_reward", "Set a stop-loss and take-profit on more trades to evaluate risk/reward discipline."
        )

    flagged = []
    for trade in with_both:
        risk = abs(float(trade.open_price) - float(trade.stop_loss))
        if risk <= 0:
            continue
        reward = abs(float(trade.take_profit) - float(trade.open_price))
        if reward / risk < POOR_RR_THRESHOLD:
            flagged.append(trade)

    if not flagged:
        return MistakeFlag(
            category="poor_risk_reward",
            status="tracked",
            severity=None,
            occurrences=0,
            description="Your planned risk/reward has stayed at or above 1:1 across your tagged trades.",
        )

    severity = "high" if len(flagged) / len(with_both) > 0.5 else "moderate" if len(flagged) >= 2 else "low"
    return MistakeFlag(
        category="poor_risk_reward",
        status="tracked",
        severity=severity,
        occurrences=len(flagged),
        description=f"{len(flagged)} of {len(with_both)} trades with a plan risked more than the planned reward.",
        example_trade_ids=[t.id for t in flagged][:20],
    )


def detect_early_exits(closed: list[TradeLedger]) -> MistakeFlag:
    candidates = [t for t in closed if t.take_profit is not None and t.close_price is not None]
    if len(candidates) < MIN_TRADES_PER_BUCKET:
        return _insufficient("early_exit", "Set a take-profit on more trades to evaluate exit discipline.")

    flagged = []
    for trade in candidates:
        open_price = float(trade.open_price)
        planned_move = float(trade.take_profit) - open_price
        realized_move = float(trade.close_price) - open_price
        if trade.direction == TradeDirection.SELL:
            planned_move, realized_move = -planned_move, -realized_move
        if planned_move <= 0:
            continue
        captured_fraction = realized_move / planned_move
        if 0 < captured_fraction < EARLY_EXIT_MAX_CAPTURED_FRACTION:
            flagged.append(trade)

    if not flagged:
        return MistakeFlag(
            category="early_exit",
            status="tracked",
            severity=None,
            occurrences=0,
            description="You've generally held winners toward your planned target.",
        )

    severity = "high" if len(flagged) / len(candidates) > 0.5 else "moderate" if len(flagged) >= 2 else "low"
    return MistakeFlag(
        category="early_exit",
        status="tracked",
        severity=severity,
        occurrences=len(flagged),
        description=f"{len(flagged)} winning trade(s) closed well short of the planned take-profit.",
        example_trade_ids=[t.id for t in flagged][:20],
    )


def _is_revenge_trade(prev: TradeLedger, current: TradeLedger) -> bool:
    """
    Purpose:    The pairwise revenge-trade predicate — opened soon after a
                loss, sized larger than that loss. Factored out so
                app/services/alerts.py's near-real-time Smart Alert can
                reuse the exact same rule detect_revenge_trading uses for
                the whole-history Mistake Detector flag, rather than
                re-deriving the thresholds.
    Args:       prev (TradeLedger): The earlier, closed trade.
                current (TradeLedger): The trade opened after it.
    Returns:    bool: True if `current` matches the revenge-trading pattern
                    relative to `prev`.
    Raises:     None.
    """
    if prev.profit is None or float(prev.profit) >= 0 or prev.close_time is None:
        return False
    gap_minutes = (current.open_time - prev.close_time).total_seconds() / 60
    return 0 <= gap_minutes <= REVENGE_WINDOW_MINUTES and float(current.volume) > float(prev.volume) * REVENGE_VOLUME_INCREASE_RATIO


def detect_revenge_trading(closed: list[TradeLedger]) -> MistakeFlag:
    ordered = sorted((t for t in closed if t.close_time is not None), key=lambda t: t.open_time)
    if len(ordered) < MIN_TRADES_PER_BUCKET + 2:
        return _insufficient("revenge_trading", "Log more trades to detect a revenge-trading pattern.")

    flagged = [current for prev, current in zip(ordered, ordered[1:]) if _is_revenge_trade(prev, current)]

    if not flagged:
        return MistakeFlag(
            category="revenge_trading",
            status="tracked",
            severity=None,
            occurrences=0,
            description="No pattern of sizing up quickly after a loss detected.",
        )

    severity = "high" if len(flagged) >= 4 else "moderate" if len(flagged) >= 2 else "low"
    return MistakeFlag(
        category="revenge_trading",
        status="tracked",
        severity=severity,
        occurrences=len(flagged),
        description=(
            f"{len(flagged)} trade(s) were opened within {REVENGE_WINDOW_MINUTES} minutes of a loss, "
            "sized larger than the losing trade."
        ),
        example_trade_ids=[t.id for t in flagged][:20],
    )


def detect_outside_preferred_hours(closed: list[TradeLedger], hourly: list[HourStat]) -> MistakeFlag:
    eligible = [h for h in hourly if h.trade_count >= MIN_TRADES_PER_BUCKET]
    if len(eligible) < 2:
        return _insufficient("outside_preferred_hours", "Trade across more hours of the day to unlock this.")

    overall_win_rate = win_rate_pct(closed)
    weak_hours = {
        h.hour_utc for h in eligible if overall_win_rate - h.win_rate_pct >= UNDERPERFORMING_HOUR_WIN_RATE_GAP_PP
    }
    if not weak_hours:
        return MistakeFlag(
            category="outside_preferred_hours",
            status="tracked",
            severity=None,
            occurrences=0,
            description="No trading hour stands out as significantly underperforming your average.",
        )

    flagged = [t for t in closed if t.open_time.hour in weak_hours]
    hours_label = ", ".join(f"{h:02d}:00" for h in sorted(weak_hours))
    severity = "high" if len(weak_hours) >= 3 else "moderate"
    return MistakeFlag(
        category="outside_preferred_hours",
        status="tracked",
        severity=severity,
        occurrences=len(flagged),
        description=f"Your win rate drops sharply during {hours_label} UTC compared to your overall average.",
        example_trade_ids=[t.id for t in flagged][:20],
    )


def _stop_loss_modification_placeholder() -> MistakeFlag:
    return MistakeFlag(
        category="stop_loss_modification",
        status="not_yet_trackable",
        severity=None,
        occurrences=0,
        description=(
            "Not yet trackable — MT5 sync only captures a trade's final closed deal, not a history of "
            "stop-loss/take-profit modifications. This will light up once modification history is captured."
        ),
    )


def compute_trading_health(
    closed: list[TradeLedger],
    metrics: PortfolioMetrics,
    rr: RiskRewardStats,
    mistakes: list[MistakeFlag],
    starting_balance: float,
) -> TradingHealthResult:
    notes: list[str] = []

    consistency_score = None
    if metrics.sharpe_ratio is not None:
        consistency_score = round(max(0.0, min(100.0, 50 + metrics.sharpe_ratio * 25)))
    else:
        notes.append("Consistency needs at least 2 closed trades with varying outcomes to score.")

    wins = [float(t.profit) for t in closed if float(t.profit) > 0]
    losses = [-float(t.profit) for t in closed if float(t.profit) < 0]
    strategy_score = None
    if closed:
        _, expectancy_r = expectancy(wins, losses)
        strategy_score = round(max(0.0, min(100.0, 50 + expectancy_r * 25)))

    risk_score = None
    if closed and starting_balance > 0:
        avg_risk_pct = statistics.mean(float(t.volume) * float(t.open_price) / starting_balance * 100 for t in closed)
        if avg_risk_pct <= 2:
            risk_score = 100
        elif avg_risk_pct <= 5:
            risk_score = 80
        elif avg_risk_pct <= 10:
            risk_score = 60
        elif avg_risk_pct <= 20:
            risk_score = 40
        else:
            risk_score = 20
        notes.append(
            f"Risk score approximates position size against current starting balance (avg {avg_risk_pct:.1f}%), "
            "not balance at the time of each trade."
        )
    else:
        notes.append("Risk score needs closed trades and a positive starting balance.")

    discipline_score = None
    if len(closed) >= MIN_TRADES_FOR_PROFILE:
        flagged_ids: set[int] = set()
        for mistake in mistakes:
            if mistake.status == "tracked":
                flagged_ids.update(mistake.example_trade_ids)
        discipline_score = round(max(0.0, (1 - len(flagged_ids) / len(closed))) * 100)
    else:
        notes.append(f"Discipline needs at least {MIN_TRADES_FOR_PROFILE} closed trades to score.")

    execution_score = None
    if rr.coverage_pct >= 20 and rr.avg_planned_rr and rr.avg_realized_rr is not None:
        capture_ratio = rr.avg_realized_rr / rr.avg_planned_rr if rr.avg_planned_rr else 0
        execution_score = round(max(0.0, min(100.0, capture_ratio * 100)))
    else:
        notes.append("Execution needs more trades with both a stop-loss and take-profit to score.")

    sub_scores = [consistency_score, strategy_score, risk_score, discipline_score, execution_score]
    available = [s for s in sub_scores if s is not None]
    overall_score = round(statistics.mean(available)) if available else None

    return TradingHealthResult(
        overall_score=overall_score,
        strategy_score=strategy_score,
        risk_score=risk_score,
        discipline_score=discipline_score,
        execution_score=execution_score,
        consistency_score=consistency_score,
        has_sufficient_data=overall_score is not None,
        notes=notes,
    )


def generate_instant_insight(profile: TradingProfile) -> str | None:
    tracked = [m for m in profile.mistakes if m.status == "tracked" and m.occurrences > 0]
    if tracked:
        top = max(tracked, key=lambda m: m.occurrences)
        return f"Your biggest opportunity right now: {top.description}"
    if profile.strongest_edge:
        return f"Keep leaning on your edge: {profile.strongest_edge}"
    return None


def compute_trading_profile(portfolio_id: int, trades: list[TradeLedger], starting_balance: float) -> TradingProfile:
    """
    Purpose:    Single source of truth behind Trading DNA, the Mistake
                Detector, and Trading Health — computed fresh from real
                closed trades on every call, never persisted, never
                fabricated. Degrades to an honest "not enough data yet"
                shape below MIN_TRADES_FOR_PROFILE closed trades.
    Args:       portfolio_id (int): The portfolio being analyzed.
                trades (list[TradeLedger]): All of the portfolio's trades
                    (open and closed), with `setup` eager-loaded.
                starting_balance (float): Portfolio's starting balance, used
                    as an approximate risk-sizing denominator.
    Returns:    TradingProfile: The full analytics snapshot.
    Raises:     None.
    """
    closed = [t for t in trades if t.status == TradeStatus.CLOSED and t.profit is not None]
    trades_analyzed = len(closed)
    has_sufficient_data = trades_analyzed >= MIN_TRADES_FOR_PROFILE
    now = datetime.now(timezone.utc)

    if not has_sufficient_data:
        hint = f"Log at least {MIN_TRADES_FOR_PROFILE} closed trades to unlock this ({trades_analyzed} so far)."
        mistakes = [_stop_loss_modification_placeholder()] + [
            _insufficient(category, hint) for category in _MISTAKE_CATEGORIES
        ]
        return TradingProfile(
            portfolio_id=portfolio_id,
            trades_analyzed=trades_analyzed,
            min_trades_required=MIN_TRADES_FOR_PROFILE,
            has_sufficient_data=False,
            best_symbol=None,
            worst_symbol=None,
            symbol_breakdown=[],
            best_hour_utc=None,
            worst_hour_utc=None,
            hourly_breakdown=[],
            avg_holding_minutes=None,
            median_holding_minutes=None,
            session_breakdown=[],
            risk_reward=RiskRewardStats(trades_with_both_sl_tp=0, coverage_pct=0.0, avg_planned_rr=None, avg_realized_rr=None, note=hint),
            setup_breakdown=[],
            setup_tagging_hint="Tag your trades with a setup to unlock setup-level performance breakdowns.",
            mistakes=mistakes,
            health=TradingHealthResult(
                overall_score=None,
                strategy_score=None,
                risk_score=None,
                discipline_score=None,
                execution_score=None,
                consistency_score=None,
                has_sufficient_data=False,
                notes=[hint],
            ),
            strongest_edge=None,
            biggest_weakness=None,
            best_trading_window=None,
            worst_trading_environment=None,
            risk_behavior_note=None,
            instant_insight=hint,
            timestamp_caveat=TIMESTAMP_CAVEAT,
            generated_at=now,
        )

    symbol_breakdown = _symbol_breakdown(closed)
    qualified_symbols = [s for s in symbol_breakdown if s.trade_count >= MIN_TRADES_PER_BUCKET]
    best_symbol = qualified_symbols[0] if qualified_symbols else None
    worst_symbol = qualified_symbols[-1] if len(qualified_symbols) > 1 else None

    hourly_breakdown = _hourly_breakdown(closed)
    qualified_hours = [h for h in hourly_breakdown if h.trade_count >= MIN_TRADES_PER_BUCKET]
    best_hour = max(qualified_hours, key=lambda h: h.avg_profit) if qualified_hours else None
    worst_hour = min(qualified_hours, key=lambda h: h.avg_profit) if len(qualified_hours) > 1 else None

    session_breakdown = _session_breakdown(closed)
    qualified_sessions = [s for s in session_breakdown if s.trade_count >= MIN_TRADES_PER_BUCKET]
    best_session = max(qualified_sessions, key=lambda s: s.avg_profit) if qualified_sessions else None
    worst_session = min(qualified_sessions, key=lambda s: s.avg_profit) if len(qualified_sessions) > 1 else None

    holding_minutes = [
        (t.close_time - t.open_time).total_seconds() / 60 for t in closed if t.close_time is not None
    ]

    risk_reward = _risk_reward_stats(closed)
    setup_breakdown = _setup_breakdown(closed)

    mistakes = [
        detect_overtrading(closed),
        detect_oversizing(closed),
        detect_poor_risk_reward(closed),
        detect_early_exits(closed),
        detect_revenge_trading(closed),
        detect_outside_preferred_hours(closed, hourly_breakdown),
        _stop_loss_modification_placeholder(),
    ]

    metrics = calculate_portfolio_metrics(trades, starting_balance)
    health = compute_trading_health(closed, metrics, risk_reward, mistakes, starting_balance)

    strongest_edge = None
    if setup_breakdown and setup_breakdown[0].total_profit > 0:
        top_setup = setup_breakdown[0]
        strongest_edge = f"{top_setup.setup_name} setups ({top_setup.win_rate_pct:.0f}% win rate over {top_setup.trade_count} trades)."
    elif best_symbol and best_symbol.total_profit > 0:
        strongest_edge = f"Trading {best_symbol.symbol} ({best_symbol.win_rate_pct:.0f}% win rate over {best_symbol.trade_count} trades)."

    top_mistake = max(
        (m for m in mistakes if m.status == "tracked" and m.occurrences > 0), key=lambda m: m.occurrences, default=None
    )
    biggest_weakness = top_mistake.description if top_mistake else None

    best_trading_window = None
    if best_hour is not None:
        best_trading_window = f"{best_hour.hour_utc:02d}:00-{(best_hour.hour_utc + 1) % 24:02d}:00 UTC"
    elif best_session is not None:
        best_trading_window = best_session.session.replace("_", " ").title()

    worst_trading_environment = None
    if worst_session is not None and worst_session.avg_profit < 0:
        worst_trading_environment = f"{worst_session.session.replace('_', ' ').title()} session trades."
    elif worst_hour is not None and worst_hour.avg_profit < 0:
        worst_trading_environment = f"Trades opened around {worst_hour.hour_utc:02d}:00 UTC."

    risk_behavior_note = None
    oversizing_flag = next((m for m in mistakes if m.category == "oversizing"), None)
    if oversizing_flag and oversizing_flag.status == "tracked" and oversizing_flag.occurrences > 0:
        risk_behavior_note = oversizing_flag.description
    elif risk_reward.avg_planned_rr is not None:
        risk_behavior_note = f"Average planned risk/reward across tagged trades: {risk_reward.avg_planned_rr:.2f}R."

    profile = TradingProfile(
        portfolio_id=portfolio_id,
        trades_analyzed=trades_analyzed,
        min_trades_required=MIN_TRADES_FOR_PROFILE,
        has_sufficient_data=True,
        best_symbol=best_symbol,
        worst_symbol=worst_symbol,
        symbol_breakdown=symbol_breakdown,
        best_hour_utc=best_hour,
        worst_hour_utc=worst_hour,
        hourly_breakdown=hourly_breakdown,
        avg_holding_minutes=round(statistics.mean(holding_minutes), 1) if holding_minutes else None,
        median_holding_minutes=round(statistics.median(holding_minutes), 1) if holding_minutes else None,
        session_breakdown=session_breakdown,
        risk_reward=risk_reward,
        setup_breakdown=setup_breakdown,
        setup_tagging_hint=None if setup_breakdown else "Tag your trades with a setup to unlock setup-level performance breakdowns.",
        mistakes=mistakes,
        health=health,
        strongest_edge=strongest_edge,
        biggest_weakness=biggest_weakness,
        best_trading_window=best_trading_window,
        worst_trading_environment=worst_trading_environment,
        risk_behavior_note=risk_behavior_note,
        instant_insight=None,
        timestamp_caveat=TIMESTAMP_CAVEAT,
        generated_at=now,
    )
    profile.instant_insight = generate_instant_insight(profile)
    return profile
