from datetime import datetime, timezone

from app.ai.rules_engine import ema_series, rsi_series
from app.models.schemas import (
    BacktestEquityPoint,
    BacktestResult,
    SimulatedTrade,
    StrategyCondition,
    StrategyRead,
)
from app.services.trade_stats import expectancy

# Confirmed live against Yahoo's real chart-endpoint limits (see
# app/services/market_data.py::fetch_yahoo_candles's docstring caveat and
# the Phase 3 plan for the exact probe results): 5m/15m cap at 60 days,
# 1h caps at 730 days, 1d works for years. range_="max" is unreliable and
# deliberately excluded — always pass one of these explicit ranges.
ALLOWED_BACKTEST_WINDOWS: dict[str, list[str]] = {
    "15m": ["5d", "1mo"],
    "1h": ["1mo", "3mo", "6mo", "1y", "2y"],
    "1d": ["3mo", "6mo", "1y", "2y", "5y", "10y"],
}

# Below this many simulated trades, win rate/profit factor/expectancy/max
# drawdown are too noisy to present as reliable — the raw simulated-trade
# list and equity curve are still shown in full either way.
MIN_SIMULATED_TRADES = 20
WARMUP_BUFFER_BARS = 5


def validate_backtest_window(interval: str, range_: str) -> None:
    """
    Purpose:    Reject any (interval, range) combination outside Yahoo's real,
                confirmed lookback limits before a backtest wastes a network
                call and returns a silently-truncated series.
    Args:       interval (str): Candle interval, e.g. "1h".
                range_ (str): Candle range, e.g. "1y".
    Returns:    None.
    Raises:     ValueError: If the combination isn't in ALLOWED_BACKTEST_WINDOWS.
    """
    allowed = ALLOWED_BACKTEST_WINDOWS.get(interval)
    if not allowed or range_ not in allowed:
        raise ValueError(
            f"interval={interval!r} range={range_!r} is not an allowed backtest window. "
            f"Allowed windows: {ALLOWED_BACKTEST_WINDOWS}"
        )


def _condition_warmup(condition: StrategyCondition) -> int:
    """Minimum candle count needed before this condition can be evaluated at all."""
    if condition.type == "ema_cross":
        return condition.slow_period + 1
    if condition.type == "rsi_threshold":
        return condition.rsi_period + 1
    if condition.type == "breakout":
        return condition.breakout_lookback + 1
    raise ValueError(f"Unknown condition type: {condition.type!r}")


def _precompute_condition_series(
    condition: StrategyCondition, closes: list[float], highs: list[float], lows: list[float]
) -> dict:
    """Precompute, once over the whole series, whatever this condition needs at each index."""
    if condition.type == "ema_cross":
        return {"fast": ema_series(closes, condition.fast_period), "slow": ema_series(closes, condition.slow_period)}
    if condition.type == "rsi_threshold":
        return {"rsi": rsi_series(closes, condition.rsi_period)}
    if condition.type == "breakout":
        lookback = condition.breakout_lookback
        n = len(closes)
        highest: list[float | None] = [None] * n
        lowest: list[float | None] = [None] * n
        for i in range(lookback, n):
            # Window excludes bar i itself — comparing a bar's own high/low to
            # a window that includes it would make "new high" trivially true.
            highest[i] = max(highs[i - lookback : i])
            lowest[i] = min(lows[i - lookback : i])
        return {"highest": highest, "lowest": lowest}
    raise ValueError(f"Unknown condition type: {condition.type!r}")


def _condition_met(condition: StrategyCondition, series: dict, i: int, close_i: float) -> bool:
    """Evaluate one precomputed condition at index i."""
    if condition.type == "ema_cross":
        fast, slow = series["fast"], series["slow"]
        if i < 1:
            return False
        prev_diff = fast[i - 1] - slow[i - 1]
        curr_diff = fast[i] - slow[i]
        if condition.cross_direction == "up":
            return prev_diff <= 0 and curr_diff > 0
        return prev_diff >= 0 and curr_diff < 0
    if condition.type == "rsi_threshold":
        rsi = series["rsi"][i]
        if rsi is None:
            return False
        return rsi >= condition.rsi_value if condition.rsi_comparison == "above" else rsi <= condition.rsi_value
    if condition.type == "breakout":
        if condition.breakout_direction == "above_high":
            highest = series["highest"][i]
            return highest is not None and close_i > highest
        lowest = series["lowest"][i]
        return lowest is not None and close_i < lowest
    raise ValueError(f"Unknown condition type: {condition.type!r}")


def _insufficient_result(
    strategy: StrategyRead, symbol: str, interval: str, range_: str, candle_count: int, warmup_bars_required: int
) -> BacktestResult:
    return BacktestResult(
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        symbol=symbol,
        interval=interval,
        range_=range_,
        candle_count=candle_count,
        warmup_bars_required=warmup_bars_required,
        trade_count=0,
        min_trades_required=MIN_SIMULATED_TRADES,
        has_sufficient_data=False,
        win_rate_pct=None,
        profit_factor=None,
        expectancy_r=None,
        max_drawdown_r=None,
        equity_curve=[],
        simulated_trades=[],
        open_at_data_end=False,
        note=(
            f"Need at least {warmup_bars_required} candles for this strategy's longest lookback "
            f"condition ({candle_count} available)."
        ),
        generated_at=datetime.now(timezone.utc),
    )


def run_backtest(
    strategy: StrategyRead,
    candles: list[dict],
    symbol: str,
    interval: str,
    range_: str,
    nominal_balance: float = 10000.0,
    max_holding_bars: int = 48,
) -> BacktestResult:
    """
    Purpose:    Walk forward through real historical candles, one position at
                a time, simulating entries when a strategy's conditions are
                all met and exits at stop-loss/take-profit/timeout — never
                persisted, always recomputed fresh from real data on request.
                Pure, synchronous, zero I/O (the caller fetches candles first).
    Args:       strategy (StrategyRead): The saved strategy definition.
                candles (list[dict]): Real chronological OHLC candles from
                    market_data.fetch_yahoo_candles.
                symbol (str): Symbol the candles belong to, for the response.
                interval (str): Candle interval used, for the response.
                range_ (str): Candle range used, for the response.
                nominal_balance (float): Reserved for future position-sizing
                    display; the backtest itself works entirely in R-multiples.
                max_holding_bars (int): Force-close a still-open simulated
                    position after this many bars if neither SL nor TP hit.
    Returns:    BacktestResult: Simulated trades, equity curve (in R), and
                    honestly-gated summary ratios.
    Raises:     None.
    """
    candle_count = len(candles)
    warmup_bars_required = max(_condition_warmup(c) for c in strategy.conditions) + WARMUP_BUFFER_BARS

    if candle_count - warmup_bars_required < 1:
        return _insufficient_result(strategy, symbol, interval, range_, candle_count, warmup_bars_required)

    closes = [c["close"] for c in candles]
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    times = [c["time"] for c in candles]

    condition_series = [_precompute_condition_series(c, closes, highs, lows) for c in strategy.conditions]
    direction_is_buy = strategy.direction.value == "buy"
    stop_loss_pct = float(strategy.stop_loss_pct) / 100.0
    target_r = float(strategy.target_r)

    simulated_trades: list[SimulatedTrade] = []
    in_position = False
    entry_index = entry_price = stop_price = target_price = None
    bars_held = 0

    for i in range(warmup_bars_required, candle_count):
        if in_position:
            bars_held += 1
            exit_price: float | None = None
            exit_reason: str | None = None

            if direction_is_buy:
                if lows[i] <= stop_price:
                    exit_price, exit_reason = stop_price, "stop_loss"
                elif highs[i] >= target_price:
                    exit_price, exit_reason = target_price, "take_profit"
            else:
                if highs[i] >= stop_price:
                    exit_price, exit_reason = stop_price, "stop_loss"
                elif lows[i] <= target_price:
                    exit_price, exit_reason = target_price, "take_profit"

            if exit_price is None and bars_held >= max_holding_bars:
                exit_price, exit_reason = closes[i], "timeout"

            if exit_price is not None:
                signed_move = (exit_price - entry_price) if direction_is_buy else (entry_price - exit_price)
                risk_amount = abs(entry_price - stop_price)
                r_multiple = signed_move / risk_amount if risk_amount > 0 else 0.0
                simulated_trades.append(
                    SimulatedTrade(
                        entry_index=entry_index,
                        entry_time=times[entry_index],
                        entry_price=entry_price,
                        exit_index=i,
                        exit_time=times[i],
                        exit_price=exit_price,
                        exit_reason=exit_reason,
                        bars_held=bars_held,
                        r_multiple=round(r_multiple, 4),
                    )
                )
                in_position = False
        else:
            all_met = all(
                _condition_met(cond, series, i, closes[i])
                for cond, series in zip(strategy.conditions, condition_series)
            )
            if all_met:
                entry_index = i
                entry_price = closes[i]
                if direction_is_buy:
                    stop_price = entry_price * (1 - stop_loss_pct)
                    target_price = entry_price + (entry_price - stop_price) * target_r
                else:
                    stop_price = entry_price * (1 + stop_loss_pct)
                    target_price = entry_price - (stop_price - entry_price) * target_r
                bars_held = 0
                in_position = True

    open_at_data_end = in_position
    trade_count = len(simulated_trades)
    has_sufficient_data = trade_count >= MIN_SIMULATED_TRADES

    win_rate_pct = None
    profit_factor = None
    expectancy_r = None
    max_drawdown_r = None
    equity_curve: list[BacktestEquityPoint] = []

    if trade_count > 0:
        r_values = [t.r_multiple for t in simulated_trades]
        wins = [r for r in r_values if r > 0]
        losses = [-r for r in r_values if r < 0]

        running_total = 0.0
        peak = float("-inf")
        max_dd = 0.0
        for idx, r in enumerate(r_values):
            running_total += r
            peak = max(peak, running_total)
            max_dd = max(max_dd, peak - running_total)
            equity_curve.append(BacktestEquityPoint(trade_index=idx, cumulative_r=round(running_total, 4)))

        if has_sufficient_data:
            win_rate_pct = round(len(wins) / trade_count * 100, 1)
            gross_wins = sum(wins)
            gross_losses = sum(losses)
            profit_factor = round(gross_wins / gross_losses, 2) if gross_losses > 0 else None
            expectancy_amount, _ = expectancy(wins, losses)
            expectancy_r = round(expectancy_amount, 3)
            max_drawdown_r = round(max_dd, 3)

    note = None
    if not has_sufficient_data:
        note = (
            f"Only {trade_count} simulated trade(s) — need at least {MIN_SIMULATED_TRADES} for reliable "
            "ratios. The full simulated-trade list and equity curve are still shown."
        )
    if open_at_data_end:
        open_note = "One simulated position was still open when the historical data ran out — excluded from all metrics."
        note = f"{note} {open_note}" if note else open_note

    return BacktestResult(
        strategy_id=strategy.id,
        strategy_name=strategy.name,
        symbol=symbol,
        interval=interval,
        range_=range_,
        candle_count=candle_count,
        warmup_bars_required=warmup_bars_required,
        trade_count=trade_count,
        min_trades_required=MIN_SIMULATED_TRADES,
        has_sufficient_data=has_sufficient_data,
        win_rate_pct=win_rate_pct,
        profit_factor=profit_factor,
        expectancy_r=expectancy_r,
        max_drawdown_r=max_drawdown_r,
        equity_curve=equity_curve,
        simulated_trades=simulated_trades,
        open_at_data_end=open_at_data_end,
        note=note,
        generated_at=datetime.now(timezone.utc),
    )
