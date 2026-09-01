"""BTC 5-minute binary option edge finder with live trade simulation.

Streams Binance BTCUSDT trades, estimates real-time volatility, prices
binary options via Black-Scholes N(d2), and simulates taking positions
against Polymarket when model price exceeds market ask.

Usage:
    python main.py                              # defaults: $100 bankroll, $5/market, $2 stop
    python main.py --bankroll 500 --max-per-market 10
    python main.py --bar-interval 30            # more responsive vol
    python main.py --debug                      # connection logs
"""
import asyncio
import logging
import signal

import click
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.table import Table

from feed import BinanceFeed
from polymarket import PolymarketClient
from pricer import binary_call_price, edge_cents, taker_fee_per_share, twap_effective_seconds
from simulator import Simulator
from vol import VolEstimator

console = Console()
logger = logging.getLogger(__name__)


class EdgeFinder:
    def __init__(
        self,
        bar_interval: float,
        ewma_halflife: int,
        bankroll: float,
        max_per_market: float,
        max_loss_per_window: float,
    ):
        self.feed = BinanceFeed()
        self.vol = VolEstimator(bar_interval=bar_interval, ewma_halflife=ewma_halflife)
        self.poly = PolymarketClient()
        self.sim = Simulator(
            bankroll=bankroll,
            max_exposure_per_market=max_per_market,
            max_loss_per_window=max_loss_per_window,
        )

        self._price = 0.0
        self._trade_count = 0

        self._market = None
        self._prices = None
        self._strike = 0.0

        self._window_open_price = 0.0
        self._strike_window_ts = 0
        self._tracked_window_ts = 0
        self._clean_strike = False
        self._start_window_ts = 0

        self._last_trade_entry = ""
        self._pending_resolution: list[int] = []
        self._twap_lookback = 0.0
        self._fee_rate = 0.0

        self.feed.on_trade(self._on_trade)

    def _on_trade(self, trade):
        window_ts = self.poly.current_window_ts()
        if window_ts != self._tracked_window_ts:
            if self._start_window_ts == 0:
                self._start_window_ts = window_ts
            elif self._tracked_window_ts:
                self._pending_resolution.append(self._tracked_window_ts)

            self._window_open_price = trade.price
            self._tracked_window_ts = window_ts
            self._clean_strike = window_ts != self._start_window_ts

        self._price = trade.price
        self._trade_count += 1
        self.vol.on_trade(trade.price, trade.ts)

    async def _poll_polymarket(self):
        while True:
            try:
                await self._settle_pending()
                market = await asyncio.to_thread(self.poly.get_market)
                if market:
                    self._market = market
                    self._twap_lookback = market.twap_lookback
                    self._fee_rate = market.fee_rate

                    if self._strike_window_ts != market.window_ts:
                        strike = await asyncio.to_thread(self.poly.get_strike, market.window_ts)
                        if strike > 0:
                            self._strike = strike
                            self._strike_window_ts = market.window_ts
                            self._clean_strike = True
                    if not self._strike and self._window_open_price > 0:
                        self._strike = self._window_open_price

                    prices = await asyncio.to_thread(self.poly.get_prices, market)
                    if prices:
                        self._prices = prices
                        self._try_trades(prices)
            except Exception as e:
                logger.error("Polymarket poll error: %s", e)
            await asyncio.sleep(3)

    async def _settle_pending(self):
        """Resolve closed windows against Polymarket's own settlement."""
        still_open = []
        for wts in self._pending_resolution:
            outcome = await asyncio.to_thread(self.poly.get_settled_outcome, wts)
            if outcome is None:
                still_open.append(wts)
            else:
                self.sim.resolve_window(wts, outcome)
        self._pending_resolution = still_open

    def _model_up(self, remaining: float) -> float:
        """P(Up) accounting for the trailing-TWAP settlement."""
        t_eff = twap_effective_seconds(remaining, self._twap_lookback)
        return binary_call_price(self._price, self._strike, t_eff, self.vol.annual_vol)

    def _try_trades(self, prices):
        if not self._clean_strike or not self._price or not self._strike:
            return

        remaining = self.poly.time_remaining()
        if remaining <= 0:
            return

        model_up = self._model_up(remaining)
        model_down = 1.0 - model_up
        window_ts = self.poly.current_window_ts()

        vol_ready = self.vol.bar_count >= self.vol.ewma_halflife
        pos = self.sim.try_trade(window_ts, "up", prices.up_ask, model_up, remaining,
                                 self._fee_rate, vol_ready)
        if pos:
            self._last_trade_entry = (
                f"BUY UP @ {pos.entry:.2f} (model={pos.model_price:.3f}, edge={pos.edge*100:.1f}c)"
            )

        pos = self.sim.try_trade(window_ts, "down", prices.down_ask, model_down, remaining,
                                 self._fee_rate, vol_ready)
        if pos:
            self._last_trade_entry = (
                f"BUY DOWN @ {pos.entry:.2f} (model={pos.model_price:.3f}, edge={pos.edge*100:.1f}c)"
            )

    def _build_display(self) -> Panel:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold cyan", justify="right", min_width=20)
        grid.add_column(min_width=55)

        # -- Binance --
        if self._price:
            grid.add_row("BTC Price", f"${self._price:,.2f}")
        else:
            grid.add_row("BTC Price", "[dim]connecting...[/]")
        grid.add_row("Trades", f"{self._trade_count:,}")

        vol_label = f"{self.vol.annual_vol * 100:.1f}%"
        if self.vol.bar_count < self.vol.ewma_halflife:
            vol_label += (f" [yellow](warming {self.vol.bar_count}/{self.vol.ewma_halflife}"
                          f" bars -- not trading)[/]")
        grid.add_row("Vol (ann.)", vol_label)

        if self._price:
            em = self.vol.five_min_expected_move(self._price)
            grid.add_row("5m expected move", f"${em:,.2f}")
        if self.vol.is_regime_shift():
            grid.add_row("", "[bold red]!! REGIME SHIFT !![/]")

        # -- Polymarket --
        grid.add_row("", "")
        remaining = self.poly.time_remaining()
        grid.add_row("[bold yellow]POLYMARKET[/]", f"Window: {int(remaining)}s remaining")

        if self._market:
            q = self._market.question
            grid.add_row("Market", q[:90] if len(q) > 90 else q)
        else:
            grid.add_row("Market", "[dim]searching...[/]")

        if self._strike:
            strike_note = "" if self._clean_strike else " [dim](approx)[/]"
            grid.add_row("Strike (K)", f"${self._strike:,.2f}{strike_note} [dim](Binance proxy for Chainlink)[/]")
        if self._twap_lookback:
            grid.add_row("Settlement", f"[dim]{self._twap_lookback:.0f}s trailing TWAP (Chainlink)[/]")
        if self._fee_rate:
            grid.add_row("Taker fee", f"[dim]rate {self._fee_rate} -> up to {self._fee_rate*25:.2f}c/share[/]")

        if self._prices:
            grid.add_row("Up bid/ask", f"{self._prices.up_bid:.2f} / {self._prices.up_ask:.2f}")
            grid.add_row("Down bid/ask", f"{self._prices.down_bid:.2f} / {self._prices.down_ask:.2f}")

        # -- Model --
        grid.add_row("", "")

        if not self._clean_strike:
            grid.add_row("[bold green]MODEL[/]", "[dim]waiting for clean window boundary...[/]")
        elif self._price and self._strike and remaining > 0:
            model_up = self._model_up(remaining)
            model_down = 1.0 - model_up

            move_pct = (self._price - self._strike) / self._strike * 100
            grid.add_row("[bold green]MODEL[/]", f"Move from strike: {move_pct:+.4f}%")
            grid.add_row("P(Up)", f"{model_up:.4f}  ({model_up * 100:.1f}%)")
            grid.add_row("P(Down)", f"{model_down:.4f}  ({model_down * 100:.1f}%)")

            if self._prices:
                up_cost = self._prices.up_ask + taker_fee_per_share(self._prices.up_ask, self._fee_rate)
                down_cost = self._prices.down_ask + taker_fee_per_share(self._prices.down_ask, self._fee_rate)
                up_e = edge_cents(model_up, up_cost)
                down_e = edge_cents(model_down, down_cost)

                def _style(e):
                    if e > 2:
                        return "bold green"
                    if e < -2:
                        return "bold red"
                    return "white"

                grid.add_row("Up edge (net of fee)", f"[{_style(up_e)}]{up_e:+.1f}c[/]")
                grid.add_row("Down edge (net of fee)", f"[{_style(down_e)}]{down_e:+.1f}c[/]")
        else:
            grid.add_row("[bold green]MODEL[/]", "[dim]waiting for data...[/]")

        # -- Simulator --
        grid.add_row("", "")
        sim = self.sim
        wts = self.poly.current_window_ts()

        bk_style = "bold green" if sim.bankroll >= sim.initial_bankroll else "bold red"
        grid.add_row("[bold magenta]BANKROLL[/]", f"[{bk_style}]${sim.bankroll:.2f}[/]")
        grid.add_row(
            "This window",
            f"${sim.window_exposure(wts):.2f} / ${sim.max_exposure_per_market:.2f} exposure"
            f"  |  loss: ${sim.window_loss(wts):.2f} / ${sim.max_loss_per_window:.2f} stop",
        )
        grid.add_row("Open positions", f"{sim.open_count}  (${sim.open_exposure:.2f} at risk)")

        if sim.trade_count > 0:
            pnl_style = "bold green" if sim.total_pnl >= 0 else "bold red"
            grid.add_row("Resolved trades", str(sim.trade_count))
            grid.add_row("Win rate", f"{sim.win_count}/{sim.trade_count} ({sim.win_rate*100:.0f}%)")
            grid.add_row("Total P&L", f"[{pnl_style}]${sim.total_pnl:+.2f}[/]")
            grid.add_row("Total risked", f"${sim.total_risked:.2f}  [dim](fees ${sim.total_fees:.2f})[/]")
            if sim.total_risked > 0:
                roi = sim.total_pnl / sim.total_risked * 100
                grid.add_row("ROI", f"[{pnl_style}]{roi:+.1f}%[/]")
            grid.add_row("Max drawdown", f"${sim.max_drawdown:.2f}")
            grid.add_row("Windows traded", str(sim.windows_traded))
        else:
            grid.add_row("Status", "[dim]no trades yet[/]")

        if self._last_trade_entry:
            grid.add_row("Last entry", self._last_trade_entry)

        # Recent resolved trades
        recent = sim.resolved[-5:]
        if recent:
            grid.add_row("", "")
            grid.add_row("[bold]RECENT TRADES[/]", "")
            for t in reversed(recent):
                pnl_s = f"[green]+${t.pnl:.2f}[/]" if t.pnl > 0 else f"[red]-${abs(t.pnl):.2f}[/]"
                grid.add_row(
                    f"  {t.side.upper()}",
                    f"entry={t.entry:.2f}  model={t.model_price:.3f}  "
                    f"edge={t.edge*100:.1f}c  outcome={t.outcome}  {pnl_s}",
                )

        return Panel(grid, title="BTC 5-Min Binary Options -- Live Simulator", border_style="blue")

    async def run(self):
        feed_task = asyncio.create_task(self.feed.run())
        poly_task = asyncio.create_task(self._poll_polymarket())

        stop = asyncio.Event()

        def on_signal():
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            asyncio.get_running_loop().add_signal_handler(sig, on_signal)

        with Live(self._build_display(), console=console, refresh_per_second=2) as live:
            while not stop.is_set():
                live.update(self._build_display())
                await asyncio.sleep(0.5)

        feed_task.cancel()
        poly_task.cancel()
        await asyncio.gather(feed_task, poly_task, return_exceptions=True)


@click.command()
@click.option("--bar-interval", default=60.0, help="Vol bar interval in seconds")
@click.option("--ewma-halflife", default=30, help="EWMA half-life in bars")
@click.option("--bankroll", default=100.0, help="Starting bankroll in dollars")
@click.option("--max-per-market", default=5.0, help="Max exposure per 5-min window")
@click.option("--max-loss", default=2.0, help="Stop-loss per window in dollars")
@click.option("--debug", is_flag=True, help="Enable debug logging")
def main(
    bar_interval: float,
    ewma_halflife: int,
    bankroll: float,
    max_per_market: float,
    max_loss: float,
    debug: bool,
):
    """BTC 5-minute binary option edge finder with live trade simulation."""
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    finder = EdgeFinder(
        bar_interval=bar_interval,
        ewma_halflife=ewma_halflife,
        bankroll=bankroll,
        max_per_market=max_per_market,
        max_loss_per_window=max_loss,
    )
    asyncio.run(finder.run())


if __name__ == "__main__":
    main()
