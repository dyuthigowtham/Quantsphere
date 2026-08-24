from app.services.market_regime import MIN_CANDLES_FOR_REGIME, classify_regime


def _candles(closes: list[float]) -> list[dict]:
    return [{"time": i, "open": c, "high": c, "low": c, "close": c} for i, c in enumerate(closes)]


def test_insufficient_candles_returns_honest_gate():
    candles = _candles([100.0] * (MIN_CANDLES_FOR_REGIME - 1))
    result = classify_regime(candles, "EURUSD", "1h", "1mo")
    assert result.has_sufficient_data is False
    assert result.trend is None
    assert result.volatility is None
    assert result.note is not None


def test_flat_prices_classified_as_ranging_low_volatility():
    candles = _candles([100.0] * 40)
    result = classify_regime(candles, "EURUSD", "1h", "1mo")
    assert result.has_sufficient_data is True
    assert result.trend == "ranging"
    assert result.volatility == "low"
    assert result.bias == "flat"


def test_strong_uptrend_classified_as_trending_up():
    closes = [100.0 + i * 0.5 for i in range(50)]
    result = classify_regime(_candles(closes), "EURUSD", "1h", "1mo")
    assert result.has_sufficient_data is True
    assert result.trend == "trending_up"
    assert result.bias == "bullish"
    assert result.net_change_pct > 0


def test_strong_downtrend_classified_as_trending_down():
    closes = [150.0 - i * 0.5 for i in range(50)]
    result = classify_regime(_candles(closes), "EURUSD", "1h", "1mo")
    assert result.has_sufficient_data is True
    assert result.trend == "trending_down"
    assert result.bias == "bearish"


def test_high_volatility_detected():
    closes = []
    for i in range(40):
        closes.append(100.0 if i % 2 == 0 else 130.0)
    result = classify_regime(_candles(closes), "EURUSD", "1h", "1mo")
    assert result.has_sufficient_data is True
    assert result.volatility == "high"


def test_label_combines_all_three_signals():
    closes = [100.0 + i * 0.5 for i in range(50)]
    result = classify_regime(_candles(closes), "EURUSD", "1h", "1mo")
    assert "trending up" in result.label.lower()
    assert result.volatility in result.label.lower()
    assert result.bias in result.label.lower()


def test_disclaimer_always_present():
    result = classify_regime(_candles([100.0] * 5), "EURUSD", "1h", "1mo")
    assert "heuristic" in result.disclaimer.lower()
