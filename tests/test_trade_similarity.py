from datetime import datetime, timedelta

from app.models.database import Setup, TradeDirection, TradeLedger, TradeStatus
from app.services.trade_similarity import MIN_SIMILAR_TRADES_FOR_INSIGHT, find_similar_trades

BASE_DAY = datetime(2026, 1, 5, 0, 0, 0)


def _trade(
    *,
    trade_id,
    symbol="EURUSD",
    direction=TradeDirection.BUY,
    volume=1.0,
    open_price=100.0,
    close_price=101.0,
    stop_loss=None,
    profit=10.0,
    day_offset=0,
    open_hour=9,
    setup=None,
    status=TradeStatus.CLOSED,
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
        open_time=open_time,
        close_time=open_time + timedelta(minutes=30),
        profit=profit,
        status=status,
    )
    trade.id = trade_id
    trade.setup = setup
    trade.setup_id = setup.id if setup else None
    return trade


def test_no_matches_returns_zero_with_note():
    trades = [_trade(trade_id=1, symbol="GBPUSD")]
    result = find_similar_trades(trades, "EURUSD", TradeDirection.BUY, None, None)
    assert result.total_matched == 0
    assert result.has_sufficient_data is False
    assert result.note is not None
    assert result.common_conditions == []


def test_symbol_match_required_direction_ignored_for_inclusion():
    trades = [
        _trade(trade_id=1, symbol="EURUSD", direction=TradeDirection.BUY),
        _trade(trade_id=2, symbol="EURUSD", direction=TradeDirection.SELL),
        _trade(trade_id=3, symbol="GBPUSD", direction=TradeDirection.BUY),
    ]
    result = find_similar_trades(trades, "eurusd", TradeDirection.BUY, None, None)
    assert result.total_matched == 2


def test_excludes_anchor_trade():
    trades = [_trade(trade_id=1, symbol="EURUSD"), _trade(trade_id=2, symbol="EURUSD")]
    result = find_similar_trades(trades, "EURUSD", TradeDirection.BUY, None, None, exclude_trade_id=1)
    assert result.total_matched == 1
    assert result.matched_trades[0].trade_id == 2


def test_open_trades_excluded():
    trades = [_trade(trade_id=1, symbol="EURUSD", status=TradeStatus.OPEN)]
    result = find_similar_trades(trades, "EURUSD", TradeDirection.BUY, None, None)
    assert result.total_matched == 0


def test_win_loss_split_and_avg_profit():
    trades = [
        _trade(trade_id=1, symbol="EURUSD", profit=50.0),
        _trade(trade_id=2, symbol="EURUSD", profit=-20.0),
        _trade(trade_id=3, symbol="EURUSD", profit=30.0),
    ]
    result = find_similar_trades(trades, "EURUSD", TradeDirection.BUY, None, None)
    assert result.winners == 2
    assert result.losers == 1
    assert result.avg_profit == 20.0


def test_common_conditions_gated_below_insight_threshold():
    trades = [_trade(trade_id=i, symbol="EURUSD", profit=10.0 if i % 2 == 0 else -10.0) for i in range(4)]
    assert len(trades) < MIN_SIMILAR_TRADES_FOR_INSIGHT
    result = find_similar_trades(trades, "EURUSD", TradeDirection.BUY, None, None)
    assert result.has_sufficient_data is False
    assert result.common_conditions == []
    assert result.total_matched == 4  # real count still shown


def test_common_conditions_populated_above_threshold_with_real_signal():
    trades = []
    for i in range(3):
        trades.append(
            _trade(trade_id=i, symbol="EURUSD", profit=50.0, open_price=100, close_price=104, stop_loss=98, open_hour=9)
        )
    for i in range(3, 6):
        trades.append(
            _trade(trade_id=i, symbol="EURUSD", profit=-30.0, open_price=100, close_price=99, stop_loss=98, open_hour=22)
        )
    result = find_similar_trades(trades, "EURUSD", TradeDirection.BUY, None, None)
    assert result.has_sufficient_data is True
    assert result.avg_realized_r is not None
    assert len(result.common_conditions) > 0


def test_setup_match_scores_higher_than_no_setup_match():
    breakout = Setup(id=1, portfolio_id=1, name="Breakout")
    trades = [
        _trade(trade_id=1, symbol="EURUSD", setup=breakout),
        _trade(trade_id=2, symbol="EURUSD", setup=None),
    ]
    result = find_similar_trades(trades, "EURUSD", TradeDirection.BUY, 1, None)
    scores = {m.trade_id: m.similarity_score for m in result.matched_trades}
    assert scores[1] > scores[2]
