import asyncio
from datetime import datetime, timedelta, timezone

from app.models.database import TradeDirection, TradeLedger, TradeStatus
from app.services.news_impact import build_position_context, compute_price_move, tag_symbols

CANDIDATES = ["EURUSD", "XAUUSD", "BTCUSD", "AAPL"]


def _trade(*, symbol, open_time, close_time, status=TradeStatus.CLOSED):
    trade = TradeLedger(
        portfolio_id=1,
        symbol=symbol,
        direction=TradeDirection.BUY,
        volume=1.0,
        open_price=100.0,
        close_price=105.0 if status == TradeStatus.CLOSED else None,
        open_time=open_time,
        close_time=close_time,
        profit=5.0 if status == TradeStatus.CLOSED else None,
        status=status,
    )
    return trade


def test_tag_symbols_matches_a_known_alias():
    assert tag_symbols("Gold prices surge as investors flee to safety", CANDIDATES) == ["XAUUSD"]


def test_tag_symbols_matches_literal_ticker():
    assert tag_symbols("EURUSD breaks key resistance level", CANDIDATES) == ["EURUSD"]


def test_tag_symbols_negative_case_no_ner_limitation_is_real():
    # "Apple" without "AAPL" must NOT tag — proves the no-NER limitation is
    # real and not silently patched with a broader keyword guess.
    assert tag_symbols("Apple unveils new iPhone lineup", CANDIDATES) == []


def test_tag_symbols_returns_multiple_matches():
    matched = tag_symbols("Bitcoin and gold both rally on Fed comments", CANDIDATES)
    assert set(matched) == {"BTCUSD", "XAUUSD"}


def test_build_position_context_none_when_no_publish_time():
    assert build_position_context(None, ["EURUSD"], []) is None


def test_build_position_context_none_when_no_symbols_matched():
    published = datetime(2026, 1, 10, tzinfo=timezone.utc)
    trades = [_trade(symbol="EURUSD", open_time=datetime(2026, 1, 9), close_time=datetime(2026, 1, 11))]
    assert build_position_context(published, [], trades) is None


def test_build_position_context_real_overlap():
    published = datetime(2026, 1, 10, tzinfo=timezone.utc)
    trades = [_trade(symbol="EURUSD", open_time=datetime(2026, 1, 9), close_time=datetime(2026, 1, 11))]
    context = build_position_context(published, ["EURUSD"], trades)
    assert context is not None
    assert "1 open EURUSD trade" in context


def test_build_position_context_none_when_trade_closed_before_publish():
    published = datetime(2026, 1, 10, tzinfo=timezone.utc)
    trades = [_trade(symbol="EURUSD", open_time=datetime(2026, 1, 1), close_time=datetime(2026, 1, 5))]
    assert build_position_context(published, ["EURUSD"], trades) is None


def test_build_position_context_counts_still_open_trades():
    published = datetime(2026, 1, 10, tzinfo=timezone.utc)
    trades = [_trade(symbol="EURUSD", open_time=datetime(2026, 1, 1), close_time=None, status=TradeStatus.OPEN)]
    context = build_position_context(published, ["EURUSD"], trades)
    assert "1 open EURUSD trade" in context


def test_compute_price_move_returns_none_outside_fetchable_window():
    async def fake_fetcher(symbol, interval, range_):
        # All candles are AFTER the published time -> nothing "before" exists.
        base = datetime(2026, 1, 15, tzinfo=timezone.utc).timestamp()
        return [{"time": base + i * 3600, "open": 1.1, "high": 1.1, "low": 1.1, "close": 1.1} for i in range(5)]

    published_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = asyncio.run(compute_price_move("EURUSD", published_at, fake_fetcher))
    assert result is None


def test_compute_price_move_computes_real_pct_change():
    published_epoch = datetime(2026, 1, 15, 12, tzinfo=timezone.utc).timestamp()

    async def fake_fetcher(symbol, interval, range_):
        return [
            {"time": published_epoch - 3600, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0},
            {"time": published_epoch + 3600, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.05},
        ]

    published_at = datetime(2026, 1, 15, 12, tzinfo=timezone.utc)
    result = asyncio.run(compute_price_move("EURUSD", published_at, fake_fetcher))
    assert result is not None
    assert result.price_before == 1.0
    assert result.price_after == 1.05
    assert result.pct_change == 5.0
    assert "not evidence" in result.disclaimer


def test_compute_price_move_returns_none_on_empty_candles():
    async def empty_fetcher(symbol, interval, range_):
        return []

    result = asyncio.run(compute_price_move("EURUSD", datetime(2026, 1, 1, tzinfo=timezone.utc), empty_fetcher))
    assert result is None
