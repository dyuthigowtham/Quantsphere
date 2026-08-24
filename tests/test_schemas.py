from datetime import datetime

import pytest
from pydantic import ValidationError

from app.models.database import TradeDirection
from app.models.schemas import MentorVerdict, TradeCreate


def test_trade_create_accepts_valid_payload() -> None:
    trade = TradeCreate(
        portfolio_id=1,
        symbol="EURUSD",
        direction=TradeDirection.BUY,
        volume=1.0,
        open_price=1.085,
        open_time=datetime(2026, 8, 22, 9, 30),
    )
    assert trade.symbol == "EURUSD"


def test_trade_create_rejects_non_positive_volume() -> None:
    with pytest.raises(ValidationError):
        TradeCreate(
            portfolio_id=1,
            symbol="EURUSD",
            direction=TradeDirection.BUY,
            volume=0,
            open_price=1.085,
            open_time=datetime(2026, 8, 22, 9, 30),
        )


def test_mentor_verdict_requires_core_fields() -> None:
    with pytest.raises(ValidationError):
        MentorVerdict(verdict="good")


def test_mentor_verdict_accepts_full_payload() -> None:
    verdict = MentorVerdict(
        verdict="good",
        grade="B+",
        reasoning="Entered with trend confirmation and respected stop loss discipline.",
        key_observations=["Followed the trading plan", "Exited near resistance"],
    )
    assert verdict.grade == "B+"
