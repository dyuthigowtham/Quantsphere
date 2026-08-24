from datetime import datetime, timedelta, timezone

from app.models.database import DecisionTrainingAttempt, TradeLedger, TradeStatus
from app.models.schemas import (
    JournalingCoverage,
    ProgressionMilestone,
    ProgressionTrend,
    ProgressionWindow,
    TraderProgressionResult,
    TradingProfile,
)
from app.services import trading_profile
from app.services.trade_stats import win_rate_pct

# Reuse Trading DNA's own minimum-trade bar rather than inventing a second
# "enough data" threshold for the same underlying signal.
CURRENT_WINDOW_DAYS = 30
BASELINE_WINDOW_DAYS = 90
TREND_MIN_TRADES = trading_profile.MIN_TRADES_FOR_PROFILE

# Small dead-zones below which a trend is called "flat" rather than
# "improving"/"declining" — avoids reading noise as a real trend on numbers
# that naturally jitter month to month.
WIN_RATE_DEAD_ZONE_PP = 2.0
SCORE_DEAD_ZONE_POINTS = 3.0

TRADE_MILESTONES = [10, 25, 50, 100, 250, 500]
# 15 matches decision_training.MIN_ATTEMPTS_FOR_ACCURACY — the same bar at
# which Decision Training itself starts trusting an accuracy percentage.
DECISION_TRAINING_MILESTONES = [15, 50, 100]
JOURNALING_MIN_TRADES = trading_profile.MIN_TRADES_FOR_PROFILE


def _closed(trades: list[TradeLedger]) -> list[TradeLedger]:
    return [t for t in trades if t.status == TradeStatus.CLOSED and t.profit is not None]


def _window(trades: list[TradeLedger], start: datetime, end: datetime) -> list[TradeLedger]:
    return [t for t in trades if t.close_time is not None and start <= t.close_time <= end]


def _compute_window(
    label: str, portfolio_id: int, window_trades: list[TradeLedger], starting_balance: float, start: datetime, end: datetime
) -> tuple[ProgressionWindow, TradingProfile]:
    profile = trading_profile.compute_trading_profile(portfolio_id, window_trades, starting_balance)
    trades_closed = len(window_trades)
    has_sufficient_data = trades_closed >= TREND_MIN_TRADES
    window = ProgressionWindow(
        label=label,
        window_start=start,
        window_end=end,
        trades_closed=trades_closed,
        min_trades_required=TREND_MIN_TRADES,
        has_sufficient_data=has_sufficient_data,
        win_rate_pct=win_rate_pct(window_trades) if trades_closed > 0 else None,
        overall_health_score=profile.health.overall_score if has_sufficient_data else None,
        discipline_score=profile.health.discipline_score if has_sufficient_data else None,
    )
    return window, profile


def _trend_for(metric: str, current: float | None, baseline: float | None, dead_zone: float) -> ProgressionTrend:
    delta = None
    direction = None
    if current is not None and baseline is not None:
        delta = round(current - baseline, 1)
        if abs(delta) < dead_zone:
            direction = "flat"
        else:
            direction = "improving" if delta > 0 else "declining"
    return ProgressionTrend(metric=metric, current_value=current, baseline_value=baseline, delta=delta, direction=direction)


def _milestones(sorted_timestamps: list[datetime], thresholds: list[int], label_fmt: str) -> list[ProgressionMilestone]:
    total = len(sorted_timestamps)
    milestones = []
    for threshold in thresholds:
        achieved = total >= threshold
        achieved_at = sorted_timestamps[threshold - 1] if achieved else None
        milestones.append(
            ProgressionMilestone(
                key=f"{label_fmt}_{threshold}",
                label=label_fmt.format(threshold),
                threshold=threshold,
                achieved=achieved,
                achieved_at=achieved_at,
            )
        )
    return milestones


def _has_real_journal_content(journal: dict | None) -> bool:
    if not journal:
        return False
    return any(bool(v) for v in journal.values())


def compute_progression(
    portfolio_id: int,
    trades: list[TradeLedger],
    starting_balance: float,
    decision_attempts: list[DecisionTrainingAttempt],
    now: datetime | None = None,
) -> TraderProgressionResult:
    """
    Purpose:    Honest trader-progression signal built entirely from data
                that already exists — a current-vs-baseline trend (reusing
                compute_trading_profile on two real time windows, never
                duplicating its scoring math), real milestone-achieved
                dates (the actual timestamp of the Nth trade/attempt, never
                now()), and real journaling coverage. No new persisted
                history/snapshot table — every number here is recomputed
                fresh from existing rows, same philosophy as
                compute_trading_profile itself.
    Args:       portfolio_id (int): The portfolio being analyzed.
                trades (list[TradeLedger]): All of the portfolio's trades
                    (open and closed).
                starting_balance (float): Portfolio's starting balance.
                decision_attempts (list[DecisionTrainingAttempt]): All of
                    the portfolio's persisted Decision Training attempts.
                now (datetime | None): Reference "now"; defaults to the
                    real current UTC time. Exposed for deterministic testing.
    Returns:    TraderProgressionResult: Honestly gated on real data.
    Raises:     None.
    """
    reference_now = now if now is not None else datetime.now(timezone.utc)
    now_naive = reference_now.replace(tzinfo=None) if reference_now.tzinfo else reference_now

    current_start = now_naive - timedelta(days=CURRENT_WINDOW_DAYS)
    baseline_start = current_start - timedelta(days=BASELINE_WINDOW_DAYS)

    closed = _closed(trades)
    current_trades = _window(closed, current_start, now_naive)
    baseline_trades = _window(closed, baseline_start, current_start)

    current_window, current_profile = _compute_window(
        "current", portfolio_id, current_trades, starting_balance, current_start, now_naive
    )
    baseline_window, _ = _compute_window(
        "baseline", portfolio_id, baseline_trades, starting_balance, baseline_start, current_start
    )

    has_sufficient_trend_data = current_window.has_sufficient_data and baseline_window.has_sufficient_data
    trend = []
    trend_note = None
    if has_sufficient_trend_data:
        trend = [
            _trend_for("win_rate_pct", current_window.win_rate_pct, baseline_window.win_rate_pct, WIN_RATE_DEAD_ZONE_PP),
            _trend_for(
                "overall_health_score",
                current_window.overall_health_score,
                baseline_window.overall_health_score,
                SCORE_DEAD_ZONE_POINTS,
            ),
            _trend_for(
                "discipline_score",
                current_window.discipline_score,
                baseline_window.discipline_score,
                SCORE_DEAD_ZONE_POINTS,
            ),
        ]
    else:
        trend_note = (
            f"Need at least {TREND_MIN_TRADES} closed trades in both the last {CURRENT_WINDOW_DAYS} days "
            f"and the {BASELINE_WINDOW_DAYS} days before that to show a trend "
            f"({current_window.trades_closed} / {baseline_window.trades_closed} so far)."
        )

    all_closed_sorted = sorted((t.close_time for t in closed if t.close_time is not None))
    trade_milestones = _milestones(all_closed_sorted, TRADE_MILESTONES, "{} closed trades")

    attempt_timestamps_sorted = sorted(a.created_at for a in decision_attempts)
    decision_training_milestones = _milestones(
        attempt_timestamps_sorted, DECISION_TRAINING_MILESTONES, "{} Decision Training attempts"
    )

    journaled_count = sum(1 for t in closed if _has_real_journal_content(t.backtest_journal))
    journaling_has_sufficient_data = len(closed) >= JOURNALING_MIN_TRADES
    journaling = JournalingCoverage(
        trades_closed=len(closed),
        trades_journaled=journaled_count,
        journaled_pct=round(journaled_count / len(closed) * 100, 1) if journaling_has_sufficient_data else None,
        has_sufficient_data=journaling_has_sufficient_data,
        note=None
        if journaling_has_sufficient_data
        else f"Log at least {JOURNALING_MIN_TRADES} closed trades to unlock journaling coverage ({len(closed)} so far).",
    )

    return TraderProgressionResult(
        portfolio_id=portfolio_id,
        current_window=current_window,
        baseline_window=baseline_window,
        trend=trend,
        has_sufficient_trend_data=has_sufficient_trend_data,
        trend_note=trend_note,
        total_trades_closed=len(closed),
        trade_milestones=trade_milestones,
        decision_training_milestones=decision_training_milestones,
        journaling=journaling,
        generated_at=reference_now,
    )
