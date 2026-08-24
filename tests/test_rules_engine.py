import pytest

from app.ai.rules_engine import (
    calculate_macd,
    calculate_profit_pct,
    calculate_rsi,
    ema_series,
    grade_trade,
    pretrade_check,
    rsi_series,
    score_trade_setup,
)


def test_profit_pct_buy_win() -> None:
    assert calculate_profit_pct(open_price=100, close_price=102, direction_is_buy=True) == pytest.approx(2.0)


def test_profit_pct_sell_win() -> None:
    assert calculate_profit_pct(open_price=100, close_price=98, direction_is_buy=False) == pytest.approx(2.0)


def test_profit_pct_buy_loss() -> None:
    assert calculate_profit_pct(open_price=100, close_price=95, direction_is_buy=True) == pytest.approx(-5.0)


def test_profit_pct_rejects_zero_open_price() -> None:
    with pytest.raises(ValueError):
        calculate_profit_pct(open_price=0, close_price=10, direction_is_buy=True)


def test_rsi_returns_none_with_insufficient_history() -> None:
    assert calculate_rsi([1.0, 2.0, 3.0], period=14) is None


def test_rsi_all_gains_is_100() -> None:
    closes = [float(i) for i in range(1, 20)]
    assert calculate_rsi(closes, period=14) == pytest.approx(100.0)


def test_macd_returns_none_with_insufficient_history() -> None:
    assert calculate_macd([1.0] * 10) is None


def test_macd_returns_pair_with_enough_history() -> None:
    closes = [100 + i * 0.5 for i in range(60)]
    result = calculate_macd(closes)
    assert result is not None
    macd_line, signal_line = result
    assert isinstance(macd_line, float)
    assert isinstance(signal_line, float)


@pytest.mark.parametrize(
    ("close_price", "expected_grade"),
    [
        (103, "A"),
        (100.7, "B"),
        (100.1, "C"),
        (99.5, "D"),
        (95, "F"),
    ],
)
def test_grade_trade_thresholds(close_price: float, expected_grade: str) -> None:
    result = grade_trade(open_price=100, close_price=close_price, direction_is_buy=True)
    assert result.grade == expected_grade


def test_grade_trade_includes_indicator_context_when_history_given() -> None:
    price_history = [100 + i * 0.3 for i in range(60)]
    result = grade_trade(open_price=100, close_price=103, direction_is_buy=True, price_history=price_history)
    assert result.rsi is not None
    assert result.macd_line is not None
    assert "strong_profit" in result.triggered_indicators


@pytest.mark.parametrize(
    ("volume", "price", "balance", "expected_level"),
    [
        (1, 100, 10_000, "low"),
        (1, 2_500, 10_000, "moderate"),
        (1, 6_000, 10_000, "high"),
    ],
)
def test_pretrade_check_risk_level_thresholds(volume, price, balance, expected_level) -> None:
    result = pretrade_check(volume=volume, price=price, portfolio_balance=balance)
    assert result.risk_level == expected_level
    assert bool(result.warnings) == (expected_level != "low")


def test_pretrade_check_zero_balance_is_low_risk_with_no_rsi() -> None:
    result = pretrade_check(volume=1, price=100, portfolio_balance=0)
    assert result.risk_pct is None
    assert result.risk_level == "low"
    assert result.rsi_zone is None
    assert result.buy_note is None
    assert result.sell_note is None


def test_pretrade_check_overbought_rsi_warns_buyers_not_sellers() -> None:
    result = pretrade_check(volume=1, price=100, portfolio_balance=10_000, rsi=82)
    assert result.rsi_zone == "overbought"
    assert "overbought" in result.buy_note
    assert "pullbacks" in result.sell_note


def test_pretrade_check_oversold_rsi_warns_sellers_not_buyers() -> None:
    result = pretrade_check(volume=1, price=100, portfolio_balance=10_000, rsi=18)
    assert result.rsi_zone == "oversold"
    assert "bounces" in result.buy_note
    assert "oversold" in result.sell_note


def test_score_trade_setup_missing_sl_tp_marks_risk_reward_unavailable() -> None:
    result = score_trade_setup(
        direction_is_buy=True,
        entry_price=100,
        stop_loss=None,
        take_profit=None,
        position_size=1,
        portfolio_balance=10_000,
    )
    rr_check = next(c for c in result.checks if c.label == "Risk/reward ratio")
    assert rr_check.status == "unavailable"
    assert result.risk_reward_ratio is None


def test_score_trade_setup_strong_rr_and_low_risk_scores_well() -> None:
    result = score_trade_setup(
        direction_is_buy=True,
        entry_price=100,
        stop_loss=95,
        take_profit=120,
        position_size=1,
        portfolio_balance=100_000,
    )
    assert result.risk_reward_ratio == pytest.approx(4.0)
    assert result.rating in ("strong", "moderate")
    rr_check = next(c for c in result.checks if c.label == "Risk/reward ratio")
    assert rr_check.status == "pass"


def test_score_trade_setup_poor_rr_fails_that_check() -> None:
    result = score_trade_setup(
        direction_is_buy=True,
        entry_price=100,
        stop_loss=90,
        take_profit=105,
        position_size=1,
        portfolio_balance=100_000,
    )
    rr_check = next(c for c in result.checks if c.label == "Risk/reward ratio")
    assert rr_check.status == "fail"


def test_score_trade_setup_oversized_position_fails_risk_check() -> None:
    result = score_trade_setup(
        direction_is_buy=True,
        entry_price=100,
        stop_loss=95,
        take_profit=120,
        position_size=1000,
        portfolio_balance=10_000,
    )
    risk_check = next(c for c in result.checks if c.label == "Position risk")
    assert risk_check.status == "fail"
    assert result.risk_pct == pytest.approx(1000.0)


def test_score_trade_setup_score_never_negative_or_over_100() -> None:
    result = score_trade_setup(
        direction_is_buy=True,
        entry_price=100,
        stop_loss=None,
        take_profit=None,
        position_size=1,
        portfolio_balance=0,
    )
    assert 0 <= result.score <= 100
    assert result.disclaimer


def test_rsi_series_matches_calculate_rsi_at_last_index() -> None:
    closes = [100 + i * 0.3 for i in range(40)]
    series = rsi_series(closes, period=14)
    assert len(series) == len(closes)
    assert series[-1] == pytest.approx(calculate_rsi(closes, period=14))


def test_rsi_series_none_before_enough_history() -> None:
    closes = [float(i) for i in range(10)]
    series = rsi_series(closes, period=14)
    assert all(v is None for v in series)


def test_rsi_series_empty_input() -> None:
    assert rsi_series([], period=14) == []


def test_ema_series_length_matches_input_and_converges_toward_trend() -> None:
    values = [100.0 + i for i in range(30)]
    series = ema_series(values, period=10)
    assert len(series) == len(values)
    assert series[0] == values[0]
    assert series[-1] < values[-1]  # EMA lags a steadily rising series
