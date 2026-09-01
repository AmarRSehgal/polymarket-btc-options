"""Live trade simulator for BTC 5-minute binary options.

Tracks hypothetical positions taken when market price is below model
fair value, resolves at window close, and reports cumulative P&L.

Risk controls:
- $100 starting bankroll
- Max $5 exposure per market (window)
- Max $15 open across ALL unsettled windows
- Max $2 loss per window (stop-loss)
"""
from __future__ import annotations

from dataclasses import dataclass

from pricer import taker_fee_per_share


@dataclass
class Position:
    window_ts: int
    side: str
    entry: float
    fee: float
    model_price: float
    edge: float
    time_remaining: float

    @property
    def cost(self) -> float:
        return self.entry + self.fee


@dataclass
class ResolvedTrade:
    window_ts: int
    side: str
    entry: float
    fee: float
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
        max_open_exposure: float | None = None,
    ):
        self.bankroll = bankroll
        self.initial_bankroll = bankroll
        self.max_exposure_per_market = max_exposure_per_market
        self.max_loss_per_window = max_loss_per_window
        # The per-window cap is not a portfolio cap. A window keeps its
        # positions until Gamma reports it settled, which lags the close by
        # the better part of a minute, so two or three windows are routinely
        # open at once -- a live 36-minute run held $14.57 against a "$5 per
        # market" limit. Bound the total explicitly.
        self.max_open_exposure = (3.0 * max_exposure_per_market
                                  if max_open_exposure is None else max_open_exposure)

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
        fee_rate: float = 0.0,
        vol_ready: bool = True,
    ) -> Position | None:
        """Buy one share at the ask if it clears fair value net of the taker fee.

        Edge is measured against ask + fee, not against the midpoint. The
        midpoint is not a price anyone can trade at, and at a 1c tick the half
        spread plus the ~1.75c/share peak fee is larger than most of the
        "edge" a 5-minute vol model will ever claim to find.
        """
        if ask_price <= 0 or model_price <= 0:
            return None
        # Before the EWMA has enough bars, annual_vol is a hardcoded 50% guess.
        # Trading on it is trading on a constant, not on a measurement.
        if not vol_ready:
            return None

        fee = taker_fee_per_share(ask_price, fee_rate) if fee_rate else 0.0
        cost = ask_price + fee
        if cost >= model_price:
            return None
        # The last 30 seconds are the worst place this model can be, not the
        # best. Backtested over 230 settled windows it wins 10% of trades there
        # (-6.6% ROI): by then the 60s TWAP is half realised, so the market
        # price is nearly resolved (Brier 0.034) while the model, which cannot
        # observe the running average, is still guessing (Brier 0.222). The
        # sophisticated late flow the sibling whale-tracker repo found is
        # trading on the settlement value itself; taking that on with a
        # Black-Scholes estimate is paying them.
        if time_remaining < 30:
            return None
        # Fat tails: standardised 5-minute returns have kurtosis ~9 against 3
        # for a normal, so N(d2) is least reliable exactly in the wings.
        if model_price < 0.20:
            return None
        if self.bankroll < cost:
            return None

        exposure = self._window_exposure.get(window_ts, 0.0)
        if exposure + cost > self.max_exposure_per_market:
            return None

        if self.open_exposure + cost > self.max_open_exposure:
            return None

        if self._window_loss.get(window_ts, 0.0) >= self.max_loss_per_window:
            return None

        pos = Position(
            window_ts=window_ts,
            side=side,
            entry=ask_price,
            fee=fee,
            model_price=model_price,
            edge=model_price - cost,
            time_remaining=time_remaining,
        )
        self.open.append(pos)
        self.bankroll -= cost
        self._window_exposure[window_ts] = exposure + cost
        return pos

    def resolve_window(self, window_ts: int, outcome: str):
        remaining = []
        for pos in self.open:
            if pos.window_ts != window_ts:
                remaining.append(pos)
                continue

            payout = 1.0 if pos.side == outcome else 0.0
            pnl = payout - pos.cost
            self.bankroll += payout

            if pnl < 0:
                self._window_loss[window_ts] = (
                    self._window_loss.get(window_ts, 0.0) + abs(pnl)
                )

            self.resolved.append(ResolvedTrade(
                window_ts=pos.window_ts,
                side=pos.side,
                entry=pos.entry,
                fee=pos.fee,
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
        return sum(t.entry + t.fee for t in self.resolved)

    @property
    def total_fees(self) -> float:
        return sum(t.fee for t in self.resolved)

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
        return sum(p.cost for p in self.open)

    @property
    def windows_traded(self) -> int:
        seen = set()
        for t in self.resolved:
            seen.add(t.window_ts)
        for p in self.open:
            seen.add(p.window_ts)
        return len(seen)

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
