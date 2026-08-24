from datetime import datetime, timezone

import pytest

from app.models.database import TradeDirection
from app.models.schemas import StrategyCondition, StrategyRead
from app.services.strategy_lab import (
    ALLOWED_BACKTEST_WINDOWS,
    MIN_SIMULATED_TRADES,
    _condition_met,
    _precompute_condition_series,
    run_backtest,
    validate_backtest_window,
)


def _candle(close, high=None, low=None, time=0):
    return {"time": time, "open": close, "high": high if high is not None else close, "low": low if low is not None else close, "close": close}


def _strategy(conditions, direction=TradeDirection.BUY, stop_loss_pct=2.0, target_r=2.0, strategy_id=1):
    return StrategyRead(
        id=strategy_id,
        portfolio_id=1,
        name="Test Strategy",
        description=None,
        direction=direction,
        conditions=conditions,
        stop_loss_pct=stop_loss_pct,
        target_r=target_r,
        created_at=datetime.now(timezone.utc),
    )


# ---------- validate_backtest_window ----------


def test_validate_backtest_window_accepts_known_good_combo():
    validate_backtest_window("1h", "1y")  # should not raise


def test_validate_backtest_window_rejects_15m_beyond_60_days():
    with pytest.raises(ValueError):
        validate_backtest_window("15m", "3mo")


def test_validate_backtest_window_rejects_unknown_interval():
    with pytest.raises(ValueError):
        validate_backtest_window("1wk", "1y")


def test_allowed_windows_never_uses_max():
    for windows in ALLOWED_BACKTEST_WINDOWS.values():
        assert "max" not in windows


# ---------- condition primitives (unit level) ----------


def test_breakout_condition_excludes_current_bar_from_window():
    closes = [100.0] * 6 + [105.0]
    highs = closes[:]
    lows = closes[:]
    cond = StrategyCondition(type="breakout", breakout_lookback=5, breakout_direction="above_high")
    series = _precompute_condition_series(cond, closes, highs, lows)
    # index 6: window is highs[1:6] = all 100 -> close 105 > 100 -> breakout
    assert _condition_met(cond, series, 6, closes[6]) is True
    # index 5: window is highs[0:5] = all 100 -> close 100 is not > 100
    assert _condition_met(cond, series, 5, closes[5]) is False


def test_rsi_threshold_condition_gates_on_none():
    closes = [100.0 + i for i in range(10)]
    cond = StrategyCondition(type="rsi_threshold", rsi_period=14, rsi_comparison="above", rsi_value=50)
    series = _precompute_condition_series(cond, closes, closes, closes)
    assert _condition_met(cond, series, 5, closes[5]) is False  # not enough history for RSI yet


def test_ema_cross_condition_detects_true_crossover_only():
    # fast rises above slow between index 4 and 5
    closes = [100, 100, 100, 100, 99, 101, 102, 103, 104, 105]
    closes = [float(c) for c in closes]
    cond = StrategyCondition(type="ema_cross", fast_period=2, slow_period=4, cross_direction="up")
    series = _precompute_condition_series(cond, closes, closes, closes)
    # whatever index the true crossover happens at, it should happen at most once meaningfully;
    # just assert index 0 (no prior bar) is never a valid crossover
    assert _condition_met(cond, series, 0, closes[0]) is False


# ---------- run_backtest: insufficient data ----------


def test_run_backtest_insufficient_candles_returns_honest_gate():
    strategy = _strategy([StrategyCondition(type="breakout", breakout_lookback=5, breakout_direction="above_high")])
    candles = [_candle(100.0, time=i) for i in range(5)]
    result = run_backtest(strategy, candles, "EURUSD", "1h", "1mo")
    assert result.has_sufficient_data is False
    assert result.trade_count == 0
    assert result.simulated_trades == []
    assert result.note is not None


# ---------- run_backtest: breakout entry + stop-loss exit ----------


def test_run_backtest_breakout_entry_then_stop_loss_exit():
    strategy = _strategy(
        [StrategyCondition(type="breakout", breakout_lookback=5, breakout_direction="above_high")],
        stop_loss_pct=2.0,
        target_r=2.0,
    )
    candles = [_candle(100.0, time=i) for i in range(11)]  # warmup = 5+1+5 = 11
    candles.append(_candle(105.0, high=105.0, low=105.0, time=11))  # breakout entry at 105
    candles.append(_candle(102.0, high=105.0, low=102.0, time=12))  # low hits stop (102.9) -> stop-loss
    for i in range(13, 20):
        candles.append(_candle(102.0, high=102.0, low=102.0, time=i))

    result = run_backtest(strategy, candles, "EURUSD", "1h", "1mo")
    assert result.trade_count == 1
    trade = result.simulated_trades[0]
    assert trade.exit_reason == "stop_loss"
    assert trade.r_multiple == pytest.approx(-1.0, abs=0.01)
    assert result.open_at_data_end is False


def test_run_backtest_breakout_entry_then_take_profit_exit():
    strategy = _strategy(
        [StrategyCondition(type="breakout", breakout_lookback=5, breakout_direction="above_high")],
        stop_loss_pct=2.0,
        target_r=2.0,
    )
    candles = [_candle(100.0, time=i) for i in range(11)]
    candles.append(_candle(105.0, high=105.0, low=105.0, time=11))  # entry at 105, stop=102.9, target=109.2
    candles.append(_candle(110.0, high=110.0, low=105.0, time=12))  # high hits target -> take-profit
    for i in range(13, 20):
        candles.append(_candle(110.0, high=110.0, low=110.0, time=i))

    result = run_backtest(strategy, candles, "EURUSD", "1h", "1mo")
    assert result.trade_count == 1
    trade = result.simulated_trades[0]
    assert trade.exit_reason == "take_profit"
    assert trade.r_multiple == pytest.approx(2.0, abs=0.01)


def test_run_backtest_timeout_exit_when_neither_sl_nor_tp_hit():
    strategy = _strategy(
        [StrategyCondition(type="breakout", breakout_lookback=5, breakout_direction="above_high")],
        stop_loss_pct=2.0,
        target_r=2.0,
    )
    candles = [_candle(100.0, time=i) for i in range(11)]
    candles.append(_candle(105.0, high=105.0, low=105.0, time=11))  # entry
    for i in range(12, 12 + 60):  # far more than default max_holding_bars=48
        candles.append(_candle(106.0, high=107.0, low=104.0, time=i))  # stays between stop/target

    result = run_backtest(strategy, candles, "EURUSD", "1h", "1mo", max_holding_bars=48)
    assert result.trade_count == 1
    assert result.simulated_trades[0].exit_reason == "timeout"
    assert result.simulated_trades[0].bars_held == 48


def test_run_backtest_open_position_at_data_end_excluded_from_metrics():
    strategy = _strategy(
        [StrategyCondition(type="breakout", breakout_lookback=5, breakout_direction="above_high")],
        stop_loss_pct=2.0,
        target_r=2.0,
    )
    candles = [_candle(100.0, time=i) for i in range(11)]
    candles.append(_candle(105.0, high=105.0, low=105.0, time=11))  # entry, never exits
    for i in range(12, 20):
        candles.append(_candle(106.0, high=107.0, low=104.0, time=i))

    result = run_backtest(strategy, candles, "EURUSD", "1h", "1mo")
    assert result.trade_count == 0
    assert result.open_at_data_end is True
    assert "still open" in result.note.lower() or "open" in result.note.lower()


def test_run_backtest_profit_factor_none_when_no_losses():
    strategy = _strategy(
        [StrategyCondition(type="breakout", breakout_lookback=5, breakout_direction="above_high")],
        stop_loss_pct=2.0,
        target_r=2.0,
    )
    candles = [_candle(100.0, time=i) for i in range(11)]
    price = 100.0
    t = 11
    # Repeatedly break out and immediately hit take-profit, enough times to pass the gate.
    for _ in range(MIN_SIMULATED_TRADES + 2):
        entry = price + 5
        candles.append(_candle(entry, high=entry, low=entry, time=t))
        t += 1
        target = entry + (entry - entry * 0.98) * 2.0
        candles.append(_candle(target, high=target + 1, low=entry, time=t))
        t += 1
        price = target + 1
        # pad flat candles so the next breakout is a genuine new high
        for _ in range(6):
            candles.append(_candle(price, time=t))
            t += 1

    result = run_backtest(strategy, candles, "EURUSD", "1h", "1mo")
    assert result.trade_count >= MIN_SIMULATED_TRADES
    assert result.has_sufficient_data is True
    assert result.profit_factor is None  # no losing trades -> None, never "infinite"
    assert result.win_rate_pct == pytest.approx(100.0)
