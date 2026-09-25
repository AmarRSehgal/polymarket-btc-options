"""Headless paper A/B: the original taker against three passive makers, same windows.

    taker_v1        main.py's EdgeFinder + Simulator, unchanged: REST poll every
                    3s, buy the ask when model > ask + fee. The control.
    maker_mid       rest quotes around the Polymarket mid (no outside information)
    maker_model     rest quotes around N(d2)
    maker_anchored  rest quotes around the mid moved by the Binance-implied change
    maker_composite the same, with BTC a spread-weighted Binance + OKX mid

All four run in one process over the same windows, so a quiet hour or a feed
outage hits every arm at once. The three makers share one websocket, one book,
one tape and one Binance feed and differ only in fair value. Everything is
appended to paper_data/*.jsonl; ab_report.py turns it into the comparison.

    env -u PYTHONPATH /opt/local/bin/python3.13 paper_ab.py
"""
from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import requests

from clob_ws import MarketStream, TapeTrade
import os

from fairvalue import Anchor, CompositeSpot, prob_sigma_c
from feed import BinanceBookTicker, OkxBBO
from main import EdgeFinder
from maker import MakerParams, desired_quotes, gate
from paper_venue import PaperVenue
from polymarket import BINANCE_API, WINDOW_SECONDS, PolymarketClient
from pricer import binary_call_price, twap_effective_seconds
from simulator import Simulator
from vol import VolEstimator

log = logging.getLogger("paper_ab")

DATA = Path(os.environ.get("PAPER_DATA") or Path(__file__).resolve().parent / "paper_data")
STEP_S = 0.2
MARKOUT_S = (5, 30, 60)


def append(name: str, row: dict):
    with open(DATA / name, "a") as f:
        f.write(json.dumps(row, default=float) + "\n")


class LoggingSimulator(Simulator):
    """The original Simulator with every entry written down. Logic untouched."""

    def try_trade(self, window_ts, side, ask_price, model_price, time_remaining,
                  fee_rate=0.0, vol_ready=True):
        pos = super().try_trade(window_ts, side, ask_price, model_price, time_remaining,
                                fee_rate, vol_ready)
        if pos is not None:
            append("taker_v1.jsonl", {"t": time.time(), **asdict(pos)})
        return pos


def seed_vol(vol: VolEstimator):
    """Warm the EWMA from REST 1m klines so a restart does not sit out 30 minutes."""
    r = requests.get(f"{BINANCE_API}/api/v3/klines",
                     params={"symbol": "BTCUSDT", "interval": "1m", "limit": 120}, timeout=10)
    r.raise_for_status()
    for k in r.json()[:-1]:
        vol.on_trade(float(k[4]), k[0] / 1000.0 + 30.0)


class MakerArm:
    def __init__(self, name: str, fv_kind: str, params: MakerParams, spot: str = "binance"):
        self.name, self.fv_kind, self.p, self.spot = name, fv_kind, params, spot
        self.venue = PaperVenue()
        self.anchor = Anchor()
        self.gates: Counter = Counter()
        self.window_ts = 0
        self.position = 0.0
        self.fills = 0

    def reset(self, window_ts: int):
        self.venue = PaperVenue()
        self.anchor.reset()
        self.window_ts = window_ts
        self.position = 0.0

    def fair_value(self, now: float, mid_c: float | None, model_p: float | None) -> float | None:
        if self.fv_kind == "mid":
            return mid_c
        if model_p is None:
            return None
        if self.fv_kind == "model":
            return model_p * 100.0
        if mid_c is not None:
            self.anchor.update(now, mid_c / 100.0, model_p)
        v = self.anchor.value(model_p)
        return None if v is None else v * 100.0

    def step(self, now: float, ctx: "Context"):
        book = ctx.stream.book
        self.venue.advance(now, book)
        comp = self.spot == "composite"
        fv = self.fair_value(now, book.mid_c, ctx.model_p_comp if comp else ctx.model_p)
        blocked = gate(now=now, window_ts=self.window_ts, strike_ok=ctx.strike > 0,
                       vol_ready=ctx.vol_ready, fv_c=fv, book_bid=book.best_bid,
                       book_ask=book.best_ask, book_updated=book.updated,
                       binance_updated=ctx.comp_updated if comp else ctx.bbo.updated,
                       uses_binance=True, p=self.p)
        self.gates[blocked or "quoting"] += 1
        if blocked:
            self.venue.cancel_all(now)
            return
        bid, ask = desired_quotes(fv, ctx.sigma_c_comp if comp else ctx.sigma_c, self.position, book.best_bid, book.best_ask, self.p)
        for side, q in (("buy", bid), ("sell", ask)):
            w = self.venue.working(side)
            if q is None:
                if w:
                    self.venue.cancel(now, w.oid)
                continue
            if w is not None:
                if abs(w.price_c - q.price_c) >= self.p.replace_ticks and now - w.sent_t >= self.p.min_order_life_s:
                    self.venue.cancel(now, w.oid)
                continue
            # Every size check counts what is still in flight, cancels included.
            s = 1 if side == "buy" else -1
            if s * self.position + self.venue.pending_size(side) + q.size > self.p.q_max + 1e-9:
                continue
            self.venue.place(now, side, q.price_c, q.size, q.tactic, fv)

    def on_trade(self, tr: TapeTrade, ctx: "Context"):
        for f in self.venue.on_trade(tr):
            s = 1 if f.side == "buy" else -1
            self.position += s * f.size
            self.fills += 1
            row = {"arm": self.name, "window_ts": self.window_ts, "t": f.t, "side": f.side,
                   "price_c": f.price_c, "size": f.size, "tactic": f.tactic,
                   "fv_at_place_c": f.fv_at_place_c, "age_s": f.t - f.placed_t,
                   "mid_c": ctx.stream.book.mid_c, "position": self.position,
                   "remaining_s": self.window_ts + WINDOW_SECONDS - f.t,
                   "btc": ctx.spot_comp if self.spot == "composite" else ctx.bbo.mid, "strike": ctx.strike,
                   "model_p": ctx.model_p_comp if self.spot == "composite" else ctx.model_p,
                   "markouts": {}}
            ctx.pending_markouts.append(row)


class Context:
    def __init__(self):
        self.poly = PolymarketClient()
        self.bbo = BinanceBookTicker()
        self.vol = VolEstimator(bar_interval=60.0, ewma_halflife=30)
        self.bbo.on_mid(lambda m, t: self.vol.on_trade(m, t))
        self.okx = OkxBBO()
        self.composite = CompositeSpot(("binance", "okx"))
        self.spot_comp = 0.0
        self.comp_updated = 0.0
        self.model_p_comp: float | None = None
        self.sigma_c_comp = 0.0
        self.stream: MarketStream | None = None
        self.market = None
        self.strike = 0.0
        self.model_p: float | None = None
        self.sigma_c = 0.0
        self.pending_markouts: list[dict] = []
        self.pending_settle: set[int] = set()

    @property
    def vol_ready(self) -> bool:
        return self.vol.bar_count >= self.vol.ewma_halflife

    def refresh_model(self, now: float):
        c = self.composite.value(now, {"binance": (self.bbo.mid, self.bbo.spread, self.bbo.updated),
                                       "okx": (self.okx.mid, self.okx.spread, self.okx.updated)})
        if c is not None:
            self.spot_comp, self.comp_updated = c, now
        if not (self.market and self.strike > 0 and self.bbo.mid > 0):
            self.model_p = self.model_p_comp = None
            return
        remaining = self.market.window_ts + WINDOW_SECONDS - now
        t_eff = twap_effective_seconds(max(remaining, 0.0), self.market.twap_lookback)
        sigma = self.vol.annual_vol
        self.model_p = binary_call_price(self.bbo.mid, self.strike, t_eff, sigma)
        self.sigma_c = prob_sigma_c(self.bbo.mid, self.strike, t_eff, sigma, 5.0)
        if c is None:
            self.model_p_comp = None
        else:
            self.model_p_comp = binary_call_price(c, self.strike, t_eff, sigma)
            self.sigma_c_comp = prob_sigma_c(c, self.strike, t_eff, sigma, 5.0)


class PaperAB:
    def __init__(self):
        DATA.mkdir(exist_ok=True)
        p = MakerParams()
        p.check()
        self.params = p
        self.ctx = Context()
        self.arms = [MakerArm("maker_mid", "mid", p), MakerArm("maker_model", "model", p),
                     MakerArm("maker_anchored", "anchored", p),
                     MakerArm("maker_composite", "anchored", p, spot="composite")]
        self.taker = EdgeFinder(bar_interval=60.0, ewma_halflife=30, bankroll=100.0,
                                max_per_market=5.0, max_loss_per_window=2.0)
        self.taker.sim = LoggingSimulator(bankroll=100.0, max_exposure_per_market=5.0,
                                          max_loss_per_window=2.0)
        self._stop = asyncio.Event()
        self._load_unsettled()

    def _load_unsettled(self):
        """Windows with fills but no settlement row, from before a restart."""
        settled = {r["window_ts"] for r in _read("settlements.jsonl")}
        for name in ("fills.jsonl", "taker_v1.jsonl"):
            for r in _read(name):
                if r["window_ts"] not in settled:
                    self.ctx.pending_settle.add(r["window_ts"])

    def _on_trade(self, tr: TapeTrade):
        for arm in self.arms:
            arm.on_trade(tr, self.ctx)

    async def _windows(self):
        ctx = self.ctx
        while not self._stop.is_set():
            wts = ctx.poly.current_window_ts()
            market = None
            for _ in range(30):
                market = await asyncio.to_thread(ctx.poly.get_market, wts)
                if market or self._stop.is_set():
                    break
                await asyncio.sleep(1)
            if market is None:
                log.warning("no market for window %d", wts)
                await asyncio.sleep(max(wts + WINDOW_SECONDS - time.time(), 1))
                continue
            strike = 0.0
            for _ in range(20):
                strike = await asyncio.to_thread(ctx.poly.get_strike, wts,
                                                 market.twap_lookback or 60.0)
                if strike > 0:
                    break
                await asyncio.sleep(1)
            ctx.market, ctx.strike = market, strike
            ctx.stream = MarketStream(market.up_token, market.down_token, self._on_trade)
            for arm in self.arms:
                arm.reset(wts)
            ctx.pending_settle.add(wts)
            append("windows.jsonl", {"window_ts": wts, "strike": strike, "t": time.time(),
                                     "twap_lookback": market.twap_lookback,
                                     "arms": ["taker_v1"] + [a.name for a in self.arms]})
            await ctx.stream.run(until=wts + WINDOW_SECONDS)
            for arm in self.arms:
                arm.venue.cancel_all(time.time())
                if arm.position:
                    log.info("%s holds %+.0f Up into resolution of %d", arm.name, arm.position, wts)

    async def _decide(self):
        ctx = self.ctx
        while not self._stop.is_set():
            now = time.time()
            if ctx.stream is not None and ctx.market is not None:
                ctx.refresh_model(now)
                for arm in self.arms:
                    if arm.window_ts == ctx.market.window_ts:
                        arm.step(now, ctx)
                self._markouts(now)
            await asyncio.sleep(STEP_S)

    def _markouts(self, now: float):
        keep = []
        mid = self.ctx.stream.book.mid_c
        for row in self.ctx.pending_markouts:
            for h in MARKOUT_S:
                if str(h) not in row["markouts"] and now >= row["t"] + h:
                    live = self.ctx.market.window_ts == row["window_ts"] and mid is not None
                    s = 1 if row["side"] == "buy" else -1
                    row["markouts"][str(h)] = s * (mid - row["price_c"]) if live else None
            if len(row["markouts"]) == len(MARKOUT_S):
                append("fills.jsonl", row)
            else:
                keep.append(row)
        self.ctx.pending_markouts = keep

    async def _settle(self):
        ctx = self.ctx
        while not self._stop.is_set():
            for wts in sorted(ctx.pending_settle):
                if not ctx.poly.can_be_settled(wts):
                    continue
                outcome = await asyncio.to_thread(ctx.poly.get_settled_outcome, wts)
                if outcome:
                    append("settlements.jsonl", {"window_ts": wts, "outcome": outcome,
                                                 "t": time.time()})
                    ctx.pending_settle.discard(wts)
                elif time.time() > wts + 6 * 3600:
                    log.warning("window %d unresolved after 6h; dropping from the poll", wts)
                    ctx.pending_settle.discard(wts)
            await asyncio.sleep(20)

    async def _status(self):
        while not self._stop.is_set():
            s = self.ctx.stream
            doc = {"t": time.time(), "window_ts": self.ctx.market.window_ts if self.ctx.market else None,
                   "binance_age_s": time.time() - self.ctx.bbo.updated if self.ctx.bbo.updated else None,
                   "book_age_s": time.time() - s.book.updated if s and s.book.updated else None,
                   "ws_messages": s.messages if s else 0, "vol": self.ctx.vol.annual_vol,
                   "vol_ready": self.ctx.vol_ready,
                   "composite": {"weights": self.ctx.composite.weights, "dropped": self.ctx.composite.dropped,
                                 "gaps": self.ctx.composite.gaps,
                                 "okx_age_s": time.time() - self.ctx.okx.updated if self.ctx.okx.updated else None},
                   "taker_v1": {"bankroll": self.taker.sim.bankroll, "trades": self.taker.sim.trade_count,
                                "open": self.taker.sim.open_count},
                   "arms": {a.name: {"fills": a.fills, "position": a.position, "gates": dict(a.gates)}
                            for a in self.arms}}
            tmp = DATA / "status.json.tmp"
            tmp.write_text(json.dumps(doc, indent=1))
            tmp.replace(DATA / "status.json")
            await asyncio.sleep(30)

    async def run(self):
        await asyncio.to_thread(seed_vol, self.ctx.vol)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._stop.set)
        tasks = [asyncio.create_task(c) for c in (
            self.ctx.bbo.run(), self.ctx.okx.run(), self._windows(), self._decide(), self._settle(), self._status(),
            self.taker.feed.run(), self.taker._poll_polymarket(), self.taker._settle_loop())]
        await self._stop.wait()
        log.info("stopping")
        for row in self.ctx.pending_markouts:
            append("fills.jsonl", row)
        if self.ctx.stream:
            self.ctx.stream.stop()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def _read(name: str) -> list[dict]:
    path = DATA / name
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            log.warning("skipping torn line in %s", name)
    return out


if __name__ == "__main__":
    import logging.handlers
    import sys
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if "--log-file" in sys.argv:
        path = sys.argv[sys.argv.index("--log-file") + 1]
        h = logging.handlers.RotatingFileHandler(path, maxBytes=20_000_000, backupCount=3)
        h.setFormatter(logging.Formatter(fmt))
        logging.basicConfig(level=logging.INFO, handlers=[h])
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
    logging.getLogger("websockets").setLevel(logging.WARNING)
    asyncio.run(PaperAB().run())
