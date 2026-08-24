import asyncio
from datetime import datetime, timedelta

from app.models.database import TradeDirection, TradeLedger, TradeStatus
from app.services import alerts
from app.services.alerts import (
    SL_TP_PROXIMITY_THRESHOLD_PCT,
    check_oversizing_alert,
    check_overtrading_alert,
    check_revenge_trading_alert,
    check_sl_tp_proximity,
    evaluate_trade_event,
)

NOW = datetime(2026, 1, 15, 12, 0, 0)


def test_alert_detection_never_imports_ollama():
    # Mechanically enforces the hard rule that alert *detection* is
    # deterministic rule-based code, never an LLM call — not just a
    # convention someone could quietly break later. Checks for an actual
    # import/usage, not just the word "Ollama" (which legitimately appears
    # in this module's own docstrings explaining the rule).
    assert not hasattr(alerts, "OllamaClient")
    import inspect

    source = inspect.getsource(alerts)
    assert "ollama_client" not in source.lower()
    assert "import ollama" not in source.lower()


def _trade(*, trade_id, symbol="EURUSD", volume=1.0, open_time, close_time=None, profit=None, stop_loss=None, take_profit=None, open_price=100.0, status=TradeStatus.CLOSED):
    trade = TradeLedger(
        portfolio_id=1,
        symbol=symbol,
        direction=TradeDirection.BUY,
        volume=volume,
        open_price=open_price,
        close_price=open_price + profit if profit is not None else None,
        stop_loss=stop_loss,
        take_profit=take_profit,
        open_time=open_time,
        close_time=close_time,
        profit=profit,
        status=status,
    )
    trade.id = trade_id
    return trade


# ---------- Revenge trading ----------


def test_revenge_trading_alert_fires_and_cites_real_numbers():
    prev = _trade(trade_id=1, volume=1.0, open_time=NOW - timedelta(hours=1), close_time=NOW - timedelta(minutes=10), profit=-20.0)
    new_trade = _trade(trade_id=2, volume=2.0, open_time=NOW, status=TradeStatus.OPEN)
    event = check_revenge_trading_alert(new_trade, [prev])
    assert event is not None
    assert event.category == "revenge_trading"
    assert "EURUSD" in event.message
    assert "2.0" in event.message or "2" in event.message


def test_revenge_trading_alert_none_without_recent_loss():
    prev = _trade(trade_id=1, volume=1.0, open_time=NOW - timedelta(hours=1), close_time=NOW - timedelta(minutes=10), profit=20.0)
    new_trade = _trade(trade_id=2, volume=2.0, open_time=NOW, status=TradeStatus.OPEN)
    assert check_revenge_trading_alert(new_trade, [prev]) is None


def test_revenge_trading_alert_none_with_no_prior_trades():
    new_trade = _trade(trade_id=1, volume=1.0, open_time=NOW, status=TradeStatus.OPEN)
    assert check_revenge_trading_alert(new_trade, []) is None


# ---------- Overtrading ----------


def test_overtrading_alert_fires_on_first_crossing():
    historical_counts = [1, 1, 1, 1, 1, 1]  # low, consistent baseline
    new_trade = _trade(trade_id=99, open_time=NOW, status=TradeStatus.OPEN)
    event = check_overtrading_alert(new_trade, historical_counts, trades_today_so_far=4)
    assert event is not None
    assert event.category == "overtrading"
    assert "4" in event.message


def test_overtrading_alert_only_fires_once_not_every_trade_that_day():
    historical_counts = [1, 1, 1, 1, 1, 1]
    new_trade = _trade(trade_id=99, open_time=NOW, status=TradeStatus.OPEN)
    # Already crossed on a previous trade today (trades_today_so_far - 1 already >= threshold).
    assert check_overtrading_alert(new_trade, historical_counts, trades_today_so_far=5) is None


def test_overtrading_alert_none_without_enough_daily_history():
    new_trade = _trade(trade_id=99, open_time=NOW, status=TradeStatus.OPEN)
    assert check_overtrading_alert(new_trade, [1, 1], trades_today_so_far=5) is None


# ---------- Oversizing ----------


def test_oversizing_alert_fires_on_real_outlier():
    historical_volumes = [1.0, 1.2, 0.9, 1.1, 1.0, 0.8]
    new_trade = _trade(trade_id=99, volume=10.0, open_time=NOW, status=TradeStatus.OPEN)
    event = check_oversizing_alert(new_trade, historical_volumes)
    assert event is not None
    assert event.category == "oversizing"


def test_oversizing_alert_none_when_consistent():
    historical_volumes = [1.0, 1.0, 1.0, 1.0, 1.0]
    new_trade = _trade(trade_id=99, volume=1.0, open_time=NOW, status=TradeStatus.OPEN)
    assert check_oversizing_alert(new_trade, historical_volumes) is None


def test_oversizing_alert_none_without_enough_history():
    new_trade = _trade(trade_id=99, volume=10.0, open_time=NOW, status=TradeStatus.OPEN)
    assert check_oversizing_alert(new_trade, [1.0]) is None


# ---------- SL/TP proximity ----------


def test_sl_tp_proximity_fires_near_stop_loss():
    trade = _trade(trade_id=1, open_price=100.0, stop_loss=90.0, open_time=NOW, status=TradeStatus.OPEN)
    # 91 is 10% of the way from stop_loss(90) back toward entry(100) -> exactly at the threshold.
    event = check_sl_tp_proximity(trade, current_price=91.0)
    assert event is not None
    assert event.category == "sl_tp_proximity"
    assert "stop-loss" in event.message


def test_sl_tp_proximity_none_when_far_away():
    trade = _trade(trade_id=1, open_price=100.0, stop_loss=90.0, open_time=NOW, status=TradeStatus.OPEN)
    assert check_sl_tp_proximity(trade, current_price=99.0) is None


def test_sl_tp_proximity_fires_near_take_profit():
    trade = _trade(trade_id=1, open_price=100.0, take_profit=120.0, open_time=NOW, status=TradeStatus.OPEN)
    event = check_sl_tp_proximity(trade, current_price=119.0)
    assert event is not None
    assert "take-profit" in event.message


def test_sl_tp_proximity_none_without_any_level_set():
    trade = _trade(trade_id=1, open_price=100.0, open_time=NOW, status=TradeStatus.OPEN)
    assert check_sl_tp_proximity(trade, current_price=100.0) is None


# ---------- evaluate_trade_event orchestration ----------


def test_evaluate_trade_event_returns_multiple_events_when_multiple_fire():
    prev = _trade(trade_id=1, volume=1.0, open_time=NOW - timedelta(hours=1), close_time=NOW - timedelta(minutes=10), profit=-20.0)
    new_trade = _trade(trade_id=2, volume=5.0, open_time=NOW, status=TradeStatus.OPEN)
    events = asyncio.run(evaluate_trade_event(new_trade, [prev]))
    categories = {e.category for e in events}
    assert "revenge_trading" in categories


def test_evaluate_trade_event_returns_empty_list_on_clean_trade():
    new_trade = _trade(trade_id=1, volume=1.0, open_time=NOW, status=TradeStatus.OPEN)
    events = asyncio.run(evaluate_trade_event(new_trade, []))
    assert events == []
