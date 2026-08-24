import asyncio
from datetime import datetime, timedelta

import pytest

from app.models.database import TradeDirection, TradeLedger, TradeStatus
from app.services.risk_management import (
    MIN_OVERLAPPING_CLOSES_FOR_CORRELATION,
    assess_risk,
    compute_correlation,
)

BASE_DAY = datetime(2026, 1, 5, 0, 0, 0)


def _open_trade(*, trade_id, symbol, volume=1.0, open_price=100.0, day_offset=0):
    trade = TradeLedger(
        portfolio_id=1,
        symbol=symbol,
        direction=TradeDirection.BUY,
        volume=volume,
        open_price=open_price,
        open_time=BASE_DAY + timedelta(days=day_offset),
        status=TradeStatus.OPEN,
    )
    trade.id = trade_id
    trade.setup = None
    trade.setup_id = None
    return trade


def test_compute_correlation_none_below_min_points():
    assert compute_correlation([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) is None


def test_compute_correlation_perfect_positive():
    series = [float(i) for i in range(MIN_OVERLAPPING_CLOSES_FOR_CORRELATION)]
    result = compute_correlation(series, series)
    assert result == pytest.approx(1.0)


def test_compute_correlation_perfect_negative():
    series_a = [float(i) for i in range(MIN_OVERLAPPING_CLOSES_FOR_CORRELATION)]
    series_b = [-float(i) for i in range(MIN_OVERLAPPING_CLOSES_FOR_CORRELATION)]
    result = compute_correlation(series_a, series_b)
    assert result == pytest.approx(-1.0)


def _make_fetcher(candles_by_symbol):
    async def fetcher(symbol, interval, range_):
        return candles_by_symbol.get(symbol, [])

    return fetcher


def test_assess_risk_no_stop_loss_gives_no_max_loss():
    result = asyncio.run(
        assess_risk(
            candidate_symbol="EURUSD",
            candidate_direction=TradeDirection.BUY,
            candidate_stop_loss=None,
            candidate_entry_price=1.1,
            candidate_position_size=1.0,
            portfolio_balance=10_000,
            open_trades=[],
            candle_fetcher=_make_fetcher({}),
        )
    )
    assert result.max_loss_amount is None
    assert "unbounded" in result.note.lower()


def test_assess_risk_computes_max_loss_and_impact():
    result = asyncio.run(
        assess_risk(
            candidate_symbol="EURUSD",
            candidate_direction=TradeDirection.BUY,
            candidate_stop_loss=1.09,
            candidate_entry_price=1.1,
            candidate_position_size=100.0,
            portfolio_balance=10_000,
            open_trades=[],
            candle_fetcher=_make_fetcher({}),
        )
    )
    assert result.max_loss_amount == pytest.approx(1.0, abs=0.01)
    assert result.portfolio_impact_pct is not None


def test_assess_risk_existing_exposure_from_open_trades():
    open_trades = [_open_trade(trade_id=1, symbol="GBPUSD", volume=10, open_price=100)]
    result = asyncio.run(
        assess_risk(
            candidate_symbol="EURUSD",
            candidate_direction=TradeDirection.BUY,
            candidate_stop_loss=None,
            candidate_entry_price=1.1,
            candidate_position_size=1.0,
            portfolio_balance=10_000,
            open_trades=open_trades,
            candle_fetcher=_make_fetcher({}),
        )
    )
    assert result.existing_exposure_pct == pytest.approx(10.0)
    assert result.open_position_count == 1


def test_assess_risk_flags_high_correlation():
    candles_a = [{"close": float(i)} for i in range(30)]
    candles_b = [{"close": float(i)} for i in range(30)]  # identical -> correlation 1.0
    open_trades = [_open_trade(trade_id=1, symbol="GBPUSD")]
    result = asyncio.run(
        assess_risk(
            candidate_symbol="EURUSD",
            candidate_direction=TradeDirection.BUY,
            candidate_stop_loss=None,
            candidate_entry_price=1.1,
            candidate_position_size=1.0,
            portfolio_balance=10_000,
            open_trades=open_trades,
            candle_fetcher=_make_fetcher({"EURUSD": candles_a, "GBPUSD": candles_b}),
        )
    )
    assert result.correlation_checked_count == 1
    assert len(result.correlated_positions) == 1
    assert result.correlated_positions[0].is_high_correlation is True
    assert "correlated" in result.note.lower()


def test_assess_risk_skips_same_symbol_open_trades():
    open_trades = [_open_trade(trade_id=1, symbol="EURUSD")]
    result = asyncio.run(
        assess_risk(
            candidate_symbol="EURUSD",
            candidate_direction=TradeDirection.BUY,
            candidate_stop_loss=None,
            candidate_entry_price=1.1,
            candidate_position_size=1.0,
            portfolio_balance=10_000,
            open_trades=open_trades,
            candle_fetcher=_make_fetcher({}),
        )
    )
    assert result.correlation_checked_count == 0
    assert result.correlated_positions == []


def test_assess_risk_never_raises_on_fetch_failure():
    async def failing_fetcher(symbol, interval, range_):
        raise RuntimeError("network down")

    open_trades = [_open_trade(trade_id=1, symbol="GBPUSD")]
    result = asyncio.run(
        assess_risk(
            candidate_symbol="EURUSD",
            candidate_direction=TradeDirection.BUY,
            candidate_stop_loss=None,
            candidate_entry_price=1.1,
            candidate_position_size=1.0,
            portfolio_balance=10_000,
            open_trades=open_trades,
            candle_fetcher=failing_fetcher,
        )
    )
    assert result.correlated_positions == []
