from datetime import datetime, timedelta

from app.models.database import DecisionTrainingAttempt, TradeDirection, TradeLedger, TradeStatus
from app.services.trader_progression import (
    CURRENT_WINDOW_DAYS,
    DECISION_TRAINING_MILESTONES,
    JOURNALING_MIN_TRADES,
    TRADE_MILESTONES,
    TREND_MIN_TRADES,
    compute_progression,
)

NOW = datetime(2026, 1, 15, 12, 0, 0)


def _trade(*, trade_id, days_ago, profit=10.0, journal=None, status=TradeStatus.CLOSED):
    open_time = NOW - timedelta(days=days_ago, hours=1)
    close_time = NOW - timedelta(days=days_ago)
    trade = TradeLedger(
        portfolio_id=1,
        symbol="EURUSD",
        direction=TradeDirection.BUY,
        volume=1.0,
        open_price=100.0,
        close_price=100.0 + profit,
        open_time=open_time,
        close_time=close_time if status == TradeStatus.CLOSED else None,
        profit=profit if status == TradeStatus.CLOSED else None,
        status=status,
        backtest_journal=journal,
    )
    trade.id = trade_id
    return trade


def _attempt(attempt_id, days_ago):
    a = DecisionTrainingAttempt(
        portfolio_id=1,
        trade_id=1,
        symbol="EURUSD",
        interval="1h",
        range_="1mo",
        decision_candle_time=0,
        guess="buy",
        outcome="correct",
        price_move_pct=1.0,
        evaluated_after_bars=5,
    )
    a.id = attempt_id
    a.created_at = NOW - timedelta(days=days_ago)
    return a


def test_empty_portfolio_gates_everything():
    result = compute_progression(1, [], 10000.0, [], now=NOW)
    assert result.has_sufficient_trend_data is False
    assert result.trend_note is not None
    assert result.total_trades_closed == 0
    assert all(not m.achieved for m in result.trade_milestones)
    assert result.journaling.has_sufficient_data is False


def test_trend_gated_below_min_trades_in_either_window():
    # Only a handful of trades in the current window, none in baseline.
    trades = [_trade(trade_id=i, days_ago=i) for i in range(1, 4)]
    result = compute_progression(1, trades, 10000.0, [], now=NOW)
    assert result.current_window.has_sufficient_data is False
    assert result.has_sufficient_trend_data is False
    assert result.trend == []
    assert str(TREND_MIN_TRADES) in result.trend_note


def test_trend_computed_when_both_windows_sufficient():
    current_trades = [_trade(trade_id=i, days_ago=i, profit=10.0) for i in range(1, TREND_MIN_TRADES + 1)]
    baseline_trades = [
        _trade(trade_id=100 + i, days_ago=CURRENT_WINDOW_DAYS + i, profit=-10.0) for i in range(1, TREND_MIN_TRADES + 1)
    ]
    result = compute_progression(1, current_trades + baseline_trades, 10000.0, [], now=NOW)
    assert result.has_sufficient_trend_data is True
    assert result.trend_note is None
    win_rate_trend = next(t for t in result.trend if t.metric == "win_rate_pct")
    # current window is all wins, baseline all losses -> clearly improving.
    assert win_rate_trend.direction == "improving"
    assert win_rate_trend.current_value == 100.0
    assert win_rate_trend.baseline_value == 0.0


def test_trade_milestone_achieved_at_is_the_real_nth_trade_timestamp():
    trades = [_trade(trade_id=i, days_ago=TRADE_MILESTONES[0] - i) for i in range(TRADE_MILESTONES[0])]
    result = compute_progression(1, trades, 10000.0, [], now=NOW)
    milestone = next(m for m in result.trade_milestones if m.threshold == TRADE_MILESTONES[0])
    assert milestone.achieved is True
    sorted_close_times = sorted(t.close_time for t in trades)
    assert milestone.achieved_at == sorted_close_times[TRADE_MILESTONES[0] - 1]


def test_trade_milestone_not_achieved_below_threshold():
    trades = [_trade(trade_id=i, days_ago=i) for i in range(1, 3)]
    result = compute_progression(1, trades, 10000.0, [], now=NOW)
    milestone = next(m for m in result.trade_milestones if m.threshold == TRADE_MILESTONES[0])
    assert milestone.achieved is False
    assert milestone.achieved_at is None


def test_decision_training_milestone_uses_real_attempt_timestamp():
    attempts = [_attempt(i, days_ago=DECISION_TRAINING_MILESTONES[0] - i) for i in range(DECISION_TRAINING_MILESTONES[0])]
    result = compute_progression(1, [], 10000.0, attempts, now=NOW)
    milestone = next(m for m in result.decision_training_milestones if m.threshold == DECISION_TRAINING_MILESTONES[0])
    assert milestone.achieved is True
    sorted_created = sorted(a.created_at for a in attempts)
    assert milestone.achieved_at == sorted_created[DECISION_TRAINING_MILESTONES[0] - 1]


def test_journaling_coverage_counts_only_real_content():
    trades = [
        _trade(trade_id=1, days_ago=1, journal={"what_worked": "waited for confirmation"}),
        _trade(trade_id=2, days_ago=2, journal={"what_worked": "", "notes": None}),  # no real content
        _trade(trade_id=3, days_ago=3, journal=None),
    ] + [_trade(trade_id=10 + i, days_ago=10 + i) for i in range(JOURNALING_MIN_TRADES - 3)]
    result = compute_progression(1, trades, 10000.0, [], now=NOW)
    assert result.journaling.has_sufficient_data is True
    assert result.journaling.trades_journaled == 1
    assert result.journaling.trades_closed == JOURNALING_MIN_TRADES


def test_open_trades_excluded_from_all_windows():
    trades = [_trade(trade_id=1, days_ago=1, status=TradeStatus.OPEN)]
    result = compute_progression(1, trades, 10000.0, [], now=NOW)
    assert result.total_trades_closed == 0
    assert result.current_window.trades_closed == 0
