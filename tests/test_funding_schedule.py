"""Historical settlement schedules: changes are not missing records."""

from backtest_support import FINE, H4, START, candle, config, store, universe

from trending_basket.backtest.engine import BacktestEngine
from trending_basket.backtest.metrics import calculate_metrics
from trending_basket.data.funding_schedule import FundingSchedule
from trending_basket.strategies.benchmarks import BuyAndHoldBTC

HOUR = 3600000


def schedule(hours, start=0, end=None):
    return FundingSchedule(
        [h * HOUR for h in hours], start * HOUR, (end if end is not None else max(hours) + 1) * HOUR
    )


def test_eight_to_four_to_one_hour_changes_have_full_coverage():
    times = [0, 8, 16, 24, 28, 32, 36, 37, 38, 39]
    value = schedule(times, end=40)
    assert not value.gaps
    assert value.metadata()["intervals_ms"] == [HOUR, 4 * HOUR, 8 * HOUR]


def test_phase_bridge_is_not_a_gap():
    value = schedule([0, 1, 2, 3, 8, 16, 24])
    assert not value.gaps
    assert value.transitions == [
        {
            "start_ms": 3 * HOUR,
            "end_ms": 8 * HOUR,
            "before_interval_ms": HOUR,
            "after_interval_ms": 8 * HOUR,
        }
    ]


def test_missing_record_within_stable_regime_is_detected():
    value = schedule([0, 8, 16, 32, 40, 48])
    assert list(value.missing) == [24 * HOUR]
    assert value.missing[24 * HOUR].interval_ms == 8 * HOUR
    assert value.missing[24 * HOUR].source == "internal"


def test_multiple_missing_records_and_history_edges_are_explicit():
    value = schedule([16, 24, 32, 56, 64, 72], start=0, end=89)
    assert sorted(value.missing) == [h * HOUR for h in (0, 8, 40, 48, 80, 88)]
    assert {g.source for g in value.gaps} == {
        "internal",
        "leading_extrapolation",
        "trailing_extrapolation",
    }


def test_one_or_zero_records_cannot_prove_full_coverage():
    for times in ([], [8]):
        value = schedule(times, end=24)
        assert not value.known
        assert not value.gaps


def test_per_symbol_coverage_and_missing_range_reporting():
    # One missing settlement at hour 24 in a stable 8h schedule.
    rates = {START + h * HOUR: 0.0001 for h in (-16, -8, 0, 8, 16, 32, 40, 48, 56, 64)}
    result = BacktestEngine(
        config(3),
        store([candle(d) for d in range(-1, 3)]),
        universe(),
        {"BTCUSDT": FINE},
        {"BTCUSDT": rates},
    ).run(BuyAndHoldBTC())
    metrics = calculate_metrics(result)
    row = metrics["funding_coverage_by_symbol"]["BTCUSDT"]
    assert row["observed"] == 7
    assert row["expected"] == 8
    assert row["coverage_frac"] == 7 / 8
    assert metrics["funding_gaps"] == [
        {
            "symbol": "BTCUSDT",
            "start_ms": START + 6 * H4,
            "end_ms": START + 6 * H4,
            "interval_ms": 2 * H4,
            "missing_count": 1,
            "source": "internal",
        }
    ]
    assert metrics["btc_funding_paid_usd"] > 0


def test_current_instrument_interval_does_not_change_payments_or_coverage():
    from dataclasses import replace

    rates = {START + h * HOUR: 0.0001 for h in range(-16, 72, 8)}
    results = [
        BacktestEngine(
            config(3),
            store([candle(d) for d in range(-1, 3)]),
            universe(),
            {"BTCUSDT": replace(FINE, funding_interval_ms=period)},
            {"BTCUSDT": rates},
        ).run(BuyAndHoldBTC())
        for period in (HOUR, 4 * HOUR, 8 * HOUR)
    ]
    assert results[0] == results[1] == results[2]
    assert calculate_metrics(results[0])["funding_coverage_frac"] == 1


def test_paid_funding_is_gross_outflow_not_the_net_balance():
    rates = {START + 2 * H4: 0.01, START + 4 * H4: -0.015}
    result = BacktestEngine(
        config(1), store([candle(-1), candle(0)]), universe(), {"BTCUSDT": FINE}, {"BTCUSDT": rates}
    ).run(BuyAndHoldBTC())
    metrics = calculate_metrics(result)
    assert metrics["btc_funding_paid_usd"] == 10
    assert metrics["funding_received_usd"] == 15
    assert metrics["funding_usd"] == 5
