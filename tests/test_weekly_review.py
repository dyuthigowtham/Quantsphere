from datetime import datetime, timedelta

from app.models.database import Setup, TradeDirection, TradeLedger, TradeStatus
from app.services.weekly_review import MIN_TRADES_FOR_WEEKLY_REVIEW, compute_weekly_review

NOW = datetime(2026, 1, 15, 12, 0, 0)


def _trade(
    *,
    trade_id,
    symbol="EURUSD",
    profit=10.0,
    days_ago=1,
    stop_loss=None,
    setup=None,
    status=TradeStatus.CLOSED,
):
    open_time = NOW - timedelta(days=days_ago, hours=1)
    close_time = NOW - timedelta(days=days_ago)
    trade = TradeLedger(
        portfolio_id=1,
        symbol=symbol,
        direction=TradeDirection.BUY,
        volume=1.0,
        open_price=100.0,
        close_price=100.0 + profit,
        stop_loss=stop_loss,
        open_time=open_time,
        close_time=close_time if status == TradeStatus.CLOSED else None,
        profit=profit if status == TradeStatus.CLOSED else None,
        status=status,
    )
    trade.id = trade_id
    trade.setup = setup
    trade.setup_id = setup.id if setup else None
    return trade


def test_empty_portfolio_insufficient():
    result = compute_weekly_review(1, [], now=NOW)
    assert result.has_sufficient_data is False
    assert result.closed_trade_count == 0
    assert result.note is not None


def test_trades_outside_window_excluded():
    trades = [_trade(trade_id=1, days_ago=10), _trade(trade_id=2, days_ago=20)]
    result = compute_weekly_review(1, trades, now=NOW)
    assert result.closed_trade_count == 0


def test_trades_inside_window_counted():
    trades = [_trade(trade_id=i, days_ago=i) for i in range(1, 6)]
    result = compute_weekly_review(1, trades, now=NOW)
    assert result.closed_trade_count == 5


def test_below_minimum_shows_total_profit_but_no_ratios():
    trades = [_trade(trade_id=1, days_ago=1, profit=50.0)]
    result = compute_weekly_review(1, trades, now=NOW)
    assert result.has_sufficient_data is False
    assert result.closed_trade_count == 1
    assert result.total_profit == 50.0
    assert result.win_rate_pct is None
    assert result.best_trade is None


def test_best_and_worst_trade_identified():
    trades = [
        _trade(trade_id=1, days_ago=1, profit=50.0),
        _trade(trade_id=2, days_ago=2, profit=-30.0),
        _trade(trade_id=3, days_ago=3, profit=10.0),
    ]
    result = compute_weekly_review(1, trades, now=NOW)
    assert result.has_sufficient_data is True
    assert result.best_trade.trade_id == 1
    assert result.worst_trade.trade_id == 2


def test_open_trades_counted_separately():
    trades = [
        _trade(trade_id=1, days_ago=1, profit=10.0),
        _trade(trade_id=2, days_ago=1, profit=10.0),
        _trade(trade_id=3, days_ago=1, profit=10.0),
        _trade(trade_id=4, days_ago=1, status=TradeStatus.OPEN),
    ]
    result = compute_weekly_review(1, trades, now=NOW)
    assert result.closed_trade_count == 3
    assert result.open_trade_count == 1


def test_best_worst_setup_gated_on_min_trades_per_bucket():
    breakout = Setup(id=1, portfolio_id=1, name="Breakout")
    reversal = Setup(id=2, portfolio_id=1, name="Reversal")
    trades = [
        _trade(trade_id=1, days_ago=1, profit=10.0, setup=breakout),
        _trade(trade_id=2, days_ago=2, profit=20.0, setup=breakout),
        _trade(trade_id=3, days_ago=3, profit=30.0, setup=breakout),
        _trade(trade_id=4, days_ago=4, profit=-10.0, setup=reversal),  # only 1 trade -> not gated in
    ]
    result = compute_weekly_review(1, trades, now=NOW)
    assert result.best_setup_name == "Breakout"
    assert result.worst_setup_name is None  # reversal has too few trades to qualify


def test_min_trades_required_matches_constant():
    result = compute_weekly_review(1, [], now=NOW)
    assert result.min_trades_required == MIN_TRADES_FOR_WEEKLY_REVIEW
