"""Bar-close decisions, next-open fills, timestamped funding and delisting events."""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Any

from trending_basket.backtest.config import Experiment
from trending_basket.backtest.market import DataStore
from trending_basket.backtest.sim_executor import InstrumentRules, SimExecutor
from trending_basket.domain.types import Candle
from trending_basket.portfolio.limits import apply_limits
from trending_basket.strategies.base import DecisionContext, Strategy, TargetPosition
from trending_basket.universe.storage import Universe


@dataclass
class BacktestResult:
    fills: list[dict[str, Any]]
    positions: list[dict[str, Any]]
    equity: list[dict[str, Any]]
    events: list[dict[str, Any]]
    btc_price_return: float | None


class BacktestEngine:
    def __init__(
        self,
        experiment: Experiment,
        data: DataStore,
        universe: Universe,
        rules: dict[str, InstrumentRules],
        funding: dict[str, dict[int, float]],
    ) -> None:
        self.config, self.data, self.universe = experiment, data, universe
        self.rules, self.funding = rules, funding
        self.funding_times = {s: sorted(values) for s, values in funding.items()}
        self.book = SimExecutor(experiment.run.initial_capital_usd, rules, experiment.costs)
        self.marks: dict[str, float] = {}

    def _delisted(self, at_ms: int) -> None:
        for symbol in sorted(self.book.positions):
            if not self.universe.is_tradeable_at(symbol, at_ms):
                price = self.data.close_price(symbol, at_ms)
                self.book.close(symbol, price, at_ms, "delisting")
                self.book.event(at_ms, "delisting", symbol, reference_price=price)

    def _fund(self, symbol: str, at_ms: int) -> None:
        if symbol not in self.book.positions:
            return
        rate = self.funding.get(symbol, {}).get(at_ms)
        if rate is None:
            self.book.funding(symbol, at_ms, None, 0.0, "missing_rate")
        else:
            price, source = self.data.funding_price(symbol, at_ms)
            self.book.funding(symbol, at_ms, rate, price, source)

    def _fund_boundary(self, at_ms: int) -> None:
        for symbol in sorted(self.book.positions):
            if at_ms % self.rules[symbol].funding_interval_ms == 0 or at_ms in self.funding.get(
                symbol, {}
            ):
                self._fund(symbol, at_ms)

    def _intrabar_events(self, at_ms: int, end_ms: int) -> None:
        events: set[tuple[int, int, str]] = set()
        for symbol in self.book.positions:
            period = self.rules[symbol].funding_interval_ms
            for time_ms in range((at_ms // period + 1) * period, end_ms, period):
                events.add((time_ms, 1, symbol))
            times = self.funding_times.get(symbol, [])
            for time_ms in times[bisect_left(times, at_ms + 1) : bisect_left(times, end_ms)]:
                events.add((time_ms, 1, symbol))
            until = self.universe.metadata["trading_periods"][symbol]["listed_until_ms"]
            if until is not None and at_ms < until <= end_ms:
                events.add((until, 0, symbol))
        for time_ms, kind, symbol in sorted(events):
            if symbol not in self.book.positions:
                continue
            if kind == 0:
                self._delisted(time_ms)
            else:
                self._fund(symbol, time_ms)

    def _decide_and_fill(self, strategy: Strategy, at_ms: int) -> None:
        allowed = tuple(self.universe.universe_at(at_ms))
        symbols = sorted(set(allowed) | set(self.book.positions))
        market = self.data.view(at_ms, symbols)
        for symbol in symbols:
            last = self.data.last_closed(symbol, self.config.run.interval, at_ms)
            if last is not None:
                self.marks[symbol] = last.close
        ctx = DecisionContext(
            at_ms,
            self.book.equity(self.marks),
            MappingProxyType(self.book.views(self.marks)),
            allowed,
            market,
        )
        requested = strategy.decide(ctx)
        targets = dict(requested)
        removal: set[str] = set()
        for symbol in sorted(set(targets) | set(self.book.positions)):
            target = targets.get(symbol, TargetPosition(0))
            position = self.book.positions.get(symbol)
            current = position.quantity * self.marks[symbol] if position else 0.0
            if not self.universe.is_tradeable_at(symbol, at_ms):
                if target.notional_usd:
                    self.book.event(at_ms, "not_tradeable", symbol)
                targets[symbol] = TargetPosition(0)
            elif symbol not in allowed:
                if self.config.limits.exit_on_universe_removal:
                    targets[symbol] = TargetPosition(0)
                    if position:
                        removal.add(symbol)
                else:
                    # Outside the universe: allow reductions/exits, never an increase or flip.
                    permitted = (
                        0.0
                        if target.notional_usd * current <= 0
                        else min(abs(target.notional_usd), abs(current))
                        * (1 if current > 0 else -1)
                    )
                    if permitted != target.notional_usd:
                        self.book.event(at_ms, "outside_universe", symbol)
                    targets[symbol] = replace(target, notional_usd=permitted)
        limited, triggered = apply_limits(targets, ctx.equity_usd, self.config.limits)
        for kind in triggered:
            self.book.event(at_ms, "limit", limit=kind)
        # Reductions before increases avoid a transient increase from the old basket.
        ordered = sorted(
            set(limited) | set(self.book.positions),
            key=lambda s: (
                abs(limited.get(s, TargetPosition(0)).notional_usd)
                > abs(ctx.positions[s].notional_usd)
                if s in ctx.positions
                else True,
                s,
            ),
        )
        for symbol in ordered:
            target = limited.get(symbol, TargetPosition(0))
            if not target.notional_usd and symbol not in self.book.positions:
                continue
            bar = self.data.bar(symbol, self.config.run.interval, at_ms)
            if bar is None:
                raise ValueError(f"missing execution bar: {symbol} at {at_ms}")
            if symbol not in self.rules:
                raise ValueError(f"missing instrument rules: {symbol}")
            reason = (
                "universe_removal"
                if symbol in removal
                else "limit"
                if target != targets.get(symbol, TargetPosition(0))
                else "signal"
            )
            self.book.rebalance(
                symbol, target, self.marks.get(symbol, bar.open), bar.open, at_ms, reason
            )
            self.marks[symbol] = bar.open

    def _stop(self, symbol: str, bar: Candle, time_ms: int, gap_only: bool) -> None:
        position = self.book.positions.get(symbol)
        if position is None or position.stop_price is None:
            return
        stop = position.stop_price
        long = position.quantity > 0
        gap = bar.open <= stop if long else bar.open >= stop
        touch = bar.low <= stop if long else bar.high >= stop
        if gap or (not gap_only and touch):
            self.book.close(symbol, bar.open if gap else stop, time_ms, "stop")
            self.book.event(time_ms, "stop", symbol, gap=gap, bar_open_time_ms=bar.open_time_ms)

    def run(self, strategy: Strategy) -> BacktestResult:
        run = self.config.run
        equity = [self.book.snapshot(run.start_ms, {})]
        for symbol, rules in sorted(self.rules.items()):
            if rules.min_notional_value is None:
                self.book.event(run.start_ms, "unknown_min_notional", symbol)
        for at in range(run.start_ms, run.end_ms, run.interval.duration_ms):
            end = at + run.interval.duration_ms
            self._delisted(at)
            # Funding at a boundary belongs to the carried position, before new orders.
            self._fund_boundary(at)
            self._decide_and_fill(strategy, at)
            bars: dict[str, Candle] = {}
            for symbol in sorted(self.book.positions):
                bar = self.data.bar(symbol, run.interval, at)
                if bar is None:
                    raise ValueError(f"missing held-position bar: {symbol} at {at}")
                bars[symbol] = bar
                self._stop(symbol, bar, at, gap_only=True)
            self._intrabar_events(at, end)
            for symbol, bar in bars.items():
                self._stop(symbol, bar, end, gap_only=False)
                self.marks[symbol] = bar.close
            equity.append(self.book.snapshot(end, self.marks))
        positions = self.book.closed_positions + [
            self.book.position_record(p, None, "open", self.marks[s])
            for s, p in sorted(self.book.positions.items())
        ]
        first = self.data.bar("BTCUSDT", run.interval, run.start_ms)
        last = self.data.last_closed("BTCUSDT", run.interval, run.end_ms)
        btc_return = last.close / first.open - 1 if first and last else None
        funding_events = [e for e in self.book.events if e["kind"] == "funding"]
        if funding_events:
            coverage = sum(e["observed"] for e in funding_events) / len(funding_events)
            if coverage < 0.99:
                self.book.event(run.end_ms, "funding_coverage_warning", coverage_frac=coverage)
        return BacktestResult(self.book.fills, positions, equity, self.book.events, btc_return)
