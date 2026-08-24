from datetime import datetime, timedelta

from app.models.database import Setup, TradeDirection, TradeLedger, TradeStatus
from app.models.schemas import SetupPerformanceFilter
from app.services.setup_performance import compute_setup_performance

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
    take_profit=None,
    profit=10.0,
    day_offset=0,
    open_hour=9,
    setup=None,
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
        close_time=open_time + timedelta(minutes=30),
        profit=profit,
        status=TradeStatus.CLOSED,
    )
    trade.id = trade_id
    trade.setup = setup
    trade.setup_id = setup.id if setup else None
    return trade


def test_empty_trades_returns_no_rows_with_note():
    result = compute_setup_performance(1, [], SetupPerformanceFilter())
    assert result.trades_matched == 0
    assert result.rows == []
    assert result.note is not None


def test_untagged_and_tagged_setups_grouped_separately():
    breakout = Setup(id=1, portfolio_id=1, name="Breakout")
    trades = [
        _trade(trade_id=1, profit=10, setup=breakout, day_offset=0),
        _trade(trade_id=2, profit=20, setup=breakout, day_offset=1),
        _trade(trade_id=3, profit=30, setup=breakout, day_offset=2),
        _trade(trade_id=4, profit=-5, setup=None, day_offset=3),
        _trade(trade_id=5, profit=-5, setup=None, day_offset=4),
        _trade(trade_id=6, profit=-5, setup=None, day_offset=5),
    ]
    result = compute_setup_performance(1, trades, SetupPerformanceFilter())
    by_name = {r.setup_name: r for r in result.rows}
    assert by_name["Breakout"].trade_count == 3
    assert by_name["Breakout"].total_profit == 60.0
    assert by_name["Untagged"].trade_count == 3
    assert by_name["Untagged"].total_profit == -15.0


def test_row_below_min_trades_has_no_ratios_but_shows_count():
    trades = [_trade(trade_id=1, profit=10, day_offset=0)]
    result = compute_setup_performance(1, trades, SetupPerformanceFilter())
    row = result.rows[0]
    assert row.trade_count == 1
    assert row.has_sufficient_data is False
    assert row.avg_realized_r is None
    assert row.profit_factor is None
    assert row.expectancy is None


def test_profit_factor_is_none_not_infinite_when_no_losses():
    trades = [_trade(trade_id=i, profit=10, day_offset=i) for i in range(3)]
    result = compute_setup_performance(1, trades, SetupPerformanceFilter())
    row = result.rows[0]
    assert row.has_sufficient_data is True
    assert row.profit_factor is None


def test_symbol_filter():
    trades = [
        _trade(trade_id=1, symbol="EURUSD", day_offset=0),
        _trade(trade_id=2, symbol="GBPUSD", day_offset=1),
    ]
    result = compute_setup_performance(1, trades, SetupPerformanceFilter(symbol="eurusd"))
    assert result.trades_matched == 1
    assert result.rows[0].trade_count == 1


def test_direction_filter():
    trades = [
        _trade(trade_id=1, direction=TradeDirection.BUY, day_offset=0),
        _trade(trade_id=2, direction=TradeDirection.SELL, day_offset=1),
    ]
    result = compute_setup_performance(1, trades, SetupPerformanceFilter(direction=TradeDirection.SELL))
    assert result.trades_matched == 1


def test_setup_id_filter():
    breakout = Setup(id=1, portfolio_id=1, name="Breakout")
    trades = [
        _trade(trade_id=1, setup=breakout, day_offset=0),
        _trade(trade_id=2, setup=None, day_offset=1),
    ]
    result = compute_setup_performance(1, trades, SetupPerformanceFilter(setup_id=1))
    assert result.trades_matched == 1
    assert result.rows[0].setup_name == "Breakout"


def test_date_range_filter():
    trades = [_trade(trade_id=i, day_offset=i) for i in range(10)]
    result = compute_setup_performance(
        1, trades, SetupPerformanceFilter(date_from=BASE_DAY + timedelta(days=5), date_to=BASE_DAY + timedelta(days=8))
    )
    assert result.trades_matched == 3


def test_open_trades_excluded():
    open_trade = TradeLedger(
        portfolio_id=1,
        symbol="EURUSD",
        direction=TradeDirection.BUY,
        volume=1.0,
        open_price=100.0,
        open_time=BASE_DAY,
        status=TradeStatus.OPEN,
    )
    open_trade.id = 99
    open_trade.setup = None
    open_trade.setup_id = None
    result = compute_setup_performance(1, [open_trade], SetupPerformanceFilter())
    assert result.trades_matched == 0
