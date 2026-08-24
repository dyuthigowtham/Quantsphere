import statistics

from app.models.schemas import RegimeResult

# A 10-vs-30-period SMA gap is a common, simple trend filter; 30 periods is
# also the minimum needed to compute the long SMA at all.
SMA_SHORT_PERIOD = 10
SMA_LONG_PERIOD = 30
MIN_CANDLES_FOR_REGIME = SMA_LONG_PERIOD
SLOPE_LOOKBACK = 5

# |sma_short - sma_long| / sma_long, as a percentage. Roughly the noise floor
# for a 10/30 SMA gap on typical 15m-1h FX/index candles — small enough to
# catch real drift, large enough that two SMAs sitting on top of each other
# in a flat market don't register as "trending." A heuristic, not a fit.
TREND_SEPARATION_THRESHOLD_PCT = 0.15

# Coefficient of variation (stdev/mean) of closes, as a percentage. A normal,
# non-news-event session for major FX pairs/large-cap indices typically sits
# well under this; readings above it reflect a genuinely more volatile
# session. Also a heuristic, not fitted to any specific symbol's history.
VOLATILITY_COV_THRESHOLD_PCT = 0.3


def _sma(values: list[float], period: int) -> float:
    return statistics.mean(values[-period:])


def classify_regime(candles: list[dict], symbol: str, interval: str, range_: str) -> RegimeResult:
    """
    Purpose:    Deterministic, heuristic-threshold classification of a
                symbol's CURRENT price regime from real recent candles — pure
                math, no I/O, no AI. Does not reconstruct the regime as of
                any point in the past (see market_data.fetch_yahoo_candles's
                always-ends-at-now limitation).
    Args:       candles (list[dict]): Chronological OHLC candles as returned
                    by market_data.fetch_yahoo_candles (each with a "close" key).
                symbol (str): The symbol these candles belong to, for the response.
                interval (str): The candle interval used, for the response.
                range_ (str): The candle range used, for the response.
    Returns:    RegimeResult: Trend/volatility/bias classification plus the
                    raw signals, or an honest "not enough data" state below
                    MIN_CANDLES_FOR_REGIME candles.
    Raises:     None.
    """
    candle_count = len(candles)
    if candle_count < MIN_CANDLES_FOR_REGIME:
        return RegimeResult(
            symbol=symbol,
            interval=interval,
            range_=range_,
            candle_count=candle_count,
            has_sufficient_data=False,
            trend=None,
            volatility=None,
            bias=None,
            label=None,
            sma_short=None,
            sma_long=None,
            separation_pct=None,
            volatility_cov_pct=None,
            net_change_pct=None,
            note=f"Need at least {MIN_CANDLES_FOR_REGIME} candles to classify a regime ({candle_count} available).",
        )

    closes = [c["close"] for c in candles]

    sma_short = _sma(closes, SMA_SHORT_PERIOD)
    sma_long = _sma(closes, SMA_LONG_PERIOD)
    separation_pct = abs(sma_short - sma_long) / sma_long * 100 if sma_long else 0.0

    prior_sma_short = _sma(closes[:-SLOPE_LOOKBACK], SMA_SHORT_PERIOD) if candle_count > SMA_LONG_PERIOD + SLOPE_LOOKBACK else sma_short
    slope_up = sma_short > prior_sma_short
    trend_up = sma_short > sma_long

    if separation_pct >= TREND_SEPARATION_THRESHOLD_PCT and slope_up == trend_up:
        trend = "trending_up" if trend_up else "trending_down"
    else:
        trend = "ranging"

    mean_close = statistics.mean(closes)
    volatility_cov_pct = (statistics.pstdev(closes) / mean_close * 100) if mean_close else 0.0
    volatility = "high" if volatility_cov_pct >= VOLATILITY_COV_THRESHOLD_PCT else "low"

    net_change_pct = (closes[-1] - closes[0]) / closes[0] * 100 if closes[0] else 0.0
    bias = "bullish" if net_change_pct > 0 else "bearish" if net_change_pct < 0 else "flat"

    trend_label = trend.replace("_", " ")
    label = f"{trend_label.capitalize()}, {volatility} volatility, {bias} bias"

    return RegimeResult(
        symbol=symbol,
        interval=interval,
        range_=range_,
        candle_count=candle_count,
        has_sufficient_data=True,
        trend=trend,
        volatility=volatility,
        bias=bias,
        label=label,
        sma_short=round(sma_short, 6),
        sma_long=round(sma_long, 6),
        separation_pct=round(separation_pct, 3),
        volatility_cov_pct=round(volatility_cov_pct, 3),
        net_change_pct=round(net_change_pct, 3),
        note=None,
    )
