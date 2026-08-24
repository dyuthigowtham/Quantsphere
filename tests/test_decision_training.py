from app.models.database import DecisionTrainingAttempt
from app.services.decision_training import (
    DECISION_CADENCE_BARS,
    EVAL_LOOKAHEAD_BARS,
    MAX_DECISION_POINTS_PER_REPLAY,
    MIN_ATTEMPTS_FOR_ACCURACY,
    compute_decision_points,
    compute_summary,
    grade_attempt,
)


def _candle(close, time=0):
    return {"time": time, "open": close, "high": close, "low": close, "close": close}


def _attempt(portfolio_id=1, guess="buy", outcome="correct"):
    a = DecisionTrainingAttempt(
        portfolio_id=portfolio_id,
        trade_id=1,
        symbol="EURUSD",
        interval="1h",
        range_="1mo",
        decision_candle_time=0,
        guess=guess,
        outcome=outcome,
        price_move_pct=0.0,
        evaluated_after_bars=EVAL_LOOKAHEAD_BARS,
    )
    return a


# ---------- compute_decision_points ----------


def test_compute_decision_points_is_deterministic():
    a = compute_decision_points(start_index=10, total_candles=200)
    b = compute_decision_points(start_index=10, total_candles=200)
    assert a == b


def test_compute_decision_points_capped_at_max():
    points = compute_decision_points(start_index=0, total_candles=10_000)
    assert len(points) == MAX_DECISION_POINTS_PER_REPLAY


def test_compute_decision_points_respects_cadence():
    points = compute_decision_points(start_index=0, total_candles=10_000)
    assert points[0] == DECISION_CADENCE_BARS
    assert points[1] == DECISION_CADENCE_BARS * 2


def test_compute_decision_points_leaves_room_for_lookahead():
    points = compute_decision_points(start_index=0, total_candles=15)
    for p in points:
        assert p + EVAL_LOOKAHEAD_BARS < 15


def test_compute_decision_points_empty_when_no_room():
    points = compute_decision_points(start_index=0, total_candles=5)
    assert points == []


# ---------- grade_attempt ----------


def test_grade_attempt_buy_correct_on_rise():
    candles = [_candle(100.0 + i, time=i) for i in range(20)]
    outcome, move_pct, bars = grade_attempt(candles, decision_index=5, guess="buy")
    assert outcome == "correct"
    assert move_pct > 0
    assert bars == EVAL_LOOKAHEAD_BARS


def test_grade_attempt_sell_correct_on_fall():
    candles = [_candle(100.0 - i, time=i) for i in range(20)]
    outcome, move_pct, bars = grade_attempt(candles, decision_index=5, guess="sell")
    assert outcome == "correct"
    assert move_pct < 0


def test_grade_attempt_buy_incorrect_on_fall():
    candles = [_candle(100.0 - i, time=i) for i in range(20)]
    outcome, _, _ = grade_attempt(candles, decision_index=5, guess="buy")
    assert outcome == "incorrect"


def test_grade_attempt_wait_correct_on_flat_move():
    candles = [_candle(100.0, time=i) for i in range(20)]
    outcome, move_pct, _ = grade_attempt(candles, decision_index=5, guess="wait")
    assert outcome == "correct"
    assert abs(move_pct) < 0.01


def test_grade_attempt_wait_incorrect_on_real_move():
    candles = [_candle(100.0 + i * 2, time=i) for i in range(20)]
    outcome, _, _ = grade_attempt(candles, decision_index=5, guess="wait")
    assert outcome == "incorrect"


def test_grade_attempt_inconclusive_when_not_enough_future_candles():
    candles = [_candle(100.0, time=i) for i in range(10)]
    outcome, move_pct, bars = grade_attempt(candles, decision_index=8, guess="buy")
    assert outcome == "inconclusive"
    assert move_pct == 0.0


# ---------- compute_summary ----------


def test_compute_summary_empty_attempts():
    summary = compute_summary(portfolio_id=1, attempts=[])
    assert summary.total_attempts == 0
    assert summary.has_sufficient_data is False
    assert summary.accuracy_pct is None
    assert summary.note is not None


def test_compute_summary_gated_below_minimum():
    attempts = [_attempt(outcome="correct") for _ in range(MIN_ATTEMPTS_FOR_ACCURACY - 1)]
    summary = compute_summary(portfolio_id=1, attempts=attempts)
    assert summary.has_sufficient_data is False
    assert summary.accuracy_pct is None
    assert summary.total_attempts == MIN_ATTEMPTS_FOR_ACCURACY - 1


def test_compute_summary_accuracy_above_minimum():
    attempts = [_attempt(outcome="correct") for _ in range(10)] + [_attempt(outcome="incorrect") for _ in range(10)]
    summary = compute_summary(portfolio_id=1, attempts=attempts)
    assert summary.has_sufficient_data is True
    assert summary.accuracy_pct == 50.0
    assert summary.correct == 10
    assert summary.incorrect == 10


def test_compute_summary_inconclusive_excluded_from_gradeable_count():
    attempts = [_attempt(outcome="correct") for _ in range(14)] + [_attempt(outcome="inconclusive") for _ in range(5)]
    summary = compute_summary(portfolio_id=1, attempts=attempts)
    # 14 correct + 0 incorrect = 14 gradeable, below MIN_ATTEMPTS_FOR_ACCURACY of 15
    assert summary.has_sufficient_data is False
    assert summary.inconclusive == 5


def test_compute_summary_guess_counts_tally():
    attempts = [_attempt(guess="buy"), _attempt(guess="buy"), _attempt(guess="sell"), _attempt(guess="wait")]
    summary = compute_summary(portfolio_id=1, attempts=attempts)
    assert summary.guess_counts == {"buy": 2, "sell": 1, "wait": 1}
