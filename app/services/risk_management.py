import asyncio
import statistics
from typing import Awaitable, Callable

from app.models.database import TradeDirection, TradeLedger
from app.models.schemas import CorrelatedPosition, RiskAssessment

CORRELATION_THRESHOLD = 0.7  # standard "strong correlation" cutoff (|r| >= 0.7)
MIN_OVERLAPPING_CLOSES_FOR_CORRELATION = 20
MAX_OPEN_POSITIONS_FOR_CORRELATION_CHECK = 10
CORRELATION_FETCH_CONCURRENCY = 5  # mirrors news.py's _fetch_og_image concurrency-limiting pattern
CANDLE_INTERVAL = "15m"
CANDLE_RANGE = "5d"

CandleFetcher = Callable[[str, str, str], Awaitable[list[dict]]]


def compute_correlation(closes_a: list[float], closes_b: list[float]) -> float | None:
    """
    Purpose:    Pearson correlation between two symbols' recent closing
                prices, as a real (not fabricated) measure of how closely
                they move together. Uses stdlib statistics.correlation
                (Python >=3.10) — no new dependency required.
    Args:       closes_a (list[float]): First symbol's closes, chronological.
                closes_b (list[float]): Second symbol's closes, chronological.
    Returns:    float | None: Correlation in [-1, 1], or None if there aren't
                    enough overlapping points to compute it honestly. The two
                    series are aligned by POSITION (taking the last N closes
                    from each, both fetched with identical interval/range) —
                    an approximation, not true timestamp alignment; it
                    degrades when the two symbols don't share a trading
                    calendar (e.g. FX vs. 24/7 crypto).
    Raises:     None.
    """
    n = min(len(closes_a), len(closes_b))
    if n < MIN_OVERLAPPING_CLOSES_FOR_CORRELATION:
        return None
    try:
        return statistics.correlation(closes_a[-n:], closes_b[-n:])
    except statistics.StatisticsError:
        return None


async def assess_risk(
    candidate_symbol: str,
    candidate_direction: TradeDirection,
    candidate_stop_loss: float | None,
    candidate_entry_price: float,
    candidate_position_size: float,
    portfolio_balance: float,
    open_trades: list[TradeLedger],
    candle_fetcher: CandleFetcher,
) -> RiskAssessment:
    """
    Purpose:    Pre-trade risk read: max loss, portfolio impact, existing
                exposure, and correlation warnings against currently-open
                positions — real math on real data, never fabricated. Never
                raises; any per-symbol fetch/compute failure just omits that
                position rather than failing the whole assessment (the base
                Check My Trade score must never depend on this succeeding).
    Args:       candidate_symbol (str): Symbol being considered.
                candidate_direction (TradeDirection): Direction being considered.
                candidate_stop_loss (float | None): Planned stop-loss, if any.
                candidate_entry_price (float): Planned/actual entry price.
                candidate_position_size (float): Planned position size.
                portfolio_balance (float): Portfolio's current balance.
                open_trades (list[TradeLedger]): The portfolio's currently-open trades.
                candle_fetcher (CandleFetcher): Async (symbol, interval, range) ->
                    candles callable, injected for testability — normally
                    market_data.fetch_yahoo_candles.
    Returns:    RiskAssessment: Exposure/impact figures and correlated positions.
    Raises:     None.
    """
    candidate_notional = candidate_entry_price * candidate_position_size

    max_loss_amount = None
    max_loss_pct = None
    note = None
    if candidate_stop_loss is not None:
        max_loss_amount = abs(candidate_entry_price - candidate_stop_loss) * candidate_position_size
        max_loss_pct = round(max_loss_amount / portfolio_balance * 100, 2) if portfolio_balance > 0 else None
    else:
        note = "No stop-loss set — max loss is unbounded and can't be estimated."

    existing_exposure_pct = 0.0
    if portfolio_balance > 0 and open_trades:
        existing_notional = sum(float(t.volume) * float(t.open_price) for t in open_trades)
        existing_exposure_pct = round(existing_notional / portfolio_balance * 100, 2)

    portfolio_impact_pct = None
    if portfolio_balance > 0:
        impact_basis = max_loss_amount if max_loss_amount is not None else candidate_notional
        portfolio_impact_pct = round(impact_basis / portfolio_balance * 100, 2)

    correlated_positions: list[CorrelatedPosition] = []
    correlation_checked_count = 0

    other_symbol_trades = [t for t in open_trades if t.symbol.upper() != candidate_symbol.upper()]
    # Most-recently-opened first, capped — an arbitrary but documented bound
    # on concurrent Yahoo fetches, not a claim that older positions matter less.
    other_symbol_trades = sorted(other_symbol_trades, key=lambda t: t.open_time, reverse=True)[
        :MAX_OPEN_POSITIONS_FOR_CORRELATION_CHECK
    ]

    if other_symbol_trades:
        try:
            candidate_candles = await candle_fetcher(candidate_symbol, CANDLE_INTERVAL, CANDLE_RANGE)
        except Exception:
            candidate_candles = []
        candidate_closes = [c["close"] for c in candidate_candles]

        if candidate_closes:
            semaphore = asyncio.Semaphore(CORRELATION_FETCH_CONCURRENCY)

            async def _check(trade: TradeLedger) -> CorrelatedPosition | None:
                try:
                    async with semaphore:
                        candles = await candle_fetcher(trade.symbol, CANDLE_INTERVAL, CANDLE_RANGE)
                    closes = [c["close"] for c in candles]
                    correlation = compute_correlation(candidate_closes, closes)
                    if correlation is None:
                        return None
                    return CorrelatedPosition(
                        trade_id=trade.id,
                        symbol=trade.symbol,
                        direction=trade.direction,
                        correlation=round(correlation, 3),
                        is_high_correlation=abs(correlation) >= CORRELATION_THRESHOLD,
                    )
                except Exception:
                    return None

            results = await asyncio.gather(*(_check(t) for t in other_symbol_trades))
            correlation_checked_count = sum(1 for r in results if r is not None)
            correlated_positions = [r for r in results if r is not None and r.is_high_correlation]

    if any(p.is_high_correlation for p in correlated_positions):
        symbols = ", ".join(p.symbol for p in correlated_positions if p.is_high_correlation)
        correlation_note = f"You already have highly correlated open position(s) in: {symbols}."
        note = f"{note} {correlation_note}" if note else correlation_note

    return RiskAssessment(
        max_loss_amount=round(max_loss_amount, 2) if max_loss_amount is not None else None,
        max_loss_pct=max_loss_pct,
        candidate_notional=round(candidate_notional, 2),
        existing_exposure_pct=existing_exposure_pct,
        portfolio_impact_pct=portfolio_impact_pct,
        open_position_count=len(open_trades),
        correlation_checked_count=correlation_checked_count,
        correlated_positions=correlated_positions,
        note=note,
    )
