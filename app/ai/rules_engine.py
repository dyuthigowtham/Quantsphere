from app.models.schemas import PreTradeCheckResult, RulesEngineResult, SetupCheckItem, SetupQualityResult


def calculate_profit_pct(open_price: float, close_price: float, direction_is_buy: bool) -> float:
    """
    Purpose:    Compute percentage return of a closed trade relative to its entry price.
    Args:       open_price (float): Entry price. Must be > 0.
                close_price (float): Exit price.
                direction_is_buy (bool): True for a long/buy trade, False for short/sell.
    Returns:    float: Signed percentage return, e.g. 1.5 for +1.5%.
    Raises:     ValueError: If open_price is not positive.
    """
    if open_price <= 0:
        raise ValueError("open_price must be positive")
    raw_move = (close_price - open_price) / open_price
    return raw_move * 100 if direction_is_buy else -raw_move * 100


def rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """
    Purpose:    Compute the Relative Strength Index at every index of a
                closing-price series (not just the latest value) — needed by
                Strategy Lab to evaluate an RSI condition candle-by-candle
                during a backtest walk-forward, and reused by calculate_rsi
                for the single-value case.
    Args:       closes (list[float]): Chronologically ordered closing prices.
                period (int): Lookback window. Defaults to 14.
    Returns:    list[float | None]: Same length as `closes`; entries before
                    enough history exists are None.
    Raises:     None.
    """
    series: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return series

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta

    avg_gain = gains / period
    avg_loss = losses / period
    series[period] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        series[i] = 100.0 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    return series


def calculate_rsi(closes: list[float], period: int = 14) -> float | None:
    """
    Purpose:    Latest Relative Strength Index reading for a closing-price
                series — a thin convenience wrapper around rsi_series for
                call sites that only need the most recent value.
    Args:       closes (list[float]): Chronologically ordered closing prices.
                period (int): Lookback window. Defaults to 14.
    Returns:    float | None: RSI value in [0, 100], or None if there is not
                enough price history to compute it.
    Raises:     None.
    """
    series = rsi_series(closes, period)
    return series[-1] if series else None


def ema_series(values: list[float], period: int) -> list[float]:
    """
    Purpose:    Exponential moving average series for `values` over `period`.
                Public (not underscore-prefixed) since Strategy Lab imports
                this directly to evaluate EMA-cross conditions candle-by-candle.
    Args:       values (list[float]): Chronologically ordered values (closes,
                    or another series such as a MACD line).
                period (int): EMA window.
    Returns:    list[float]: Same length as `values`. The first element is
                    seeded from values[0] (not a true SMA seed).
    Raises:     None.
    """
    k = 2 / (period + 1)
    values_ema = [values[0]]
    for price in values[1:]:
        values_ema.append(price * k + values_ema[-1] * (1 - k))
    return values_ema


def calculate_macd(
    closes: list[float],
    fast_period: int = 12,
    slow_period: int = 26,
    signal_period: int = 9,
) -> tuple[float, float] | None:
    """
    Purpose:    Compute the MACD line and its signal line for a closing-price series.
    Args:       closes (list[float]): Chronologically ordered closing prices.
                fast_period (int): Fast EMA window. Defaults to 12.
                slow_period (int): Slow EMA window. Defaults to 26.
                signal_period (int): Signal line EMA window. Defaults to 9.
    Returns:    tuple[float, float] | None: (macd_line, signal_line) as of the
                latest close, or None if there is not enough price history.
    Raises:     None.
    """
    if len(closes) < slow_period + signal_period:
        return None

    fast_ema = ema_series(closes, fast_period)
    slow_ema = ema_series(closes, slow_period)
    macd_series = [f - s for f, s in zip(fast_ema, slow_ema)]
    signal_series = ema_series(macd_series, signal_period)
    return macd_series[-1], signal_series[-1]


def grade_trade(
    open_price: float,
    close_price: float,
    direction_is_buy: bool,
    price_history: list[float] | None = None,
) -> RulesEngineResult:
    """
    Purpose:    Produce the instant, zero-latency Phase A grade for a just-closed
                trade using hardcoded math only — no network or LLM calls.
    Args:       open_price (float): Entry price of the trade.
                close_price (float): Exit price of the trade.
                direction_is_buy (bool): True for long/buy, False for short/sell.
                price_history (list[float] | None): Recent closing prices for the
                    instrument (oldest to newest, ideally including close_price)
                    used to derive RSI/MACD context. Omit if unavailable.
    Returns:    RulesEngineResult: Grade (A-F), profit percentage, RSI/MACD
                readings when computable, and the list of indicators that
                triggered the final grade.
    Raises:     ValueError: If open_price is not positive.
    """
    profit_pct = calculate_profit_pct(open_price, close_price, direction_is_buy)

    rsi = calculate_rsi(price_history) if price_history else None
    macd = calculate_macd(price_history) if price_history else None
    macd_line, macd_signal = macd if macd else (None, None)

    triggered: list[str] = []

    if profit_pct >= 2:
        grade = "A"
        triggered.append("strong_profit")
    elif profit_pct >= 0.5:
        grade = "B"
        triggered.append("solid_profit")
    elif profit_pct >= 0:
        grade = "C"
        triggered.append("marginal_profit")
    elif profit_pct >= -1:
        grade = "D"
        triggered.append("small_loss")
    else:
        grade = "F"
        triggered.append("large_loss")

    if rsi is not None and (rsi >= 70 or rsi <= 30):
        triggered.append("rsi_extreme")
    if macd_line is not None and macd_signal is not None:
        triggered.append("macd_bullish_cross" if macd_line > macd_signal else "macd_bearish_cross")

    return RulesEngineResult(
        grade=grade,
        profit_pct=round(profit_pct, 4),
        rsi=round(rsi, 2) if rsi is not None else None,
        macd_line=round(macd_line, 6) if macd_line is not None else None,
        macd_signal=round(macd_signal, 6) if macd_signal is not None else None,
        triggered_indicators=triggered,
    )


def pretrade_check(
    volume: float,
    price: float,
    portfolio_balance: float,
    rsi: float | None = None,
) -> PreTradeCheckResult:
    """
    Purpose:    Produce an instant, zero-latency pre-trade risk readout — the
                paper-trading learning area's checkpoint shown before a market
                order is placed. Pure math only, mirroring grade_trade's
                Phase A approach; no network or LLM calls.
    Args:       volume (float): Requested trade size in lots/units.
                price (float): Current market price for the symbol.
                portfolio_balance (float): Portfolio's current balance.
                rsi (float | None): Recent RSI reading for the symbol, if
                    enough price history is cached; None if unavailable.
    Returns:    PreTradeCheckResult: Position-size risk level plus RSI-based
                    buy/sell guidance notes.
    Raises:     None.
    """
    notional_value = volume * price
    risk_pct = (notional_value / portfolio_balance) * 100 if portfolio_balance > 0 else None

    if risk_pct is None or risk_pct < 20:
        risk_level = "low"
    elif risk_pct < 50:
        risk_level = "moderate"
    else:
        risk_level = "high"

    warnings: list[str] = []
    if risk_level == "high":
        warnings.append(
            f"This position is {risk_pct:.0f}% of your portfolio balance — a single bad trade could hurt badly."
        )
    elif risk_level == "moderate":
        warnings.append(f"This position uses {risk_pct:.0f}% of your balance. Consider sizing down to manage risk.")

    rsi_zone = None
    buy_note = None
    sell_note = None
    if rsi is not None:
        if rsi >= 70:
            rsi_zone = "overbought"
            buy_note = f"RSI is {rsi:.0f} (overbought) — buying here goes against short-term momentum exhaustion."
            sell_note = f"RSI is {rsi:.0f} (overbought) — often where pullbacks begin."
        elif rsi <= 30:
            rsi_zone = "oversold"
            buy_note = f"RSI is {rsi:.0f} (oversold) — often where bounces begin."
            sell_note = f"RSI is {rsi:.0f} (oversold) — selling here goes against short-term momentum exhaustion."
        else:
            rsi_zone = "neutral"
            buy_note = f"RSI is {rsi:.0f} — no extreme signal either way."
            sell_note = f"RSI is {rsi:.0f} — no extreme signal either way."

    return PreTradeCheckResult(
        price=round(price, 5),
        notional_value=round(notional_value, 2),
        risk_pct=round(risk_pct, 2) if risk_pct is not None else None,
        risk_level=risk_level,
        rsi=round(rsi, 2) if rsi is not None else None,
        rsi_zone=rsi_zone,
        warnings=warnings,
        buy_note=buy_note,
        sell_note=sell_note,
    )


def score_trade_setup(
    direction_is_buy: bool,
    entry_price: float,
    stop_loss: float | None,
    take_profit: float | None,
    position_size: float,
    portfolio_balance: float,
    price_history: list[float] | None = None,
) -> SetupQualityResult:
    """
    Purpose:    Deterministic, zero-latency 0-100 setup-quality score for the
                "Check My Trade" pre-trade decision-support feature — pure
                math against the trader's own inputs plus recent price
                history, no LLM involved. Every check that lacks the data to
                evaluate honestly is marked "unavailable" and excluded from
                scoring, rather than guessed, so a thin-data check never
                inflates or deflates the score.
    Args:       direction_is_buy (bool): True for long/buy, False for short/sell.
                entry_price (float): Planned or actual entry price.
                stop_loss (float | None): Planned stop-loss price, if set.
                take_profit (float | None): Planned take-profit price, if set.
                position_size (float): Planned position size (lots/units).
                portfolio_balance (float): Portfolio's current balance.
                price_history (list[float] | None): Recent closing prices,
                    oldest to newest, if available.
    Returns:    SetupQualityResult: 0-100 score, strong/moderate/weak rating,
                    a breakdown of each check, and a fixed non-guarantee disclaimer.
    Raises:     None.
    """
    checks: list[SetupCheckItem] = []
    achieved = 0.0
    available_weight = 0.0

    risk_reward_ratio: float | None = None
    if stop_loss is not None and take_profit is not None:
        risk = abs(entry_price - stop_loss)
        reward = abs(take_profit - entry_price)
        risk_reward_ratio = round(reward / risk, 2) if risk > 0 else None

    weight = 30.0
    if risk_reward_ratio is None:
        checks.append(
            SetupCheckItem(
                label="Risk/reward ratio",
                status="unavailable",
                detail="Set both a stop-loss and take-profit to score risk/reward.",
            )
        )
    else:
        available_weight += weight
        if risk_reward_ratio >= 2:
            achieved += weight
            checks.append(
                SetupCheckItem(label="Risk/reward ratio", status="pass", detail=f"{risk_reward_ratio:.2f}R planned.")
            )
        elif risk_reward_ratio >= 1.5:
            achieved += weight * 0.7
            checks.append(
                SetupCheckItem(
                    label="Risk/reward ratio", status="pass", detail=f"{risk_reward_ratio:.2f}R — solid."
                )
            )
        elif risk_reward_ratio >= 1:
            achieved += weight * 0.35
            checks.append(
                SetupCheckItem(
                    label="Risk/reward ratio", status="warn", detail=f"{risk_reward_ratio:.2f}R — on the thin side."
                )
            )
        else:
            checks.append(
                SetupCheckItem(
                    label="Risk/reward ratio",
                    status="fail",
                    detail=f"{risk_reward_ratio:.2f}R — risking more than the planned reward.",
                )
            )

    notional_value = position_size * entry_price
    risk_pct = (notional_value / portfolio_balance) * 100 if portfolio_balance > 0 else None

    weight = 20.0
    if risk_pct is None:
        checks.append(
            SetupCheckItem(label="Position risk", status="unavailable", detail="Portfolio balance unavailable.")
        )
    else:
        available_weight += weight
        if risk_pct < 5:
            achieved += weight
            checks.append(SetupCheckItem(label="Position risk", status="pass", detail=f"{risk_pct:.1f}% of balance."))
        elif risk_pct < 10:
            achieved += weight * 0.6
            checks.append(SetupCheckItem(label="Position risk", status="warn", detail=f"{risk_pct:.1f}% of balance."))
        else:
            checks.append(
                SetupCheckItem(
                    label="Position risk", status="fail", detail=f"{risk_pct:.1f}% of balance — oversized."
                )
            )

    rsi = calculate_rsi(price_history) if price_history else None
    weight = 20.0
    if rsi is None:
        checks.append(
            SetupCheckItem(label="Momentum (RSI)", status="unavailable", detail="Not enough recent price history.")
        )
    else:
        available_weight += weight
        extended = (direction_is_buy and rsi >= 70) or (not direction_is_buy and rsi <= 30)
        confirming = (direction_is_buy and rsi > 50) or (not direction_is_buy and rsi < 50)
        if extended:
            checks.append(
                SetupCheckItem(
                    label="Momentum (RSI)", status="warn", detail=f"RSI {rsi:.0f} — momentum looks extended."
                )
            )
        elif confirming:
            achieved += weight
            checks.append(
                SetupCheckItem(label="Momentum (RSI)", status="pass", detail=f"RSI {rsi:.0f} confirms direction.")
            )
        else:
            achieved += weight * 0.4
            checks.append(
                SetupCheckItem(
                    label="Momentum (RSI)", status="warn", detail=f"RSI {rsi:.0f} doesn't confirm direction."
                )
            )

    weight = 15.0
    if price_history and len(price_history) >= 10:
        available_weight += weight
        sma = sum(price_history[-10:]) / 10
        trend_up = price_history[-1] > sma
        aligned = trend_up == direction_is_buy
        if aligned:
            achieved += weight
            checks.append(SetupCheckItem(label="Trend alignment", status="pass", detail="Aligned with short-term trend."))
        else:
            checks.append(
                SetupCheckItem(label="Trend alignment", status="warn", detail="Against the short-term trend.")
            )
    else:
        checks.append(
            SetupCheckItem(label="Trend alignment", status="unavailable", detail="Not enough recent price history.")
        )

    weight = 15.0
    if price_history and len(price_history) >= 10:
        available_weight += weight
        recent_high = max(price_history)
        recent_low = min(price_history)
        recent_range = recent_high - recent_low
        if recent_range > 0:
            if direction_is_buy:
                extended_pct = (entry_price - recent_high) / recent_range
                if extended_pct > 0.05:
                    checks.append(
                        SetupCheckItem(label="Entry vs. recent range", status="warn", detail="Entry is extended above recent resistance.")
                    )
                else:
                    achieved += weight
                    checks.append(
                        SetupCheckItem(label="Entry vs. recent range", status="pass", detail="Entry is within the recent range.")
                    )
            else:
                extended_pct = (recent_low - entry_price) / recent_range
                if extended_pct > 0.05:
                    checks.append(
                        SetupCheckItem(label="Entry vs. recent range", status="warn", detail="Entry is extended below recent support.")
                    )
                else:
                    achieved += weight
                    checks.append(
                        SetupCheckItem(label="Entry vs. recent range", status="pass", detail="Entry is within the recent range.")
                    )
        else:
            checks.append(
                SetupCheckItem(label="Entry vs. recent range", status="unavailable", detail="Recent price range is flat.")
            )
    else:
        checks.append(
            SetupCheckItem(label="Entry vs. recent range", status="unavailable", detail="Not enough recent price history.")
        )

    score = round((achieved / available_weight) * 100) if available_weight > 0 else 0
    rating = "strong" if score >= 70 else "moderate" if score >= 45 else "weak"

    return SetupQualityResult(
        score=score,
        rating=rating,
        risk_reward_ratio=risk_reward_ratio,
        risk_pct=round(risk_pct, 2) if risk_pct is not None else None,
        rsi=round(rsi, 2) if rsi is not None else None,
        checks=checks,
    )
