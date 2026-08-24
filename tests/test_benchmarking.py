from datetime import datetime, timedelta

from app.models.database import TradeDirection, TradeLedger, TradeStatus
from app.services.benchmarking import MIN_PEER_TRADERS_FOR_BENCHMARK, PeerAggregate, compute_benchmark
from app.services.trading_profile import MIN_TRADES_FOR_PROFILE

NOW = datetime(2026, 1, 15, 12, 0, 0)


def _trade(trade_id, profit):
    trade = TradeLedger(
        portfolio_id=1,
        symbol="EURUSD",
        direction=TradeDirection.BUY,
        volume=1.0,
        open_price=100.0,
        close_price=100.0 + profit,
        open_time=NOW - timedelta(days=2),
        close_time=NOW - timedelta(days=1),
        profit=profit,
        status=TradeStatus.CLOSED,
    )
    trade.id = trade_id
    return trade


def test_empty_platform_gates_on_zero_peers():
    trades = [_trade(i, 10.0) for i in range(MIN_TRADES_FOR_PROFILE)]
    result = compute_benchmark(1, trades, own_avg_realized_r=1.0, peers=[])
    assert result.has_sufficient_data is False
    assert result.peer_trader_count == 0
    assert "0" in result.note
    assert str(MIN_PEER_TRADERS_FOR_BENCHMARK) in result.note
    assert result.win_rate_percentile is None


def test_gated_when_requester_below_own_min_trades_regardless_of_peer_count():
    trades = [_trade(i, 10.0) for i in range(3)]
    peers = [PeerAggregate(win_rate_pct=50.0, avg_realized_r=1.0) for _ in range(MIN_PEER_TRADERS_FOR_BENCHMARK)]
    result = compute_benchmark(1, trades, own_avg_realized_r=1.0, peers=peers)
    assert result.has_sufficient_data is False
    assert str(MIN_TRADES_FOR_PROFILE) in result.note


def test_gated_when_peers_present_but_below_floor():
    trades = [_trade(i, 10.0) for i in range(MIN_TRADES_FOR_PROFILE)]
    peers = [PeerAggregate(win_rate_pct=50.0, avg_realized_r=1.0) for _ in range(5)]
    result = compute_benchmark(1, trades, own_avg_realized_r=1.0, peers=peers)
    assert result.has_sufficient_data is False
    assert result.peer_trader_count == 5
    assert "5 of" in result.note


def test_correct_percentile_math_at_the_median():
    # 10 wins out of 10 -> requester's win rate is 100%.
    trades = [_trade(i, 10.0) for i in range(MIN_TRADES_FOR_PROFILE)]
    # 19 peers spread 0..90 in steps of 5, plus one more at 100 -> 20 peers total.
    peer_rates = [i * 5.0 for i in range(19)] + [100.0]
    peers = [PeerAggregate(win_rate_pct=r, avg_realized_r=None) for r in peer_rates]
    result = compute_benchmark(1, trades, own_avg_realized_r=None, peers=peers)
    assert result.has_sufficient_data is True
    assert result.own_win_rate_pct == 100.0
    # Requester (100%) is >= every peer, including the other 100%-rate peer.
    assert result.win_rate_percentile == 100.0
    # No real avg_realized_r data on either side -> stays None, never guessed.
    assert result.avg_realized_r_percentile is None


def test_avg_realized_r_percentile_ignores_peers_with_no_real_r_data():
    trades = [_trade(i, 10.0) for i in range(MIN_TRADES_FOR_PROFILE)]
    peers = [PeerAggregate(win_rate_pct=50.0, avg_realized_r=None) for _ in range(MIN_PEER_TRADERS_FOR_BENCHMARK - 2)]
    peers += [PeerAggregate(win_rate_pct=50.0, avg_realized_r=0.5), PeerAggregate(win_rate_pct=50.0, avg_realized_r=1.5)]
    result = compute_benchmark(1, trades, own_avg_realized_r=1.0, peers=peers)
    assert result.has_sufficient_data is True
    # Only 2 peers have real R data; requester's 1.0 beats exactly one of them.
    assert result.avg_realized_r_percentile == 50.0


def test_requester_never_counted_among_their_own_peers():
    # compute_benchmark itself trusts the caller excluded the requester;
    # this test documents that peers is exactly what gets used for the
    # denominator, so an accidental self-inclusion would show up as a
    # peer_trader_count mismatch in integration, not silently absorbed here.
    trades = [_trade(i, 10.0) for i in range(MIN_TRADES_FOR_PROFILE)]
    peers = [PeerAggregate(win_rate_pct=50.0, avg_realized_r=1.0) for _ in range(MIN_PEER_TRADERS_FOR_BENCHMARK)]
    result = compute_benchmark(1, trades, own_avg_realized_r=1.0, peers=peers)
    assert result.peer_trader_count == MIN_PEER_TRADERS_FOR_BENCHMARK
