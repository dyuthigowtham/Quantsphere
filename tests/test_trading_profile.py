from datetime import datetime, timedelta

from app.models.database import Setup, TradeDirection, TradeLedger, TradeStatus
from app.services.trading_profile import (
    MIN_TRADES_FOR_PROFILE,
    compute_trading_profile,
    detect_early_exits,
    detect_oversizing,
    detect_overtrading,
    detect_poor_risk_reward,
    detect_revenge_trading,
)

BASE_DAY = datetime(2026, 1, 5, 0, 0, 0)  # a Monday, arbitrary anchor


def _trade(
    *,
    symbol="EURUSD",
    direction=TradeDirection.BUY,
    volume=1.0,
    open_price=100.0,
    close_price=101.0,
    stop_loss=None,
    take_profit=None,
    profit=10.0,
    day_offset=0,
    open_hour=9,
    holding_minutes=30,
    setup=None,
    trade_id=None,
):
    open_time = BASE_DAY + timedelta(days=day_offset, hours=open_hour)
    trade = TradeLedger(
        portfolio_id=1,
        symbol=symbol,
        direction=direction,
        volume=volume,
        open_price=open_price,
        close_price=close_price,
        stop_loss=stop_loss,
        take_profit=take_profit,
        open_time=open_time,
        close_time=open_time + timedelta(minutes=holding_minutes),
        profit=profit,
        status=TradeStatus.CLOSED,
    )
    trade.id = trade_id
    trade.setup = setup
    trade.setup_id = setup.id if setup else None
    return trade


def _many_baseline_trades(count: int, **overrides) -> list[TradeLedger]:
    """A run of unremarkable, evenly-spread trades used as filler to reach MIN_TRADES_FOR_PROFILE."""
    trades = []
    for i in range(count):
        kwargs = {"day_offset": i, "trade_id": 1000 + i}
        kwargs.update(overrides)
        trades.append(_trade(**kwargs))
    return trades


def test_compute_trading_profile_insufficient_data_is_honest_not_fabricated() -> None:
    trades = _many_baseline_trades(MIN_TRADES_FOR_PROFILE - 1)
    profile = compute_trading_profile(portfolio_id=1, trades=trades, starting_balance=10_000)

    assert profile.has_sufficient_data is False
    assert profile.trades_analyzed == MIN_TRADES_FOR_PROFILE - 1
    assert profile.best_symbol is None
    assert profile.symbol_breakdown == []
    assert profile.health.has_sufficient_data is False
    assert profile.health.overall_score is None
    for mistake in profile.mistakes:
        assert mistake.status in ("insufficient_data", "not_yet_trackable")
        assert mistake.occurrences == 0


def test_compute_trading_profile_empty_portfolio() -> None:
    profile = compute_trading_profile(portfolio_id=1, trades=[], starting_balance=10_000)
    assert profile.has_sufficient_data is False
    assert profile.trades_analyzed == 0


def test_risk_reward_zero_coverage_degrades_gracefully() -> None:
    # Plenty of trades, but none carry a stop-loss/take-profit (the common
    # case for MT5-synced trades) — coverage should read 0%, not fabricate an R:R.
    trades = _many_baseline_trades(MIN_TRADES_FOR_PROFILE + 2, stop_loss=None, take_profit=None)
    profile = compute_trading_profile(portfolio_id=1, trades=trades, starting_balance=10_000)

    assert profile.has_sufficient_data is True
    assert profile.risk_reward.trades_with_both_sl_tp == 0
    assert profile.risk_reward.coverage_pct == 0.0
    assert profile.risk_reward.avg_planned_rr is None
    assert profile.risk_reward.avg_realized_rr is None
    assert profile.risk_reward.note is not None
    # Execution sub-score needs R:R coverage — must stay unscored, not guessed.
    assert profile.health.execution_score is None


def test_symbol_breakdown_reflects_real_per_symbol_profit() -> None:
    trades = [
        _trade(symbol="EURUSD", profit=50.0, day_offset=0, trade_id=1),
        _trade(symbol="EURUSD", profit=30.0, day_offset=1, trade_id=2),
        _trade(symbol="EURUSD", profit=20.0, day_offset=2, trade_id=3),
        _trade(symbol="GBPUSD", profit=-40.0, day_offset=3, trade_id=4),
        _trade(symbol="GBPUSD", profit=-10.0, day_offset=4, trade_id=5),
        _trade(symbol="GBPUSD", profit=-5.0, day_offset=5, trade_id=6),
    ] + _many_baseline_trades(MIN_TRADES_FOR_PROFILE - 6, day_offset=10, symbol="USDJPY")

    profile = compute_trading_profile(portfolio_id=1, trades=trades, starting_balance=10_000)
    symbols = {s.symbol: s for s in profile.symbol_breakdown}
    assert symbols["EURUSD"].total_profit == 100.0
    assert symbols["GBPUSD"].total_profit == -55.0
    assert profile.best_symbol.symbol == "EURUSD"
    assert profile.worst_symbol.symbol == "GBPUSD"


def test_setup_breakdown_only_includes_tagged_trades() -> None:
    breakout = Setup(id=1, portfolio_id=1, name="Breakout")
    trades = [
        _trade(profit=50.0, day_offset=0, trade_id=1, setup=breakout),
        _trade(profit=30.0, day_offset=1, trade_id=2, setup=breakout),
        _trade(profit=-10.0, day_offset=2, trade_id=3, setup=None),
    ] + _many_baseline_trades(MIN_TRADES_FOR_PROFILE - 3, day_offset=10)

    profile = compute_trading_profile(portfolio_id=1, trades=trades, starting_balance=10_000)
    assert len(profile.setup_breakdown) == 1
    assert profile.setup_breakdown[0].setup_name == "Breakout"
    assert profile.setup_breakdown[0].trade_count == 2
    assert profile.setup_breakdown[0].total_profit == 80.0


def test_detect_overtrading_flags_outlier_day() -> None:
    quiet_days = _many_baseline_trades(12, day_offset=0)  # will be overwritten below
    trades = []
    for day in range(12):
        trades.append(_trade(day_offset=day, trade_id=day, open_hour=9))
    # Day 12: a burst of 6 trades, far above the ~1/day baseline.
    burst_day = 12
    for i in range(6):
        trades.append(_trade(day_offset=burst_day, open_hour=9 + i, trade_id=100 + i))

    flag = detect_overtrading([t for t in trades])
    assert flag.status == "tracked"
    assert flag.occurrences >= 1
    assert flag.severity is not None


def test_detect_oversizing_flags_outlier_volume() -> None:
    trades = _many_baseline_trades(10, volume=1.0)
    trades.append(_trade(volume=50.0, day_offset=20, trade_id=999))
    flag = detect_oversizing(trades)
    assert flag.status == "tracked"
    assert any(t.id == 999 for t in trades if t.id in flag.example_trade_ids)


def test_detect_poor_risk_reward_flags_bad_ratio() -> None:
    trades = [
        _trade(open_price=100, stop_loss=90, take_profit=105, trade_id=1, day_offset=0),  # 0.5R, poor
        _trade(open_price=100, stop_loss=95, take_profit=120, trade_id=2, day_offset=1),  # 4R, fine
        _trade(open_price=100, stop_loss=90, take_profit=102, trade_id=3, day_offset=2),  # 0.2R, poor
    ]
    flag = detect_poor_risk_reward(trades)
    assert flag.status == "tracked"
    assert flag.occurrences == 2
    assert 1 in flag.example_trade_ids
    assert 3 in flag.example_trade_ids
    assert 2 not in flag.example_trade_ids


def test_detect_early_exits_flags_trades_closed_well_short_of_target() -> None:
    trades = [
        _trade(open_price=100, close_price=102, take_profit=120, trade_id=1, day_offset=0),  # 10% captured
        _trade(open_price=100, close_price=119, take_profit=120, trade_id=2, day_offset=1),  # 95% captured
        _trade(open_price=100, close_price=101, take_profit=120, trade_id=3, day_offset=2),  # 5% captured
    ]
    flag = detect_early_exits(trades)
    assert flag.status == "tracked"
    assert 1 in flag.example_trade_ids
    assert 3 in flag.example_trade_ids
    assert 2 not in flag.example_trade_ids


def test_detect_revenge_trading_flags_quick_larger_reentry_after_loss() -> None:
    loss_open = BASE_DAY + timedelta(hours=9)
    loss = TradeLedger(
        portfolio_id=1,
        symbol="EURUSD",
        direction=TradeDirection.BUY,
        volume=1.0,
        open_price=100,
        close_price=95,
        open_time=loss_open,
        close_time=loss_open + timedelta(minutes=20),
        profit=-50.0,
        status=TradeStatus.CLOSED,
    )
    loss.id = 1
    loss.setup = None

    revenge_open = loss.close_time + timedelta(minutes=10)
    revenge = TradeLedger(
        portfolio_id=1,
        symbol="EURUSD",
        direction=TradeDirection.BUY,
        volume=3.0,
        open_price=95,
        close_price=94,
        open_time=revenge_open,
        close_time=revenge_open + timedelta(minutes=15),
        profit=-30.0,
        status=TradeStatus.CLOSED,
    )
    revenge.id = 2
    revenge.setup = None

    filler = _many_baseline_trades(6, day_offset=5)
    flag = detect_revenge_trading([loss, revenge, *filler])
    assert flag.status == "tracked"
    assert 2 in flag.example_trade_ids
    assert 1 not in flag.example_trade_ids


def test_stop_loss_modification_is_always_not_yet_trackable() -> None:
    trades = _many_baseline_trades(MIN_TRADES_FOR_PROFILE)
    profile = compute_trading_profile(portfolio_id=1, trades=trades, starting_balance=10_000)
    sl_mod = next(m for m in profile.mistakes if m.category == "stop_loss_modification")
    assert sl_mod.status == "not_yet_trackable"
    assert sl_mod.occurrences == 0
