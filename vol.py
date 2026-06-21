"""Real-time EWMA volatility estimator from trade prices.

Aggregates trades into fixed-interval bars, computes log returns,
and maintains an exponentially weighted variance estimate.
"""
import math
from collections import deque

MINUTES_PER_YEAR = 365.25 * 24 * 60


class VolEstimator:
    def __init__(
        self,
        bar_interval: float = 60.0,
        ewma_halflife: int = 30,
        default_annual_vol: float = 0.50,
    ):
        self.bar_interval = bar_interval
        self.ewma_halflife = ewma_halflife
        self.default_annual_vol = default_annual_vol

        self._alpha = 1 - math.exp(-math.log(2) / ewma_halflife)
        self._bars_per_year = MINUTES_PER_YEAR * 60 / bar_interval

        self._current_bar_start = 0.0
        self._current_bar_prices: list[float] = []
        self._last_bar_close = 0.0

        self._ewma_var = 0.0
        self._bar_count = 0
        self._returns: deque = deque(maxlen=120)

    def on_trade(self, price: float, ts: float) -> float | None:
        """Process a trade. Returns updated annual vol when a bar completes."""
        if self._current_bar_start == 0.0:
            self._current_bar_start = ts - (ts % self.bar_interval)
            self._current_bar_prices.append(price)
            self._last_bar_close = price
            return None

        bar_end = self._current_bar_start + self.bar_interval

        if ts < bar_end:
            self._current_bar_prices.append(price)
            return None

        bar_close = self._current_bar_prices[-1] if self._current_bar_prices else self._last_bar_close

        if self._last_bar_close > 0:
            log_ret = math.log(bar_close / self._last_bar_close)
            self._returns.append(log_ret)

            if self._bar_count == 0:
                self._ewma_var = log_ret ** 2
            else:
                self._ewma_var = self._alpha * log_ret ** 2 + (1 - self._alpha) * self._ewma_var

            self._bar_count += 1

        self._last_bar_close = bar_close
        self._current_bar_start = ts - (ts % self.bar_interval)
        self._current_bar_prices = [price]
        return self.annual_vol

    @property
    def annual_vol(self) -> float:
        if self._bar_count < 5:
            return self.default_annual_vol

        blend = min(1.0, self._bar_count / 30)
        bar_vol = math.sqrt(self._ewma_var)
        annual_est = bar_vol * math.sqrt(self._bars_per_year)
        return blend * annual_est + (1 - blend) * self.default_annual_vol

    @property
    def bar_count(self) -> int:
        return self._bar_count

    def five_min_expected_move(self, price: float) -> float:
        """Expected 5-minute move in dollar terms."""
        return price * self.annual_vol * math.sqrt(5 / MINUTES_PER_YEAR)

    def is_regime_shift(self, threshold: float = 3.0) -> bool:
        if self._bar_count < 10 or not self._returns:
            return False
        bar_vol = math.sqrt(self._ewma_var)
        if bar_vol == 0:
            return False
        return abs(self._returns[-1]) > threshold * bar_vol
