"""Live trade simulator for BTC 5-minute binary options.

Tracks hypothetical positions taken when market price is below model
fair value, resolves at window close, and reports cumulative P&L.

Risk controls:
- $100 starting bankroll
- Max $5 exposure per market (window)
- Max $2 loss per window (stop-loss)
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Position:
    window_ts: int
    side: str
    entry: float
    model_price: float
    edge: float
    time_remaining: float


@dataclass
class ResolvedTrade:
    window_ts: int
    side: str
    entry: float
    model_price: float
    edge: float
    outcome: str
    payout: float
    pnl: float


class Simulator:
    def __init__(
        self,
        bankroll: float = 100.0,
        max_exposure_per_market: float = 5.0,
        max_loss_per_window: float = 2.0,
    ):
        self.bankroll = bankroll
        self.initial_bankroll = bankroll
        self.max_exposure_per_market = max_exposure_per_market
        self.max_loss_per_window = max_loss_per_window

        self.open: list[Position] = []
        self.resolved: list[ResolvedTrade] = []
        self._window_exposure: dict[int, float] = {}
        self._window_loss: dict[int, float] = {}

    def window_exposure(self, window_ts: int) -> float:
        return self._window_exposure.get(window_ts, 0.0)

    def window_loss(self, window_ts: int) -> float:
        return self._window_loss.get(window_ts, 0.0)

    def try_trade(
        self,
        window_ts: int,
        side: str,
        ask_price: float,
        model_price: float,
        time_remaining: float,
    ) -> Position | None:
        if ask_price <= 0 or model_price <= 0:
            return None
        if ask_price >= model_price:
            return None
        if time_remaining < 30:
            return None
        if model_price < 0.20:
            return None
        if self.bankroll < ask_price:
            return None

        exposure = self._window_exposure.get(window_ts, 0.0)
        if exposure + ask_price > self.max_exposure_per_market:
            return None

        if self._window_loss.get(window_ts, 0.0) >= self.max_loss_per_window:
            return None

        pos = Position(
            window_ts=window_ts,
            side=side,
            entry=ask_price,
            model_price=model_price,
            edge=model_price - ask_price,
            time_remaining=time_remaining,
        )
        self.open.append(pos)
        self.bankroll -= ask_price
        self._window_exposure[window_ts] = exposure + ask_price
        return pos

    def resolve_window(self, window_ts: int, outcome: str):
        remaining = []
        for pos in self.open:
            if pos.window_ts != window_ts:
                remaining.append(pos)
                continue

            payout = 1.0 if pos.side == outcome else 0.0
            pnl = payout - pos.entry
            self.bankroll += payout

            if pnl < 0:
                self._window_loss[window_ts] = (
                    self._window_loss.get(window_ts, 0.0) + abs(pnl)
                )

            self.resolved.append(ResolvedTrade(
                window_ts=pos.window_ts,
                side=pos.side,
                entry=pos.entry,
                model_price=pos.model_price,
                edge=pos.edge,
                outcome=outcome,
                payout=payout,
                pnl=pnl,
            ))

        self.open = remaining
        self._window_exposure.pop(window_ts, None)

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.resolved)

    @property
    def total_risked(self) -> float:
        return sum(t.entry for t in self.resolved)

    @property
    def trade_count(self) -> int:
        return len(self.resolved)

    @property
    def win_count(self) -> int:
        return sum(1 for t in self.resolved if t.pnl > 0)

    @property
    def win_rate(self) -> float:
        if not self.resolved:
            return 0.0
        return self.win_count / len(self.resolved)

    @property
    def open_count(self) -> int:
        return len(self.open)

    @property
    def open_exposure(self) -> float:
        return sum(p.entry for p in self.open)

    @property
    def windows_traded(self) -> int:
        seen = set()
        for t in self.resolved:
            seen.add(t.window_ts)
        for p in self.open:
            seen.add(p.window_ts)
        return len(seen)

    @property
    def peak_bankroll(self) -> float:
        bal = self.initial_bankroll
        peak = bal
        for t in self.resolved:
            bal += t.pnl
            peak = max(peak, bal)
        return peak

    @property
    def max_drawdown(self) -> float:
        bal = self.initial_bankroll
        peak = bal
        worst = 0.0
        for t in self.resolved:
            bal += t.pnl
            peak = max(peak, bal)
            worst = max(worst, peak - bal)
        return worst
