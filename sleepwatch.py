"""Detect that the host slept: wall time advanced but monotonic time did not.

On macOS `time.monotonic()` is mach_absolute_time(), which stops while the
machine sleeps, and `time.time()` does not. The difference between the two
since the last check is exactly how long the lid was closed.
"""
import time


class SleepWatch:
    def __init__(self, threshold_s: float = 5.0):
        self.threshold_s = threshold_s
        self._wall, self._mono = time.time(), time.monotonic()

    def check(self) -> float:
        """Seconds slept since the last call (0.0 if under the threshold)."""
        wall, mono = time.time(), time.monotonic()
        gap = (wall - self._wall) - (mono - self._mono)
        self._wall, self._mono = wall, mono
        return gap if gap > self.threshold_s else 0.0
