from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import DecisionTrainingAttempt
from app.models.schemas import DecisionTrainingAttemptCreate, DecisionTrainingSummary

# A decision point every 10 candles keeps prompts frequent enough to be a
# real training exercise without turning every replay into a quiz.
DECISION_CADENCE_BARS = 10
MAX_DECISION_POINTS_PER_REPLAY = 5
EVAL_LOOKAHEAD_BARS = 5
# Heuristic noise floor below which a price move is treated as "flat" (so
# "wait" can be scored correct) — disclosed, not fitted to any symbol.
FLAT_MOVE_THRESHOLD_PCT = 0.05
MIN_ATTEMPTS_FOR_ACCURACY = 15


def compute_decision_points(start_index: int, total_candles: int) -> list[int]:
    """
    Purpose:    Deterministic set of candle indices at which Decision Training
                should pause and prompt "what would you do?" — no randomness,
                so reloading the same trade produces the same prompts every time.
    Args:       start_index (int): The replay's starting candle index.
                total_candles (int): Total candles available for this replay.
    Returns:    list[int]: Ascending decision-point indices, capped at
                    MAX_DECISION_POINTS_PER_REPLAY and leaving room for
                    EVAL_LOOKAHEAD_BARS candles to grade the last one against.
    Raises:     None.
    """
    last_valid_index = total_candles - EVAL_LOOKAHEAD_BARS - 1
    points = []
    offset = DECISION_CADENCE_BARS
    while start_index + offset <= last_valid_index and len(points) < MAX_DECISION_POINTS_PER_REPLAY:
        points.append(start_index + offset)
        offset += DECISION_CADENCE_BARS
    return points


def grade_attempt(candles: list[dict], decision_index: int, guess: str) -> tuple[str, float, int]:
    """
    Purpose:    Grade one "what would you do here?" guess against what
                actually happened next, using real candles only. Always
                computed server-side from a fresh candle fetch — the
                persisted accuracy stat must never trust a client-reported
                outcome, even though the browser already has the future
                candles for its own instant UI feedback.
    Args:       candles (list[dict]): Real chronological OHLC candles.
                decision_index (int): Index the prompt was shown at.
                guess (str): One of "buy", "sell", "wait".
    Returns:    tuple[str, float, int]: (outcome, price_move_pct, evaluated_after_bars).
                    outcome is "correct"/"incorrect"/"inconclusive" (inconclusive
                    only if too few candles remain to grade against).
    Raises:     None.
    """
    lookahead_index = decision_index + EVAL_LOOKAHEAD_BARS
    if lookahead_index >= len(candles):
        evaluated_after_bars = max(0, len(candles) - 1 - decision_index)
        return "inconclusive", 0.0, evaluated_after_bars

    decision_close = candles[decision_index]["close"]
    future_close = candles[lookahead_index]["close"]
    price_move_pct = (future_close - decision_close) / decision_close * 100 if decision_close else 0.0

    if abs(price_move_pct) < FLAT_MOVE_THRESHOLD_PCT:
        outcome = "correct" if guess == "wait" else "incorrect"
    elif price_move_pct > 0:
        outcome = "correct" if guess == "buy" else "incorrect"
    else:
        outcome = "correct" if guess == "sell" else "incorrect"

    return outcome, round(price_move_pct, 4), EVAL_LOOKAHEAD_BARS


def compute_summary(portfolio_id: int, attempts: list[DecisionTrainingAttempt]) -> DecisionTrainingSummary:
    """
    Purpose:    Aggregate a portfolio's Decision Training attempts into an
                honestly-gated accuracy summary.
    Args:       portfolio_id (int): The portfolio these attempts belong to.
                attempts (list[DecisionTrainingAttempt]): All persisted attempts.
    Returns:    DecisionTrainingSummary: Real counts always shown; accuracy_pct
                    is None below MIN_ATTEMPTS_FOR_ACCURACY.
    Raises:     None.
    """
    total_attempts = len(attempts)
    correct = sum(1 for a in attempts if a.outcome == "correct")
    incorrect = sum(1 for a in attempts if a.outcome == "incorrect")
    inconclusive = sum(1 for a in attempts if a.outcome == "inconclusive")

    guess_counts: dict[str, int] = {"buy": 0, "sell": 0, "wait": 0}
    for a in attempts:
        if a.guess in guess_counts:
            guess_counts[a.guess] += 1

    gradeable = correct + incorrect
    has_sufficient_data = gradeable >= MIN_ATTEMPTS_FOR_ACCURACY
    accuracy_pct = round(correct / gradeable * 100, 1) if has_sufficient_data and gradeable > 0 else None

    note = None
    if not has_sufficient_data:
        note = f"Answer at least {MIN_ATTEMPTS_FOR_ACCURACY} decision points (excluding inconclusive ones) to unlock an accuracy score ({gradeable} so far)."

    return DecisionTrainingSummary(
        portfolio_id=portfolio_id,
        total_attempts=total_attempts,
        correct=correct,
        incorrect=incorrect,
        inconclusive=inconclusive,
        guess_counts=guess_counts,
        accuracy_pct=accuracy_pct,
        min_attempts_required=MIN_ATTEMPTS_FOR_ACCURACY,
        has_sufficient_data=has_sufficient_data,
        note=note,
    )


async def create_attempt(
    db: AsyncSession,
    payload: DecisionTrainingAttemptCreate,
    outcome: str,
    price_move_pct: float,
    evaluated_after_bars: int,
) -> DecisionTrainingAttempt:
    """
    Purpose:    Persist one Decision Training attempt using the server-graded
                outcome from grade_attempt — the caller must never pass a
                client-reported outcome here.
    Args:       db (AsyncSession): The active database session.
                payload (DecisionTrainingAttemptCreate): The original request.
                outcome (str): Server-computed "correct"/"incorrect"/"inconclusive".
                price_move_pct (float): Server-computed price move over the
                    lookahead window.
                evaluated_after_bars (int): Server-computed bars actually graded against.
    Returns:    DecisionTrainingAttempt: The persisted attempt.
    Raises:     None.
    """
    attempt = DecisionTrainingAttempt(
        portfolio_id=payload.portfolio_id,
        trade_id=payload.trade_id,
        symbol=payload.symbol.upper(),
        interval=payload.interval,
        range_=payload.range_,
        decision_candle_time=payload.decision_candle_time,
        guess=payload.guess,
        outcome=outcome,
        price_move_pct=price_move_pct,
        evaluated_after_bars=evaluated_after_bars,
    )
    db.add(attempt)
    await db.commit()
    await db.refresh(attempt)
    return attempt


async def list_attempts_for_portfolio(db: AsyncSession, portfolio_id: int) -> list[DecisionTrainingAttempt]:
    """
    Purpose:    Fetch every Decision Training attempt persisted for a
                portfolio, for the accuracy summary.
    Args:       db (AsyncSession): The active database session.
                portfolio_id (int): Portfolio whose attempts should be listed.
    Returns:    list[DecisionTrainingAttempt]: All persisted attempts.
    Raises:     None.
    """
    stmt = select(DecisionTrainingAttempt).where(DecisionTrainingAttempt.portfolio_id == portfolio_id)
    return list((await db.execute(stmt)).scalars().all())
