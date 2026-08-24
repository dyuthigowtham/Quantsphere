import statistics
from collections import defaultdict
from dataclasses import dataclass

from app.models.database import Alert, AsyncSessionLocal, TradeLedger, TradeStatus
from app.services.trading_profile import (
    MIN_TRADES_PER_BUCKET,
    OVERSIZING_STDEV_MULTIPLIER,
    OVERTRADING_MIN_TRADES_ON_DAY,
    OVERTRADING_STDEV_MULTIPLIER,
    _is_revenge_trade,
)

# Tier 2 — how close price must get to a real, user-set SL/TP (as a fraction
# of the original entry-to-level distance) before it's worth surfacing.
# Deliberately NOT a generic support/resistance detector — that logic
# doesn't exist anywhere in this codebase, and inventing one here would risk
# presenting a heuristic as a level the user never actually set.
SL_TP_PROXIMITY_THRESHOLD_PCT = 0.10


@dataclass(frozen=True)
class AlertEvent:
    """One detected, real, cite-the-number alert — never a probability or a guess."""

    category: str
    severity: str | None
    message: str
    trade_id: int | None
    symbol: str | None


def check_revenge_trading_alert(new_trade: TradeLedger, recent_closed_trades: list[TradeLedger]) -> AlertEvent | None:
    """
    Purpose:    Tier-1 event-triggered check — does opening `new_trade` match
                the exact revenge-trading predicate the Mistake Detector
                already uses (trading_profile._is_revenge_trade), evaluated
                against the single most recent closed trade rather than the
                whole history.
    Args:       new_trade (TradeLedger): The just-opened trade.
                recent_closed_trades (list[TradeLedger]): The portfolio's
                    closed trades (any order) to find the most recent one from.
    Returns:    AlertEvent | None: None if there's no recent closed trade or
                    the pattern doesn't match.
    Raises:     None.
    """
    closed_with_time = [t for t in recent_closed_trades if t.close_time is not None]
    if not closed_with_time:
        return None
    most_recent = max(closed_with_time, key=lambda t: t.close_time)
    if not _is_revenge_trade(most_recent, new_trade):
        return None

    gap_minutes = round((new_trade.open_time - most_recent.close_time).total_seconds() / 60, 1)
    volume_ratio = round(float(new_trade.volume) / float(most_recent.volume), 2)
    return AlertEvent(
        category="revenge_trading",
        severity="moderate",
        message=(
            f"Opened within {gap_minutes} min of a loss on {most_recent.symbol}, sized {volume_ratio}x that "
            "trade — matches your revenge-trading pattern."
        ),
        trade_id=new_trade.id,
        symbol=new_trade.symbol,
    )


def check_overtrading_alert(new_trade: TradeLedger, historical_daily_counts: list[int], trades_today_so_far: int) -> AlertEvent | None:
    """
    Purpose:    Tier-1 check — does opening `new_trade` push today's trade
                count across the same mean+stdev threshold
                detect_overtrading uses for the whole-history flag, evaluated
                fresh against today so far.
    Args:       new_trade (TradeLedger): The just-opened trade (used only
                    for its id/symbol in the message).
                historical_daily_counts (list[int]): Trade counts for each
                    PAST day (excluding today), used to compute the baseline.
                trades_today_so_far (int): Trade count for today, INCLUDING
                    new_trade.
    Returns:    AlertEvent | None: None if there isn't enough daily history
                    to establish a baseline, or today hasn't just crossed
                    the threshold for the first time.
    Raises:     None.
    """
    if len(historical_daily_counts) < 5:
        return None
    mean_count = statistics.mean(historical_daily_counts)
    stdev_count = statistics.pstdev(historical_daily_counts)
    threshold = max(mean_count + OVERTRADING_STDEV_MULTIPLIER * stdev_count, OVERTRADING_MIN_TRADES_ON_DAY)

    # Only fire the moment the threshold is first crossed today, not on
    # every subsequent trade that day.
    if trades_today_so_far < threshold or (trades_today_so_far - 1) >= threshold:
        return None

    return AlertEvent(
        category="overtrading",
        severity="moderate",
        message=(
            f"You've opened {trades_today_so_far} trades today — that's above your own daily average of "
            f"~{mean_count:.1f}, matching your overtrading pattern."
        ),
        trade_id=new_trade.id,
        symbol=new_trade.symbol,
    )


def check_oversizing_alert(new_trade: TradeLedger, historical_volumes: list[float]) -> AlertEvent | None:
    """
    Purpose:    Tier-1 check — is `new_trade`'s volume an outlier by the
                exact same mean+stdev rule detect_oversizing uses, evaluated
                against the trader's OWN prior volumes (excluding this trade).
    Args:       new_trade (TradeLedger): The just-opened trade.
                historical_volumes (list[float]): Volumes of PAST trades
                    (excluding new_trade).
    Returns:    AlertEvent | None: None if there isn't enough history, sizing
                    has been perfectly consistent, or this trade isn't an outlier.
    Raises:     None.
    """
    if len(historical_volumes) < MIN_TRADES_PER_BUCKET + 2:
        return None
    mean_volume = statistics.mean(historical_volumes)
    stdev_volume = statistics.pstdev(historical_volumes)
    if stdev_volume == 0:
        return None
    threshold = mean_volume + OVERSIZING_STDEV_MULTIPLIER * stdev_volume
    if float(new_trade.volume) <= threshold:
        return None

    return AlertEvent(
        category="oversizing",
        severity="moderate",
        message=(
            f"{new_trade.symbol} sized at {float(new_trade.volume):.2f} lots is well above your typical "
            f"~{mean_volume:.2f} lots — matches your oversizing pattern."
        ),
        trade_id=new_trade.id,
        symbol=new_trade.symbol,
    )


def check_sl_tp_proximity(open_trade: TradeLedger, current_price: float) -> AlertEvent | None:
    """
    Purpose:    Tier-2 check — is the live price getting close to a real,
                user-set stop-loss or take-profit on an open trade? Never an
                invented "key level" — only the trade's own recorded
                stop_loss/take_profit.
    Args:       open_trade (TradeLedger): An open trade with a real
                    stop_loss and/or take_profit set.
                current_price (float): The symbol's current live price.
    Returns:    AlertEvent | None: None if neither level is set, or price
                    isn't within SL_TP_PROXIMITY_THRESHOLD_PCT of either.
    Raises:     None.
    """
    entry = float(open_trade.open_price)
    for level_name, level_value in (("stop-loss", open_trade.stop_loss), ("take-profit", open_trade.take_profit)):
        if level_value is None:
            continue
        level_value = float(level_value)
        total_distance = abs(entry - level_value)
        if total_distance == 0:
            continue
        remaining_distance = abs(current_price - level_value)
        proximity_pct = remaining_distance / total_distance
        if proximity_pct <= SL_TP_PROXIMITY_THRESHOLD_PCT:
            return AlertEvent(
                category="sl_tp_proximity",
                severity="high" if level_name == "stop-loss" else "low",
                message=(
                    f"{open_trade.symbol} is {proximity_pct * 100:.1f}% away from your {level_name} "
                    f"({level_value}) on trade #{open_trade.id}."
                ),
                trade_id=open_trade.id,
                symbol=open_trade.symbol,
            )
    return None


async def evaluate_trade_event(new_trade: TradeLedger, other_trades: list[TradeLedger]) -> list[AlertEvent]:
    """
    Purpose:    Run every Tier-1, event-triggered detection check against
                one just-opened trade. Deterministic, rule-based only —
                never calls Ollama or any other AI to decide whether an
                alert fires.
    Args:       new_trade (TradeLedger): The just-opened trade.
                other_trades (list[TradeLedger]): The rest of the
                    portfolio's trades (open and closed), NOT including new_trade.
    Returns:    list[AlertEvent]: Every check that fired (usually 0 or 1).
    Raises:     None.
    """
    closed = [t for t in other_trades if t.status == TradeStatus.CLOSED and t.profit is not None]

    events = []
    revenge = check_revenge_trading_alert(new_trade, closed)
    if revenge:
        events.append(revenge)

    by_day: dict[str, int] = defaultdict(int)
    for t in closed:
        by_day[t.open_time.date().isoformat()] += 1
    today_key = new_trade.open_time.date().isoformat()
    historical_daily_counts = [count for day, count in by_day.items() if day != today_key]
    trades_today_so_far = by_day.get(today_key, 0) + 1
    overtrading = check_overtrading_alert(new_trade, historical_daily_counts, trades_today_so_far)
    if overtrading:
        events.append(overtrading)

    oversizing = check_oversizing_alert(new_trade, [float(t.volume) for t in closed])
    if oversizing:
        events.append(oversizing)

    return events


async def persist_alert(portfolio_id: int, event: AlertEvent) -> Alert:
    """
    Purpose:    Save one triggered alert so it survives a reopened app —
                live delivery over /ws/alerts is best-effort, this is what
                makes an alert durable.
    Args:       portfolio_id (int): The portfolio the alert belongs to.
                event (AlertEvent): The triggered alert.
    Returns:    Alert: The persisted row.
    Raises:     None.
    """
    async with AsyncSessionLocal() as session:
        alert = Alert(
            portfolio_id=portfolio_id,
            category=event.category,
            severity=event.severity,
            message=event.message,
            trade_id=event.trade_id,
            symbol=event.symbol,
        )
        session.add(alert)
        await session.commit()
        await session.refresh(alert)
        return alert
