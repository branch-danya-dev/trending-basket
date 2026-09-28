"""Tests for domain value types."""

from __future__ import annotations

import pytest

from trending_basket.domain.types import Candle, FundingRate, Interval, Side, TargetWeight


def test_interval_to_bybit() -> None:
    assert Interval.H4.to_bybit() == "240"
    assert Interval.D1.to_bybit() == "D"


def test_interval_duration_ms() -> None:
    assert Interval.H4.duration_ms == 4 * 60 * 60 * 1000
    assert Interval.D1.duration_ms == 24 * 60 * 60 * 1000


def _valid_candle_kwargs() -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "interval": Interval.H4,
        "open_time_ms": Interval.H4.duration_ms * 100,
        "open": 100.0,
        "high": 110.0,
        "low": 90.0,
        "close": 105.0,
        "volume": 10.0,
        "turnover": 1_000.0,
    }


def test_valid_candle() -> None:
    candle = Candle(**_valid_candle_kwargs())
    assert candle.symbol == "BTCUSDT"


@pytest.mark.parametrize(
    "overrides",
    [
        {"open": 0.0},
        {"high": -1.0},
        {"low": 0.0},
        {"close": -5.0},
    ],
)
def test_candle_rejects_non_positive_prices(overrides: dict[str, object]) -> None:
    kwargs = _valid_candle_kwargs() | overrides
    with pytest.raises(ValueError, match="positive"):
        Candle(**kwargs)


def test_candle_rejects_high_below_open_close() -> None:
    kwargs = _valid_candle_kwargs() | {"high": 100.0, "open": 100.0, "close": 105.0}
    with pytest.raises(ValueError, match="high"):
        Candle(**kwargs)


def test_candle_rejects_low_above_open_close() -> None:
    kwargs = _valid_candle_kwargs() | {"low": 101.0, "open": 100.0, "close": 105.0}
    with pytest.raises(ValueError, match="low"):
        Candle(**kwargs)


def test_candle_rejects_negative_volume() -> None:
    kwargs = _valid_candle_kwargs() | {"volume": -1.0}
    with pytest.raises(ValueError, match="volume"):
        Candle(**kwargs)


def test_candle_rejects_misaligned_open_time() -> None:
    kwargs = _valid_candle_kwargs() | {"open_time_ms": Interval.H4.duration_ms * 100 + 1}
    with pytest.raises(ValueError, match="aligned"):
        Candle(**kwargs)


def test_daily_candle_requires_utc_day_boundary() -> None:
    kwargs = _valid_candle_kwargs() | {
        "interval": Interval.D1,
        "open_time_ms": Interval.D1.duration_ms * 20,
    }
    Candle(**kwargs)

    bad_kwargs = _valid_candle_kwargs() | {
        "interval": Interval.D1,
        "open_time_ms": Interval.D1.duration_ms * 20 + 3_600_000,
    }
    with pytest.raises(ValueError, match="aligned"):
        Candle(**bad_kwargs)


def test_funding_rate_construction() -> None:
    rate = FundingRate(symbol="BTCUSDT", funding_time_ms=1_000, rate_frac=0.0001)
    assert rate.rate_frac == 0.0001


def test_side_values() -> None:
    assert Side.LONG == "LONG"
    assert Side.SHORT == "SHORT"


@pytest.mark.parametrize("weight", [-1.0, 0.0, 1.0, 0.5])
def test_target_weight_accepts_valid_range(weight: float) -> None:
    tw = TargetWeight(symbol="BTCUSDT", weight_frac=weight)
    assert tw.weight_frac == weight


@pytest.mark.parametrize("weight", [-1.0001, 1.0001, 2.0, -5.0])
def test_target_weight_rejects_out_of_range(weight: float) -> None:
    with pytest.raises(ValueError, match="weight_frac"):
        TargetWeight(symbol="BTCUSDT", weight_frac=weight)
