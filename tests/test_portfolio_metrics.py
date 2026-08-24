from dataclasses import dataclass
from datetime import datetime

from app.models.database import TradeStatus
from app.services.execution import calculate_portfolio_metrics


@dataclass
class FakeTrade:
    status: TradeStatus
    profit: float | None
    close_time: datetime | None = None


def test_metrics_with_no_closed_trades_returns_none_sharpe() -> None:
    result = calculate_portfolio_metrics([], starting_balance=10000)
    assert result.trade_count == 0
    assert result.sharpe_ratio is None
    assert result.win_rate_pct is None
    assert result.max_drawdown_amount == 0.0


def test_metrics_ignore_open_trades() -> None:
    trades = [FakeTrade(TradeStatus.OPEN, None, datetime(2026, 1, 1))]
    result = calculate_portfolio_metrics(trades, starting_balance=10000)
    assert result.trade_count == 0


def test_win_rate_and_sharpe_with_mixed_results() -> None:
    trades = [
        FakeTrade(TradeStatus.CLOSED, 100, datetime(2026, 1, 1)),
        FakeTrade(TradeStatus.CLOSED, -50, datetime(2026, 1, 2)),
        FakeTrade(TradeStatus.CLOSED, 200, datetime(2026, 1, 3)),
        FakeTrade(TradeStatus.CLOSED, -20, datetime(2026, 1, 4)),
    ]
    result = calculate_portfolio_metrics(trades, starting_balance=10000)
    assert result.trade_count == 4
    assert result.win_rate_pct == 50.0
    assert result.sharpe_ratio is not None


def test_sharpe_is_none_with_single_closed_trade() -> None:
    trades = [FakeTrade(TradeStatus.CLOSED, 100, datetime(2026, 1, 1))]
    result = calculate_portfolio_metrics(trades, starting_balance=10000)
    assert result.sharpe_ratio is None
    assert result.win_rate_pct == 100.0


def test_max_drawdown_tracks_peak_to_trough_decline() -> None:
    # Equity curve: 100 -> 300 -> 100 -> 250. Peak 300, trough 100 => drawdown 200.
    trades = [
        FakeTrade(TradeStatus.CLOSED, 100, datetime(2026, 1, 1)),
        FakeTrade(TradeStatus.CLOSED, 200, datetime(2026, 1, 2)),
        FakeTrade(TradeStatus.CLOSED, -200, datetime(2026, 1, 3)),
        FakeTrade(TradeStatus.CLOSED, 150, datetime(2026, 1, 4)),
    ]
    result = calculate_portfolio_metrics(trades, starting_balance=1000)
    assert result.max_drawdown_amount == 200.0
    assert result.max_drawdown_pct == 20.0


def test_max_drawdown_zero_when_always_climbing() -> None:
    trades = [
        FakeTrade(TradeStatus.CLOSED, 50, datetime(2026, 1, 1)),
        FakeTrade(TradeStatus.CLOSED, 75, datetime(2026, 1, 2)),
    ]
    result = calculate_portfolio_metrics(trades, starting_balance=1000)
    assert result.max_drawdown_amount == 0.0
